"""
Blueprint Réclamations — Contrôleur MVC.

Routes :
  GET  /api/reclamations
  POST /api/reclamations
  PUT  /api/reclamations/<id>
  POST /api/reclamations/<id>/process_ia
  POST /api/reclamations/<id>/apply_proposal
  POST /api/reclamations/<id>/reject_proposal

Migré depuis app.py — logique identique.
"""
import re
from datetime import timedelta, timezone
from flask import Blueprint, request, jsonify
from sqlalchemy.orm import joinedload
from sqlalchemy import desc

from auth_paseto   import paseto_required, get_current_user_id
from helpers       import utcnow
from models        import (
    get_session, User, UserRole,
    StudentPaper, CorrectionHistory,
    Reclamation, ReclamationStatus,
    ExamAttempt, OnlineExam,
)
from services.ai_service import call_ai as _call_ai

reclamations_bp = Blueprint('reclamations', __name__)


def _call_claude(system_prompt: str, user_message: str, temperature: float = 0.2) -> str:
    return _call_ai(system_prompt, user_message, temperature=temperature)


def _extract_score(text: str) -> float:
    patterns = [
        r'Note totale\s*:\s*(\d+\.?\d*)\s*/\s*20',
        r'Note totale\s*:\s*(\d+\.?\d*)\s*/\s*(\d+)',
        r'Score\s*:\s*(\d+\.?\d*)\s*/\s*20',
        r'Total\s*:\s*(\d+\.?\d*)\s*/\s*20',
        r'Note finale\s*:\s*(\d+\.?\d*)\s*/\s*20',
        r'Note\s*:\s*(\d+\.?\d*)\s*/\s*20',
        r'(\d+\.?\d*)\s*/\s*20\s*points?',
        r'(\d+\.?\d*)\s*sur\s*20',
    ]
    for pattern in patterns:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            score = float(m.group(1))
            if len(m.groups()) > 1 and m.group(2):
                score = (score / float(m.group(2))) * 20
            return round(score, 2)
    return 0.0


def _serialize(r: Reclamation) -> dict:
    if r.paper and r.paper.subject:
        subject_title = r.paper.subject.title
    elif r.attempt and r.attempt.exam:
        subject_title = r.attempt.exam.title
    else:
        subject_title = 'Sujet supprimé'
    return {
        'id':                  r.id,
        'paper_id':            r.paper_id,
        'attempt_id':          r.attempt_id,
        'type':                'online_exam' if r.attempt_id else 'paper',
        'student_id':          r.student_id,
        'student_name':        r.student.full_name if r.student else 'Inconnu',
        'subject_title':       subject_title,
        'exam_title':          r.attempt.exam.title if r.attempt and r.attempt.exam else None,
        'attempt_score':       r.attempt.score if r.attempt else None,
        'attempt_feedback':    r.attempt.feedback if r.attempt else None,
        'reason':              r.reason,
        'status':              r.status.value,
        'response':            r.response,
        'ia_decision':         r.ia_decision,
        'ia_proposed_status':  r.ia_proposed_status,
        'ia_proposed_score':   r.ia_proposed_score,
        'ia_proposed_grade':   r.ia_proposed_grade,
        'ia_proposed_reason':  r.ia_proposed_reason,
        'ia_processed_at':     r.ia_processed_at.isoformat() if r.ia_processed_at else None,
        'responded_by_id':     r.responded_by_id,
        'responder_name':      r.responder.full_name if r.responder else None,
        'created_at':          r.created_at.isoformat() if r.created_at else None,
        'updated_at':          r.updated_at.isoformat() if r.updated_at else None,
    }


# ── GET liste ─────────────────────────────────────────────────────────────────
@reclamations_bp.route('/api/reclamations', methods=['GET'])
@paseto_required
def get_reclamations():
    try:
        user_id = get_current_user_id()
        session = get_session()
        user    = session.query(User).filter_by(id=user_id).first()

        query = session.query(Reclamation).options(
            joinedload(Reclamation.student),
            joinedload(Reclamation.paper).joinedload(StudentPaper.subject),
            joinedload(Reclamation.attempt).joinedload(ExamAttempt.exam),
        )

        if user.role == UserRole.STUDENT:
            recs = query.filter_by(student_id=user_id).order_by(desc(Reclamation.created_at)).all()
        elif user.role == UserRole.PROFESSOR:
            paper_ids  = [r.id for r in session.query(Reclamation)
                          .join(StudentPaper, Reclamation.paper_id == StudentPaper.id)
                          .filter(StudentPaper.corrected_by_id == user_id).all()]
            online_ids = [r.id for r in session.query(Reclamation)
                          .join(ExamAttempt, Reclamation.attempt_id == ExamAttempt.id)
                          .join(OnlineExam, ExamAttempt.exam_id == OnlineExam.id)
                          .filter(OnlineExam.created_by_id == user_id).all()]
            visible = list(set(paper_ids + online_ids))
            recs = query.filter(Reclamation.id.in_(visible)).order_by(desc(Reclamation.created_at)).all() if visible else []
        else:
            recs = query.order_by(desc(Reclamation.created_at)).all()

        result = [_serialize(r) for r in recs]
        session.close()
        return jsonify(result)
    except Exception as e:
        print(f"ERROR get_reclamations: {e}")
        import traceback; traceback.print_exc()
        return jsonify({'error': str(e)}), 500


