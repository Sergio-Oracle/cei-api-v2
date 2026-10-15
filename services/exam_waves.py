"""Examens par vagues.

Un examen peut être passé en plusieurs vagues (créneaux successifs) pour étaler
la charge réseau : chaque vague a sa propre fenêtre d'accès, et chaque étudiant
est rattaché à une seule vague.

Principe de compatibilité : `OnlineExam.start_time` / `end_time` restent la
fenêtre ENVELOPPE (début de la première vague → fin de la dernière). Tout ce qui
lit l'examen dans son ensemble (surveillance, planning des groupes, calendrier
Moodle, clôture) continue donc de fonctionner. Seul ce qui concerne UN étudiant
(peut-il démarrer ? jusqu'à quand ? quand sa tentative est-elle en retard ?) passe
par `window_for` / `attempt_deadline` ci-dessous. Sans vague, ces fonctions
renvoient exactement la fenêtre de l'examen : aucun changement de comportement.
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

from models import (
    ExamWave, ExamWaveStudent, ExamAttempt, OnlineExam, StudentUEEnrollment,
    User, UserRole,
)


# ── Lecture ─────────────────────────────────────────────────────────────────

def exam_has_waves(session, exam_id: int) -> bool:
    return session.query(ExamWave.id).filter_by(exam_id=exam_id).first() is not None


def waves_of(session, exam_id: int) -> list:
    return session.query(ExamWave).filter_by(exam_id=exam_id).order_by(ExamWave.position).all()


def wave_of(session, exam_id: int, student_id: int):
    """Vague de l'étudiant pour cet examen (None s'il n'en a pas)."""
    return (session.query(ExamWave)
            .join(ExamWaveStudent, ExamWaveStudent.wave_id == ExamWave.id)
            .filter(ExamWaveStudent.exam_id == exam_id, ExamWaveStudent.student_id == student_id)
            .first())


def window_for(session, exam, student_id: int):
    """(début, fin, vague, vague_manquante) applicables à cet étudiant.

    Examen sans vague : fenêtre de l'examen, vague None, manquante False.
    Examen par vagues : fenêtre de SA vague ; s'il n'en a aucune, la fenêtre de
    l'examen est renvoyée avec manquante=True (l'appelant refuse l'accès)."""
    if not exam_has_waves(session, exam.id):
        return exam.start_time, exam.end_time, None, False
    w = wave_of(session, exam.id, student_id)
    if w is None:
        return exam.start_time, exam.end_time, None, True
    return w.start_time, w.end_time, w, False


def attempt_deadline(session, exam, attempt) -> datetime:
    """Échéance d'une tentative : fin de la vague de l'étudiant (ou de l'examen)
    plus le temps supplémentaire qui lui a été accordé."""
    _, end, _, _ = window_for(session, exam, attempt.student_id)
    return end + timedelta(minutes=attempt.extra_minutes or 0)


def wave_dict(w) -> dict:
    return {
        'id': w.id, 'position': w.position, 'name': w.name,
        'start_time': _iso(w.start_time), 'end_time': _iso(w.end_time),
    }


def _iso(dt):
    """Même format que OnlineExam.to_dict : UTC avec fuseau explicite."""
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc).isoformat() if dt.tzinfo is None else dt.isoformat()


def eligible_student_ids(session, exam) -> list:
    """Étudiants actifs inscrits à l'UE de l'EC du sujet (même règle que partout
    ailleurs dans la plateforme)."""
    ec = exam.subject.ec if exam.subject else None
    if not ec or not ec.ue_id:
        return []
    rows = (session.query(User.id)
            .join(StudentUEEnrollment, StudentUEEnrollment.student_id == User.id)
            .filter(StudentUEEnrollment.ue_id == ec.ue_id, User.is_active == True,
                    User.role == UserRole.STUDENT)
            .distinct().all())
    return [r[0] for r in rows]


def summary(session, exam) -> dict:
    """État des vagues : effectif, tentatives, étudiants éligibles sans vague."""
    waves = waves_of(session, exam.id)
    eligible = set(eligible_student_ids(session, exam))
    assigned = {sid for (sid,) in session.query(ExamWaveStudent.student_id).filter_by(exam_id=exam.id)}
    out = []
    for w in waves:
        ids = [sid for (sid,) in session.query(ExamWaveStudent.student_id).filter_by(wave_id=w.id)]
        started = (session.query(ExamAttempt.id)
                   .filter(ExamAttempt.exam_id == exam.id, ExamAttempt.student_id.in_(ids)).count()) if ids else 0
        d = wave_dict(w)
        d.update({'students': len(ids), 'attempts': started})
        out.append(d)
    return {
        'exam_id': exam.id,
        'enabled': bool(waves),
        'waves': out,
        'eligible': len(eligible),
        'unassigned': len(eligible - assigned),
        'unassigned_ids': sorted(eligible - assigned)[:200],
        'attempts_total': session.query(ExamAttempt.id).filter_by(exam_id=exam.id).count(),
    }


# ── Écriture ────────────────────────────────────────────────────────────────

class WaveError(ValueError):
    """Erreur de saisie, message prêt à afficher."""


def sync_envelope(session, exam) -> None:
    """L'enveloppe de l'examen couvre toutes les vagues."""
    waves = waves_of(session, exam.id)
    if not waves:
        return
    exam.start_time = min(w.start_time for w in waves)
    exam.end_time = max(w.end_time for w in waves)


