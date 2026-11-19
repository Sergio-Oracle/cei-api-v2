#!/usr/bin/env python
"""Profil des requêtes d'un examen (19/11/2026) : combien de requêtes SQL et combien de millisecondes coûte chaque appel utilisé PENDANT un examen, avec N étudiants et P surveillants.

    cd cei-api-v2 && .venv-base/bin/python scripts/profil_requetes.py [N=300] [P=30]      (serveur LOCAL ; données temporaires supprimées)

Sert à repérer ce qui devient lourd avec le nombre d'étudiants : un appel dont le nombre de requêtes ou le temps croît avec N est un risque de lenteur et de déconnexions sous charge.
"""
import os, sys, time, uuid, json, statistics
from datetime import datetime, timedelta
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as A
from sqlalchemy import event, text
from models import (get_session, engine, User, UserRole, Subject, OnlineExam, ExamStatus, EC, StudentUEEnrollment, ExamAttempt, AttemptStatus, ExamActivityLog, ExamProctor, ProctorAssignment, ExamAccessCode)
from auth_paseto import create_access_token
from cache import cache_delete
N = int(sys.argv[1]) if len(sys.argv) > 1 else 300; P = int(sys.argv[2]) if len(sys.argv) > 2 else 30
s = get_session(); tag = uuid.uuid4().hex[:6]; now = datetime.utcnow(); c = A.app.test_client()
cnt = {'n': 0, 'ms': 0.0}
@event.listens_for(engine, 'before_cursor_execute')
def _b(conn, cur, st, par, ctx, many): conn.info['t0'] = time.time()
@event.listens_for(engine, 'after_cursor_execute')
def _a(conn, cur, st, par, ctx, many): cnt['n'] += 1; cnt['ms'] += (time.time() - conn.info.get('t0', time.time())) * 1000
H = lambda u: {'Authorization': 'Bearer ' + create_access_token(u.id, u.role.value, u.email)}
made = {'users': [], 'ex': None, 'sub': None}
def mk(role, i):
    u = User(email=f'prof.{tag}.{i}@test.invalid', full_name=f'Prof {i}', role=role, is_active=True, email_verified=True, has_email=True, password_hash='x'); return u
