"""API externe — module Superviseur. Lecture seule, voir student.py pour le
détail du double contrôle (@paseto_required + @api_key_required + rôle)."""
from flask import Blueprint, jsonify, g

from auth_paseto import paseto_required, get_current_user_id, get_current_user_role
from api_key_auth import api_key_required, api_client_allows_role
from extensions import limiter
from models import get_session, ProctorGroup, ProctorGroupSupervisor

external_superviseur_bp = Blueprint('external_superviseur', __name__)


def _forbid_unless_superviseur_and_allowed():
    if get_current_user_role() != 'superviseur':
        return jsonify({'error': 'Réservé au module Superviseur'}), 403
    if not api_client_allows_role(g.api_client, 'superviseur'):
        return jsonify({'error': "Cette clé API n'est pas autorisée pour le module Superviseur"}), 403
    return None


@external_superviseur_bp.route('/api/external/superviseur/groups', methods=['GET'])
@paseto_required
@api_key_required
@limiter.limit("60 per minute")
def external_superviseur_groups():
    denied = _forbid_unless_superviseur_and_allowed()
    if denied:
        return denied

    user_id = get_current_user_id()
    session = get_session()
    try:
        groups = (
            session.query(ProctorGroup)
            .join(ProctorGroupSupervisor, ProctorGroupSupervisor.group_id == ProctorGroup.id)
            .filter(ProctorGroupSupervisor.supervisor_id == user_id)
            .order_by(ProctorGroup.name)
            .all()
        )
        result = [{
            'id': g_.id,
            'name': g_.name,
            'vigilance_level': g_.vigilance_level or 'A',
            'member_count': len(g_.members),
        } for g_ in groups]
        return jsonify({'groups': result})
    finally:
        session.close()
