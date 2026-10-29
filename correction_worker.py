#!/usr/bin/env python
"""Service de correction IA en parallèle (processus à part, voir deploy/cei-correction.service).

Prend les copies déposées dans la file (services/correction_queue.py) et les corrige avec N travailleurs en même
temps (CORRECTION_WORKERS, 8 par défaut). La correction elle-même est celle de l'API, inchangée.

    cd cei-api-v2 && .venv-base/bin/python correction_worker.py
"""
import os
import signal
import sys
import threading
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'))
except Exception:
    pass

WORKERS = max(1, int(os.getenv('CORRECTION_WORKERS', '8')))
# Le client IA limite les appels simultanés par processus (2 par défaut, pour protéger les workers de l'API) :
# ce processus-ci est fait pour en lancer beaucoup, et attend son tour au lieu d'échouer.
os.environ['AI_CONCURRENCY'] = str(WORKERS)
os.environ['AI_ACQUIRE_TIMEOUT'] = '900'
# Chaque travailleur garde une connexion à la base pendant l'appel IA : le réservoir doit suivre.
os.environ['DB_POOL_SIZE'] = str(WORKERS + 4)
os.environ['DB_MAX_OVERFLOW'] = '4'

from cache import _get_client                                   # noqa: E402
from models import (get_session, ExamAttempt, OnlineExam, AttemptStatus, ExamActivityLog)  # noqa: E402
from services import correction_queue as Q                      # noqa: E402

stop = threading.Event()
_fail_streak = [0]
_lock = threading.Lock()
_stats = {'ok': 0, 'ko': 0, 'since': time.time()}
SUBMITTED = (AttemptStatus.SUBMITTED, AttemptStatus.AUTO_SUBMITTED)


def log(msg: str) -> None:
    print(f"[correction] {time.strftime('%H:%M:%S')} {msg}", flush=True)


def _state(attempt_id: int):
    """(copie soumise ?, déjà corrigée ?) lus en base."""
    s = get_session()
    try:
        a = s.query(ExamAttempt.status, ExamAttempt.score, ExamAttempt.corrected_at).filter_by(id=attempt_id).first()
        if a is None:
            return False, True                      # disparue : rien à faire
        return a.status in SUBMITTED, (a.score is not None and a.corrected_at is not None)
    finally:
        s.close()


def _manual(attempt_id: int, by_id: int) -> None:
    """Correction demandée par un enseignant/responsable (même séquence que le bouton « Corriger »)."""
    from sqlalchemy.orm import joinedload
    from routes import exams as R
    s = get_session()
    try:
        a = s.query(ExamAttempt).options(joinedload(ExamAttempt.exam).joinedload(OnlineExam.subject),
                                         joinedload(ExamAttempt.student)).filter_by(id=attempt_id).first()
        if a is None or a.status not in SUBMITTED:
            return
        R._run_ai_correction(a, corrected_by_id=by_id)
        s.commit()
        R._push_grades_if_published(a.exam)
    finally:
        s.close()


def _final_failure(attempt_id: int, meta: dict) -> None:
    """Dernier essai manqué : trace dans l'historique de la copie et alerte de l'enseignant (une seule fois)."""
    s = get_session()
    try:
        a = s.query(ExamAttempt).filter_by(id=attempt_id).first()
        if a is None or a.score is not None:
            return
        exam = s.query(OnlineExam).filter_by(id=a.exam_id).first()
        import json
        s.add(ExamActivityLog(attempt_id=attempt_id, event_type='auto_correction_failed',
                              event_data=json.dumps({'error': f"abandon après {Q.MAX_TRIES} essais",
                                                     'timestamp': time.strftime('%Y-%m-%dT%H:%M:%S')})))
        s.commit()
        creator, title = (exam.created_by_id, exam.title) if exam else (None, f'Examen #{a.exam_id}')
    finally:
        s.close()
    if creator and meta.get('by') is not None:           # corrections automatiques : déjà signalées par l'API
        try:
            from notif_bus import notify_user
            notify_user(creator, 'auto_correction_failed', 'Échec de la correction automatique',
                        f'La correction IA de la copie #{attempt_id} ({title}) a échoué — notation manuelle requise.',
                        priority='high', tags=['warning'])
        except Exception:
            pass