try:
    adm = mk(UserRole.ADMIN, 'adm'); svs = [mk(UserRole.SURVEILLANT, f'sv{i}') for i in range(P)]; stu = [mk(UserRole.STUDENT, f'e{i}') for i in range(N)]
    NF = 12; fresh_free = [mk(UserRole.STUDENT, f'ff{i}') for i in range(NF)]; fresh_pre = [mk(UserRole.STUDENT, f'fp{i}') for i in range(NF)]
    s.add_all([adm, *svs, *stu, *fresh_free, *fresh_pre]); s.commit(); made['users'] = [u.id for u in [adm, *svs, *stu, *fresh_free, *fresh_pre]]
    ec = s.query(EC).filter(EC.ue_id.isnot(None)).first()
    sub = Subject(title='profil', content='1. Q ? [QCM]\nA. a\nB. b\n', rubric='Question 1 (1 pt) : A', creator_id=adm.id, ec_id=ec.id); s.add(sub); s.commit(); made['sub'] = sub.id
    ex = OnlineExam(subject_id=sub.id, title='profil', duration_minutes=60, start_time=now - timedelta(minutes=30), end_time=now + timedelta(hours=1), status=ExamStatus.ACTIVE, created_by_id=adm.id)
    s.add(ex); s.commit(); made['ex'] = ex.id
    s.add_all([StudentUEEnrollment(student_id=u.id, ue_id=ec.ue_id) for u in [*stu, *fresh_free, *fresh_pre]]); s.add_all([ExamProctor(exam_id=ex.id, proctor_id=v.id, assigned_by_id=adm.id) for v in svs]); s.commit()
    atts = [ExamAttempt(exam_id=ex.id, student_id=u.id, status=AttemptStatus.IN_PROGRESS, started_at=now - timedelta(minutes=20), exam_seen_at=now - timedelta(minutes=19), last_seen_at=now, risk_score=(i % 70), answers='{"r":"x"}') for i, u in enumerate(stu)]
    s.add_all(atts); s.commit()
    s.add_all([ProctorAssignment(exam_id=ex.id, proctor_id=svs[i % P].id, attempt_id=a.id, student_id=a.student_id) for i, a in enumerate(atts)]); s.add_all([ProctorAssignment(exam_id=ex.id, proctor_id=svs[i % P].id, student_id=u.id) for i, u in enumerate(fresh_pre)]); s.commit()
    logs = []
    for i, a in enumerate(atts):                                         # 20 événements par étudiant : de quoi rendre les journaux réalistes
        for k in range(20): logs.append(ExamActivityLog(attempt_id=a.id, event_type=['window_blur', 'no_face_detected', 'sustained_audio_detected', 'whisper_detected'][k % 4], event_data='x', timestamp=now - timedelta(minutes=k)))
    s.add_all(logs); s.commit()
    import proctoring_routes as PR
    PR._agent_auth = lambda: True
    for a in atts[:60]: c.post('/api/agent/alerts', json={'exam_id': ex.id, 'attempt_id': a.id, 'student_name': 'x', 'risk_score': 70, 'level': 'ALERTE'})
    st0, sv0 = stu[0], svs[0]; a0 = atts[0]
    def prof(nom, method, url, user, body=None, n=4):
        ts, qs, qms = [], [], []
        for _ in range(n):
            cnt['n'] = 0; cnt['ms'] = 0.0; t = time.time()
            r = c.open(url, method=method, headers=H(user), json=body)
            ts.append((time.time() - t) * 1000); qs.append(cnt['n']); qms.append(cnt['ms'])
        return (nom, r.status_code, statistics.median(qs), statistics.median(ts), statistics.median(qms))
    rows = [
        ('ÉTUDIANT  heartbeat (20 s)',               'POST', f'/api/exam_attempts/{a0.id}/heartbeat', st0, {}),
        ('ÉTUDIANT  pending_messages (30 s)',        'GET',  f'/api/exam_attempts/{a0.id}/pending_messages', st0, None),
        ('ÉTUDIANT  extra-time (30 s)',              'GET',  f'/api/exam_attempts/{a0.id}/subject', st0, None),
        ('ÉTUDIANT  save (30 s)',                    'POST', f'/api/exam_attempts/{a0.id}/save', st0, {'answers': '{"r":"y"}'}),
        ('ÉTUDIANT  log_activity (événement)',       'POST', f'/api/exam_attempts/{a0.id}/log_activity', st0, {'event_type': 'window_blur', 'event_data': 'x'}),
        ('ÉTUDIANT  proctoring_event (événement)',   'POST', f'/api/exam_attempts/{a0.id}/proctoring_event', st0, {'event_type': 'no_face_detected', 'event_data': 'x'}),
        ('ÉTUDIANT  tech_test/me (1 fois)',          'GET',  '/api/tech_test/me', st0, None),
        ('SURVEILLANT  active_proctoring (8 s)',     'GET',  f'/api/online_exams/{ex.id}/active_proctoring', sv0, None),
        ('SURVEILLANT  student_messages (20 s)',     'GET',  f'/api/online_exams/{ex.id}/student_messages', sv0, None),
        ('SURVEILLANT  agent/alerts (15 s)',         'GET',  f'/api/agent/alerts?exam_id={ex.id}', sv0, None),
        ('SURVEILLANT  agent/status (30 s)',         'GET',  f'/api/agent/status?exam_id={ex.id}', sv0, None),
        ('SURVEILLANT  readiness (60 s)',            'GET',  f'/api/online_exams/{ex.id}/readiness', sv0, None),
        ('ADMIN  active_proctoring (8 s)',           'GET',  f'/api/online_exams/{ex.id}/active_proctoring', adm, None),
        ('ADMIN  agent/alerts (15 s)',               'GET',  f'/api/agent/alerts?exam_id={ex.id}', adm, None),
        ('ADMIN  readiness (60 s)',                  'GET',  f'/api/online_exams/{ex.id}/readiness', adm, None),
        ('ADMIN  vitals (60 s)',                     'GET',  f'/api/online_exams/{ex.id}/vitals', adm, None),
    ]
    def prof_chacun(nom, users, body=None):
        qs, ts, qms, code = [], [], [], 0
        for u in users:
            cnt['n'] = 0; cnt['ms'] = 0.0; t = time.time()
            r = c.open(f'/api/online_exams/{ex.id}/start', method='POST', headers=H(u), json=body); code = r.status_code
            ts.append((time.time() - t) * 1000); qs.append(cnt['n']); qms.append(cnt['ms'])
        return (nom, code, statistics.median(qs), statistics.median(ts), statistics.median(qms))
    for a in atts[2:2 + NF]: a.exam_seen_at = None; a.answers = '{}'
    s.commit()
    if os.environ.get('CPROF'):
        import cProfile, pstats
        pr = cProfile.Profile(); pr.enable(); prof_chacun('x', fresh_free); pr.disable()
        pstats.Stats(pr).sort_stats('cumulative').print_stats(35)
    start_rows = [
        prof_chacun('ETUDIANT  /start 1re entree, NON pre-affecte', fresh_free),
        prof_chacun('ETUDIANT  /start 1re entree, pre-affecte', fresh_pre),
        prof_chacun('ETUDIANT  /start reprise (copie jamais vue)', stu[2:2 + NF]),
        prof_chacun('ETUDIANT  /start reprise (copie vue, code demande)', stu[2 + NF:2 + 2 * NF]),
    ]
    print(f"\nProfil avec {N} étudiants, {P} surveillants, {len(logs)} événements en base\n{'appel':46s} {'code':>4s} {'requêtes SQL':>13s} {'temps total':>12s} {'dont SQL':>9s}")
    for r in start_rows: print(f"{r[0]:46s} {r[1]:4d} {r[2]:13.0f} {r[3]:9.0f} ms {r[4]:7.0f} ms")
    for nom, m, u, who, b in rows:
        r = prof(nom, m, u, who, b); print(f"{r[0]:46s} {r[1]:4d} {r[2]:13.0f} {r[3]:9.0f} ms {r[4]:7.0f} ms")
