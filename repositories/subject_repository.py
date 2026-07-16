"""SubjectRepository — all SQL for Subject objects lives here."""
from __future__ import annotations
from typing import Optional

from sqlalchemy import desc
from sqlalchemy.orm import joinedload

from models import (
    get_session, Subject, EC, User, UserRole,
    OnlineExam, ExamAttempt, ExamActivityLog,
    CameraLog, ProctorAssignment, ExamProctor,
    StudentPaper, Reclamation, CorrectionHistory,
)
from .base import db_session


class SubjectRepository:

    # ── Read ──────────────────────────────────────────────────────────────────

    @staticmethod
    def find_all(user_id: int, role: UserRole) -> list[dict]:
        session = get_session()
        try:
            query = session.query(Subject).options(
                joinedload(Subject.ec).joinedload(EC.ue),
                joinedload(Subject.creator),
            )
            if role == UserRole.STUDENT:
                subjects = query.filter_by(is_active=True).order_by(desc(Subject.created_at)).all()
            elif role == UserRole.PROFESSOR:
                subjects = query.filter(Subject.creator_id == user_id).order_by(desc(Subject.created_at)).all()
            else:
                subjects = query.order_by(desc(Subject.created_at)).all()

            result = []
            for s in subjects:
                d = s.to_dict()
                d['papers_count'] = len(s.papers)       if s.papers       else 0
                d['exam_count']   = len(s.online_exams) if s.online_exams else 0
                result.append(d)
            return result
        finally:
            session.close()

    @staticmethod
    def find_by_id(subject_id: int) -> Optional[Subject]:
        session = get_session()
        try:
            return session.query(Subject).options(
                joinedload(Subject.ec).joinedload(EC.ue),
                joinedload(Subject.creator),
            ).filter_by(id=subject_id).first()
        finally:
            session.close()

    @staticmethod
    def find_by_id_dict(subject_id: int) -> Optional[dict]:
        session = get_session()
        try:
            subj = session.query(Subject).options(
                joinedload(Subject.ec).joinedload(EC.ue),
                joinedload(Subject.creator),
            ).filter_by(id=subject_id).first()
            return subj.to_dict() if subj else None
        finally:
            session.close()

    # ── Write ─────────────────────────────────────────────────────────────────

    @staticmethod
    def create(
        title: str,
        content: str,
        rubric: str,
        filename: str,
        creator_id: int,
        ec_id: Optional[int] = None,
    ) -> dict:
        with db_session() as session:
            subj = Subject(
                title=title, content=content, rubric=rubric,
                filename=filename, creator_id=creator_id,
                ec_id=ec_id,
            )
            session.add(subj)
            session.flush()          # get subj.id before commit
            result = subj.to_dict()
        return result

    @staticmethod
    def update_image(subject_id: int, image_url: str) -> None:
        with db_session() as session:
            subj = session.query(Subject).filter_by(id=subject_id).first()
            if subj:
                subj.image_url = image_url

    @staticmethod
    def has_locked_exam(subject_id: int) -> bool:
        """Un sujet ne doit plus être modifié une fois lié à un examen déjà
        actif/clôturé ou ayant reçu au moins une tentative — sinon le contenu
        vu par l'étudiant pendant/après l'examen divergerait de la correction."""
        session = get_session()
        try:
            exams = session.query(OnlineExam).filter_by(subject_id=subject_id).all()
            for exam in exams:
                if exam.status.value in ('active', 'closed'):
                    return True
                if session.query(ExamAttempt).filter_by(exam_id=exam.id).first():
                    return True
            return False
        finally:
            session.close()

    @staticmethod
    def update_content(subject_id: int, title: Optional[str] = None,
                        content: Optional[str] = None, rubric: Optional[str] = None) -> dict:
        with db_session() as session:
            subj = session.query(Subject).options(
                joinedload(Subject.ec).joinedload(EC.ue),
                joinedload(Subject.creator),
            ).filter_by(id=subject_id).first()
            if not subj:
                raise LookupError('Sujet non trouvé')
            if title is not None:
                subj.title = title
            if content is not None:
                subj.content = content
            if rubric is not None:
                subj.rubric = rubric
            session.flush()
            result = subj.to_dict()
        return result

    # ── Delete ────────────────────────────────────────────────────────────────

    @staticmethod
    def delete_with_cascade(subject_id: int) -> None:
        """Delete a subject and all its dependent records."""
        with db_session() as session:
            for exam in session.query(OnlineExam).filter_by(subject_id=subject_id).all():
                attempt_ids = [
                    a.id for a in session.query(ExamAttempt.id).filter_by(exam_id=exam.id).all()
                ]
                if attempt_ids:
                    session.query(CameraLog).filter(
                        CameraLog.attempt_id.in_(attempt_ids)).delete(synchronize_session=False)
                    session.query(ExamActivityLog).filter(
                        ExamActivityLog.attempt_id.in_(attempt_ids)).delete(synchronize_session=False)
                    session.query(ProctorAssignment).filter(
                        ProctorAssignment.attempt_id.in_(attempt_ids)).delete(synchronize_session=False)
                    session.query(Reclamation).filter(
                        Reclamation.attempt_id.in_(attempt_ids)).update(
                        {'attempt_id': None}, synchronize_session=False)
                session.query(ProctorAssignment).filter_by(exam_id=exam.id).delete(synchronize_session=False)
                session.query(ExamProctor).filter_by(exam_id=exam.id).delete(synchronize_session=False)
                session.query(ExamAttempt).filter_by(exam_id=exam.id).delete(synchronize_session=False)
                session.delete(exam)

            for paper in session.query(StudentPaper).filter_by(subject_id=subject_id).all():
                session.query(Reclamation).filter_by(paper_id=paper.id).delete(synchronize_session=False)
                session.query(CorrectionHistory).filter_by(paper_id=paper.id).delete(synchronize_session=False)
                session.delete(paper)

            subj = session.query(Subject).filter_by(id=subject_id).first()
            if subj:
                session.delete(subj)
