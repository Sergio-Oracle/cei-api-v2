"""
Blueprint SSO — CEI client OIDC du Keycloak UNCHK (realm "UNCHK").

Routes :
  GET  /api/auth/oidc/login        — redirige vers Keycloak
  GET  /api/auth/oidc/callback     — reçoit le retour, ouvre une session CEI
  POST /api/auth/oidc/force-login  — résout un conflit de session étudiant
  POST /api/auth/oidc/exchange     — échange un jeton Keycloak (ENT) contre un
                                      jeton PASETO CEI, pour un appel serveur-
                                      à-serveur à l'API externe (voir plus bas)

Le rôle n'est jamais déduit de Keycloak (qui ne sert qu'à prouver
l'identité). Compte CEI existant → son rôle CEI. Personne inconnue de CEI
mais connue de Moodle → compte créé automatiquement, rôle déduit de Moodle
(services/provisioning.py, phase 1 du 25/09 — remplace le refus d'origine).
Inconnue de CEI et de tout Moodle → refus.
"""
import os
import secrets
from urllib.parse import urlencode

from flask import Blueprint, request, redirect, jsonify, make_response, g

import oidc_keycloak
from extensions import limiter
from helpers import utcnow
from auth_paseto import (
    create_access_token, create_refresh_token, set_refresh_cookie,
    hash_token, session_key, REFRESH_TTL, decode_token, COOKIE_NAME,
)
from api_key_auth import api_key_required, api_client_allows_role
from models import get_session, User, UserRole, TokenBlocklist
from services.provisioning import provision_from_moodle, schedule_refresh_person

# Messages de l'échange ENT (JSON) ; la connexion navigateur renvoie le même
# code dans ?sso_error= et la page de connexion affiche son propre texte.
_REFUSAL_MESSAGES = {
    'unknown_account': 'Aucun compte CEI pour cet utilisateur',
    'not_in_moodle': "Inconnu de CEI et d'aucune plateforme Moodle UNCHK — création manuelle par l'administration CEI",
    'moodle_suspended': 'Compte suspendu sur Moodle',
    'no_moodle_course': "Inscrit sur Moodle mais dans aucun cours : rôle impossible à déterminer",
    'moodle_unavailable': 'Moodle injoignable : compte impossible à créer pour le moment, réessayer plus tard',
}
from cache import cache_get, cache_set, cache_delete

oidc_bp = Blueprint('oidc', __name__)

_STATE_TTL = 300          # 5 min — le temps de se connecter sur Keycloak
_RETRY_TTL = 120          # 2 min — le temps de cliquer "continuer" sur le conflit de session


def _app_url() -> str:
    return os.getenv('APP_URL', 'http://localhost:3000').rstrip('/')


def _device_label(req) -> str:
    ua = (req.headers.get('User-Agent') or '').lower()
    if 'mobile' in ua or 'android' in ua or 'iphone' in ua:
        return 'un appareil mobile (via UNCHK SSO)'
    return 'un ordinateur (via UNCHK SSO)'


def _set_active_session(user_id: int, refresh_token: str, device_label: str, ttl_seconds: int) -> None:
    cache_set(session_key(user_id), {
        'token_hash': hash_token(refresh_token),
        'device_label': device_label,
        'since': utcnow().isoformat(),
    }, ttl=ttl_seconds)


def _issue_session_and_redirect(user: User, target: str):
    """Mint les tokens CEI exactement comme /api/auth/login, pose les deux
    cookies (refresh httpOnly + flag cei_logged_in), redirige vers `target`."""
    refresh_token = create_refresh_token(user.id)
    student_sid = hash_token(refresh_token) if user.role == UserRole.STUDENT else None
    access_token = create_access_token(user.id, user.role.value, user.email, sid=student_sid)
    if user.role == UserRole.STUDENT:
        _set_active_session(user.id, refresh_token, _device_label(request), int(REFRESH_TTL.total_seconds()))

    resp = make_response(redirect(target))
    set_refresh_cookie(resp, refresh_token)
    # Reproduit exactement AuthContext.tsx::setAuthCookie() côté serveur —
    # même nom/attributs, pour que middleware.ts l'accepte immédiatement.
    resp.set_cookie(
        'cei_logged_in', '1',
        max_age=60 * 60 * 24 * 7,
        samesite='Strict',
        secure=_app_url().startswith('https://'),
        httponly=False,
        path='/',
    )
    return resp


