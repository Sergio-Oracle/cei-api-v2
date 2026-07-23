"""
Blueprint Exams (Examens en ligne, tentatives, correction IA).

Routes : online_exams CRUD, activation, fermeture, start/submit/correct,
         bilan, stats, incidents, plagiat, rapport intégrité PDF, QR code,
         analytics professeur, historique admin, etc.
"""
import io, csv, json, re, os, threading, statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import Blueprint, request, jsonify, send_file, make_response, Response, current_app
from sqlalchemy import desc, func as sa_func
from sqlalchemy.orm import joinedload

from auth_paseto import paseto_required, get_current_user_id, get_current_user_role
from helpers     import utcnow, strip_bareme_from_content as _strip_bareme_from_content
from models      import (
    get_session, User, UserRole,
    Subject, StudentPaper, Reclamation, CorrectionHistory,
    OnlineExam, ExamAttempt, ExamActivityLog, GradeTranscript,
    CameraLog, ExamStatus, AttemptStatus, ExamProctor, ProctorAssignment,
    QuestionBank, EC, ECAssignment, StudentUEEnrollment,
    SubjectMedia, IncidentDismissal,
)
from werkzeug.utils import secure_filename
from utils import (
    send_paper_corrected_email, allowed_file, extract_text_from_file,
)
from services.ai_service import (
    call_ai             as call_claude,
    call_ai_simple,
    analyze_media,
    extract_score       as extract_score_from_correction,
    build_correction_prompt as _build_correction_system_prompt,
)
from s3_client import upload_subject_media, get_snapshot_url
from routes.question_bank import _similarity, DUPLICATE_THRESHOLD

exams_bp = Blueprint('exams', __name__)

_UPLOAD_FOLDER = os.getenv('UPLOAD_FOLDER', 'static/uploads')

@exams_bp.route('/api/online_exams', methods=['GET'])
@paseto_required
def get_online_exams():
    """Liste des examens en ligne"""
    try:
        user_id = get_current_user_id()
        session = get_session()
        user = session.query(User).filter_by(id=user_id).first()
        
        query = session.query(OnlineExam).options(
            joinedload(OnlineExam.subject),
            joinedload(OnlineExam.creator)
        )
        
        if user.role == UserRole.STUDENT:
            # Étudiants : examens actifs/planifiés + examens terminés (participé OU fermés dans les 7 derniers jours)
            active_exams = query.filter(OnlineExam.status.in_([ExamStatus.SCHEDULED, ExamStatus.ACTIVE])).all()
            participated_ids = set(
                a.exam_id for a in session.query(ExamAttempt.exam_id)
                .filter_by(student_id=user_id).all()
            )
            recent_cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=7)
            recent_closed_ids = set(
                r.id for r in session.query(OnlineExam.id).filter(
                    OnlineExam.status == ExamStatus.CLOSED,
                    OnlineExam.end_time >= recent_cutoff
                ).all()
            )
            all_closed_ids = participated_ids | recent_closed_ids
            closed_exams = query.filter(
                OnlineExam.id.in_(list(all_closed_ids)),
                OnlineExam.status == ExamStatus.CLOSED
            ).all() if all_closed_ids else []
            exams = active_exams + closed_exams
        elif user.role == UserRole.PROFESSOR:
            # Professeurs : leurs propres examens
            exams = query.filter_by(created_by_id=user_id).all()
        else:
            # Admin : tous
            exams = query.all()

        # Pré-charger toutes les tentatives de l'étudiant en une seule requête (évite N+1)
        attempts_by_exam = {}
        if user.role == UserRole.STUDENT:
            student_attempts = session.query(ExamAttempt).filter_by(student_id=user_id).all()
            attempts_by_exam = {a.exam_id: a for a in student_attempts}

        # Auto-close exams whose end_time has passed
        now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
        needs_commit = False
        for exam in exams:
            if exam.status == ExamStatus.ACTIVE and exam.end_time and exam.end_time < now_utc:
                exam.status = ExamStatus.CLOSED
                in_progress = session.query(ExamAttempt).filter_by(
                    exam_id=exam.id, status=AttemptStatus.IN_PROGRESS
                ).all()
                for att in in_progress:
                    att.status = AttemptStatus.AUTO_SUBMITTED
                    att.submitted_at = now_utc
                needs_commit = True
                print(f"⏰ Auto-close examen #{exam.id} '{exam.title}' (end_time dépassé)")
        if needs_commit:
            session.commit()
            # Recharger les tentatives après commit
            if user.role == UserRole.STUDENT:
                student_attempts = session.query(ExamAttempt).filter_by(student_id=user_id).all()
                attempts_by_exam = {a.exam_id: a for a in student_attempts}

        exams_list = []
        for exam in exams:
            d = exam.to_dict()
            if user.role == UserRole.STUDENT:
                attempt = attempts_by_exam.get(exam.id)
                if attempt:
                    d['my_attempt'] = {
                        'id':           attempt.id,
                        'status':       attempt.status.value,
                        'score':        attempt.score,
                        'feedback':     attempt.feedback,
                        'corrected_at': attempt.corrected_at.isoformat() if attempt.corrected_at else None,
                        'submitted_at': attempt.submitted_at.isoformat() if attempt.submitted_at else None,
                    }
                else:
                    d['my_attempt'] = None
            exams_list.append(d)
        session.close()
        return jsonify(exams_list)
    except Exception as e:
        print(f"❌ Erreur get_online_exams: {e}")
        try: session.rollback(); session.close()
        except: pass
        return jsonify({'error': str(e)}), 500

# Exemple pour l'endpoint create_online_exam (ligne ~1570)
@exams_bp.route('/api/online_exams', methods=['POST'])
@paseto_required
def create_online_exam():
    """Créer un examen en ligne — le frontend envoie déjà du UTC via toISOString()"""
    try:
        user_id = get_current_user_id()
        session = get_session()

        user = session.query(User).filter_by(id=user_id).first()
        if user.role not in [UserRole.PROFESSOR, UserRole.ADMIN]:
            session.close()
            return jsonify({'error': 'Accès réservé aux professeurs et administrateurs'}), 403

        data = request.get_json(silent=True) or {}

        # Validation
        required_fields = ['subject_id', 'title', 'start_time', 'end_time']
        for field in required_fields:
            if field not in data:
                session.close()
                return jsonify({'error': f'Le champ "{field}" est requis'}), 400

        # Vérifier le sujet
        subject = session.query(Subject).filter_by(id=data['subject_id']).first()
        if not subject:
            session.close()
            return jsonify({'error': 'Le sujet sélectionné n\'existe pas'}), 404

        # Le frontend envoie la valeur brute du datetime-local + "Z"
        # (ex: "2026-03-30T18:05:00Z"). Dakar = UTC+0, donc la valeur
        # saisie EST déjà l'heure UTC. On parse puis on stocke naïf (sans tzinfo)
        # pour éviter que psycopg2 ne convertisse en Europe/Berlin avant stockage.
        try:
            raw_start = data['start_time'].strip().replace('Z', '+00:00')
            raw_end   = data['end_time'].strip().replace('Z', '+00:00')
            if '+' not in raw_start and raw_start[-6:] != '+00:00':
                raw_start += '+00:00'
            if '+' not in raw_end and raw_end[-6:] != '+00:00':
                raw_end += '+00:00'
            # Convertir en UTC puis supprimer tzinfo → stockage naïf UTC dans PG
            start_time = datetime.fromisoformat(raw_start).astimezone(timezone.utc).replace(tzinfo=None)
            end_time   = datetime.fromisoformat(raw_end).astimezone(timezone.utc).replace(tzinfo=None)

            # Validation : Fin > Début
            if end_time <= start_time:
                session.close()
                return jsonify({'error': 'La date de fin doit être après la date de début'}), 400

            # Calcul durée auto en minutes
            duration_minutes = int((end_time - start_time).total_seconds() / 60)
            if duration_minutes <= 0 or duration_minutes > 1440:  # Max 24h
                session.close()
                return jsonify({'error': 'Durée invalide (doit être entre 1 min et 24h)'}), 400

        except ValueError as ve:
            session.close()
            return jsonify({'error': f'Format de date invalide: {str(ve)}'}), 400
       
        # Créer l'examen avec durée calculée
        exam = OnlineExam(
            subject_id=data['subject_id'],
            title=data['title'],
            instructions=data.get('instructions', ''),
            duration_minutes=duration_minutes,  # Auto-calculé
            start_time=start_time,
            end_time=end_time,
            max_tab_switches=data.get('max_tab_switches', 2),
            enable_copy_paste=data.get('enable_copy_paste', False),
            enable_right_click=data.get('enable_right_click', False),
            randomize_questions=data.get('randomize_questions', False),
            questions_per_page=data.get('questions_per_page', 5),
            max_no_face_count=data.get('max_no_face_count', 10),
            ban_on_devtools=data.get('ban_on_devtools', True),
            auto_correct=data.get('auto_correct', False),
            status=ExamStatus.SCHEDULED,
            created_by_id=user_id
        )
       
        session.add(exam)
        session.commit()
        exam_dict = exam.to_dict()
        print(f"✅ Examen créé: {exam.title} stocké de {start_time} à {end_time} UTC (durée: {duration_minutes} min)")

        # Auto-assignation des surveillants des groupes rattachés à l'EC du sujet,
        # et pré-répartition des étudiants inscrits entre eux — seule source de
        # vérité désormais (Groupes Surveillants), plus de gestion manuelle par
        # examen (Notes point 6/9 — "prévoir les groupes des surveillants par ECs")
        if subject.ec_id:
            from services.proctor_service import sync_ec_proctors
            sync_ec_proctors(session, subject.ec_id)

        session.close()

        return jsonify({'success': True, 'exam': exam_dict}), 201
    except Exception as e:
        print(f"❌ Erreur create_online_exam: {e}")
        return jsonify({'error': 'Erreur lors de la création de l\'examen'}), 500

@exams_bp.route('/api/online_exams/<int:exam_id>/activate', methods=['POST'])
@paseto_required
def activate_online_exam(exam_id):
    """Activer un examen (le rendre disponible aux étudiants)"""
    try:
        user_id = get_current_user_id()
        session = get_session()
        
        user = session.query(User).filter_by(id=user_id).first()
        if user.role not in [UserRole.PROFESSOR, UserRole.ADMIN]:
            session.close()
            return jsonify({'error': 'Accès non autorisé'}), 403
        
        exam = session.query(OnlineExam).filter_by(id=exam_id).first()
        if not exam:
            session.close()
            return jsonify({'error': 'Examen non trouvé'}), 404
        
        # Vérifier propriété (prof) ou admin
        if user.role == UserRole.PROFESSOR and exam.created_by_id != user_id:
            session.close()
            return jsonify({'error': 'Vous ne pouvez activer que vos propres examens'}), 403
        
        # Passer en statut ACTIVE
        exam.status = ExamStatus.ACTIVE
        session.commit()
        
        exam_dict = exam.to_dict()

        # Notifier par email tous les étudiants inscrits à la formation de l'examen
        try:
            app_url  = os.getenv('APP_URL', 'https://dev-cei.ddns.net').rstrip('/')
            exam_url = f"{app_url}/app"
            end_str  = exam.end_time.strftime('%d/%m/%Y à %H:%M') if exam.end_time else 'voir sur la plateforme'
            from models import StudentUEEnrollment, EC as ECModel, UE as UEModel
            # Récupérer les EC liés à cet examen
            ec = session.query(ECModel).filter_by(id=exam.ec_id).first() if hasattr(exam, 'ec_id') and exam.ec_id else None
            if ec and ec.ue:
                enrollments = session.query(StudentUEEnrollment).filter_by(ue_id=ec.ue_id).all()
                for enr in enrollments:
                    student = enr.student
                    if student and student.email and student.is_active:
                        try:
                            send_exam_started_email(student.email, student.full_name, exam.title, exam_url, end_str)
                        except Exception:
                            pass
        except Exception:
            pass

        session.close()
        return jsonify({'success': True, 'exam': exam_dict})
    except Exception as e:
        print(f"Erreur activate_online_exam: {e}")
        return jsonify({'error': str(e)}), 500

@exams_bp.route('/api/online_exams/<int:exam_id>/extend', methods=['POST'])
@paseto_required
def extend_online_exam(exam_id):
    try:
        user_id = get_current_user_id()
        session = get_session()
        user = session.query(User).filter_by(id=user_id).first()
        if user.role not in [UserRole.PROFESSOR, UserRole.ADMIN]:
            session.close()
            return jsonify({'error': 'Accès non autorisé'}), 403
        exam = session.query(OnlineExam).filter_by(id=exam_id).first()
        if not exam:
            session.close()
            return jsonify({'error': 'Examen non trouvé'}), 404
        if user.role == UserRole.PROFESSOR and exam.created_by_id != user_id:
            session.close()
            return jsonify({'error': 'Vous ne pouvez modifier que vos propres examens'}), 403
        if exam.status not in [ExamStatus.ACTIVE, ExamStatus.SCHEDULED]:
            session.close()
            return jsonify({'error': 'Impossible de modifier un examen terminé'}), 400
        data = request.json or {}
        extra_minutes = int(data.get('extra_minutes', 0))
        if extra_minutes <= 0 or extra_minutes > 300:
            session.close()
            return jsonify({'error': 'Durée invalide (1–300 minutes)'}), 400
        exam.end_time = exam.end_time + timedelta(minutes=extra_minutes)
        exam.duration_minutes = exam.duration_minutes + extra_minutes
        session.commit()
        exam_dict = exam.to_dict()
        session.close()
        return jsonify({
            'success': True,
            'message': f'Durée prolongée de {extra_minutes} minutes',
            'new_end_time': exam_dict.get('end_time'),
            'new_duration_minutes': exam_dict.get('duration_minutes'),
        })
    except Exception as e:
        print(f"❌ Erreur extend_online_exam: {e}")
        return jsonify({'error': str(e)}), 500


@exams_bp.route('/api/online_exams/<int:exam_id>/close', methods=['POST'])
@paseto_required
def close_online_exam(exam_id):
    """Fermer un examen (terminer)"""
    try:
        user_id = get_current_user_id()
        session = get_session()
        
        user = session.query(User).filter_by(id=user_id).first()
        if user.role not in [UserRole.PROFESSOR, UserRole.ADMIN]:
            session.close()
            return jsonify({'error': 'Accès non autorisé'}), 403
        
        exam = session.query(OnlineExam).filter_by(id=exam_id).first()
        if not exam:
            session.close()
            return jsonify({'error': 'Examen non trouvé'}), 404
        
        if user.role == UserRole.PROFESSOR and exam.created_by_id != user_id:
            session.close()
            return jsonify({'error': 'Accès non autorisé'}), 403
        
        # Auto-soumettre toutes les tentatives encore en cours avant de fermer
        in_progress = session.query(ExamAttempt).filter_by(
            exam_id=exam_id, status=AttemptStatus.IN_PROGRESS
        ).all()
        for att in in_progress:
            att.status = AttemptStatus.AUTO_SUBMITTED

        # Passer en statut CLOSED
        exam.status = ExamStatus.CLOSED
        session.commit()

        exam_dict      = exam.to_dict()
        prof_email     = user.email
        prof_name      = user.full_name or user.username
        exam_id_local  = exam.id

        session.close()

        # Envoyer le résumé par email en arrière-plan
        import threading as _threading
        _threading.Thread(
            target=_send_exam_closure_summary,
            args=(exam_id_local, prof_email, prof_name),
            daemon=True
        ).start()

        return jsonify({'success': True, 'exam': exam_dict})
    except Exception as e:
        print(f"❌ Erreur close_online_exam: {e}")
        return jsonify({'error': str(e)}), 500

@exams_bp.route('/api/admin/online_exams/<int:exam_id>', methods=['PUT'])
@paseto_required
def edit_online_exam(exam_id):
    """Modifier le titre, la date de début et la durée d'un examen (draft/scheduled)"""
    session = None
    try:
        user_id = get_current_user_id()
        session = get_session()
        user = session.query(User).filter_by(id=user_id).first()
        if user.role not in [UserRole.PROFESSOR, UserRole.ADMIN]:
            session.close()
            return jsonify({'error': 'Accès non autorisé'}), 403
        exam = session.query(OnlineExam).filter_by(id=exam_id).first()
        if not exam:
            session.close()
            return jsonify({'error': 'Examen non trouvé'}), 404
        if user.role == UserRole.PROFESSOR and exam.created_by_id != user_id:
            session.close()
            return jsonify({'error': 'Vous ne pouvez modifier que vos propres examens'}), 403
        if exam.status not in [ExamStatus.DRAFT, ExamStatus.SCHEDULED]:
            session.close()
            return jsonify({'error': 'Seuls les examens en brouillon ou planifiés peuvent être modifiés'}), 400
        data = request.get_json() or {}
        if 'title' in data and data['title']:
            exam.title = data['title'].strip()
        if 'start_time' in data and data['start_time']:
            from datetime import datetime
            try:
                exam.start_time = datetime.fromisoformat(data['start_time'])
            except ValueError:
                pass
        # Retour #6 — reprogrammation par édition : end_time peut être fourni
        # explicitement (priorité, durée recalculée à partir de lui) ou dérivé
        # de duration_minutes comme avant — jamais désynchronisé de la
        # nouvelle date/durée, sinon l'examen reste accessible sur la
        # mauvaise fenêtre horaire.
        end_time_set = False
        if 'end_time' in data and data['end_time']:
            from datetime import datetime
            try:
                new_end = datetime.fromisoformat(data['end_time'])
                if new_end <= exam.start_time:
                    session.close()
                    return jsonify({'error': 'La date de fin doit être après la date de début'}), 400
                exam.end_time = new_end
                exam.duration_minutes = max(5, int((new_end - exam.start_time).total_seconds() / 60))
                end_time_set = True
            except ValueError:
                pass
        if not end_time_set:
            if 'duration_minutes' in data:
                exam.duration_minutes = max(5, int(data['duration_minutes']))
            exam.end_time = exam.start_time + timedelta(minutes=exam.duration_minutes)
        session.commit()
        result = exam.to_dict()
        session.close()
        return jsonify({'success': True, 'exam': result})
    except Exception as e:
        if session:
            session.rollback()
            session.close()
        print(f"❌ Erreur edit_online_exam: {e}")
        return jsonify({'error': str(e)}), 500


@exams_bp.route('/api/online_exams/<int:exam_id>', methods=['DELETE'])
@paseto_required
def delete_online_exam(exam_id):
    """Supprimer un examen en ligne (admin/professeur propriétaire uniquement)"""
    session = None
    try:
        user_id = get_current_user_id()
        session = get_session()
        
        user = session.query(User).filter_by(id=user_id).first()
        if user.role not in [UserRole.PROFESSOR, UserRole.ADMIN]:
            session.close()
            return jsonify({'error': 'Accès non autorisé'}), 403
        
        exam = session.query(OnlineExam).filter_by(id=exam_id).first()
        if not exam:
            session.close()
            return jsonify({'error': 'Examen non trouvé'}), 404
        
        # Vérifier propriété (prof) ou admin
        if user.role == UserRole.PROFESSOR and exam.created_by_id != user_id:
            session.close()
            return jsonify({'error': 'Vous ne pouvez supprimer que vos propres examens'}), 403

        # Suppression explicite dans l'ordre pour éviter les violations de clés
        # étrangères, notamment proctor_assignments -> exam_attempts/online_exams.
        attempt_ids = [a.id for a in session.query(ExamAttempt.id).filter_by(exam_id=exam_id).all()]
        session.query(ProctorAssignment).filter_by(exam_id=exam_id).delete(synchronize_session=False)

        if attempt_ids:
            session.query(CameraLog).filter(
                CameraLog.attempt_id.in_(attempt_ids)
            ).delete(synchronize_session=False)
            session.query(ExamActivityLog).filter(
                ExamActivityLog.attempt_id.in_(attempt_ids)
            ).delete(synchronize_session=False)
            session.query(Reclamation).filter(
                Reclamation.attempt_id.in_(attempt_ids)
            ).delete(synchronize_session=False)
            session.query(ExamAttempt).filter(
                ExamAttempt.id.in_(attempt_ids)
            ).delete(synchronize_session=False)

        session.query(ExamProctor).filter_by(exam_id=exam_id).delete(synchronize_session=False)
        session.delete(exam)
        session.commit()
        session.close()

        return jsonify({'success': True, 'message': 'Examen supprimé avec succès'})
    except Exception as e:
        if session:
            session.rollback()
            session.close()
        print(f"❌ Erreur delete_online_exam: {e}")
        import traceback; traceback.print_exc()
        return jsonify({'error': str(e)}), 500