# ── POST création ─────────────────────────────────────────────────────────────
@reclamations_bp.route('/api/reclamations', methods=['POST'])
@paseto_required
def create_reclamation():
    try:
        user_id = get_current_user_id()
        session = get_session()
        user    = session.query(User).filter_by(id=user_id).first()

        if user.role != UserRole.STUDENT:
            session.close()
            return jsonify({'error': 'Seuls les étudiants peuvent créer des réclamations'}), 403

        data       = request.json or {}
        paper_id   = data.get('paper_id')
        attempt_id = data.get('attempt_id')
        reason     = (data.get('reason') or '').strip()

        if not reason or (not paper_id and not attempt_id):
            session.close()
            return jsonify({'error': 'Données manquantes (reason + paper_id ou attempt_id)'}), 400

        if paper_id:
            paper = session.query(StudentPaper).filter_by(id=paper_id).first()
            if not paper: session.close(); return jsonify({'error': 'Copie non trouvée'}), 404
            if paper.student_id != user_id:
                session.close(); return jsonify({'error': 'Cette copie ne vous appartient pas'}), 403
            if paper.reclamation_window_end:
                rwe = paper.reclamation_window_end
                if rwe.tzinfo is None: rwe = rwe.replace(tzinfo=timezone.utc)
                if rwe < utcnow():
                    session.close(); return jsonify({'error': 'Période de réclamation expirée (7 jours)'}), 400
            if session.query(Reclamation).filter_by(paper_id=paper_id, status=ReclamationStatus.PENDING).first():
                session.close(); return jsonify({'error': 'Une réclamation est déjà en cours'}), 400
            rec = Reclamation(paper_id=paper_id, student_id=user_id, reason=reason)
        else:
            attempt = session.query(ExamAttempt).filter_by(id=attempt_id, student_id=user_id).first()
            if not attempt: session.close(); return jsonify({'error': 'Tentative non trouvée'}), 404
            if not attempt.corrected_at:
                session.close(); return jsonify({'error': "La copie n'a pas encore été corrigée"}), 400
            corrected = attempt.corrected_at
            if corrected.tzinfo is None: corrected = corrected.replace(tzinfo=timezone.utc)
            if utcnow() > corrected + timedelta(days=7):
                session.close(); return jsonify({'error': 'Période de réclamation expirée (7 jours)'}), 400
            if session.query(Reclamation).filter_by(attempt_id=attempt_id, status=ReclamationStatus.PENDING).first():
                session.close(); return jsonify({'error': 'Une réclamation est déjà en cours'}), 400
            rec = Reclamation(attempt_id=attempt_id, student_id=user_id, reason=reason)

        session.add(rec); session.commit()
        result = rec.to_dict(); session.close()
        return jsonify({'success': True, 'reclamation': result}), 201
    except Exception as e:
        print(f"ERROR create_reclamation: {e}")
        return jsonify({'error': str(e)}), 500


# ── PUT réponse professeur/admin ──────────────────────────────────────────────
@reclamations_bp.route('/api/reclamations/<int:rid>', methods=['PUT'])
@paseto_required
def respond_reclamation(rid):
    try:
        user_id = get_current_user_id()
        session = get_session()
        user    = session.query(User).filter_by(id=user_id).first()

        if user.role not in [UserRole.PROFESSOR, UserRole.ADMIN]:
            session.close(); return jsonify({'error': 'Accès non autorisé'}), 403

        rec = session.query(Reclamation).filter_by(id=rid).first()
        if not rec: session.close(); return jsonify({'error': 'Réclamation non trouvée'}), 404

        data      = request.json or {}
        status    = data.get('status')
        response  = data.get('response') or data.get('admin_response')
        new_score = data.get('new_score')

        # Accepter aussi approved/rejected (utilisé par le frontend)
        status_map = {
            'approved': 'resolved', 'rejected': 'rejected', 'rejected': 'rejected',
            'in_review': 'in_review', 'resolved': 'resolved',
        }
        mapped = status_map.get(status, status)
        if not mapped or mapped not in ['in_review', 'resolved', 'rejected']:
            session.close(); return jsonify({'error': 'Statut invalide'}), 400

        rec.status           = ReclamationStatus[mapped.upper()]
        rec.response         = response
        rec.responded_by_id  = user_id
        rec.updated_at       = utcnow()

        if mapped == 'resolved' and new_score is not None:
            paper = session.query(StudentPaper).filter_by(id=rec.paper_id).first()
            if paper:
                session.add(CorrectionHistory(
                    paper_id=paper.id, corrector_id=user_id,
                    old_score=paper.score, new_score=new_score,
                    old_grade=paper.grade,
                    new_grade=f"Modifié suite à réclamation: {new_score}/20",
                    reason=f"Réclamation acceptée: {response}",
                ))
                paper.score = new_score; paper.corrected_at = utcnow()

        session.commit()
        result = {'id': rec.id, 'status': rec.status.value,
                  'response': rec.response,
                  'updated_at': rec.updated_at.isoformat() if rec.updated_at else None}
        session.close()
        return jsonify({'success': True, 'reclamation': result})
    except Exception as e:
        print(f"ERROR respond_reclamation: {e}")
        return jsonify({'error': str(e)}), 500