finally:
    s.rollback()
    try:
        if made['ex']:
            ids = [a.id for a in s.query(ExamAttempt).filter_by(exam_id=made['ex'])]
            if ids:
                s.query(ExamAccessCode).filter(ExamAccessCode.attempt_id.in_(ids)).delete(synchronize_session=False)
                s.query(ExamActivityLog).filter(ExamActivityLog.attempt_id.in_(ids)).delete(synchronize_session=False)
            s.query(ProctorAssignment).filter_by(exam_id=made['ex']).delete(); s.query(ExamAttempt).filter_by(exam_id=made['ex']).delete()
            s.query(ExamProctor).filter_by(exam_id=made['ex']).delete(); s.query(OnlineExam).filter_by(id=made['ex']).delete()
        s.query(StudentUEEnrollment).filter(StudentUEEnrollment.student_id.in_(made['users'])).delete(synchronize_session=False)
        if made['sub']: s.query(Subject).filter_by(id=made['sub']).delete()
        s.query(User).filter(User.id.in_(made['users'])).delete(synchronize_session=False); s.commit()
        try:
            import proctoring_routes as PR2
            r = PR2._redis_alerts()
            for raw in r.lrange(PR2._ALERTS_KEY, 0, -1):
                try:
                    if json.loads(raw).get('exam_id') == made['ex']: r.lrem(PR2._ALERTS_KEY, 0, raw)
                except Exception: pass
            r.close()
        except Exception: pass
        print('\nnettoyé')
    except Exception as e:
        s.rollback(); print('nettoyage incomplet :', e)
    s.close()
