"""
Routes LTI 1.3 (phase 6) — voir services/lti.py pour l'organisation.

Parcours d'un clic sur l'activité « CEI » d'un cours Moodle (lancement
« Fenêtre existante » : CEI remplace Moodle dans la même fenêtre) :
  1. /api/lti/login   : Moodle initie la connexion → redirection vers Moodle
                        avec state + nonce (Redis, usage unique).
  2. /api/lti/launch  : Moodle poste le jeton signé → vérifié, personne
                        identifiée par son email (même règle que le SSO :
                        compte créé si connu de Moodle). Répond une page qui
                        ouvre la session CEI.
  3. /api/lti/session : échange un code à usage unique (2 min) contre la
                        session CEI (mêmes cookies que la connexion normale),
                        puis /lti/enter mène au tableau de bord du rôle.
/lti/enter garde l'adresse de retour vers Moodle : la flèche « Retour » du
navigateur, le bouton « Retour à Moodle » et la déconnexion y ramènent.
Si Moodle affiche malgré tout l'activité dans un cadre, la page propose un
bouton qui fait passer CEI en pleine fenêtre.
/api/lti/register : enregistrement dynamique (l'admin colle une adresse
dans Moodle, CEI retient lui-même son identifiant client et son déploiement).
"""
import html
import secrets
from urllib.parse import urlencode

from flask import Blueprint, request, jsonify, redirect, make_response

from auth_paseto import paseto_required, get_current_user_id, session_key
from extensions import limiter
from helpers import require_admin, utcnow
from cache import cache_get, cache_set, cache_delete
from models import get_session, User, UserRole, OnlineExam, LtiLineItem
from routes.oidc import _app_url, _issue_session_and_redirect
from services import lti
from services.lti import LtiError
from services.provisioning import provision_from_moodle

lti_bp = Blueprint('lti', __name__)

_CODE_TTL = 120


def _page(title: str, body: str, status: int = 200):
    """Page HTML minimale (hors Next.js) pour les étapes du lancement."""
    resp = make_response(f"""<!doctype html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(title)}</title>
<style>body{{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:#f8fafc;color:#0f172a;
display:flex;min-height:100vh;align-items:center;justify-content:center;margin:0;padding:16px}}
.box{{background:#fff;border:1px solid #e2e8f0;border-radius:14px;padding:28px;max-width:520px;text-align:center}}
h1{{font-size:20px;margin:0 0 10px}}p{{color:#475569;line-height:1.6;margin:0 0 16px}}
a.btn{{display:inline-block;background:#3b82f6;color:#fff;text-decoration:none;padding:12px 20px;border-radius:10px;font-weight:700}}</style>
</head><body><div class="box">{body}</div></body></html>""", status)
    resp.headers['Content-Type'] = 'text/html; charset=utf-8'
    resp.headers['Cache-Control'] = 'no-store'
    return resp


def _framable_by(resp, platform_url: str):
    """La page de lancement peut être affichée dans un cadre de la plateforme
    Moodle (et seulement elle) : elle y propose alors d'ouvrir CEI dans un
    onglet. Toutes les autres pages CEI restent interdites de cadre."""
    resp.headers['Content-Security-Policy'] = (
        "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
        f"img-src 'self' data:; frame-ancestors 'self' {platform_url}")
    return resp


def _error_page(message: str, status: int = 400):
    return _page('CEI — lancement impossible',
                 f"<h1>Impossible d'ouvrir CEI</h1><p>{html.escape(message)}</p>", status)


@lti_bp.route('/api/lti/jwks', methods=['GET'])
def lti_jwks():
    """Clé publique de CEI, relue par Moodle pour vérifier ses demandes de jeton."""
    return jsonify(lti.jwks())


@lti_bp.route('/api/lti/login', methods=['GET', 'POST'])
@limiter.limit("60 per minute")
def lti_login():
    params = request.values.to_dict()
    session = get_session()
    try:
        return redirect(lti.login_redirect(session, params, f"{_app_url()}/api/lti/launch"))
    except LtiError as e:
        return _error_page(str(e))
    finally:
        session.close()


