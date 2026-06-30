"""
Blueprint Papers (Correction de copies).

POST /api/papers/correct   (alias /api/papers/upload)
POST /api/papers/upload-batch
GET  /api/papers/subject/<subject_id>
GET  /api/papers/detail/<paper_id>
"""
import os
from datetime import datetime, timedelta
from flask import Blueprint, request, jsonify
from werkzeug.utils import secure_filename
from sqlalchemy import desc
from sqlalchemy.orm import joinedload

from auth_paseto import paseto_required, get_current_user_id
from helpers     import utcnow
from extensions  import bcrypt
from models      import (
    get_session, User, UserRole, Subject, StudentPaper,
    ECAssignment, Reclamation, CorrectionHistory,
)
from utils import (
    allowed_file, extract_text_from_file,
    generate_corrected_paper_pdf, send_paper_corrected_email,
    calculate_file_hash, extract_student_name_from_content,
    match_student_by_name,
)
from services.ai_service import (
    call_ai             as call_claude,
    extract_score       as extract_score_from_correction,
    build_correction_prompt as _build_correction_system_prompt,
)

papers_bp = Blueprint('papers', __name__)

_UPLOAD_FOLDER = os.getenv('UPLOAD_FOLDER', 'static/uploads')


# ── upload + correction ───────────────────────────────────────────────────────

@papers_bp.route('/api/papers/correct', methods=['POST'])
@papers_bp.route('/api/papers/upload', methods=['POST'])
@paseto_required
def upload_paper():
    try:
        user_id = get_current_user_id()
        session = get_session()
        user    = session.query(User).filter_by(id=user_id).first()

        if 'file' not in request.files:
            session.close(); return jsonify({'error': 'Aucun fichier fourni'}), 400

        file        = request.files['file']
        subject_id  = request.form.get('subject_id')
        student_id  = request.form.get('student_id')
        student_name= request.form.get('student_name')

        if not subject_id:
            session.close(); return jsonify({'error': 'ID du sujet requis'}), 400
        if not student_id and not student_name:
            session.close(); return jsonify({'error': "ID ou nom de l'étudiant requis"}), 400

        subject = session.query(Subject).filter_by(id=subject_id).first()
        if not subject: session.close(); return jsonify({'error': 'Sujet non trouvé'}), 404
        if user.role == UserRole.PROFESSOR and subject.creator_id != user_id:
            session.close(); return jsonify({'error': 'Vous ne pouvez corriger que vos propres sujets'}), 403

        # Matching intelligent si nom fourni sans ID
        if student_name and not student_id:
            matched = match_student_by_name(student_name, session)
            if matched:
                student_id = matched.id
            else:
                session.close()
                return jsonify({
                    'error': f'Étudiant "{student_name}" non trouvé dans le système',
                    'suggestion': 'Veuillez creer cet etudiant via interface Admin',
                    'extracted_name': student_name,
                }), 404

        if file.filename == '':
            session.close(); return jsonify({'error': 'Aucun fichier sélectionné'}), 400
        if not allowed_file(file.filename):
            session.close(); return jsonify({'error': 'Type de fichier non autorisé'}), 400

        filename = f"paper_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{secure_filename(file.filename)}"
        filepath = os.path.join(_UPLOAD_FOLDER, filename)
        file.save(filepath)

        # Vérification doublons par hash
        file_hash = calculate_file_hash(filepath)
        if file_hash:
            existing = session.query(StudentPaper).filter_by(file_hash=file_hash).first()
            if existing:
                os.remove(filepath); session.close()
                return jsonify({
                    'error': 'Cette copie a déjà été corrigée',
                    'duplicate': True,
                    'existing_paper_id': existing.id,
                    'student_name': existing.student.full_name if existing.student else 'Inconnu',
                    'score': existing.score,
                }), 400

        paper_content = extract_text_from_file(filepath)
        if not paper_content:
            os.remove(filepath); session.close()
            return jsonify({'error': "Impossible d'extraire le texte du fichier"}), 400

        extracted_name = extract_student_name_from_content(paper_content)
        if extracted_name and not student_id:
            matched = match_student_by_name(extracted_name, session)
            if matched:
                student_id = matched.id
            else:
                os.remove(filepath); session.close()
                return jsonify({
                    'error': f'Étudiant "{extracted_name}" non trouvé',
                    'suggestion': "Créez d'abord cet étudiant via Admin -> Utilisateurs",
                    'extracted_name': extracted_name,
                }), 404

        system_prompt = _build_correction_system_prompt(subject.title, subject.content)
        user_message  = (
            f"SUJET D'EXAMEN:\n{subject.content}\n\n"
            f"BARÈME DE NOTATION:\n{subject.rubric}\n\n"
            f"COPIE À CORRIGER:\n{paper_content}\n\n"
            "RAPPEL: Tu DOIS finir par \"Note totale: XX.XX/20\" "
        )
        result = call_claude(system_prompt, user_message, temperature=0.15)
        score  = extract_score_from_correction(result)

        corrected_at = utcnow()
        new_paper = StudentPaper(
            subject_id=subject_id, student_id=student_id,
            content=paper_content, grade=result, score=score, filename=filename,
            file_hash=file_hash, extracted_student_name=extracted_name,
            corrected_by_id=user_id if user.role in [UserRole.PROFESSOR, UserRole.ADMIN] else None,
            corrected_at=corrected_at,
            reclamation_window_end=corrected_at + timedelta(days=7),
        )
        session.add(new_paper); session.commit()

        # Email + PDF
        try:
            student_obj = session.query(User).filter_by(id=student_id).first()
            if student_obj and student_obj.email and '@temp.edu' not in student_obj.email:
                paper_data = {
                    'student_name': student_obj.full_name, 'subject_title': subject.title,
                    'score': score, 'grade': result, 'corrected_at': corrected_at.isoformat(),
                }
                pdf_path = f"exports/copie_{new_paper.id}.pdf"
                generate_corrected_paper_pdf(paper_data, pdf_path)
                email_sent = send_paper_corrected_email(
                    student_email=student_obj.email, student_name=student_obj.full_name,
                    subject_title=subject.title, score=score, paper_id=new_paper.id,
                    attachments=[{'filename': f'copie_{new_paper.id}.pdf', 'path': pdf_path}],
                )
                if email_sent:
                    new_paper.email_sent = True; session.commit()
                try: os.remove(pdf_path)
                except Exception: pass
        except Exception as email_error:
            print(f"WARNING email: {email_error}")

        result_dict = new_paper.to_dict(); session.close()
        return jsonify({
            'success': True, 'paper': result_dict,
            'duplicate_check': 'passed', 'extracted_name': extracted_name,
            'email_sent': new_paper.email_sent,
        })
    except Exception as e:
        print(f"ERROR upload_paper: {e}")
        import traceback; traceback.print_exc()
        return jsonify({'error': str(e)}), 500


