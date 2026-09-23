"""API externe — module Superviseur. Voir student.py pour le détail du
double contrôle (@paseto_required + @api_key_required + rôle).

Lecture seule (v2, 23/09) — l'initiation d'appel réel dépend de LiveKit et
reste interne. Voir /root/.claude/plans/eager-discovering-star.md.
"""
import json

from flask import Blueprint, jsonify, g

from auth_paseto import paseto_required, get_current_user_id, get_current_user_role
from api_key_auth import api_key_required, api_client_allows_role
from extensions import limiter
from models import (
    get_session, ProctorGroup, ProctorGroupSupervisor, OnlineExam, Subject,
    ExamAttempt, ExamActivityLog, ProctorAssignment,
)

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


@external_superviseur_bp.route('/api/external/superviseur/dashboard', methods=['GET'])
@paseto_required
@api_key_required
@limiter.limit("60 per minute")
def external_superviseur_dashboard():
    denied = _forbid_unless_superviseur_and_allowed()
    if denied:
        return denied

    from proctoring_routes import get_proctor_status

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

        groups_data = []
        total_members = 0
        total_engaged = 0
        for grp in groups:
            members_data = []
            for m in grp.members:
                proctor = m.proctor
                status, exam_id = get_proctor_status(proctor.id, session) if proctor else ('disconnected', None)
                if status == 'engaged':
                    total_engaged += 1
                total_members += 1
                members_data.append({
                    'id': proctor.id if proctor else None,
                    'full_name': proctor.full_name if proctor else None,
                    'status': status,
                    'monitoring_exam_id': exam_id,
                })
            groups_data.append({
                'id': grp.id,
                'name': grp.name,
                'vigilance_level': grp.vigilance_level or 'A',
                'members': members_data,
            })

        return jsonify({
            'groups': groups_data,
            'total_groups': len(groups_data),
            'total_surveillants': total_members,
            'active_surveillants': total_engaged,
        })
    finally:
        session.close()


@external_superviseur_bp.route('/api/external/superviseur/call-requests', methods=['GET'])
@paseto_required
@api_key_required
@limiter.limit("60 per minute")
def external_superviseur_call_requests():
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
            .all()
        )
        ec_ids = {ge.ec_id for grp in groups for ge in grp.ecs}
        if not ec_ids:
            return jsonify({'requests': []})

        exam_ids = [
            row.id for row in session.query(OnlineExam.id)
            .join(Subject, OnlineExam.subject_id == Subject.id)
            .filter(Subject.ec_id.in_(ec_ids)).all()
        ]
        if not exam_ids:
            return jsonify({'requests': []})

        logs = (
            session.query(ExamActivityLog)
            .join(ExamAttempt, ExamActivityLog.attempt_id == ExamAttempt.id)
            .filter(ExamAttempt.exam_id.in_(exam_ids), ExamActivityLog.event_type == 'student_call_request')
            .order_by(ExamActivityLog.timestamp.desc())
            .limit(30).all()
        )

        results = []
        seen_attempts = set()
        for log in logs:
            if log.attempt_id in seen_attempts:
                continue
            attempt = session.query(ExamAttempt).filter_by(id=log.attempt_id).first()
            if not attempt:
                continue
            has_surveillant = session.query(ProctorAssignment).filter_by(exam_id=attempt.exam_id).filter(
                (ProctorAssignment.attempt_id == attempt.id) | (ProctorAssignment.student_id == attempt.student_id)
            ).first() is not None
            if has_surveillant:
                continue
            seen_attempts.add(log.attempt_id)
            try:
                d = json.loads(log.event_data)
            except Exception:
                d = {}
            results.append({
                'attempt_id': log.attempt_id,
                'exam_id': attempt.exam_id,
                'exam_title': attempt.exam.title if attempt.exam else None,
                'student_name': d.get('student_name', '?'),
                'timestamp': log.timestamp.isoformat() if log.timestamp else None,
            })

        return jsonify({'requests': results})
    finally:
        session.close()
