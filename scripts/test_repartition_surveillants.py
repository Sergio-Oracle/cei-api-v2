#!/usr/bin/env python
"""Répartition des étudiants entre surveillants pendant l'examen : rien n'est supprimé, tout est rééquilibré.

    cd cei-api-v2 && .venv-base/bin/python scripts/test_repartition_surveillants.py

Vérifie : un surveillant absent voit ses étudiants passer aux présents ; un surveillant qui revient ou qui est
ajouté en cours d'examen reçoit sa part ; aucune ligne d'affectation n'est supprimée (mêmes identifiants,
copies rendues et étudiants pas encore entrés intacts) ; la réponse reste réservée au surveillant affecté ;
un message en attente suit l'étudiant chez son nouveau surveillant. Données temporaires, supprimées en fin de test.
"""
import os, sys, json, uuid
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as A
import proctoring_routes as PR
from cache import cache_set, cache_delete
from models import (get_session, User, UserRole, Subject, OnlineExam, ExamStatus, ExamAttempt, AttemptStatus,
                    ExamProctor, ProctorAssignment, ExamActivityLog)
from auth_paseto import create_access_token
from werkzeug.security import generate_password_hash

RESULTS = []
def check(nom, ok, detail=''):
    RESULTS.append((nom, bool(ok)))
    print(('  ✔ ' if ok else '  ✘ ') + nom + (f'  — {detail}' if detail and not ok else ''))

s = get_session(); tag = uuid.uuid4().hex[:6]; made = {'users': [], 'ex': None, 'sub': None}
c = A.app.test_client()
def mk(role, n):
    u = User(email=f'rep.{tag}.{n}@test.invalid', full_name=f'Rep Test {n}', role=role, is_active=True, email_verified=True,
             has_email=True, password_hash=generate_password_hash('x'))
    s.add(u); s.commit(); made['users'].append(u.id); return u
def H(u): return {'Authorization': 'Bearer ' + create_access_token(u.id, u.role.value, u.email)}

