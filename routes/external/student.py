"""
API externe — module Étudiant. Destinée à l'intégration ENT.
Chaque route exige @paseto_required (session CEI réelle, ex. via SSO) ET
@api_key_required (clé d'intégration ENT), avec vérification croisée que le
rôle de l'utilisateur connecté figure dans allowed_roles de la clé utilisée.

Couverture volontairement élargie (v2, 23/09), mais restent explicitement
exclus (accessibles seulement depuis l'appli CEI elle-même, jamais via cette
API) : démarrer/soumettre/sauvegarder/heartbeat/signaler une activité pendant
un examen (doit passer par l'interface d'examen sécurisée CEI), la biométrie
(enroll/verify) et toute route de proctoring/LiveKit côté étudiant — voir
/root/.claude/plans/eager-discovering-star.md.
"""
from datetime import datetime, timedelta, timezone

from flask import Blueprint, jsonify, g, request
from sqlalchemy import desc
from sqlalchemy.orm import joinedload

from auth_paseto import paseto_required, get_current_user_id, get_current_user_role
from api_key_auth import api_key_required, api_client_allows_role
from extensions import limiter
from helpers import utcnow
from models import (
    get_session, User, UserRole, OnlineExam, ExamStatus, ExamAttempt,
    GradeTranscript, Reclamation, ReclamationStatus, StudentPaper,
)

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


@external_student_bp.route('/api/external/student/exams/<int:exam_id>', methods=['GET'])
@paseto_required
@api_key_required
@limiter.limit("60 per minute")
def external_student_exam_detail(exam_id):
    denied = _forbid_unless_student_and_allowed()
    if denied:
        return denied

    user_id = get_current_user_id()
    session = get_session()
    try:
        exam = session.query(OnlineExam).options(joinedload(OnlineExam.subject)).filter_by(id=exam_id).first()
        if not exam:
            return jsonify({'error': 'Examen non trouvé'}), 404

        attempt = session.query(ExamAttempt).filter_by(exam_id=exam_id, student_id=user_id).first()
        if attempt and attempt.status.value == 'in_progress':
            return jsonify({'error': 'Examen en cours — non consultable via cette API, utilisez l\'interface CEI'}), 403

        return jsonify({
            'id': exam.id,
            'title': exam.title,
            'status': exam.status.value,
            'subject_title': exam.subject.title if exam.subject else None,
            'start_time': exam.start_time.isoformat() if exam.start_time else None,
            'end_time': exam.end_time.isoformat() if exam.end_time else None,
            'duration_minutes': exam.duration_minutes,
            'my_attempt_status': attempt.status.value if attempt else None,
            'my_score': attempt.score if (attempt and attempt.score is not None and exam.results_published) else None,
        })
    finally:
        session.close()


@external_student_bp.route('/api/external/student/results', methods=['GET'])
@paseto_required
@api_key_required
@limiter.limit("60 per minute")
def external_student_results():
    denied = _forbid_unless_student_and_allowed()
    if denied:
        return denied

    user_id = get_current_user_id()
    session = get_session()
    try:
        attempts = session.query(ExamAttempt).options(
            joinedload(ExamAttempt.exam).joinedload(OnlineExam.subject)
        ).filter(
            ExamAttempt.student_id == user_id,
            ExamAttempt.corrected_at.isnot(None),
        ).order_by(desc(ExamAttempt.corrected_at)).all()

        results = []
        for att in attempts:
            exam = att.exam
            published = bool(exam.results_published) if exam else True
            results.append({
                'attempt_id': att.id,
                'exam_id': att.exam_id,
                'exam_title': exam.title if exam else '—',
                'subject_title': exam.subject.title if exam and exam.subject else None,
                'score': att.score if published else None,
                'feedback': att.feedback if published else None,
                'corrected_at': att.corrected_at.isoformat() if (att.corrected_at and published) else None,
                'results_published': published,
            })
        return jsonify({'results': results})
    finally:
        session.close()


