"""
Blueprint Transcripts (Relevés de notes) — Contrôleur MVC.

Routes :
  POST /api/transcripts/generate/<student_id>/<semester_id>
  GET  /api/transcripts
  GET  /api/student/transcripts
  GET  /api/transcripts/<id>/pdf
  GET  /api/transcripts/bulk-pdf
  DELETE /api/transcripts/<id>
  PUT  /api/transcripts/<id>/publish

Migré depuis app.py — logique LMD identique.
"""
import json, io, zipfile
from datetime import datetime as _dt
from flask import Blueprint, request, jsonify, send_file
from sqlalchemy.orm import joinedload

from auth_paseto import paseto_required, get_current_user_id
from helpers     import utcnow
from models      import (
    get_session, User, UserRole,
    Formation, Semester, UE, EC, ECAssignment, StudentUEEnrollment,
    Subject, StudentPaper, ExamAttempt, OnlineExam, GradeTranscript,
)

transcripts_bp = Blueprint('transcripts', __name__)


# ═══════════════════════════════════════════════════════════════════════════════
# GÉNÉRATION (logique LMD complète)
# ═══════════════════════════════════════════════════════════════════════════════

@transcripts_bp.route('/api/transcripts/generate/<int:student_id>/<int:semester_id>', methods=['POST'])
@paseto_required
def generate_transcript(student_id, semester_id):
    try:
        user_id = get_current_user_id()
        session = get_session()
        user    = session.query(User).filter_by(id=user_id).first()

        if user.role not in [UserRole.ADMIN, UserRole.PROFESSOR]:
            session.close(); return jsonify({'error': 'Accès non autorisé'}), 403

        student  = session.query(User).filter_by(id=student_id, role=UserRole.STUDENT).first()
        if not student: session.close(); return jsonify({'error': 'Étudiant non trouvé'}), 404

        semester = session.query(Semester).filter_by(id=semester_id).first()
        if not semester: session.close(); return jsonify({'error': 'Semestre non trouvé'}), 404

        # Vérification d'accès professeur
        if user.role == UserRole.PROFESSOR:
            prof_ue_ids = set()
            for asgn in session.query(ECAssignment).filter_by(professor_id=user_id).all():
                ec = session.query(EC).filter_by(id=asgn.ec_id).first()
                if ec:
                    ue = session.query(UE).filter_by(id=ec.ue_id, semester_id=semester_id).first()
                    if ue: prof_ue_ids.add(ue.id)
            enrolled = session.query(StudentUEEnrollment).filter(
                StudentUEEnrollment.student_id == student_id,
                StudentUEEnrollment.ue_id.in_(prof_ue_ids)
            ).first() if prof_ue_ids else None
            if not enrolled:
                session.close()
                return jsonify({'error': 'Vous ne pouvez générer un relevé que pour les étudiants inscrits dans vos UEs.'}), 403

        # UEs du semestre
        ues = session.query(UE).options(joinedload(UE.ecs)).filter_by(
            semester_id=semester_id, is_active=True
        ).order_by(UE.code).all()
        if not ues:
            session.close(); return jsonify({'error': 'Aucune UE configurée pour ce semestre'}), 400

        _epoch = _dt(2000, 1, 1)
        ue_results = []
        total_notes_found = 0

        for ue in ues:
            ec_results = []
            ue_weighted_sum = 0.0
            ue_total_coef   = 0

            for ec in sorted(ue.ecs, key=lambda e: e.code):
                if not ec.is_active: continue

                papers_dated = (
                    session.query(StudentPaper.score, StudentPaper.corrected_at)
                    .join(Subject, StudentPaper.subject_id == Subject.id)
                    .filter(StudentPaper.student_id == student_id, Subject.ec_id == ec.id,
                            StudentPaper.score.isnot(None))
                    .order_by(StudentPaper.corrected_at.desc()).all()
                )
                attempts_dated = (
                    session.query(ExamAttempt.score, ExamAttempt.corrected_at, ExamAttempt.submitted_at)
                    .join(OnlineExam, ExamAttempt.exam_id == OnlineExam.id)
                    .join(Subject, OnlineExam.subject_id == Subject.id)
                    .filter(ExamAttempt.student_id == student_id, Subject.ec_id == ec.id,
                            ExamAttempt.score.isnot(None))
                    .order_by(ExamAttempt.corrected_at.desc(), ExamAttempt.submitted_at.desc()).all()
                )

                unified = (
                    [(row[0], row[1] or _epoch) for row in papers_dated] +
                    [(row[0], row[1] or row[2] or _epoch) for row in attempts_dated]
                )
                if unified:
                    unified.sort(key=lambda x: x[1], reverse=True)
                    ec_note = unified[0][0]
                    ue_weighted_sum += ec_note * ec.coefficient
                    ue_total_coef   += ec.coefficient
                    total_notes_found += 1
                    ec_avg_rounded = round(ec_note, 2)
                else:
                    ec_avg_rounded = None

                ec_results.append({'ec_id': ec.id, 'ec_code': ec.code, 'ec_name': ec.name,
                                   'coefficient': ec.coefficient, 'note': ec_avg_rounded})

            ue_avg       = (ue_weighted_sum / ue_total_coef) if ue_total_coef > 0 else None
            ue_validated = (ue_avg >= 10) if ue_avg is not None else None
            ue_results.append({
                'ue_id': ue.id, 'ue_code': ue.code, 'ue_name': ue.name, 'credits': ue.credits,
                'ecs': ec_results, 'moyenne': round(ue_avg, 2) if ue_avg is not None else None,
                'validated': ue_validated, 'validated_by_compensation': False,
                'credits_acquis': ue.credits if ue_validated else 0,
            })

        if total_notes_found == 0:
            session.close()
            return jsonify({'success': False, 'error': 'Aucune note disponible pour ce semestre'}), 200

        ues_with_grades = [u for u in ue_results if u['moyenne'] is not None]
        sem_weighted    = sum(u['moyenne'] * u['credits'] for u in ues_with_grades)
        sem_credits     = sum(u['credits'] for u in ues_with_grades)
        semester_avg    = round(sem_weighted / sem_credits, 2) if sem_credits > 0 else 0.0

        # Compensation semestrielle LMD
        if semester_avg >= 10:
            for u in ue_results:
                if u['moyenne'] is not None and not u['validated']:
                    u['validated'] = True
                    u['validated_by_compensation'] = True
                    u['credits_acquis'] = u['credits']

        semester_total_credits = sum(u['credits'] for u in ue_results)
        obtained_credits       = sum(u['credits_acquis'] for u in ue_results)

        transcript = session.query(GradeTranscript).filter_by(
            student_id=student_id, semester_id=semester_id
        ).first()
        if not transcript:
            transcript = GradeTranscript(student_id=student_id, semester_id=semester_id,
                                          generated_by_id=user_id)
            session.add(transcript)

        transcript.gpa              = semester_avg
        transcript.total_credits    = semester_total_credits
        transcript.obtained_credits = obtained_credits
        transcript.ue_details       = json.dumps(ue_results, ensure_ascii=False)
        transcript.generated_at     = utcnow()
        transcript.generated_by_id  = user_id
        session.commit()
        result = transcript.to_dict()
        session.close()
        return jsonify({'success': True, 'transcript': result})
    except Exception as e:
        print(f"ERROR generate_transcript: {e}")
        import traceback; traceback.print_exc()
        return jsonify({'error': str(e)}), 500


