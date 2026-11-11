"""Clôture automatique des examens : à l'heure de fin (et seulement à celle-là), les copies encore en cours
sont soumises par le serveur et la salle vidéo de l'examen est fermée.

Jusqu'ici, cette clôture n'avait lieu que lorsque quelqu'un chargeait la liste des examens ; sans lecture de la
liste, des étudiants pouvaient continuer à composer après l'heure, et leur salle restait ouverte avec des
participants « fantômes ». Une tâche de fond (une seule à la fois sur tout le serveur, voir `ensure_loop`) :

- soumet toute tentative dont l'échéance personnelle est dépassée (fin de l'examen ou de la vague de l'étudiant,
  plus le temps supplémentaire accordé) ;
- ferme l'examen une fois son heure de fin passée ;
- ferme la salle LiveKit dès qu'il ne reste plus aucune copie en cours (ou en retire l'étudiant, si d'autres
  composent encore) ;
- répartit les étudiants entre les surveillants présents, même si aucun surveillant ne parvient à joindre l'API.

Les routes d'enregistrement et de lecture de l'étudiant appliquent la même échéance (`autosubmit_if_expired`),
pour qu'aucune réponse ne soit acceptée longtemps après l'heure même si cette tâche avait du retard.
"""
import json
import os
import threading
import time
import urllib.request as urlreq
from datetime import datetime, timedelta

import jwt as pyjwt

from cache import cache_get, cache_set, cache_set_nx
from models import (get_session, OnlineExam, ExamStatus, ExamAttempt, AttemptStatus)

TICK_SECONDS = 30
# Marge laissée au navigateur de l'étudiant pour envoyer lui-même sa dernière copie à l'échéance : le serveur
# ne soumet à sa place qu'au bout de ce délai (la boucle) ou d'un délai plus court quand l'étudiant écrit encore.
LOOP_GRACE_SECONDS = 60
ROUTE_GRACE_SECONDS = 30

_started = False
_lock = threading.Lock()


def _now() -> datetime:
    return datetime.utcnow()


def _deadlines(session, exam, attempts) -> dict:
    """{id de tentative: échéance} — fin de la vague de l'étudiant (ou de l'examen) + temps supplémentaire."""
    from models import ExamWave, ExamWaveStudent
    ends = {}
    rows = (session.query(ExamWaveStudent.student_id, ExamWave.end_time)
            .join(ExamWave, ExamWave.id == ExamWaveStudent.wave_id)
            .filter(ExamWaveStudent.exam_id == exam.id).all())
    for sid, end in rows:
        ends[sid] = end
    return {a.id: ends.get(a.student_id, exam.end_time) + timedelta(minutes=a.extra_minutes or 0) for a in attempts}


def autosubmit_if_expired(session, attempt, exam=None, grace=ROUTE_GRACE_SECONDS) -> bool:
    """Soumet la tentative si son échéance (+ marge) est dépassée. Renvoie True si elle vient d'être soumise.
    Ne valide pas elle-même la transaction : l'appelant s'en charge."""
    if attempt.status != AttemptStatus.IN_PROGRESS:
        return False
    exam = exam or attempt.exam
    if not exam or not exam.end_time:
        return False
    try:
        due = _deadlines(session, exam, [attempt])[attempt.id]
    except Exception:
        return False
    if _now() > due + timedelta(seconds=grace):
        attempt.status = AttemptStatus.AUTO_SUBMITTED
        attempt.submitted_at = _now()
        return True
    return False


# ── LiveKit ───────────────────────────────────────────────────────────────────

def _livekit(method: str, payload: dict, room: str):
    """Appel administrateur à l'API LiveKit (silencieux : LiveKit injoignable ne doit jamais bloquer la clôture)."""
    try:
        from proctoring_routes import get_livekit_config
        cfg = get_livekit_config()
        if not (cfg['api_url'] and cfg['api_key'] and cfg['api_secret']):
            return None
        now = int(time.time())
        token = pyjwt.encode(
            {'exp': now + 60, 'iss': cfg['api_key'], 'nbf': now, 'sub': 'cei-closer',
             'video': {'roomAdmin': True, 'roomCreate': True, 'roomList': True, 'room': room}},
            cfg['api_secret'], algorithm='HS256')
        req = urlreq.Request(f"{cfg['api_url']}/twirp/livekit.RoomService/{method}",
                             data=json.dumps(payload).encode(),
                             headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'})
        with urlreq.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read() or b'{}')
    except Exception as e:
        print(f"[exam_closer] LiveKit {method} {room} : {e}")
        return None


def close_room(exam_id: int) -> None:
    room = f'exam-{exam_id}'
    _livekit('DeleteRoom', {'room': room}, room)
    cache_set(f'cei:room_closed:{exam_id}', '1', ttl=7 * 86400)


def remove_student(exam_id: int, student_id: int) -> None:
    room = f'exam-{exam_id}'
    for identity in (f'student-{student_id}', f'student-{student_id}-phone'):
        _livekit('RemoveParticipant', {'room': room, 'identity': identity}, room)


# ── Passage périodique ────────────────────────────────────────────────────────

