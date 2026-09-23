"""API externe — module Surveillant. Lecture seule, voir student.py pour le
détail du double contrôle (@paseto_required + @api_key_required + rôle)."""
from flask import Blueprint, jsonify, g

from auth_paseto import paseto_required, get_current_user_id, get_current_user_role
from api_key_auth import api_key_required, api_client_allows_role
from extensions import limiter
from models import get_session, ExamProctor, ProctorAssignment, ExamAttempt, ExamStatus

external_surveillant_bp = Blueprint('external_surveillant', __name__)


def _forbid_unless_surveillant_and_allowed():
    if get_current_user_role() != 'surveillant':
        return jsonify({'error': 'Réservé au module Surveillant'}), 403
    if not api_client_allows_role(g.api_client, 'surveillant'):
        return jsonify({'error': "Cette clé API n'est pas autorisée pour le module Surveillant"}), 403
    return None


@external_surveillant_bp.route('/api/external/surveillant/assignments', methods=['GET'])
@paseto_required
@api_key_required
@limiter.limit("60 per minute")
def external_surveillant_assignments():
    denied = _forbid_unless_surveillant_and_allowed()
    if denied:
        return denied

    user_id = get_current_user_id()
    session = get_session()
    try:
        exam_proctors = session.query(ExamProctor).filter_by(proctor_id=user_id).all()
        result = []
        for ep in exam_proctors:
            if not ep.exam or ep.exam.status not in (ExamStatus.SCHEDULED, ExamStatus.ACTIVE):
                continue
            student_count = session.query(ProctorAssignment).filter_by(
                exam_id=ep.exam_id, proctor_id=user_id
            ).count()
            result.append({
                'exam_id': ep.exam_id,
                'exam_title': ep.exam.title,
                'status': ep.exam.status.value,
                'start_time': ep.exam.start_time.isoformat() if ep.exam.start_time else None,
                'end_time': ep.exam.end_time.isoformat() if ep.exam.end_time else None,
                'assigned_student_count': student_count,
            })
        return jsonify({'assignments': result})
    finally:
        session.close()