# ═══════════════════════════════════════════════════════════════════════════════
# LECTURE
# ═══════════════════════════════════════════════════════════════════════════════

def _serialize_transcript(t, session):
    generator_name = 'Système'
    if t.generated_by_id:
        g = session.query(User).filter_by(id=t.generated_by_id).first()
        if g: generator_name = g.full_name
    ue_data = None
    if t.ue_details:
        try: ue_data = json.loads(t.ue_details)
        except Exception: pass
    return {
        'id':               t.id,
        'student_id':       t.student_id,
        'student_name':     t.student.full_name if t.student else 'Inconnu',
        'student_email':    t.student.email     if t.student else 'N/A',
        'semester_id':      t.semester_id,
        'semester_name':    t.semester.name      if t.semester else 'N/A',
        'formation_name':   t.semester.formation.name if t.semester and t.semester.formation else 'N/A',
        'gpa':              t.gpa,
        'total_credits':    t.total_credits,
        'obtained_credits': t.obtained_credits,
        'validated':        (t.gpa >= 10) if t.gpa is not None else False,
        'ue_details':       ue_data,
        'generated_by':     generator_name,
        'generated_by_id':  t.generated_by_id,
        'generated_at':     t.generated_at.isoformat() if t.generated_at else None,
        'is_published':     bool(getattr(t, 'is_published', False)),
    }