def close_expired(grace=LOOP_GRACE_SECONDS) -> dict:
    """Un passage : soumissions, fermeture d'examen, fermeture de salle. Renvoie les compteurs."""
    stats = {'submitted': 0, 'closed': 0, 'rooms': 0}
    session = get_session()
    after_commit = []                      # appels LiveKit à faire une fois la base à jour
    to_correct = []                        # (examen, copies) à confier au service de correction
    try:
        now = _now()
        from sqlalchemy import exists
        from services import exam_waves as _W
        running_attempt = exists().where(ExamAttempt.exam_id == OnlineExam.id,
                                         ExamAttempt.status == AttemptStatus.IN_PROGRESS)
        # Examens à traiter : ouverts et dont l'heure de fin est passée, clos avec des copies encore en cours (temps
        # supplémentaire), ou clos depuis moins de 6 h (fermeture de la salle) — jamais tout l'historique.
        exams = session.query(OnlineExam).filter(
            ((OnlineExam.status == ExamStatus.ACTIVE) & (OnlineExam.end_time < now))
            | ((OnlineExam.status == ExamStatus.CLOSED) & (running_attempt | (OnlineExam.end_time > now - timedelta(hours=6))))
        ).all()
        # Examens par vagues encore ouverts : une vague terminée est soumise sans attendre la dernière.
        waves = session.query(OnlineExam).filter(
            OnlineExam.status == ExamStatus.ACTIVE, OnlineExam.end_time >= now).all()
        exams += [e for e in waves if _W.exam_has_waves(session, e.id)]
        for exam in exams:
            running = session.query(ExamAttempt).filter_by(exam_id=exam.id, status=AttemptStatus.IN_PROGRESS).all()
            due = _deadlines(session, exam, running) if running else {}
            expired = [a for a in running if now > due[a.id] + timedelta(seconds=grace)]
            from routes.exams import _never_seen
            for a in expired:
                a.status = AttemptStatus.AUTO_SUBMITTED
                a.submitted_at = now
                if a.exam_seen_at is None and not _never_seen(a):
                    a.exam_seen_at = now                          # des réponses existent : il a vu sa copie (signal manqué)
            stats['submitted'] += len(expired)
            seen_expired = [a.id for a in expired if a.exam_seen_at is not None]      # jamais affichée et sans réponse = n'a pas composé : pas de correction, pas de 0/20
            if seen_expired and exam.auto_correct:
                to_correct.append((exam.id, seen_expired))
            if exam.status == ExamStatus.ACTIVE and exam.end_time < now:
                exam.status = ExamStatus.CLOSED
                stats['closed'] += 1
            left = len(running) - len(expired)
            if exam.status == ExamStatus.CLOSED and left == 0 and not cache_get(f'cei:room_closed:{exam.id}'):
                after_commit.append(('room', exam.id, None))
            elif expired:
                after_commit.extend(('student', exam.id, a.student_id) for a in expired)
        session.commit()
    except Exception as e:
        session.rollback()
        print(f"[exam_closer] clôture : {e}")
    finally:
        session.close()
    if to_correct:
        try:
            from services import correction_queue as _cq
            if _cq.worker_alive():
                for exam_id, ids in to_correct:
                    _cq.enqueue(ids, exam_id)
        except Exception as e:
            print(f"[exam_closer] file de correction : {e}")
    for kind, exam_id, student_id in after_commit:
        if kind == 'room':
            close_room(exam_id)
            stats['rooms'] += 1
        else:
            remove_student(exam_id, student_id)
    return stats


def rebalance_active_exams() -> None:
    """Répartition des étudiants entre surveillants présents pour chaque examen en cours — sans dépendre du
    fait qu'un surveillant parvienne à joindre l'API (voir proctoring_routes._check_disconnected_proctors)."""
    from proctoring_routes import _check_disconnected_proctors
    session = get_session()
    notes = []
    try:
        for (exam_id,) in session.query(OnlineExam.id).filter_by(status=ExamStatus.ACTIVE).all():
            try:
                notes.extend(_check_disconnected_proctors(exam_id, session))
            except Exception as e:
                session.rollback()
                print(f"[exam_closer] répartition examen {exam_id} : {e}")
    finally:
        session.close()
    try:
        from notif_bus import notify_exam
        for n in notes:
            notify_exam(n['exam_id'], 'proctor_disconnected', n['title'], n['message'],
                        priority=n.get('priority', 'default'), tags=n.get('tags', []))
    except Exception:
        pass


def tick() -> None:
    """Un seul serveur à la fois : le premier à poser le verrou travaille, les autres passent leur tour."""
    if not cache_set_nx('cei:exam_closer:tick', TICK_SECONDS - 5):
        return
    stats = close_expired()
    if stats['submitted'] or stats['closed'] or stats['rooms']:
        print(f"[exam_closer] {stats['submitted']} copie(s) soumise(s), {stats['closed']} examen(s) clos, "
              f"{stats['rooms']} salle(s) fermée(s)")
    rebalance_active_exams()
    if cache_set_nx('cei:certwatch:daily', 24 * 3600):
        try:
            from services import cert_watch
            cert_watch.check()
        except Exception as e:
            print(f"[exam_closer] certificats : {e}")


def _loop() -> None:
    while True:
        try:
            tick()
        except Exception as e:
            print(f"[exam_closer] passage : {e}")
        time.sleep(TICK_SECONDS)


def ensure_loop() -> None:
    """Démarre la tâche de fond dans ce processus (appelée à la première requête de chaque worker)."""
    global _started
    if _started or os.environ.get('CEI_EXAM_CLOSER') == '0':
        return
    with _lock:
        if _started:
            return
        _started = True
        threading.Thread(target=_loop, name='exam-closer', daemon=True).start()