def _order_students(session, ids, order: str, seed) -> list:
    if order == 'aleatoire':
        ids = list(ids)
        random.Random(seed if seed is not None else 0).shuffle(ids)
        return ids
    names = dict(session.query(User.id, User.full_name).filter(User.id.in_(ids)).all()) if ids else {}
    return sorted(ids, key=lambda i: ((names.get(i) or '').lower(), i))


def plan_waves(session, exam, count: int, first_start: datetime, window_minutes: int = None,
               gap_minutes: int = 15, order: str = 'alphabetique', seed=None) -> list:
    """(Re)crée `count` vagues consécutives et répartit équitablement les étudiants
    éligibles. Refusé dès qu'une tentative existe : on ne déplace pas des étudiants
    en cours d'examen."""
    if count < 2 or count > 40:
        raise WaveError("Le nombre de vagues doit être compris entre 2 et 40.")
    if session.query(ExamAttempt.id).filter_by(exam_id=exam.id).first():
        raise WaveError("Des étudiants ont déjà commencé cet examen : les vagues ne peuvent plus être redéfinies.")
    window = int(window_minutes or exam.duration_minutes or 0)
    if window < 5 or window > 600:
        raise WaveError("La durée d'une vague doit être comprise entre 5 et 600 minutes.")
    if window < (exam.duration_minutes or 0):
        raise WaveError("La fenêtre d'une vague ne peut pas être plus courte que la durée de l'examen "
                        f"({exam.duration_minutes} min).")
    gap = int(gap_minutes if gap_minutes is not None else 15)
    if gap < 0 or gap > 240:
        raise WaveError("L'écart entre deux vagues doit être compris entre 0 et 240 minutes.")
    if order not in ('alphabetique', 'aleatoire'):
        raise WaveError("Répartition inconnue (alphabetique ou aleatoire).")

    ids = eligible_student_ids(session, exam)
    if not ids:
        raise WaveError("Aucun étudiant éligible : vérifiez que l'EC du sujet est bien rattaché à une UE "
                        "avec des étudiants inscrits.")
    if len(ids) < count:
        raise WaveError(f"{len(ids)} étudiant(s) seulement pour {count} vagues.")

    # purge des vagues existantes (aucune tentative, donc sans conséquence)
    session.query(ExamWaveStudent).filter_by(exam_id=exam.id).delete(synchronize_session=False)
    session.query(ExamWave).filter_by(exam_id=exam.id).delete(synchronize_session=False)
    session.flush()

    ordered = _order_students(session, ids, order, seed if seed is not None else exam.id)
    n = len(ordered)
    base, extra = divmod(n, count)          # les premières vagues reçoivent 1 étudiant de plus si ça ne tombe pas juste
    waves, cursor = [], 0
    for i in range(count):
        start = first_start + timedelta(minutes=i * (window + gap))
        w = ExamWave(exam_id=exam.id, position=i + 1, name=f"Vague {i + 1}",
                     start_time=start, end_time=start + timedelta(minutes=window))
        session.add(w)
        session.flush()
        size = base + (1 if i < extra else 0)
        for sid in ordered[cursor:cursor + size]:
            session.add(ExamWaveStudent(wave_id=w.id, exam_id=exam.id, student_id=sid))
        cursor += size
        waves.append(w)
    session.flush()
    sync_envelope(session, exam)
    return waves


def update_wave(session, exam, wave, name=None, start_time=None, end_time=None) -> None:
    if name is not None:
        name = name.strip()
        if not name or len(name) > 80:
            raise WaveError("Nom de vague invalide.")
        wave.name = name
    new_start = start_time or wave.start_time
    new_end = end_time or wave.end_time
    if new_end <= new_start:
        raise WaveError("La fin d'une vague doit être postérieure à son début.")
    if (new_end - new_start) < timedelta(minutes=(exam.duration_minutes or 0)):
        raise WaveError(f"La fenêtre d'une vague ne peut pas être plus courte que la durée de l'examen "
                        f"({exam.duration_minutes} min).")
    wave.start_time, wave.end_time = new_start, new_end
    session.flush()
    sync_envelope(session, exam)


def move_students(session, exam, wave, student_ids) -> int:
    """Rattache des étudiants à `wave` (les retire de leur vague précédente).
    Refusé pour un étudiant qui a déjà commencé l'examen."""
    moved = 0
    for sid in {int(s) for s in student_ids}:
        if session.query(ExamAttempt.id).filter_by(exam_id=exam.id, student_id=sid).first():
            raise WaveError(f"L'étudiant #{sid} a déjà commencé l'examen : il ne peut plus changer de vague.")
        row = session.query(ExamWaveStudent).filter_by(exam_id=exam.id, student_id=sid).first()
        if row:
            if row.wave_id == wave.id:
                continue
            row.wave_id = wave.id
        else:
            session.add(ExamWaveStudent(wave_id=wave.id, exam_id=exam.id, student_id=sid))
        moved += 1
    return moved


def remove_waves(session, exam) -> None:
    if not exam_has_waves(session, exam.id):
        return                                   # rien à supprimer
    if session.query(ExamAttempt.id).filter_by(exam_id=exam.id).first():
        raise WaveError("Des étudiants ont déjà commencé cet examen : les vagues ne peuvent plus être supprimées.")
    session.query(ExamWaveStudent).filter_by(exam_id=exam.id).delete(synchronize_session=False)
    session.query(ExamWave).filter_by(exam_id=exam.id).delete(synchronize_session=False)
