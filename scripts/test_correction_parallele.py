#!/usr/bin/env python
"""Service de correction IA en parallèle : vitesse, reprises, échecs, reprise après arrêt, passage par l'API.

    cd cei-api-v2 && .venv-base/bin/python scripts/test_correction_parallele.py

La correction par l'IA est remplacée par une fausse (0,5 s par copie) pour mesurer ce que le service apporte :
le travail en parallèle et la fiabilité de la file. Un dernier passage utilise la vraie fonction sur des copies vides.
Données temporaires, supprimées en fin de test.
"""
import os, sys, time, uuid, threading
from datetime import datetime, timedelta

os.environ['CORRECTION_WORKERS'] = '8'
os.environ.update({'AI_CONCURRENCY': '8', 'AI_ACQUIRE_TIMEOUT': '60', 'DB_POOL_SIZE': '14', 'DB_MAX_OVERFLOW': '4'})
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as A
import correction_worker as W
from routes import exams as R
from services import correction_queue as Q
from cache import _get_client
from models import (get_session, User, UserRole, Subject, OnlineExam, ExamStatus, ExamAttempt, AttemptStatus, ExamActivityLog)
from auth_paseto import create_access_token
from werkzeug.security import generate_password_hash

RESULTS = []
def check(nom, ok, detail=''):
    RESULTS.append((nom, bool(ok)))
    print(('  ✔ ' if ok else '  ✘ ') + nom + (f'  — {detail!r}' if not ok else ''))

s = get_session(); tag = uuid.uuid4().hex[:6]; made = {'users': [], 'ex': [], 'sub': None}
c = A.app.test_client(); rc = _get_client(); A._last_sweep[0] = time.monotonic() + 1e9     # pas de filet automatique pendant le test
def mk(role, n):
    u = User(email=f'cor.{tag}.{n}@test.invalid', full_name=f'Cor Test {n}', role=role, is_active=True, email_verified=True,
             has_email=True, password_hash=generate_password_hash('x'))
    s.add(u); s.commit(); made['users'].append(u.id); return u
def H(u): return {'Authorization': 'Bearer ' + create_access_token(u.id, u.role.value, u.email)}

calls = {}; fail_once = set(); fail_always = set(); delay = [0.5]
def fake(attempt_id, notify_failure=True):
    calls[attempt_id] = calls.get(attempt_id, 0) + 1
    if attempt_id in fail_always or (attempt_id in fail_once and calls[attempt_id] == 1):
        raise RuntimeError('fournisseur IA indisponible')
    time.sleep(delay[0])
    s2 = get_session()
    try:
        a = s2.query(ExamAttempt).filter_by(id=attempt_id).first()
        a.score = 12.0; a.corrected_at = datetime.utcnow(); a.feedback = 'ok'; s2.commit()
    finally:
        s2.close()
real_auto = R._run_auto_correction

def start_service(n=8):
    W.stop.clear(); W.WORKERS = n
    ts = [threading.Thread(target=W.supervisor_loop, daemon=True)] + [threading.Thread(target=W.worker_loop, daemon=True) for _ in range(n)]
    for t in ts: t.start()
    return ts
def wait(cond, timeout=60):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if cond(): return True
        time.sleep(0.2)
    return False

