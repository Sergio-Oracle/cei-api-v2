"""
Blueprint Auth — Contrôleur MVC.

Routes : register, login, refresh, logout, public-key, me,
         profile (GET/PUT), profile/password, forgot-password, reset-password.

Migré depuis app.py — logique identique, zéro regression.
"""
from flask import Blueprint, request, jsonify, make_response
from datetime import datetime, timedelta, timezone
from threading import Thread
from flask_limiter.util import get_remote_address
import os

from extensions import bcrypt, limiter
from helpers    import utcnow
from auth_paseto import (
    paseto_required, get_current_user_id,
    create_access_token, create_refresh_token,
    set_refresh_cookie, clear_refresh_cookie,
    get_refresh_token_from_cookie, hash_token,
    ACCESS_TTL, REFRESH_TTL, session_key,
)
from auth_paseto import decode_token as paseto_decode_token
from models import get_session, User, UserRole, TokenBlocklist, Formation, Semester, UE, StudentUEEnrollment
from utils  import (
    send_account_created_email, send_password_reset_email,
    send_password_changed_email,
)
from cache import cache_get, cache_set, cache_delete

auth_bp = Blueprint('auth', __name__)

# ── Session unique par compte ────────────────────────────────────────────────
# Retour utilisateur (24/08) : un étudiant peut se connecter sur plusieurs
# appareils avec le même compte pendant un examen (ex. un tiers répond depuis
# un autre appareil). Une nouvelle connexion est bloquée tant qu'une session
# est déjà active ailleurs, sauf si l'appelant confirme explicitement
# (force=True) vouloir déconnecter l'autre appareil. Stocké en Redis (pas en
# base) — c'est un état transitoire, pas une donnée à conserver.
# La clé (session_key) vit maintenant dans auth_paseto.py, pas ici — pour que
# paseto_required puisse la revérifier à chaque requête (correctif 29/08,
# voir son commentaire pour le détail du bug que ça referme).
_session_key = session_key  # alias local — évite de toucher tous les appels ci-dessous


def _device_label(req) -> str:
    """Étiquette lisible dérivée du User-Agent — juste assez pour que
    l'étudiant reconnaisse SON autre appareil (pas une empreinte précise)."""
    ua = (req.headers.get('User-Agent') or '').lower()
    if 'iphone' in ua or 'ipad' in ua: os_label = 'iOS'
    elif 'android' in ua: os_label = 'Android'
    elif 'mac os' in ua: os_label = 'Mac'
    elif 'windows' in ua: os_label = 'Windows'
    elif 'linux' in ua: os_label = 'Linux'
    else: os_label = 'Appareil inconnu'
    if 'edg/' in ua: browser = 'Edge'
    elif 'chrome' in ua: browser = 'Chrome'
    elif 'firefox' in ua: browser = 'Firefox'
    elif 'safari' in ua: browser = 'Safari'
    else: browser = 'Navigateur'
    return f'{browser} sur {os_label}'


def _set_active_session(user_id: int, refresh_token: str, device_label: str, ttl_seconds: int) -> None:
    cache_set(_session_key(user_id), {
        'token_hash': hash_token(refresh_token),
        'device_label': device_label,
        'since': utcnow().isoformat(),
    }, ttl=ttl_seconds)


def _link_student_to_formation(session, student, formation_id):
    """Rattache un étudiant à une Formation (hiérarchie Pôle → Niveau →
    Formation → Semestre → UE) à l'inscription publique — renseigne
    formation_id, synchronise le niveau, inscrit à toutes les UE de la
    formation. Même logique que admin_users._link_student_to_formation :
    évite qu'un étudiant auto-inscrit se retrouve "Sans pôle"."""
    formation = session.query(Formation).filter_by(id=formation_id).first()
    if not formation:
        return
    student.formation_id = formation_id
    if formation.niveau:
        student.niveau = formation.niveau.code[:5]
    for sem in session.query(Semester).filter_by(formation_id=formation_id).all():
        for ue in session.query(UE).filter_by(semester_id=sem.id).all():
            if not session.query(StudentUEEnrollment).filter_by(student_id=student.id, ue_id=ue.id).first():
                session.add(StudentUEEnrollment(student_id=student.id, ue_id=ue.id))