@transcripts_bp.route('/api/transcripts', methods=['GET'])
@paseto_required
def get_all_transcripts():
    try:
        user_id = get_current_user_id()
        session = get_session()
        user    = session.query(User).filter_by(id=user_id).first()

        if user.role not in [UserRole.ADMIN, UserRole.PROFESSOR]:
            session.close(); return jsonify({'error': 'Accès non autorisé'}), 403

        query = session.query(GradeTranscript).options(
            joinedload(GradeTranscript.student),
            joinedload(GradeTranscript.semester).joinedload(Semester.formation),
        )
        if user.role == UserRole.PROFESSOR:
            query = query.filter(GradeTranscript.generated_by_id == user_id)

        transcripts = query.order_by(GradeTranscript.generated_at.desc()).all()
        result = [_serialize_transcript(t, session) for t in transcripts]
        session.close()
        return jsonify(result)
    except Exception as e:
        print(f"ERROR get_all_transcripts: {e}")
        return jsonify({'error': str(e)}), 500


@transcripts_bp.route('/api/student/transcripts', methods=['GET'])
@paseto_required
def get_student_transcripts():
    try:
        user_id = get_current_user_id()
        session = get_session()
        user    = session.query(User).filter_by(id=user_id).first()

        if user.role != UserRole.STUDENT:
            session.close(); return jsonify({'error': 'Accès réservé aux étudiants'}), 403

        transcripts = session.query(GradeTranscript).options(
            joinedload(GradeTranscript.semester).joinedload(Semester.formation)
        ).filter_by(student_id=user_id).order_by(GradeTranscript.generated_at.desc()).all()

        result = []
        for t in transcripts:
            if not getattr(t, 'is_published', False): continue
            generator_name = 'Système'
            if t.generated_by_id:
                g = session.query(User).filter_by(id=t.generated_by_id).first()
                if g: generator_name = g.full_name
            ue_data = None
            if t.ue_details:
                try: ue_data = json.loads(t.ue_details)
                except Exception: pass
            result.append({
                'id':               t.id,
                'semester_name':    t.semester.name   if t.semester else 'N/A',
                'semester_number':  t.semester.number if t.semester else None,
                'formation_name':   t.semester.formation.name if t.semester and t.semester.formation else 'N/A',
                'gpa':              t.gpa,
                'total_credits':    t.total_credits,
                'obtained_credits': t.obtained_credits,
                'validated':        (t.gpa >= 10) if t.gpa is not None else False,
                'ue_details':       ue_data,
                'generated_by':     generator_name,
                'generated_at':     t.generated_at.isoformat() if t.generated_at else None,
                'is_published':     True,
            })
        session.close()
        return jsonify(result)
    except Exception as e:
        print(f"ERROR get_student_transcripts: {e}")
        return jsonify({'error': str(e)}), 500


# ═══════════════════════════════════════════════════════════════════════════════
# EXPORT PDF
# ═══════════════════════════════════════════════════════════════════════════════

