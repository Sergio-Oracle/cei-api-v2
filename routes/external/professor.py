"""API externe — module Professeur. Voir student.py pour le détail du
double contrôle (@paseto_required + @api_key_required + rôle).

Couverture volontairement élargie (v2, 23/09) pour représenter l'usage réel
d'un professeur dans CEI. Restent explicitement exclues (accessibles
seulement depuis l'appli CEI elle-même, jamais via cette API) : les actions
à fort enjeu d'intégrité d'un examen en cours (activer/prolonger/clôturer,
notes manuelles par question, détection de plagiat, références faciales,
rapport d'intégrité, incidents, QR code), la gestion des groupes/affectations
de surveillants, les enregistrements, la génération de sujet par IA et
l'upload de copies — voir /root/.claude/plans/eager-discovering-star.md.
"""
from datetime import datetime, timezone, timedelta

from flask import Blueprint, jsonify, g, request
from sqlalchemy.orm import joinedload

from auth_paseto import paseto_required, get_current_user_id, get_current_user_role
from api_key_auth import api_key_required, api_client_allows_role
from extensions import limiter
from helpers import utcnow
from models import (
    get_session, User, UserRole, OnlineExam, ExamStatus, ExamAttempt, AttemptStatus,
    Subject, QuestionBank, GradeTranscript, Reclamation, StudentPaper,
    ECAssignment, EC, UE,
)
from services.subject_service import SubjectService

external_professor_bp = Blueprint('external_professor', __name__)


def _forbid_unless_professor_and_allowed():
    if get_current_user_role() != 'professor':
        return jsonify({'error': 'Réservé au module Professeur'}), 403
    if not api_client_allows_role(g.api_client, 'professor'):
        return jsonify({'error': "Cette clé API n'est pas autorisée pour le module Professeur"}), 403
    return None


# ── Lecture : examens ────────────────────────────────────────────────────────

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


@external_professor_bp.route('/api/external/professor/exams/<int:exam_id>', methods=['GET'])
@paseto_required
@api_key_required
@limiter.limit("60 per minute")
def external_professor_exam_detail(exam_id):
    denied = _forbid_unless_professor_and_allowed()
    if denied:
        return denied

    user_id = get_current_user_id()
    session = get_session()
    try:
        exam = session.query(OnlineExam).options(joinedload(OnlineExam.subject)).filter_by(id=exam_id).first()
        if not exam:
            return jsonify({'error': 'Examen non trouvé'}), 404
        if exam.created_by_id != user_id:
            return jsonify({'error': 'Accès non autorisé'}), 403
        return jsonify(exam.to_dict())
    finally:
        session.close()


@external_professor_bp.route('/api/external/professor/exams', methods=['POST'])
@paseto_required
@api_key_required
@limiter.limit("60 per minute")
def external_professor_create_exam():
    denied = _forbid_unless_professor_and_allowed()
    if denied:
        return denied

    user_id = get_current_user_id()
    session = get_session()
    try:
        data = request.get_json(silent=True) or {}
        for field in ('subject_id', 'title', 'start_time', 'end_time'):
            if field not in data:
                return jsonify({'error': f'Le champ "{field}" est requis'}), 400

        subject = session.query(Subject).filter_by(id=data['subject_id']).first()
        if not subject:
            return jsonify({'error': "Le sujet sélectionné n'existe pas"}), 404
        if subject.creator_id != user_id:
            return jsonify({'error': 'Vous ne pouvez utiliser que vos propres sujets'}), 403

        try:
            raw_start = data['start_time'].strip().replace('Z', '+00:00')
            raw_end = data['end_time'].strip().replace('Z', '+00:00')
            start_time = datetime.fromisoformat(raw_start).astimezone(timezone.utc).replace(tzinfo=None)
            end_time = datetime.fromisoformat(raw_end).astimezone(timezone.utc).replace(tzinfo=None)
            if end_time <= start_time:
                return jsonify({'error': 'La date de fin doit être après la date de début'}), 400
            duration_minutes = int((end_time - start_time).total_seconds() / 60)
            if duration_minutes <= 0 or duration_minutes > 1440:
                return jsonify({'error': 'Durée invalide (doit être entre 1 min et 24h)'}), 400
        except ValueError as ve:
            return jsonify({'error': f'Format de date invalide: {ve}'}), 400

        exam = OnlineExam(
            subject_id=data['subject_id'],
            title=data['title'],
            instructions=data.get('instructions', ''),
            duration_minutes=duration_minutes,
            start_time=start_time,
            end_time=end_time,
            status=ExamStatus.SCHEDULED,
            created_by_id=user_id,
        )
        session.add(exam)
        session.commit()
        return jsonify({'success': True, 'exam': exam.to_dict()}), 201
    except Exception as e:
        session.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        session.close()