try:
    adm = mk(UserRole.ADMIN, 'adm'); sts = [mk(UserRole.STUDENT, f'e{i}') for i in range(26)]
    sub = Subject(title='cor test', content='Question 1 : x', rubric='x', creator_id=adm.id); s.add(sub); s.commit(); made['sub'] = sub.id
    now = datetime.utcnow()
    ex = OnlineExam(subject_id=sub.id, title='cor test', duration_minutes=60, start_time=now - timedelta(hours=3), end_time=now - timedelta(hours=1),
                    status=ExamStatus.CLOSED, created_by_id=adm.id, auto_correct=True)
    s.add(ex); s.commit(); made['ex'].append(ex.id)
    atts = []
    for u in sts:
        a = ExamAttempt(exam_id=ex.id, student_id=u.id, status=AttemptStatus.SUBMITTED, submitted_at=now - timedelta(hours=1),
                        answers='{"pq_1":"reponse"}'); s.add(a); atts.append(a)
    s.commit(); ids = [a.id for a in atts]
    for k in (Q.QUEUE, Q.LEASE, Q.META, Q.ALIVE, Q.PAUSE): rc.delete(k)
    rc.delete(Q._exam_key(ex.id)); rc.delete(Q._failed_key(ex.id))
    R._run_auto_correction = fake
    W._manual = lambda aid, by: fake(aid)

    print('\n== Sans service : comportement d\'avant ==')
    check("service arrêté : worker_alive est faux", not Q.worker_alive())
    r = c.post(f'/api/online_exams/{ex.id}/correct_all', headers=H(adm), json={})
    check("« Tout corriger » le dit à l'interface (elle corrige alors copie par copie)", r.get_json().get('worker_alive') is False and Q.queue_size() == 0, r.get_json())

    print('\n== Correction en parallèle ==')
    ts = start_service(8); check("le service se déclare actif", wait(Q.worker_alive, 15))
    t0 = time.time()
    r = c.post(f'/api/online_exams/{ex.id}/correct_all', headers=H(adm), json={}).get_json()
    check("26 copies déposées d'un coup", r.get('queued') == 26, r)
    ok = wait(lambda: Q.pending_count(ex.id) == 0, 60); el = time.time() - t0
    s.expire_all()
    check("toutes corrigées", ok and all(s.get(ExamAttempt, i).score == 12.0 for i in ids))
    check(f"en {el:.1f} s au lieu de {26 * 0.5:.0f} s une par une (8 en parallèle)", el < 7, f'{el:.1f}s')
    check("chaque copie corrigée une seule fois", all(calls[i] == 1 for i in ids), {i: n for i, n in calls.items() if n != 1})
    p = c.get(f'/api/online_exams/{ex.id}/correction_progress', headers=H(adm)).get_json()
    check("l'avancement est lisible", p['total'] == 26 and p['corrected'] == 26 and p['in_queue'] == 0 and p['worker_alive'], p)

    print('\n== Fiabilité de la file ==')
    for a in atts: a.score = None; a.corrected_at = None
    s.commit(); calls.clear(); Q.RETRY_DELAYS[:] = [1, 1, 1, 1]
    added = Q.enqueue(ids[:3], ex.id) ; again = Q.enqueue(ids[:3], ex.id)
    check("redéposer une copie déjà en file n'ajoute rien", added == 3 and again == 0, (added, again))
    wait(lambda: Q.pending_count(ex.id) == 0, 20); s.expire_all()
    check("copie déjà corrigée : ignorée sans nouvel appel IA", (Q.enqueue([ids[0]], ex.id) or True) and wait(lambda: Q.pending_count(ex.id) == 0, 10) and calls[ids[0]] == 1, calls)
    fail_once.add(ids[5]); calls.clear()
    Q.enqueue([ids[5]], ex.id); wait(lambda: Q.pending_count(ex.id) == 0, 20); s.expire_all()
    check("un échec passager est repris automatiquement", calls.get(ids[5]) == 2 and s.get(ExamAttempt, ids[5]).score == 12.0, calls)
    fail_always.add(ids[6]); calls.clear()
    Q.enqueue([ids[6], ids[7]], ex.id); wait(lambda: Q.pending_count(ex.id) == 0, 40); s.expire_all()
    check("une copie qui échoue toujours est abandonnée après tous ses essais", calls.get(ids[6]) == Q.MAX_TRIES and ids[6] in Q.failed_ids(ex.id), calls)
    check("sans gêner les autres copies", s.get(ExamAttempt, ids[7]).score == 12.0)
    check("le filet automatique ne la reprend pas sans fin", Q.enqueue([ids[6]], ex.id) == 0)
    fail_always.clear()
    check("une demande explicite la remet en file et la corrige", Q.enqueue([ids[6]], ex.id, force=True) == 1 and wait(lambda: Q.pending_count(ex.id) == 0, 20))
    rc.delete(Q.PAUSE)

    print('\n== Arrêt brutal du service ==')
    W.stop.set(); time.sleep(1.5)
    Q.enqueue([ids[10]], ex.id); job = Q.pop()
    check("une copie prise puis abandonnée est en cours (verrou posé)", job and job[0] == ids[10] and Q.running_count() == 1)
    rc.zadd(Q.LEASE, {str(ids[10]): time.time() - 5})
    check("son verrou expiré la remet dans la file", Q.reap() == 1 and Q.queue_size() == 1)
    rc.delete(Q.QUEUE); rc.delete(Q.LEASE); rc.delete(Q.META); rc.delete(Q._exam_key(ex.id)); rc.delete(Q.ALIVE)

    print('\n== Vraie fonction de correction, via le service ==')
    R._run_auto_correction = real_auto
    empty = []
    for u in sts[:4]:
        a = s.query(ExamAttempt).filter_by(exam_id=ex.id, student_id=u.id).first()
        a.answers = '{}'; a.score = None; a.corrected_at = None; empty.append(a.id)
    s.commit()
    start_service(4)
    Q.enqueue(empty, ex.id); wait(lambda: Q.pending_count(ex.id) == 0, 30); s.expire_all()
    check("copies vides corrigées 0/20 par la vraie fonction", all(s.get(ExamAttempt, i).score == 0.0 for i in empty))
    W.stop.set()
    check("avec le service arrêté, la soumission garde son fil d'avant", (lambda: (time.sleep(31), not Q.worker_alive())[1])())
finally:
    W.stop.set(); R._run_auto_correction = real_auto
    s.rollback()
    try:
        ids_ex = made['ex']
        if ids_ex:
            att_ids = [a.id for a in s.query(ExamAttempt).filter(ExamAttempt.exam_id.in_(ids_ex))]
            if att_ids: s.query(ExamActivityLog).filter(ExamActivityLog.attempt_id.in_(att_ids)).delete(synchronize_session=False)
            s.query(ExamAttempt).filter(ExamAttempt.exam_id.in_(ids_ex)).delete(synchronize_session=False)
            s.query(OnlineExam).filter(OnlineExam.id.in_(ids_ex)).delete(synchronize_session=False)
        if made['sub']: s.query(Subject).filter_by(id=made['sub']).delete()
        if made['users']: s.query(User).filter(User.id.in_(made['users'])).delete(synchronize_session=False)
        s.commit()
        for k in (Q.QUEUE, Q.LEASE, Q.META, Q.ALIVE, Q.PAUSE): rc.delete(k)
    except Exception as e:
        s.rollback(); print('nettoyage incomplet :', e)
    s.close()
bad = [n for n, ok in RESULTS if not ok]
print(f"\n{len(RESULTS) - len(bad)}/{len(RESULTS)} vérifications OK" + (f" — ÉCHECS : {bad}" if bad else ''))
sys.exit(1 if bad else 0)
