"""
Blueprint Professeur.

POST /api/reclamations/<id>/apply_proposal
POST /api/reclamations/<id>/reject_proposal
GET  /api/professor/dashboard
GET  /api/student/online_results
GET  /api/student/papers
"""
from flask import Blueprint, request, jsonify
from sqlalchemy import desc
from sqlalchemy.orm import joinedload

from auth_paseto import paseto_required, get_current_user_id
from helpers     import utcnow
from models      import (
    get_session, User, UserRole,
    Subject, StudentPaper, Reclamation, ReclamationStatus, CorrectionHistory,
    OnlineExam, ExamAttempt,
)

professor_bp = Blueprint('professor', __name__)


# ── Gestion des propositions IA ───────────────────────────────────────────────

@professor_bp.route('/api/reclamations/<int:reclamation_id>/apply_proposal', methods=['POST'])
@paseto_required
def apply_ai_proposal(reclamation_id):
    try:
        user_id = get_current_user_id()
        session = get_session()
        user    = session.query(User).filter_by(id=user_id).first()

        reclamation = session.query(Reclamation).options(
            joinedload(Reclamation.paper).joinedload(StudentPaper.subject)
        ).filter_by(id=reclamation_id).first()
        if not reclamation: session.close(); return jsonify({'error': 'Réclamation introuvable'}), 404

        paper = reclamation.paper
        if not paper:
            session.close()
            return jsonify({'error': "Impossible d'appliquer une proposition IA sur une réclamation d'examen en ligne. Répondez manuellement."}), 400
        subject = paper.subject

        if user.role != UserRole.ADMIN and not (user.role == UserRole.PROFESSOR and subject and subject.creator_id == user_id):
            session.close(); return jsonify({'error': 'Accès non autorisé'}), 403

        if not reclamation.ia_proposed_status:
            session.close(); return jsonify({'error': 'Aucune proposition IA disponible pour cette réclamation'}), 400

        old_score = paper.score
        old_grade = paper.grade
        new_score = reclamation.ia_proposed_score or old_score
        new_grade = reclamation.ia_proposed_grade or old_grade

        history = CorrectionHistory(
            paper_id=paper.id, corrector_id=user_id,
            old_score=old_score, new_score=new_score,
            old_grade=old_grade, new_grade=new_grade,
            reason=f"Application de la proposition IA: {reclamation.ia_proposed_reason or 'N/A'}",
        )
        session.add(history)

        paper.score      = new_score
        paper.grade      = new_grade
        paper.corrected_at = utcnow()

        reclamation.status           = ReclamationStatus.RESOLVED
        reclamation.response         = reclamation.ia_proposed_reason or 'Proposition IA acceptée'
        reclamation.responded_by_id  = user_id
        reclamation.updated_at       = utcnow()

        session.commit(); session.close()
        return jsonify({'success': True, 'message': 'Proposition IA appliquée'})
    except Exception as e:
        print(f"ERROR apply_ai_proposal: {e}")
        return jsonify({'error': str(e)}), 500


@professor_bp.route('/api/reclamations/<int:reclamation_id>/reject_proposal', methods=['POST'])
@paseto_required
def reject_ai_proposal(reclamation_id):
    try:
        user_id = get_current_user_id()
        session = get_session()
        user    = session.query(User).filter_by(id=user_id).first()

        reclamation = session.query(Reclamation).options(
            joinedload(Reclamation.paper).joinedload(StudentPaper.subject)
        ).filter_by(id=reclamation_id).first()
        if not reclamation: session.close(); return jsonify({'error': 'Réclamation introuvable'}), 404

        paper = reclamation.paper
        if not paper:
            session.close()
            return jsonify({'error': "Impossible de rejeter une proposition IA sur une réclamation d'examen en ligne. Répondez manuellement."}), 400
        subject = paper.subject

        if user.role != UserRole.ADMIN and not (user.role == UserRole.PROFESSOR and subject and subject.creator_id == user_id):
            session.close(); return jsonify({'error': 'Accès non autorisé'}), 403

        payload = request.get_json() or {}
        reclamation.status          = ReclamationStatus.REJECTED
        reclamation.response        = payload.get('response', 'Proposition IA rejetée par le professeur')
        reclamation.responded_by_id = user_id
        reclamation.updated_at      = utcnow()

        session.commit(); session.close()
        return jsonify({'success': True, 'message': 'Proposition IA rejetée'})
    except Exception as e:
        print(f"ERROR reject_ai_proposal: {e}")
        return jsonify({'error': str(e)}), 500