# ── Inscription ───────────────────────────────────────────────────────────────
@auth_bp.route('/api/auth/register', methods=['POST'])
@limiter.limit("10 per hour")
def register():
    session = get_session()
    try:
        data = request.get_json(silent=True) or {}
        if session.query(User).filter_by(email=data.get('email', '')).first():
            return jsonify({'error': 'Cet email est déjà utilisé'}), 400

        hashed = bcrypt.generate_password_hash(data['password']).decode('utf-8')
        user   = User(
            email=data['email'], password_hash=hashed,
            full_name=data['full_name'], role=UserRole.STUDENT,
        )
        session.add(user); session.flush()
        formation_id = data.get('formation_id')
        if formation_id:
            _link_student_to_formation(session, user, formation_id)
        session.commit()
        user_dict = user.to_dict()
        # Envoi en tâche de fond — ne doit pas faire attendre l'étudiant qui s'inscrit.
        Thread(target=send_account_created_email, args=(data['email'], data['full_name'], 'student'), daemon=True).start()
        return jsonify({'success': True, 'message': 'Inscription réussie', 'user': user_dict}), 201
    except Exception as e:
        session.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        session.close()


def _login_rate_limit_key():
    """Limite par COMPTE tente (email), pas par IP source.

    Avant ce correctif, le login etait soumis a la limite par defaut du
    limiter (60/min, 300/h, indexee par IP) : 1000 etudiants derriere une
    seule IP publique (reseau d'etablissement) se partageaient ce quota,
    les premiers passaient, les 940 suivants recevaient 429 des la
    premiere minute. Indexer par email tente protege chaque compte
    individuellement contre le brute-force (l'objectif reel de cette
    limite) sans jamais penaliser les utilisateurs legitimes qui partagent
    juste le meme reseau.
    """
    try:
        data = request.get_json(silent=True) or {}
        email = (data.get('email') or '').strip().lower()
    except Exception:
        email = ''
    return email or get_remote_address()