@exams_bp.route('/api/online_exams/<int:exam_id>/details', methods=['GET'])
@paseto_required
def get_online_exam_details(exam_id):
    """Récupérer les détails complets d'un examen (avec contenu du sujet) - Pour composition étudiants"""
    try:
        user_id = get_current_user_id()
        session = get_session()
        
        user = session.query(User).filter_by(id=user_id).first()
        
        exam = session.query(OnlineExam).options(
            joinedload(OnlineExam.subject),
            joinedload(OnlineExam.creator)
        ).filter_by(id=exam_id).first()
        
        if not exam:
            session.close()
            return jsonify({'error': 'Examen non trouvé'}), 404
        
        # Vérifier les permissions
        if user.role == UserRole.STUDENT:
            # Étudiants : seulement les examens actifs dans la plage horaire
            now = utcnow()
            start_time = exam.start_time if exam.start_time.tzinfo else exam.start_time.replace(tzinfo=timezone.utc)
            end_time = exam.end_time if exam.end_time.tzinfo else exam.end_time.replace(tzinfo=timezone.utc)
            
            if exam.status != ExamStatus.ACTIVE or now < start_time or now > end_time:
                session.close()
                return jsonify({'error': 'Examen non disponible actuellement'}), 403
        
        elif user.role == UserRole.PROFESSOR:
            # Professeurs : seulement leurs propres examens
            if exam.created_by_id != user_id:
                session.close()
                return jsonify({'error': 'Accès non autorisé'}), 403
        
        # Préparer la réponse avec les détails complets
        exam_dict = exam.to_dict()
        
        # Ajouter le contenu du sujet (sans le barème pour les étudiants)
        if exam.subject:
            content = exam.subject.content or ''
            # Pour les étudiants, retirer la section barème si elle est incluse dans content
            if user.role == UserRole.STUDENT:
                content = _strip_bareme_from_content(content)
            subject_content = {
                'id': exam.subject.id,
                'title': exam.subject.title,
                'content': content,
            }
            # Barème (contient les réponses) réservé aux professeurs/admins
            if user.role in [UserRole.PROFESSOR, UserRole.ADMIN]:
                subject_content['rubric'] = exam.subject.rubric
            exam_dict['subject_content'] = subject_content
        
        session.close()
        return jsonify(exam_dict)
        
    except Exception as e:
        print(f"❌ Erreur get_online_exam_details: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

@exams_bp.route('/api/online_exams/<int:exam_id>/start', methods=['POST'])
@paseto_required
def start_exam_attempt(exam_id):
    """Démarrer une tentative d'examen (étudiant)"""
    try:
        user_id = get_current_user_id()
        session = get_session()
        
        user = session.query(User).filter_by(id=user_id).first()
        if user.role != UserRole.STUDENT:
            session.close()
            return jsonify({'error': 'Accès réservé aux étudiants'}), 403
        
        exam = session.query(OnlineExam).filter_by(id=exam_id).first()
        if not exam:
            session.close()
            return jsonify({'error': 'Examen non trouvé'}), 404
        
        now = utcnow()

        # S'assurer que les datetime sont timezone-aware pour la comparaison
        start_time = exam.start_time if exam.start_time.tzinfo else exam.start_time.replace(tzinfo=timezone.utc)
        end_time = exam.end_time if exam.end_time.tzinfo else exam.end_time.replace(tzinfo=timezone.utc)

        # Vérifier la plage horaire d'abord
        if now < start_time:
            start_str = start_time.strftime('%d/%m/%Y à %H:%M') + ' UTC'
            session.close()
            return jsonify({
                'error': f"L'examen n'a pas encore commencé. Il débutera le {start_str}.",
                'starts_at': start_time.isoformat()
            }), 400
        if now > end_time:
            session.close()
            return jsonify({'error': 'Cet examen est terminé'}), 400

        if exam.status == ExamStatus.SCHEDULED:
            session.close()
            return jsonify({'error': "Cet examen n'a pas encore été activé par votre professeur. Veuillez patienter."}), 400
        elif exam.status != ExamStatus.ACTIVE:
            session.close()
            return jsonify({'error': 'Examen non disponible actuellement'}), 400
        
        # Vérifier tentative existante
        existing = session.query(ExamAttempt).filter_by(
            exam_id=exam_id,
            student_id=user_id
        ).first()
        
        if existing:
            if existing.status == AttemptStatus.BANNED:
                session.close()
                return jsonify({'error': 'Vous êtes banni de cet examen', 'banned': True}), 403
            if existing.status in [AttemptStatus.SUBMITTED, AttemptStatus.AUTO_SUBMITTED]:
                session.close()
                return jsonify({'error': 'Vous avez déjà soumis cet examen'}), 400
            # Si IN_PROGRESS, continuer
            attempt_dict = existing.to_dict()
            session.close()
            return jsonify({'success': True, 'attempt': attempt_dict, 'continuing': True})
        
        # Signature pré-examen transmise par le frontend
        import json as _json
        body = request.get_json(silent=True) or {}
        pre_sig      = body.get('pre_exam_signature')
        pre_sig_meta = body.get('pre_exam_signature_meta')

        # Validation côté serveur de la qualité de la signature
        if pre_sig_meta:
            try:
                meta = pre_sig_meta if isinstance(pre_sig_meta, dict) else _json.loads(pre_sig_meta)
                strokes    = int(meta.get('strokes', 0))
                path_len   = float(meta.get('path_length', 0))
                duration   = int(meta.get('duration_ms', 0))
                if strokes < 2 or path_len < 80 or duration < 600:
                    session.close()
                    return jsonify({
                        'error': 'Signature non conforme. Veuillez tracer une signature complète (plusieurs traits, durée suffisante).',
                        'signature_invalid': True
                    }), 400
            except Exception:
                pass  # meta malformé → on laisse passer, le frontend a déjà validé

        meta_str = _json.dumps(pre_sig_meta) if isinstance(pre_sig_meta, dict) else pre_sig_meta

        # Créer nouvelle tentative
        attempt = ExamAttempt(
            exam_id=exam_id,
            student_id=user_id,
            status=AttemptStatus.IN_PROGRESS,
            answers='{}',
            pre_exam_signature_data=pre_sig,
            pre_exam_signature_meta=meta_str
        )
        session.add(attempt)
        session.flush()  # obtenir attempt.id avant commit

        # Lier la pré-affectation surveillant si elle existe
        pre = session.query(ProctorAssignment).filter_by(
            exam_id=exam_id, student_id=user_id, attempt_id=None
        ).first()
        if pre:
            pre.attempt_id = attempt.id

        session.commit()
        attempt_dict = attempt.to_dict()
        session.close()

        return jsonify({'success': True, 'attempt': attempt_dict}), 201
    except Exception as e:
        print(f"❌ Erreur start_exam_attempt: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

@exams_bp.route('/api/exam_attempts/<int:attempt_id>/save', methods=['POST'])
@paseto_required
def save_exam_answers(attempt_id):
    """Sauvegarder les réponses en temps réel"""
    try:
        user_id = get_current_user_id()
        session = get_session()
        
        attempt = session.query(ExamAttempt).filter_by(id=attempt_id, student_id=user_id).first()
        if not attempt:
            session.close()
            return jsonify({'error': 'Tentative non trouvée'}), 404
        
        if attempt.status != AttemptStatus.IN_PROGRESS:
            session.close()
            return jsonify({'error': 'Impossible de modifier une tentative terminée'}), 400
        
        data = request.get_json(silent=True) or {}
        attempt.answers = data.get('answers', '{}')
        
        session.commit()
        session.close()
        
        return jsonify({'success': True, 'message': 'Réponses sauvegardées'})
    except Exception as e:
        print(f"❌ Erreur save_exam_answers: {e}")
        return jsonify({'error': str(e)}), 500

@exams_bp.route('/api/exam_attempts/<int:attempt_id>/log_activity', methods=['POST'])
@paseto_required
def log_exam_activity(attempt_id):
    """Logger une activité suspecte avec gestion améliorée des violations"""
    try:
        user_id = get_current_user_id()
        session = get_session()
        
        attempt = session.query(ExamAttempt).filter_by(id=attempt_id, student_id=user_id).first()
        if not attempt:
            session.close()
            return jsonify({'error': 'Tentative non trouvée'}), 404
        
        data = request.get_json(silent=True) or {}
        event_type = data.get('event_type', 'unknown')
        event_data = data.get('event_data', '')

        # Logger l'activité
        log = ExamActivityLog(
            attempt_id=attempt_id,
            event_type=event_type,
            event_data=event_data
        )
        session.add(log)

        exam = attempt.exam
        severity_tab_events   = ['tab_switch', 'fullscreen_exit', 'window_blur']
        severity_medium_events = ['right_click', 'copy_attempt', 'paste_attempt', 'f12_attempt']

        ban_reason = None

        # ── 1. Outils développeur ─────────────────────────────────────────────
        # Incréments atomiques via UPDATE SQL pour éviter les race conditions multi-thread
        from sqlalchemy import update as _sa_update
        if event_type == 'devtools_attempt':
            ban_on_dev = exam.ban_on_devtools if exam.ban_on_devtools is not None else True
            session.execute(
                _sa_update(ExamAttempt).where(ExamAttempt.id == attempt_id).values(
                    tab_switches=ExamAttempt.tab_switches + 1,
                    warnings_count=ExamAttempt.warnings_count + 2
                )
            )
            session.refresh(attempt)
            if ban_on_dev:
                ban_reason = "Ouverture des outils développeur détectée"

        # ── 2. Changements de fenêtre / onglet / plein écran ──────────────────
        elif event_type in severity_tab_events:
            session.execute(
                _sa_update(ExamAttempt).where(ExamAttempt.id == attempt_id).values(
                    tab_switches=ExamAttempt.tab_switches + 1,
                    warnings_count=ExamAttempt.warnings_count + 2
                )
            )
            session.refresh(attempt)
            max_sw = exam.max_tab_switches if exam.max_tab_switches is not None else 2
            if max_sw >= 0 and attempt.tab_switches > max_sw:
                ban_reason = f"Trop de changements de contexte : {attempt.tab_switches} (seuil : {max_sw})"

        # ── 3. Photo de référence — stocker dans CameraLog si image fournie ────
        elif event_type == 'face_reference_captured':
            # event_data peut être une chaîne texte OU un JSON avec image_data base64
            import json as _json
            photo_b64 = None
            try:
                parsed = _json.loads(event_data) if isinstance(event_data, str) else event_data
                if isinstance(parsed, dict):
                    photo_b64 = parsed.get('image_data') or parsed.get('photo')
                    log.event_data = parsed.get('label', event_data)
            except Exception:
                pass
            if photo_b64:
                cam_log = CameraLog(
                    attempt_id=attempt_id,
                    event_type='face_reference_captured',
                    violation_type='face_reference',
                    image_data=photo_b64
                )
                session.add(cam_log)

        # ── 4. Visage non détecté (face_absent = alias FaceDetector.js) ──────
        elif event_type in ('no_face_detected', 'face_absent'):
            session.execute(
                _sa_update(ExamAttempt).where(ExamAttempt.id == attempt_id).values(
                    no_face_count=ExamAttempt.no_face_count + 1,
                    warnings_count=ExamAttempt.warnings_count + 1
                )
            )
            session.refresh(attempt)
            max_nf = exam.max_no_face_count if exam.max_no_face_count is not None else 10
            if max_nf >= 0 and (attempt.no_face_count or 0) >= max_nf:
                ban_reason = f"Visage absent trop souvent : {attempt.no_face_count} fois (seuil : {max_nf})"

        # ── 3b. Plusieurs visages détectés ────────────────────────────────────
        elif event_type == 'multiple_faces':
            session.execute(
                _sa_update(ExamAttempt).where(ExamAttempt.id == attempt_id).values(
                    warnings_count=ExamAttempt.warnings_count + 2,
                    tab_switches=ExamAttempt.tab_switches + 1
                )
            )
            session.refresh(attempt)

        # ── 4. Violations mineures ────────────────────────────────────────────
        elif event_type in severity_medium_events:
            session.execute(
                _sa_update(ExamAttempt).where(ExamAttempt.id == attempt_id).values(
                    warnings_count=ExamAttempt.warnings_count + 1
                )
            )
            session.refresh(attempt)

        # ── Appliquer le bannissement si nécessaire ───────────────────────────
        if ban_reason:
            attempt.status = AttemptStatus.BANNED
            attempt.banned_at = utcnow()
            attempt.ban_reason = ban_reason
            session.commit()
            session.close()
            return jsonify({
                'success': True,
                'banned': True,
                'ban_reason': ban_reason,
                'severity': 'high',
                'message': f"Vous avez été exclu de cet examen : {ban_reason}"
            })

        session.commit()

        response_data = {
            'success': True,
            'warnings_count': attempt.warnings_count,
            'tab_switches': attempt.tab_switches,
            'no_face_count': attempt.no_face_count or 0,
            'max_tab_switches': exam.max_tab_switches,
            'max_no_face_count': exam.max_no_face_count if exam.max_no_face_count is not None else 10,
            'severity': 'high' if event_type in (severity_tab_events + ['devtools_attempt', 'no_face_detected', 'face_absent', 'multiple_faces']) else 'medium',
            'banned': False
        }

        session.close()
        return jsonify(response_data)
        
    except Exception as e:
        print(f"❌ Erreur log_exam_activity: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

@exams_bp.route('/api/exam_attempts/<int:attempt_id>/result', methods=['GET'])
@paseto_required
def get_exam_attempt_result(attempt_id):
    """Résultat d'une tentative pour l'étudiant concerné"""
    try:
        user_id = get_current_user_id()
        session = get_session()
        attempt = session.query(ExamAttempt).filter_by(id=attempt_id, student_id=user_id).first()
        if not attempt:
            session.close()
            return jsonify({'error': 'Tentative introuvable'}), 404
        exam = session.query(OnlineExam).filter_by(id=attempt.exam_id).first()
        # Retour #29 — ne pas afficher la note à l'étudiant avant publication
        # par le professeur/admin (délibération), même une fois corrigée.
        published = bool(exam.results_published) if exam else True
        result = {
            'attempt_id':   attempt.id,
            'exam_title':   exam.title if exam else '',
            'score':        attempt.score if published else None,
            'feedback':     attempt.feedback if published else None,
            'corrected_at': attempt.corrected_at.isoformat() if (attempt.corrected_at and published) else None,
            'submitted_at': attempt.submitted_at.isoformat() if attempt.submitted_at else None,
            'status':       attempt.status.value,
            'results_published': published,
            'pending_publication': attempt.score is not None and not published,
        }
        session.close()
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@exams_bp.route('/api/exam_attempts/<int:attempt_id>/subject', methods=['GET'])
@paseto_required
def get_exam_attempt_subject(attempt_id):
    """Récupérer le contenu du sujet pour une tentative en cours"""
    try:
        user_id = get_current_user_id()
        session = get_session()
        
        attempt = session.query(ExamAttempt).options(
            joinedload(ExamAttempt.exam).joinedload(OnlineExam.subject)
        ).filter_by(id=attempt_id, student_id=user_id).first()
        
        if not attempt:
            session.close()
            return jsonify({'error': 'Tentative non trouvée'}), 404
        
        if attempt.status != AttemptStatus.IN_PROGRESS:
            session.close()
            return jsonify({'error': 'Cette tentative n\'est plus active'}), 400
        
        subject = attempt.exam.subject
        if not subject:
            session.close()
            return jsonify({'error': 'Sujet non trouvé'}), 404
        
        # Extraire la réponse actuelle si elle existe
        current_answer = ''
        if attempt.answers:
            try:
                saved = json.loads(attempt.answers)
                current_answer = saved.get('reponse', '')
            except Exception:
                current_answer = attempt.answers

        subject_data = {
            'id': subject.id,
            'title': subject.title,
            # Retirer la section barème du contenu (elle contient les réponses)
            'content': _strip_bareme_from_content(subject.content or ''),
            # Barème NON transmis aux étudiants — contient les réponses
            # Infos exam/tentative pour la page proctorée
            'exam_title': attempt.exam.title,
            'duration_minutes': attempt.exam.duration_minutes,
            'extra_minutes': attempt.extra_minutes or 0,
            'started_at': attempt.started_at.isoformat() if attempt.started_at else None,
            'current_answer': current_answer,
        }

        session.close()
        return jsonify(subject_data)

    except Exception as e:
        print(f"❌ Erreur get_exam_attempt_subject: {e}")
        return jsonify({'error': str(e)}), 500


@exams_bp.route('/api/exam_attempts/<int:attempt_id>/paginated', methods=['GET'])
@paseto_required
def get_exam_attempt_paginated(attempt_id):
    """
    Pagination des questions façon Moodle (Notes points 22/23) : le découpage
    en pages et l'ordre (mélange) des questions sont calculés UNE FOIS côté
    serveur — comme Moodle stocke `quiz_slots.page` en base plutôt que de
    laisser le client recalculer à chaque rendu — au lieu d'être recalculés
    côté client à chaque montage du composant (ce qui remélangeait différemment
    les questions/choix à chaque rechargement de page, un vrai bug corrigé ici).

    Le mélange est déterministe (seed = attempt_id) : stable pour un même
    étudiant à travers les rechargements, comme une page Moodle assignée une
    fois pour toutes à une tentative.
    """
    try:
        user_id = get_current_user_id()
        session = get_session()

        attempt = session.query(ExamAttempt).options(
            joinedload(ExamAttempt.exam).joinedload(OnlineExam.subject)
        ).filter_by(id=attempt_id, student_id=user_id).first()

        if not attempt:
            session.close()
            return jsonify({'error': 'Tentative non trouvée'}), 404

        exam = attempt.exam
        subject = exam.subject if exam else None
        content = _strip_bareme_from_content(subject.content or '') if subject else ''

        blocks = _parse_subject_blocks_ordered(content) if content else []

        p1_types = ('qcm', 'qcm_multi', 'vf', 'appariement')
        p2_types = ('section', 'open', 'subopen', 'code')
        p1_blocks = [b for b in blocks if b['type'] in p1_types]
        p2_items  = [b for b in blocks if b['type'] in p2_types]

        if exam and exam.randomize_questions:
            p1_blocks = _seeded_shuffle(p1_blocks, attempt_id)
            for b in p1_blocks:
                if b['type'] in ('qcm', 'qcm_multi') and b.get('choices'):
                    b['choices'] = _seeded_shuffle(b['choices'], f'{attempt_id}:{b["num"]}')
            # Partie 2 (questions ouvertes) : jamais mélangée, comme côté client

        per_page = exam.questions_per_page if exam and exam.questions_per_page and exam.questions_per_page > 0 else 0
        p1_pages = _paginate_moodle_style(p1_blocks, per_page)
        p2_pages = _paginate_moodle_style(p2_items, per_page)

        session.close()
        return jsonify({
            'questions_per_page': per_page,
            'p1_blocks': p1_blocks,
            'p2_items': p2_items,
            'p1_pages': p1_pages,
            'p2_pages': p2_pages,
        })
    except Exception as e:
        print(f"❌ Erreur get_exam_attempt_paginated: {e}")
        try: session.close()
        except: pass
        return jsonify({'error': str(e)}), 500


def _parse_subject_questions_for_grading(content: str) -> dict:
    """Extrait num→{type, text, choices:{letter:text}, pairs:[{left,right}]} du
    contenu brut du sujet — port Python minimal de parseExamBlocks (frontend) pour
    reconstruire des réponses lisibles par l'IA, notamment pour l'appariement où
    l'énoncé de la paire de gauche n'est jamais stocké dans les réponses brutes."""
    Q_RE = re.compile(r'^(?:(?:Question|Q)\.?\s+)?(\d{1,3})\s*[—\-–:.)]\s*(.+)', re.I)
    TYPE_MARKER = re.compile(r'\[(QCM_MULTI|QCM|VF|OUVERT|SUBOPEN|APPARIEMENT|CODE)\]', re.I)
    C_RE = re.compile(r'^(?:\(?([A-Fa-f])\)?)\s*[.):\s-]\s+(.+)')
    PAIR_RE = re.compile(r'^(?:\(?([A-Fa-f])\)?)\s*[.):\s-]\s+(.+?)\s*(?:→|->)\s*(.+)')

    questions = {}
    lines = content.split('\n')
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        m = Q_RE.match(line)
        if not m:
            i += 1
            continue
        num = m.group(1)
        rest = m.group(2)
        marker_m = TYPE_MARKER.search(rest)
        marker = marker_m.group(1).upper() if marker_m else None
        text = TYPE_MARKER.sub('', rest).strip()
        i += 1
        choices, pairs = {}, []
        while i < len(lines):
            l = lines[i].strip()
            if not l:
                i += 1
                continue
            if Q_RE.match(l):
                break
            if marker == 'APPARIEMENT':
                pm = PAIR_RE.match(l)
                if pm:
                    pairs.append({'left': pm.group(2).strip(), 'right': pm.group(3).strip()})
                    i += 1
                    continue
            cm = C_RE.match(l)
            if cm:
                choices[cm.group(1).upper()] = cm.group(2).strip()
                i += 1
                continue
            i += 1
        questions[num] = {'marker': marker, 'text': text, 'choices': choices, 'pairs': pairs}
    return questions


# ============================================================================
# PAGINATION FAÇON MOODLE (Notes points 22/23 — référence : mod_quiz de Moodle
# 4.4.2, quiz_slots.page + quiz_repaginate_questions() + mod_quiz_get_attempt_data)
# ============================================================================

def _parse_subject_blocks_ordered(raw: str) -> list:
    """Port Python fidèle du parser JS `parseExamBlocks` (app/exam/[id]/page.tsx) —
    reconstruit la séquence ORDONNÉE de blocs (sections + questions typées) du
    contenu brut d'un sujet, pour permettre une pagination et un mélange calculés
    côté serveur (source de vérité unique, comme Moodle stocke `quiz_slots.page`
    plutôt que de laisser le client recalculer)."""
    VF_RE = re.compile(r'\bvrai\s*[/|ou]\s*faux\b|\bV\s*[/|]\s*F\b', re.I)
    strip = lambda s: re.sub(r'\s*[*_]{1,2}$', '', re.sub(r'^[*_]{1,2}\s*', '', s.strip())).strip()
    Q_RE = re.compile(r'^(?:(?:Question|Q)\.?\s+)?(\d{1,2})(?!\s*\.\s*\d)(?:\s*[.:)–—-]|\.\s+|\s{2,})\s*(.+)', re.I)
    TYPE_MARKER = re.compile(r'\[(QCM_MULTI|QCM|VF|OUVERT|SUBOPEN|APPARIEMENT|CODE|OUVERT[ES]*)\]', re.I)
    C_RE = re.compile(r'^(?:\(?([A-Fa-f])\)?)\s*[.):\s-]\s+(.+)')
    PAIR_RE = re.compile(r'^(?:\(?([A-Fa-f])\)?)\s*[.):\s-]\s+(.+?)\s*(?:→|->)\s*(.+)')
    SEP_RE = re.compile(r'^[-=*─═▬]{3,}$')
    SECT_RE = re.compile(r'^(?:Partie|Section|Exercice|Part)\s+(?:[IVX]+|\d+)', re.I)
    INSTR_RE = re.compile(
        r'^(?:Défini[rz]|Expliqu[eé][rz]?|Décri[vz]|Analys[eé][rz]?|Calcul[eé][rz]?|Rédig[eé][rz]?|'
        r'Démontr[eé][rz]?|Comment[eé][rz]?|Identifi[eé][rz]?|Justifi[eé][rz]?|Compar[eé][rz]?|'
        r'Présent[eé][rz]?|Discut[eé][rz]?|Montr[eé][rz]?|Propos[eé][rz]?|Cit[eé][rz]?|Donner?)', re.I)
    PTS_RE = re.compile(r'\(\s*\d+\s*pts?\s*\)', re.I)
    MEDIA_RE = re.compile(r'^\[(IMAGE|AUDIO|VIDEO):(.+)\]$', re.I)

    def is_q(l): return bool(Q_RE.match(strip(l)))
    def is_c(l): return bool(C_RE.match(strip(l))) and len(strip(l)) > 3
    def is_sep(l): return not l.strip() or bool(SEP_RE.match(l.strip()))
    def is_sect(l): return bool(SECT_RE.match(strip(l))) and not is_q(l)

    def get_q(l):
        s = strip(l)
        m = Q_RE.match(s)
        if not m:
            return None
        marker_m = TYPE_MARKER.search(s)
        text = TYPE_MARKER.sub('', strip(m.group(2))).strip()
        return {'num': m.group(1), 'text': text, 'markerType': marker_m.group(1).upper() if marker_m else None}

    def get_c(l):
        m = C_RE.match(strip(l))
        return {'letter': m.group(1).upper(), 'text': strip(m.group(2))} if m else None

    def get_pair(l):
        m = PAIR_RE.match(strip(l))
        return {'left': strip(m.group(2)), 'right': strip(m.group(3))} if m else None

    lines = raw.split('\n')
    blocks = []
    i = 0
    n = len(lines)
    while i < n and not is_q(lines[i]):
        i += 1
    # préambule ignoré ici (déjà affiché intégralement via le panneau "sujet complet")

    while i < n:
        if is_sect(lines[i]):
            blocks.append({'type': 'section', 'title': strip(lines[i])})
            i += 1
            continue
        if is_sep(lines[i]) and not is_q(lines[i]):
            i += 1
            continue
        if not is_q(lines[i]):
            i += 1
            continue
        q = get_q(lines[i])
        if not q:
            i += 1
            continue
        is_pair_mode = q['markerType'] == 'APPARIEMENT'
        i += 1
        extra_lines, choices, pairs = [], [], []
        while i < n:
            if is_sep(lines[i]):
                i += 1
                if len(choices) >= 2 or len(pairs) >= 2:
                    break
                continue
            if is_sect(lines[i]) and not is_q(lines[i]):
                break
            if is_q(lines[i]) and not is_c(lines[i]):
                break
            if is_pair_mode:
                p = get_pair(lines[i])
                if p:
                    pairs.append(p)
                    i += 1
                elif not pairs:
                    extra_lines.append(lines[i])
                    i += 1
                else:
                    break
                continue
            c = get_c(lines[i])
            if c:
                choices.append(c)
                i += 1
            elif not choices:
                extra_lines.append(lines[i])
                i += 1
            else:
                break

        if q['markerType']:
            btype = {
                'QCM': 'qcm', 'QCM_MULTI': 'qcm_multi', 'VF': 'vf', 'SUBOPEN': 'subopen',
                'APPARIEMENT': 'appariement', 'CODE': 'code',
            }.get(q['markerType'], 'open')
        else:
            has_pts_choices = any(PTS_RE.search(c['text']) for c in choices)
            has_instr_verbs = any(INSTR_RE.match(c['text']) for c in choices)
            if (has_pts_choices or has_instr_verbs) and len(choices) >= 1:
                btype = 'subopen'
            elif len(choices) >= 2:
                btype = 'qcm'
            elif VF_RE.search(q['text']) or VF_RE.search(' '.join(extra_lines)):
                btype = 'vf'
            else:
                btype = 'open'

        media = []
        clean_extra = []
        for l in extra_lines:
            m = MEDIA_RE.match(strip(l))
            if m:
                media.append({'type': m.group(1).lower(), 'filename': m.group(2).strip()})
            else:
                clean_extra.append(l)

        blocks.append({
            'type': btype, 'num': q['num'], 'text': q['text'], 'extraLines': clean_extra,
            'choices': choices, 'pairs': pairs or None, 'media': media or None,
        })
    return blocks


def _paginate_moodle_style(blocks: list, per_page: int) -> list:
    """Port Python de `paginateBlocks` (frontend) — groupe N questions par page,
    MAIS force en plus un saut de page à chaque en-tête de section rencontré,
    exactement comme `quiz_repaginate_questions()` de Moodle force un saut à
    chaque `firstslot` de section même si le quota par page n'est pas atteint
    (mod/quiz/locallib.php:515-544 sur le serveur Moodle de référence)."""
    if not blocks:
        return []
    if not per_page or per_page <= 0:
        return [blocks]
    pages, current, q_count = [], [], 0
    for b in blocks:
        is_question = b['type'] not in ('section', 'text')
        if b['type'] == 'section' and current:
            pages.append(current)
            current, q_count = [], 0
        elif is_question and q_count == per_page:
            pages.append(current)
            current, q_count = [], 0
        current.append(b)
        if is_question:
            q_count += 1
    if current:
        pages.append(current)
    return pages


def _seeded_shuffle(items: list, seed) -> list:
    """Mélange déterministe — stable pour un même attempt_id à travers les
    rechargements de page (contrairement au Math.random() côté client qui
    remélangeait différemment à chaque montage du composant)."""
    import random as _random
    rng = _random.Random(seed)
    shuffled = list(items)
    for idx in range(len(shuffled) - 1, 0, -1):
        j = rng.randint(0, idx)
        shuffled[idx], shuffled[j] = shuffled[j], shuffled[idx]
    return shuffled


def _build_readable_student_answers(subject_content: str, answers_data, exclude_nums=None) -> str:
    """Reconstruit un texte lisible 'Question N : réponse' à partir des réponses
    brutes de l'examen en ligne (clés plates pq_N / pq_N_lettre / pq_N_index),
    en résolvant les lettres/indices vers le texte réel des choix/paires. Corrige
    le bug où la correction IA recevait un format {qcm:..,texte:..} obsolète, qui
    ne correspond plus à ce que le frontend envoie réellement depuis longtemps."""
    if not isinstance(answers_data, dict):
        return str(answers_data) if answers_data else ''

    questions = _parse_subject_questions_for_grading(subject_content or '')
    lines = []

    # Regrouper les clés pq_N* par numéro de question
    nums = sorted({k.split('_')[1] for k in answers_data if k.startswith('pq_') and len(k.split('_')) >= 2},
                  key=lambda x: int(x) if x.isdigit() else 0)

    for num in nums:
        if exclude_nums and num in exclude_nums:
            continue
        q = questions.get(num, {})
        marker = q.get('marker')
        qtext = q.get('text', '')
        direct_key = f'pq_{num}'

        if marker == 'APPARIEMENT' and q.get('pairs'):
            parts = []
            for idx, pair in enumerate(q['pairs']):
                ans = answers_data.get(f'{direct_key}_{idx}', '').strip()
                if ans:
                    parts.append(f"  • {pair['left']} → {ans}")
            if parts:
                lines.append(f"Question {num} ({qtext}) — Appariements de l'étudiant :\n" + '\n'.join(parts))
        elif marker == 'SUBOPEN' and q.get('choices'):
            parts = []
            for letter, ctext in q['choices'].items():
                ans = answers_data.get(f'{direct_key}_{letter}', '').strip()
                if ans:
                    parts.append(f"  • {ctext} : {ans}")
            if parts:
                lines.append(f"Question {num} ({qtext}) :\n" + '\n'.join(parts))
        elif marker == 'QCM_MULTI':
            raw = answers_data.get(direct_key, '').strip()
            if raw:
                letters = [l.strip() for l in raw.split(',') if l.strip()]
                resolved = [f"{l}) {q.get('choices', {}).get(l, '')}" for l in letters]
                lines.append(f"Question {num} ({qtext}) — Réponses cochées : {', '.join(resolved)}")
        elif marker == 'QCM':
            raw = answers_data.get(direct_key, '').strip()
            if raw:
                ctext = q.get('choices', {}).get(raw, '')
                lines.append(f"Question {num} ({qtext}) — Réponse : {raw}) {ctext}" if ctext else f"Question {num} ({qtext}) — Réponse : {raw}")
        else:
            raw = answers_data.get(direct_key, '').strip()
            if raw:
                lines.append(f"Question {num} ({qtext}) — Réponse : {raw}")

    if lines:
        return '\n\n'.join(lines)

    # Repli : formats hérités ({qcm:.., texte:..}) ou blob déjà textuel
    qcm_a  = answers_data.get('qcm', {}) if isinstance(answers_data.get('qcm'), dict) else {}
    text_a = answers_data.get('texte', answers_data.get('text', {}))
    text_a = text_a if isinstance(text_a, dict) else {}
    if qcm_a or text_a:
        legacy_lines = []
        all_keys = sorted(set(list(qcm_a.keys()) + list(text_a.keys())),
                           key=lambda x: int(x) if str(x).isdigit() else 0)
        for k in all_keys:
            if k in qcm_a:  legacy_lines.append(f"Question {k} : {qcm_a[k]}")
            if k in text_a: legacy_lines.append(f"Question {k} : {text_a[k]}")
        return '\n'.join(legacy_lines)

    return (answers_data.get('content') or answers_data.get('reponse') or
            answers_data.get('answer') or answers_data.get('text') or '')


_RUBRIC_Q_BLOCK_RE = re.compile(
    r'Question\s+(\d{1,3})\s*[—\-–:.].*?(?=\nQuestion\s+\d{1,3}\s*[—\-–:.]|\n─+\nTOTAL|\Z)', re.S)
_RUBRIC_QCM_ANSWER_RE = re.compile(r'[Bb]onnes?\s+r[ée]ponses?\s*:\s*([A-Fa-f]\)?(?:\s*,\s*[A-Fa-f]\)?)*)')
_RUBRIC_VF_ANSWER_RE  = re.compile(r'R[ée]ponse\s*:\s*(Vrai|Faux)', re.I)
_Q_POINTS_RE          = re.compile(r'Question\s+(\d{1,3})\s*[—\-–:.].*?\((\d+(?:\.\d+)?)\s*pts?\)')


def _extract_correct_answers(rubric: str) -> dict:
    """Extrait, pour chaque question, la bonne réponse encodée dans le barème
    par `_build_rubric_prompt`/le prompt de génération principal : lettre(s)
    pour QCM (une ou plusieurs), Vrai/Faux pour VF. Sert de base à la notation
    automatique — équivalent du champ `rightanswer` stocké par Moodle pour
    qtype_multichoice/truefalse, plutôt que de renvoyer à l'IA une réponse
    pourtant déjà mécaniquement vérifiable."""
    result = {}
    for m in _RUBRIC_Q_BLOCK_RE.finditer(rubric or ''):
        num, block = m.group(1), m.group(0)
        am = _RUBRIC_QCM_ANSWER_RE.search(block)
        if am:
            letters = {l.upper() for l in re.findall(r'[A-Fa-f]', am.group(1))}
            if letters:
                result[num] = {'type': 'qcm', 'letters': letters}
                continue
        vm = _RUBRIC_VF_ANSWER_RE.search(block)
        if vm:
            result[num] = {'type': 'vf', 'value': vm.group(1).capitalize()}
    return result


def _question_points_map(content: str) -> dict:
    return {m.group(1): float(m.group(2)) for m in _Q_POINTS_RE.finditer(content or '')}


def _deterministic_grade(content: str, rubric: str, answers_data) -> tuple:
    """Note automatiquement — sans IA, comparaison exacte ou crédit partiel —
    les questions QCM / QCM à réponses multiples / Vrai-Faux / Appariement,
    exactement comme Moodle le fait pour qtype_multichoice (grade_response
    somme les fractions des choix cochés), qtype_truefalse (correspondance
    exacte, 0 ou 1) et qtype_match (fraction = paires correctes / total).
    Retourne (score, score_max, lignes_de_détail, {numéros déjà notés})."""
    if not isinstance(answers_data, dict):
        return 0.0, 0.0, [], set()

    questions   = _parse_subject_questions_for_grading(content)
    points_map  = _question_points_map(content)
    correct_map = _extract_correct_answers(rubric)

    score, max_score = 0.0, 0.0
    breakdown = []
    graded_nums = set()

    for num, q in questions.items():
        marker = q.get('marker')
        pts = points_map.get(num)
        if pts is None:
            continue

        if marker == 'QCM':
            key = correct_map.get(num)
            if not key or key['type'] != 'qcm' or len(key['letters']) != 1:
                continue
            correct_letter = next(iter(key['letters']))
            given = (answers_data.get(f'pq_{num}') or '').strip().upper()
            earned = pts if given == correct_letter else 0.0
            score += earned; max_score += pts; graded_nums.add(num)
            breakdown.append(
                f"Question {num} (QCM) : {'✓' if earned else '✗'} réponse {given or '(aucune)'} "
                f"— attendu {correct_letter}) — {earned:.2f}/{pts:.2f} pt(s)")

        elif marker == 'QCM_MULTI':
            key = correct_map.get(num)
            if not key or key['type'] != 'qcm' or not key['letters']:
                continue
            correct_set = key['letters']
            raw = (answers_data.get(f'pq_{num}') or '').strip()
            given_set = {l.strip().upper() for l in raw.split(',') if l.strip()}
            n_right = len(given_set & correct_set)
            n_wrong = len(given_set - correct_set)
            fraction = max(0.0, (n_right - n_wrong) / len(correct_set))
            earned = round(pts * fraction, 2)
            score += earned; max_score += pts; graded_nums.add(num)
            breakdown.append(
                f"Question {num} (QCM à réponses multiples) : coché {sorted(given_set) or '(aucune)'} "
                f"— attendu {sorted(correct_set)} — {earned:.2f}/{pts:.2f} pt(s)")

        elif marker == 'VF':
            key = correct_map.get(num)
            if not key or key['type'] != 'vf':
                continue
            given = (answers_data.get(f'pq_{num}') or '').strip().capitalize()
            earned = pts if given == key['value'] else 0.0
            score += earned; max_score += pts; graded_nums.add(num)
            breakdown.append(
                f"Question {num} (Vrai/Faux) : {'✓' if earned else '✗'} réponse {given or '(aucune)'} "
                f"— attendu {key['value']} — {earned:.2f}/{pts:.2f} pt(s)")

        elif marker == 'APPARIEMENT' and q.get('pairs'):
            pairs = q['pairs']
            if not pairs:
                continue
            n_right = 0
            for idx, pair in enumerate(pairs):
                given = (answers_data.get(f'pq_{num}_{idx}') or '').strip().lower()
                if given and given == pair['right'].strip().lower():
                    n_right += 1
            fraction = n_right / len(pairs)
            earned = round(pts * fraction, 2)
            score += earned; max_score += pts; graded_nums.add(num)
            breakdown.append(
                f"Question {num} (Appariement) : {n_right}/{len(pairs)} paire(s) correcte(s) "
                f"— {earned:.2f}/{pts:.2f} pt(s)")

    total_max = round(sum(points_map.values()), 2)
    return round(score, 2), round(max_score, 2), total_max, breakdown, graded_nums


def _extract_points_obtenus(text: str, denom: float) -> float:
    """Extrait 'Points obtenus : XX.XX' — marqueur dédié à la portion de
    correction confiée à l'IA une fois les questions QCM/VF/Appariement
    retirées, distinct de 'Note totale: XX.XX/20' pour ne jamais être
    confondu avec une notation IA de l'examen entier."""
    m = re.search(r'Points\s+obtenus\s*:\s*(\d+\.?\d*)', text or '', re.I)
    if not m:
        return 0.0
    return max(0.0, min(denom, float(m.group(1))))


def _run_auto_correction(attempt_id: int):
    """Correction IA automatique dans un thread séparé (session DB indépendante)."""
    session = get_session()
    try:
        attempt = session.query(ExamAttempt).options(
            joinedload(ExamAttempt.exam).joinedload(OnlineExam.subject),
            joinedload(ExamAttempt.student)
        ).filter_by(id=attempt_id).first()

        if not attempt:
            print(f"⚠️  Auto-correction : tentative {attempt_id} introuvable")
            return

        exam    = attempt.exam
        subject = exam.subject

        if not subject or not subject.content:
            print(f"⚠️  Auto-correction {attempt_id} : sujet sans contenu, correction ignorée")
            return

        # Extraire les réponses (clés plates pq_N/pq_N_x réellement envoyées par le
        # frontend — reconstruites avec le texte des questions/choix/paires pour que
        # l'IA voie le contexte complet, notamment pour l'appariement)
        try:
            answers_data = json.loads(attempt.answers) if attempt.answers else {}
        except Exception:
            answers_data = {}

        if not answers_data:
            print(f"⚠️  Auto-correction {attempt_id} : aucune réponse, correction ignorée")
            return

        # Notation automatique (sans IA) des questions QCM/QCM_MULTI/Vrai-Faux/
        # Appariement — comme Moodle. Seul le reliquat de points (questions
        # ouvertes/code) est confié à l'IA, sur son propre total réduit. Le
        # score final est ramené proportionnellement sur 20 à partir du total
        # RÉEL des points du sujet (souvent ≠ 20 malgré la consigne — l'IA de
        # génération ne respecte pas toujours parfaitement la répartition
        # demandée), plutôt que de supposer un total fixe.
        det_score, det_max, total_max, det_breakdown, det_nums = _deterministic_grade(
            subject.content, subject.rubric or '', answers_data)
        remaining_max = round(total_max - det_max, 2)
        det_section = (
            "=== NOTATION AUTOMATIQUE (QCM / Vrai-Faux / Appariement — sans IA) ===\n"
            + ('\n'.join(det_breakdown) if det_breakdown else 'Aucune question de ce type notée.') + "\n\n"
        ) if det_breakdown else ""

        def _to_20(raw_points: float) -> float:
            if total_max <= 0.01:
                return 0.0
            return max(0.0, min(20.0, raw_points / total_max * 20))

        student_answers = ''
        if remaining_max > 0.01:
            student_answers = _build_readable_student_answers(subject.content, answers_data, exclude_nums=det_nums)
            if not student_answers:
                student_answers = attempt.answers or ''

        if remaining_max <= 0.01 or not student_answers.strip():
            score    = _to_20(det_score)
            result   = f"{det_section}Note totale: {score:.2f}/20"
        else:
            system_prompt = _build_correction_system_prompt(
                exam.title + (" — " + subject.title if subject.title else ""),
                subject.content
            )
            excluded_note = (
                f"Questions déjà notées automatiquement en dehors de cette correction, ne les évalue pas : "
                f"{', '.join(sorted(det_nums, key=int))}.\n" if det_nums else ""
            )
            user_message = f"""SUJET D'EXAMEN:
{subject.content}

BARÈME DE NOTATION:
{subject.rubric or 'Barème standard sur 20 points'}

COPIE À CORRIGER (Examen en ligne — correction automatique) :
Étudiant: {attempt.student.full_name}
Durée de l'examen: {exam.duration_minutes} minutes

RÉPONSES DE L'ÉTUDIANT (questions restantes uniquement) :
{student_answers}

{excluded_note}Tu DOIS noter UNIQUEMENT les questions listées ci-dessus, sur un total de {remaining_max:.2f} points (PAS 20).
Tu DOIS terminer ta correction par une ligne contenant EXACTEMENT : "Points obtenus : XX.XX" (jamais "Note totale", jamais "/20")."""

            print(f"🤖 Auto-correction tentative {attempt_id} ({attempt.student.full_name}) — en cours…")
            ai_result  = call_claude(system_prompt, user_message, temperature=0.15)
            ai_partial = _extract_points_obtenus(ai_result, remaining_max)
            score      = _to_20(det_score + ai_partial)
            result     = f"{det_section}{ai_result}\n\nNote totale: {score:.2f}/20"

        attempt.score          = score
        attempt.feedback       = result
        attempt.corrected_at   = utcnow()
        attempt.corrected_by_id = None  # None = correction automatique
        session.commit()
        print(f"✅ Auto-correction {attempt_id} terminée : {score}/20")

        # Notification temps réel étudiant : Redis + ntfy
        try:
            from notif_bus import notify_user
            notify_user(
                attempt.student_id,
                'correction_done',
                'Copie corrigée',
                f'Note : {score:.2f}/20 — {exam.title}',
                priority='high',
                tags=['white_check_mark'],
            )
        except Exception as _nb_err:
            print(f"⚠️  notif_bus auto-correction : {_nb_err}")

        # Email à l'étudiant
        try:
            if attempt.student.email and '@temp.edu' not in attempt.student.email:
                send_paper_corrected_email(
                    student_email=attempt.student.email,
                    student_name=attempt.student.full_name,
                    subject_title=f"{exam.title} (Examen en ligne)",
                    score=score,
                    paper_id=attempt.id
                )
        except Exception as email_err:
            print(f"⚠️  Email auto-correction : {email_err}")

    except Exception as e:
        print(f"❌ Erreur auto-correction tentative {attempt_id} : {e}")
        try:
            session.rollback()
        except Exception:
            pass
    finally:
        session.close()


@exams_bp.route('/api/exam_attempts/<int:attempt_id>/submit', methods=['POST'])
@paseto_required
def submit_exam_attempt(attempt_id):
    """Soumettre l'examen (étudiant)"""
    try:
        user_id = get_current_user_id()
        session = get_session()
        
        attempt = session.query(ExamAttempt).filter_by(id=attempt_id, student_id=user_id).first()
        if not attempt:
            session.close()
            return jsonify({'error': 'Tentative non trouvée'}), 404
        
        if attempt.status != AttemptStatus.IN_PROGRESS:
            session.close()
            return jsonify({'error': 'Tentative déjà soumise ou bannie'}), 400
        
        # Sauvegarder les dernières réponses
        data = request.get_json(silent=True) or {}
        if 'answers' in data:
            attempt.answers = data['answers']
        if 'signature_data' in data and data['signature_data']:
            attempt.signature_data = data['signature_data']

        attempt.status = AttemptStatus.SUBMITTED
        attempt.submitted_at = utcnow()

        # Charger le flag auto_correct avant de fermer la session
        exam = session.query(OnlineExam).filter_by(id=attempt.exam_id).first()
        auto_correct = exam.auto_correct if exam else False
        attempt_id_for_thread = attempt.id

        session.commit()
        session.close()

        # Lancer la correction IA en arrière-plan si activée par le prof
        if auto_correct:
            t = threading.Thread(
                target=_run_auto_correction,
                args=(attempt_id_for_thread,),
                daemon=True
            )
            t.start()
            return jsonify({'success': True, 'message': 'Examen soumis — correction automatique en cours', 'auto_correct': True})

        return jsonify({'success': True, 'message': 'Examen soumis avec succès', 'auto_correct': False})
    except Exception as e:
        print(f"Erreur submit_exam_attempt: {e}")
        try:
            session.close()
        except Exception:
            pass
        return jsonify({'error': str(e)}), 500


# ============================================================================
# #7 — UPLOAD D'IMAGE POUR LES SUJETS D'EXAMEN
# ============================================================================

@exams_bp.route('/api/exam_attempts/<int:attempt_id>/unban', methods=['POST'])
@paseto_required
def unban_exam_attempt(attempt_id):
    """
    Lever le bannissement d'un étudiant sur une tentative d'examen.
    - Admin : peut unban n'importe quelle tentative
    - Professeur : uniquement si l'examen lui appartient
    """
    try:
        user_id = get_current_user_id()
        session = get_session()
        user = session.query(User).filter_by(id=user_id).first()

        if user.role not in [UserRole.ADMIN, UserRole.PROFESSOR]:
            session.close()
            return jsonify({'error': 'Accès non autorisé'}), 403

        attempt = session.query(ExamAttempt).filter_by(id=attempt_id).first()
        if not attempt:
            session.close()
            return jsonify({'error': 'Tentative introuvable'}), 404

        if user.role == UserRole.PROFESSOR and attempt.exam.created_by_id != user_id:
            session.close()
            return jsonify({'error': 'Vous ne pouvez lever que les bannissements de vos propres examens'}), 403

        if attempt.status != AttemptStatus.BANNED:
            session.close()
            return jsonify({'error': "Cet étudiant n'est pas banni sur cette tentative"}), 400

        data = request.get_json() or {}
        reason = data.get('reason', '').strip()

        attempt.status = AttemptStatus.IN_PROGRESS
        # Log the unban action
        log = ExamActivityLog(
            attempt_id=attempt.id,
            event_type='unban',
            event_data=json.dumps({'author': user.full_name, 'reason': reason or ''}),
        )
        session.add(log)
        session.commit()

        student_name = attempt.student.full_name if attempt.student else 'Inconnu'
        session.close()
        return jsonify({'success': True, 'message': f'Bannissement de {student_name} levé avec succès.'})
    except Exception as e:
        print(f"❌ Erreur unban_exam_attempt: {e}")
        return jsonify({'error': str(e)}), 500


# ============================================================================
# EXPORT CSV DES RÉSULTATS D'UN EXAMEN EN LIGNE
# ============================================================================

@exams_bp.route('/api/online_exams/<int:exam_id>/results/csv', methods=['GET'])
@paseto_required
def export_exam_results_csv(exam_id):
    """Export CSV des résultats d'un examen en ligne (prof propriétaire ou admin)."""
    try:
        import csv, io
        from flask import Response

        user_id = get_current_user_id()
        session = get_session()
        user = session.query(User).filter_by(id=user_id).first()

        if user.role not in [UserRole.ADMIN, UserRole.PROFESSOR]:
            session.close()
            return jsonify({'error': 'Accès non autorisé'}), 403

        exam = session.query(OnlineExam).filter_by(id=exam_id).first()
        if not exam:
            session.close()
            return jsonify({'error': 'Examen introuvable'}), 404

        if user.role == UserRole.PROFESSOR and exam.created_by_id != user_id:
            session.close()
            return jsonify({'error': 'Accès réservé au professeur propriétaire de cet examen'}), 403

        attempts = session.query(ExamAttempt).options(
            joinedload(ExamAttempt.student)
        ).filter_by(exam_id=exam_id).order_by(ExamAttempt.started_at).all()

        output = io.StringIO()
        writer = csv.writer(output, delimiter=';')
        writer.writerow([
            'Nom complet', 'Email', 'Statut', 'Note /20',
            'Début', 'Soumission', 'Durée (min)', 'Incidents'
        ])

        for a in attempts:
            status_labels = {
                'submitted': 'Soumis', 'in_progress': 'En cours',
                'banned': 'Banni', 'timed_out': 'Temps écoulé'
            }
            status_val = (a.status.value if hasattr(a.status, 'value') else str(a.status)) if a.status else ''
            status_label = status_labels.get(status_val, str(a.status))

            duration = ''
            if a.started_at and a.submitted_at:
                delta = a.submitted_at - a.started_at
                duration = str(round(delta.total_seconds() / 60, 1))

            incidents = session.query(ExamActivityLog).filter_by(
                attempt_id=a.id
            ).count()

            writer.writerow([
                a.student.full_name if a.student else 'Inconnu',
                a.student.email if a.student else '',
                status_label,
                a.score if a.score is not None else '',
                a.started_at.strftime('%d/%m/%Y %H:%M') if a.started_at else '',
                a.submitted_at.strftime('%d/%m/%Y %H:%M') if a.submitted_at else '',
                duration,
                incidents
            ])

        csv_content = '﻿' + output.getvalue()  # BOM UTF-8 pour Excel
        session.close()

        safe_title = ''.join(c if c.isalnum() or c in ' -_' else '_' for c in (exam.title or 'examen'))
        filename = f"resultats_{safe_title[:40]}.csv"
        return Response(
            csv_content,
            mimetype='text/csv; charset=utf-8',
            headers={'Content-Disposition': f'attachment; filename="{filename}"'}
        )
    except Exception as e:
        print(f"❌ Erreur export_exam_results_csv: {e}")
        return jsonify({'error': str(e)}), 500


# ============================================================================
# CORRECTION AUTOMATIQUE DES EXAMENS EN LIGNE AVEC IA
# ============================================================================

@exams_bp.route('/api/exam_attempts/<int:attempt_id>/correct', methods=['POST'])
@paseto_required
def correct_exam_attempt(attempt_id):
    """Corriger automatiquement une tentative d'examen avec IA"""
    try:
        user_id = get_current_user_id()
        session = get_session()
        
        user = session.query(User).filter_by(id=user_id).first()
        if user.role not in [UserRole.PROFESSOR, UserRole.ADMIN]:
            session.close()
            return jsonify({'error': 'Accès non autorisé'}), 403
        
        # Récupérer la tentative
        attempt = session.query(ExamAttempt).options(
            joinedload(ExamAttempt.exam).joinedload(OnlineExam.subject),
            joinedload(ExamAttempt.student)
        ).filter_by(id=attempt_id).first()
        
        if not attempt:
            session.close()
            return jsonify({'error': 'Tentative non trouvée'}), 404
        
        # Vérifier que la tentative est soumise
        if attempt.status not in [AttemptStatus.SUBMITTED, AttemptStatus.AUTO_SUBMITTED]:
            session.close()
            return jsonify({'error': 'Cette tentative n\'est pas encore soumise'}), 400
        
        # Vérifier que le professeur est propriétaire de l'examen
        if user.role == UserRole.PROFESSOR and attempt.exam.created_by_id != user_id:
            session.close()
            return jsonify({'error': 'Vous ne pouvez corriger que vos propres examens'}), 403
        
        exam = attempt.exam
        subject = exam.subject
        
        # Extraire les réponses de l'étudiant (clés plates pq_N/pq_N_x réellement
        # envoyées par le frontend — reconstruites avec le texte des questions/
        # choix/paires pour que l'IA voie le contexte complet)
        try:
            answers_data = json.loads(attempt.answers) if attempt.answers else {}
        except Exception:
            answers_data = {}

        if not answers_data:
            session.close()
            return jsonify({'error': 'Aucune réponse à corriger pour cet étudiant'}), 400

        # Notation automatique (sans IA) des questions QCM/QCM_MULTI/Vrai-Faux/
        # Appariement — comme Moodle. Seul le reliquat de points (questions
        # ouvertes/code) est confié à l'IA, sur son propre total réduit. Le
        # score final est ramené proportionnellement sur 20 à partir du total
        # RÉEL des points du sujet (souvent ≠ 20 malgré la consigne de
        # génération), plutôt que de supposer un total fixe.
        det_score, det_max, total_max, det_breakdown, det_nums = _deterministic_grade(
            subject.content, subject.rubric or '', answers_data if isinstance(answers_data, dict) else {})
        remaining_max = round(total_max - det_max, 2)
        det_section = (
            "=== NOTATION AUTOMATIQUE (QCM / Vrai-Faux / Appariement — sans IA) ===\n"
            + ('\n'.join(det_breakdown) if det_breakdown else 'Aucune question de ce type notée.') + "\n\n"
        ) if det_breakdown else ""

        def _to_20(raw_points: float) -> float:
            if total_max <= 0.01:
                return 0.0
            return max(0.0, min(20.0, raw_points / total_max * 20))

        student_answers = ''
        if remaining_max > 0.01:
            student_answers = _build_readable_student_answers(subject.content, answers_data, exclude_nums=det_nums) if isinstance(answers_data, dict) else \
                (answers_data if isinstance(answers_data, str) else '')
            if not student_answers or not student_answers.strip():
                student_answers = attempt.answers or ''

        if remaining_max <= 0.01 or not student_answers.strip():
            if det_max <= 0.01:
                session.close()
                return jsonify({'error': 'Aucune réponse à corriger pour cet étudiant'}), 400
            score  = _to_20(det_score)
            result = f"{det_section}Note totale: {score:.2f}/20"
        else:
            system_prompt = _build_correction_system_prompt(
                exam.title + (" — " + subject.title if subject.title else ""),
                subject.content
            )
            excluded_note = (
                f"Questions déjà notées automatiquement en dehors de cette correction, ne les évalue pas : "
                f"{', '.join(sorted(det_nums, key=int))}.\n" if det_nums else ""
            )
            user_message = f"""SUJET D'EXAMEN:
{subject.content}

BARÈME DE NOTATION:
{subject.rubric}

COPIE À CORRIGER (Examen en ligne) :
Étudiant: {attempt.student.full_name}
Durée de l'examen: {exam.duration_minutes} minutes

RÉPONSES DE L'ÉTUDIANT (questions restantes uniquement) :
{student_answers}

{excluded_note}Tu DOIS noter UNIQUEMENT les questions listées ci-dessus, sur un total de {remaining_max:.2f} points (PAS 20).
Tu DOIS terminer ta correction par une ligne contenant EXACTEMENT : "Points obtenus : XX.XX" (jamais "Note totale", jamais "/20")."""

            # Appeler Claude pour la correction
            ai_result  = call_claude(system_prompt, user_message, temperature=0.15)
            ai_partial = _extract_points_obtenus(ai_result, remaining_max)
            score      = _to_20(det_score + ai_partial)
            result     = f"{det_section}{ai_result}\n\nNote totale: {score:.2f}/20"

        # Stocker les résultats
        attempt.score = score
        attempt.feedback = result
        attempt.corrected_at = utcnow()
        attempt.corrected_by_id = user_id
        
        session.commit()
        
        # Envoyer email à l'étudiant si adresse valide
        try:
            if attempt.student.email and '@temp.edu' not in attempt.student.email:
                email_sent = send_paper_corrected_email(
                    student_email=attempt.student.email,
                    student_name=attempt.student.full_name,
                    subject_title=f"{exam.title} (Examen en ligne)",
                    score=score,
                    paper_id=attempt.id
                )
                if email_sent:
                    print(f"✅ Email envoyé à {attempt.student.email}")
        except Exception as email_error:
            print(f"⚠️ Erreur envoi email: {email_error}")
        
        attempt_dict = attempt.to_dict()
        session.close()
        
        return jsonify({
            'success': True,
            'attempt': attempt_dict,
            'message': f'Correction terminée: {score}/20'
        })
        
    except Exception as e:
        print(f"❌ Erreur correct_exam_attempt: {e}")
        import traceback; traceback.print_exc()
        try: session.rollback(); session.close()
        except: pass
        return jsonify({'error': str(e)}), 500


@exams_bp.route('/api/online_exams/<int:exam_id>/attempts', methods=['GET'])
@paseto_required
def get_exam_attempts(exam_id):
    """Récupérer toutes les tentatives d'un examen (professeur/admin)"""
    try:
        user_id = get_current_user_id()
        session = get_session()
        
        user = session.query(User).filter_by(id=user_id).first()
        if user.role not in [UserRole.PROFESSOR, UserRole.ADMIN]:
            session.close()
            return jsonify({'error': 'Accès non autorisé'}), 403
        
        exam = session.query(OnlineExam).filter_by(id=exam_id).first()
        if not exam:
            session.close()
            return jsonify({'error': 'Examen non trouvé'}), 404
        
        # Vérifier propriété pour professeur
        if user.role == UserRole.PROFESSOR and exam.created_by_id != user_id:
            session.close()
            return jsonify({'error': 'Accès non autorisé'}), 403
        
        attempts = session.query(ExamAttempt).options(
            joinedload(ExamAttempt.student)
        ).filter_by(exam_id=exam_id).order_by(ExamAttempt.started_at.desc()).all()
        
        attempts_list = []
        for attempt in attempts:
            attempt_dict = attempt.to_dict()
            # Ajouter info incidents
            attempt_dict['has_incidents'] = attempt.warnings_count > 0 or attempt.tab_switches > 0
            attempt_dict['needs_correction'] = attempt.status in [AttemptStatus.SUBMITTED, AttemptStatus.AUTO_SUBMITTED] and attempt.score is None
            attempts_list.append(attempt_dict)
        
        session.close()
        return jsonify(attempts_list)
        
    except Exception as e:
        print(f"❌ Erreur get_exam_attempts: {e}")
        try: session.rollback(); session.close()
        except: pass
        return jsonify({'error': str(e)}), 500


# ============================================================================
# EXPORT CSV DES NOTES
# ============================================================================

@exams_bp.route('/api/online_exams/<int:exam_id>/export-csv', methods=['GET'])
@paseto_required
def export_exam_csv(exam_id):
    """Exporte les notes d'un examen en CSV (prof/admin)."""
    import csv
    try:
        user_id = get_current_user_id()
        session = get_session()
        user = session.query(User).filter_by(id=user_id).first()
        if user.role not in [UserRole.PROFESSOR, UserRole.ADMIN]:
            session.close()
            return jsonify({'error': 'Accès non autorisé'}), 403
        exam = session.query(OnlineExam).filter_by(id=exam_id).first()
        if not exam:
            session.close()
            return jsonify({'error': 'Examen non trouvé'}), 404
        if user.role == UserRole.PROFESSOR and exam.created_by_id != user_id:
            session.close()
            return jsonify({'error': 'Accès non autorisé'}), 403
        attempts = session.query(ExamAttempt).options(
            joinedload(ExamAttempt.student)
        ).filter_by(exam_id=exam_id).order_by(ExamAttempt.submitted_at).all()
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(['Étudiant', 'Email', 'Statut', 'Note /20', 'Risque %',
                         'Tab switches', 'Alertes', 'Durée (min)', 'Soumis à', 'Signature pré'])
        for a in attempts:
            name  = a.student.full_name if a.student else '?'
            email = a.student.email if a.student else ''
            dur   = int((a.submitted_at - a.started_at).total_seconds() / 60) if a.submitted_at and a.started_at else ''
            writer.writerow([
                name, email, a.status.value,
                a.score if a.score is not None else '',
                a.risk_score or 0,
                a.tab_switches or 0,
                a.warnings_count or 0,
                dur,
                a.submitted_at.strftime('%Y-%m-%d %H:%M') if a.submitted_at else '',
                'Oui' if a.pre_exam_signature_data else 'Non',
            ])
        session.close()
        filename = f"notes_{exam.title.replace(' ','_')}.csv"
        return send_file(
            io.BytesIO(output.getvalue().encode('utf-8-sig')),
            mimetype='text/csv',
            as_attachment=True,
            download_name=filename
        )
    except Exception as e:
        print(f"❌ export_exam_csv {exam_id}: {e}")
        try: session.close()
        except: pass
        return jsonify({'error': str(e)}), 500


# ============================================================================
# IMPORT EXCEL/CSV DES NOTES NON COMPOSÉES SUR LA PLATEFORME
# ============================================================================

@exams_bp.route('/api/online_exams/<int:exam_id>/import-grades', methods=['POST'])
@paseto_required
def import_exam_grades(exam_id):
    """
    Importe des notes déjà calculées ailleurs (épreuve papier, autre système)
    pour des étudiants n'ayant pas composé sur la plateforme (Notes point 14).
    Fichier Excel (.xlsx) ou CSV avec colonnes 'email' et 'note' (0-20).
    Crée une ExamAttempt marquée imported_grade=True, ou met à jour la note
    si une tentative existe déjà pour cet étudiant sur cet examen.
    """
    import pandas as pd
    try:
        user_id = get_current_user_id()
        session = get_session()
        user = session.query(User).filter_by(id=user_id).first()
        if user.role not in [UserRole.PROFESSOR, UserRole.ADMIN]:
            session.close()
            return jsonify({'error': 'Accès non autorisé'}), 403
        exam = session.query(OnlineExam).filter_by(id=exam_id).first()
        if not exam:
            session.close()
            return jsonify({'error': 'Examen non trouvé'}), 404
        if user.role == UserRole.PROFESSOR and exam.created_by_id != user_id:
            session.close()
            return jsonify({'error': 'Accès non autorisé'}), 403

        if 'file' not in request.files:
            session.close()
            return jsonify({'error': 'Aucun fichier fourni'}), 400
        file = request.files['file']
        if file.filename == '':
            session.close()
            return jsonify({'error': 'Aucun fichier sélectionné'}), 400

        filename = file.filename.lower()
        try:
            if filename.endswith('.xlsx') or filename.endswith('.xls'):
                df = pd.read_excel(file)
            elif filename.endswith('.csv'):
                df = pd.read_csv(file)
            else:
                session.close()
                return jsonify({'error': 'Format invalide. Utilisez .xlsx, .xls ou .csv'}), 400
        except Exception as e:
            session.close()
            return jsonify({'error': f'Lecture du fichier impossible: {e}'}), 400

        df.columns = [str(c).strip().lower() for c in df.columns]
        email_col = next((c for c in df.columns if c in ('email', 'e-mail', 'mail')), None)
        score_col = next((c for c in df.columns if c in ('note', 'score', 'note /20', 'note/20')), None)
        if not email_col or not score_col:
            session.close()
            return jsonify({'error': "Colonnes requises: 'email' et 'note'"}), 400

        imported, updated, errors = [], [], []
        for idx, row in df.iterrows():
            line = idx + 2
            email = str(row[email_col]).strip().lower() if pd.notna(row[email_col]) else ''
            if not email:
                errors.append(f"Ligne {line}: email manquant")
                continue
            try:
                score = float(row[score_col])
            except (ValueError, TypeError):
                errors.append(f"Ligne {line}: note invalide")
                continue
            if not (0 <= score <= 20):
                errors.append(f"Ligne {line}: note hors intervalle 0-20")
                continue

            student = session.query(User).filter_by(email=email, role=UserRole.STUDENT).first()
            if not student:
                errors.append(f"Ligne {line}: aucun étudiant avec l'email '{email}'")
                continue

            attempt = session.query(ExamAttempt).filter_by(exam_id=exam_id, student_id=student.id).first()
            if attempt:
                attempt.score = score
                attempt.corrected_at = utcnow()
                attempt.corrected_by_id = user_id
                attempt.imported_grade = True
                updated.append(email)
            else:
                attempt = ExamAttempt(
                    exam_id=exam_id,
                    student_id=student.id,
                    status=AttemptStatus.SUBMITTED,
                    started_at=utcnow(),
                    submitted_at=utcnow(),
                    score=score,
                    corrected_at=utcnow(),
                    corrected_by_id=user_id,
                    imported_grade=True,
                    feedback="Note importée depuis un fichier externe (composition hors plateforme).",
                )
                session.add(attempt)
                imported.append(email)

        session.commit()
        session.close()
        return jsonify({
            'success': True,
            'created': len(imported),
            'updated': len(updated),
            'errors': errors,
        })
    except Exception as e:
        print(f"❌ import_exam_grades {exam_id}: {e}")
        try: session.rollback(); session.close()
        except: pass
        return jsonify({'error': str(e)}), 500


# ============================================================================
# PUBLICATION DES NOTES (Retour #29 — masquées jusqu'à délibération)
# ============================================================================

@exams_bp.route('/api/online_exams/<int:exam_id>/publish-results', methods=['PUT'])
@paseto_required
def publish_exam_results(exam_id):
    """Publie ou dépublie les notes d'un examen aux étudiants (après
    délibération). Tant que non publié, le prof/admin voit toujours les
    notes (correction/gestion) mais l'étudiant reçoit score=null."""
    try:
        user_id = get_current_user_id()
        session = get_session()
        user = session.query(User).filter_by(id=user_id).first()
        if user.role not in [UserRole.PROFESSOR, UserRole.ADMIN]:
            session.close()
            return jsonify({'error': 'Accès non autorisé'}), 403
        exam = session.query(OnlineExam).filter_by(id=exam_id).first()
        if not exam:
            session.close()
            return jsonify({'error': 'Examen non trouvé'}), 404
        if user.role == UserRole.PROFESSOR and exam.created_by_id != user_id:
            session.close()
            return jsonify({'error': 'Accès non autorisé'}), 403
        data = request.get_json(silent=True) or {}
        exam.results_published = bool(data.get('published', True))
        session.commit()
        published = exam.results_published
        session.close()
        return jsonify({'success': True, 'results_published': published})
    except Exception as e:
        print(f"❌ publish_exam_results {exam_id}: {e}")
        try: session.rollback(); session.close()
        except: pass
        return jsonify({'error': str(e)}), 500


# ============================================================================
# STATISTIQUES PAR EXAMEN
# ============================================================================

@exams_bp.route('/api/online_exams/<int:exam_id>/stats', methods=['GET'])
@paseto_required
def get_exam_stats(exam_id):
    """Statistiques détaillées d'un examen : distribution, médiane, taux réussite."""
    try:
        user_id = get_current_user_id()
        session = get_session()
        user = session.query(User).filter_by(id=user_id).first()
        if user.role not in [UserRole.PROFESSOR, UserRole.ADMIN]:
            session.close()
            return jsonify({'error': 'Accès non autorisé'}), 403
        exam = session.query(OnlineExam).filter_by(id=exam_id).first()
        if not exam:
            session.close()
            return jsonify({'error': 'Examen non trouvé'}), 404
        attempts = session.query(ExamAttempt).filter_by(exam_id=exam_id).all()
        done = [a for a in attempts if a.status.value in ('submitted', 'auto_submitted')]
        scores = [a.score for a in done if a.score is not None]
        distribution = [0] * 5  # [0-4, 5-9, 10-13, 14-16, 17-20]
        for s in scores:
            if   s < 5:  distribution[0] += 1
            elif s < 10: distribution[1] += 1
            elif s < 14: distribution[2] += 1
            elif s < 17: distribution[3] += 1
            else:        distribution[4] += 1
        durations = []
        for a in done:
            if a.submitted_at and a.started_at:
                durations.append(int((a.submitted_at - a.started_at).total_seconds() / 60))
        session.close()
        return jsonify({
            'exam_title':       exam.title,
            'total':            len(attempts),
            'submitted':        len(done),
            'in_progress':      sum(1 for a in attempts if a.status.value == 'in_progress'),
            'banned':           sum(1 for a in attempts if a.status.value == 'banned'),
            'corrected':        sum(1 for a in done if a.score is not None),
            'avg_score':        round(sum(scores)/len(scores), 2) if scores else None,
            'median_score':     round(statistics.median(scores), 2) if scores else None,
            'min_score':        min(scores) if scores else None,
            'max_score':        max(scores) if scores else None,
            'pass_rate':        round(sum(1 for s in scores if s >= 10) / len(scores) * 100, 1) if scores else None,
            'distribution':     distribution,
            'avg_duration_min': round(sum(durations)/len(durations), 1) if durations else None,
            'avg_risk':         round(sum(a.risk_score or 0 for a in attempts) / len(attempts), 1) if attempts else 0,
            'high_risk_count':  sum(1 for a in attempts if (a.risk_score or 0) >= 70),
            'pre_sig_rate':     round(sum(1 for a in done if a.pre_exam_signature_data) / len(done) * 100, 1) if done else 0,
        })
    except Exception as e:
        print(f"❌ get_exam_stats {exam_id}: {e}")
        try: session.close()
        except: pass
        return jsonify({'error': str(e)}), 500


# ============================================================================
# BILAN PAR ÉTUDIANT
# ============================================================================

@exams_bp.route('/api/online_exams/<int:exam_id>/bilan', methods=['GET'])
@paseto_required
def get_exam_bilan(exam_id):
    """Liste détaillée par étudiant : score, risque, durée, statut, extra-temps, notes."""
    try:
        user_id = get_current_user_id()
        session = get_session()
        user = session.query(User).filter_by(id=user_id).first()
        if not user or user.role not in [UserRole.PROFESSOR, UserRole.ADMIN]:
            session.close()
            return jsonify({'error': 'Accès non autorisé'}), 403
        exam = session.query(OnlineExam).filter_by(id=exam_id).first()
        if not exam:
            session.close()
            return jsonify({'error': 'Examen non trouvé'}), 404

        from sqlalchemy import func as sa_func
        attempts = session.query(ExamAttempt).filter_by(exam_id=exam_id).all()

        # Compter les notes de surveillance par tentative
        note_counts = {}
        if attempts:
            ids = [a.id for a in attempts]
            rows = session.query(
                ExamActivityLog.attempt_id,
                sa_func.count(ExamActivityLog.id)
            ).filter(
                ExamActivityLog.attempt_id.in_(ids),
                ExamActivityLog.event_type == 'proctor_note'
            ).group_by(ExamActivityLog.attempt_id).all()
            note_counts = {r[0]: r[1] for r in rows}

        rows_out = []
        for a in attempts:
            duration_min = None
            if a.submitted_at and a.started_at:
                duration_min = round((a.submitted_at - a.started_at).total_seconds() / 60, 1)
            status_val = (a.status.value if hasattr(a.status, 'value') else str(a.status)) if a.status else ''
            rows_out.append({
                'attempt_id':    a.id,
                'student_name':  a.student.full_name if a.student else '—',
                'student_email': a.student.email if a.student else '—',
                'status':        status_val,
                'score':         a.score,
                'feedback':      a.feedback or '',
                'risk_score':    a.risk_score or 0,
                'extra_minutes': a.extra_minutes or 0,
                'duration_min':  duration_min,
                'submitted_at':  a.submitted_at.isoformat() if a.submitted_at else None,
                'corrected_at':  a.corrected_at.isoformat() if a.corrected_at else None,
                'note_count':    note_counts.get(a.id, 0),
            })

        rows_out.sort(key=lambda r: (r['status'] != 'submitted', -(r['score'] or -1)))
        exam_title = exam.title
        session.close()
        return jsonify({'exam_title': exam_title, 'attempts': rows_out})
    except Exception as e:
        print(f"❌ get_exam_bilan {exam_id}: {e}")
        try: session.close()
        except: pass
        return jsonify({'error': str(e)}), 500


@exams_bp.route('/api/online_exams/<int:exam_id>/bilan/pdf', methods=['GET'])
@paseto_required
def get_exam_bilan_pdf(exam_id):
    """Génère un PDF du bilan par étudiant avec reportlab."""
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib import colors
        from reportlab.lib.units import cm
        from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.enums import TA_CENTER, TA_LEFT

        user_id = get_current_user_id()
        session = get_session()
        user = session.query(User).filter_by(id=user_id).first()
        if not user or user.role not in [UserRole.PROFESSOR, UserRole.ADMIN]:
            session.close()
            return jsonify({'error': 'Accès non autorisé'}), 403
        exam = session.query(OnlineExam).filter_by(id=exam_id).first()
        if not exam:
            session.close()
            return jsonify({'error': 'Examen non trouvé'}), 404

        attempts = session.query(ExamAttempt).filter_by(exam_id=exam_id).all()
        exam_title = exam.title
        generated_at = utcnow().strftime('%d/%m/%Y %H:%M')

        status_labels = {
            'submitted': 'Soumis', 'auto_submitted': 'Auto-soumis',
            'in_progress': 'En cours', 'banned': 'Exclu', 'not_started': 'Absent'
        }

        rows_data = []
        scores = []
        for a in sorted(attempts, key=lambda x: (x.student.full_name if x.student else '')):
            sv = (a.status.value if hasattr(a.status, 'value') else str(a.status)) if a.status else ''
            dur = None
            if a.submitted_at and a.started_at:
                dur = round((a.submitted_at - a.started_at).total_seconds() / 60, 0)
            rows_data.append({
                'name':    a.student.full_name if a.student else '—',
                'status':  status_labels.get(sv, sv),
                'score':   a.score,
                'risk':    a.risk_score or 0,
                'dur':     int(dur) if dur is not None else None,
                'extra':   a.extra_minutes or 0,
            })
            if a.score is not None:
                scores.append(a.score)

        session.close()

        buf = io.BytesIO()
        doc = SimpleDocTemplate(buf, pagesize=A4,
                                leftMargin=2*cm, rightMargin=2*cm,
                                topMargin=2*cm, bottomMargin=2*cm)
        styles = getSampleStyleSheet()
        title_style = ParagraphStyle('title', parent=styles['Title'], fontSize=16, spaceAfter=4)
        sub_style   = ParagraphStyle('sub',   parent=styles['Normal'], fontSize=10, textColor=colors.grey, spaceAfter=12)
        story = [
            Paragraph(f"Bilan — {exam_title}", title_style),
            Paragraph(f"Généré le {generated_at} • {len(rows_data)} participant(s) • Moyenne : {round(sum(scores)/len(scores),2) if scores else '—'}/20", sub_style),
            Spacer(1, 0.3*cm),
        ]

        header = ['Étudiant', 'Statut', 'Note /20', 'Risque', 'Durée', 'Extra']
        table_data = [header]
        for r in rows_data:
            table_data.append([
                r['name'],
                r['status'],
                f"{r['score']:.2f}" if r['score'] is not None else '—',
                f"{r['risk']}%",
                f"{r['dur']} min" if r['dur'] is not None else '—',
                f"+{r['extra']} min" if r['extra'] > 0 else '—',
            ])

        col_widths = [6*cm, 3*cm, 2.5*cm, 2*cm, 2*cm, 2*cm]
        tbl = Table(table_data, colWidths=col_widths, repeatRows=1)

        ts = TableStyle([
            ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#1e293b')),
            ('TEXTCOLOR',  (0,0), (-1,0), colors.white),
            ('FONTNAME',   (0,0), (-1,0), 'Helvetica-Bold'),
            ('FONTSIZE',   (0,0), (-1,-1), 9),
            ('ALIGN',      (1,0), (-1,-1), 'CENTER'),
            ('ALIGN',      (0,0), (0,-1), 'LEFT'),
            ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.white, colors.HexColor('#f8fafc')]),
            ('GRID',       (0,0), (-1,-1), 0.3, colors.HexColor('#e2e8f0')),
            ('TOPPADDING', (0,0), (-1,-1), 5),
            ('BOTTOMPADDING', (0,0), (-1,-1), 5),
        ])
        # Colorer les notes
        for i, r in enumerate(rows_data, start=1):
            if r['score'] is not None:
                c = colors.HexColor('#059669') if r['score'] >= 10 else colors.HexColor('#dc2626')
                ts.add('TEXTCOLOR', (2,i), (2,i), c)
                ts.add('FONTNAME',  (2,i), (2,i), 'Helvetica-Bold')
        tbl.setStyle(ts)
        story.append(tbl)

        doc.build(story)
        buf.seek(0)
        safe_title = ''.join(c for c in exam_title if c.isalnum() or c in '-_ ')[:40]
        response = make_response(buf.read())
        response.headers['Content-Type'] = 'application/pdf'
        response.headers['Content-Disposition'] = f'attachment; filename="bilan-{safe_title}.pdf"'
        return response
    except Exception as e:
        import traceback
        print(f"❌ get_exam_bilan_pdf {exam_id}: {e}\n{traceback.format_exc()}")
        try: session.close()
        except: pass
        return jsonify({'error': str(e)}), 500


# ============================================================================
# CORRECTION MANUELLE
# ============================================================================

@exams_bp.route('/api/exam_attempts/<int:attempt_id>/manual-grade', methods=['PUT'])
@paseto_required
def manual_grade_attempt(attempt_id):
    """Correction manuelle par le professeur : saisie note + commentaire."""
    try:
        user_id = get_current_user_id()
        session = get_session()
        user = session.query(User).filter_by(id=user_id).first()
        if user.role not in [UserRole.PROFESSOR, UserRole.ADMIN]:
            session.close()
            return jsonify({'error': 'Accès non autorisé'}), 403
        attempt = session.query(ExamAttempt).filter_by(id=attempt_id).first()
        if not attempt:
            session.close()
            return jsonify({'error': 'Tentative non trouvée'}), 404
        if user.role == UserRole.PROFESSOR and attempt.exam.created_by_id != user_id:
            session.close()
            return jsonify({'error': 'Accès non autorisé'}), 403
        data   = request.get_json(silent=True) or {}
        score  = data.get('score')
        feedback = data.get('feedback', '').strip()
        if score is None:
            session.close()
            return jsonify({'error': 'Note obligatoire'}), 400
        try:
            score = float(score)
        except (ValueError, TypeError):
            session.close()
            return jsonify({'error': 'Note invalide'}), 400
        if not (0 <= score <= 20):
            session.close()
            return jsonify({'error': 'Note doit être entre 0 et 20'}), 400
        attempt.score          = score
        attempt.feedback       = feedback
        attempt.corrected_at   = utcnow()
        attempt.corrected_by_id = user_id
        session.commit()
        student_email = attempt.student.email if attempt.student else None
        attempt_id_copy = attempt.id
        session.close()
        return jsonify({'success': True, 'score': score, 'message': 'Note enregistrée'})
    except Exception as e:
        print(f"❌ manual_grade_attempt {attempt_id}: {e}")
        try: session.rollback(); session.close()
        except: pass
        return jsonify({'error': str(e)}), 500


# ============================================================================
# HISTORIQUE EXAMENS ÉTUDIANT
# ============================================================================

@exams_bp.route('/api/student/exam-history', methods=['GET'])
@paseto_required
def get_student_exam_history():
    """Historique complet des examens passés par l'étudiant connecté."""
    try:
        user_id = get_current_user_id()
        session = get_session()
        attempts = session.query(ExamAttempt).options(
            joinedload(ExamAttempt.exam)
        ).filter_by(student_id=user_id).order_by(ExamAttempt.started_at.desc()).all()
        history = []
        for a in attempts:
            exam = a.exam
            dur  = int((a.submitted_at - a.started_at).total_seconds() / 60) if a.submitted_at and a.started_at else None
            # Retour #29 — notes masquées tant que le professeur/admin n'a pas
            # publié les résultats de l'examen (délibération)
            published = bool(exam.results_published) if exam else True
            history.append({
                'attempt_id':   a.id,
                'exam_id':      a.exam_id,
                'exam_title':   exam.title if exam else '?',
                'status':       a.status.value,
                'score':        a.score if published else None,
                'feedback':     a.feedback if published else None,
                'risk_score':   a.risk_score or 0,
                'started_at':   a.started_at.isoformat() if a.started_at else None,
                'submitted_at': a.submitted_at.isoformat() if a.submitted_at else None,
                'duration_min': dur,
                'tab_switches': a.tab_switches or 0,
                'warnings':     a.warnings_count or 0,
                'has_pre_sig':  bool(a.pre_exam_signature_data),
                'corrected_at': a.corrected_at.isoformat() if (a.corrected_at and published) else None,
                'results_published': published,
                'pending_publication': a.score is not None and not published,
            })
        session.close()
        return jsonify({'history': history, 'total': len(history)})
    except Exception as e:
        print(f"❌ get_student_exam_history: {e}")
        try: session.close()
        except: pass
        return jsonify({'error': str(e)}), 500


# ============================================================================
# DÉTECTION DE PLAGIAT
# ============================================================================

@exams_bp.route('/api/online_exams/<int:exam_id>/plagiarism-check', methods=['GET'])
@paseto_required
def plagiarism_check(exam_id):
    """Détecte les copies suspectes en comparant les réponses soumises."""
    from difflib import SequenceMatcher
    import json as _json
    try:
        user_id = get_current_user_id()
        session = get_session()
        user = session.query(User).filter_by(id=user_id).first()
        if user.role not in [UserRole.PROFESSOR, UserRole.ADMIN]:
            session.close()
            return jsonify({'error': 'Accès non autorisé'}), 403
        exam = session.query(OnlineExam).filter_by(id=exam_id).first()
        if not exam:
            session.close()
            return jsonify({'error': 'Examen non trouvé'}), 404
        threshold = float(request.args.get('threshold', 0.75))
        attempts = session.query(ExamAttempt).options(
            joinedload(ExamAttempt.student)
        ).filter(
            ExamAttempt.exam_id == exam_id,
            ExamAttempt.status.in_([AttemptStatus.SUBMITTED, AttemptStatus.AUTO_SUBMITTED])
        ).all()
        def extract_text(answers_raw):
            if not answers_raw:
                return ''
            try:
                data = answers_raw if isinstance(answers_raw, dict) else _json.loads(answers_raw)
                parts = []
                if isinstance(data, dict):
                    for v in data.values():
                        parts.append(str(v))
                elif isinstance(data, list):
                    for item in data:
                        if isinstance(item, dict):
                            parts.append(str(item.get('answer', item.get('response', ''))))
                        else:
                            parts.append(str(item))
                return ' '.join(parts).lower().strip()
            except Exception:
                return str(answers_raw).lower().strip()
        suspicious = []
        for i in range(len(attempts)):
            for j in range(i+1, len(attempts)):
                a1, a2 = attempts[i], attempts[j]
                t1 = extract_text(a1.answers)
                t2 = extract_text(a2.answers)
                if not t1 or not t2 or len(t1) < 30 or len(t2) < 30:
                    continue
                ratio = SequenceMatcher(None, t1, t2).ratio()
                if ratio >= threshold:
                    suspicious.append({
                        'student1_id':   a1.student_id,
                        'student1_name': a1.student.full_name if a1.student else '?',
                        'attempt1_id':   a1.id,
                        'student2_id':   a2.student_id,
                        'student2_name': a2.student.full_name if a2.student else '?',
                        'attempt2_id':   a2.id,
                        'similarity':    round(ratio * 100, 1),
                        'level':         'CRITIQUE' if ratio >= 0.9 else 'SUSPECT',
                    })
        suspicious.sort(key=lambda x: x['similarity'], reverse=True)
        session.close()
        return jsonify({
            'exam_title':    exam.title,
            'total_checked': len(attempts),
            'threshold_pct': round(threshold * 100),
            'suspicious':    suspicious,
            'total_pairs':   len(suspicious),
        })
    except Exception as e:
        import traceback
        print(f"❌ Erreur plagiarism_check exam {exam_id}: {e}\n{traceback.format_exc()}")
        try: session.close()
        except: pass
        return jsonify({'error': str(e)}), 500


# ============================================================================
# RAPPORT D'INTÉGRITÉ PDF
# ============================================================================

@exams_bp.route('/api/security/face_references', methods=['GET'])
@paseto_required
def list_face_references():
    """Liste toutes les photos de référence pour les examens du prof/admin."""
    user_id = get_current_user_id()
    session = get_session()
    try:
        user = session.query(User).filter_by(id=user_id).first()
        if not user or user.role not in [UserRole.ADMIN, UserRole.PROFESSOR]:
            session.close()
            return jsonify({'error': 'Accès non autorisé'}), 403

        # Récupérer les tentatives concernées
        if user.role == UserRole.PROFESSOR:
            exam_ids = [e.id for e in session.query(OnlineExam).filter_by(created_by_id=user_id).all()]
            if not exam_ids:
                session.close()
                return jsonify({'references': []})
            attempts = session.query(ExamAttempt).filter(
                ExamAttempt.exam_id.in_(exam_ids)
            ).all()
        else:
            attempts = session.query(ExamAttempt).all()

        attempt_ids = [a.id for a in attempts]
        attempt_map = {a.id: a for a in attempts}

        import json as _json
        results = []

        # Chercher dans CameraLog d'abord
        cam_logs = session.query(CameraLog).filter(
            CameraLog.attempt_id.in_(attempt_ids),
            CameraLog.event_type == 'face_reference_captured'
        ).all() if attempt_ids else []

        cam_by_attempt = {}
        for c in cam_logs:
            if c.attempt_id not in cam_by_attempt:
                cam_by_attempt[c.attempt_id] = c

        # Chercher dans ExamActivityLog pour les events sans photo dans CameraLog
        act_logs = session.query(ExamActivityLog).filter(
            ExamActivityLog.attempt_id.in_(attempt_ids),
            ExamActivityLog.event_type == 'face_reference_captured'
        ).all() if attempt_ids else []

        processed = set()
        for log in act_logs:
            if log.attempt_id in processed:
                continue
            processed.add(log.attempt_id)
            att = attempt_map.get(log.attempt_id)
            if not att:
                continue
            student = session.query(User).filter_by(id=att.student_id).first()
            exam = session.query(OnlineExam).filter_by(id=att.exam_id).first()

            image_data = None
            cam = cam_by_attempt.get(log.attempt_id)
            if cam and cam.image_data:
                image_data = cam.image_data
            else:
                try:
                    parsed = _json.loads(log.event_data or '{}')
                    if isinstance(parsed, dict):
                        image_data = parsed.get('image_data') or parsed.get('photo')
                except Exception:
                    pass

            results.append({
                'attempt_id':   att.id,
                'student_name': student.full_name if student else '—',
                'exam_title':   exam.title if exam else '—',
                'captured_at':  log.timestamp.isoformat() if log.timestamp else None,
                'image_data':   image_data,
                'has_photo':    bool(image_data)
            })

        session.close()
        return jsonify({'references': results})
    except Exception as e:
        try: session.rollback(); session.close()
        except: pass
        return jsonify({'error': str(e)}), 500


@exams_bp.route('/api/exam_attempts/<int:attempt_id>/face_reference', methods=['GET'])
@paseto_required
def get_face_reference_photo(attempt_id):
    """Retourne la photo de référence de l'étudiant pour une tentative (prof/admin)."""
    user_id = get_current_user_id()
    session = get_session()
    try:
        user = session.query(User).filter_by(id=user_id).first()
        if not user or user.role not in [UserRole.ADMIN, UserRole.PROFESSOR]:
            session.close()
            return jsonify({'error': 'Accès non autorisé'}), 403

        attempt = session.query(ExamAttempt).filter_by(id=attempt_id).first()
        if not attempt:
            session.close()
            return jsonify({'error': 'Tentative non trouvée'}), 404

        # Vérification accès prof
        if user.role == UserRole.PROFESSOR:
            exam = session.query(OnlineExam).filter_by(id=attempt.exam_id).first()
            if not exam or exam.created_by_id != user_id:
                session.close()
                return jsonify({'error': 'Accès non autorisé à cet examen'}), 403

        # Chercher dans CameraLog
        cam = session.query(CameraLog).filter_by(
            attempt_id=attempt_id,
            event_type='face_reference_captured'
        ).order_by(CameraLog.timestamp.asc()).first()

        if cam and cam.image_filename and (
            cam.image_filename.startswith('snapshots/') or cam.image_filename.startswith('local:')
        ):
            from s3_client import get_snapshot_url
            url = get_snapshot_url(cam.image_filename)
            session.close()
            return jsonify({'image_data': None, 'image_url': url, 'has_photo': bool(url), 'source': 'camera_log'})

        if cam and cam.image_data:
            session.close()
            return jsonify({'image_data': cam.image_data, 'has_photo': True, 'source': 'camera_log'})

        # Fallback : chercher dans event_data de ExamActivityLog
        import json as _json
        log = session.query(ExamActivityLog).filter_by(
            attempt_id=attempt_id,
            event_type='face_reference_captured'
        ).first()
        if log and log.event_data:
            try:
                parsed = _json.loads(log.event_data)
                if isinstance(parsed, dict) and ('image_data' in parsed or 'photo' in parsed):
                    img = parsed.get('image_data') or parsed.get('photo')
                    session.close()
                    return jsonify({'image_data': img, 'has_photo': True, 'source': 'activity_log'})
            except Exception:
                pass

        session.close()
        return jsonify({'image_data': None, 'has_photo': False, 'message': 'Photo non disponible (capture non stockée)'})
    except Exception as e:
        try: session.rollback(); session.close()
        except: pass
        return jsonify({'error': str(e)}), 500


@exams_bp.route('/api/exam_attempts/<int:attempt_id>/integrity-report', methods=['GET'])
@paseto_required
def download_integrity_report(attempt_id):
    """Génère un rapport d'intégrité PDF pour une tentative (prof/admin)."""
    import base64, textwrap
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import cm
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image as RLImage
    from reportlab.lib import colors as rl_colors
    from reportlab.lib.enums import TA_CENTER, TA_LEFT
    try:
        user_id = get_current_user_id()
        session = get_session()
        user = session.query(User).filter_by(id=user_id).first()
        if user.role not in [UserRole.PROFESSOR, UserRole.ADMIN]:
            session.close()
            return jsonify({'error': 'Accès non autorisé'}), 403
        attempt = session.query(ExamAttempt).options(
            joinedload(ExamAttempt.student),
            joinedload(ExamAttempt.exam),
        ).filter_by(id=attempt_id).first()
        if not attempt:
            session.close()
            return jsonify({'error': 'Tentative non trouvée'}), 404
        student = attempt.student
        exam    = attempt.exam
        logs    = session.query(ExamActivityLog).filter_by(attempt_id=attempt_id).order_by(ExamActivityLog.timestamp).all()
        session.close()

        buf = io.BytesIO()
        doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=2*cm, rightMargin=2*cm, topMargin=2*cm, bottomMargin=2*cm)
        styles = getSampleStyleSheet()
        title_style = ParagraphStyle('t', parent=styles['Title'], fontSize=16, textColor=rl_colors.HexColor('#1e293b'), spaceAfter=6)
        h2_style    = ParagraphStyle('h2', parent=styles['Heading2'], fontSize=12, textColor=rl_colors.HexColor('#2563eb'), spaceBefore=14, spaceAfter=4)
        normal      = ParagraphStyle('n', parent=styles['Normal'], fontSize=9, leading=13)
        small       = ParagraphStyle('s', parent=styles['Normal'], fontSize=8, textColor=rl_colors.HexColor('#64748b'))
        story = []

        # Entête
        story.append(Paragraph('RAPPORT D\'INTÉGRITÉ — CEI', title_style))
        story.append(Paragraph(f'Examen : {exam.title if exam else "?"}', styles['Heading2']))
        story.append(Paragraph(f'Généré le {utcnow().strftime("%d/%m/%Y à %H:%M")} UTC', small))
        story.append(Spacer(1, 12))

        # Infos étudiant
        story.append(Paragraph('Informations étudiant', h2_style))
        info_data = [
            ['Nom complet', student.full_name if student else '?'],
            ['Email', student.email if student else ''],
            ['Statut tentative', attempt.status.value],
            ['Note obtenue', f'{attempt.score}/20' if attempt.score is not None else 'Non corrigé'],
            ['Démarré le', attempt.started_at.strftime('%d/%m/%Y %H:%M') if attempt.started_at else '—'],
            ['Soumis le', attempt.submitted_at.strftime('%d/%m/%Y %H:%M') if attempt.submitted_at else '—'],
        ]
        if attempt.submitted_at and attempt.started_at:
            dur = int((attempt.submitted_at - attempt.started_at).total_seconds() / 60)
            info_data.append(['Durée', f'{dur} minutes'])
        t = Table(info_data, colWidths=[5*cm, 12*cm])
        t.setStyle(TableStyle([
            ('BACKGROUND', (0,0), (0,-1), rl_colors.HexColor('#f1f5f9')),
            ('FONTSIZE', (0,0), (-1,-1), 9),
            ('GRID', (0,0), (-1,-1), 0.5, rl_colors.HexColor('#e2e8f0')),
            ('PADDING', (0,0), (-1,-1), 5),
        ]))
        story.append(t)
        story.append(Spacer(1, 10))

        # Score de risque
        story.append(Paragraph('Indicateurs de surveillance', h2_style))
        risk_color = rl_colors.red if (attempt.risk_score or 0) >= 70 else rl_colors.orange if (attempt.risk_score or 0) >= 40 else rl_colors.green
        risk_data = [
            ['Score de risque', f'{attempt.risk_score or 0}/100'],
            ['Changements d\'onglet', str(attempt.tab_switches or 0)],
            ['Alertes comportementales', str(attempt.warnings_count or 0)],
            ['Signature pré-examen', 'Présente ✓' if attempt.pre_exam_signature_data else 'Absente ✗'],
            ['Signature post-examen', 'Présente ✓' if attempt.signature_data else ('Auto-soumission' if attempt.status.value == 'auto_submitted' else 'Absente ✗')],
        ]
        rt = Table(risk_data, colWidths=[5*cm, 12*cm])
        rt.setStyle(TableStyle([
            ('BACKGROUND', (0,0), (0,-1), rl_colors.HexColor('#f1f5f9')),
            ('FONTSIZE', (0,0), (-1,-1), 9),
            ('GRID', (0,0), (-1,-1), 0.5, rl_colors.HexColor('#e2e8f0')),
            ('PADDING', (0,0), (-1,-1), 5),
            ('TEXTCOLOR', (1,0), (1,0), risk_color),
            ('FONTNAME', (1,0), (1,0), 'Helvetica-Bold'),
        ]))
        story.append(rt)
        story.append(Spacer(1, 10))

        # Signature pré-examen
        if attempt.pre_exam_signature_data:
            story.append(Paragraph('Signature pré-examen (attestation)', h2_style))
            try:
                sig_data = attempt.pre_exam_signature_data
                if ',' in sig_data:
                    sig_data = sig_data.split(',', 1)[1]
                sig_bytes = base64.b64decode(sig_data)
                sig_buf = io.BytesIO(sig_bytes)
                img = RLImage(sig_buf, width=8*cm, height=4*cm)
                story.append(img)
                if attempt.pre_exam_signature_meta:
                    try:
                        meta = json.loads(attempt.pre_exam_signature_meta)
                        story.append(Paragraph(
                            f"Traits: {meta.get('strokes','?')} · Durée: {round((meta.get('duration_ms',0))/1000,1)}s · Longueur: {round(meta.get('path_length',0))}px",
                            small
                        ))
                    except Exception: pass
            except Exception as e:
                story.append(Paragraph(f'[Signature non lisible: {e}]', small))
            story.append(Spacer(1, 8))

        # Timeline des événements
        if logs:
            story.append(Paragraph('Chronologie des événements', h2_style))
            EVENT_FR = {
                'tab_switch': 'Changement d\'onglet',
                'window_blur': 'Fenêtre au second plan',
                'copy_attempt': 'Tentative de copie détectée',
                'paste_attempt': 'Tentative de collage détectée',
                'right_click': 'Clic droit bloqué',
                'no_face_detected': 'Absent 3 vérifications consécutives',
                'face_reference_captured': 'Référence faciale capturée',
                'warning_issued': 'Avertissement émis',
                'ban': 'Exclusion de l\'examen',
                'unban': 'Bannissement levé',
                'submit': 'Copie soumise',
                'auto_submit': 'Soumission automatique',
                'proctor_note': 'Note du surveillant',
                'extra_time': 'Temps supplémentaire accordé',
            }
            DETAIL_FR = {
                'tab_switch': 'Changement d\'onglet',
                'window_blur': 'Fenêtre au second plan',
                'copy_attempt': 'Tentative de copie détectée',
                'paste_attempt': 'Tentative de collage détectée',
                'right_click': 'Clic droit bloqué',
                'no_face_detected': 'Absent 3 vérifications consécutives',
                'face_reference_captured': 'Référence capturée (3 frames)',
            }
            log_data = [['Heure', 'Type', 'Détail']]
            for log in logs[:50]:
                ts = log.timestamp.strftime('%H:%M:%S') if log.timestamp else '—'
                evt = EVENT_FR.get(log.event_type, log.event_type or '?')
                detail = DETAIL_FR.get(log.event_type, '')
                if not detail:
                    try:
                        ed = json.loads(log.event_data) if log.event_data else {}
                        detail = ed.get('message') or ed.get('reason') or ed.get('note') or ''
                    except Exception:
                        detail = (log.event_data or '')[:80]
                log_data.append([ts, evt, detail[:80]])
            lt = Table(log_data, colWidths=[2*cm, 4*cm, 11*cm])
            lt.setStyle(TableStyle([
                ('BACKGROUND', (0,0), (-1,0), rl_colors.HexColor('#1e293b')),
                ('TEXTCOLOR', (0,0), (-1,0), rl_colors.white),
                ('FONTSIZE', (0,0), (-1,-1), 8),
                ('GRID', (0,0), (-1,-1), 0.3, rl_colors.HexColor('#e2e8f0')),
                ('PADDING', (0,0), (-1,-1), 4),
                ('ROWBACKGROUNDS', (0,1), (-1,-1), [rl_colors.white, rl_colors.HexColor('#f8fafc')]),
            ]))
            story.append(lt)

        doc.build(story)
        buf.seek(0)
        safe_name = (student.full_name if student else 'etudiant').replace(' ', '_')
        return send_file(buf, mimetype='application/pdf', as_attachment=True,
                         download_name=f'rapport_integrite_{safe_name}_{attempt_id}.pdf')
    except Exception as e:
        try: session.close()
        except: pass
        print(f'Erreur integrity_report: {e}')
        return jsonify({'error': str(e)}), 500