@transcripts_bp.route('/api/transcripts/<int:tid>/pdf', methods=['GET'])
@paseto_required
def export_transcript_pdf(tid):
    try:
        user_id = get_current_user_id()
        session = get_session()
        t = session.query(GradeTranscript).filter_by(id=tid).first()
        if not t: session.close(); return jsonify({'error': 'Relevé non trouvé'}), 404

        user = session.query(User).filter_by(id=user_id).first()
        if user.role == UserRole.STUDENT and t.student_id != user_id:
            session.close(); return jsonify({'error': 'Accès non autorisé'}), 403

        from utils import generate_transcript_pdf

        ue_details = None
        if t.ue_details:
            try: ue_details = json.loads(t.ue_details)
            except Exception: pass

        if not ue_details:
            # Fallback pour anciens relevés
            ue_map = {}
            for p in session.query(StudentPaper).join(Subject).join(EC).join(UE).filter(
                StudentPaper.student_id == t.student_id,
                UE.semester_id == t.semester_id, StudentPaper.score.isnot(None)
            ).all():
                ec = p.subject.ec
                if not ec: continue
                ue = ec.ue
                if ue.id not in ue_map:
                    ue_map[ue.id] = {'ue_code': ue.code, 'ue_name': ue.name,
                                     'credits': ue.credits, 'ecs': [], 'moyenne': None,
                                     'validated': None, 'validated_by_compensation': False, 'credits_acquis': 0}
                ue_map[ue.id]['ecs'].append({'ec_code': ec.code, 'ec_name': ec.name,
                                             'coefficient': ec.coefficient, 'note': p.score})
            for att in session.query(ExamAttempt).join(OnlineExam, ExamAttempt.exam_id == OnlineExam.id).join(
                Subject, OnlineExam.subject_id == Subject.id
            ).join(EC).join(UE).filter(
                ExamAttempt.student_id == t.student_id,
                UE.semester_id == t.semester_id, ExamAttempt.score.isnot(None)
            ).all():
                ec = att.exam.subject.ec if att.exam and att.exam.subject else None
                if not ec: continue
                ue = ec.ue
                if ue.id not in ue_map:
                    ue_map[ue.id] = {'ue_code': ue.code, 'ue_name': ue.name,
                                     'credits': ue.credits, 'ecs': [], 'moyenne': None,
                                     'validated': None, 'validated_by_compensation': False, 'credits_acquis': 0}
                ue_map[ue.id]['ecs'].append({'ec_code': ec.code, 'ec_name': ec.name,
                                             'coefficient': ec.coefficient, 'note': att.score})
            ue_details = list(ue_map.values())

        transcript_data = {
            'student_name':    t.student.full_name,
            'student_email':   t.student.email,
            'semester_name':   t.semester.name,
            'formation_name':  t.semester.formation.name if t.semester.formation else 'N/A',
            'gpa':             t.gpa,
            'total_credits':   t.total_credits,
            'obtained_credits': t.obtained_credits,
            'ue_details':      ue_details,
            'generated_at':    t.generated_at.strftime('%d/%m/%Y'),
        }

        pdf_path = f"exports/releve_{t.id}.pdf"
        generate_transcript_pdf(transcript_data, pdf_path)
        student_name = t.student.full_name
        session.close()
        return send_file(pdf_path, as_attachment=True,
                         download_name=f"releve_notes_{student_name}.pdf")
    except Exception as e:
        print(f"ERROR export_transcript_pdf: {e}")
        return jsonify({'error': str(e)}), 500