@external_professor_bp.route('/api/external/professor/exams/<int:exam_id>', methods=['PUT'])
@paseto_required
@api_key_required
@limiter.limit("60 per minute")
def external_professor_update_exam(exam_id):
    denied = _forbid_unless_professor_and_allowed()
    if denied:
        return denied

    user_id = get_current_user_id()
    session = get_session()
    try:
        exam = session.query(OnlineExam).filter_by(id=exam_id).first()
        if not exam:
            return jsonify({'error': 'Examen non trouvé'}), 404
        if exam.created_by_id != user_id:
            return jsonify({'error': 'Accès non autorisé'}), 403
        if exam.status not in (ExamStatus.DRAFT, ExamStatus.SCHEDULED):
            return jsonify({'error': 'Seuls les examens en brouillon ou planifiés peuvent être modifiés'}), 400

        data = request.get_json(silent=True) or {}
        if data.get('title'):
            exam.title = data['title'].strip()
        if data.get('start_time'):
            try:
                exam.start_time = datetime.fromisoformat(data['start_time'])
            except ValueError:
                pass
        if data.get('end_time'):
            try:
                new_end = datetime.fromisoformat(data['end_time'])
                if new_end <= exam.start_time:
                    return jsonify({'error': 'La date de fin doit être après la date de début'}), 400
                exam.end_time = new_end
                exam.duration_minutes = max(5, int((new_end - exam.start_time).total_seconds() / 60))
            except ValueError:
                pass
        elif 'duration_minutes' in data:
            exam.duration_minutes = max(5, int(data['duration_minutes']))
            exam.end_time = exam.start_time + timedelta(minutes=exam.duration_minutes)

        session.commit()
        return jsonify({'success': True, 'exam': exam.to_dict()})
    except Exception as e:
        session.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        session.close()


@external_professor_bp.route('/api/external/professor/exams/<int:exam_id>/attempts', methods=['GET'])
@paseto_required
@api_key_required
@limiter.limit("60 per minute")
def external_professor_exam_attempts(exam_id):
    denied = _forbid_unless_professor_and_allowed()
    if denied:
        return denied

    user_id = get_current_user_id()
    session = get_session()
    try:
        exam = session.query(OnlineExam).filter_by(id=exam_id).first()
        if not exam:
            return jsonify({'error': 'Examen non trouvé'}), 404
        if exam.created_by_id != user_id:
            return jsonify({'error': 'Accès non autorisé'}), 403

        page = max(1, request.args.get('page', 1, type=int))
        limit = min(200, max(1, request.args.get('limit', 50, type=int)))
        query = session.query(ExamAttempt).options(joinedload(ExamAttempt.student)).filter_by(exam_id=exam_id)
        total = query.count()
        attempts = query.order_by(ExamAttempt.started_at.desc()).offset((page - 1) * limit).limit(limit).all()

        result = []
        for a in attempts:
            d = a.to_dict()
            d.pop('answers', None)
            result.append(d)

        return jsonify({'attempts': result, 'total': total, 'page': page, 'limit': limit})
    finally:
        session.close()


