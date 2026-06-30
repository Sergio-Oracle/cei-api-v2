"""
Blueprint Statistiques.

GET /api/statistics/<subject_id>
"""
import statistics as _stats
from flask import Blueprint, jsonify
from sqlalchemy import desc
from sqlalchemy.orm import joinedload

from auth_paseto import paseto_required, get_current_user_id
from models import (
    get_session, User, UserRole, Subject, StudentPaper,
    OnlineExam, ExamAttempt,
)

statistics_bp = Blueprint('statistics', __name__)


@statistics_bp.route('/api/statistics/<int:subject_id>', methods=['GET'])
@paseto_required
def get_subject_statistics(subject_id):
    try:
        user_id = get_current_user_id()
        session = get_session()
        user    = session.query(User).filter_by(id=user_id).first()

        if user.role not in [UserRole.PROFESSOR, UserRole.ADMIN]:
            session.close(); return jsonify({'error': 'Accès non autorisé'}), 403

        subject = session.query(Subject).filter_by(id=subject_id).first()
        if not subject: session.close(); return jsonify({'error': 'Sujet non trouvé'}), 404
        if user.role == UserRole.PROFESSOR and subject.creator_id != user_id:
            session.close()
            return jsonify({'error': 'Vous ne pouvez voir que les statistiques de vos propres sujets'}), 403

        papers = session.query(StudentPaper).options(
            joinedload(StudentPaper.student)
        ).filter_by(subject_id=subject_id).all()

        papers_details = [{
            'id':           p.id,
            'student_name': p.student.full_name  if p.student else 'Inconnu',
            'student_email':p.student.email      if p.student else 'N/A',
            'score':        p.score,
            'corrected_at': p.corrected_at.isoformat() if p.corrected_at else None,
            'filename':     p.filename,
            'type':         'paper',
        } for p in papers if p.score is not None]

        online_exams   = session.query(OnlineExam).filter_by(subject_id=subject_id).all()
        online_exam_ids= [e.id for e in online_exams]

        attempts_details = []
        if online_exam_ids:
            for att in session.query(ExamAttempt).options(
                joinedload(ExamAttempt.student),
                joinedload(ExamAttempt.exam)
            ).filter(
                ExamAttempt.exam_id.in_(online_exam_ids),
                ExamAttempt.score.isnot(None)
            ).all():
                attempts_details.append({
                    'id':           att.id,
                    'student_name': att.student.full_name if att.student else 'Inconnu',
                    'student_email':att.student.email     if att.student else 'N/A',
                    'score':        att.score,
                    'corrected_at': att.corrected_at.isoformat() if att.corrected_at
                                    else (att.submitted_at.isoformat() if att.submitted_at else None),
                    'exam_title':   att.exam.title if att.exam else '—',
                    'type':         'online',
                })

        online_exams_info = [{
            'id': e.id, 'title': e.title, 'status': e.status.value,
            'start_time': e.start_time.isoformat() if e.start_time else None,
            'attempts_count': sum(1 for a in attempts_details if a['exam_title'] == e.title),
        } for e in online_exams]

        _EMPTY = {
            'subject_id': subject_id, 'subject_title': subject.title,
            'totalStudents': 0, 'averageScore': 0, 'medianScore': 0,
            'minScore': 0, 'maxScore': 0, 'stdDeviation': 0, 'passRate': 0,
            'scoreDistribution': {'0-5': 0, '5-10': 0, '10-15': 0, '15-20': 0},
            'papers': [], 'attempts': [], 'online_exams': online_exams_info,
        }

        all_entries = papers_details + attempts_details
        if not all_entries:
            session.close(); return jsonify(_EMPTY)

        scores        = [e['score'] for e in all_entries]
        scores_sorted = sorted(scores)
        n             = len(scores_sorted)
        average       = sum(scores) / n
        median        = scores_sorted[n // 2] if n % 2 == 1 else (scores_sorted[n//2-1] + scores_sorted[n//2]) / 2
        std_dev       = _stats.stdev(scores) if n > 1 else 0
        pass_rate     = (sum(1 for s in scores if s >= 10) / n) * 100
        distribution  = {
            '0-5':   sum(1 for s in scores if 0  <= s <  5),
            '5-10':  sum(1 for s in scores if 5  <= s < 10),
            '10-15': sum(1 for s in scores if 10 <= s < 15),
            '15-20': sum(1 for s in scores if 15 <= s <= 20),
        }

        session.close()
        return jsonify({
            'subject_id':      subject_id,
            'subject_title':   subject.title,
            'totalStudents':   n,
            'averageScore':    round(average, 2),
            'medianScore':     round(median, 2),
            'minScore':        min(scores),
            'maxScore':        max(scores),
            'stdDeviation':    round(std_dev, 2),
            'passRate':        round(pass_rate, 2),
            'scoreDistribution': distribution,
            'papers':          papers_details,
            'attempts':        attempts_details,
            'online_exams':    online_exams_info,
        })
    except Exception as e:
        print(f"ERROR get_subject_statistics: {e}")
        import traceback; traceback.print_exc()
        return jsonify({'error': str(e)}), 500