# ── upload-batch ──────────────────────────────────────────────────────────────

@papers_bp.route('/api/papers/upload-batch', methods=['POST'])
@paseto_required
def upload_papers_batch():
    try:
        user_id = get_current_user_id()
        session = get_session()
        user    = session.query(User).filter_by(id=user_id).first()

        if user.role not in [UserRole.PROFESSOR, UserRole.ADMIN]:
            session.close(); return jsonify({'error': 'Accès non autorisé'}), 403

        subject_id = request.form.get('subject_id')
        if not subject_id:
            session.close(); return jsonify({'error': 'ID du sujet requis'}), 400

        subject = session.query(Subject).filter_by(id=subject_id).first()
        if not subject: session.close(); return jsonify({'error': 'Sujet non trouvé'}), 404
        if user.role == UserRole.PROFESSOR and subject.creator_id != user_id:
            session.close(); return jsonify({'error': 'Vous ne pouvez corriger que vos propres sujets'}), 403

        files = request.files.getlist('files')
        if not files:
            session.close(); return jsonify({'error': 'Aucun fichier fourni'}), 400

        results = []; errors = []
        system_prompt = _build_correction_system_prompt(subject.title, subject.content)

        for idx, file in enumerate(files):
            try:
                if file.filename == '':
                    errors.append(f"Fichier {idx+1}: Nom vide"); continue
                if not allowed_file(file.filename):
                    errors.append(f"Fichier {idx+1}: Type non autorisé"); continue

                fname_saved = f"paper_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{idx}_{secure_filename(file.filename)}"
                filepath    = os.path.join(_UPLOAD_FOLDER, fname_saved)
                file.save(filepath)

                file_hash = calculate_file_hash(filepath)
                if file_hash:
                    existing = session.query(StudentPaper).filter_by(file_hash=file_hash).first()
                    if existing:
                        os.remove(filepath)
                        results.append({
                            'filename': file.filename,
                            'student_name': existing.student.full_name if existing.student else 'Inconnu',
                            'score': existing.score, 'success': False, 'duplicate': True,
                            'message': 'Copie déjà corrigée (hash trouvé)',
                        }); continue

                paper_content = extract_text_from_file(filepath)
                if not paper_content:
                    os.remove(filepath)
                    errors.append(f"Fichier {idx+1}: Extraction impossible"); continue

                extracted_name = extract_student_name_from_content(paper_content)
                student        = None
                student_name   = None

                if extracted_name:
                    student      = match_student_by_name(extracted_name, session)
                    student_name = extracted_name

                if not student:
                    guessed = os.path.splitext(file.filename)[0].replace('copie_', '').replace('_', ' ').title()
                    student      = match_student_by_name(guessed, session)
                    student_name = student_name or guessed

                if not student:
                    temp_email = f"{student_name.lower().replace(' ', '.')}@temp.edu"
                    student = User(
                        email=temp_email,
                        password_hash=bcrypt.generate_password_hash('TempPassword123').decode('utf-8'),
                        full_name=student_name, role=UserRole.STUDENT,
                    )
                    session.add(student); session.flush()

                user_message = (
                    f"SUJET: {subject.content}\nBARÈME: {subject.rubric}\n"
                    f"COPIE: {paper_content}\n\n"
                    "RAPPEL: Termine par \"Note totale: XX.XX/20\" "
                )
                correction = call_claude(system_prompt, user_message, temperature=0.15)
                score      = extract_score_from_correction(correction)

                corrected_at = utcnow()
                new_paper = StudentPaper(
                    subject_id=subject_id, student_id=student.id,
                    content=paper_content, grade=correction, score=score,
                    filename=fname_saved, file_hash=file_hash,
                    corrected_by_id=user_id, corrected_at=corrected_at,
                    reclamation_window_end=corrected_at + timedelta(days=7),
                )
                session.add(new_paper); session.flush()

                if (student.email and '@temp.edu' not in student.email
                        and '@noemail.local' not in student.email
                        and getattr(student, 'has_email', True)):
                    paper_data = {
                        'student_name': student.full_name, 'subject_title': subject.title,
                        'score': score, 'grade': correction, 'corrected_at': corrected_at.isoformat(),
                    }
                    pdf_path = f"exports/copie_{new_paper.id}.pdf"
                    generate_corrected_paper_pdf(paper_data, pdf_path)
                    email_sent = send_paper_corrected_email(
                        student_email=student.email, student_name=student.full_name,
                        subject_title=subject.title, score=score, paper_id=new_paper.id,
                        attachments=[{'filename': f'copie_{new_paper.id}.pdf', 'path': pdf_path}],
                    )
                    if email_sent: new_paper.email_sent = True
                    try: os.remove(pdf_path)
                    except Exception: pass

                results.append({'filename': file.filename, 'student_name': student_name, 'score': score, 'success': True})
            except Exception as e:
                errors.append(f"Fichier {idx+1}: {str(e)}")

        session.commit(); session.close()
        return jsonify({'success': True, 'corrected': len(results), 'errors': len(errors),
                        'results': results, 'error_details': errors})
    except Exception as e:
        print(f"ERROR upload_papers_batch: {e}")
        import traceback; traceback.print_exc()
        return jsonify({'error': str(e)}), 500


