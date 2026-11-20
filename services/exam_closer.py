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
    """{id de tentative: échéance} — fin de la vague de l'étudiant (ou de l'examen) + temps supplémentaire.
    19/11/2026 : pour quelques copies seulement (sauvegarde, battement, remise : UNE copie), on ne lit que les lignes de CES étudiants. Avant, tout l'effectif des vagues de
    l'examen (3 000 à 10 000 lignes) était relu à CHAQUE sauvegarde et chaque battement : ce fut, avec 3 000 étudiants, la requête qui saturait les processeurs de la base."""
    from models import ExamWave, ExamWaveStudent
    ends = {}
    q = (session.query(ExamWaveStudent.student_id, ExamWave.end_time)
         .join(ExamWave, ExamWave.id == ExamWaveStudent.wave_id)
         .filter(ExamWaveStudent.exam_id == exam.id))
    sids = {a.student_id for a in attempts}
    if 0 < len(sids) <= 50:
        q = q.filter(ExamWaveStudent.student_id.in_(sids))
    for sid, end in q.all():
        ends[sid] = end
    from services.deadline_cap import deadline_extra_minutes      # le crédit de la pause (3 min) n'y compte pas
    return {a.id: ends.get(a.student_id, exam.end_time) + timedelta(minutes=deadline_extra_minutes(a)) for a in attempts}


def expired_attempt_ids(session, exam, now=None, grace=0):
    """([id des copies EN COURS dont l'échéance + `grace` secondes est dépassée], nombre de copies en cours) — en 2 requêtes LÉGÈRES (colonnes utiles seulement),
    quel que soit le nombre d'étudiants. 19/11/2026 : l'ancienne façon (une copie entière + 2 requêtes par copie pour son échéance) coûtait 12 000 requêtes et 12 s
    à chaque ouverture du tableau de bord pendant un examen par vagues de 3 000 copies (≈ 40 s à 10 000)."""
    now = now or _now()
    rows = (session.query(ExamAttempt.id, ExamAttempt.student_id, ExamAttempt.extra_minutes, ExamAttempt.pause_used)
            .filter(ExamAttempt.exam_id == exam.id, ExamAttempt.status == AttemptStatus.IN_PROGRESS).all())
    if not rows:
        return [], 0
    due = _deadlines(session, exam, rows)
    limit = timedelta(seconds=grace)
    return [r.id for r in rows if now > due[r.id] + limit], len(rows)


def wave_expiry_possible(session, exam, now=None, grace=0) -> bool:
    """Une copie en cours d'un examen PAR VAGUES a-t-elle dépassé l'échéance de sa vague (+ temps accordé) ? Un seul EXISTS en SQL, sans rien rapatrier :
    c'est le contrôle bon marché fait à CHAQUE ouverture du tableau de bord ; le balayage groupé ne tourne que s'il répond oui."""
    from sqlalchemy import func, case, and_, exists
    from models import ExamWave, ExamWaveStudent
    from services.deadline_cap import PAUSE_CREDIT_MINUTES
    now = now or _now()
    # Pré-contrôle minuscule : le temps accordé ne peut que RETARDER une échéance ; tant que la plus proche fin de vague n'est pas dépassée, aucune copie ne peut être échue.
    first_end = session.query(func.min(ExamWave.end_time)).filter(ExamWave.exam_id == exam.id).scalar()
    if first_end is None or first_end >= now - timedelta(seconds=grace):
        return False
    extra = func.greatest(func.coalesce(ExamAttempt.extra_minutes, 0) - case((ExamAttempt.pause_used == True, PAUSE_CREDIT_MINUTES), else_=0), 0)
    q = (session.query(ExamAttempt.id)
         .join(ExamWaveStudent, and_(ExamWaveStudent.exam_id == ExamAttempt.exam_id, ExamWaveStudent.student_id == ExamAttempt.student_id))
         .join(ExamWave, ExamWave.id == ExamWaveStudent.wave_id)
         .filter(ExamAttempt.exam_id == exam.id, ExamAttempt.status == AttemptStatus.IN_PROGRESS,
                 ExamWave.end_time + func.make_interval(0, 0, 0, 0, 0, extra) < now - timedelta(seconds=grace)))
    return session.query(q.exists()).scalar() is True


def submit_expired_bulk(session, exam, now=None, grace=0) -> int:
    """Soumet d'un seul UPDATE les copies en cours dont l'échéance est dépassée ; renvoie leur nombre. Ne valide pas la transaction."""
    now = now or _now()
    ids, _n = expired_attempt_ids(session, exam, now, grace)
    if ids:
        session.query(ExamAttempt).filter(ExamAttempt.id.in_(ids), ExamAttempt.status == AttemptStatus.IN_PROGRESS).update(
            {'status': AttemptStatus.AUTO_SUBMITTED, 'submitted_at': now}, synchronize_session=False)
    return len(ids)


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
            # Colonnes utiles d'abord (19/11/2026) : charger toutes les copies EN ENTIER (réponses comprises) toutes les 30 s pour 5 000 copies n'a pas de sens ;
            # seules les copies réellement échues sont lues en entier.
            expired_ids, n_running = expired_attempt_ids(session, exam, now, grace)
            expired = session.query(ExamAttempt).filter(ExamAttempt.id.in_(expired_ids)).all() if expired_ids else []
            running = range(n_running)
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