@external_professor_bp.route('/api/external/professor/exams/<int:exam_id>/stats', methods=['GET'])
@paseto_required
@api_key_required
@limiter.limit("60 per minute")
def external_professor_exam_stats(exam_id):
    denied = _forbid_unless_professor_and_allowed()
    if denied:
        return denied

    user_id = get_current_user_id()
    session = get_session()
    try:
        exam = session.query(OnlineExam).filter_by(id=exam_id).first()
        if not exam:
            return jsonify({'error': 'Examen non trouvé'}), 404
        if exam.created_by_id != user_id:
            return jsonify({'error': 'Accès non autorisé'}), 403

        attempts = session.query(ExamAttempt).filter_by(exam_id=exam_id).all()
        done = [a for a in attempts if a.status.value in ('submitted', 'auto_submitted')]
        scores = [a.score for a in done if a.score is not None]
        return jsonify({
            'exam_title': exam.title,
            'total': len(attempts),
            'submitted': len(done),
            'corrected': sum(1 for a in done if a.score is not None),
            'avg_score': round(sum(scores) / len(scores), 2) if scores else None,
            'min_score': min(scores) if scores else None,
            'max_score': max(scores) if scores else None,
            'pass_rate': round(sum(1 for s in scores if s >= 10) / len(scores) * 100, 1) if scores else None,
        })
    finally:
        session.close()


@external_professor_bp.route('/api/external/professor/exams/<int:exam_id>/publish-results', methods=['PUT'])
@paseto_required
@api_key_required
@limiter.limit("60 per minute")
def external_professor_publish_results(exam_id):
    denied = _forbid_unless_professor_and_allowed()
    if denied:
        return denied

    user_id = get_current_user_id()
    session = get_session()
    try:
        exam = session.query(OnlineExam).filter_by(id=exam_id).first()
        if not exam:
            return jsonify({'error': 'Examen non trouvé'}), 404
        if exam.created_by_id != user_id:
            return jsonify({'error': 'Accès non autorisé'}), 403

        data = request.get_json(silent=True) or {}
        exam.results_published = bool(data.get('published', True))
        session.commit()
        return jsonify({'success': True, 'results_published': exam.results_published})
    except Exception as e:
        session.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        session.close()


# ── Lecture/écriture : copies et corrections ─────────────────────────────────

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


# ── Lecture : sujets ──────────────────────────────────────────────────────────

@external_professor_bp.route('/api/external/professor/subjects', methods=['GET'])
@paseto_required
@api_key_required
@limiter.limit("60 per minute")
def external_professor_subjects():
    denied = _forbid_unless_professor_and_allowed()
    if denied:
        return denied

    user_id = get_current_user_id()
    result = SubjectService.list_for_user(user_id, UserRole.PROFESSOR)
    return jsonify({'subjects': result})


@external_professor_bp.route('/api/external/professor/subjects/<int:subject_id>', methods=['GET'])
@paseto_required
@api_key_required
@limiter.limit("60 per minute")
def external_professor_subject_detail(subject_id):
    denied = _forbid_unless_professor_and_allowed()
    if denied:
        return denied

    user_id = get_current_user_id()
    try:
        result = SubjectService.get_detail(subject_id, user_id, UserRole.PROFESSOR)
        return jsonify(result)
    except LookupError as e:
        return jsonify({'error': str(e)}), 404
    except PermissionError as e:
        return jsonify({'error': str(e)}), 403


# ── Lecture : banque de questions (propres questions uniquement) ────────────

@external_professor_bp.route('/api/external/professor/questions', methods=['GET'])
@paseto_required
@api_key_required
@limiter.limit("60 per minute")
def external_professor_questions():
    denied = _forbid_unless_professor_and_allowed()
    if denied:
        return denied

    user_id = get_current_user_id()
    session = get_session()
    try:
        questions = session.query(QuestionBank).filter_by(created_by_id=user_id).order_by(
            QuestionBank.created_at.desc()
        ).all()
        return jsonify({'questions': [q.to_dict() for q in questions]})
    finally:
        session.close()


# ── Lecture : relevés générés par ce professeur ─────────────────────────────

