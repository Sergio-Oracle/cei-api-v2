"""
Service Examens — couche Modèle du MVC.

Logique métier des examens en ligne : scoring, progression, statuts.
"""
from __future__ import annotations
from datetime import datetime, timezone

from models import (
    OnlineExam, ExamAttempt, ExamStatus, AttemptStatus,
    QuestionBank, get_session
)


def get_exam(exam_id: int) -> OnlineExam | None:
    with get_session() as session:
        return session.query(OnlineExam).filter_by(id=exam_id).first()


def list_exams(formation_id: int | None = None,
               status: str | None = None) -> list[dict]:
    with get_session() as session:
        q = session.query(OnlineExam)
        if formation_id:
            q = q.filter(OnlineExam.formation_id == formation_id)
        if status:
            q = q.filter(OnlineExam.status == ExamStatus(status))
        exams = q.order_by(OnlineExam.start_time.desc()).all()
        return [_serialize_exam(e) for e in exams]


def score_attempt(attempt_id: int) -> float:
    """Calculer et persister la note d'une tentative (QCM uniquement)."""
    with get_session() as session:
        attempt = session.query(ExamAttempt).filter_by(id=attempt_id).first()
        if not attempt:
            return 0.0

        questions = (session.query(QuestionBank)
                     .filter_by(exam_id=attempt.exam_id)
                     .all())
        if not questions:
            return 0.0

        total_points = sum(q.points or 1 for q in questions)
        earned = 0.0

        answers = attempt.answers or {}
        for q in questions:
            student_answer = answers.get(str(q.id))
            if student_answer is not None and student_answer == q.correct_answer:
                earned += (q.points or 1)

        score = round((earned / total_points) * 20, 2) if total_points else 0.0
        attempt.score = score
        attempt.status = AttemptStatus.submitted
        attempt.submitted_at = datetime.now(timezone.utc)
        session.commit()
        return score


def get_attempt_progress(attempt_id: int) -> dict:
    with get_session() as session:
        attempt = session.query(ExamAttempt).filter_by(id=attempt_id).first()
        if not attempt:
            return {}
        total = (session.query(QuestionBank)
                 .filter_by(exam_id=attempt.exam_id).count())
        answered = len(attempt.answers or {})
        return {
            'attempt_id':      attempt.id,
            'total_questions': total,
            'answers_count':   answered,
            'status':          attempt.status.value if hasattr(attempt.status, 'value') else attempt.status,
            'score':           attempt.score,
        }


def _serialize_exam(e: OnlineExam) -> dict:
    return {
        'id':           e.id,
        'title':        e.title,
        'description':  e.description,
        'status':       e.status.value if hasattr(e.status, 'value') else e.status,
        'start_time':   e.start_time.isoformat() if e.start_time else None,
        'end_time':     e.end_time.isoformat()   if e.end_time   else None,
        'formation_id': e.formation_id,
        'subject_id':   e.subject_id,
        'duration_minutes': e.duration_minutes,
    }
