#!/usr/bin/env python
"""Clôture automatique : à l'heure de fin, copies soumises par le serveur et salle fermée ; prolongation prise en compte.

    cd cei-api-v2 && .venv-base/bin/python scripts/test_cloture_examen.py

Vérifie : les copies dont l'échéance est dépassée sont soumises sans qu'on charge la liste des examens ; une copie avec
du temps supplémentaire reste ouverte (et la salle aussi) ; la salle est fermée quand il ne reste plus personne ; un
étudiant qui s'entête à enregistrer après l'heure est refusé et soumis ; une prolongation de l'examen est visible
par l'étudiant dans sa lecture périodique, sans rechargement, et empêche la soumission. Données temporaires.
"""
import os, sys, uuid
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as A
from services import exam_closer as EC
from cache import cache_delete, cache_get
from models import (get_session, User, UserRole, Subject, OnlineExam, ExamStatus, ExamAttempt, AttemptStatus)
from auth_paseto import create_access_token
from werkzeug.security import generate_password_hash

RESULTS = []
def check(nom, ok, detail=''):
    RESULTS.append((nom, bool(ok)))
    print(('  ✔ ' if ok else '  ✘ ') + nom + (f'  — {detail}' if detail and not ok else ''))

s = get_session(); tag = uuid.uuid4().hex[:6]; made = {'users': [], 'ex': [], 'sub': None}
c = A.app.test_client()
def mk(role, n):
    u = User(email=f'clo.{tag}.{n}@test.invalid', full_name=f'Clo Test {n}', role=role, is_active=True, email_verified=True,
             has_email=True, password_hash=generate_password_hash('x'))
    s.add(u); s.commit(); made['users'].append(u.id); return u
def H(u): return {'Authorization': 'Bearer ' + create_access_token(u.id, u.role.value, u.email)}
calls = []
EC._livekit = lambda method, payload, room: calls.append((method, payload)) or {}     # LiveKit simulé
def mine(e): return [(m, p) for m, p in calls if p.get('room') == f'exam-{e.id}']