@lti_bp.route('/api/lti/launch', methods=['POST'])
@limiter.limit("60 per minute")
def lti_launch():
    session = get_session()
    try:
        inst, claims = lti.validate_launch(session, request.form.get('id_token', ''), request.form.get('state', ''))
        email = (claims.get('email') or '').strip().lower()
        if not email:
            return _framable_by(_error_page("Moodle n'a pas transmis votre adresse email. L'administrateur Moodle doit "
                                            "régler l'outil CEI sur « Partager l'adresse email du lanceur : Toujours »."),
                                inst.base_url)
        back = lti.return_url(inst, claims)
        user, reason = provision_from_moodle(session, email)
        if not user or not user.is_active:
            return _framable_by(_error_page("Votre compte n'a pas pu être ouvert dans CEI. Contactez l'administration CEI.", 403),
                                inst.base_url)

        # Session étudiante déjà ouverte ailleurs : même confirmation que le SSO.
        if user.role == UserRole.STUDENT and cache_get(session_key(user.id)):
            existing = cache_get(session_key(user.id)) or {}
            retry_token = secrets.token_urlsafe(24)
            cache_set(f"cei:oidc:retry:{retry_token}", {'user_id': user.id}, ttl=120)
            target = f"{_app_url()}/login?" + urlencode({
                'sso_conflict': '1', 'retry_token': retry_token, 'lti_back': back,
                'device_label': existing.get('device_label', 'un autre appareil')})
        else:
            code_value = secrets.token_urlsafe(32)
            cache_set(f"cei:lti:code:{code_value}", {'user_id': user.id, 'back': back}, ttl=_CODE_TTL)
            target = f"{_app_url()}/api/lti/session?code={code_value}"

        safe = html.escape(target, quote=True)
        return _framable_by(_page('CEI', f"""<h1>Centre d'Examen Intelligent</h1>
<p id="msg">Ouverture de CEI…</p>
<p><a class="btn" id="go" href="{safe}" target="_top" style="display:none">Ouvrir CEI</a></p>
<script>
  // Normalement CEI remplace Moodle (lancement « Fenêtre existante »). Si
  // Moodle l'a placé dans un cadre, le navigateur n'autorise la sortie du
  // cadre que sur un clic : on affiche alors un bouton.
  if (window.top !== window.self) {{
    document.getElementById('msg').textContent = "Cliquez pour ouvrir CEI en pleine fenêtre (caméra et plein écran des examens).";
    document.getElementById('go').style.display = 'inline-block';
  }} else {{ window.location.replace({target!r}); }}
</script>"""), inst.base_url)
    except LtiError as e:
        return _error_page(str(e))
    finally:
        session.close()


_DASHBOARDS = {UserRole.ADMIN: 'admin', UserRole.PROFESSOR: 'professor', UserRole.STUDENT: 'student',
               UserRole.SURVEILLANT: 'surveillant', UserRole.SUPERVISEUR: 'superviseur'}


@lti_bp.route('/api/lti/session', methods=['GET'])
@limiter.limit("60 per minute")
def lti_session():
    code = request.args.get('code', '')
    entry = cache_get(f"cei:lti:code:{code}") if code else None
    if not entry:
        return _error_page("Lien expiré. Cliquez à nouveau sur l'activité « CEI » dans Moodle.")
    cache_delete(f"cei:lti:code:{code}")
    session = get_session()
    try:
        user = session.get(User, entry['user_id'])
        if not user or not user.is_active:
            return _error_page("Compte CEI introuvable ou désactivé.", 403)
        user.last_login = utcnow()
        session.commit()
        target = f"{_app_url()}/lti/enter?" + urlencode({
            'to': f"/dashboard/{_DASHBOARDS.get(user.role, 'student')}",
            'back': entry.get('back') or '', 'k': secrets.token_urlsafe(8)})
        # Cookies posés ici (navigation de premier niveau sur CEI), puis
        # redirection par la page elle-même (replace : cette étape ne reste pas
        # dans l'historique du navigateur).
        issued = _issue_session_and_redirect(user, target)
        page = _page('CEI', f"<p>Ouverture de CEI…</p><script>window.location.replace({target!r});</script>")
        for header in issued.headers.getlist('Set-Cookie'):
            page.headers.add('Set-Cookie', header)
        return page
    finally:
        session.close()


