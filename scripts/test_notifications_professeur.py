#!/usr/bin/env python
"""Page « Notifications » du professeur : suppression des alertes de la cloche (Redis) + contexte des affectations d'EC.
    cd cei-api-v2 && .venv-base/bin/python scripts/test_notifications_professeur.py   (crée puis supprime ses propres données)"""
import sys, uuid, json, time
sys.path.insert(0, '.')
from app import app
from models import *
from routes.exams import get_session
from auth_paseto import create_access_token
import redis as _r, os
from datetime import datetime

s = get_session(); sfx = uuid.uuid4().hex[:6]; c = app.test_client(use_cookies=False); RES = []; ids = {'users': [], 'asg': []}
R = _r.from_url(os.getenv('REDIS_URL', 'redis://127.0.0.1:6379/0'), decode_responses=True)
def chk(n, ok, d=''):
    RES.append(bool(ok)); print(('  ✔ ' if ok else '  ✘ ') + n + (f'  — {d}' if d and not ok else ''))
key = None
try:
    prof = User(email=f'zz_np_{sfx}@test.local', full_name='ZZ prof', role=UserRole.PROFESSOR, is_active=True, password_hash='x'); s.add(prof); s.commit(); ids['users'].append(prof.id)
    stu = User(email=f'zz_nps_{sfx}@test.local', full_name='ZZ etu', role=UserRole.STUDENT, is_active=True, password_hash='x'); s.add(stu); s.commit(); ids['users'].append(stu.id)
    H = {'Authorization': 'Bearer ' + create_access_token(prof.id, 'professor', prof.email)}
    HS = {'Authorization': 'Bearer ' + create_access_token(stu.id, 'student', stu.email)}
    key = f'cei:notif:history:{prof.id}'
    now = int(time.time() * 1000)
    for i, t in enumerate(['exam_no_proctor', 'fraud_alert', 'exam_no_proctor']):
        R.rpush(key, json.dumps({'type': t, 'title': f'T{i}', 'message': 'm', 'ts': now + i}))
    got = c.get('/api/notifications', headers=H).get_json()['notifications']
    chk('3 alertes listées', len(got) == 3, len(got))
    one = got[0]['id']
    r = c.post('/api/notifications/dismiss', headers=H, json={'ids': [one]})
    chk('suppression d\'une alerte', r.status_code == 200 and r.get_json()['removed'] == 1, r.get_json())
    left = [n['id'] for n in c.get('/api/notifications', headers=H).get_json()['notifications']]
    chk('il en reste 2 et l\'alerte supprimée a disparu', len(left) == 2 and one not in left, left)
    r = c.post('/api/notifications/dismiss', headers=H, json={})
    chk('sans ids ni all : 400', r.status_code == 400, r.status_code)
    r = c.post('/api/notifications/dismiss', headers=HS, json={'all': True})
    chk('étudiant : 403', r.status_code == 403, r.status_code)
    r = c.post('/api/notifications/dismiss', headers=H, json={'all': True})
    chk('tout supprimer', r.status_code == 200 and not c.get('/api/notifications', headers=H).get_json()['notifications'], r.get_json())
    # contexte d'affectation
    ec = s.query(EC).first(); a = ECAssignment(ec_id=ec.id, professor_id=prof.id, assigned_at=datetime.utcnow(), kind='tuteur'); s.add(a); s.commit(); ids['asg'].append(a.id)
    items = c.get('/api/professor/recent_incidents', headers=H).get_json()['incidents']
    it = [i for i in items if i['id'] == f'ec_assign_{a.id}']
    chk('affectation listée avec EC, UE, formation et rôle', bool(it) and it[0]['ec_code'] == ec.code and it[0]['ec_name'] == ec.name and it[0]['ue_name'] and it[0]['formation_name'] and it[0]['assignment_kind'] == 'tuteur', str(it)[:400])
finally:
    s.close()
    if key: R.delete(key)
    s2 = get_session()
    for i in ids['asg']:
        x = s2.get(ECAssignment, i)
        if x: s2.delete(x)
    s2.flush()
    for uid in ids['users']:
        s2.query(IncidentDismissal).filter_by(user_id=uid).delete(synchronize_session=False)
        u = s2.get(User, uid)
        if u: s2.delete(u)
    s2.commit(); print('données de test supprimées')
ok = sum(RES); print(f'\n{ok}/{len(RES)} vérifications réussies'); sys.exit(0 if ok == len(RES) else 1)