@external_professor_bp.route('/api/external/professor/transcripts', methods=['GET'])
@paseto_required
@api_key_required
@limiter.limit("60 per minute")
def external_professor_transcripts():
    denied = _forbid_unless_professor_and_allowed()
    if denied:
        return denied

    from routes.transcripts import _serialize_transcript

    user_id = get_current_user_id()
    session = get_session()
    try:
        transcripts = session.query(GradeTranscript).filter_by(generated_by_id=user_id).order_by(
            GradeTranscript.generated_at.desc()
        ).all()
        return jsonify({'transcripts': [_serialize_transcript(t, session) for t in transcripts]})
    finally:
        session.close()


@external_professor_bp.route('/api/external/professor/transcripts/<int:tid>/publish', methods=['PUT'])
@paseto_required
@api_key_required
@limiter.limit("60 per minute")
def external_professor_publish_transcript(tid):
    denied = _forbid_unless_professor_and_allowed()
    if denied:
        return denied

    user_id = get_current_user_id()
    session = get_session()
    try:
        t = session.query(GradeTranscript).filter_by(id=tid).first()
        if not t:
            return jsonify({'error': 'Relevé introuvable'}), 404
        if t.generated_by_id != user_id:
            return jsonify({'error': 'Vous ne pouvez publier que les relevés que vous avez générés'}), 403

        data = request.get_json(silent=True) or {}
        t.is_published = bool(data.get('is_published', not t.is_published))
        session.commit()
        return jsonify({'success': True, 'is_published': t.is_published})
    except Exception as e:
        session.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        session.close()


# ── Lecture/réponse : réclamations sur ses examens/copies ───────────────────

@external_professor_bp.route('/api/external/professor/reclamations', methods=['GET'])
@paseto_required
@api_key_required
@limiter.limit("60 per minute")
def external_professor_reclamations():
    denied = _forbid_unless_professor_and_allowed()
    if denied:
        return denied

    from routes.reclamations import _serialize

    user_id = get_current_user_id()
    session = get_session()
    try:
        paper_ids = [r.id for r in session.query(Reclamation)
                     .join(StudentPaper, Reclamation.paper_id == StudentPaper.id)
                     .filter(StudentPaper.corrected_by_id == user_id).all()]
        online_ids = [r.id for r in session.query(Reclamation)
                      .join(ExamAttempt, Reclamation.attempt_id == ExamAttempt.id)
                      .join(OnlineExam, ExamAttempt.exam_id == OnlineExam.id)
                      .filter(OnlineExam.created_by_id == user_id).all()]
        visible = list(set(paper_ids + online_ids))
        recs = session.query(Reclamation).options(
            joinedload(Reclamation.student),
            joinedload(Reclamation.paper).joinedload(StudentPaper.subject),
            joinedload(Reclamation.attempt).joinedload(ExamAttempt.exam),
        ).filter(Reclamation.id.in_(visible)).order_by(Reclamation.created_at.desc()).all() if visible else []
        return jsonify({'reclamations': [_serialize(r) for r in recs]})
    finally:
        session.close()


@external_professor_bp.route('/api/external/professor/reclamations/<int:rid>/respond', methods=['PUT'])
@paseto_required
@api_key_required
@limiter.limit("60 per minute")
def external_professor_respond_reclamation(rid):
    denied = _forbid_unless_professor_and_allowed()
    if denied:
        return denied

    user_id = get_current_user_id()
    session = get_session()
    try:
        rec = session.query(Reclamation).options(
            joinedload(Reclamation.paper).joinedload(StudentPaper.subject),
            joinedload(Reclamation.attempt).joinedload(ExamAttempt.exam),
        ).filter_by(id=rid).first()
        if not rec:
            return jsonify({'error': 'Réclamation non trouvée'}), 404

        owner_id = (rec.paper.subject.creator_id if rec.paper and rec.paper.subject
                    else rec.attempt.exam.created_by_id if rec.attempt and rec.attempt.exam
                    else None)
        if owner_id != user_id:
            return jsonify({'error': 'Accès non autorisé'}), 403

        data = request.get_json(silent=True) or {}
        status = data.get('status')
        status_map = {'approved': 'resolved', 'rejected': 'rejected', 'in_review': 'in_review', 'resolved': 'resolved'}
        mapped = status_map.get(status)
        if not mapped:
            return jsonify({'error': 'Statut invalide (approved | rejected | in_review)'}), 400

        from models import ReclamationStatus
        rec.status = ReclamationStatus[mapped.upper()]
        rec.response = data.get('response')
        rec.responded_by_id = user_id
        rec.updated_at = utcnow()
        session.commit()
        return jsonify({'success': True, 'reclamation': {
            'id': rec.id, 'status': rec.status.value, 'response': rec.response,
        }})
    except Exception as e:
        session.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        session.close()