# ── Connexion ─────────────────────────────────────────────────────────────────
@auth_bp.route('/api/auth/login', methods=['POST'])
@limiter.limit("10 per minute; 50 per hour", key_func=_login_rate_limit_key)
def login():
    try:
        data     = request.json or {}
        email    = (data.get('email') or '').strip().lower()
        password = data.get('password', '')
        session  = get_session()

        user = session.query(User).filter_by(email=email).first()
        if not user or not bcrypt.check_password_hash(user.password_hash, password):
            session.close()
            return jsonify({'error': 'Email ou mot de passe incorrect'}), 401
        if not user.is_active:
            session.close()
            return jsonify({'error': "Votre compte a été désactivé par l'administrateur. Contactez l'administration de la plateforme pour le réactiver."}), 403

        # Session unique — étudiants uniquement (voir _session_key ci-dessus).
        # Le personnel (professeur/surveillant/superviseur/admin) est
        # légitimement connecté sur plusieurs appareils sans risque de fraude
        # associé — restreindre là créerait une gêne sans bénéfice réel.
        if user.role == UserRole.STUDENT:
            existing = cache_get(_session_key(user.id))
            if existing and not data.get('force'):
                session.close()
                return jsonify({
                    'error': 'Vous êtes déjà connecté sur un autre appareil.',
                    'session_conflict': True,
                    'device_label': existing.get('device_label', 'un autre appareil'),
                    'since': existing.get('since'),
                }), 409
            if existing and data.get('force'):
                old_hash = existing.get('token_hash')
                if old_hash:
                    session.add(TokenBlocklist(
                        token_hash=old_hash, user_id=user.id,
                        expires_at=utcnow() + REFRESH_TTL,
                    ))

        # Re-hash paresseux : le cout bcrypt est integre au hash stocke, donc
        # abaisser BCRYPT_LOG_ROUNDS n'accelere jamais retroactivement les
        # comptes existants (impossible sans connaitre le mot de passe en
        # clair). Standard de l'industrie : re-hasher au prochain login
        # reussi, de facon transparente, jusqu'a ce que toute la base soit
        # migree naturellement vers le nouveau cout au fil des connexions.
        current_rounds = int(os.getenv('BCRYPT_LOG_ROUNDS', '10'))
        try:
            stored_rounds = int(user.password_hash.split('$')[2])
        except Exception:
            stored_rounds = None
        if stored_rounds is not None and stored_rounds != current_rounds:
            user.password_hash = bcrypt.generate_password_hash(password).decode('utf-8')

        user.last_login   = utcnow()
        session.commit()
        # Correctif 29/08 — refresh_token créé AVANT access_token pour que ce
        # dernier puisse porter le même sid (hash du refresh) que celui posé
        # dans cei:session:active ci-dessous ; paseto_required compare les
        # deux à chaque requête pour détecter une session remplacée ailleurs.
        refresh_token = create_refresh_token(user.id)
        student_sid = hash_token(refresh_token) if user.role == UserRole.STUDENT else None
        access_token  = create_access_token(user.id, user.role.value, user.email, sid=student_sid)
        if user.role == UserRole.STUDENT:
            _set_active_session(user.id, refresh_token, _device_label(request), int(REFRESH_TTL.total_seconds()))
        user_dict     = user.to_dict(); session.close()

        resp = make_response(jsonify({
            'success': True, 'access_token': access_token, 'user': user_dict,
            'expires_in': int(ACCESS_TTL.total_seconds()),
        }))
        set_refresh_cookie(resp, refresh_token)
        return resp, 200
    except Exception as e:
        try: session.rollback(); session.close()
        except Exception: pass
        return jsonify({'error': str(e)}), 500


# ── Refresh ───────────────────────────────────────────────────────────────────
@auth_bp.route('/api/auth/refresh', methods=['POST'])
@limiter.limit("30 per minute")
def refresh_token_endpoint():
    token = get_refresh_token_from_cookie()
    if not token:
        return jsonify({'error': 'Refresh token manquant'}), 401
    session = get_session()
    try:
        token_hash = hash_token(token)
        if session.query(TokenBlocklist).filter_by(token_hash=token_hash).first():
            session.close()
            return jsonify({'error': 'Token révoqué — reconnectez-vous'}), 401

        payload = paseto_decode_token(token)
        if payload.get('type') != 'refresh':
            raise ValueError("Ce n'est pas un refresh token")

        user = session.query(User).filter_by(id=int(payload['sub'])).first()
        if not user or not user.is_active:
            session.close()
            return jsonify({'error': 'Utilisateur introuvable'}), 401

        block = TokenBlocklist(
            token_hash=token_hash, user_id=user.id,
            expires_at=datetime.fromisoformat(payload['exp']),
        )
        session.add(block); session.commit()
        # Correctif 29/08 — même raisonnement que login() : sid = hash du
        # nouveau refresh token, synchronisé avec cei:session:active ci-dessous.
        new_refresh = create_refresh_token(user.id)
        new_student_sid = hash_token(new_refresh) if user.role == UserRole.STUDENT else None
        new_access  = create_access_token(user.id, user.role.value, user.email, sid=new_student_sid)
        if user.role == UserRole.STUDENT:
            # Garde le marqueur de session unique synchronisé avec le token
            # tout juste renouvelé — sinon une déconnexion forcée ultérieure
            # blocklisterait un hash déjà obsolète. Toujours réécrit (pas
            # seulement si un marqueur existait déjà) : auto-guérison si
            # Redis a été vidé/redémarré entre-temps.
            _set_active_session(user.id, new_refresh, _device_label(request), int(REFRESH_TTL.total_seconds()))
        session.close()

        resp = make_response(jsonify({
            'access_token': new_access,
            'expires_in': int(ACCESS_TTL.total_seconds()),
        }))
        set_refresh_cookie(resp, new_refresh)
        return resp, 200
    except ValueError as e:
        session.close()
        return jsonify({'error': str(e)}), 401
    except Exception as e:
        session.close()
        return jsonify({'error': str(e)}), 500


