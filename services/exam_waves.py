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

from sqlalchemy import event
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from models import (
    ExamWave, ExamWaveStudent, ExamAttempt, OnlineExam, StudentUEEnrollment,
    User, UserRole, EC, Subject, ExamStatus, AttemptStatus,
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


def window_for(session, exam, student_id: int, ensure: bool = False):
    """(début, fin, vague, vague_manquante) applicables à cet étudiant.

    Examen sans vague : fenêtre de l'examen, vague None, manquante False.
    Examen par vagues : fenêtre de SA vague ; s'il n'en a aucune, la fenêtre de
    l'examen est renvoyée avec manquante=True (l'appelant refuse l'accès).

    `ensure=True` (points d'entrée de l'étudiant) : un étudiant inscrit mais sans
    vague y est rattaché sur-le-champ (voir auto_assign_student) avant de conclure
    qu'il n'en a pas — filet de sécurité pour les inscriptions qui auraient
    échappé au rattachement automatique."""
    if not exam_has_waves(session, exam.id):
        return exam.start_time, exam.end_time, None, False
    w = wave_of(session, exam.id, student_id)
    if w is None and ensure:
        w = auto_assign_student(session, exam, student_id)
        if w is not None:
            session.commit()
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


# ── Rattachement automatique, prolongation, report ──────────────────────────

def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _is_enrolled(session, exam, student_id: int) -> bool:
    ec = exam.subject.ec if exam.subject else None
    if not ec or not ec.ue_id:
        return False
    return session.query(StudentUEEnrollment.id).join(User, User.id == StudentUEEnrollment.student_id).filter(
        StudentUEEnrollment.student_id == student_id, StudentUEEnrollment.ue_id == ec.ue_id,
        User.is_active == True, User.role == UserRole.STUDENT).first() is not None


def _headcounts(session, exam_id: int) -> dict:
    from sqlalchemy import func
    return dict(session.query(ExamWaveStudent.wave_id, func.count(ExamWaveStudent.id))
                .filter_by(exam_id=exam_id).group_by(ExamWaveStudent.wave_id).all())


def auto_assign_student(session, exam, student_id: int, now: datetime = None, counts: dict = None):
    """Rattache un étudiant inscrit à une vague, sans commit. Renvoie la vague ou None.

    Règle : la vague qui n'a PAS ENCORE COMMENCÉ la moins remplie (à effectif égal,
    la plus proche). Si toutes ont commencé, la vague en cours qui se termine le
    plus tard (l'étudiant garde du temps pour composer). Si toutes sont terminées,
    aucune : l'examen est passé pour lui. Un étudiant déjà rattaché, ayant déjà
    une tentative, ou non inscrit à l'UE de l'examen, n'est jamais touché."""
    if exam.status not in (ExamStatus.DRAFT, ExamStatus.SCHEDULED, ExamStatus.ACTIVE):
        return None
    existing = wave_of(session, exam.id, student_id)
    if existing is not None:
        return existing
    if session.query(ExamAttempt.id).filter_by(exam_id=exam.id, student_id=student_id).first():
        return None
    if not _is_enrolled(session, exam, student_id):
        return None
    waves = waves_of(session, exam.id)
    if not waves:
        return None
    now = now or _utcnow()
    if counts is None:
        counts = _headcounts(session, exam.id)
    upcoming = [w for w in waves if w.start_time > now]
    if upcoming:
        target = min(upcoming, key=lambda w: (counts.get(w.id, 0), w.start_time))
    else:
        running = [w for w in waves if w.end_time > now]
        if not running:
            return None
        target = max(running, key=lambda w: w.end_time)
    # INSERT direct, tolérant aux doublons : utilisable pendant un flush et sans risque de
    # faire échouer la transaction de l'appelant si deux requêtes rattachent le même étudiant.
    session.execute(pg_insert(ExamWaveStudent.__table__).values(
        wave_id=target.id, exam_id=exam.id, student_id=student_id
    ).on_conflict_do_nothing(index_elements=['exam_id', 'student_id']))
    counts[target.id] = counts.get(target.id, 0) + 1
    return target


def auto_assign_enrollment(session, student_id: int, ue_id: int, cache: dict = None) -> int:
    """Un étudiant vient d'être inscrit à l'UE `ue_id` : le rattacher à une vague de
    chaque examen par vagues (non terminé) de cette UE. Renvoie le nombre de rattachements."""
    exams = (session.query(OnlineExam).join(Subject, Subject.id == OnlineExam.subject_id)
             .join(EC, EC.id == Subject.ec_id)
             .join(ExamWave, ExamWave.exam_id == OnlineExam.id)
             .filter(EC.ue_id == ue_id,
                     OnlineExam.status.in_([ExamStatus.DRAFT, ExamStatus.SCHEDULED, ExamStatus.ACTIVE]))
             .distinct().all())
    n = 0
    for exam in exams:
        counts = None
        if cache is not None:                     # import en masse : effectifs tenus en mémoire
            if exam.id not in cache:
                cache[exam.id] = _headcounts(session, exam.id)
            counts = cache[exam.id]
        if auto_assign_student(session, exam, student_id, counts=counts) is not None:
            n += 1
    return n


def extend_waves(session, exam, minutes: int, wave_id: int = None, now: datetime = None) -> list:
    """Prolonge de `minutes` le créneau d'une vague (ou, sans `wave_id`, de toutes les
    vagues EN COURS). Les étudiants de ces vagues qui composent reçoivent le même
    temps en plus (extra_minutes), comme une prolongation classique. Les vagues à
    venir ne bougent pas. Renvoie les vagues prolongées."""
    if minutes <= 0 or minutes > 300:
        raise WaveError("Durée invalide (1–300 minutes).")
    now = now or _utcnow()
    waves = waves_of(session, exam.id)
    if wave_id is not None:
        targets = [w for w in waves if w.id == wave_id]
        if not targets:
            raise WaveError("Vague introuvable.")
        if targets[0].end_time <= now:
            raise WaveError("Cette vague est terminée : elle ne peut plus être prolongée.")
    else:
        targets = [w for w in waves if w.start_time <= now < w.end_time]
        if not targets:
            raise WaveError("Aucune vague n'est en cours : indiquez la vague à prolonger.")
    for w in targets:
        w.end_time = w.end_time + timedelta(minutes=minutes)
        ids = [sid for (sid,) in session.query(ExamWaveStudent.student_id).filter_by(wave_id=w.id)]
        if ids:
            for att in session.query(ExamAttempt).filter(
                    ExamAttempt.exam_id == exam.id, ExamAttempt.student_id.in_(ids),
                    ExamAttempt.status == AttemptStatus.IN_PROGRESS).all():
                att.extra_minutes = (att.extra_minutes or 0) + minutes
    session.flush()
    sync_envelope(session, exam)
    return targets


def reschedule_waves(session, exam, new_start: datetime = None, new_duration: int = None) -> None:
    """Reprogrammation d'un examen par vagues AVANT qu'il ne commence : toutes les vagues
    se décalent ensemble (écarts conservés) si la date de début change ; si la durée
    change, chaque fenêtre s'allonge/raccourcit d'autant et les vagues suivantes se
    recalent pour garder le même écart. Les étudiants restent dans leur vague."""
    if session.query(ExamAttempt.id).filter_by(exam_id=exam.id).first():
        raise WaveError("Des étudiants ont déjà commencé cet examen : il ne peut plus être reprogrammé.")
    waves = waves_of(session, exam.id)
    if not waves:
        return
    first_start = new_start or waves[0].start_time
    window = int((waves[0].end_time - waves[0].start_time).total_seconds() // 60)
    gap = int((waves[1].start_time - waves[0].end_time).total_seconds() // 60) if len(waves) > 1 else 0
    if new_duration is not None:
        new_duration = int(new_duration)
        if new_duration < 5:
            raise WaveError("Durée trop courte.")
        window = max(new_duration, window + (new_duration - (exam.duration_minutes or new_duration)))
        exam.duration_minutes = new_duration
    for i, w in enumerate(waves):
        w.start_time = first_start + timedelta(minutes=i * (window + gap))
        w.end_time = w.start_time + timedelta(minutes=window)
    session.flush()
    sync_envelope(session, exam)


# Rattachement automatique à l'inscription : un seul point d'accrochage pour TOUS les
# chemins qui créent une inscription (import CSV, SSO/Moodle, formations, inscription
# manuelle…) plutôt qu'un appel à retrouver dans chacun.
@event.listens_for(Session, 'after_flush')
def _collect_new_enrollments(session, flush_context):
    new = [(o.student_id, o.ue_id) for o in session.new if isinstance(o, StudentUEEnrollment)]
    if new:
        session.info.setdefault('_wave_enrollments', []).extend(new)


@event.listens_for(Session, 'after_flush_postexec')
def _assign_new_enrollments(session, flush_context):
    pending = session.info.pop('_wave_enrollments', None)
    if not pending:
        return
    try:
        if session.query(ExamWave.id).first() is None:
            return                                   # aucun examen par vagues : rien à faire (1 requête)
        cache = {}
        for sid, ue_id in pending:
            if sid and ue_id:
                auto_assign_enrollment(session, sid, ue_id, cache)
    except Exception as e:                           # ne jamais faire échouer une inscription
        print(f"[vagues] rattachement automatique ignoré: {e}")
