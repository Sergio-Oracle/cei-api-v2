"""Rattrapage d'un examen : faire recomposer des étudiants qui n'ont pas pu composer.

Cas réel : des étudiants sans moyens (pas de connexion, pas de matériel) ou bloqués avant d'avoir vu leur copie n'ont pas composé.
Jusqu'ici la plateforme ne permettait pas de les faire passer à nouveau. Le personnel choisit ces étudiants et une nouvelle fenêtre ;
ils composent le MÊME sujet, dans le MÊME examen, sur ce créneau — les autres étudiants ne sont pas touchés.

Principe (réutilise les vagues, voir services/exam_waves.py) :
  - l'examen passe en mode « vagues » : la fenêtre d'origine devient la vague « Session normale » (tous les autres étudiants y restent) ;
  - une vague « Rattrapage » est créée avec la nouvelle fenêtre et SEULEMENT les étudiants choisis ;
  - la tentative vide d'un étudiant jamais affiché (aucune copie) est retirée pour qu'il reparte de zéro ;
  - l'examen est rouvert jusqu'à la fin du rattrapage ; les étudiants sont prévenus (notification + e-mail) ;
  - une copie réelle (vue ou avec des réponses) n'est JAMAIS effacée : ces étudiants ne sont pas candidats.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy.dialects.postgresql import insert as pg_insert

from models import (
    ExamWave, ExamWaveStudent, ExamAttempt, ExamAccessCode, ExamActivityLog, CameraLog, ProctorAssignment,
    OnlineExam, User, ExamStatus, AttemptStatus,
)
from services import exam_waves as W

MAKEUP_NAME = 'Rattrapage'


def _real_attempt(session, exam, attempt) -> bool:
    """Vrai si l'étudiant a une vraie copie (vue ou avec réponses), ou compose encore."""
    from routes.exams import _never_seen
    if attempt is None:
        return False
    if not _never_seen(attempt):
        return True
    # tentative vide mais encore dans son temps : il peut encore entrer, on ne la retire pas
    if attempt.status == AttemptStatus.IN_PROGRESS and W.attempt_deadline(session, exam, attempt) > W._utcnow():
        return True
    return False


def candidates(session, exam, search: str = '', limit: int = 200, offset: int = 0) -> dict:
    """Étudiants inscrits à l'examen qui n'ont aucune copie : jamais entrés, ou bloqués avant d'avoir vu les questions."""
    eligible = W.eligible_student_ids(session, exam)
    if not eligible:
        return {'candidates': [], 'total': 0}
    attempts = {a.student_id: a for a in session.query(ExamAttempt).filter(
        ExamAttempt.exam_id == exam.id, ExamAttempt.student_id.in_(eligible)).all()}
    ids = [sid for sid in eligible if not _real_attempt(session, exam, attempts.get(sid))]
    q = session.query(User.id, User.full_name, User.email).filter(User.id.in_(ids)) if ids else None
    rows = q.all() if q is not None else []
    s = (search or '').strip().lower()
    if s:
        rows = [r for r in rows if s in (r[1] or '').lower() or s in (r[2] or '').lower()]
    rows.sort(key=lambda r: ((r[1] or '').lower(), r[0]))
    total = len(rows)
    page = rows[max(0, offset):max(0, offset) + max(1, min(limit, 500))]
    return {'candidates': [{'id': r[0], 'name': r[1], 'email': r[2]} for r in page], 'total': total}


