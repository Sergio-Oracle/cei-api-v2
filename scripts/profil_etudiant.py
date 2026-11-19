#!/usr/bin/env python
"""Profil de CROISSANCE des routes de l'étudiant (19/11/2026) : mêmes appels mesurés avec N petit puis N grand, sur un examen PAR VAGUES en cours.

    cd cei-api-v2 && .venv-base/bin/python scripts/profil_etudiant.py [N_petit=300] [N_grand=3000]     (serveur LOCAL ; données temporaires supprimées)

Une route dont le nombre de requêtes SQL ou le temps CROÎT avec N est un risque à 10 000 utilisateurs : elle est signalée « ▲ CROÎT ».
"""
import os, sys, time, uuid, json, statistics, threading
from datetime import datetime, timedelta
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('LIVEKIT_URL', 'wss://x.invalid'); os.environ.setdefault('LIVEKIT_API_KEY', 'k' * 12); os.environ.setdefault('LIVEKIT_API_SECRET', 's' * 40)
import app as A
from sqlalchemy import event
from models import (get_session, engine, User, UserRole, Subject, OnlineExam, ExamStatus, EC, StudentUEEnrollment, ExamAttempt, AttemptStatus, ExamActivityLog,
                    ExamProctor, ProctorAssignment, ExamWave, ExamWaveStudent, ExamAccessCode, CameraLog)
from auth_paseto import create_access_token
from werkzeug.security import generate_password_hash
NS = [int(sys.argv[1]) if len(sys.argv) > 1 else 300, int(sys.argv[2]) if len(sys.argv) > 2 else 3000]
c = A.app.test_client(); cnt = {'n': 0}; MAIN = threading.get_ident()
@event.listens_for(engine, 'before_cursor_execute')
def _b(conn, cur, st, par, ctx, many):
    if threading.get_ident() == MAIN: cnt['n'] += 1          # uniquement CE fil : la tâche de fond du même processus ne fausse pas le compte
H = lambda u: {'Authorization': 'Bearer ' + create_access_token(u.id, u.role.value, u.email, sid=None)}
from extensions import bcrypt as _bc
HASH = _bc.generate_password_hash('Passw0rd!').decode()
SUBJ = '\n'.join(f"{i}. Question {i} ? [QCM]\nA. a\nB. b\nC. c\nD. d\n" for i in range(1, 41))

