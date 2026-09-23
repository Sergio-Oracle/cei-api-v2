"""API externe — module Professeur. Lecture seule, voir student.py pour le
détail du double contrôle (@paseto_required + @api_key_required + rôle)."""
from flask import Blueprint, jsonify, g

from auth_paseto import paseto_required, get_current_user_id, get_current_user_role
from api_key_auth import api_key_required, api_client_allows_role
from extensions import limiter
from models import get_session, OnlineExam, ExamAttempt, AttemptStatus

external_professor_bp = Blueprint('external_professor', __name__)


def _forbid_unless_professor_and_allowed():
    if get_current_user_role() != 'professor':
        return jsonify({'error': 'Réservé au module Professeur'}), 403
    if not api_client_allows_role(g.api_client, 'professor'):
        return jsonify({'error': "Cette clé API n'est pas autorisée pour le module Professeur"}), 403
    return None


@external_professor_bp.route('/api/external/professor/exams', methods=['GET'])
@paseto_required
@api_key_required
@limiter.limit("60 per minute")
def external_professor_exams():
    denied = _forbid_unless_professor_and_allowed()
    if denied:
        return denied

    user_id = get_current_user_id()
    session = get_session()
    try:
        exams = session.query(OnlineExam).filter_by(created_by_id=user_id).order_by(
            OnlineExam.start_time.desc()
        ).all()
        result = [{
            'id': e.id,
            'title': e.title,
            'status': e.status.value,
            'start_time': e.start_time.isoformat() if e.start_time else None,
            'end_time': e.end_time.isoformat() if e.end_time else None,
        } for e in exams]
        return jsonify({'exams': result})
    finally:
        session.close()


@external_professor_bp.route('/api/external/professor/corrections', methods=['GET'])
@paseto_required
@api_key_required
@limiter.limit("60 per minute")
def external_professor_corrections():
    denied = _forbid_unless_professor_and_allowed()
    if denied:
        return denied

    user_id = get_current_user_id()
    session = get_session()
    try:
        pending = (
            session.query(ExamAttempt)
            .join(OnlineExam, ExamAttempt.exam_id == OnlineExam.id)
            .filter(
                OnlineExam.created_by_id == user_id,
                ExamAttempt.status.in_([AttemptStatus.SUBMITTED, AttemptStatus.AUTO_SUBMITTED]),
                ExamAttempt.score.is_(None),
            )
            .order_by(ExamAttempt.submitted_at.asc())
            .all()
        )
        result = [{
            'attempt_id': a.id,
            'exam_id': a.exam_id,
            'exam_title': a.exam.title if a.exam else None,
            'student_name': a.student.full_name if a.student else None,
            'submitted_at': a.submitted_at.isoformat() if a.submitted_at else None,
        } for a in pending]
        return jsonify({'pending_corrections': result, 'count': len(result)})
    finally:
        session.close()