@exams_bp.route('/api/online_exams/<int:exam_id>/security-report/pdf', methods=['GET'])
@paseto_required
def download_exam_security_report(exam_id):
    """
    Rapport de sécurité PDF agrégé pour UN examen (toutes ses tentatives) —
    Notes point 18. Contrairement à admin_security_report (JSON, toutes
    évaluations confondues) et download_integrity_report (PDF, une seule
    tentative), ce rapport synthétise les incidents de surveillance de
    tous les étudiants d'un même examen, triés par risque décroissant.
    """
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import cm
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
    from reportlab.lib import colors as rl_colors
    try:
        user_id = get_current_user_id()
        session = get_session()
        user = session.query(User).filter_by(id=user_id).first()
        if user.role not in [UserRole.PROFESSOR, UserRole.ADMIN]:
            session.close()
            return jsonify({'error': 'Accès non autorisé'}), 403
        exam = session.query(OnlineExam).filter_by(id=exam_id).first()
        if not exam:
            session.close()
            return jsonify({'error': 'Examen non trouvé'}), 404
        if user.role == UserRole.PROFESSOR and exam.created_by_id != user_id:
            session.close()
            return jsonify({'error': 'Accès non autorisé'}), 403

        attempts = session.query(ExamAttempt).options(
            joinedload(ExamAttempt.student)
        ).filter_by(exam_id=exam_id).order_by(desc(ExamAttempt.risk_score)).all()

        ids = [a.id for a in attempts]
        incident_counts = {}
        if ids:
            rows = session.query(
                ExamActivityLog.attempt_id, sa_func.count(ExamActivityLog.id)
            ).filter(
                ExamActivityLog.attempt_id.in_(ids),
                ExamActivityLog.event_type != 'proctor_note'
            ).group_by(ExamActivityLog.attempt_id).all()
            incident_counts = {r[0]: r[1] for r in rows}

        session.close()

        buf = io.BytesIO()
        doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=1.5*cm, rightMargin=1.5*cm, topMargin=1.5*cm, bottomMargin=1.5*cm)
        styles = getSampleStyleSheet()
        title_style = ParagraphStyle('t', parent=styles['Title'], fontSize=16, textColor=rl_colors.HexColor('#1e293b'), spaceAfter=6)
        h2_style    = ParagraphStyle('h2', parent=styles['Heading2'], fontSize=12, textColor=rl_colors.HexColor('#2563eb'), spaceBefore=14, spaceAfter=4)
        small       = ParagraphStyle('s', parent=styles['Normal'], fontSize=8, textColor=rl_colors.HexColor('#64748b'))
        story = []

        story.append(Paragraph('RAPPORT DE SÉCURITÉ — CEI', title_style))
        story.append(Paragraph(f'Examen : {exam.title}', styles['Heading2']))
        story.append(Paragraph(f'Généré le {utcnow().strftime("%d/%m/%Y à %H:%M")} UTC', small))
        story.append(Spacer(1, 12))

        banned = sum(1 for a in attempts if a.status == AttemptStatus.BANNED)
        risky  = sum(1 for a in attempts if (a.risk_score or 0) >= 70)
        avg_risk = round(sum(a.risk_score or 0 for a in attempts) / len(attempts), 1) if attempts else 0
        total_incidents = sum(incident_counts.values())

        story.append(Paragraph('Synthèse', h2_style))
        summary_data = [
            ['Participants', str(len(attempts))],
            ['Exclus (bannis)', str(banned)],
            ['Risque élevé (≥ 70%)', str(risky)],
            ['Risque moyen', f'{avg_risk}%'],
            ['Total incidents', str(total_incidents)],
        ]
        st = Table(summary_data, colWidths=[6*cm, 11*cm])
        st.setStyle(TableStyle([
            ('BACKGROUND', (0,0), (0,-1), rl_colors.HexColor('#f1f5f9')),
            ('FONTSIZE', (0,0), (-1,-1), 9),
            ('GRID', (0,0), (-1,-1), 0.5, rl_colors.HexColor('#e2e8f0')),
            ('PADDING', (0,0), (-1,-1), 5),
        ]))
        story.append(st)
        story.append(Spacer(1, 10))

        story.append(Paragraph('Détail par étudiant (trié par risque décroissant)', h2_style))
        rows_data = [['Étudiant', 'Statut', 'Risque', 'Onglets', 'Alertes', 'Incidents', 'Note']]
        for a in attempts:
            rows_data.append([
                a.student.full_name if a.student else '?',
                a.status.value,
                f'{a.risk_score or 0}%',
                str(a.tab_switches or 0),
                str(a.warnings_count or 0),
                str(incident_counts.get(a.id, 0)),
                f'{a.score}/20' if a.score is not None else '—',
            ])
        rt = Table(rows_data, colWidths=[5*cm, 2.7*cm, 1.8*cm, 1.8*cm, 1.8*cm, 1.9*cm, 1.8*cm])
        row_styles = [
            ('BACKGROUND', (0,0), (-1,0), rl_colors.HexColor('#1e293b')),
            ('TEXTCOLOR', (0,0), (-1,0), rl_colors.white),
            ('FONTSIZE', (0,0), (-1,-1), 8),
            ('GRID', (0,0), (-1,-1), 0.3, rl_colors.HexColor('#e2e8f0')),
            ('PADDING', (0,0), (-1,-1), 4),
            ('ROWBACKGROUNDS', (0,1), (-1,-1), [rl_colors.white, rl_colors.HexColor('#f8fafc')]),
        ]
        for i, a in enumerate(attempts, start=1):
            risk_val = a.risk_score or 0
            color = rl_colors.HexColor('#ef4444') if risk_val >= 70 else rl_colors.HexColor('#f59e0b') if risk_val >= 40 else rl_colors.HexColor('#10b981')
            row_styles.append(('TEXTCOLOR', (2, i), (2, i), color))
            row_styles.append(('FONTNAME', (2, i), (2, i), 'Helvetica-Bold'))
        rt.setStyle(TableStyle(row_styles))
        story.append(rt)

        doc.build(story)
        buf.seek(0)
        safe_title = exam.title.replace(' ', '_')
        return send_file(buf, mimetype='application/pdf', as_attachment=True,
                         download_name=f'rapport_securite_{safe_title}_{exam_id}.pdf')
    except Exception as e:
        try: session.close()
        except: pass
        print(f'Erreur security_report exam {exam_id}: {e}')
        return jsonify({'error': str(e)}), 500


