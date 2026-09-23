"""
API externe — module Étudiant. Destinée à l'intégration ENT (lecture seule).
Chaque route exige @paseto_required (session CEI réelle, ex. via SSO) ET
@api_key_required (clé d'intégration ENT), avec vérification croisée que le
rôle de l'utilisateur connecté figure dans allowed_roles de la clé utilisée.
"""
from datetime import datetime, timedelta, timezone

from flask import Blueprint, jsonify

from auth_paseto import paseto_required, get_current_user_id, get_current_user_role
from api_key_auth import api_key_required, api_client_allows_role
from extensions import limiter
from models import (
    get_session, User, UserRole, OnlineExam, ExamStatus, ExamAttempt,
    GradeTranscript,
)
from flask import g

external_student_bp = Blueprint('external_student', __name__)


def _forbid_unless_student_and_allowed():
    if get_current_user_role() != 'student':
        return jsonify({'error': 'Réservé au module Étudiant'}), 403
    if not api_client_allows_role(g.api_client, 'student'):
        return jsonify({'error': "Cette clé API n'est pas autorisée pour le module Étudiant"}), 403
    return None


@external_student_bp.route('/api/external/student/exams', methods=['GET'])
@paseto_required
@api_key_required
@limiter.limit("60 per minute")
def external_student_exams():
    denied = _forbid_unless_student_and_allowed()
    if denied:
        return denied

    user_id = get_current_user_id()
    session = get_session()
    try:
        active = session.query(OnlineExam).filter(
            OnlineExam.status.in_([ExamStatus.SCHEDULED, ExamStatus.ACTIVE])
        ).all()
        recent_cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=7)
        participated_ids = {
            r[0] for r in session.query(ExamAttempt.exam_id).filter_by(student_id=user_id).all()
        }
        closed_recent = []
        if participated_ids:
            closed_recent = session.query(OnlineExam).filter(
                OnlineExam.status == ExamStatus.CLOSED,
                OnlineExam.end_time >= recent_cutoff,
                OnlineExam.id.in_(list(participated_ids)),
            ).all()

        result = []
        for exam in active + closed_recent:
            attempt = session.query(ExamAttempt).filter_by(exam_id=exam.id, student_id=user_id).first()
            result.append({
                'id': exam.id,
                'title': exam.title,
                'status': exam.status.value,
                'start_time': exam.start_time.isoformat() if exam.start_time else None,
                'end_time': exam.end_time.isoformat() if exam.end_time else None,
                'duration_minutes': exam.duration_minutes,
                'my_attempt_status': attempt.status.value if attempt else None,
                'my_score': attempt.score if (attempt and attempt.score is not None) else None,
            })
        return jsonify({'exams': result})
    finally:
        session.close()


@external_student_bp.route('/api/external/student/transcripts', methods=['GET'])
@paseto_required
@api_key_required
@limiter.limit("60 per minute")
def external_student_transcripts():
    denied = _forbid_unless_student_and_allowed()
    if denied:
        return denied

    user_id = get_current_user_id()
    session = get_session()
    try:
        transcripts = session.query(GradeTranscript).filter_by(
            student_id=user_id, is_published=True
        ).order_by(GradeTranscript.generated_at.desc()).all()

        result = [{
            'id': t.id,
            'semester_name': t.semester.name if t.semester else None,
            'formation_name': t.semester.formation.name if t.semester and t.semester.formation else None,
            'gpa': t.gpa,
            'total_credits': t.total_credits,
            'obtained_credits': t.obtained_credits,
            'validated': (t.gpa >= 10) if t.gpa is not None else False,
            'generated_at': t.generated_at.isoformat() if t.generated_at else None,
        } for t in transcripts]
        return jsonify({'transcripts': result})
    finally:
        session.close()
