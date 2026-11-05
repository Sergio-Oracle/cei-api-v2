#!/usr/bin/env python
"""Alerte « seuil de surveillance atteint » : une seule par copie et par 10 min (avant : une notification URGENTE à CHAQUE
événement suivant dès que le seuil d'absences de visage était franchi, bannissement automatique désactivé).
    cd cei-api-v2 && .venv-base/bin/python scripts/test_alerte_seuil.py   (crée puis supprime ses propres données)"""
import sys, uuid
sys.path.insert(0, '.')
from unittest import mock
from app import app
from models import *
from routes.exams import get_session
from auth_paseto import create_access_token
from cache import cache_delete
from datetime import datetime, timedelta
import proctoring_routes, notif_bus, cache

s = get_session(); sfx = uuid.uuid4().hex[:6]; now = datetime.utcnow(); c = app.test_client(use_cookies=False); RES = []; ids = {'users': [], 'exams': []}
def chk(n, ok, d=''):
    RES.append(bool(ok)); print(('  ✔ ' if ok else '  ✘ ') + n + (f'  — {d}' if d and not ok else ''))
try:
    adm = s.query(User).filter(User.role == UserRole.ADMIN).first()
    stu = User(email=f'zz_seuil_{sfx}@test.local', full_name='ZZ seuil', role=UserRole.STUDENT, is_active=True, password_hash='x'); s.add(stu); s.commit(); ids['users'].append(stu.id)
    e = OnlineExam(subject_id=s.query(Subject).first().id, title='TEST seuil', duration_minutes=60, start_time=now - timedelta(minutes=5), end_time=now + timedelta(minutes=55),
                   status=ExamStatus.ACTIVE, created_by_id=adm.id, max_no_face_count=1, auto_ban_enabled=False); s.add(e); s.commit(); ids['exams'].append(e.id)
    a = ExamAttempt(exam_id=e.id, student_id=stu.id, status=AttemptStatus.IN_PROGRESS, started_at=now); s.add(a); s.commit(); aid = a.id
    H = {'Authorization': 'Bearer ' + create_access_token(stu.id, 'student', stu.email)}; P = f'/api/exam_attempts/{aid}/log_activity'
    pushes = []; notifs = []
    def fake_push(d): pushes.append(d)
    def fake_notify(*a, **k): notifs.append(a)
    def post(): return c.post(P, headers=H, json={'event_type': 'no_face_detected', 'event_data': 'test'})
    with mock.patch.object(proctoring_routes, '_push_alert', fake_push), mock.patch.object(notif_bus, 'notify_exam', fake_notify), \
         mock.patch.object(cache, 'cache_should_score', lambda *a, **k: True):
        cache_delete(f'cei:threshold_alert:{aid}')
        codes = [post().status_code for _ in range(5)]
        chk('5 événements « visage non détecté » acceptés', codes == [200] * 5, codes)
        chk('une seule notification urgente et une seule alerte tableau de bord (avant : une par événement)', len(notifs) == 1 and len(pushes) == 1, (len(notifs), len(pushes)))
        cache_delete(f'cei:threshold_alert:{aid}'); post()
        chk('après 10 min (clé expirée) une nouvelle alerte peut partir', len(notifs) == 2 and len(pushes) == 2, (len(notifs), len(pushes)))
        with mock.patch.object(cache, 'cache_set_nx_value', lambda *a, **k: None):
            n0 = len(notifs); post(); post()
            chk('sans Redis : on envoie quand même (seul filet quand le bannissement auto est désactivé)', len(notifs) == n0 + 2, (n0, len(notifs)))
finally:
    s.close(); s2 = get_session()
    for i in ids['exams']:
        for x in s2.query(ExamAttempt).filter_by(exam_id=i).all():
            s2.query(ExamActivityLog).filter_by(attempt_id=x.id).delete(synchronize_session=False); s2.delete(x)
        s2.flush(); x = s2.get(OnlineExam, i)
        if x: s2.delete(x)
    s2.flush()
    for uid in ids['users']:
        u = s2.get(User, uid)
        if u: s2.delete(u)
    s2.commit(); print('données de test supprimées')
ok = sum(RES); print(f'\n{ok}/{len(RES)} vérifications réussies'); sys.exit(0 if ok == len(RES) else 1)