@transcripts_bp.route('/api/transcripts/bulk-pdf', methods=['GET'])
@paseto_required
def export_transcripts_bulk_pdf():
    try:
        user_id = get_current_user_id()
        session = get_session()
        user    = session.query(User).filter_by(id=user_id).first()

        if user.role == UserRole.STUDENT:
            session.close(); return jsonify({'error': 'Accès non autorisé'}), 403

        semester_id = request.args.get('semester_id', type=int)
        if not semester_id:
            session.close(); return jsonify({'error': 'semester_id requis'}), 400

        transcripts = session.query(GradeTranscript).filter_by(semester_id=semester_id).all()
        if not transcripts:
            session.close(); return jsonify({'error': 'Aucun relevé trouvé'}), 404

        from utils import generate_transcript_pdf

        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
            for t in transcripts:
                papers = session.query(StudentPaper).join(Subject).join(EC).join(UE).filter(
                    StudentPaper.student_id == t.student_id,
                    UE.semester_id == t.semester_id, StudentPaper.score.isnot(None)
                ).all()
                attempts = session.query(ExamAttempt).join(
                    OnlineExam, ExamAttempt.exam_id == OnlineExam.id
                ).join(Subject, OnlineExam.subject_id == Subject.id).join(EC).join(UE).filter(
                    ExamAttempt.student_id == t.student_id,
                    UE.semester_id == t.semester_id, ExamAttempt.score.isnot(None)
                ).all()

                notes_list = [{'ec_code': p.subject.ec.code if p.subject.ec else 'N/A',
                               'ec_name': p.subject.ec.name if p.subject.ec else p.subject.title,
                               'score': p.score, 'coefficient': p.subject.ec.coefficient if p.subject.ec else 1}
                              for p in papers]
                for att in attempts:
                    ec = att.exam.subject.ec if att.exam and att.exam.subject else None
                    notes_list.append({'ec_code': ec.code if ec else 'N/A',
                                       'ec_name': ec.name if ec else 'N/A',
                                       'score': att.score, 'coefficient': ec.coefficient if ec else 1})

                td = {'student_name': t.student.full_name, 'student_email': t.student.email,
                      'semester_name': t.semester.name,
                      'formation_name': t.semester.formation.name if t.semester.formation else 'N/A',
                      'gpa': t.gpa, 'total_credits': t.total_credits,
                      'obtained_credits': t.obtained_credits,
                      'papers': notes_list, 'generated_at': t.generated_at.strftime('%d/%m/%Y')}
                safe_name = t.student.full_name.replace(' ', '_').replace('/', '-')
                pdf_path  = f"exports/releve_{t.id}.pdf"
                generate_transcript_pdf(td, pdf_path)
                zf.write(pdf_path, arcname=f"releve_{safe_name}.pdf")

        semester_label = transcripts[0].semester.name.replace(' ', '_').replace('/', '-')
        session.close()
        zip_buffer.seek(0)
        return send_file(zip_buffer, mimetype='application/zip', as_attachment=True,
                         download_name=f"releves_{semester_label}.zip")
    except Exception as e:
        print(f"ERROR export_transcripts_bulk_pdf: {e}")
        return jsonify({'error': str(e)}), 500


# ═══════════════════════════════════════════════════════════════════════════════
# SUPPRESSION / PUBLICATION
# ═══════════════════════════════════════════════════════════════════════════════

@transcripts_bp.route('/api/transcripts/<int:tid>', methods=['DELETE'])
@paseto_required
def delete_transcript(tid):
    try:
        user_id = get_current_user_id()
        session = get_session()
        user    = session.query(User).filter_by(id=user_id).first()

        if user.role == UserRole.STUDENT:
            session.close(); return jsonify({'error': 'Les étudiants ne peuvent pas supprimer un relevé.'}), 403

        t = session.query(GradeTranscript).filter_by(id=tid).first()
        if not t: session.close(); return jsonify({'error': 'Relevé introuvable.'}), 404

        if user.role == UserRole.PROFESSOR and t.generated_by_id != user_id:
            session.close()
            return jsonify({'error': 'Vous ne pouvez supprimer que les relevés que vous avez générés.'}), 403

        student_name  = t.student.full_name  if t.student  else 'Inconnu'
        semester_name = t.semester.name      if t.semester else 'Inconnu'
        session.delete(t); session.commit(); session.close()
        return jsonify({'success': True,
                        'message': f'Relevé de {student_name} ({semester_name}) supprimé.'})
    except Exception as e:
        print(f"ERROR delete_transcript: {e}")
        return jsonify({'error': str(e)}), 500


@transcripts_bp.route('/api/transcripts/<int:tid>/publish', methods=['PUT'])
@paseto_required
def toggle_transcript_publish(tid):
    try:
        user_id = get_current_user_id()
        session = get_session()
        user    = session.query(User).filter_by(id=user_id).first()

        if user.role not in [UserRole.ADMIN, UserRole.PROFESSOR]:
            session.close(); return jsonify({'error': 'Accès non autorisé'}), 403

        t = session.query(GradeTranscript).filter_by(id=tid).first()
        if not t: session.close(); return jsonify({'error': 'Relevé introuvable'}), 404

        data = request.get_json() or {}
        t.is_published = bool(data.get('is_published', not t.is_published))
        session.commit()
        result = {'success': True, 'is_published': t.is_published}
        session.close()
        return jsonify(result)
    except Exception as e:
        print(f"ERROR toggle_transcript_publish: {e}")
        return jsonify({'error': str(e)}), 500
