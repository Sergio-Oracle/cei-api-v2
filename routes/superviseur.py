"""
Blueprint Superviseur — rôle positionné au-dessus des surveillants, pour
superviser leur activité (Demandes d'évolution CEI, point 1 / Retour DTSI §1).

GET /api/superviseur/dashboard
GET /api/superviseur/call_requests
"""
import json
from flask import Blueprint, jsonify

from auth_paseto import paseto_required, get_current_user_id
from models      import (
    get_session, User, UserRole, ProctorGroup, ProctorGroupEC, ProctorGroupSupervisor,
    OnlineExam, Subject, ExamAttempt, ExamActivityLog, ProctorAssignment,
)

superviseur_bp = Blueprint('superviseur', __name__)


@superviseur_bp.route('/api/superviseur/dashboard', methods=['GET'])
@paseto_required
def superviseur_dashboard():
    try:
        user_id = get_current_user_id()
        session = get_session()
        user    = session.query(User).filter_by(id=user_id).first()

        if not user or user.role != UserRole.SUPERVISEUR:
            session.close(); return jsonify({'error': 'Accès réservé aux superviseurs'}), 403

        from proctoring_routes import get_proctor_status, get_proctor_signals, get_vigilance_level

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
        for g in groups:
            members_data = []
            for m in g.members:
                proctor = m.proctor
                status, exam_id = get_proctor_status(proctor.id, session) if proctor else ('disconnected', None)
                if status == 'engaged':
                    total_engaged += 1
                total_members += 1
                # Détail des signaux — n'a de sens que si le surveillant est
                # bien "vu" (exam_id trouvé) ; sinon aucun signal à détailler.
                signals = None
                if proctor and exam_id:
                    level = get_vigilance_level(exam_id, proctor.id, session)
                    signals = get_proctor_signals(exam_id, proctor.id, level)
                members_data.append({
                    'id': proctor.id if proctor else None,
                    'full_name': proctor.full_name if proctor else None,
                    'email': proctor.email if proctor else None,
                    # Rétrocompatibilité (ancien badge binaire) : 'idle' compte
                    # comme "présent" mais pas comme "activement engagé".
                    'is_active_now': status in ('engaged', 'idle'),
                    'status': status,  # 'engaged' | 'idle' | 'disconnected'
                    'monitoring_exam_id': exam_id,
                    'vigilance_level': g.vigilance_level or 'A',
                    'signals': signals,  # {engaged, viewed?, face?} — détail du palier
                })
            groups_data.append({
                'id': g.id,
                'name': g.name,
                'vigilance_level': g.vigilance_level or 'A',
                'members': members_data,
            })

        result = {
            'groups': groups_data,
            'total_groups': len(groups_data),
            'total_surveillants': total_members,
            'active_surveillants': total_engaged,
        }
        session.close()
        return jsonify(result)
    except Exception as e:
        try: session.rollback(); session.close()
        except Exception: pass
        return jsonify({'error': str(e)}), 500


@superviseur_bp.route('/api/superviseur/call_requests', methods=['GET'])
@paseto_required
def superviseur_call_requests():
    """Demandes d'appel étudiant en attente, pour les examens dont l'EC est
    couvert par un groupe que ce superviseur supervise — uniquement celles où
    AUCUN surveillant n'est actuellement assigné (sinon c'est au surveillant
    de répondre, pas au superviseur : voir la règle d'autorité unique dans
    generate_access_code)."""
    try:
        user_id = get_current_user_id()
        session = get_session()
        user = session.query(User).filter_by(id=user_id).first()
        if not user or user.role != UserRole.SUPERVISEUR:
            session.close(); return jsonify({'error': 'Accès réservé aux superviseurs'}), 403

        groups = (
            session.query(ProctorGroup)
            .join(ProctorGroupSupervisor, ProctorGroupSupervisor.group_id == ProctorGroup.id)
            .filter(ProctorGroupSupervisor.supervisor_id == user_id)
            .all()
        )
        ec_ids = {ge.ec_id for g in groups for ge in g.ecs}
        if not ec_ids:
            session.close(); return jsonify({'requests': []})

        exam_ids = [
            row.id for row in session.query(OnlineExam.id)
            .join(Subject, OnlineExam.subject_id == Subject.id)
            .filter(Subject.ec_id.in_(ec_ids)).all()
        ]
        if not exam_ids:
            session.close(); return jsonify({'requests': []})

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
                continue  # une seule entrée par tentative — la plus récente
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

        session.close()
        return jsonify({'requests': results})
    except Exception as e:
        try: session.rollback(); session.close()
        except Exception: pass
        return jsonify({'error': str(e)}), 500