# ============================================================================
# LOGS ET INCIDENTS DES EXAMENS
# ============================================================================

@exams_bp.route('/api/online_exams/<int:exam_id>/incidents', methods=['GET'])
@paseto_required
def get_exam_incidents(exam_id):
    """Récupérer tous les incidents/logs d'un examen (professeur/admin)"""
    try:
        user_id = get_current_user_id()
        session = get_session()
        
        user = session.query(User).filter_by(id=user_id).first()
        if user.role not in [UserRole.PROFESSOR, UserRole.ADMIN, UserRole.SURVEILLANT]:
            session.close()
            return jsonify({'error': 'Accès non autorisé'}), 403

        exam = session.query(OnlineExam).filter_by(id=exam_id).first()
        if not exam:
            session.close()
            return jsonify({'error': 'Examen non trouvé'}), 404

        if user.role == UserRole.PROFESSOR and exam.created_by_id != user_id:
            session.close()
            return jsonify({'error': 'Accès non autorisé'}), 403

        # Récupérer les logs — pour un surveillant, uniquement ses étudiants assignés
        if user.role == UserRole.SURVEILLANT:
            assigned_attempt_ids = [
                pa.attempt_id for pa in session.query(ProctorAssignment).filter_by(proctor_id=user_id).all()
            ]
            logs = session.query(ExamActivityLog).join(ExamAttempt).filter(
                ExamAttempt.exam_id == exam_id,
                ExamActivityLog.attempt_id.in_(assigned_attempt_ids)
            ).order_by(ExamActivityLog.timestamp.desc()).all()
        else:
            logs = session.query(ExamActivityLog).join(ExamAttempt).filter(
                ExamAttempt.exam_id == exam_id
            ).order_by(ExamActivityLog.timestamp.desc()).all()
        
        incidents_list = []
        for log in logs:
            log_dict = log.to_dict()
            log_dict['student_name'] = log.attempt.student.full_name if log.attempt.student else 'Inconnu'
            log_dict['student_id'] = log.attempt.student_id
            log_dict['severity'] = 'high' if log.event_type in ['tab_switch', 'devtools_attempt'] else 'medium'
            incidents_list.append(log_dict)
        
        # Statistiques
        total_incidents = len(logs)
        tab_switches = len([l for l in logs if l.event_type == 'tab_switch'])
        banned_students = session.query(ExamAttempt).filter_by(
            exam_id=exam_id,
            status=AttemptStatus.BANNED
        ).count()
        
        session.close()
        
        return jsonify({
            'incidents': incidents_list,
            'statistics': {
                'total_incidents': total_incidents,
                'tab_switches': tab_switches,
                'banned_students': banned_students
            }
        })
        
    except Exception as e:
        print(f"❌ Erreur get_exam_incidents: {e}")
        return jsonify({'error': str(e)}), 500