# ── liste + détail ────────────────────────────────────────────────────────────

@papers_bp.route('/api/papers/subject/<int:subject_id>', methods=['GET'])
@paseto_required
def get_papers_by_subject(subject_id):
    try:
        user_id = get_current_user_id()
        session = get_session()
        user    = session.query(User).filter_by(id=user_id).first()

        if user.role not in [UserRole.PROFESSOR, UserRole.ADMIN]:
            session.close(); return jsonify({'error': 'Accès non autorisé'}), 403

        subject = session.query(Subject).filter_by(id=subject_id).first()
        if not subject: session.close(); return jsonify({'error': 'Sujet non trouvé'}), 404
        if user.role == UserRole.PROFESSOR and subject.creator_id != user_id:
            session.close(); return jsonify({'error': 'Vous ne pouvez voir que les copies de vos propres sujets'}), 403

        papers = session.query(StudentPaper).options(
            joinedload(StudentPaper.student)
        ).filter_by(subject_id=subject_id).all()

        result = [{
            'id':           p.id,
            'student_id':   p.student_id,
            'student_name': p.student.full_name if p.student else 'Inconnu',
            'student_email':p.student.email     if p.student else 'N/A',
            'score':        p.score,
            'grade':        p.grade,
            'content':      p.content,
            'filename':     p.filename,
            'corrected_at': p.corrected_at.isoformat() if p.corrected_at else None,
            'created_at':   p.created_at.isoformat()   if p.created_at   else None,
        } for p in papers]
        session.close()
        return jsonify(result)
    except Exception as e:
        print(f"ERROR get_papers_by_subject: {e}")
        import traceback; traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@papers_bp.route('/api/papers/detail/<int:paper_id>', methods=['GET'])
@paseto_required
def get_paper_detail(paper_id):
    try:
        user_id = get_current_user_id()
        session = get_session()
        user    = session.query(User).filter_by(id=user_id).first()

        paper = session.query(StudentPaper).options(
            joinedload(StudentPaper.subject),
            joinedload(StudentPaper.student)
        ).filter_by(id=paper_id).first()

        if not paper: session.close(); return jsonify({'error': 'Copie non trouvée'}), 404
        if user.role == UserRole.STUDENT and paper.student_id != user_id:
            session.close(); return jsonify({'error': 'Accès non autorisé'}), 403
        if user.role == UserRole.PROFESSOR and paper.subject.creator_id != user_id:
            session.close(); return jsonify({'error': 'Accès non autorisé'}), 403

        result = paper.to_dict(); session.close()
        return jsonify(result)
    except Exception as e:
        print(f"ERROR get_paper_detail: {e}")
        import traceback; traceback.print_exc()
        return jsonify({'error': str(e)}), 500