@external_student_bp.route('/api/external/student/papers', methods=['GET'])
@paseto_required
@api_key_required
@limiter.limit("60 per minute")
def external_student_papers():
    denied = _forbid_unless_student_and_allowed()
    if denied:
        return denied

    user_id = get_current_user_id()
    session = get_session()
    try:
        papers = session.query(StudentPaper).options(joinedload(StudentPaper.subject)).filter_by(
            student_id=user_id
        ).order_by(desc(StudentPaper.created_at)).all()

        result = []
        for p in papers:
            published = bool(p.is_published)
            result.append({
                'id': p.id,
                'subject_title': p.subject.title if p.subject else None,
                'score': p.score if published else None,
                'grade': p.grade if published else None,
                'corrected_at': p.corrected_at.isoformat() if (p.corrected_at and published) else None,
                'is_published': published,
            })
        return jsonify({'papers': result})
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


@external_student_bp.route('/api/external/student/reclamations', methods=['GET'])
@paseto_required
@api_key_required
@limiter.limit("60 per minute")
def external_student_reclamations():
    denied = _forbid_unless_student_and_allowed()
    if denied:
        return denied

    from routes.reclamations import _serialize

    user_id = get_current_user_id()
    session = get_session()
    try:
        recs = session.query(Reclamation).options(
            joinedload(Reclamation.student),
            joinedload(Reclamation.paper).joinedload(StudentPaper.subject),
            joinedload(Reclamation.attempt).joinedload(ExamAttempt.exam),
        ).filter_by(student_id=user_id).order_by(desc(Reclamation.created_at)).all()
        return jsonify({'reclamations': [_serialize(r) for r in recs]})
    finally:
        session.close()


@external_student_bp.route('/api/external/student/reclamations', methods=['POST'])
@paseto_required
@api_key_required
@limiter.limit("60 per minute")
def external_student_create_reclamation():
    denied = _forbid_unless_student_and_allowed()
    if denied:
        return denied

    user_id = get_current_user_id()
    session = get_session()
    try:
        data = request.get_json(silent=True) or {}
        paper_id = data.get('paper_id')
        attempt_id = data.get('attempt_id')
        reason = (data.get('reason') or '').strip()

        if not reason or (not paper_id and not attempt_id):
            return jsonify({'error': 'Données manquantes (reason + paper_id ou attempt_id)'}), 400

        if paper_id:
            paper = session.query(StudentPaper).filter_by(id=paper_id).first()
            if not paper:
                return jsonify({'error': 'Copie non trouvée'}), 404
            if paper.student_id != user_id:
                return jsonify({'error': "Cette copie ne vous appartient pas"}), 403
            if paper.reclamation_window_end:
                rwe = paper.reclamation_window_end
                if rwe.tzinfo is None:
                    rwe = rwe.replace(tzinfo=timezone.utc)
                if rwe < utcnow():
                    return jsonify({'error': 'Période de réclamation expirée (7 jours)'}), 400
            if session.query(Reclamation).filter_by(paper_id=paper_id, status=ReclamationStatus.PENDING).first():
                return jsonify({'error': 'Une réclamation est déjà en cours'}), 400
            rec = Reclamation(paper_id=paper_id, student_id=user_id, reason=reason)
        else:
            attempt = session.query(ExamAttempt).filter_by(id=attempt_id, student_id=user_id).first()
            if not attempt:
                return jsonify({'error': 'Tentative non trouvée'}), 404
            if not attempt.corrected_at:
                return jsonify({'error': "La copie n'a pas encore été corrigée"}), 400
            corrected = attempt.corrected_at
            if corrected.tzinfo is None:
                corrected = corrected.replace(tzinfo=timezone.utc)
            if utcnow() > corrected + timedelta(days=7):
                return jsonify({'error': 'Période de réclamation expirée (7 jours)'}), 400
            if session.query(Reclamation).filter_by(attempt_id=attempt_id, status=ReclamationStatus.PENDING).first():
                return jsonify({'error': 'Une réclamation est déjà en cours'}), 400
            rec = Reclamation(attempt_id=attempt_id, student_id=user_id, reason=reason)

        session.add(rec)
        session.commit()
        return jsonify({'success': True, 'reclamation': rec.to_dict()}), 201
    except Exception as e:
        session.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        session.close()
