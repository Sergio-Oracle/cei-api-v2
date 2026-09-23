"""Authentification par clé API — pour les applications externes (ENT UNCHK)
qui appellent /api/external/<role>/*, en plus (jamais à la place) de
l'authentification PASETO normale de l'utilisateur connecté.

Distinct de auth_paseto.py : ici on identifie une APPLICATION appelante
(via ApiClient), pas une session utilisateur."""
import secrets
import json
from datetime import datetime, timezone
from functools import wraps
from flask import request, jsonify, g

from extensions import bcrypt
from models import get_session, ApiClient

KEY_PREFIX_LEN = 12


def generate_api_key() -> str:
    """Clé brute affichée une seule fois à la création — jamais stockée en clair."""
    return f"cei_{secrets.token_urlsafe(32)}"


def hash_api_key(raw_key: str) -> str:
    return bcrypt.generate_password_hash(raw_key).decode('utf-8')


def api_key_required(f):
    """À empiler EN PLUS de @paseto_required (jamais seul) sur les routes
    /api/external/<role>/*. Vérifie l'en-tête X-CEI-API-Key et pose g.api_client."""
    @wraps(f)
    def decorated(*args, **kwargs):
        raw_key = request.headers.get('X-CEI-API-Key', '').strip()
        if not raw_key:
            return jsonify({'error': "Clé API manquante (en-tête X-CEI-API-Key)"}), 401

        session = get_session()
        try:
            prefix = raw_key[:KEY_PREFIX_LEN]
            candidates = session.query(ApiClient).filter_by(key_prefix=prefix, is_active=True).all()
            client = next((c for c in candidates if bcrypt.check_password_hash(c.key_hash, raw_key)), None)
            if not client:
                return jsonify({'error': 'Clé API invalide ou révoquée'}), 401

            # Copie en dict simple AVANT tout commit/close — l'objet ORM devient
            # inutilisable (DetachedInstanceError) une fois la session fermée,
            # et le reste de la requête accède à g.api_client bien après ce point.
            g.api_client = {
                'id': client.id,
                'name': client.name,
                'allowed_roles': json.loads(client.allowed_roles) if client.allowed_roles else [],
            }
            # Mise à jour best-effort, ne bloque jamais la requête sur cette écriture.
            try:
                client.last_used_at = datetime.now(timezone.utc)
                session.commit()
            except Exception:
                session.rollback()
        finally:
            session.close()

        return f(*args, **kwargs)
    return decorated


def api_client_allows_role(client: dict, role: str) -> bool:
    """`client` est le dict posé sur g.api_client par api_key_required
    (jamais l'objet ORM — voir le commentaire dans le décorateur)."""
    return role in (client.get('allowed_roles') or [])