# ── Dashboard professeur ──────────────────────────────────────────────────────

@professor_bp.route('/api/professor/dashboard', methods=['GET'])
@paseto_required
def professor_dashboard():
    try:
        user_id = get_current_user_id()
        session = get_session()
        user    = session.query(User).filter_by(id=user_id).first()

        if user.role != UserRole.PROFESSOR:
            session.close(); return jsonify({'error': 'Accès réservé aux professeurs'}), 403

        my_subjects       = session.query(Subject).filter_by(creator_id=user_id).count()
        papers_corrected  = session.query(StudentPaper).filter_by(corrected_by_id=user_id).count()
        online_corrected  = session.query(ExamAttempt).join(
            OnlineExam, ExamAttempt.exam_id == OnlineExam.id
        ).filter(
            OnlineExam.created_by_id == user_id,
            ExamAttempt.score.isnot(None),
        ).count()

        session.close()
        return jsonify({
            'my_subjects':     my_subjects,
            'papers_corrected':papers_corrected + online_corrected,
        })
    except Exception as e:
        print(f"ERROR professor_dashboard: {e}")
        import traceback; traceback.print_exc()
        return jsonify({'error': str(e)}), 500


# ── Routes étudiant ───────────────────────────────────────────────────────────

@professor_bp.route('/api/student/online_results', methods=['GET'])
@paseto_required
def get_student_online_results():
    try:
        user_id = get_current_user_id()
        session = get_session()
        user    = session.query(User).filter_by(id=user_id).first()

        if not user or user.role != UserRole.STUDENT:
            session.close(); return jsonify([])

        attempts = session.query(ExamAttempt).options(
            joinedload(ExamAttempt.exam).joinedload(OnlineExam.subject)
        ).filter(
            ExamAttempt.student_id == user_id,
            ExamAttempt.corrected_at != None,
        ).order_by(desc(ExamAttempt.corrected_at)).all()

        results = []
        for att in attempts:
            exam    = att.exam
            subject = exam.subject if exam else None
            existing_rec = session.query(Reclamation).filter_by(
                attempt_id=att.id, student_id=user_id
            ).first()
            results.append({
                'attempt_id':        att.id,
                'exam_id':           att.exam_id,
                'exam_title':        exam.title   if exam    else '—',
                'subject_title':     subject.title if subject else None,
                'score':             att.score,
                'feedback':          att.feedback,
                'corrected_at':      att.corrected_at.isoformat() if att.corrected_at else None,
                'submitted_at':      att.submitted_at.isoformat() if att.submitted_at else None,
                'auto_correct':      exam.auto_correct if exam else False,
                'has_reclamation':   existing_rec is not None,
                'reclamation_status':existing_rec.status.value if existing_rec else None,
            })

        session.close()
        return jsonify(results)
    except Exception as e:
        print(f"ERROR get_student_online_results: {e}")
        return jsonify([])


@professor_bp.route('/api/student/papers', methods=['GET'])
@paseto_required
def get_student_papers():
    try:
        user_id = get_current_user_id()
        session = get_session()
        user    = session.query(User).filter_by(id=user_id).first()

        if user.role != UserRole.STUDENT:
            session.close(); return jsonify({'error': 'Accès réservé aux étudiants'}), 403

        papers = session.query(StudentPaper).options(
            joinedload(StudentPaper.subject)
        ).filter_by(student_id=user_id).order_by(desc(StudentPaper.created_at)).all()

        paper_ids = [p.id for p in papers]
        recs_by_paper = {}
        if paper_ids:
            for r in session.query(Reclamation).filter(
                Reclamation.paper_id.in_(paper_ids),
                Reclamation.student_id == user_id,
            ).all():
                recs_by_paper[r.paper_id] = r

        papers_list = []
        for p in papers:
            d   = p.to_dict()
            rec = recs_by_paper.get(p.id)
            d['has_reclamation']   = rec is not None
            d['reclamation_status']= rec.status.value if rec else None
            papers_list.append(d)

        session.close()
        return jsonify(papers_list)
    except Exception as e:
        print(f"ERROR get_student_papers: {e}")
        return jsonify({'error': str(e)}), 500