# ── POST traitement IA ────────────────────────────────────────────────────────
@reclamations_bp.route('/api/reclamations/<int:rid>/process_ia', methods=['POST'])
@paseto_required
def process_reclamation_ia(rid):
    try:
        user_id = get_current_user_id()
        session = get_session()
        user    = session.query(User).filter_by(id=user_id).first()

        if user.role not in [UserRole.PROFESSOR, UserRole.ADMIN]:
            session.close(); return jsonify({'error': 'Accès non autorisé'}), 403

        rec = session.query(Reclamation).filter_by(id=rid).first()
        if not rec: session.close(); return jsonify({'error': 'Réclamation non trouvée'}), 404
        if rec.status != ReclamationStatus.PENDING:
            session.close(); return jsonify({'error': 'Réclamation déjà traitée'}), 400

        paper   = rec.paper
        attempt = rec.attempt
        if not paper and not attempt:
            session.close(); return jsonify({'error': "Réclamation sans copie — impossible d'analyser."}), 400

        DECISION_FORMAT = """Format de sortie OBLIGATOIRE:
=== DÉCISION ===
[RESOLVED ou REJECTED]

=== RAISON ===
[Explication détaillée]

=== NOUVELLE NOTE ===
Si RESOLVED: XX.XX/20
Si REJECTED: Note originale inchangée

=== NOUVELLE CORRECTION ===
Si RESOLVED: Correction révisée
Si REJECTED: Correction originale inchangée"""

        if paper:
            subject = paper.subject
            original_score = paper.score or 0
            system_prompt = f"Tu es un arbitre impartial pour les réclamations de notes d'examen.\nAnalyse la réclamation et décide si elle est valide.\n\n{DECISION_FORMAT}"
            user_message  = (
                f"SUJET: {subject.content if subject else 'N/A'}\n"
                f"BARÈME: {subject.rubric if subject else 'N/A'}\n"
                f"COPIE: {paper.content}\n"
                f"CORRECTION ORIGINALE: {paper.grade} (Note: {paper.score}/20)\n"
                f"RÉCLAMATION: {rec.reason}\n\nAnalyse et décide."
            )
        else:
            exam = attempt.exam
            original_score = attempt.score or 0
            system_prompt = f"Tu es un arbitre impartial pour les réclamations d'examens en ligne.\n\n{DECISION_FORMAT}"
            user_message  = (
                f"EXAMEN: {exam.title if exam else 'N/A'}\n"
                f"INSTRUCTIONS: {exam.instructions[:500] if exam and exam.instructions else 'N/A'}\n"
                f"RÉPONSES: {(attempt.answers or 'N/A')[:3000]}\n"
                f"CORRECTION: {(attempt.feedback or 'N/A')[:3000]} (Note: {original_score}/20)\n"
                f"RÉCLAMATION: {rec.reason}\n\nAnalyse et décide."
            )

        ia_response = _call_claude(system_prompt, user_message, temperature=0.1)

        decision_m  = re.search(r'=== DÉCISION ===\n(RESOLVED|REJECTED)', ia_response)
        reason_m    = re.search(r'=== RAISON ===\n(.*?)=== NOUVELLE NOTE ===', ia_response, re.DOTALL)
        score_m     = re.search(r'=== NOUVELLE NOTE ===\n(.*?)=== NOUVELLE CORRECTION ===', ia_response, re.DOTALL)
        grade_m     = re.search(r'=== NOUVELLE CORRECTION ===\n(.*)', ia_response, re.DOTALL)

        if not decision_m:
            session.close(); return jsonify({'error': 'Réponse IA invalide'}), 500

        decision  = decision_m.group(1)
        reason    = reason_m.group(1).strip() if reason_m else ''
        new_grade = grade_m.group(1).strip() if grade_m else (paper.grade if paper else '')
        new_score = original_score
        if decision == 'RESOLVED' and score_m:
            new_score = _extract_score(score_m.group(1).strip())

        rec.ia_decision         = ia_response
        rec.ia_proposed_status  = 'resolved' if decision == 'RESOLVED' else 'rejected'
        rec.ia_proposed_reason  = reason
        rec.ia_proposed_grade   = new_grade if decision == 'RESOLVED' else None
        rec.ia_proposed_score   = new_score if decision == 'RESOLVED' else original_score
        rec.ia_processed_at     = utcnow()
        rec.updated_at          = utcnow()
        session.commit(); session.close()

        return jsonify({'success': True, 'decision': decision, 'ia_response': ia_response})
    except Exception as e:
        print(f"ERROR process_reclamation_ia: {e}")
        return jsonify({'error': str(e)}), 500