# ── Logout ────────────────────────────────────────────────────────────────────
@auth_bp.route('/api/auth/logout', methods=['POST'])
@paseto_required
def logout():
    token = get_refresh_token_from_cookie()
    if token:
        session = get_session()
        try:
            payload = paseto_decode_token(token)
            block   = TokenBlocklist(
                token_hash=hash_token(token),
                user_id=get_current_user_id(),
                expires_at=datetime.fromisoformat(payload['exp']),
            )
            session.add(block); session.commit()
        except Exception:
            pass
        finally:
            session.close()
    cache_delete(_session_key(get_current_user_id()))
    resp = make_response(jsonify({'success': True, 'message': 'Déconnecté'}))
    clear_refresh_cookie(resp)
    return resp, 200


# ── Clé publique ──────────────────────────────────────────────────────────────
@auth_bp.route('/api/auth/public-key', methods=['GET'])
@limiter.exempt
def auth_public_key():
    return jsonify({
        'public_key':        os.environ.get('PASETO_PUBLIC_KEY', ''),
        'algorithm':         'Ed25519',
        'version':           'v4.public',
        'token_ttl_minutes': int(os.getenv('PASETO_ACCESS_TTL_MIN', '15')),
    })


# ── Profil courant ────────────────────────────────────────────────────────────
@auth_bp.route('/api/auth/me', methods=['GET'])
@paseto_required
@limiter.exempt
def get_current_user():
    try:
        session = get_session()
        user = session.query(User).filter_by(id=get_current_user_id()).first()
        if not user:
            session.close()
            return jsonify({'error': 'Utilisateur non trouvé'}), 404
        result = user.to_dict(); session.close()
        return jsonify(result)
    except Exception as e:
        try: session.rollback(); session.close()
        except Exception: pass
        return jsonify({'error': str(e)}), 500


# ── Mise à jour profil ────────────────────────────────────────────────────────
@auth_bp.route('/api/profile', methods=['PUT'])
@paseto_required
def update_profile():
    try:
        session = get_session()
        user = session.query(User).filter_by(id=get_current_user_id()).first()
        if not user:
            session.close()
            return jsonify({'error': 'Utilisateur non trouvé'}), 404

        data      = request.json or {}
        full_name = data.get('full_name', '').strip()
        email     = data.get('email', '').strip()

        if full_name:
            user.full_name = full_name
        if email and email != user.email:
            if session.query(User).filter_by(email=email).first():
                session.close()
                return jsonify({'error': 'Cet email est déjà utilisé'}), 400
            user.email = email

        session.commit()
        result = user.to_dict(); session.close()
        return jsonify({'success': True, 'user': result})
    except Exception as e:
        try: session.rollback(); session.close()
        except Exception: pass
        return jsonify({'error': str(e)}), 500


# ── Changement de mot de passe ────────────────────────────────────────────────
@auth_bp.route('/api/profile/password', methods=['PUT'])
@paseto_required
def change_password():
    try:
        session = get_session()
        user = session.query(User).filter_by(id=get_current_user_id()).first()
        if not user:
            session.close()
            return jsonify({'error': 'Utilisateur non trouvé'}), 404

        data       = request.json or {}
        current_pw = data.get('current_password', '')
        new_pw     = data.get('new_password', '')
        confirm_pw = data.get('confirm_password', '')

        if not bcrypt.check_password_hash(user.password_hash, current_pw):
            session.close()
            return jsonify({'error': 'Mot de passe actuel incorrect'}), 400
        if len(new_pw) < 8:
            session.close()
            return jsonify({'error': 'Le mot de passe doit comporter au moins 8 caractères'}), 400
        if confirm_pw and new_pw != confirm_pw:
            session.close()
            return jsonify({'error': 'Les mots de passe ne correspondent pas'}), 400

        user.password_hash = bcrypt.generate_password_hash(new_pw).decode('utf-8')
        session.commit()
        app_url   = os.getenv('APP_URL', 'https://dev-cei.ddns.net').rstrip('/')
        reset_url = f"{app_url}/app?action=forgot"
        saved_email = user.email
        saved_name = user.full_name
        session.close()
        try:
            if saved_email:
                send_password_changed_email(saved_email, saved_name, reset_url)
        except Exception:
            pass
        return jsonify({'success': True, 'message': 'Mot de passe modifié avec succès'})
    except Exception as e:
        try: session.rollback(); session.close()
        except Exception: pass
        return jsonify({'error': str(e)}), 500