_DASHBOARDS = {UserRole.ADMIN: 'admin', UserRole.PROFESSOR: 'professor', UserRole.STUDENT: 'student',
               UserRole.SURVEILLANT: 'surveillant', UserRole.SUPERVISEUR: 'superviseur'}


def moodle_back(url) -> str | None:
    """Adresse de retour acceptée : https sur une plateforme Moodle UNCHK
    (*.unchk.sn ou plateforme déclarée dans CEI) — jamais une adresse
    quelconque (redirection ouverte)."""
    from urllib.parse import urlsplit
    from models import MoodleInstance
    try:
        parts = urlsplit(url or '')
    except ValueError:
        return None
    host = (parts.hostname or '').lower()
    if parts.scheme != 'https' or not host:
        return None
    if host == 'unchk.sn' or host.endswith('.unchk.sn'):
        return url
    session = get_session()
    try:
        known = {urlsplit(i.base_url).hostname for i in session.query(MoodleInstance).all()}
    finally:
        session.close()
    return url if host in known else None


def open_session_page(user: User, back: str | None):
    """Ouvre la session CEI puis mène au tableau de bord du rôle en passant
    par /lti/enter, qui retient l'adresse de retour vers Moodle. Page HTML
    plutôt que redirection HTTP : la navigation suivante part alors de CEI,
    et le cookie cei_logged_in (SameSite=Strict) l'accompagne — après une
    redirection venue de Keycloak ou de Moodle, il ne serait pas envoyé."""
    target = f"{_app_url()}/lti/enter?" + urlencode({
        'to': f"/dashboard/{_DASHBOARDS.get(user.role, 'student')}", 'back': back or '', 'k': secrets.token_urlsafe(8)})
    issued = _issue_session_and_redirect(user, target)
    resp = _relay_page(target)
    for header in issued.headers.getlist('Set-Cookie'):
        resp.headers.add('Set-Cookie', header)
    return resp


def _relay_page(target: str):
    """Page qui mène à `target` par JavaScript : navigation partie de CEI,
    donc cookies SameSite=Strict envoyés (voir open_session_page)."""
    import html
    resp = make_response(f"""<!doctype html><html lang="fr"><head><meta charset="utf-8"><title>CEI</title></head>
<body style="font-family:system-ui,sans-serif;display:flex;min-height:100vh;align-items:center;justify-content:center;color:#475569">
<p>Ouverture de CEI…</p><script>window.location.replace({target!r});</script>
<noscript><a href="{html.escape(target, quote=True)}">Continuer</a></noscript></body></html>""")
    resp.headers['Content-Type'] = 'text/html; charset=utf-8'
    resp.headers['Cache-Control'] = 'no-store'
    return resp


@oidc_bp.route('/api/auth/from-moodle', methods=['GET'])
@limiter.limit("60 per minute")
def from_moodle():
    """Bouton « CEI » du menu de Moodle (une ligne ajoutée une fois par l'admin
    Moodle, visible sur toutes les pages et dans tous les cours). Même fenêtre,
    tableau de bord du rôle, sans reconnexion :
      - session CEI déjà ouverte dans ce navigateur (cookie cei_refresh, envoyé
        car la route est sous /api/auth) → on y va directement ;
      - sinon connexion UNCHK (Keycloak, déjà ouverte par Moodle : aucun écran).
    Retour : page Moodle d'où l'on vient (Referer), sinon l'adresse passée
    en paramètre back, sinon l'accueil de la plateforme."""
    from models import MoodleInstance
    back = moodle_back(request.args.get('back')) or moodle_back(request.referrer)
    if not back:
        session = get_session()
        try:
            inst = session.query(MoodleInstance).filter_by(is_active=True).order_by(MoodleInstance.id).first()
            back = f"{inst.base_url}/my/" if inst else None
        finally:
            session.close()

    token = request.cookies.get(COOKIE_NAME)
    if token:
        session = get_session()
        try:
            payload = decode_token(token)
            user = session.get(User, int(payload.get('sub')))
            revoked = session.query(TokenBlocklist).filter_by(token_hash=hash_token(token)).first()
            active = cache_get(session_key(user.id)) if user and user.role == UserRole.STUDENT else None
            same_session = user and user.role != UserRole.STUDENT or (active or {}).get('token_hash') == hash_token(token)
            if payload.get('type') == 'refresh' and user and user.is_active and not revoked and same_session:
                schedule_refresh_person(user.id)
                target = f"{_app_url()}/lti/enter?" + urlencode({
                    'to': f"/dashboard/{_DASHBOARDS.get(user.role, 'student')}", 'back': back or '',
                    'k': secrets.token_urlsafe(8)})
                return _relay_page(target)
        except Exception:
            pass   # jeton illisible ou expiré : connexion UNCHK ci-dessous
        finally:
            session.close()
    if not _configured():
        return redirect(f"{_app_url()}/login?sso_error=not_configured")
    return redirect(f"{_app_url()}/api/auth/oidc/login?" + urlencode({'back': back or ''}))