# ── POST appliquer proposition IA ─────────────────────────────────────────────
@reclamations_bp.route('/api/reclamations/<int:rid>/apply_proposal', methods=['POST'])
@paseto_required
def apply_ai_proposal(rid):
    try:
        user_id = get_current_user_id()
        session = get_session()
        user    = session.query(User).filter_by(id=user_id).first()

        rec = session.query(Reclamation).options(
            joinedload(Reclamation.paper).joinedload(StudentPaper.subject)
        ).filter_by(id=rid).first()
        if not rec: session.close(); return jsonify({'error': 'Réclamation introuvable'}), 404

        paper   = rec.paper
        if not paper:
            session.close()
            return jsonify({'error': "Répondez manuellement pour les réclamations d'examens en ligne."}), 400

        subject = paper.subject
        if user.role != UserRole.ADMIN and not (
            user.role == UserRole.PROFESSOR and subject and subject.creator_id == user_id
        ):
            session.close(); return jsonify({'error': 'Accès non autorisé'}), 403

        if not rec.ia_proposed_status:
            session.close(); return jsonify({'error': 'Aucune proposition IA disponible'}), 400

        old_score = paper.score; old_grade = paper.grade
        new_score = rec.ia_proposed_score or old_score
        new_grade = rec.ia_proposed_grade or old_grade

        session.add(CorrectionHistory(
            paper_id=paper.id, corrector_id=user_id,
            old_score=old_score, new_score=new_score,
            old_grade=old_grade, new_grade=new_grade,
            reason=f"Proposition IA: {rec.ia_proposed_reason or 'N/A'}",
        ))
        paper.score = new_score; paper.grade = new_grade; paper.corrected_at = utcnow()
        rec.status           = ReclamationStatus.RESOLVED
        rec.response         = rec.ia_proposed_reason or 'Proposition IA acceptée'
        rec.responded_by_id  = user_id
        rec.updated_at       = utcnow()
        session.commit(); session.close()
        return jsonify({'success': True, 'message': 'Proposition IA appliquée'})
    except Exception as e:
        print(f"ERROR apply_ai_proposal: {e}")
        return jsonify({'error': str(e)}), 500


# ── POST rejeter proposition IA ───────────────────────────────────────────────
@reclamations_bp.route('/api/reclamations/<int:rid>/reject_proposal', methods=['POST'])
@paseto_required
def reject_ai_proposal(rid):
    try:
        user_id = get_current_user_id()
        session = get_session()
        user    = session.query(User).filter_by(id=user_id).first()

        rec = session.query(Reclamation).options(
            joinedload(Reclamation.paper).joinedload(StudentPaper.subject)
        ).filter_by(id=rid).first()
        if not rec: session.close(); return jsonify({'error': 'Réclamation introuvable'}), 404

        paper   = rec.paper
        if not paper:
            session.close()
            return jsonify({'error': "Répondez manuellement pour les réclamations d'examens en ligne."}), 400

        subject = paper.subject
        if user.role != UserRole.ADMIN and not (
            user.role == UserRole.PROFESSOR and subject and subject.creator_id == user_id
        ):
            session.close(); return jsonify({'error': 'Accès non autorisé'}), 403

        payload              = request.get_json() or {}
        rec.status           = ReclamationStatus.REJECTED
        rec.response         = payload.get('response', 'Proposition IA rejetée')
        rec.responded_by_id  = user_id
        rec.updated_at       = utcnow()
        session.commit(); session.close()
        return jsonify({'success': True, 'message': 'Proposition IA rejetée'})
    except Exception as e:
        print(f"ERROR reject_ai_proposal: {e}")
        return jsonify({'error': str(e)}), 500