# ── Mot de passe oublié ───────────────────────────────────────────────────────
@auth_bp.route('/api/auth/forgot-password', methods=['POST'])
@limiter.limit("5 per minute;20 per hour")
def forgot_password():
    try:
        import secrets as _secrets
        data  = request.get_json() or {}
        email = (data.get('email') or '').strip().lower()
        if not email:
            return jsonify({'error': 'Email requis'}), 400

        session = get_session()
        user    = session.query(User).filter_by(email=email).first()

        if not user or not user.has_email:
            session.close()
            return jsonify({'success': True, 'masked_email': None, 'email_sent': False})

        token = _secrets.token_urlsafe(32)
        user.reset_token         = token
        user.reset_token_expires = utcnow() + timedelta(hours=1)
        session.commit()

        app_url    = os.getenv('APP_URL', request.host_url.rstrip('/'))
        reset_link = f"{app_url}/app?reset_token={token}"
        email_to_send = user.email
        email_name = user.full_name
        parts  = (user.email or '').split('@')
        masked = parts[0][:2] + '***@' + parts[1] if len(parts) == 2 and len(parts[0]) > 2 else user.email
        session.close()

        email_sent = False
        try:
            if email_to_send:
                email_sent = send_password_reset_email(email_to_send, email_name, reset_link)
        except Exception as e:
            print(f"WARNING email reset: {e}")

        return jsonify({'success': True, 'masked_email': masked, 'email_sent': email_sent})
    except Exception as e:
        try: session.rollback(); session.close()
        except Exception: pass
        return jsonify({'error': str(e)}), 500


# ── Réinitialisation mot de passe ─────────────────────────────────────────────
@auth_bp.route('/api/auth/reset-password', methods=['POST'])
def reset_password():
    try:
        data         = request.get_json() or {}
        token        = (data.get('token') or '').strip()
        new_password = data.get('new_password', '')

        if not token or not new_password:
            return jsonify({'error': 'Token et nouveau mot de passe requis'}), 400
        if len(new_password) < 8:
            return jsonify({'error': 'Le mot de passe doit contenir au moins 8 caractères'}), 400

        session = get_session()
        user    = session.query(User).filter_by(reset_token=token).first()
        if not user:
            session.close()
            return jsonify({'error': 'Lien invalide ou déjà utilisé'}), 400

        exp = user.reset_token_expires
        if exp is not None and exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        if exp is not None and utcnow() > exp:
            user.reset_token = None; user.reset_token_expires = None
            session.commit(); session.close()
            return jsonify({'error': 'Ce lien a expiré. Faites une nouvelle demande.'}), 400

        saved_email = user.email; saved_name = user.full_name
        user.password_hash       = bcrypt.generate_password_hash(new_password).decode('utf-8')
        user.reset_token         = None
        user.reset_token_expires = None
        session.commit()
        app_url   = os.getenv('APP_URL', 'https://dev-cei.ddns.net').rstrip('/')
        reset_url = f"{app_url}/app?action=forgot"
        session.close()
        try:
            if saved_email:
                send_password_changed_email(saved_email, saved_name, reset_url)
        except Exception:
            pass
        return jsonify({'success': True, 'message': 'Mot de passe mis à jour avec succès.'})
    except Exception as e:
        try: session.rollback(); session.close()
        except Exception: pass
        return jsonify({'error': str(e)}), 500