def _batch_done(exam_id) -> None:
    if exam_id is None:
        return
    who = Q.pop_batch(exam_id)
    if not who:
        return
    s = get_session()
    try:
        total = s.query(ExamAttempt).filter(ExamAttempt.exam_id == exam_id, ExamAttempt.status.in_(SUBMITTED)).count()
        corrected = s.query(ExamAttempt).filter(ExamAttempt.exam_id == exam_id, ExamAttempt.status.in_(SUBMITTED),
                                                ExamAttempt.score.isnot(None)).count()
        title = s.query(OnlineExam.title).filter_by(id=exam_id).scalar() or f'Examen #{exam_id}'
    finally:
        s.close()
    try:
        from notif_bus import notify_user
        notify_user(who, 'scheduled_correction_done', 'Correction terminée',
                    f'La correction de « {title} » est terminée : {corrected} copie(s) corrigée(s) sur {total}'
                    + (f', {total - corrected} en attente ou en échec' if total > corrected else '') + '.',
                    priority='default', tags=['clipboard-check'], extra={'exam_id': exam_id})
    except Exception:
        pass


def process(attempt_id: int, meta: dict) -> None:
    from routes import exams as R
    submitted, corrected = _state(attempt_id)
    if corrected or not submitted:
        Q.done(attempt_id, meta)
        _batch_done(meta.get('exam'))
        return
    last_try = int(meta.get('tries', 0)) + 1 >= Q.MAX_TRIES
    t0 = time.time()
    try:
        if meta.get('by'):
            _manual(attempt_id, int(meta['by']))
        else:
            R._run_auto_correction(attempt_id, notify_failure=last_try)
    except Exception as e:
        log(f"copie {attempt_id} : {e}")
        traceback.print_exc()
    _, corrected = _state(attempt_id)
    with _lock:
        if corrected:
            _fail_streak[0] = 0
            _stats['ok'] += 1
        else:
            _fail_streak[0] += 1
            _stats['ko'] += 1
            if _fail_streak[0] >= 6:                       # disjoncteur : le fournisseur IA ne répond plus
                _get_client().setex(Q.PAUSE, 60, '1')
                _fail_streak[0] = 0
                log("6 échecs de suite : pause d'une minute")
    if corrected:
        Q.done(attempt_id, meta)
        log(f"copie {attempt_id} corrigée en {time.time() - t0:.0f}s")
    elif Q.retry(attempt_id, meta):
        log(f"copie {attempt_id} : échec, nouvel essai plus tard ({meta['tries']}/{Q.MAX_TRIES - 1})")
        return
    else:
        _final_failure(attempt_id, meta)
        Q.fail(attempt_id, meta)
        log(f"copie {attempt_id} : abandonnée après {Q.MAX_TRIES} essais")
    _batch_done(meta.get('exam'))


def worker_loop() -> None:
    while not stop.is_set():
        try:
            c = _get_client()
            if c is None:
                time.sleep(5)
                continue
            if c.exists(Q.PAUSE):
                time.sleep(5)
                continue
            job = Q.pop()
            if job is None:
                stop.wait(1.0)
                continue
            process(*job)
        except Exception as e:
            log(f"travailleur : {e}")
            stop.wait(3.0)


def supervisor_loop() -> None:
    last = time.time()
    while not stop.is_set():
        try:
            c = _get_client()
            c.setex(Q.ALIVE, 30, str(WORKERS))
            Q.reap()
            if time.time() - last >= 60:
                last = time.time()
                with _lock:
                    ok, ko = _stats['ok'], _stats['ko']
                log(f"file : {Q.queue_size()} en attente, {Q.running_count()} en cours — {ok} corrigées, {ko} échecs depuis le démarrage")
        except Exception as e:
            log(f"supervision : {e}")
        stop.wait(10.0)


def main() -> None:
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    back = Q.reap_all()
    log(f"démarré — {WORKERS} travailleurs en parallèle" + (f", {back} copie(s) interrompue(s) reprise(s)" if back else ''))
    threading.Thread(target=supervisor_loop, daemon=True).start()
    threads = [threading.Thread(target=worker_loop, name=f'corr-{i}') for i in range(WORKERS)]
    for t in threads:
        t.start()
    while not stop.is_set():
        stop.wait(1.0)
    log("arrêt demandé : fin des corrections en cours…")
    _get_client().delete(Q.ALIVE)
    deadline = time.time() + 120
    for t in threads:
        t.join(max(0.0, deadline - time.time()))
    log("arrêté")


if __name__ == '__main__':
    main()