@lti_bp.route('/api/lti/register', methods=['GET'])
@limiter.limit("20 per minute")
def lti_register():
    """Enregistrement dynamique LTI : Moodle ouvre cette page (dans une fenêtre
    ou un cadre) avec openid_configuration et registration_token."""
    session = get_session()
    try:
        inst = lti.register_dynamic(session, request.args.get('openid_configuration', ''),
                                    request.args.get('registration_token', ''), _app_url())
        return _framable_by(_page('CEI enregistré', f"""<h1>CEI est enregistré dans {html.escape(inst.name)}</h1>
<p>Identifiant client et déploiement retenus automatiquement par CEI. Dans Moodle, activez l'outil
(« Gérer les outils »), puis lancez la synchronisation depuis CEI.</p>
<script>
  setTimeout(function () {{
    (window.opener || window.parent).postMessage({{subject: 'org.imsglobal.lti.close'}}, '*');
  }}, 2500);
</script>"""), inst.base_url)
    except LtiError as e:
        return _framable_by(_error_page(str(e)), "https://*.unchk.sn")
    except Exception as e:
        return _framable_by(_error_page(f"Enregistrement impossible : {e}"), "https://*.unchk.sn")
    finally:
        session.close()


# ── Administration ──────────────────────────────────────────────────────────

@lti_bp.route('/api/admin/moodle/lti/tool-config', methods=['GET'])
@paseto_required
def lti_tool_config():
    """Valeurs à recopier dans Moodle pour enregistrer CEI comme outil LTI 1.3."""
    session = get_session()
    try:
        if not require_admin(session):
            return jsonify({'error': 'Accès réservé aux administrateurs'}), 403
        base = _app_url()
        return jsonify({
            'registration_url': f"{base}/api/lti/register",
            'tool_url': f"{base}/api/lti/launch",
            'initiate_login_url': f"{base}/api/lti/login",
            'redirection_uris': f"{base}/api/lti/launch",
            'public_keyset_url': f"{base}/api/lti/jwks",
            'lti_version': 'LTI 1.3', 'public_key_type': 'Keyset URL',
        })
    finally:
        session.close()


@lti_bp.route('/api/admin/moodle/lti/grades/<int:exam_id>', methods=['POST'])
@paseto_required
def lti_push_grades(exam_id):
    """Dépôt (ou nouveau dépôt) des notes publiées d'un examen dans Moodle.
    Automatique à la publication ; ceci sert au rattrapage. dry_run=true par défaut."""
    session = get_session()
    try:
        if not require_admin(session):
            return jsonify({'error': 'Accès réservé aux administrateurs'}), 403
        dry_run = (request.get_json(silent=True) or {}).get('dry_run', True) is not False
        return jsonify({'dry_run': dry_run, **lti.push_exam_scores(session, exam_id, dry_run=dry_run)})
    except LtiError as e:
        return jsonify({'error': str(e)}), 502
    finally:
        session.close()


@lti_bp.route('/api/admin/moodle/lti/grades', methods=['GET'])
@paseto_required
def lti_grades_status():
    """Examens aux résultats publiés et état de leur colonne de notes Moodle."""
    session = get_session()
    try:
        if not require_admin(session):
            return jsonify({'error': 'Accès réservé aux administrateurs'}), 403
        rows = {r.exam_id: r for r in session.query(LtiLineItem).all()}
        exams = (session.query(OnlineExam).filter(OnlineExam.results_published.is_(True))
                 .order_by(OnlineExam.end_time.desc()).limit(100).all())
        out = []
        for ex in exams:
            r = rows.get(ex.id)
            out.append({'exam_id': ex.id, 'title': ex.title, 'ec_id': ex.subject.ec_id if ex.subject else None,
                        'pushed_at': r.pushed_at.isoformat() if r and r.pushed_at else None,
                        'pushed_count': r.pushed_count if r else 0, 'last_error': r.last_error if r else None})
        return jsonify({'exams': out})
    finally:
        session.close()