@exams_bp.route('/api/professor/recent_incidents', methods=['GET'])
@paseto_required
def get_professor_recent_incidents():
    """Récupérer les incidents récents pour le professeur connecté"""
    try:
        user_id = get_current_user_id()
        session = get_session()
        
        user = session.query(User).filter_by(id=user_id).first()
        if user.role != UserRole.PROFESSOR:
            session.close()
            return jsonify({'error': 'Accès réservé aux professeurs'}), 403
        
        # Récupérer les examens actifs du professeur
        active_exams = session.query(OnlineExam).filter_by(
            created_by_id=user_id,
            status=ExamStatus.ACTIVE
        ).all()
        
        exam_ids = [e.id for e in active_exams]
        
        if not exam_ids:
            session.close()
            return jsonify({'incidents': [], 'unread_count': 0})
        
        # Incidents des dernières 24h
        since = utcnow() - timedelta(hours=24)
        
        incidents = session.query(ExamActivityLog).join(ExamAttempt).filter(
            ExamAttempt.exam_id.in_(exam_ids),
            ExamActivityLog.timestamp >= since
        ).order_by(ExamActivityLog.timestamp.desc()).limit(100).all()

        # Charger les snapshots caméra pour les events visuels (face)
        VISUAL_EVENTS = {'no_face_detected', 'no_face', 'multiple_faces', 'face_reference_captured',
                         'face_absent', 'mismatch_detected'}
        attempt_ids_with_visual = [i.attempt_id for i in incidents if i.event_type in VISUAL_EVENTS]
        cam_logs = {}
        if attempt_ids_with_visual:
            raw_cams = session.query(CameraLog).filter(
                CameraLog.attempt_id.in_(attempt_ids_with_visual),
                CameraLog.timestamp >= since,
                CameraLog.image_data.isnot(None)
            ).order_by(CameraLog.timestamp.desc()).all()
            # Grouper par attempt_id pour lookup rapide
            for cam in raw_cams:
                cam_logs.setdefault(cam.attempt_id, []).append(cam)

        HIGH_SEVERITY = {'tab_switch', 'devtools_attempt', 'multiple_faces', 'proctor_ban', 'teacher_ban'}

        incidents_list = []
        for incident in incidents:
            incident_dict = incident.to_dict()
            incident_dict['student_name'] = incident.attempt.student.full_name
            incident_dict['exam_title']   = incident.attempt.exam.title
            incident_dict['severity']     = 'high' if incident.event_type in HIGH_SEVERITY else 'medium'
            # Trouver le snapshot caméra le plus proche (±30s)
            snapshot_data = None
            if incident.event_type in VISUAL_EVENTS and incident.attempt_id in cam_logs:
                inc_ts = incident.timestamp
                best = None
                best_diff = 30  # secondes max
                for cam in cam_logs[incident.attempt_id]:
                    diff = abs((cam.timestamp - inc_ts).total_seconds())
                    if diff < best_diff:
                        best_diff = diff
                        best = cam
                if best:
                    snapshot_data = best.image_data
            incident_dict['snapshot_data'] = snapshot_data
            incidents_list.append(incident_dict)

        # Notifications EC affectés (7 derniers jours) — section séparée
        ec_since = utcnow() - timedelta(days=7)
        new_assignments = session.query(ECAssignment).filter(
            ECAssignment.professor_id == user_id,
            ECAssignment.assigned_at >= ec_since
        ).all()
        ec_notifs = []
        for asgn in new_assignments:
            ec = asgn.ec
            ec_notifs.append({
                'id': f'ec_assign_{asgn.id}',
                'event_type': 'ec_assignment',
                'timestamp': asgn.assigned_at.isoformat() if asgn.assigned_at else None,
                'details': f"Affectation à l'EC : {ec.name if ec else asgn.ec_id}",
                'student_name': '',
                'exam_title': '',
                'severity': 'info',
                'snapshot_data': None
            })

        all_items = incidents_list + ec_notifs

        # Retirer les items déjà supprimés/marqués comme lus par ce professeur
        dismissed_ids = {d.item_id for d in session.query(IncidentDismissal).filter_by(user_id=user_id).all()}
        all_items = [item for item in all_items if str(item['id']) not in dismissed_ids]

        session.close()

        return jsonify({
            'incidents': all_items,
            'unread_count': len(all_items)
        })

    except Exception as e:
        print(f"❌ Erreur get_professor_recent_incidents: {e}")
        return jsonify({'error': str(e)}), 500


@exams_bp.route('/api/professor/recent_incidents/dismiss', methods=['POST'])
@paseto_required
def dismiss_recent_incidents():
    """Supprimer/marquer comme lu un ou plusieurs items du flux « Notifications
    d'Incidents » (individuellement, ou en masse pour « Tout marquer comme lu »).
    Le flux lui-même n'étant jamais stocké, seule cette suppression persiste."""
    try:
        user_id = get_current_user_id()
        session = get_session()
        user = session.query(User).filter_by(id=user_id).first()
        if user.role != UserRole.PROFESSOR:
            session.close()
            return jsonify({'error': 'Accès réservé aux professeurs'}), 403

        data = request.get_json(silent=True) or {}
        item_ids = data.get('item_ids') or ([data['item_id']] if data.get('item_id') else [])
        item_ids = [str(i) for i in item_ids]
        if not item_ids:
            session.close()
            return jsonify({'error': 'item_id(s) requis'}), 400

        existing = {
            d.item_id for d in session.query(IncidentDismissal)
            .filter_by(user_id=user_id).filter(IncidentDismissal.item_id.in_(item_ids)).all()
        }
        added = 0
        for iid in item_ids:
            if iid in existing:
                continue
            session.add(IncidentDismissal(user_id=user_id, item_id=iid))
            added += 1
        session.commit()
        session.close()
        return jsonify({'success': True, 'dismissed': added})
    except Exception as e:
        print(f"❌ Erreur dismiss_recent_incidents: {e}")
        return jsonify({'error': str(e)}), 500


# ============================================================================
# HISTORIQUE DES EXAMENS (ADMIN)
# ============================================================================

@exams_bp.route('/api/admin/exams_history', methods=['GET'])
@paseto_required
def get_exams_history():
    """Historique des examens terminés avec statistiques (admin only)"""
    try:
        user_id = get_current_user_id()
        session = get_session()
        
        user = session.query(User).filter_by(id=user_id).first()
        if user.role != UserRole.ADMIN:
            session.close()
            return jsonify({'error': 'Accès réservé aux administrateurs'}), 403
        
        # Récupérer tous les examens terminés
        closed_exams = session.query(OnlineExam).filter_by(
            status=ExamStatus.CLOSED
        ).order_by(OnlineExam.end_time.desc()).all()
        
        history_list = []
        for exam in closed_exams:
            attempts = session.query(ExamAttempt).filter_by(exam_id=exam.id).all()
            
            submitted_count = len([a for a in attempts if a.status in [AttemptStatus.SUBMITTED, AttemptStatus.AUTO_SUBMITTED]])
            banned_count = len([a for a in attempts if a.status == AttemptStatus.BANNED])
            corrected_count = len([a for a in attempts if a.score is not None])
            
            # Moyenne des notes
            scores = [a.score for a in attempts if a.score is not None]
            average_score = round(sum(scores) / len(scores), 2) if scores else 0
            
            # Incidents totaux
            incidents_count = session.query(ExamActivityLog).join(ExamAttempt).filter(
                ExamAttempt.exam_id == exam.id
            ).count()
            
            history_list.append({
                'id': exam.id,
                'title': exam.title,
                'subject_title': exam.subject.title if exam.subject else None,
                'creator_name': exam.creator.full_name if exam.creator else None,
                'start_time': exam.start_time.isoformat(),
                'end_time': exam.end_time.isoformat(),
                'duration_minutes': exam.duration_minutes,
                'total_attempts': len(attempts),
                'submitted_count': submitted_count,
                'banned_count': banned_count,
                'corrected_count': corrected_count,
                'average_score': average_score,
                'incidents_count': incidents_count,
                'created_at': exam.created_at.isoformat()
            })
        
        session.close()
        return jsonify(history_list)
        
    except Exception as e:
        print(f" Erreur get_exams_history: {e}")
        return jsonify({'error': str(e)}), 500

# ============================================================================
# NOUVEAU : CRÉATION ÉTUDIANT SANS EMAIL
# ============================================================================
# NOUVEAU : LISTE DES COPIES CORRIGÉES (PROFESSEUR)
# ============================================================================

@exams_bp.route('/api/professor/corrected_papers', methods=['GET'])
@paseto_required
def professor_corrected_papers():
    """Liste des copies corrigées : copies papier + examens en ligne"""
    try:
        user_id = get_current_user_id()
        session = get_session()

        user = session.query(User).filter_by(id=user_id).first()
        if user.role not in [UserRole.PROFESSOR, UserRole.ADMIN]:
            session.close()
            return jsonify({'error': 'Accès non autorisé'}), 403

        # ── Copies papier ────────────────────────────────────────────────────
        paper_query = session.query(StudentPaper).options(
            joinedload(StudentPaper.student),
            joinedload(StudentPaper.subject)
        ).filter(StudentPaper.corrected_at != None)

        if user.role == UserRole.PROFESSOR:
            paper_query = paper_query.filter(StudentPaper.corrected_by_id == user_id)

        papers = paper_query.order_by(StudentPaper.corrected_at.desc()).limit(100).all()

        papers_list = []
        for p in papers:
            papers_list.append({
                'id': p.id,
                'type': 'paper',
                'student_name':  p.student.full_name if p.student else 'Inconnu',
                'student_email': p.student.email if p.student and p.student.has_email else 'Pas d\'email',
                'subject_title': p.subject.title if p.subject else 'N/A',
                'score': p.score,
                'corrected_at': p.corrected_at.isoformat() if p.corrected_at else None,
                'email_sent': p.email_sent,
                'filename': p.filename
            })

        # ── Examens en ligne corrigés ────────────────────────────────────────
        attempt_query = session.query(ExamAttempt).options(
            joinedload(ExamAttempt.student),
            joinedload(ExamAttempt.exam).joinedload(OnlineExam.subject)
        ).join(OnlineExam, ExamAttempt.exam_id == OnlineExam.id).filter(
            ExamAttempt.score.isnot(None)
        )

        if user.role == UserRole.PROFESSOR:
            attempt_query = attempt_query.filter(OnlineExam.created_by_id == user_id)

        attempts = attempt_query.order_by(ExamAttempt.corrected_at.desc()).limit(100).all()

        for att in attempts:
            papers_list.append({
                'id': att.id,
                'type': 'online',
                'student_name':  att.student.full_name if att.student else 'Inconnu',
                'student_email': att.student.email if att.student else 'Pas d\'email',
                'subject_title': att.exam.title if att.exam else 'Examen en ligne',
                'score': att.score,
                'corrected_at': (att.corrected_at or att.submitted_at).isoformat() if (att.corrected_at or att.submitted_at) else None,
                'email_sent': False,
                'exam_id': att.exam_id
            })

        # Tri global par date décroissante
        papers_list.sort(key=lambda x: x['corrected_at'] or '', reverse=True)

        session.close()
        return jsonify({'papers': papers_list})

    except Exception as e:
        print(f"❌ Erreur professor_corrected_papers: {e}")
        return jsonify({'error': str(e)}), 500

