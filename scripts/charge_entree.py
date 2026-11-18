#!/usr/bin/env python
"""Banc de charge de l'ENTRÉE en examen (18/11/2026) : N étudiants simultanés font démarrer, recharger la page 2 fois (reprise sans code), afficher la copie (/seen) et lire le sujet.

    cd cei-api-v2 && .venv-base/bin/python scripts/charge_entree.py 600      (serveur LOCAL uniquement ; données temporaires supprimées à la fin)
"""
import sys, os, json, time, uuid, threading, statistics, ssl, urllib.request
sys.path.insert(0, '/root/cei-api-v2'); os.chdir('/root/cei-api-v2')
from app import app
from models import *
from auth_paseto import create_access_token
from werkzeug.security import generate_password_hash
from datetime import datetime, timedelta
N = int(sys.argv[1]); tag = uuid.uuid4().hex[:6]
s = get_session(); ec = s.query(EC).filter(EC.ue_id.isnot(None)).first()
adm = User(email=f'ch.{tag}.adm@test.invalid', full_name='Charge adm', role=UserRole.ADMIN, is_active=True, email_verified=True, has_email=True, password_hash='x'); s.add(adm); s.commit()
sub = Subject(title='charge', content='1. Q ? [QCM]\nA. a\nB. b\n', rubric='Question 1 (1 pt) : A', creator_id=adm.id, ec_id=ec.id); s.add(sub); s.commit()
now = datetime.utcnow()
ex = OnlineExam(subject_id=sub.id, title='charge entrée', duration_minutes=60, start_time=now - timedelta(minutes=5), end_time=now + timedelta(hours=2), status=ExamStatus.ACTIVE, created_by_id=adm.id); s.add(ex); s.commit()
users = []
for i in range(N):
    u = User(email=f'ch.{tag}.{i}@test.invalid', full_name=f'Charge {i}', role=UserRole.STUDENT, is_active=True, email_verified=True, has_email=True, password_hash='x'); users.append(u)
s.add_all(users); s.commit()
s.add_all([StudentUEEnrollment(student_id=u.id, ue_id=ec.ue_id) for u in users]); s.commit()
toks = [create_access_token(u.id, u.role.value, u.email) for u in users]
ids = [u.id for u in users]; exid = ex.id; admid = adm.id; subid = sub.id; s.close()
ctx = ssl._create_unverified_context()
BASE = 'https://127.0.0.1'; HOST = 'dev-cei.ddns.net'
lat = {'start': [], 'reprise': [], 'seen': [], 'subject': []}; err = []; lock = threading.Lock()
def call(method, path, tok, body=None):
    req = urllib.request.Request(BASE + path, method=method, data=(json.dumps(body or {}).encode() if method == 'POST' else None), headers={'Authorization': 'Bearer ' + tok, 'Content-Type': 'application/json', 'Host': HOST})
    t = time.time()
    try:
        r = urllib.request.urlopen(req, context=ctx, timeout=60); data = json.load(r); code = r.status
    except urllib.error.HTTPError as e:
        code = e.code; data = {}
    return code, data, (time.time() - t) * 1000
barrier = threading.Barrier(N)
def student(i):
    tok = toks[i]
    try:
        barrier.wait()
        c, d, ms = call('POST', f'/api/online_exams/{exid}/start', tok); 
        with lock: lat['start'].append(ms)
        if c not in (200, 201): err.append(('start', c)); return
        aid = d['attempt']['id']
        for _ in range(2):                                                  # rechargements avant l'affichage de la copie
            c, d, ms = call('POST', f'/api/online_exams/{exid}/start', tok)
            with lock: lat['reprise'].append(ms)
            if c != 200 or d.get('code_required'): err.append(('reprise', c, d.get('code_required')))
        c, d, ms = call('POST', f'/api/exam_attempts/{aid}/seen', tok)
        with lock: lat['seen'].append(ms)
        if c != 200: err.append(('seen', c))
        c, d, ms = call('GET', f'/api/exam_attempts/{aid}/subject', tok)
        with lock: lat['subject'].append(ms)
        if c != 200: err.append(('subject', c))
    except Exception as e:
        err.append(('exc', str(e)[:80]))
t0 = time.time(); th = [threading.Thread(target=student, args=(i,)) for i in range(N)]
[t.start() for t in th]; [t.join() for t in th]; dur = time.time() - t0
print(f"{N} étudiants simultanés (4 appels chacun, dont 2 rechargements) en {dur:.1f} s ; erreurs : {len(err)} {err[:3]}")
for k, v in lat.items():
    v.sort(); p = lambda q: v[min(len(v) - 1, int(len(v) * q))]
    print(f"  {k:8s} n={len(v):4d}  p50={p(.5):7.0f} ms  p90={p(.9):7.0f} ms  p99={p(.99):7.0f} ms  max={v[-1]:7.0f} ms")
# nettoyage
s = get_session()
from sqlalchemy import text
aids = [a.id for a in s.query(ExamAttempt).filter_by(exam_id=exid)]
if aids:
    s.query(ExamAccessCode).filter(ExamAccessCode.attempt_id.in_(aids)).delete(synchronize_session=False)
    s.query(ExamActivityLog).filter(ExamActivityLog.attempt_id.in_(aids)).delete(synchronize_session=False)
s.query(ExamAttempt).filter_by(exam_id=exid).delete(); s.query(OnlineExam).filter_by(id=exid).delete()
s.query(StudentUEEnrollment).filter(StudentUEEnrollment.student_id.in_(ids)).delete(synchronize_session=False)
s.query(Subject).filter_by(id=subid).delete(); s.query(User).filter(User.id.in_(ids + [admid])).delete(synchronize_session=False); s.commit(); print('nettoyé')