# ── Lecture : étudiants de ses EC/UE ─────────────────────────────────────────

@external_professor_bp.route('/api/external/professor/students', methods=['GET'])
@paseto_required
@api_key_required
@limiter.limit("60 per minute")
def external_professor_students():
    denied = _forbid_unless_professor_and_allowed()
    if denied:
        return denied

    from models import StudentUEEnrollment

    user_id = get_current_user_id()
    session = get_session()
    try:
        ec_ids = [a.ec_id for a in session.query(ECAssignment).filter_by(professor_id=user_id).all()]
        ecs = session.query(EC).filter(EC.id.in_(ec_ids)).all() if ec_ids else []
        ue_ids = list({ec.ue_id for ec in ecs if ec.ue_id})
        if not ue_ids:
            return jsonify({'students': [], 'total': 0})

        student_ids = {
            r[0] for r in session.query(StudentUEEnrollment.student_id)
            .join(User, StudentUEEnrollment.student_id == User.id)
            .filter(StudentUEEnrollment.ue_id.in_(ue_ids), User.role == UserRole.STUDENT)
            .all()
        }
        students = session.query(User).filter(User.id.in_(student_ids)).order_by(User.full_name).all()
        return jsonify({
            'students': [{'id': s.id, 'full_name': s.full_name, 'email': s.email} for s in students],
            'total': len(students),
        })
    finally:
        session.close()


# ── Lecture : EC/UE assignées à ce professeur ────────────────────────────────

@external_professor_bp.route('/api/external/professor/ecs', methods=['GET'])
@paseto_required
@api_key_required
@limiter.limit("60 per minute")
def external_professor_ecs():
    denied = _forbid_unless_professor_and_allowed()
    if denied:
        return denied

    user_id = get_current_user_id()
    session = get_session()
    try:
        ec_ids = [a.ec_id for a in session.query(ECAssignment).filter_by(professor_id=user_id).all()]
        ecs = session.query(EC).filter(EC.id.in_(ec_ids)).all() if ec_ids else []
        result = []
        for ec in ecs:
            ue = session.query(UE).filter_by(id=ec.ue_id).first()
            result.append({'ec_id': ec.id, 'ec_code': ec.code, 'ec_name': ec.name,
                            'ue_code': ue.code if ue else None})
        return jsonify({'ecs': result})
    finally:
        session.close()


# ── Lecture : analytique de base ─────────────────────────────────────────────

@external_professor_bp.route('/api/external/professor/analytics', methods=['GET'])
@paseto_required
@api_key_required
@limiter.limit("60 per minute")
def external_professor_analytics():
    denied = _forbid_unless_professor_and_allowed()
    if denied:
        return denied

    user_id = get_current_user_id()
    session = get_session()
    try:
        my_subjects = session.query(Subject).filter_by(creator_id=user_id).count()
        online_corrected = session.query(ExamAttempt).join(
            OnlineExam, ExamAttempt.exam_id == OnlineExam.id
        ).filter(OnlineExam.created_by_id == user_id, ExamAttempt.score.isnot(None)).count()
        return jsonify({'my_subjects': my_subjects, 'papers_corrected': online_corrected})
    finally:
        session.close()