_REQUIRED_ENVS = ('OIDC_ISSUER', 'OIDC_CLIENT_ID', 'OIDC_CLIENT_SECRET', 'OIDC_REDIRECT_URI')


def _configured() -> bool:
    return all(os.getenv(k) for k in _REQUIRED_ENVS)


@oidc_bp.route('/api/auth/oidc/enabled', methods=['GET'])
def oidc_enabled():
    """Indique à la page de connexion s'il faut afficher le bouton UNCHK :
    tant que le client Keycloak n'est pas configuré sur ce serveur, le bouton
    reste masqué au lieu de mener à une erreur."""
    return jsonify({'enabled': _configured()})


@oidc_bp.route('/api/auth/oidc/login', methods=['GET'])
@limiter.limit("30 per minute")
def oidc_login():
    if not _configured():
        return redirect(f"{_app_url()}/login?sso_error=not_configured")
    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    # back : adresse Moodle où revenir (arrivée par le bouton « CEI » de Moodle).
    cache_set(f"cei:oidc:state:{state}", {'nonce': nonce, 'back': moodle_back(request.args.get('back'))},
              ttl=_STATE_TTL)
    return redirect(oidc_keycloak.build_authorization_url(state, nonce))


@oidc_bp.route('/api/auth/oidc/callback', methods=['GET'])
@limiter.limit("30 per minute")
def oidc_callback():
    error = request.args.get('error')
    if error:
        return redirect(f"{_app_url()}/login?sso_error=denied")

    state = request.args.get('state', '')
    code = request.args.get('code', '')
    entry = cache_get(f"cei:oidc:state:{state}") if state else None
    if not entry:
        return redirect(f"{_app_url()}/login?sso_error=state_invalid")
    cache_delete(f"cei:oidc:state:{state}")

    try:
        tokens = oidc_keycloak.exchange_code_for_tokens(code)
    except Exception as e:
        print(f"[oidc] exchange_code_for_tokens échoué : {e}")
        return redirect(f"{_app_url()}/login?sso_error=token_exchange_failed")

    try:
        claims = oidc_keycloak.validate_id_token(tokens['id_token'], entry['nonce'])
    except (ValueError, KeyError) as e:
        print(f"[oidc] validate_id_token échoué : {e}")
        return redirect(f"{_app_url()}/login?sso_error=invalid_token")

    email = (claims.get('email') or '').strip().lower()
    if not email:
        return redirect(f"{_app_url()}/login?sso_error=no_email")

    session = get_session()
    try:
        # Personne inconnue de CEI mais connue de Moodle → compte créé ici
        # (services/provisioning.py). Inconnue partout → refus, comme avant.
        user, reason = provision_from_moodle(session, email)
        if not user:
            return redirect(f"{_app_url()}/login?sso_error={reason}")
        if not user.is_active:
            return redirect(f"{_app_url()}/login?sso_error=unknown_account")

        # Conflit de session — étudiants uniquement, même logique que /api/auth/login.
        if user.role == UserRole.STUDENT:
            existing = cache_get(session_key(user.id))
            if existing:
                retry_token = secrets.token_urlsafe(24)
                cache_set(f"cei:oidc:retry:{retry_token}", {'user_id': user.id}, ttl=_RETRY_TTL)
                params = {
                    'sso_conflict': '1',
                    'retry_token': retry_token,
                    'device_label': existing.get('device_label', 'un autre appareil'),
                }
                if entry.get('back'):
                    params['lti_back'] = entry['back']
                return redirect(f"{_app_url()}/login?{urlencode(params)}")

        user.last_login = utcnow()
        session.commit()
        schedule_refresh_person(user.id)   # rôle, UE, EC, formation alignés sur Moodle
        if entry.get('back'):
            return open_session_page(user, entry['back'])
        return _issue_session_and_redirect(user, f"{_app_url()}/dashboard")
    finally:
        session.close()