@exams_bp.route('/api/ai/generate-exam-suggestions', methods=['POST'])
@paseto_required
def generate_exam_suggestions():
    """Génère des suggestions de sujets d'examen à partir d'un cours uploadé"""
    current_user_id = get_current_user_id()
    session = get_session()
    
    user = session.query(User).filter_by(id=int(current_user_id)).first()
    
    if not user or user.role not in [UserRole.PROFESSOR, UserRole.ADMIN]:
        session.close()
        return jsonify({'success': False, 'error': 'Accès non autorisé'}), 403
    
    try:
        # ✅ NOUVELLE VERSION : Upload du fichier cours
        if 'course_file' not in request.files:
            session.close()
            return jsonify({'success': False, 'error': 'Fichier cours requis'}), 400
        
        file = request.files['course_file']
        
        if file.filename == '':
            session.close()
            return jsonify({'success': False, 'error': 'Aucun fichier sélectionné'}), 400
        
        if not allowed_file(file.filename):
            session.close()
            return jsonify({'success': False, 'error': 'Type de fichier non autorisé (PDF, DOCX, TXT uniquement)'}), 400
        
        # Sauvegarder temporairement le fichier
        filename = secure_filename(file.filename)
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        temp_filename = f"course_{timestamp}_{filename}"
        temp_filepath = os.path.join(_UPLOAD_FOLDER, temp_filename)
        file.save(temp_filepath)
        
        # Extraire le contenu du cours
        course_content = extract_text_from_file(temp_filepath)
        
        if not course_content or len(course_content.strip()) < 100:
            os.remove(temp_filepath)
            session.close()
            return jsonify({
                'success': False, 
                'error': 'Le contenu du cours est trop court ou illisible (minimum 100 caractères)'
            }), 400
        
        # Récupérer les paramètres additionnels du formulaire
        difficulty = request.form.get('difficulty', 'Moyen')
        student_level = request.form.get('student_level', 'Licence')
        exam_type = request.form.get('exam_type', '')
        # Types de questions choisis par l'utilisateur (QCM, Vrai/Faux, Questions ouvertes)
        question_types = request.form.get('question_types', '')

        # Construire la contrainte de type selon les choix de l'utilisateur
        if question_types:
            types_list = [t.strip() for t in question_types.split(',') if t.strip()]
            if len(types_list) == 1:
                forced_type_line = f"- Type d'examen OBLIGATOIRE : {types_list[0]}"
                suggested_exam_type_hint = f"({types_list[0]})"
            else:
                combined = ' + '.join(types_list)
                forced_type_line = f"- Types de questions OBLIGATOIRES à inclure : {combined} (examen mixte)"
                suggested_exam_type_hint = f"(Mixte : {combined})"
        elif exam_type:
            forced_type_line = f"- Type d'examen souhaité : {exam_type}"
            suggested_exam_type_hint = f"({exam_type})"
        else:
            forced_type_line = ""
            suggested_exam_type_hint = ""

        prompt = f"""Tu es un expert en pédagogie universitaire francophone, spécialiste dans TOUS les domaines académiques (sciences exactes, droit, médecine, lettres, sciences humaines, ingénierie, arts, langues, économie, agronomie, architecture, etc.).

CONTENU DU COURS UPLOADÉ :
{course_content[:8000]}
{"[... contenu tronqué ...]" if len(course_content) > 8000 else ""}

PARAMÈTRES :
- Niveau de difficulté : {difficulty}
- Niveau des étudiants : {student_level}
{forced_type_line}

ÉTAPE 1 — IDENTIFICATION DU DOMAINE :
Identifie d'abord silencieusement la discipline enseignée (ex: droit des obligations, biochimie, algèbre linéaire, histoire médiévale, architecture urbaine, littérature africaine, etc.) en lisant le contenu du cours.

ÉTAPE 2 — GÉNÉRATION DES SUGGESTIONS :
Génère 3 suggestions de sujets d'examen directement basées sur les concepts, théories et exercices présents dans ce cours.
{"IMPORTANT : Le type d'examen de TOUTES les suggestions doit respecter les types demandés : " + question_types if question_types else ""}

Pour chaque suggestion :
1. Un titre précis et disciplinaire
2. Une description détaillée (2-3 phrases) de ce qui sera évalué
3. Le type d'examen adapté à la discipline (QCM, Dissertation, Exercices, Étude de cas, Problème, Commentaire de texte, Calcul, TP, Oral, etc.)
4. La durée recommandée en minutes
5. 4-6 points clés extraits du cours
6. 3-5 exemples de questions concrètes issues du cours
7. Critères d'évaluation avec barème sur 20 points

Réponds UNIQUEMENT avec un JSON valide dans ce format exact (OBLIGATOIREMENT 3 suggestions) :
{{
    "course_summary": "Résumé de la discipline et du contenu en 2-3 phrases",
    "detected_domain": "Domaine détecté (ex: Droit civil, Biochimie, Mathématiques...)",
    "main_topics": ["Thème 1", "Thème 2", "Thème 3"],
    "suggestions": [
        {{
            "title": "Titre de la suggestion 1",
            "description": "Description détaillée de ce qui sera évalué (2-3 phrases)",
            "exam_type": "QCM,Questions ouvertes",
            "duration": 120,
            "difficulty": "{difficulty}",
            "key_points": ["Point clé 1", "Point clé 2", "Point clé 3"],
            "questions_examples": ["Exemple question 1", "Exemple question 2"],
            "grading_criteria": "Barème : Q1 (5pts) — ..., Q2 (8pts) — ..., Q3 (7pts) — ..."
        }},
        {{
            "title": "Titre de la suggestion 2",
            "description": "Description détaillée de ce qui sera évalué (2-3 phrases)",
            "exam_type": "Questions ouvertes",
            "duration": 90,
            "difficulty": "{difficulty}",
            "key_points": ["Point clé 1", "Point clé 2", "Point clé 3"],
            "questions_examples": ["Exemple question 1", "Exemple question 2"],
            "grading_criteria": "Barème : Q1 (5pts) — ..., Q2 (8pts) — ..., Q3 (7pts) — ..."
        }},
        {{
            "title": "Titre de la suggestion 3",
            "description": "Description détaillée de ce qui sera évalué (2-3 phrases)",
            "exam_type": "QCM,Vrai/Faux",
            "duration": 60,
            "difficulty": "{difficulty}",
            "key_points": ["Point clé 1", "Point clé 2", "Point clé 3"],
            "questions_examples": ["Exemple question 1", "Exemple question 2"],
            "grading_criteria": "Barème : Q1 (5pts) — ..., Q2 (8pts) — ..., Q3 (7pts) — ..."
        }}
    ]
}}
"""
        
        # ── Redis cache check (key = hash of course content + params) ──────────
        import json
        import re
        from cache import cache_get, cache_set, make_content_key

        cache_key = make_content_key(course_content[:4000], difficulty, student_level, question_types)
        cached    = cache_get(cache_key)
        if cached:
            os.remove(temp_filepath)
            session.close()
            return jsonify({**cached, 'from_cache': True})

        response_text = call_ai_simple(prompt)

        json_match = re.search(r'\{.*\}', response_text, re.DOTALL)
        if json_match:
            suggestions_data = json.loads(json_match.group())
            detected_domain = suggestions_data.get('detected_domain', '')

            enriched_suggestions = []
            for s in suggestions_data.get('suggestions', []):
                s['detected_domain'] = detected_domain
                s['student_level'] = student_level
                if question_types:
                    s['exam_type'] = question_types
                    s['question_types'] = question_types
                enriched_suggestions.append(s)

            payload = {
                'success': True,
                'course_summary': suggestions_data.get('course_summary', ''),
                'detected_domain': detected_domain,
                'main_topics': suggestions_data.get('main_topics', []),
                'suggestions': enriched_suggestions,
                'course_filename': filename,
            }
            cache_set(cache_key, payload, ttl=7200)   # cache 2 hours
            session.close()
            return jsonify(payload)
        else:
            os.remove(temp_filepath)
            session.close()
            return jsonify({'success': False, 'error': 'Format de réponse IA invalide'}), 500
            
    except Exception as e:
        print(f" Erreur génération suggestions: {e}")
        import traceback
        traceback.print_exc()
        if 'temp_filepath' in locals() and os.path.exists(temp_filepath):
            os.remove(temp_filepath)
        session.close()
        err_str = str(e)
        if 'credit balance' in err_str or 'too low' in err_str:
            user_msg = "Le service d'intelligence artificielle est temporairement indisponible. Veuillez contacter l'administrateur."
        elif 'rate_limit' in err_str or 'rate limit' in err_str.lower():
            user_msg = "Trop de requêtes simultanées. Veuillez patienter quelques secondes et réessayer."
        else:
            user_msg = "Une erreur est survenue lors de la génération. Veuillez réessayer."
        return jsonify({'success': False, 'error': user_msg}), 500

@exams_bp.route('/api/subjects/generate-full-exam', methods=['POST'])
@paseto_required
def generate_full_exam_from_suggestion():
    """Génère un sujet d'examen complet avec questions numérotées et barème (sans sauvegarder)"""
    user_id = get_current_user_id()
    session = get_session()
    user = session.query(User).filter_by(id=int(user_id)).first()
    session.close()

    if not user or user.role not in [UserRole.PROFESSOR, UserRole.ADMIN]:
        return jsonify({'error': 'Accès non autorisé'}), 403

    data = request.get_json()
    suggestion = data.get('suggestion', {})

    title = suggestion.get('title', 'Examen')
    exam_type = suggestion.get('exam_type', 'Examen écrit')
    difficulty = suggestion.get('difficulty', 'Moyen')
    duration = suggestion.get('duration', 120)
    description = suggestion.get('description', '')
    key_points = suggestion.get('key_points', [])
    student_level = suggestion.get('student_level', 'Licence 3')
    questions_examples = suggestion.get('questions_examples', [])

    key_points_str = '\n'.join(f'- {p}' for p in key_points)
    examples_str = '\n'.join(f'{i+1}. {q}' for i, q in enumerate(questions_examples)) if questions_examples else ''
    examples_section = f'- Exemples de questions de base:\n{examples_str}' if examples_str else ''

    detected_domain = suggestion.get('detected_domain', '')
    domain_line = f"- Domaine disciplinaire : {detected_domain}" if detected_domain else ""

    try:
        question_count = max(1, min(int(suggestion.get('question_count') or 20), 60))
    except (TypeError, ValueError):
        question_count = 20

    # Niveaux taxonomiques de Bloom (Retour #5) — ciblent la répartition cognitive
    # des questions générées ; le professeur les sélectionne dans le formulaire.
    _BLOOM_LABELS = {
        'connaissance':  'Connaissance (mémorisation, restitution de faits)',
        'comprehension': 'Compréhension (expliquer avec ses propres mots)',
        'application':   'Application (utiliser un concept dans une situation nouvelle)',
        'analyse':       'Analyse (décomposer, comparer, établir des relations)',
        'synthese':      'Synthèse (combiner des éléments pour créer quelque chose de nouveau)',
        'evaluation':    'Évaluation (juger, argumenter, critiquer selon des critères explicites)',
    }
    bloom_levels = [b for b in (suggestion.get('bloom_levels') or []) if b in _BLOOM_LABELS]
    bloom_line = ''
    if bloom_levels:
        bloom_line = ("- Niveaux taxonomiques de Bloom à cibler (répartir les questions entre ces niveaux) :\n"
                       + '\n'.join(f'  • {_BLOOM_LABELS[b]}' for b in bloom_levels))

    # Médias (image/audio/vidéo) joints par l'enseignant AVANT génération — déjà
    # analysés par l'IA (services.ai_service.analyze_media) au moment de l'upload.
    # On transmet l'analyse + la consigne de l'enseignant pour que l'IA sache
    # exactement comment exploiter le média dans une question, et lui demande
    # d'insérer elle-même le marqueur exact au bon endroit (Retour équipe DFIP —
    # remplace l'insertion manuelle post-génération dans l'aperçu).
    media_items = [m for m in (suggestion.get('media') or []) if isinstance(m, dict) and m.get('marker')]
    media_line = ''
    if media_items:
        media_desc = []
        for m in media_items:
            marker = str(m.get('marker', ''))[:120]
            analysis = str(m.get('analysis', ''))[:800]
            instr = str(m.get('instructions', '')).strip()
            entry = f"  • Marqueur {marker} — {analysis}"
            if instr:
                entry += f"\n    Consigne de l'enseignant : {instr}"
            media_desc.append(entry)
        media_line = ("- Médias fournis par l'enseignant à intégrer dans le sujet — pour chacun, rédige la "
                       "question la plus pertinente en te basant sur son analyse et la consigne, puis insère "
                       "le marqueur EXACT, seul sur sa propre ligne, juste après l'énoncé de cette question :\n"
                       + '\n'.join(media_desc))

    # Récupérer les types de questions choisis par l'utilisateur (prioritaire sur exam_type de l'IA)
    question_types = suggestion.get('question_types', '')
    if question_types:
        exam_type = question_types  # override avec le choix utilisateur

    # Détecter le(s) type(s) de questions demandés — non exclusifs, un examen
    # combine souvent plusieurs types (ex: QCM + Vrai/Faux + Ouvertes).
    exam_type_lower = exam_type.lower()
    has_qcm_multi   = any(k in exam_type_lower for k in ['réponses multiples', 'qcm multi', 'choix multiples multiples'])
    has_qcm         = (any(k in exam_type_lower for k in ['qcm', 'choix multiple', 'mcq']) and not has_qcm_multi)
    has_vf          = any(k in exam_type_lower for k in ['vrai', 'faux', 'vrai/faux', 'v/f'])
    has_appariement = any(k in exam_type_lower for k in ['appariement', 'matching', 'associat'])
    has_code        = any(k in exam_type_lower for k in ['code', 'programmation', 'algorithme'])
    has_open        = any(k in exam_type_lower for k in ['ouvert', 'open', 'développ', 'court', 'dissertation', 'synthèse', 'problème', 'cas', 'exercice', 'commentaire', 'calcul'])
    selected_count  = sum([has_qcm, has_qcm_multi, has_vf, has_appariement, has_code, has_open])
    is_mixed        = selected_count >= 2 or any(k in exam_type_lower for k in ['mixte', 'mix', 'combiné', 'partiel', ',', '+'])

    # ── Templates avec marqueurs de type explicites ──────────────────────────
    # Le marqueur [QCM], [VF], [OUVERT], [QCM_MULTI], [APPARIEMENT], [CODE]
    # est écrit dans le titre de chaque question. Le parser JavaScript le lit en
    # priorité → classification garantie côté frontend, sans heuristiques fragiles.

    # (titre_partie, marqueur, gabarit_question, règle_format)
    # 5ᵉ élément de chaque tuple : format EXACT exigé pour la ligne de barème de
    # ce type — condition nécessaire à la notation automatique déterministe
    # (comme Moodle qtype_multichoice/truefalse : rightanswer stocké et comparé
    # mécaniquement, sans IA). Pour l'Appariement, la bonne réponse est déjà
    # dans l'énoncé (colonne de droite) — le barème n'a donc besoin que d'un
    # critère générique, pas d'une clé de réponse séparée.
    _TEMPLATES = {
        'qcm': ("Questions à Choix Multiples (une seule bonne réponse)", 'QCM',
            """Question {n} — [Titre court] ............. (1 pt) [QCM]
[Énoncé de la question, clair et précis]
A) [Premier choix — description courte, max 15 mots]
B) [Deuxième choix — description courte, max 15 mots]
C) [Troisième choix — description courte, max 15 mots]
D) [Quatrième choix — description courte, max 15 mots]""",
            "chaque titre QCM se termine par [QCM] ; 4 choix A) B) C) D) courts (max 15 mots), jamais de verbes d'instruction (Définissez, Expliquez...) ; une seule bonne réponse",
            "  • Bonne réponse : X) — [justification courte]"),
        'qcm_multi': ("Questions à Choix Multiples (plusieurs bonnes réponses)", 'QCM_MULTI',
            """Question {n} — [Titre court] ............. (1 pt) [QCM_MULTI]
[Énoncé précisant qu'il peut y avoir plusieurs bonnes réponses]
A) [Choix court]
B) [Choix court]
C) [Choix court]
D) [Choix court]
E) [Choix court]""",
            "chaque titre se termine par [QCM_MULTI] ; 4 à 6 choix A) B) C)... ; AU MOINS 2 bonnes réponses par question",
            "  • Bonnes réponses : X), Y) — [justification courte]"),
        'vf': ("Vrai / Faux", 'VF',
            """Question {n} — [Affirmation à évaluer] ............. (1 pt) [VF]
Vrai / Faux""",
            'chaque titre se termine par [VF] ; la ligne suivante est UNIQUEMENT "Vrai / Faux" (rien d\'autre)',
            "  • Réponse : Vrai (ou Faux) — [justification courte]"),
        'appariement': ("Appariement", 'APPARIEMENT',
            """Question {n} — Associez chaque élément de gauche à sa correspondance ............. (2 pts) [APPARIEMENT]
A. [Terme ou élément 1] → [Définition/correspondance 1]
B. [Terme ou élément 2] → [Définition/correspondance 2]
C. [Terme ou élément 3] → [Définition/correspondance 3]
D. [Terme ou élément 4] → [Définition/correspondance 4]""",
            "chaque titre se termine par [APPARIEMENT] ; 4 à 6 paires \"A. Gauche → Droite\" (flèche → obligatoire, un seul \"→\" par ligne)",
            "  • Crédit proportionnel au nombre de paires correctes (la bonne réponse figure déjà dans l'énoncé)"),
        'code': ("Maths et Programmation", 'CODE',
            """Question {n} — [Énoncé de l'exercice de calcul/algorithme] ............. (X pts) [CODE]
[Énoncé complet — formule à démontrer, algorithme à écrire ou problème à résoudre pas à pas]""",
            "chaque titre se termine par [CODE] ; énoncés d'exercices mathématiques ou de programmation nécessitant une réponse structurée (formules, pseudo-code)",
            "  • Critère : Z pts — [Ce qui est attendu]"),
        'open': ("Questions Ouvertes", 'OUVERT',
            """Question {n} — [Titre court] ............. (X pts) [OUVERT]
[Énoncé complet, précis et détaillé]""",
            "chaque titre se termine par [OUVERT] ; énoncés complets et détaillés",
            "  • Critère : Z pts — [Ce qui est attendu]"),
    }
    _selected = [k for k, sel in (('qcm', has_qcm), ('qcm_multi', has_qcm_multi), ('vf', has_vf),
                                   ('appariement', has_appariement), ('code', has_code),
                                   ('open', has_open)) if sel]
    if not _selected:
        _selected = ['open']  # comportement historique par défaut

    if not is_mixed and len(_selected) == 1:
        _title, _marker, _tpl, _rule, _rubric_rule = _TEMPLATES[_selected[0]]
        questions_format = "\n\n".join(_tpl.format(n=i) for i in (1, 2)) + f"\n\n[Continuer ainsi jusqu'à {question_count} questions au total, selon durée et difficulté. Total des points = 20 pts]"
        format_rules = f"- OBLIGATOIRE : {_rule}\n- EXACTEMENT {question_count} questions au total, numérotées Question 1 à Question {question_count} × points répartis pour totaliser 20 pts"
        rubric_example = f"Question 1 — [Titre] (X pts)\n{_rubric_rule}"
        rubric_format_rules = f"- OBLIGATOIRE, pour CHAQUE question du barème : {_rubric_rule.strip()}"
    else:
        pts_per_part = max(1, 20 // len(_selected))
        q_per_part = max(1, question_count // len(_selected))
        sections = []
        n_start = 1
        for k in _selected:
            _title, _marker, _tpl, _rule, _rubric_rule = _TEMPLATES[k]
            sections.append(
                f"Partie — {_title} ({pts_per_part} pts, ~{q_per_part} questions)\n\n" +
                "\n\n".join(_tpl.format(n=i) for i in (n_start, n_start+1)) +
                "\n\n[... continuer cette partie selon durée/difficulté ...]"
            )
            n_start += 2
        questions_format = "\n\n".join(sections) + f"\n\n[Numérotation continue d'une partie à l'autre. EXACTEMENT {question_count} questions au total. Total de toutes les parties = 20 pts]"
        format_rules = "\n".join(f"- Partie {_TEMPLATES[k][0]} : {_TEMPLATES[k][3]}" for k in _selected) + f"\n- EXACTEMENT {question_count} questions au total (toutes parties confondues)\n- Total toutes parties confondues = 20 pts"
        rubric_example = "\n\n".join(f"Question {i} — [Titre] (X pts)  ({_TEMPLATES[k][0]})\n{_TEMPLATES[k][4]}" for i, k in enumerate(_selected, start=1))
        rubric_format_rules = "\n".join(f"- Pour toute question de type {_TEMPLATES[k][0]} : {_TEMPLATES[k][4].strip()}" for k in _selected)

    prompt = f"""Tu es un expert en création d'examens universitaires francophones, compétent dans TOUS les domaines académiques (sciences, droit, médecine, lettres, arts, ingénierie, langues, économie, histoire, philosophie, agronomie, architecture, etc.).

Crée un sujet d'examen COMPLET et DÉTAILLÉ avec ces informations :
- Titre : {title}
- Type : {exam_type}
- Niveau : {student_level}
- Difficulté : {difficulty}
- Durée : {duration} minutes
- Description : {description}
{domain_line}
{bloom_line}
{media_line}
- Thèmes à couvrir :
{key_points_str}
{examples_section}

GÉNÈRE le sujet en respectant EXACTEMENT ce format (NE DÉVIE JAMAIS de ce format) :

══════════════════════════════════════
{title.upper()}
══════════════════════════════════════
Type d'examen : {exam_type}
Niveau : {student_level} | Difficulté : {difficulty}
Durée : {duration} minutes | Note totale : 20 points
══════════════════════════════════════

INSTRUCTIONS AUX ÉTUDIANTS
──────────────────────────
[2-3 phrases d'instructions claires et précises adaptées au type d'examen]

══════════════════════════════════════
QUESTIONS
══════════════════════════════════════

{questions_format}

══════════════════════════════════════
BARÈME DE NOTATION
══════════════════════════════════════

{rubric_example}

[Un critère par question — respecte EXACTEMENT le format demandé ci-dessous selon le type de chaque question]

──────────────────────────
TOTAL : 20 / 20 points
══════════════════════════════════════

Règles ABSOLUES à respecter :
{format_rules}
- Langage académique et rigoureux en français
- Questions adaptées au niveau {student_level} et à {duration} minutes de composition{"" if not media_items else chr(10) + "- Chaque marqueur média listé ci-dessus DOIT apparaître EXACTEMENT UNE FOIS, tel quel, seul sur sa ligne, juste après l'énoncé de la question qui l'exploite"}

Règles ABSOLUES pour le BARÈME (notation automatique sans IA pour QCM/Vrai-Faux/Appariement — la bonne réponse DOIT être écrite exactement dans ce format pour être reconnue) :
{rubric_format_rules}"""

    try:
        # call_ai_simple() plafonne à 4000 tokens de sortie — largement
        # insuffisant dès qu'on demande beaucoup de questions détaillées
        # (contenu + barème par question) : au-delà, la génération est
        # tronquée en plein milieu et les instructions de gabarit destinées
        # à l'IA (ex. "[... continuer cette partie ...]") se retrouvent
        # recopiées telles quelles dans le sujet, à la place de vraies
        # questions — constaté en conditions réelles (30 questions demandées,
        # 24 obtenues + texte d'instruction laissé tel quel). On calcule donc
        # une limite proportionnelle au nombre de questions demandées.
        _max_tokens = min(16000, 2500 + question_count * 300)
        full_exam_text = call_claude("", prompt, temperature=0.2, max_tokens=_max_tokens)

        # Séparer contenu et barème
        bareme_markers = ['BARÈME DE NOTATION', 'BAREME DE NOTATION', 'BARÈME', 'Barème']
        rubric_start = -1
        for marker in bareme_markers:
            idx = full_exam_text.find(marker)
            if idx != -1:
                # Remonter jusqu'à la ligne de séparation
                line_start = full_exam_text.rfind('\n', 0, idx)
                rubric_start = line_start if line_start != -1 else idx
                break

        if rubric_start != -1:
            content = full_exam_text[:rubric_start].strip()
            rubric = full_exam_text[rubric_start:].strip()
        else:
            content = full_exam_text
            rubric = full_exam_text

        # Filet de sécurité : si l'IA recopie quand même une instruction de
        # gabarit au lieu de générer une vraie question (ex. "[... continuer
        # cette partie ...]", "[Numérotation continue ... EXACTEMENT N
        # questions ...]"), la retirer plutôt que de l'afficher au professeur
        # comme si c'était une question.
        _TEMPLATE_LEAK_RE = re.compile(r'^\[(?:\.\.\.|Continuer|Numérotation|Un critère).*\]\s*$', re.M | re.I)
        content = _TEMPLATE_LEAK_RE.sub('', content).strip()
        rubric  = _TEMPLATE_LEAK_RE.sub('', rubric).strip()

        # Retour #10 — vérifier les doublons AVANT validation : questions du lot
        # généré qui se ressemblent entre elles à ≥95% (même pattern que
        # generate_more_questions, qui compare contre un sujet déjà existant).
        q_texts = re.findall(r'Question\s+\d{1,3}\s*[—\-–:.].*?(?=\nQuestion\s+\d{1,3}\s*[—\-–:.]|\Z)', content, re.S)
        duplicates = []
        for i in range(len(q_texts)):
            for j in range(i + 1, len(q_texts)):
                sim = _similarity(q_texts[i][:300], q_texts[j][:300])
                if sim >= DUPLICATE_THRESHOLD:
                    duplicates.append({'similarity': round(sim * 100, 1)})
                    break

        return jsonify({
            'success': True,
            'title': title,
            'content': content,
            'rubric': rubric,
            'full_text': full_exam_text,
            'duplicates': duplicates,
        })

    except Exception as e:
        import traceback
        traceback.print_exc()
        err_str = str(e)
        if 'credit balance' in err_str or 'too low' in err_str:
            user_msg = "Le service d'intelligence artificielle est temporairement indisponible. Veuillez contacter l'administrateur."
        elif 'rate_limit' in err_str or 'rate limit' in err_str.lower():
            user_msg = "Trop de requêtes simultanées. Veuillez patienter quelques secondes et réessayer."
        else:
            user_msg = "Une erreur est survenue lors de la génération. Veuillez réessayer."
        return jsonify({'error': user_msg}), 500


def _patch_question_points(text, points_map):
    """Remplace le nombre de points de chaque question numérotée dans `text`
    (contenu OU barème) selon `points_map` ({numéro: points}) — utilisé pour
    redistribuer sur 20 points après ajout de nouvelles questions.

    Traite ligne par ligne en suivant la question en cours : remplace TOUTES
    les mentions "X pts" tant qu'on reste dans le bloc de cette question
    (titre ET ligne de critère du barème, un seul critère par question dans
    nos gabarits) — sans ça, le titre affichait le nouveau total mais le
    critère gardait l'ancien. S'arrête explicitement à la ligne TOTAL pour ne
    jamais toucher au total général (toujours 20)."""
    current_num = None
    out_lines = []
    for line in text.split('\n'):
        m = re.match(r'\s*Question\s+(\d{1,3})\s*[—\-–:.]', line)
        if m:
            current_num = int(m.group(1))
        if re.search(r'TOTAL\s*:', line, re.I):
            current_num = None
        if current_num is not None and current_num in points_map:
            pts = points_map[current_num]
            pts_str = str(int(pts)) if float(pts).is_integer() else f'{pts:.1f}'
            line = re.sub(r'\d+(?:\.\d+)?(\s*pts?\b)', lambda mm, s=pts_str: f'{s}{mm.group(1)}', line)
        out_lines.append(line)
    return '\n'.join(out_lines)


@exams_bp.route('/api/subjects/generate-more-questions', methods=['POST'])
@paseto_required
def generate_more_questions():
    """Génère N questions supplémentaires d'un type donné à AJOUTER à un sujet
    déjà généré (sans le remplacer), en évitant de dupliquer les thèmes déjà
    couverts. Redistribue les points sur 20 au total (anciennes + nouvelles
    questions) et étend le barème avec une entrée par nouvelle question —
    Retour : "le barème est toujours à 20/20 points, il devrait appliquer les
    nouvelles questions"."""
    user_id = get_current_user_id()
    session = get_session()
    user = session.query(User).filter_by(id=int(user_id)).first()
    session.close()

    if not user or user.role not in [UserRole.PROFESSOR, UserRole.ADMIN]:
        return jsonify({'error': 'Accès non autorisé'}), 403

    data = request.get_json() or {}
    existing_content = (data.get('existing_content') or '').strip()
    existing_rubric  = (data.get('existing_rubric') or '').strip()
    if not existing_content:
        return jsonify({'error': 'Contenu existant requis'}), 400

    count          = max(1, min(int(data.get('count', 3)), 10))
    question_type  = (data.get('question_type') or 'QCM').strip()
    title          = data.get('title', 'Examen')
    student_level  = data.get('student_level', 'Licence 3')
    difficulty     = data.get('difficulty', 'Moyen')

    _MARKER_BY_LABEL = {
        'qcm': 'QCM', 'qcu': 'QCM', 'qcm multiple': 'QCM_MULTI', 'qcm (réponses multiples)': 'QCM_MULTI',
        'vrai/faux': 'VF', 'vrai / faux': 'VF', 'appariement': 'APPARIEMENT',
        'maths et programmation': 'CODE', 'maths / programmation': 'CODE', 'code': 'CODE',
        'questions ouvertes': 'OUVERT', 'ouvert': 'OUVERT',
    }
    marker = _MARKER_BY_LABEL.get(question_type.lower(), 'QCM')

    # Continuer la numérotation après la dernière question existante
    existing_numbers = [int(n) for n in re.findall(r'Question\s+(\d{1,3})\s*[—\-–:.]', existing_content)]
    next_num = (max(existing_numbers) + 1) if existing_numbers else 1

    prompt = f"""Tu es un expert en création d'examens universitaires francophones.

Voici un sujet d'examen déjà généré (titre : {title}, niveau {student_level}, difficulté {difficulty}) :

--- DÉBUT SUJET EXISTANT ---
{existing_content[:6000]}
--- FIN SUJET EXISTANT ---

Génère EXACTEMENT {count} NOUVELLES questions de type [{marker}] à AJOUTER à ce sujet.

RÈGLES ABSOLUES :
- Numérote-les en continuant à partir de {next_num} (Question {next_num}, Question {next_num + 1}, ...)
- Ces nouvelles questions doivent couvrir des thèmes ou aspects DIFFÉRENTS de ceux déjà présents dans le sujet existant ci-dessus — aucune reformulation ni répétition d'une question déjà posée
- Chaque titre de question se termine par [{marker}]
- Respecte STRICTEMENT le même format que les questions [{marker}] déjà visibles dans le sujet existant (nombre de choix, structure des paires, etc.)
- Réponds UNIQUEMENT avec les {count} nouvelles questions, rien d'autre (pas de titre de section, pas de commentaire, pas de barème)"""

    try:
        new_questions_text = call_ai_simple(prompt).strip()

        # Vérifier les doublons contre les questions déjà présentes dans le sujet
        existing_q_texts = re.findall(r'Question\s+\d{1,3}\s*[—\-–:.].*?(?=\nQuestion\s+\d{1,3}\s*[—\-–:.]|\Z)', existing_content, re.S)
        new_q_texts = re.findall(r'Question\s+\d{1,3}\s*[—\-–:.].*?(?=\nQuestion\s+\d{1,3}\s*[—\-–:.]|\Z)', new_questions_text, re.S)
        duplicates = []
        for nq in new_q_texts:
            for eq in existing_q_texts:
                sim = _similarity(nq[:300], eq[:300])
                if sim >= DUPLICATE_THRESHOLD:
                    duplicates.append({'similarity': round(sim * 100, 1)})
                    break

        # ── Redistribution des points sur 20 au total (anciennes + nouvelles
        # questions) et extension du barème — sans ça, les nouvelles questions
        # n'ont ni point ni critère, et le total restait figé sur l'ancien
        # découpage.
        full_content = existing_content
        full_rubric  = existing_rubric
        existing_nums = sorted(set(int(n) for n in re.findall(r'Question\s+(\d{1,3})\s*[—\-–:.]', existing_content)))
        new_nums      = sorted(set(int(n) for n in re.findall(r'Question\s+(\d{1,3})\s*[—\-–:.]', new_questions_text)))
        all_nums = sorted(set(existing_nums) | set(new_nums))
        if all_nums:
            total = len(all_nums)
            base_pts  = 20 // total
            remainder = 20 - base_pts * total
            points_map = {}
            for i, num in enumerate(all_nums):
                points_map[num] = base_pts + (1 if i < remainder else 0)

            full_content = _patch_question_points(f'{existing_content}\n\n{new_questions_text}', points_map)

            if existing_rubric:
                full_rubric = _patch_question_points(existing_rubric, points_map)
                new_titles = dict(re.findall(r'Question\s+(\d{1,3})\s*[—\-–:.]\s*(.+?)\s*\.{3,}', new_questions_text))
                new_rubric_entries = []
                for num in new_nums:
                    pts = points_map.get(num, base_pts)
                    pts_str = str(int(pts)) if float(pts).is_integer() else f'{pts:.1f}'
                    ttl = new_titles.get(str(num), '').strip() or f'Question {num}'
                    new_rubric_entries.append(
                        f'Question {num} — {ttl} ({pts_str} pt{"s" if pts != 1 else ""})\n'
                        f'  • Réponse attendue : {pts_str} pt{"s" if pts != 1 else ""}\n'
                    )
                addendum = '\n'.join(new_rubric_entries)
                # Insérer avant la ligne de séparation qui précède le TOTAL,
                # sinon ajouter simplement à la fin (format non reconnu).
                total_marker = re.search(r'\n─+\nTOTAL\s*:', full_rubric)
                if total_marker:
                    idx = total_marker.start()
                    full_rubric = f'{full_rubric[:idx].rstrip()}\n\n{addendum}\n{full_rubric[idx:].lstrip(chr(10))}'
                else:
                    full_rubric = f'{full_rubric}\n\n{addendum}'

        return jsonify({
            'success': True,
            'new_content': new_questions_text,
            'full_content': full_content,
            'full_rubric': full_rubric,
            'count_generated': len(new_q_texts),
            'duplicates': duplicates,
        })
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'error': 'Erreur lors de la génération des questions supplémentaires'}), 500