try:
    adm = mk(UserRole.ADMIN, 'adm')
    P = [mk(UserRole.SURVEILLANT, f'p{i}') for i in range(4)]          # A B C + D (ajouté en cours)
    sub = Subject(title='rep test', content='Q1', rubric='x', creator_id=adm.id); s.add(sub); s.commit(); made['sub'] = sub.id
    now = datetime.utcnow()
    ex = OnlineExam(subject_id=sub.id, title='rep test', duration_minutes=120, start_time=now - timedelta(minutes=30),
                    end_time=now + timedelta(hours=2), status=ExamStatus.ACTIVE, created_by_id=adm.id)
    s.add(ex); s.commit(); made['ex'] = ex.id
    for p in P[:3]:
        s.add(ExamProctor(exam_id=ex.id, proctor_id=p.id, assigned_by_id=adm.id))
    s.commit()
    A_, B_, C_, D_ = P
    run, done, wait = [], [], []
    for i in range(30):                                                  # 10 en cours chacun
        st = mk(UserRole.STUDENT, f'run{i}'); at = ExamAttempt(exam_id=ex.id, student_id=st.id, status=AttemptStatus.IN_PROGRESS)
        s.add(at); s.commit()
        s.add(ProctorAssignment(exam_id=ex.id, proctor_id=P[i % 3].id, student_id=st.id, attempt_id=at.id)); run.append((st, at))
    for i in range(6):                                                   # copies rendues, chez A
        st = mk(UserRole.STUDENT, f'done{i}'); at = ExamAttempt(exam_id=ex.id, student_id=st.id, status=AttemptStatus.SUBMITTED)
        s.add(at); s.commit(); s.add(ProctorAssignment(exam_id=ex.id, proctor_id=A_.id, student_id=st.id, attempt_id=at.id)); done.append(st)
    for i in range(4):                                                   # pas encore entrés, chez C
        st = mk(UserRole.STUDENT, f'wait{i}'); s.add(ProctorAssignment(exam_id=ex.id, proctor_id=C_.id, student_id=st.id)); wait.append(st)
    s.commit()
    ids_before = {pa.student_id: pa.id for pa in s.query(ProctorAssignment).filter_by(exam_id=ex.id)}

    def live(*ps, absent=()):
        for p in ps:
            for k in ('present', 'seen'):
                cache_set(f'cei:proctor_{k}:{ex.id}:{p.id}', '1', ttl=600)
        for p in absent:
            cache_delete(f'cei:proctor_present:{ex.id}:{p.id}'); cache_set(f'cei:proctor_seen:{ex.id}:{p.id}', '1', ttl=600)
    def tick():
        cache_delete(f'cei:proctor_rebalance_lock:{ex.id}')
        s2 = get_session()
        try: return PR._check_disconnected_proctors(ex.id, s2)
        finally: s2.close()
    def loads(only=('run',)):
        s.expire_all(); out = {}
        for pa in s.query(ProctorAssignment).filter_by(exam_id=ex.id):
            a = s.query(ExamAttempt).filter_by(exam_id=ex.id, student_id=pa.student_id).first()
            if a is not None and a.status != AttemptStatus.IN_PROGRESS: continue
            if a is None and 'wait' not in only: continue
            out[pa.proctor_id] = out.get(pa.proctor_id, 0) + 1
        return out

    print('\n== C est absent ==')
    c_att = next(at for (st, at), i in zip(run, range(30)) if i % 3 == 2)      # un étudiant de C avec un message en attente
    s.add(ExamActivityLog(attempt_id=c_att.id, event_type='student_message', event_data=json.dumps({'message': 'Question !', 'student_name': 'x'})))
    s.commit()
    since = (datetime.utcnow() + timedelta(seconds=0.5)).strftime('%Y-%m-%dT%H:%M:%S.%f') + 'Z'
    import time; time.sleep(1)
    live(A_, B_, absent=[C_]); cache_delete(f'cei:proctor_presenthash:{ex.id}')
    notes = tick()
    s.expire_all()
    total = s.query(ProctorAssignment).filter_by(exam_id=ex.id).count()
    check("aucune affectation supprimée", total == len(ids_before), f'{total} vs {len(ids_before)}')
    check("les lignes gardent leur identifiant", all(s.query(ProctorAssignment).filter_by(id=i).first() for i in ids_before.values()))
    L = loads(('run', 'wait'))
    check("C n'a plus personne", L.get(C_.id, 0) == 0, L)
    check("A et B se partagent équitablement les étudiants", abs(L.get(A_.id, 0) - L.get(B_.id, 0)) <= 1, L)
    check("copies rendues : toujours chez A", all(s.query(ProctorAssignment).filter_by(exam_id=ex.id, student_id=d.id).first().proctor_id == A_.id for d in done))
    check("une alerte annonce le changement", notes and 'absent' in notes[0]['message'], notes)

    print('\n== Un message en attente suit l\'étudiant ==')
    s.expire_all()
    new_owner = s.query(ProctorAssignment).filter_by(exam_id=ex.id, attempt_id=c_att.id).first().proctor_id
    check("l'étudiant de C est passé à un surveillant présent", new_owner in (A_.id, B_.id), new_owner)
    owner = A_ if new_owner == A_.id else B_
    r = c.get(f'/api/online_exams/{ex.id}/student_messages?since={since}', headers=H(owner)).get_json()
    check("le nouveau surveillant voit le message d'avant le changement", any(m['attempt_id'] == c_att.id for m in r['messages']), r)

    print('\n== C revient : rééquilibrage ==')
    live(A_, B_, C_); n = tick()
    L = loads(('run',))
    check("les trois sont à égalité (±1)", max(L.values()) - min(L.values()) <= 1 and len(L) == 3, L)
    check("toujours aucune suppression", s.query(ProctorAssignment).filter_by(exam_id=ex.id).count() == len(ids_before))

    print('\n== D est ajouté en cours d\'examen ==')
    s.add(ExamProctor(exam_id=ex.id, proctor_id=D_.id, assigned_by_id=adm.id)); s.commit()
    cache_delete(f'cei:proctor_presenthash:{ex.id}'); live(A_, B_, C_, D_); tick()
    L = loads(('run',))
    check("D reçoit sa part, tout est équilibré (±1)", len(L) == 4 and max(L.values()) - min(L.values()) <= 1, L)
    check("toujours aucune suppression", s.query(ProctorAssignment).filter_by(exam_id=ex.id).count() == len(ids_before))

    print('\n== Panne : presque personne ne répond, puis tout revient ==')
    for p in (A_, B_, C_):
        cache_delete(f'cei:proctor_present:{ex.id}:{p.id}')
    live(D_); cache_delete(f'cei:proctor_presenthash:{ex.id}')
    tick(); L = loads(('run',))
    check("la répartition continue : D prend tout, rien n'est supprimé", L == {D_.id: 30} and s.query(ProctorAssignment).filter_by(exam_id=ex.id).count() == len(ids_before), L)
    live(A_, B_, C_, D_); tick(); L = loads(('run',))
    check("quand les autres reviennent, chacun reprend sa part (±1)", len(L) == 4 and max(L.values()) - min(L.values()) <= 1, L)
    check("toujours aucune suppression", s.query(ProctorAssignment).filter_by(exam_id=ex.id).count() == len(ids_before))

    print('\n== Réponse réservée au surveillant affecté ==')
    pa = s.query(ProctorAssignment).filter_by(exam_id=ex.id, proctor_id=A_.id).filter(ProctorAssignment.attempt_id.isnot(None)).first()
    other = next(p for p in (B_, C_, D_) if p.id != pa.proctor_id)
    r_other = c.post(f'/api/exam_attempts/{pa.attempt_id}/send_warning', headers=H(other), json={'message': 'x', 'type': 'message'})
    r_own = c.post(f'/api/exam_attempts/{pa.attempt_id}/send_warning', headers=H(A_), json={'message': 'ok', 'type': 'message'})
    check("un autre surveillant de l'examen reçoit 403", r_other.status_code == 403, r_other.status_code)
    check("le surveillant affecté peut répondre", r_own.status_code == 200, r_own.status_code)
finally:
    s.rollback()
    try:
        from models import ExamActivityLog as L_
        if made['ex']:
            att = [a.id for a in s.query(ExamAttempt).filter_by(exam_id=made['ex'])]
            if att: s.query(L_).filter(L_.attempt_id.in_(att)).delete(synchronize_session=False)
            s.query(ProctorAssignment).filter_by(exam_id=made['ex']).delete()
            s.query(ExamAttempt).filter_by(exam_id=made['ex']).delete()
            s.query(ExamProctor).filter_by(exam_id=made['ex']).delete()
            s.query(OnlineExam).filter_by(id=made['ex']).delete()
        if made['sub']: s.query(Subject).filter_by(id=made['sub']).delete()
        if made['users']: s.query(User).filter(User.id.in_(made['users'])).delete(synchronize_session=False)
        s.commit()
    except Exception as e:
        s.rollback(); print('nettoyage incomplet :', e)
    s.close()
bad = [n for n, ok in RESULTS if not ok]
print(f"\n{len(RESULTS) - len(bad)}/{len(RESULTS)} vérifications OK" + (f" — ÉCHECS : {bad}" if bad else ''))
sys.exit(1 if bad else 0)
