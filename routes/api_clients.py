"""
Blueprint Admin — Gestion des clés API pour l'intégration externe (ENT UNCHK).

Routes :
  GET    /api/admin/api-clients
  POST   /api/admin/api-clients
  PUT    /api/admin/api-clients/<id>
"""
import json
from flask import Blueprint, request, jsonify

from auth_paseto import paseto_required
from extensions import limiter
from helpers import require_admin
from models import get_session, ApiClient
from api_key_auth import generate_api_key, hash_api_key, KEY_PREFIX_LEN

api_clients_bp = Blueprint('api_clients', __name__)

# Les 4 rôles externes autorisés — Admin explicitement exclu de toute clé API,
# conformément à la consigne "excepté la partie Administrateur".
_ALLOWED_ROLE_VALUES = {'professor', 'student', 'surveillant', 'superviseur'}


@api_clients_bp.route('/api/admin/api-clients', methods=['GET'])
@paseto_required
@limiter.exempt
def list_api_clients():
    try:
        session = get_session()
        if not require_admin(session):
            return jsonify({'error': 'Accès non autorisé'}), 403

        clients = session.query(ApiClient).order_by(ApiClient.created_at.desc()).all()
        result = [c.to_dict() for c in clients]
        session.close()
        return jsonify(result)
    except Exception as e:
        try: session.rollback(); session.close()
        except Exception: pass
        print(f"ERROR list_api_clients: {e}")
        return jsonify({'error': str(e)}), 500


@api_clients_bp.route('/api/admin/api-clients', methods=['POST'])
@paseto_required
def create_api_client():
    session = get_session()
    try:
        admin = require_admin(session)
        if not admin:
            return jsonify({'error': 'Accès non autorisé'}), 403

        data = request.get_json(force=True) or {}
        name = (data.get('name') or '').strip()
        allowed_roles = data.get('allowed_roles') or []

        if not name:
            session.close()
            return jsonify({'error': 'Nom requis'}), 400
        if not allowed_roles or not all(r in _ALLOWED_ROLE_VALUES for r in allowed_roles):
            session.close()
            return jsonify({'error': f"allowed_roles doit être un sous-ensemble non vide de {sorted(_ALLOWED_ROLE_VALUES)}"}), 400

        raw_key = generate_api_key()
        client = ApiClient(
            name=name,
            key_hash=hash_api_key(raw_key),
            key_prefix=raw_key[:KEY_PREFIX_LEN],
            allowed_roles=json.dumps(allowed_roles),
            is_active=True,
            created_by_admin_id=admin.id,
        )
        session.add(client)
        session.commit()
        result = client.to_dict()
        session.close()
        return jsonify({
            'success': True,
            'api_client': result,
            'api_key': raw_key,
            'warning': "Cette clé ne sera plus jamais affichée — copiez-la maintenant.",
        }), 201
    except Exception as e:
        try: session.rollback(); session.close()
        except Exception: pass
        print(f"ERROR create_api_client: {e}")
        return jsonify({'error': str(e)}), 500


@api_clients_bp.route('/api/admin/api-clients/<int:client_id>', methods=['PUT'])
@paseto_required
def update_api_client(client_id):
    session = get_session()
    try:
        if not require_admin(session):
            return jsonify({'error': 'Accès non autorisé'}), 403

        client = session.query(ApiClient).filter_by(id=client_id).first()
        if not client:
            session.close()
            return jsonify({'error': 'Clé API introuvable'}), 404

        data = request.get_json(force=True) or {}
        if 'name' in data:
            client.name = (data['name'] or '').strip() or client.name
        if 'allowed_roles' in data:
            roles = data['allowed_roles'] or []
            if not all(r in _ALLOWED_ROLE_VALUES for r in roles):
                session.close()
                return jsonify({'error': f"allowed_roles doit être un sous-ensemble de {sorted(_ALLOWED_ROLE_VALUES)}"}), 400
            client.allowed_roles = json.dumps(roles)
        if 'is_active' in data:
            from helpers import utcnow
            client.is_active = bool(data['is_active'])
            client.revoked_at = None if client.is_active else utcnow()

        session.commit()
        result = client.to_dict()
        session.close()
        return jsonify({'success': True, 'api_client': result})
    except Exception as e:
        try: session.rollback(); session.close()
        except Exception: pass
        print(f"ERROR update_api_client: {e}")
        return jsonify({'error': str(e)}), 500
