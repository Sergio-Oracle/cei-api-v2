"""API externe — module Surveillant. Voir student.py pour le détail du
double contrôle (@paseto_required + @api_key_required + rôle).

Lecture seule uniquement (v2, 23/09) : avertissement, bannissement, code
d'accès, pilotage caméra/enregistrement restent internes — la surveillance en
direct doit se faire depuis l'interface CEI elle-même, jamais via un appel
API tiers. Voir /root/.claude/plans/eager-discovering-star.md.
"""
from flask import Blueprint, jsonify, g, request
from sqlalchemy.orm import joinedload

from auth_paseto import paseto_required, get_current_user_id, get_current_user_role
from api_key_auth import api_key_required, api_client_allows_role
from extensions import limiter
from models import (
    get_session, ExamProctor, ProctorAssignment, ExamAttempt, ExamStatus,
    OnlineExam, ExamActivityLog, AttemptStatus,
)

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


@external_surveillant_bp.route('/api/external/surveillant/exams/<int:exam_id>/status', methods=['GET'])
@paseto_required
@api_key_required
@limiter.limit("60 per minute")
def external_surveillant_exam_status(exam_id):
    """Statut de surveillance agrégé, sans flux vidéo ni identifiants LiveKit
    (voir get_active_proctoring en interne — cette version externe retire
    livekit_identity/current_egress_id/proctor_identity)."""
    denied = _forbid_unless_surveillant_and_allowed()
    if denied:
        return denied

    user_id = get_current_user_id()
    session = get_session()
    try:
        exam = session.query(OnlineExam).filter_by(id=exam_id).first()
        if not exam:
            return jsonify({'error': 'Examen introuvable'}), 404

        ep_check = session.query(ExamProctor).filter_by(exam_id=exam_id, proctor_id=user_id).first()
        if not ep_check:
            return jsonify({'error': "Vous n'êtes pas affecté à cet examen"}), 403

        assignments = session.query(ProctorAssignment).filter_by(exam_id=exam_id, proctor_id=user_id).all()
        attempt_ids_direct = [pa.attempt_id for pa in assignments if pa.attempt_id]
        student_ids_preassign = [pa.student_id for pa in assignments if pa.student_id and not pa.attempt_id]

        attempts_by_id = session.query(ExamAttempt).options(joinedload(ExamAttempt.student)).filter(
            ExamAttempt.id.in_(attempt_ids_direct)
        ).all() if attempt_ids_direct else []
        attempts_by_student = session.query(ExamAttempt).options(joinedload(ExamAttempt.student)).filter(
            ExamAttempt.exam_id == exam_id, ExamAttempt.student_id.in_(student_ids_preassign)
        ).all() if student_ids_preassign else []

        seen_ids = {a.id for a in attempts_by_id}
        attempts = list(attempts_by_id) + [a for a in attempts_by_student if a.id not in seen_ids]

        result = [{
            'attempt_id': a.id,
            'student_name': a.student.full_name if a.student else '?',
            'status': a.status.value,
            'risk_score': a.risk_score or 0,
            'warnings_count': a.warnings_count,
            'tab_switches': a.tab_switches,
            'started_at': a.started_at.isoformat() if a.started_at else None,
            'submitted_at': a.submitted_at.isoformat() if a.submitted_at else None,
            'banned': a.status == AttemptStatus.BANNED,
        } for a in attempts]

        return jsonify({
            'exam_title': exam.title,
            'exam_status': exam.status.value,
            'attempts': result,
            'total': len(result),
        })
    finally:
        session.close()


@external_surveillant_bp.route('/api/external/surveillant/exams/<int:exam_id>/incidents', methods=['GET'])
@paseto_required
@api_key_required
@limiter.limit("60 per minute")
def external_surveillant_exam_incidents(exam_id):
    denied = _forbid_unless_surveillant_and_allowed()
    if denied:
        return denied

    user_id = get_current_user_id()
    session = get_session()
    try:
        exam = session.query(OnlineExam).filter_by(id=exam_id).first()
        if not exam:
            return jsonify({'error': 'Examen non trouvé'}), 404

        assigned_attempt_ids = [
            pa.attempt_id for pa in session.query(ProctorAssignment).filter_by(proctor_id=user_id).all()
        ]
        logs = session.query(ExamActivityLog).join(ExamAttempt).options(
            joinedload(ExamActivityLog.attempt).joinedload(ExamAttempt.student)
        ).filter(
            ExamAttempt.exam_id == exam_id,
            ExamActivityLog.attempt_id.in_(assigned_attempt_ids or [0]),
        ).order_by(ExamActivityLog.timestamp.desc()).limit(200).all()

        result = []
        for log in logs:
            d = log.to_dict()
            d['student_name'] = log.attempt.student.full_name if log.attempt.student else 'Inconnu'
            d['severity'] = 'high' if log.event_type in ('tab_switch', 'devtools_attempt') else 'medium'
            result.append(d)

        return jsonify({'incidents': result})
    finally:
        session.close()