def schedule_makeup(session, exam, student_ids, start: datetime, end: datetime, name: str = None) -> dict:
    """Crée (ou complète) la vague de rattrapage. Sans commit : l'appelant valide. Lève WaveError si la demande est invalide."""
    name = (name or MAKEUP_NAME).strip()[:80] or MAKEUP_NAME
    try:
        ids = sorted({int(i) for i in (student_ids or [])})
    except (TypeError, ValueError):
        raise W.WaveError("Liste d'étudiants invalide.")
    if not ids:
        raise W.WaveError("Choisissez au moins un étudiant.")
    if len(ids) > 3000:
        raise W.WaveError("Trop d'étudiants d'un coup (3 000 au maximum).")
    if start is None or end is None or end <= start:
        raise W.WaveError("La fin du rattrapage doit être postérieure à son début.")
    if start < W._utcnow() - timedelta(minutes=1):
        raise W.WaveError("Le rattrapage doit commencer dans le futur.")
    if (end - start) < timedelta(minutes=(exam.duration_minutes or 0)):
        raise W.WaveError(f"La fenêtre de rattrapage ne peut pas être plus courte que la durée de l'examen ({exam.duration_minutes} min).")
    if exam.status == ExamStatus.DRAFT:
        raise W.WaveError("Cet examen n'a pas encore eu lieu : modifiez simplement ses horaires.")

    eligible = set(W.eligible_student_ids(session, exam))
    if any(i not in eligible for i in ids):
        raise W.WaveError("Certains étudiants ne sont pas inscrits à cet examen.")
    attempts = {a.student_id: a for a in session.query(ExamAttempt).filter(
        ExamAttempt.exam_id == exam.id, ExamAttempt.student_id.in_(ids)).all()}
    blocked = [i for i in ids if _real_attempt(session, exam, attempts.get(i))]
    if blocked:
        raise W.WaveError(f"{len(blocked)} étudiant(s) ont déjà une copie : seuls ceux qui n'ont pas composé peuvent passer le rattrapage.")

    # 1) Mode « vagues » : la fenêtre d'origine devient la session normale, tous les autres y restent
    waves = W.waves_of(session, exam.id)
    if not waves:
        first = ExamWave(exam_id=exam.id, position=1, name='Session normale', start_time=exam.start_time, end_time=exam.end_time)
        session.add(first)
        session.flush()
        waves = [first]
    base_wave = min(waves, key=lambda w: w.start_time)
    already = {sid for (sid,) in session.query(ExamWaveStudent.student_id).filter_by(exam_id=exam.id)}
    with_attempt = {sid for (sid,) in session.query(ExamAttempt.student_id).filter_by(exam_id=exam.id)}
    selected = set(ids)
    for sid in (eligible | with_attempt) - already - selected:
        session.execute(pg_insert(ExamWaveStudent.__table__).values(
            wave_id=base_wave.id, exam_id=exam.id, student_id=sid
        ).on_conflict_do_nothing(index_elements=['exam_id', 'student_id']))

    # 2) Les tentatives vides des étudiants choisis sont retirées : ils repartent de zéro
    removed = 0
    for sid, att in attempts.items():
        session.query(ExamAccessCode).filter_by(attempt_id=att.id).delete(synchronize_session=False)
        session.query(ExamActivityLog).filter_by(attempt_id=att.id).delete(synchronize_session=False)
        session.query(CameraLog).filter_by(attempt_id=att.id).delete(synchronize_session=False)
        session.query(ProctorAssignment).filter_by(attempt_id=att.id).update({'attempt_id': None}, synchronize_session=False)
        session.delete(att)
        removed += 1
    session.flush()

    # 3) La vague de rattrapage (une seule par créneau : si une vague porte déjà ce nom et ces horaires, on la complète)
    wave = next((w for w in waves if w.name == name and w.start_time == start and w.end_time == end), None)
    if wave is None:
        wave = ExamWave(exam_id=exam.id, position=max(w.position for w in waves) + 1, name=name, start_time=start, end_time=end)
        session.add(wave)
        session.flush()
    W.move_students(session, exam, wave, ids)
    W.sync_envelope(session, exam)

    # 4) L'examen est rouvert jusqu'à la fin du rattrapage
    reopened = False
    if exam.status == ExamStatus.CLOSED:
        exam.status = ExamStatus.ACTIVE
        reopened = True
    if reopened:
        try:
            from cache import cache_delete
            cache_delete(f'cei:room_closed:{exam.id}')
        except Exception:
            pass
    W.queue_notices(session, exam.id, ids, 'assigned')
    out = W.summary(session, exam)
    out.update({'makeup_wave_id': wave.id, 'students': len(ids), 'attempts_removed': removed, 'reopened': reopened})
    return out