def purge_expired_tokens(batch: int = 20000, max_batches: int = 25) -> int:
    """Supprime les jetons de renouvellement révoqués DONT L'ÉCHÉANCE EST PASSÉE depuis plus d'un jour. 19/11/2026 : la table `token_blocklist` reçoit une ligne à CHAQUE renouvellement de
    session (176 000 lignes, 46 Mo, jamais purgée) — à 10 000 étudiants, ≈ 120 000 lignes par examen. Un jeton échu est de toute façon refusé (échéance) : sa ligne ne sert plus à rien.
    Par lots (jamais de long verrou). Renvoie le nombre de lignes supprimées."""
    from datetime import timezone
    from sqlalchemy import text as _text
    cut = datetime.now(timezone.utc) - timedelta(days=1)
    total = 0
    session = get_session()
    try:
        for _ in range(max_batches):
            n = session.execute(_text("DELETE FROM token_blocklist WHERE id IN (SELECT id FROM token_blocklist WHERE expires_at < :cut LIMIT :n)"), {'cut': cut, 'n': batch}).rowcount
            session.commit()
            total += n or 0
            if not n or n < batch:
                break
    except Exception as e:
        session.rollback()
        print(f"[exam_closer] purge des jetons : {e}")
    finally:
        session.close()
    return total


def check_backup_fresh() -> bool:
    """Prévient les administrateurs si la dernière sauvegarde VALIDE de la base a plus de 36 h (ou n'existe pas). 20/11/2026 : il n'y avait aucune sauvegarde automatique ; maintenant
    scripts/sauvegarde_base.sh (cron quotidien) pose un fichier LAST_OK après une sauvegarde vérifiée, et ce contrôle quotidien rend tout échec VISIBLE au lieu de silencieux.
    Inactif sauf CEI_BACKUP_WATCH=1 (réglé en production seulement : un poste de développement n'a pas de sauvegarde à surveiller). Renvoie True si une alerte a été émise."""
    if os.getenv('CEI_BACKUP_WATCH', '').strip().lower() not in ('1', 'true', 'yes'):
        return False
    path = os.path.join(os.getenv('BACKUP_DIR', '/home/serge/backups/db'), 'LAST_OK')
    try:
        age_h = (time.time() - os.path.getmtime(path)) / 3600.0
    except OSError:
        age_h = None
    if age_h is not None and age_h <= 36:
        return False
    from notif_bus import notify_admins
    detail = "aucune sauvegarde valide trouvée" if age_h is None else f"la dernière date de {age_h:.0f} h"
    notify_admins('backup', 'Sauvegarde de la base manquante', f"La sauvegarde quotidienne de la base n'a pas abouti : {detail}. Vérifiez le journal /home/serge/backups/db/sauvegarde.log.")
    print(f"[exam_closer] sauvegarde de la base : {detail}")
    return True


def tick() -> None:
    """Un seul serveur à la fois : le premier à poser le verrou travaille, les autres passent leur tour."""
    if not cache_set_nx('cei:exam_closer:tick', TICK_SECONDS - 5):
        return
    stats = close_expired()
    if stats['submitted'] or stats['closed'] or stats['rooms']:
        print(f"[exam_closer] {stats['submitted']} copie(s) soumise(s), {stats['closed']} examen(s) clos, "
              f"{stats['rooms']} salle(s) fermée(s)")
    rebalance_active_exams()
    if cache_set_nx('cei:vitals:tick', 55):                  # santé de l'examen : une fois par minute, prévient l'administrateur d'un problème critique (voir services/exam_vitals.py)
        try:
            from services import exam_vitals
            exam_vitals.watch_once()
        except Exception as e:
            print(f"[exam_closer] santé des examens : {e}")
    try:                                                      # contrôle sujet ↔ barème 30 puis 10 min avant chaque examen (silencieux si tout va bien)
        from services import exam_preflight
        exam_preflight.run_once()
    except Exception as e:
        print(f"[exam_closer] contrôle avant examen : {e}")
    if cache_set_nx('cei:backupwatch:daily', 24 * 3600):
        try:
            check_backup_fresh()
        except Exception as e:
            print(f"[exam_closer] contrôle de la sauvegarde : {e}")
    if cache_set_nx('cei:tokenpurge:daily', 24 * 3600):         # une fois par jour (verrou Redis)
        try:
            n = purge_expired_tokens()
            if n:
                print(f"[exam_closer] {n} jeton(s) de renouvellement expiré(s) purgé(s)")
        except Exception as e:
            print(f"[exam_closer] purge des jetons : {e}")
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
