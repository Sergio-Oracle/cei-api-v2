"""
Blueprint Professeur.

GET  /api/professor/dashboard
GET  /api/student/online_results
GET  /api/student/papers
"""
from flask import Blueprint, jsonify
from sqlalchemy import desc
from sqlalchemy.orm import joinedload

from auth_paseto import paseto_required, get_current_user_id
from extensions  import limiter
from models      import (
    get_session, User, UserRole,
    Subject, StudentPaper, Reclamation,
    OnlineExam, ExamAttempt,
    ECAssignment, ProctorGroupEC, ProctorGroupMember,
)

professor_bp = Blueprint('professor', __name__)


# ── Dashboard professeur ──────────────────────────────────────────────────────

@professor_bp.route('/api/professor/dashboard', methods=['GET'])
@paseto_required
@limiter.exempt
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

        # Retour DFIP — nombre de surveillants affectés aux ECs du professeur
        # (via les Groupes Surveillants rattachés), pour qu'il sache combien de
        # personnes surveilleront ses examens sans devoir ouvrir chaque groupe.
        ec_ids = [r[0] for r in session.query(ECAssignment.ec_id).filter_by(professor_id=user_id).all()]
        total_surveillants = 0
        active_surveillants = 0
        if ec_ids:
            group_ids = [r[0] for r in session.query(ProctorGroupEC.group_id).filter(ProctorGroupEC.ec_id.in_(ec_ids)).distinct().all()]
            if group_ids:
                proctor_ids = [r[0] for r in session.query(ProctorGroupMember.proctor_id).filter(
                    ProctorGroupMember.group_id.in_(group_ids)
                ).distinct().all()]
                total_surveillants = len(proctor_ids)
                if proctor_ids:
                    from proctoring_routes import get_proctor_status
                    for pid in proctor_ids:
                        status, _ = get_proctor_status(pid, session)
                        if status == 'engaged':
                            active_surveillants += 1

        session.close()
        return jsonify({
            'my_subjects':     my_subjects,
            'papers_corrected':papers_corrected + online_corrected,
            'total_surveillants': total_surveillants,
            'active_surveillants': active_surveillants,
        })
    except Exception as e:
        try: session.rollback(); session.close()
        except Exception: pass
        print(f"ERROR professor_dashboard: {e}")
        import traceback; traceback.print_exc()
        return jsonify({'error': str(e)}), 500


# ── Routes étudiant ───────────────────────────────────────────────────────────

@professor_bp.route('/api/student/online_results', methods=['GET'])
@paseto_required
@limiter.exempt
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

        # Correctif montée en charge (29/08, audit) : une requête Reclamation
        # par tentative dans la boucle ci-dessous, sur un endpoint exempté
        # de limite de fréquence — un seul aller-retour couvre tout.
        rec_by_attempt = {}
        if attempts:
            recs = session.query(Reclamation).filter(
                Reclamation.attempt_id.in_([a.id for a in attempts]),
                Reclamation.student_id == user_id,
            ).all()
            rec_by_attempt = {r.attempt_id: r for r in recs}

        results = []
        for att in attempts:
            exam    = att.exam
            subject = exam.subject if exam else None
            existing_rec = rec_by_attempt.get(att.id)
            # Retour #29/point 19 — notes masquées à l'étudiant tant que le
            # professeur/admin n'a pas explicitement publié les résultats.
            published = bool(exam.results_published) if exam else True
            results.append({
                'attempt_id':        att.id,
                'exam_id':           att.exam_id,
                'exam_title':        exam.title   if exam    else '—',
                'subject_title':     subject.title if subject else None,
                'score':             att.score if published else None,
                'feedback':          att.feedback if published else None,
                'corrected_at':      att.corrected_at.isoformat() if (att.corrected_at and published) else None,
                'submitted_at':      att.submitted_at.isoformat() if att.submitted_at else None,
                'auto_correct':      exam.auto_correct if exam else False,
                'has_reclamation':   existing_rec is not None,
                'reclamation_status':existing_rec.status.value if existing_rec else None,
                'results_published': published,
                'pending_publication': att.score is not None and not published,
            })

        session.close()
        return jsonify(results)
    except Exception as e:
        try: session.rollback(); session.close()
        except Exception: pass
        print(f"ERROR get_student_online_results: {e}")
        return jsonify([])


@professor_bp.route('/api/student/papers', methods=['GET'])
@paseto_required
@limiter.exempt
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
            # Retour #29 — note/grade masquées tant que le professeur n'a pas
            # explicitement publié la copie (après vérification manuelle).
            published = bool(p.is_published)
            if not published:
                d['score'] = None
                d['grade'] = None
                d['corrected_at'] = None
            d['pending_publication'] = p.score is not None and not published
            rec = recs_by_paper.get(p.id)
            d['has_reclamation']   = rec is not None
            d['reclamation_status']= rec.status.value if rec else None
            papers_list.append(d)

        session.close()
        return jsonify(papers_list)
    except Exception as e:
        try: session.rollback(); session.close()
        except Exception: pass
        print(f"ERROR get_student_papers: {e}")
        return jsonify({'error': str(e)}), 500