@oidc_bp.route('/api/auth/oidc/force-login', methods=['POST'])
@limiter.limit("30 per minute")
def oidc_force_login():
    data = request.get_json(force=True) or {}
    retry_token = data.get('retry_token', '')
    entry = cache_get(f"cei:oidc:retry:{retry_token}") if retry_token else None
    if not entry:
        return jsonify({'error': 'Lien expiré, reconnectez-vous via UNCHK.'}), 400
    cache_delete(f"cei:oidc:retry:{retry_token}")

    session = get_session()
    try:
        user = session.query(User).filter_by(id=entry['user_id']).first()
        if not user or not user.is_active:
            return jsonify({'error': 'Compte introuvable ou désactivé.'}), 404

        existing = cache_get(session_key(user.id))
        if existing and existing.get('token_hash'):
            session.add(TokenBlocklist(
                token_hash=existing['token_hash'], user_id=user.id,
                expires_at=utcnow() + REFRESH_TTL,
            ))

        user.last_login = utcnow()
        session.commit()
        # Ici on répond en JSON (appelé via fetch depuis la page /login),
        # pas en redirection HTTP — le frontend fait router.push('/dashboard') lui-même.
        # On ne réutilise que les Set-Cookie de _issue_session_and_redirect,
        # jamais ses en-têtes de redirection (Location/Content-Type) qui n'ont
        # pas de sens sur une réponse JSON.
        redirect_resp = _issue_session_and_redirect(user, f"{_app_url()}/dashboard")
        json_resp = jsonify({'success': True})
        for cookie_header in redirect_resp.headers.getlist('Set-Cookie'):
            json_resp.headers.add('Set-Cookie', cookie_header)
        return json_resp
    finally:
        session.close()


@oidc_bp.route('/api/auth/oidc/exchange', methods=['POST'])
@api_key_required
@limiter.limit("30 per minute")
def oidc_exchange():
    """Échange serveur-à-serveur : un backend ENT qui a déjà authentifié un
    utilisateur via le Keycloak UNCHK (donc possède un access_token Keycloak
    valide pour lui) l'échange ici contre un jeton PASETO CEI pour ce même
    utilisateur — sans jamais lui redemander ses identifiants CEI. Protégé
    uniquement par la clé API (X-CEI-API-Key) : à ce stade, l'appelant n'a
    par définition pas encore de session CEI, donc pas de @paseto_required.
    """
    data = request.get_json(silent=True) or {}
    keycloak_access_token = (data.get('keycloak_access_token') or '').strip()
    if not keycloak_access_token:
        return jsonify({'error': "Champ 'keycloak_access_token' requis"}), 400

    try:
        userinfo = oidc_keycloak.get_userinfo(keycloak_access_token)
    except Exception:
        return jsonify({'error': 'Jeton Keycloak invalide ou expiré'}), 401

    email = (userinfo.get('email') or '').strip().lower()
    if not email:
        return jsonify({'error': "Le jeton Keycloak ne contient pas d'email"}), 400

    session = get_session()
    try:
        user, reason = provision_from_moodle(session, email)
        if not user:
            return jsonify({'error': _REFUSAL_MESSAGES.get(reason, 'Aucun compte CEI pour cet utilisateur'),
                            'reason': reason}), 404
        if not user.is_active:
            return jsonify({'error': 'Aucun compte CEI actif pour cet utilisateur'}), 404

        role_value = user.role.value
        if not api_client_allows_role(g.api_client, role_value):
            return jsonify({'error': f"Cette clé API n'est pas autorisée pour le module {role_value}"}), 403

        user.last_login = utcnow()
        session.commit()

        access_token = create_access_token(user.id, role_value, user.email)
        return jsonify({
            'success': True,
            'access_token': access_token,
            'expires_in': 3600,
            'user': {
                'id': user.id,
                'email': user.email,
                'full_name': user.full_name,
                'role': role_value,
            },
        })
    finally:
        session.close()