def run(N):
    s = get_session(); tag = uuid.uuid4().hex[:6]; now = datetime.utcnow(); made = {'u': [], 'ex': None, 'sub': None}
    out = {}
    try:
        adm = User(email=f'pe.{tag}.adm@test.invalid', full_name='Adm', role=UserRole.ADMIN, is_active=True, email_verified=True, has_email=True, password_hash=HASH); s.add(adm); s.commit()
        stu = [User(email=f'pe.{tag}.e{i}@test.invalid', full_name=f'Etu {i}', role=UserRole.STUDENT, is_active=True, email_verified=True, has_email=True, password_hash=HASH) for i in range(N)]
        s.add_all(stu); s.commit(); made['u'] = [adm.id] + [u.id for u in stu]
        ec = s.query(EC).filter(EC.ue_id.isnot(None)).first()
        sub = Subject(title='pe', content=SUBJ, rubric='\n'.join(f'Question {i} (1 pt) : A' for i in range(1, 41)), creator_id=adm.id, ec_id=ec.id); s.add(sub); s.commit(); made['sub'] = sub.id
        ex = OnlineExam(subject_id=sub.id, title='pe', duration_minutes=60, start_time=now - timedelta(minutes=40), end_time=now + timedelta(hours=1), status=ExamStatus.ACTIVE, created_by_id=adm.id, allow_secondary_camera=True)
        s.add(ex); s.commit(); made['ex'] = ex.id
        w1 = ExamWave(exam_id=ex.id, position=1, name='V1', start_time=now - timedelta(minutes=40), end_time=now + timedelta(hours=1)); w2 = ExamWave(exam_id=ex.id, position=2, name='V2', start_time=now - timedelta(minutes=30), end_time=now + timedelta(hours=1, minutes=10))
        s.add_all([w1, w2]); s.commit()
        s.add_all([StudentUEEnrollment(student_id=u.id, ue_id=ec.ue_id) for u in stu]); s.commit()
        deja = {r[0] for r in s.query(ExamWaveStudent.student_id).filter_by(exam_id=ex.id)}       # le rattachement automatique des inscrits a pu déjà s'en charger
        s.add_all([ExamWaveStudent(wave_id=(w1 if i % 2 == 0 else w2).id, exam_id=ex.id, student_id=u.id) for i, u in enumerate(stu) if u.id not in deja])
        s.add_all([ExamAttempt(exam_id=ex.id, student_id=u.id, status=AttemptStatus.IN_PROGRESS, started_at=now - timedelta(minutes=20), exam_seen_at=now - timedelta(minutes=19), last_seen_at=now, answers='{"1":"A","2":"B","3":"C"}' + ' ' * 3000) for u in stu]); s.commit()
        atts = s.query(ExamAttempt).filter_by(exam_id=ex.id).order_by(ExamAttempt.id).all()
        s.add_all([ExamActivityLog(attempt_id=a.id, event_type='window_blur', event_data='x', timestamp=now - timedelta(minutes=k)) for a in atts for k in range(5)]); s.commit()
        u0, a0 = stu[0], atts[0]
        tests = [
            ('login',                          lambda: c.post('/api/auth/login', json={'email': u0.email, 'password': 'Passw0rd!', 'force': True})),
            ('GET /api/online_exams (liste)',  lambda: c.get('/api/online_exams', headers=H(u0))),
            ('GET /api/auth/me',               lambda: c.get('/api/auth/me', headers=H(u0))),
            ('GET exam details',               lambda: c.get(f'/api/online_exams/{ex.id}/details', headers=H(u0))),
            ('GET paginated (copie)',          lambda: c.get(f'/api/exam_attempts/{a0.id}/paginated', headers=H(u0))),
            ('GET livekit_token',              lambda: c.get(f'/api/exam_attempts/{a0.id}/livekit_token', headers=H(u0))),
            ('POST camera_snapshot',           lambda: c.post(f'/api/exam_attempts/{a0.id}/camera_snapshot', headers=H(u0), json={'event_type': 'periodic', 'image_data': 'data:image/jpeg;base64,' + 'A' * 12000, 'face_detected': True, 'faces_count': 1})),
            ('POST proctoring_event',          lambda: c.post(f'/api/exam_attempts/{a0.id}/proctoring_event', headers=H(u0), json={'event_type': 'no_face_detected', 'event_data': 'x'})),
            ('GET pending_messages',           lambda: c.get(f'/api/exam_attempts/{a0.id}/pending_messages', headers=H(u0))),
            ('GET notifications',              lambda: c.get('/api/notifications', headers=H(u0))),
            ('GET notifications/poll',         lambda: c.get('/api/notifications/poll?timeout=0', headers=H(u0))),
            ('GET tech_test/me',               lambda: c.get('/api/tech_test/me', headers=H(u0))),
            ('GET subject (relève)',           lambda: c.get(f'/api/exam_attempts/{a0.id}/subject', headers=H(u0))),
            ('POST heartbeat',                 lambda: c.post(f'/api/exam_attempts/{a0.id}/heartbeat', headers=H(u0), json={})),
            ('POST save',                      lambda: c.post(f'/api/exam_attempts/{a0.id}/save', headers=H(u0), json={'answers': '{"1":"B"}'})),
            ('POST start (reprise)',           lambda: c.post(f'/api/online_exams/{ex.id}/start', headers=H(u0), json={})),
            ('PROF  GET /api/online_exams',    lambda: c.get('/api/online_exams', headers=H(adm))),
            ('ADMIN GET attempts (liste)',     lambda: c.get(f'/api/online_exams/{ex.id}/attempts', headers=H(adm))),
            ('ADMIN GET stats',                lambda: c.get(f'/api/online_exams/{ex.id}/stats', headers=H(adm))),
            ('ADMIN GET waves',                lambda: c.get(f'/api/online_exams/{ex.id}/waves', headers=H(adm))),
            ('ADMIN GET incidents',            lambda: c.get(f'/api/online_exams/{ex.id}/incidents', headers=H(adm))),
        ]
        for nom, fn in tests:
            ts, qs, code = [], [], 0
            for _ in range(3):
                cnt['n'] = 0; t = time.time(); r = fn(); ts.append((time.time() - t) * 1000); qs.append(cnt['n']); code = r.status_code
            out[nom] = (code, statistics.median(qs), statistics.median(ts), len(r.get_data()))
    finally:
        s.rollback()
        try:
            if made['ex']:
                ids = [a.id for a in s.query(ExamAttempt.id).filter_by(exam_id=made['ex'])]
                for chunk in range(0, len(ids), 5000):
                    part = ids[chunk:chunk + 5000]
                    s.query(ExamActivityLog).filter(ExamActivityLog.attempt_id.in_(part)).delete(synchronize_session=False)
                    s.query(CameraLog).filter(CameraLog.attempt_id.in_(part)).delete(synchronize_session=False)
                    s.query(ExamAccessCode).filter(ExamAccessCode.attempt_id.in_(part)).delete(synchronize_session=False)
                s.query(ProctorAssignment).filter_by(exam_id=made['ex']).delete(); s.query(ExamAttempt).filter_by(exam_id=made['ex']).delete()
                s.query(ExamWaveStudent).filter_by(exam_id=made['ex']).delete(); s.query(ExamWave).filter_by(exam_id=made['ex']).delete()
                s.query(ExamProctor).filter_by(exam_id=made['ex']).delete(); s.query(OnlineExam).filter_by(id=made['ex']).delete()
            s.query(StudentUEEnrollment).filter(StudentUEEnrollment.student_id.in_(made['u'])).delete(synchronize_session=False)
            if made['sub']: s.query(Subject).filter_by(id=made['sub']).delete()
            from models import TokenBlocklist
            try: s.query(TokenBlocklist).filter(TokenBlocklist.user_id.in_(made['u'])).delete(synchronize_session=False)
            except Exception: s.rollback()
            s.query(User).filter(User.id.in_(made['u'])).delete(synchronize_session=False); s.commit()
        except Exception as e:
            s.rollback(); print('nettoyage incomplet :', e)
        s.close()
    return out

res = {n: run(n) for n in NS}
a, b = NS
print(f"\n{'route':34s} {'code':>4s} | {'SQL @'+str(a):>9s} {'ms':>7s} | {'SQL @'+str(b):>9s} {'ms':>7s} {'Ko':>6s}")
for nom in res[a]:
    ra, rb = res[a][nom], res[b][nom]
    flag = ''
    if rb[1] > ra[1] * 1.5 + 2 or rb[2] > ra[2] * 2 + 15: flag = '  ▲ CROÎT'
    print(f"{nom:34s} {rb[0]:4d} | {ra[1]:9.0f} {ra[2]:7.0f} | {rb[1]:9.0f} {rb[2]:7.0f} {rb[3]/1024:6.1f}{flag}")
