"""
Blueprint Auth — Contrôleur MVC.

Routes : register, login, refresh, logout, public-key, me,
         profile (GET/PUT), profile/password, forgot-password, reset-password.

Migré depuis app.py — logique identique, zéro regression.
"""
from flask import Blueprint, request, jsonify, make_response
from datetime import datetime, timedelta, timezone
from threading import Thread
import os

from extensions import bcrypt, limiter
from helpers    import utcnow
from auth_paseto import (
    paseto_required, get_current_user_id,
    create_access_token, create_refresh_token,
    set_refresh_cookie, clear_refresh_cookie,
    get_refresh_token_from_cookie, hash_token,
)
from auth_paseto import decode_token as paseto_decode_token
from models import get_session, User, UserRole, TokenBlocklist
from utils  import (
    send_account_created_email, send_password_reset_email,
    send_password_changed_email,
)

auth_bp = Blueprint('auth', __name__)


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
        session.add(user); session.commit()
        user_dict = user.to_dict()
        # Envoi en tâche de fond — ne doit pas faire attendre l'étudiant qui s'inscrit.
        Thread(target=send_account_created_email, args=(data['email'], data['full_name'], 'student'), daemon=True).start()
        return jsonify({'success': True, 'message': 'Inscription réussie', 'user': user_dict}), 201
    except Exception as e:
        session.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        session.close()


# ── Connexion ─────────────────────────────────────────────────────────────────
@auth_bp.route('/api/auth/login', methods=['POST'])
@limiter.limit("10 per minute;50 per hour")
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
            return jsonify({'error': 'Compte désactivé'}), 403

        user.last_login   = utcnow()
        session.commit()
        access_token  = create_access_token(user.id, user.role.value, user.email)
        refresh_token = create_refresh_token(user.id)
        user_dict     = user.to_dict(); session.close()

        resp = make_response(jsonify({
            'success': True, 'access_token': access_token, 'user': user_dict,
        }))
        set_refresh_cookie(resp, refresh_token)
        return resp, 200
    except Exception as e:
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
        new_access  = create_access_token(user.id, user.role.value, user.email)
        new_refresh = create_refresh_token(user.id)
        session.close()

        resp = make_response(jsonify({'access_token': new_access}))
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
    resp = make_response(jsonify({'success': True, 'message': 'Déconnecté'}))
    clear_refresh_cookie(resp)
    return resp, 200


# ── Clé publique ──────────────────────────────────────────────────────────────
@auth_bp.route('/api/auth/public-key', methods=['GET'])
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
        try:
            if user.email:
                send_password_changed_email(user.email, user.full_name, reset_url)
        except Exception:
            pass
        session.close()
        return jsonify({'success': True, 'message': 'Mot de passe modifié avec succès'})
    except Exception as e:
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
        email_sent = False
        try:
            email_sent = send_password_reset_email(user.email, user.full_name, reset_link)
        except Exception as e:
            print(f"WARNING email reset: {e}")

        parts  = (user.email or '').split('@')
        masked = parts[0][:2] + '***@' + parts[1] if len(parts) == 2 and len(parts[0]) > 2 else user.email
        session.close()
        return jsonify({'success': True, 'masked_email': masked, 'email_sent': email_sent})
    except Exception as e:
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
        try:
            if saved_email:
                send_password_changed_email(saved_email, saved_name, reset_url)
        except Exception:
            pass
        session.close()
        return jsonify({'success': True, 'message': 'Mot de passe mis à jour avec succès.'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