try:
    adm = mk(UserRole.ADMIN, 'adm'); st = [mk(UserRole.STUDENT, f'e{i}') for i in range(4)]
    sub = Subject(title='clo test', content='Q1', rubric='x', creator_id=adm.id); s.add(sub); s.commit(); made['sub'] = sub.id
    now = datetime.utcnow()
    def exam(title, end):
        e = OnlineExam(subject_id=sub.id, title=title, duration_minutes=60, start_time=now - timedelta(hours=2), end_time=end,
                       status=ExamStatus.ACTIVE, created_by_id=adm.id)
        s.add(e); s.commit(); made['ex'].append(e.id); return e
    def att(e, u, extra=0):
        a = ExamAttempt(exam_id=e.id, student_id=u.id, status=AttemptStatus.IN_PROGRESS, extra_minutes=extra,
                        started_at=now - timedelta(hours=1), answers='{"reponse":"x"}')
        s.add(a); s.commit(); return a

    print('\n== Heure de fin dépassée ==')
    e1 = exam('clo 1', now - timedelta(minutes=5))
    a1, a2, a3 = att(e1, st[0]), att(e1, st[1]), att(e1, st[2], extra=30)
    calls.clear(); cache_delete(f'cei:room_closed:{e1.id}')
    stats = EC.close_expired()
    s.expire_all()
    check("les copies sans temps supplémentaire sont soumises", s.get(ExamAttempt, a1.id).status == AttemptStatus.AUTO_SUBMITTED and s.get(ExamAttempt, a2.id).status == AttemptStatus.AUTO_SUBMITTED)
    check("la copie avec temps supplémentaire reste ouverte", s.get(ExamAttempt, a3.id).status == AttemptStatus.IN_PROGRESS)
    check("l'examen est clos", s.get(OnlineExam, e1.id).status == ExamStatus.CLOSED)
    check("la salle reste ouverte tant qu'une copie compose", not any(m == 'DeleteRoom' for m, _ in mine(e1)), mine(e1))
    check("les étudiants soumis sont retirés de la salle", sum(1 for m, _ in mine(e1) if m == 'RemoveParticipant') == 4, mine(e1))
    s.get(ExamAttempt, a3.id).extra_minutes = 0; s.commit(); calls.clear()
    EC.close_expired(); s.expire_all()
    check("dernière copie soumise une fois son temps écoulé", s.get(ExamAttempt, a3.id).status == AttemptStatus.AUTO_SUBMITTED)
    check("la salle est fermée quand il ne reste plus personne", any(m == 'DeleteRoom' for m, _ in mine(e1)), mine(e1))
    calls.clear(); EC.close_expired()
    check("la salle n'est fermée qu'une fois", not any(m == 'DeleteRoom' for m, _ in mine(e1)), mine(e1))

    print('\n== Étudiant qui s\'entête après l\'heure ==')
    e2 = exam('clo 2', now - timedelta(minutes=5)); a4 = att(e2, st[3])
    r = c.post(f'/api/exam_attempts/{a4.id}/save', headers=H(st[3]), json={'answers': '{"reponse":"encore"}'})
    s.expire_all()
    check("l'enregistrement est refusé et la copie soumise", r.status_code == 400 and r.get_json().get('already_submitted') and s.get(ExamAttempt, a4.id).status == AttemptStatus.AUTO_SUBMITTED, (r.status_code, r.get_json()))
    check("sa dernière sauvegarde reste celle de la copie", s.get(ExamAttempt, a4.id).answers == '{"reponse":"x"}')

    print('\n== Prolongation de l\'examen en cours ==')
    e3 = exam('clo 3', now + timedelta(minutes=2)); a5 = att(e3, st[0])
    r0 = c.get(f'/api/exam_attempts/{a5.id}/subject', headers=H(st[0])).get_json()
    r = c.post(f'/api/online_exams/{e3.id}/extend', headers=H(adm), json={'extra_minutes': 30})
    r1 = c.get(f'/api/exam_attempts/{a5.id}/subject', headers=H(st[0])).get_json()
    check("la prolongation est acceptée", r.status_code == 200, r.get_json())
    check("l'étudiant voit la nouvelle durée à sa prochaine lecture (sans recharger)", r1['duration_minutes'] == r0['duration_minutes'] + 30, (r0.get('duration_minutes'), r1.get('duration_minutes')))
    s.get(OnlineExam, e3.id).end_time = now + timedelta(minutes=32); s.commit()
    EC.close_expired(); s.expire_all()
    check("la copie n'est pas soumise avant la nouvelle heure", s.get(ExamAttempt, a5.id).status == AttemptStatus.IN_PROGRESS)
    s.get(OnlineExam, e3.id).end_time = now - timedelta(minutes=3); s.commit()
    EC.close_expired(); s.expire_all()
    check("… et l'est à la nouvelle heure de fin", s.get(ExamAttempt, a5.id).status == AttemptStatus.AUTO_SUBMITTED)
finally:
    s.rollback()
    try:
        from models import ExamActivityLog, ProctorAssignment, ExamProctor
        ids = made['ex']
        if ids:
            att_ids = [a.id for a in s.query(ExamAttempt).filter(ExamAttempt.exam_id.in_(ids))]
            if att_ids: s.query(ExamActivityLog).filter(ExamActivityLog.attempt_id.in_(att_ids)).delete(synchronize_session=False)
            s.query(ExamAttempt).filter(ExamAttempt.exam_id.in_(ids)).delete(synchronize_session=False)
            s.query(OnlineExam).filter(OnlineExam.id.in_(ids)).delete(synchronize_session=False)
        if made['sub']: s.query(Subject).filter_by(id=made['sub']).delete()
        if made['users']: s.query(User).filter(User.id.in_(made['users'])).delete(synchronize_session=False)
        s.commit()
    except Exception as ex_:
        s.rollback(); print('nettoyage incomplet :', ex_)
    s.close()
bad = [n for n, ok in RESULTS if not ok]
print(f"\n{len(RESULTS) - len(bad)}/{len(RESULTS)} vérifications OK" + (f" — ÉCHECS : {bad}" if bad else ''))
sys.exit(1 if bad else 0)