@exams_bp.route('/api/subjects/suggest-question-count', methods=['POST'])
@paseto_required
def suggest_question_count():
    """Retour équipe DFIP — l'IA suggère un nombre de questions adapté à la
    durée/difficulté/niveau, au lieu de laisser le professeur deviner."""
    user_id = get_current_user_id()
    session = get_session()
    user = session.query(User).filter_by(id=int(user_id)).first()
    session.close()
    if not user or user.role not in [UserRole.PROFESSOR, UserRole.ADMIN]:
        return jsonify({'error': 'Accès non autorisé'}), 403

    data = request.get_json(silent=True) or {}
    duration = int(data.get('duration') or 60)
    difficulty = data.get('difficulty') or 'Moyen'
    student_level = data.get('student_level') or 'Licence 3'
    question_types = data.get('question_types') or 'mixte'

    prompt = f"""Tu es un expert en ingénierie pédagogique universitaire.
Un examen dure {duration} minutes, niveau {student_level}, difficulté {difficulty}, types de questions : {question_types}.
En te basant sur le temps de réponse réaliste par question selon son type et la difficulté (une question ouverte prend bien plus de temps qu'un QCM), indique UNIQUEMENT le nombre de questions recommandé pour occuper la durée de l'examen sans que les étudiants ne soient pressés ni n'aient trop de temps libre.
Réponds STRICTEMENT avec un nombre entier seul, rien d'autre (pas de phrase, pas d'unité)."""

    try:
        raw = call_ai_simple(prompt).strip()
        m = re.search(r'\d+', raw)
        suggested = int(m.group()) if m else max(1, duration // 5)
        suggested = max(1, min(suggested, 60))
        return jsonify({'success': True, 'suggested_count': suggested})
    except Exception as e:
        # Repli heuristique si l'IA est indisponible — ne bloque jamais l'UI
        fallback = max(1, min(duration // 5, 60))
        return jsonify({'success': True, 'suggested_count': fallback, 'fallback': True})


_BANK_TYPE_QUESTION = ('qcm', 'qcm_multi', 'vf', 'appariement', 'open', 'subopen', 'code')
# Correspondance directe — auparavant 'qcm_multi'/'appariement'/'code' n'y
# figuraient pas et retombaient silencieusement sur 'open' via le .get(...,
# 'open') plus bas, perdant leur étiquette d'origine alors que le frontend
# (admin/questions TYPE_LABEL) sait déjà tous les afficher distinctement.
_BANK_TYPE_MAP = {t: t for t in _BANK_TYPE_QUESTION}


def _enrich_question_bank_from_subject(session, subject, user_id: int) -> int:
    """Retour équipe DFIP — ajoute automatiquement les questions d'un sujet
    validé à la banque de questions (enrichissement sans action manuelle).
    Ignore les questions déjà ≥95% similaires à une entrée existante — évite
    de flooder la banque avec des variantes quasi identiques."""
    blocks = [b for b in _parse_subject_blocks_ordered(subject.content or '') if b.get('type') in _BANK_TYPE_QUESTION]
    if not blocks:
        return 0
    existing_contents = [c for (c,) in session.query(QuestionBank.content).all()]
    added = 0
    for b in blocks:
        lines = [b.get('text') or '']
        lines.extend(b.get('extraLines') or [])
        if b.get('choices'):
            lines.extend(f"{c['letter']}) {c['text']}" for c in b['choices'])
        if b.get('pairs'):
            lines.extend(f"{chr(65 + i)}. {p['left']} → {p['right']}" for i, p in enumerate(b['pairs']))
        content = '\n'.join(l for l in lines if l).strip()
        if not content:
            continue
        if any(_similarity(content, ex) >= DUPLICATE_THRESHOLD for ex in existing_contents):
            continue
        session.add(QuestionBank(
            title=(b.get('text') or f"Question {b.get('num')}")[:80],
            content=content,
            question_type=_BANK_TYPE_MAP.get(b['type'], 'open'),
            ec_id=subject.ec_id,
            created_by_id=user_id,
        ))
        existing_contents.append(content)  # évite d'ajouter 2 quasi-doublons du même sujet
        added += 1
    return added


@exams_bp.route('/api/subjects/create-from-suggestion', methods=['POST'])
@paseto_required
def create_subject_from_suggestion():
    """Crée un sujet à partir d'une suggestion IA"""
    import traceback as _tb
    current_user_id = get_current_user_id()
    session = get_session()

    user = session.query(User).filter_by(id=int(current_user_id)).first()
    if not user or user.role not in [UserRole.PROFESSOR, UserRole.ADMIN]:
        session.close()
        return jsonify({'success': False, 'error': 'Accès non autorisé'}), 403

    data = request.get_json(silent=True)
    if not data:
        session.close()
        return jsonify({'success': False, 'error': 'Corps JSON invalide ou manquant'}), 400

    title   = data.get('title', '').strip()
    content = data.get('content', '').strip()
    if not title or not content:
        session.close()
        return jsonify({'success': False, 'error': 'Champs title et content obligatoires'}), 400

    try:
        # Utiliser le barème fourni — NE PAS appeler l'IA ici (déjà fait lors de la génération)
        rubric = data.get('rubric_override') or None

        new_subject = Subject(
            title=title,
            content=content,
            rubric=rubric,
            creator_id=int(current_user_id),
            ec_id=data.get('ec_id') or None,
            is_active=True
        )

        session.add(new_subject)
        session.commit()

        # Associer les médias (images/audio) uploadés pendant la composition,
        # avant que le sujet n'existe encore (Notes points 2/15)
        link_key = data.get('media_link_key')
        if link_key:
            session.query(SubjectMedia).filter_by(link_key=link_key, subject_id=None).update(
                {'subject_id': new_subject.id}
            )
            session.commit()

        # Enrichissement automatique de la banque de questions à partir du
        # sujet validé — chaque question détectée est ajoutée à la banque
        # (sauf si déjà ≥95% similaire à une question existante).
        try:
            _enrich_question_bank_from_subject(session, new_subject, int(current_user_id))
            session.commit()
        except Exception as _enrich_err:
            print(f"⚠️ Enrichissement banque de questions échoué (non bloquant) : {_enrich_err}")
            session.rollback()

        subject_id      = new_subject.id
        subject_title   = new_subject.title
        subject_content = new_subject.content
        subject_rubric  = new_subject.rubric
        subject_created = new_subject.created_at.isoformat() if new_subject.created_at else None
        session.close()

        return jsonify({
            'success': True,
            'subject': {
                'id':         subject_id,
                'title':      subject_title,
                'content':    subject_content,
                'rubric':     subject_rubric,
                'created_at': subject_created,
            }
        })

    except Exception as e:
        session.rollback()
        session.close()
        print(f"❌ Erreur création sujet from suggestion: {e}")
        _tb.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@exams_bp.route('/api/subjects/upload_media', methods=['POST'])
@paseto_required
def upload_subject_media_route():
    """Upload d'une image/audio/vidéo à insérer dans un sujet, analysée par
    l'IA selon la consigne de l'enseignant (Retour équipe DFIP) pour qu'elle
    sache comment l'exploiter dans les questions générées. Utilisable AVANT
    la sauvegarde finale du sujet via link_key (ex: uuid généré côté client)
    — associé au sujet définitif via media_link_key lors de l'appel à
    create-from-suggestion. Si subject_id est fourni (sujet déjà sauvegardé),
    l'association est immédiate."""
    try:
        user_id = get_current_user_id()
        session = get_session()
        user = session.query(User).filter_by(id=user_id).first()
        if not user or user.role not in [UserRole.PROFESSOR, UserRole.ADMIN]:
            session.close(); return jsonify({'error': 'Accès non autorisé'}), 403

        media_type = (request.form.get('media_type') or '').strip()
        if media_type not in ('image', 'audio', 'video'):
            session.close(); return jsonify({'error': "media_type doit être 'image', 'audio' ou 'video'"}), 400

        link_key     = request.form.get('link_key') or None
        instructions = (request.form.get('instructions') or '').strip()
        subject_id = request.form.get('subject_id')
        subject_id = int(subject_id) if subject_id else None
        if not link_key and not subject_id:
            session.close(); return jsonify({'error': 'link_key ou subject_id requis'}), 400

        if 'file' not in request.files:
            session.close(); return jsonify({'error': 'Aucun fichier fourni'}), 400
        f = request.files['file']
        if not f.filename:
            session.close(); return jsonify({'error': 'Nom de fichier vide'}), 400

        raw = f.read()
        max_size = 80 * 1024 * 1024 if media_type == 'video' else 25 * 1024 * 1024
        if len(raw) > max_size:
            session.close(); return jsonify({'error': f'Fichier trop volumineux ({max_size // (1024*1024)} Mo max)'}), 400

        key = upload_subject_media(link_key or f'subject_{subject_id}', media_type, f.filename, raw, f.content_type or 'application/octet-stream')
        if not key:
            ext_hint = {'image': 'jpg, png, webp, gif', 'audio': 'mp3, wav, ogg, m4a', 'video': 'mp4, webm'}[media_type]
            session.close(); return jsonify({'error': f'Type de fichier non autorisé pour {media_type} ({ext_hint})'}), 400

        safe_name = ''.join(c for c in f.filename if c.isalnum() or c in '._-') or f.filename
        analysis = analyze_media(media_type, raw, safe_name, f.content_type or '', instructions)
        media = SubjectMedia(subject_id=subject_id, link_key=link_key, media_type=media_type,
                              filename=safe_name, s3_key=key, uploaded_by_id=user_id,
                              instructions=instructions or None, ai_analysis=analysis)
        session.add(media)
        session.commit()
        result = media.to_dict()
        _marker_kind = {'image': 'IMAGE', 'audio': 'AUDIO', 'video': 'VIDEO'}[media_type]
        result['marker'] = f"[{_marker_kind}:{safe_name}]"
        session.close()
        return jsonify({'success': True, 'media': result}), 201
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@exams_bp.route('/api/subjects/<int:subject_id>/media', methods=['GET'])
@paseto_required
def get_subject_media(subject_id):
    """Liste les médias d'un sujet avec URL d'accès résolue — utilisé par la
    page d'examen pour afficher/lire les [IMAGE:...]/[AUDIO:...] du sujet."""
    try:
        session = get_session()
        rows = session.query(SubjectMedia).filter_by(subject_id=subject_id).all()
        result = []
        for m in rows:
            d = m.to_dict()
            d['url'] = get_snapshot_url(m.s3_key)
            result.append(d)
        session.close()
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@exams_bp.route('/api/admin/security_report', methods=['GET'])
@paseto_required
def admin_security_report():
    """Rapport de sécurité — incidents d'examens en ligne (admin/prof)."""
    user_id = get_current_user_id()
    session = get_session()
    try:
        user = session.query(User).filter_by(id=user_id).first()
        if not user or user.role not in [UserRole.ADMIN, UserRole.PROFESSOR]:
            session.close()
            return jsonify({'error': 'Accès non autorisé'}), 403

        from sqlalchemy import func as sqlfunc

        # Restriction prof : seulement ses propres examens
        prof_exam_ids = None
        if user.role == UserRole.PROFESSOR:
            prof_exam_ids = [
                e.id for e in session.query(OnlineExam).filter_by(created_by_id=user_id).all()
            ]
            if not prof_exam_ids:
                session.close()
                return jsonify({'event_summary': [], 'high_risk': [], 'banned_count': 0})

        # Filtre par examen — le rapport peut être global ou scopé à un seul
        # examen (sélecteur côté page Rapport de sécurité)
        exam_id_filter = request.args.get('exam_id', type=int)
        if exam_id_filter:
            if prof_exam_ids is not None and exam_id_filter not in prof_exam_ids:
                session.close()
                return jsonify({'error': 'Accès non autorisé à cet examen'}), 403
            prof_exam_ids = [exam_id_filter]

        # Top événements (filtrés si prof)
        log_query = session.query(
            ExamActivityLog.event_type,
            sqlfunc.count(ExamActivityLog.id).label('cnt')
        )
        if prof_exam_ids is not None:
            prof_attempt_ids = [
                a.id for a in session.query(ExamAttempt).filter(
                    ExamAttempt.exam_id.in_(prof_exam_ids)
                ).all()
            ]
            log_query = log_query.filter(ExamActivityLog.attempt_id.in_(prof_attempt_ids or [0]))
        event_counts = log_query.group_by(ExamActivityLog.event_type).order_by(
            sqlfunc.count(ExamActivityLog.id).desc()
        ).all()

        # Tentatives à haut risque (filtrées si prof)
        risky_q = session.query(ExamAttempt).options(
            joinedload(ExamAttempt.student),
            joinedload(ExamAttempt.exam)
        ).filter(ExamAttempt.risk_score >= 70)
        if prof_exam_ids is not None:
            risky_q = risky_q.filter(ExamAttempt.exam_id.in_(prof_exam_ids))
        risky = risky_q.order_by(ExamAttempt.risk_score.desc()).limit(20).all()

        risky_list = [{
            'attempt_id':     a.id,
            'student_name':   a.student.full_name if a.student else '—',
            'exam_title':     a.exam.title if a.exam else '—',
            'risk_score':     a.risk_score,
            'warnings_count': a.warnings_count,
            'tab_switches':   a.tab_switches,
            'no_face_count':  a.no_face_count or 0,
            'status':         a.status.value,
            'banned_at':      a.banned_at.isoformat() if a.banned_at else None,
            'ban_reason':     a.ban_reason
        } for a in risky]

        # Tentatives bannies (filtrées si prof)
        banned_q = session.query(ExamAttempt).filter(ExamAttempt.status == AttemptStatus.BANNED)
        if prof_exam_ids is not None:
            banned_q = banned_q.filter(ExamAttempt.exam_id.in_(prof_exam_ids))
        banned_count = banned_q.count()

        exam_title_filter = None
        if exam_id_filter:
            exam_row = session.query(OnlineExam).filter_by(id=exam_id_filter).first()
            exam_title_filter = exam_row.title if exam_row else None

        session.close()
        return jsonify({
            'event_summary':  [{'event': e, 'count': c} for e, c in event_counts],
            'high_risk':      risky_list,
            'banned_count':   banned_count,
            'exam_id':        exam_id_filter,
            'exam_title':     exam_title_filter,
        })
    except Exception as e:
        try: session.rollback(); session.close()
        except: pass
        return jsonify({'error': str(e)}), 500


@exams_bp.route('/api/exam_attempts/<int:attempt_id>/extra-time', methods=['PUT'])
@paseto_required
def grant_extra_time(attempt_id):
    """Accorded des minutes supplémentaires à un étudiant pendant l'examen."""
    try:
        user_id = get_current_user_id()
        session = get_session()
        role = get_current_user_role()
        if role not in ['professor', 'admin', 'surveillant']:
            session.close()
            return jsonify({'error': 'Accès non autorisé'}), 403
        attempt = session.query(ExamAttempt).filter_by(id=attempt_id).first()
        if not attempt:
            session.close()
            return jsonify({'error': 'Tentative non trouvée'}), 404
        # Refuser si l'étudiant a déjà terminé
        if attempt.status != AttemptStatus.IN_PROGRESS:
            session.close()
            return jsonify({'error': 'L\'étudiant a déjà terminé ou été exclu — impossible d\'accorder du temps'}), 400
        # Refuser si l'examen est clôturé
        exam = session.query(OnlineExam).filter_by(id=attempt.exam_id).first()
        if exam and exam.status != ExamStatus.ACTIVE:
            session.close()
            return jsonify({'error': 'L\'examen est clôturé — impossible d\'accorder du temps'}), 400
        data = request.get_json(silent=True) or {}
        minutes = int(data.get('minutes', 0))
        if not (1 <= minutes <= 60):
            session.close()
            return jsonify({'error': 'Valeur entre 1 et 60 minutes'}), 400
        prev = attempt.extra_minutes or 0
        attempt.extra_minutes = prev + minutes
        session.commit()
        total = attempt.extra_minutes  # rechargé automatiquement (session ouverte)
        session.close()
        print(f"⏱ Temps +{minutes}min accordé (tentative {attempt_id}), total extra: {total}min")
        return jsonify({'success': True, 'total_extra': total, 'added': minutes})
    except Exception as e:
        print(f"❌ grant_extra_time {attempt_id}: {e}")
        try: session.close()
        except: pass
        return jsonify({'error': str(e)}), 500


# ============================================================================
# NOTES DE SURVEILLANT SUR UN ÉTUDIANT
# ============================================================================

@exams_bp.route('/api/exam_attempts/<int:attempt_id>/proctor-note', methods=['POST'])
@paseto_required
def add_proctor_note(attempt_id):
    """Ajoute une note textuelle du surveillant/prof sur une tentative (stockée en activity_log)."""
    try:
        user_id = get_current_user_id()
        session = get_session()
        role = get_current_user_role()
        if role not in ['professor', 'admin', 'surveillant']:
            session.close()
            return jsonify({'error': 'Accès non autorisé'}), 403
        attempt = session.query(ExamAttempt).filter_by(id=attempt_id).first()
        if not attempt:
            session.close()
            return jsonify({'error': 'Tentative non trouvée'}), 404
        data = request.get_json(silent=True) or {}
        note = (data.get('note') or '').strip()
        if not note:
            session.close()
            return jsonify({'error': 'Note vide'}), 400
        author = session.query(User).filter_by(id=user_id).first()
        author_name = author.full_name if author else f'User#{user_id}'
        log = ExamActivityLog(
            attempt_id  = attempt_id,
            event_type  = 'proctor_note',
            event_data  = json.dumps({'note': note, 'author': author_name, 'author_id': user_id}, ensure_ascii=False),
            timestamp   = utcnow(),
        )
        session.add(log)
        session.commit()
        session.close()
        return jsonify({'success': True, 'note': note, 'author': author_name})
    except Exception as e:
        print(f"❌ add_proctor_note {attempt_id}: {e}")
        try: session.close()
        except: pass
        return jsonify({'error': str(e)}), 500


@exams_bp.route('/api/exam_attempts/<int:attempt_id>/proctor-notes', methods=['GET'])
@paseto_required
def get_proctor_notes(attempt_id):
    """Liste toutes les notes de surveillance d'une tentative."""
    try:
        user_id = get_current_user_id()
        session = get_session()
        role = get_current_user_role()
        if role not in ['professor', 'admin', 'surveillant']:
            session.close()
            return jsonify({'error': 'Accès non autorisé'}), 403
        logs = session.query(ExamActivityLog).filter_by(
            attempt_id=attempt_id, event_type='proctor_note'
        ).order_by(ExamActivityLog.timestamp).all()
        notes = []
        for l in logs:
            try:   d = json.loads(l.event_data or '{}')
            except: d = {}
            notes.append({
                'id':        l.id,
                'note':      d.get('note', ''),
                'author':    d.get('author', '?'),
                'author_id': d.get('author_id'),
                'timestamp': l.timestamp.isoformat() if l.timestamp else None,
            })
        session.close()
        return jsonify({'notes': notes, 'total': len(notes)})
    except Exception as e:
        print(f"❌ get_proctor_notes {attempt_id}: {e}")
        try: session.close()
        except: pass
        return jsonify({'error': str(e)}), 500


# ============================================================================
# QR CODE D'ACCÈS À L'EXAMEN
# ============================================================================

@exams_bp.route('/api/online_exams/<int:exam_id>/qrcode', methods=['GET'])
@paseto_required
def get_exam_qrcode(exam_id):
    """Génère et retourne un QR code (PNG base64) pointant vers la page de l'examen."""
    try:
        import qrcode as _qrcode, base64 as _b64
        user_id = get_current_user_id()
        session = get_session()
        role = get_current_user_role()
        if role not in ['professor', 'admin']:
            session.close()
            return jsonify({'error': 'Accès non autorisé'}), 403
        exam = session.query(OnlineExam).filter_by(id=exam_id).first()
        if not exam:
            session.close()
            return jsonify({'error': 'Examen non trouvé'}), 404
        # Récupérer les données AVANT de fermer la session
        exam_title = exam.title
        base_url   = request.host_url.rstrip('/')
        exam_url   = f"{base_url}/app"
        session.close()
        qr = _qrcode.QRCode(version=1, box_size=8, border=3,
                             error_correction=_qrcode.constants.ERROR_CORRECT_M)
        qr.add_data(exam_url)
        qr.make(fit=True)
        img = qr.make_image(fill_color='black', back_color='white')
        buf = io.BytesIO()
        img.save(buf, format='PNG')
        buf.seek(0)
        b64 = _b64.b64encode(buf.read()).decode()
        return jsonify({
            'exam_id':    exam_id,
            'exam_title': exam_title,
            'url':        exam_url,
            'qrcode_b64': f"data:image/png;base64,{b64}",
        })
    except Exception as e:
        import traceback
        print(f"❌ get_exam_qrcode {exam_id}: {e}\n{traceback.format_exc()}")
        try: session.close()
        except: pass
        return jsonify({'error': str(e)}), 500


# ============================================================================
# EMAIL RÉCAPITULATIF POST-CLÔTURE (déclenché par close_online_exam)
# ============================================================================

def _send_exam_closure_summary(exam_id: int, professor_email: str, professor_name: str):
    """Envoie un email récapitulatif au professeur après clôture de l'examen (thread)."""
    try:
        session = get_session()
        exam = session.query(OnlineExam).filter_by(id=exam_id).first()
        if not exam:
            session.close()
            return
        attempts  = session.query(ExamAttempt).filter_by(exam_id=exam_id).all()
        total     = len(attempts)
        submitted = sum(1 for a in attempts if a.status.value in ('submitted', 'auto_submitted'))
        banned    = sum(1 for a in attempts if a.status.value == 'banned')
        scores    = [a.score for a in attempts if a.score is not None]
        avg       = round(sum(scores)/len(scores), 2) if scores else None
        high_risk = sum(1 for a in attempts if (a.risk_score or 0) >= 70)
        exam_title = exam.title
        session.close()

        from utils import send_email as _send_email
        subject_line = f"[CEI] Clôture : {exam_title}"
        html_body = f"""<div style="font-family:sans-serif;max-width:520px;margin:auto;border:1px solid #e2e8f0;border-radius:10px;overflow:hidden;">
  <div style="background:#1e293b;padding:20px 24px;">
    <h2 style="color:white;margin:0;font-size:16px;">CEI — Clôture d'examen</h2>
  </div>
  <div style="padding:24px;">
    <p>Bonjour <strong>{professor_name}</strong>,</p>
    <p>L'examen <strong>« {exam_title} »</strong> vient d'être clôturé.</p>
    <table style="width:100%;border-collapse:collapse;margin:16px 0;">
      <tr style="background:#f8fafc;"><td style="padding:8px 12px;color:#64748b;font-size:13px;">Inscrits</td><td style="padding:8px 12px;font-weight:700;">{total}</td></tr>
      <tr><td style="padding:8px 12px;color:#64748b;font-size:13px;">Soumis</td><td style="padding:8px 12px;font-weight:700;color:#6366f1;">{submitted}</td></tr>
      <tr style="background:#f8fafc;"><td style="padding:8px 12px;color:#64748b;font-size:13px;">Exclus</td><td style="padding:8px 12px;font-weight:700;color:#ef4444;">{banned}</td></tr>
      <tr><td style="padding:8px 12px;color:#64748b;font-size:13px;">Note moyenne</td><td style="padding:8px 12px;font-weight:700;color:{'#10b981' if avg and avg>=10 else '#ef4444'};">{f'{avg}/20' if avg is not None else '—'}</td></tr>
      <tr style="background:#f8fafc;"><td style="padding:8px 12px;color:#64748b;font-size:13px;">Haut risque (≥70%)</td><td style="padding:8px 12px;font-weight:700;color:#f59e0b;">{high_risk}</td></tr>
    </table>
    <p style="font-size:13px;color:#64748b;">Connectez-vous à la plateforme CEI pour corriger les copies et consulter les rapports d'intégrité.</p>
  </div>
</div>"""
        _send_email(professor_email, subject_line, html_body)
        print(f"📧 Email clôture envoyé à {professor_email} pour exam#{exam_id}")
    except Exception as e:
        print(f"⚠️  Email clôture exam#{exam_id}: {e}")


# ============================================================================
# ZIP COPIES CORRIGÉES PAR EXAMEN
# ============================================================================

@exams_bp.route('/api/online_exams/<int:exam_id>/corrections/zip', methods=['GET'])
@paseto_required
def download_corrections_zip(exam_id):
    """ZIP de toutes les copies corrigées d'un examen (une copie texte par étudiant)."""
    try:
        import zipfile as zipfile_mod
        user_id = get_current_user_id()
        session = get_session()
        user = session.query(User).filter_by(id=user_id).first()
        if user.role not in [UserRole.PROFESSOR, UserRole.ADMIN]:
            session.close()
            return jsonify({'error': 'Accès non autorisé'}), 403
        exam = session.query(OnlineExam).filter_by(id=exam_id).first()
        if not exam:
            session.close()
            return jsonify({'error': 'Examen non trouvé'}), 404
        attempts = session.query(ExamAttempt).options(
            joinedload(ExamAttempt.student)
        ).filter(
            ExamAttempt.exam_id == exam_id,
            ExamAttempt.score.isnot(None)
        ).all()
        if not attempts:
            session.close()
            return jsonify({'error': 'Aucune copie corrigée pour cet examen'}), 404

        zip_buf = io.BytesIO()
        with zipfile_mod.ZipFile(zip_buf, 'w', zipfile_mod.ZIP_DEFLATED) as zf:
            for attempt in attempts:
                try:
                    answers_data = json.loads(attempt.answers) if attempt.answers else {}
                    student_text = (
                        answers_data.get('content') or answers_data.get('reponse') or
                        answers_data.get('answer')  or answers_data.get('text') or 'Non disponible'
                    )
                except Exception:
                    student_text = attempt.answers or 'Non disponible'

                duration_str = '—'
                if attempt.submitted_at and attempt.started_at:
                    mins = int((attempt.submitted_at - attempt.started_at).total_seconds() / 60)
                    duration_str = f"{mins} min"

                content = (
                    f"COPIE CORRIGÉE — {exam.title}\n"
                    f"{'='*60}\n"
                    f"Étudiant   : {attempt.student.full_name if attempt.student else '—'}\n"
                    f"Note       : {attempt.score}/20\n"
                    f"Risque     : {attempt.risk_score or 0}%\n"
                    f"Durée      : {duration_str}\n"
                    f"Corrigé le : {attempt.corrected_at.strftime('%d/%m/%Y %H:%M') if attempt.corrected_at else '—'}\n"
                    f"Infractions: {attempt.tab_switches or 0} changement(s) de fenêtre, "
                    f"{attempt.warnings_count or 0} avertissement(s), "
                    f"{attempt.no_face_count or 0} absence(s) de visage\n"
                    f"\n{'='*60}\n"
                    f"RÉPONSES DE L'ÉTUDIANT\n"
                    f"{'='*60}\n"
                    f"{student_text}\n"
                    f"\n{'='*60}\n"
                    f"CORRECTION IA\n"
                    f"{'='*60}\n"
                    f"{attempt.feedback or 'Pas de feedback disponible'}\n"
                )
                safe_name = re.sub(r'[^\w\s-]', '', attempt.student.full_name if attempt.student else 'etudiant')
                safe_name = safe_name.strip().replace(' ', '_')
                filename = f"{safe_name}_{attempt.score:.1f}_sur_20.txt"
                zf.writestr(filename, content.encode('utf-8'))

        exam_title = exam.title
        session.close()
        zip_buf.seek(0)
        safe_title = re.sub(r'[^\w\s-]', '', exam_title).strip().replace(' ', '_')
        return send_file(
            zip_buf,
            mimetype='application/zip',
            as_attachment=True,
            download_name=f"corrections_{safe_title}.zip"
        )
    except Exception as e:
        print(f"❌ download_corrections_zip {exam_id}: {e}")
        try: session.close()
        except: pass
        return jsonify({'error': str(e)}), 500


# ============================================================================
# RÉVISION DÉTAILLÉE D'UNE TENTATIVE
# ============================================================================

@exams_bp.route('/api/exam_attempts/<int:attempt_id>/review', methods=['GET'])
@paseto_required
def get_attempt_review(attempt_id):
    """Vue complète d'une tentative: réponses, correction, incidents, notes surveillant."""
    try:
        user_id = get_current_user_id()
        session = get_session()
        user = session.query(User).filter_by(id=user_id).first()

        attempt = session.query(ExamAttempt).options(
            joinedload(ExamAttempt.exam).joinedload(OnlineExam.subject),
            joinedload(ExamAttempt.student),
            joinedload(ExamAttempt.activity_logs)
        ).filter_by(id=attempt_id).first()

        if not attempt:
            session.close()
            return jsonify({'error': 'Tentative non trouvée'}), 404

        if user.role == UserRole.PROFESSOR and attempt.exam.created_by_id != user_id:
            session.close()
            return jsonify({'error': 'Accès non autorisé'}), 403

        try:
            answers_data = json.loads(attempt.answers) if attempt.answers else {}
        except Exception:
            answers_data = {}

        # Format examen en ligne : { qcm: {}, texte: {} }
        qcm_a  = answers_data.get('qcm',  {}) if isinstance(answers_data, dict) else {}
        text_a = answers_data.get('texte', answers_data.get('text', {})) if isinstance(answers_data, dict) else {}
        if qcm_a or text_a:
            lines = []
            all_keys = sorted(set(list(qcm_a.keys()) + list(text_a.keys())),
                               key=lambda x: int(x) if str(x).isdigit() else 0)
            for k in all_keys:
                if k in qcm_a:  lines.append(f"Question {k} : {qcm_a[k]}")
                if k in text_a: lines.append(f"Question {k} : {text_a[k]}")
            student_text = '\n'.join(lines)
        else:
            student_text = (
                answers_data.get('content') or answers_data.get('reponse') or
                answers_data.get('answer')  or answers_data.get('text') or
                attempt.answers or ''
            ) if isinstance(answers_data, dict) else (attempt.answers or '')

        incidents = []
        proctor_notes = []
        for log in sorted(attempt.activity_logs, key=lambda x: x.timestamp or datetime.min):
            try:
                ed = json.loads(log.event_data) if log.event_data else {}
            except Exception:
                ed = {}
            if log.event_type == 'proctor_note':
                proctor_notes.append({
                    'note':      ed.get('note', ''),
                    'author':    ed.get('author', ''),
                    'timestamp': log.timestamp.isoformat() if log.timestamp else None,
                })
            else:
                incidents.append({
                    'type':      log.event_type,
                    'data':      ed,
                    'timestamp': log.timestamp.isoformat() if log.timestamp else None,
                })

        duration_min = None
        if attempt.submitted_at and attempt.started_at:
            duration_min = round((attempt.submitted_at - attempt.started_at).total_seconds() / 60, 1)

        # Collect all scalar values before closing session to avoid DetachedInstanceError
        result = {
            'attempt_id':     attempt.id,
            'student_name':   attempt.student.full_name if attempt.student else '—',
            'student_email':  attempt.student.email if attempt.student else '—',
            'exam_title':     attempt.exam.title if attempt.exam else '—',
            'subject_title':  attempt.exam.subject.title if attempt.exam and attempt.exam.subject else '—',
            'status':         attempt.status.value,
            'score':          attempt.score,
            'started_at':     attempt.started_at.isoformat()  if attempt.started_at  else None,
            'submitted_at':   attempt.submitted_at.isoformat() if attempt.submitted_at else None,
            'duration_min':   duration_min,
            'risk_score':     attempt.risk_score or 0,
            'tab_switches':   attempt.tab_switches or 0,
            'warnings_count': attempt.warnings_count or 0,
            'no_face_count':  attempt.no_face_count or 0,
            'extra_minutes':  attempt.extra_minutes or 0,
            'ban_reason':     attempt.ban_reason,
            'student_answer': student_text,
            'raw_answers':    attempt.answers or '',
            'feedback':       attempt.feedback,
            'corrector_name': attempt.corrector.full_name if attempt.corrector else None,
            'incidents':      incidents,
            'proctor_notes':  proctor_notes,
            'corrected_at':   attempt.corrected_at.isoformat() if attempt.corrected_at else None,
        }
        session.close()
        return jsonify(result)
    except Exception as e:
        print(f"❌ get_attempt_review {attempt_id}: {e}")
        try: session.close()
        except: pass
        return jsonify({'error': str(e)}), 500


# ============================================================================
# DASHBOARD ANALYTIQUE GLOBAL PROFESSEUR
# ============================================================================

@exams_bp.route('/api/professor/analytics', methods=['GET'])
@paseto_required
def get_professor_analytics():
    """Statistiques globales : tous les examens, moyennes, taux réussite, activité récente."""
    try:
        user_id = get_current_user_id()
        session = get_session()
        user = session.query(User).filter_by(id=user_id).first()
        if user.role not in [UserRole.PROFESSOR, UserRole.ADMIN]:
            session.close()
            return jsonify({'error': 'Accès non autorisé'}), 403

        if user.role == UserRole.ADMIN:
            exams = session.query(OnlineExam).options(joinedload(OnlineExam.subject).joinedload(Subject.ec)).all()
        else:
            exams = session.query(OnlineExam).options(joinedload(OnlineExam.subject).joinedload(Subject.ec)).filter_by(created_by_id=user_id).all()

        exam_ids = [e.id for e in exams]

        all_attempts = session.query(ExamAttempt).filter(
            ExamAttempt.exam_id.in_(exam_ids)
        ).options(joinedload(ExamAttempt.student)).all() if exam_ids else []

        submitted = [a for a in all_attempts if a.status.value in ('submitted', 'auto_submitted')]
        all_scores = [a.score for a in submitted if a.score is not None]

        exam_stats = []
        for exam in exams:
            e_attempts  = [a for a in all_attempts if a.exam_id == exam.id]
            e_submitted = [a for a in e_attempts if a.status.value in ('submitted', 'auto_submitted')]
            e_scores    = [a.score for a in e_submitted if a.score is not None]
            exam_stats.append({
                'id':        exam.id,
                'title':     exam.title,
                'status':    exam.status.value,
                'total':     len(e_attempts),
                'submitted': len(e_submitted),
                'corrected': len(e_scores),
                'avg_score': round(sum(e_scores)/len(e_scores), 2) if e_scores else None,
                'pass_rate': round(sum(1 for s in e_scores if s >= 10)/len(e_scores)*100, 1) if e_scores else None,
            })

        ranked = [e for e in exam_stats if e['avg_score'] is not None and e['corrected'] >= 2]
        ranked.sort(key=lambda x: x['avg_score'], reverse=True)

        recent = session.query(ExamAttempt).filter(
            ExamAttempt.exam_id.in_(exam_ids),
            ExamAttempt.corrected_at.isnot(None)
        ).order_by(ExamAttempt.corrected_at.desc()).limit(10).all() if exam_ids else []

        recent_list = [{
            'student_name': a.student.full_name if a.student else '—',
            'exam_title':   a.exam.title if a.exam else '—',
            'score':        a.score,
            'corrected_at': a.corrected_at.isoformat() if a.corrected_at else None,
        } for a in recent]

        status_counts = {}
        for exam in exams:
            s = exam.status.value
            status_counts[s] = status_counts.get(s, 0) + 1

        # Retour #21 — 4 ratios clarifiés par l'utilisateur (le "ratio étudiants
        # par étudiants" initial était ambigu ; il désigne en réalité 4 ratios
        # distincts) :
        #   1) étudiants / surveillant — charge de travail moyenne des
        #      surveillants (nb d'affectations ProctorAssignment / nb de
        #      surveillants distincts, sur les examens du prof ou tous si admin)
        #   2) étudiants / examen — éligibles (inscrits à l'UE de l'EC du
        #      sujet) vs tentatives réelles, moyennés sur tous les examens —
        #      2 chiffres plutôt qu'un seul car l'un ou l'autre peut être ce
        #      qu'on entend par "ratio étudiants/examens" selon le contexte
        #   3) étudiants / notes — taux de correction (déjà calculé plus haut
        #      via total_submitted/total_corrected, juste reformulé en ratio)
        #   4) étudiants / validation — taux de réussite (score ≥ 10),
        #      idem déjà calculé via overall_pass_rate
        proctor_assignments = session.query(ProctorAssignment).filter(
            ProctorAssignment.exam_id.in_(exam_ids)
        ).all() if exam_ids else []
        distinct_proctors = {pa.proctor_id for pa in proctor_assignments}
        students_per_proctor = round(len(proctor_assignments) / len(distinct_proctors), 1) if distinct_proctors else None

        eligible_per_exam = []
        for exam in exams:
            ec = exam.subject.ec if exam.subject else None
            if ec and ec.ue_id:
                n = session.query(StudentUEEnrollment).filter_by(ue_id=ec.ue_id).count()
                eligible_per_exam.append(n)
        total_eligible = sum(eligible_per_exam)
        avg_eligible_per_exam = round(total_eligible / len(eligible_per_exam), 1) if eligible_per_exam else None
        avg_attempts_per_exam = round(len(all_attempts) / len(exams), 1) if exams else None
        participation_rate = round(len(all_attempts) / total_eligible * 100, 1) if total_eligible else None

        grading_completion_rate = round(len(all_scores) / len(submitted) * 100, 1) if submitted else None
        pass_count = sum(1 for s in all_scores if s >= 10)
        validation_rate = round(pass_count / len(all_scores) * 100, 1) if all_scores else None

        ratios = {
            'students_per_proctor': {
                'total_assignments': len(proctor_assignments),
                'distinct_proctors':  len(distinct_proctors),
                'avg':                students_per_proctor,
            },
            'students_per_exam': {
                'avg_eligible':       avg_eligible_per_exam,
                'avg_attempts':       avg_attempts_per_exam,
                'participation_rate': participation_rate,
            },
            'students_per_grade': {
                'total_submitted':  len(submitted),
                'total_corrected':  len(all_scores),
                'completion_rate':  grading_completion_rate,
            },
            'students_per_validation': {
                'total_scored':    len(all_scores),
                'total_validated': pass_count,
                'validation_rate': validation_rate,
            },
        }

        session.close()
        return jsonify({
            'total_exams':      len(exams),
            'status_counts':    status_counts,
            'total_attempts':   len(all_attempts),
            'total_submitted':  len(submitted),
            'total_corrected':  len(all_scores),
            'overall_avg':      round(sum(all_scores)/len(all_scores), 2) if all_scores else None,
            'overall_pass_rate':round(sum(1 for s in all_scores if s >= 10)/len(all_scores)*100, 1) if all_scores else None,
            'top_exams':        ranked[:3],
            'bottom_exams':     ranked[-3:][::-1] if len(ranked) >= 3 else [],
            'recent_corrections': recent_list,
            'exam_stats':       exam_stats,
            'ratios':           ratios,
        })
    except Exception as e:
        print(f"❌ get_professor_analytics: {e}")
        try: session.close()
        except: pass
        return jsonify({'error': str(e)}), 500


# ============================================================================
# PDF RAPPORT INDIVIDUEL PAR TENTATIVE
# ============================================================================

@exams_bp.route('/api/exam_attempts/<int:attempt_id>/report/pdf', methods=['GET'])
@paseto_required
def download_attempt_report_pdf(attempt_id):
    """PDF rapport individuel complet d'une tentative d'examen."""
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable
        from reportlab.lib import colors as rl_colors
        from reportlab.lib.units import cm

        user_id = get_current_user_id()
        session = get_session()
        user = session.query(User).filter_by(id=user_id).first()
        if user.role not in [UserRole.PROFESSOR, UserRole.ADMIN]:
            session.close()
            return jsonify({'error': 'Accès non autorisé'}), 403

        attempt = session.query(ExamAttempt).options(
            joinedload(ExamAttempt.exam).joinedload(OnlineExam.subject),
            joinedload(ExamAttempt.student),
            joinedload(ExamAttempt.activity_logs)
        ).filter_by(id=attempt_id).first()

        if not attempt:
            session.close()
            return jsonify({'error': 'Tentative non trouvée'}), 404

        if user.role == UserRole.PROFESSOR and attempt.exam.created_by_id != user_id:
            session.close()
            return jsonify({'error': 'Accès non autorisé'}), 403

        try:
            answers_data = json.loads(attempt.answers) if attempt.answers else {}
            student_text = (
                answers_data.get('content') or answers_data.get('reponse') or
                answers_data.get('answer')  or answers_data.get('text') or 'Non disponible'
            )
        except Exception:
            student_text = attempt.answers or 'Non disponible'

        duration_str = '—'
        if attempt.submitted_at and attempt.started_at:
            mins = int((attempt.submitted_at - attempt.started_at).total_seconds() / 60)
            duration_str = f"{mins} min"

        incident_count = sum(1 for log in attempt.activity_logs if log.event_type != 'proctor_note')
        note_count     = sum(1 for log in attempt.activity_logs if log.event_type == 'proctor_note')

        buffer = io.BytesIO()
        doc    = SimpleDocTemplate(buffer, pagesize=A4,
                                   leftMargin=1.5*cm, rightMargin=1.5*cm,
                                   topMargin=1.5*cm, bottomMargin=1.5*cm)
        styles = getSampleStyleSheet()
        story  = []

        # En-tête
        hdr_data = [['CEI — Rapport de Copie',
                      f"Généré le {datetime.utcnow().strftime('%d/%m/%Y %H:%M')} UTC"]]
        hdr_tbl = Table(hdr_data, colWidths=[13*cm, 5*cm])
        hdr_tbl.setStyle(TableStyle([
            ('BACKGROUND', (0,0), (-1,-1), rl_colors.HexColor('#1e293b')),
            ('TEXTCOLOR',  (0,0), (-1,-1), rl_colors.white),
            ('FONTNAME',   (0,0), (0,0),  'Helvetica-Bold'),
            ('FONTSIZE',   (0,0), (0,0),  13),
            ('FONTSIZE',   (1,0), (1,0),   9),
            ('ALIGN',      (1,0), (1,0),  'RIGHT'),
            ('VALIGN',     (0,0), (-1,-1),'MIDDLE'),
            ('PADDING',    (0,0), (-1,-1), 10),
        ]))
        story.append(hdr_tbl)
        story.append(Spacer(1, 0.4*cm))

        score_color = rl_colors.HexColor('#10b981') if (attempt.score or 0) >= 10 else rl_colors.HexColor('#ef4444')
        risk_val    = attempt.risk_score or 0
        risk_color  = (rl_colors.HexColor('#ef4444') if risk_val >= 70 else
                       rl_colors.HexColor('#f59e0b') if risk_val >= 40 else rl_colors.HexColor('#10b981'))

        info_data = [
            ['Étudiant', attempt.student.full_name if attempt.student else '—',
             'Note',        f"{attempt.score}/20" if attempt.score is not None else '—'],
            ['Examen',   attempt.exam.title if attempt.exam else '—',
             'Risque',      f"{risk_val}%"],
            ['Matière',  attempt.exam.subject.title if attempt.exam and attempt.exam.subject else '—',
             'Durée',       duration_str],
            ['Statut',   attempt.status.value,
             'Extra-temps', f"{attempt.extra_minutes or 0} min"],
        ]
        info_tbl = Table(info_data, colWidths=[3*cm, 9*cm, 2.5*cm, 3.5*cm])
        info_style = TableStyle([
            ('FONTNAME',       (0,0), (-1,-1),  'Helvetica'),
            ('FONTSIZE',       (0,0), (-1,-1),   9),
            ('FONTNAME',       (0,0), (0,-1),   'Helvetica-Bold'),
            ('FONTNAME',       (2,0), (2,-1),   'Helvetica-Bold'),
            ('TEXTCOLOR',      (0,0), (0,-1),    rl_colors.HexColor('#64748b')),
            ('TEXTCOLOR',      (2,0), (2,-1),    rl_colors.HexColor('#64748b')),
            ('ROWBACKGROUNDS', (0,0), (-1,-1),  [rl_colors.HexColor('#f8fafc'), rl_colors.white]),
            ('PADDING',        (0,0), (-1,-1),   7),
            ('BOX',            (0,0), (-1,-1),   0.5, rl_colors.HexColor('#e2e8f0')),
            ('INNERGRID',      (0,0), (-1,-1),   0.3, rl_colors.HexColor('#e2e8f0')),
        ])
        if attempt.score is not None:
            info_style.add('TEXTCOLOR', (3,0), (3,0), score_color)
            info_style.add('FONTNAME',  (3,0), (3,0), 'Helvetica-Bold')
        info_style.add('TEXTCOLOR', (3,1), (3,1), risk_color)
        info_tbl.setStyle(info_style)
        story.append(info_tbl)
        story.append(Spacer(1, 0.4*cm))

        story.append(Paragraph(
            f"<b>Incidents</b> : {attempt.tab_switches or 0} changement(s) de fenêtre · "
            f"{attempt.warnings_count or 0} avertissement(s) · "
            f"{attempt.no_face_count or 0} absence(s) de visage · "
            f"{incident_count} événement(s) total · {note_count} note(s) de surveillance",
            ParagraphStyle('inc', parent=styles['Normal'], fontSize=9)
        ))
        story.append(Spacer(1, 0.4*cm))
        story.append(HRFlowable(width="100%", thickness=0.5, color=rl_colors.HexColor('#e2e8f0')))
        story.append(Spacer(1, 0.3*cm))

        story.append(Paragraph("<b>Réponses de l'étudiant</b>",
            ParagraphStyle('h3', parent=styles['Normal'], fontSize=11,
                           textColor=rl_colors.HexColor('#1e293b'))))
        story.append(Spacer(1, 0.2*cm))
        ans_style = ParagraphStyle('ans', parent=styles['Normal'], fontSize=8.5, leading=13,
                                   textColor=rl_colors.HexColor('#334155'))
        truncated_ans = (student_text[:4000] + '…') if len(student_text) > 4000 else student_text
        for chunk in truncated_ans.split('\n'):
            if chunk.strip():
                story.append(Paragraph(chunk.replace('&','&amp;').replace('<','&lt;').replace('>','&gt;'), ans_style))
            else:
                story.append(Spacer(1, 0.1*cm))

        if attempt.feedback:
            story.append(Spacer(1, 0.4*cm))
            story.append(HRFlowable(width="100%", thickness=0.5, color=rl_colors.HexColor('#e2e8f0')))
            story.append(Spacer(1, 0.3*cm))
            story.append(Paragraph("<b>Correction IA</b>",
                ParagraphStyle('h3fb', parent=styles['Normal'], fontSize=11,
                               textColor=rl_colors.HexColor('#6366f1'))))
            story.append(Spacer(1, 0.2*cm))
            fb_style = ParagraphStyle('fb', parent=styles['Normal'], fontSize=8.5, leading=13,
                                       textColor=rl_colors.HexColor('#334155'))
            truncated_fb = (attempt.feedback[:5000] + '…') if len(attempt.feedback) > 5000 else attempt.feedback
            for chunk in truncated_fb.split('\n'):
                if chunk.strip():
                    story.append(Paragraph(chunk.replace('&','&amp;').replace('<','&lt;').replace('>','&gt;'), fb_style))
                else:
                    story.append(Spacer(1, 0.1*cm))

        doc.build(story)
        student_name = attempt.student.full_name if attempt.student else 'etudiant'
        session.close()

        buffer.seek(0)
        safe_sn = re.sub(r'[^\w\s-]', '', student_name).strip().replace(' ', '_')
        return send_file(
            buffer,
            mimetype='application/pdf',
            as_attachment=True,
            download_name=f"rapport_{safe_sn}_attempt{attempt_id}.pdf"
        )
    except Exception as e:
        print(f"❌ download_attempt_report_pdf {attempt_id}: {e}")
        import traceback; traceback.print_exc()
        try: session.close()
        except: pass
        return jsonify({'error': str(e)}), 500


