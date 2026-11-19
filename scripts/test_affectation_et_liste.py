#!/usr/bin/env python
"""Affectation d'un étudiant à un surveillant au démarrage + liste de surveillance (19/11/2026).

    cd cei-api-v2 && .venv-base/bin/python scripts/test_affectation_et_liste.py     (données temporaires, supprimées)

Au test de charge de 5 000 étudiants, /start (démarrage) était la route la plus coûteuse : 2 appels Redis PAR surveillant et le chargement de TOUTES les affectations de l'examen à chaque
entrée ; la liste de surveillance relisait toutes les affectations pour chaque surveillant et recalculait la vue complète à chaque appel. Ce test vérifie que les corrections gardent
exactement le même comportement : répartition équilibrée, surveillant pré-affecté conservé s'il est présent, remplacé s'il est absent, périmètre du surveillant, vue complète partagée.
"""
import os, sys, uuid, json
from datetime import datetime, timedelta
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as A
from models import (get_session, User, UserRole, Subject, OnlineExam, ExamStatus, EC, StudentUEEnrollment, ExamAttempt, AttemptStatus, ExamProctor, ProctorAssignment)
from auth_paseto import create_access_token
from cache import cache_set, cache_delete
from werkzeug.security import generate_password_hash

RESULTS = []
def check(nom, ok, detail=''):
    RESULTS.append((nom, bool(ok))); print(('  ✔ ' if ok else '  ✘ ') + nom + (f'  — {detail}' if detail and not ok else ''))
s = get_session(); tag = uuid.uuid4().hex[:6]; made = {'users': [], 'ex': None, 'sub': None}
c = A.app.test_client()
def mk(role, n):
    u = User(email=f'aff.{tag}.{n}@test.invalid', full_name=f'Aff Test {n}', role=role, is_active=True, email_verified=True, has_email=True, password_hash=generate_password_hash('x'))
    s.add(u); s.commit(); made['users'].append(u.id); return u
def H(u): return {'Authorization': 'Bearer ' + create_access_token(u.id, u.role.value, u.email)}
try:
    adm = mk(UserRole.ADMIN, 'adm'); prof = mk(UserRole.PROFESSOR, 'prof')
    sv = [mk(UserRole.SURVEILLANT, f'sv{i}') for i in range(3)]
    st = [mk(UserRole.STUDENT, f'e{i}') for i in range(9)]
    ec = s.query(EC).filter(EC.ue_id.isnot(None)).first()
    sub = Subject(title='aff', content='1. Q ? [QCM]\nA. a\nB. b\n', rubric='Question 1 (1 pt) : A', creator_id=adm.id, ec_id=ec.id); s.add(sub); s.commit(); made['sub'] = sub.id
    now = datetime.utcnow()
    ex = OnlineExam(subject_id=sub.id, title='aff', duration_minutes=60, start_time=now - timedelta(minutes=30), end_time=now + timedelta(hours=2), status=ExamStatus.ACTIVE, created_by_id=adm.id)
    s.add(ex); s.commit(); made['ex'] = ex.id
    s.add_all([StudentUEEnrollment(student_id=u.id, ue_id=ec.ue_id) for u in st])
    s.add_all([ExamProctor(exam_id=ex.id, proctor_id=v.id, assigned_by_id=adm.id) for v in sv]); s.commit()
    for v in sv:                                                          # l'examen a démarré il y a 30 min : seuls les surveillants VUS comptent comme présents
        cache_set(f'cei:proctor_seen:{ex.id}:{v.id}', '1', ttl=600); cache_set(f'cei:proctor_present:{ex.id}:{v.id}', '1', ttl=240)
    URL = f'/api/online_exams/{ex.id}/start'

    print('\n== Démarrage sans pré-affectation : répartition équilibrée ==')
    for u in st[:6]:
        r = c.post(URL, headers=H(u), json={}); assert r.status_code == 201, (r.status_code, r.get_data(as_text=True)[:200])
    cnt = {v.id: s.query(ProctorAssignment).filter_by(exam_id=ex.id, proctor_id=v.id).count() for v in sv}
    check("6 étudiants, 3 surveillants présents : 2 chacun", sorted(cnt.values()) == [2, 2, 2], cnt)

    print('\n== Pré-affecté à un surveillant présent : conservé ==')
    s.add(ProctorAssignment(exam_id=ex.id, proctor_id=sv[0].id, student_id=st[6].id)); s.commit()
    r = c.post(URL, headers=H(st[6]), json={}); check("entrée acceptée", r.status_code == 201, r.status_code)
    pa = s.query(ProctorAssignment).filter_by(exam_id=ex.id, student_id=st[6].id).all(); s.expire_all()
    check("reste avec son surveillant, liée à sa copie", len(pa) == 1 and pa[0].proctor_id == sv[0].id and pa[0].attempt_id, [(p.proctor_id, p.attempt_id) for p in pa])

    print('\n== Pré-affecté à un surveillant ABSENT : confié à un présent ==')
    cache_delete(f'cei:proctor_present:{ex.id}:{sv[1].id}'); cache_delete(f'cei:proctor_seen:{ex.id}:{sv[1].id}')
    s.add(ProctorAssignment(exam_id=ex.id, proctor_id=sv[1].id, student_id=st[7].id)); s.commit()
    r = c.post(URL, headers=H(st[7]), json={}); check("entrée acceptée", r.status_code == 201, r.status_code)
    s.expire_all(); pa = s.query(ProctorAssignment).filter_by(exam_id=ex.id, student_id=st[7].id).one()
    check("réaffecté à un surveillant présent (pas l'absent)", pa.proctor_id in (sv[0].id, sv[2].id), pa.proctor_id)
    cache_set(f'cei:proctor_seen:{ex.id}:{sv[1].id}', '1', ttl=600); cache_set(f'cei:proctor_present:{ex.id}:{sv[1].id}', '1', ttl=240)

    print('\n== Liste de surveillance ==')
    for a in s.query(ExamAttempt).filter_by(exam_id=ex.id): a.exam_seen_at = now
    s.commit(); cache_delete(f'cei:ap_full:{ex.id}')
    U = f'/api/online_exams/{ex.id}/active_proctoring'
    r1 = c.get(U, headers=H(adm)); j1 = r1.get_json()
    check("admin : voit les 8 copies", r1.status_code == 200 and j1['total'] == 8, (r1.status_code, j1.get('total')))
    r2 = c.get(U, headers=H(adm)); j2 = r2.get_json()
    check("admin, appel suivant (vue partagée) : même contenu", {k: v for k, v in j2.items() if not k.startswith('my_')} == {k: v for k, v in j1.items() if not k.startswith('my_')})
    r3 = c.get(U, headers=H(prof)); j3 = r3.get_json()
    check("enseignant : même vue mais SON identité (rien ne fuite de l'admin)", j3['my_identity'] == f'teacher-{prof.id}' and j3['my_role'] == 'professor' and j2['my_identity'] == f'teacher-{adm.id}', (j3.get('my_identity'), j2.get('my_identity')))
    check("… et le détail par surveillant est complet", len(j3['proctors']) == 3 and sum(p['student_count'] for p in j3['proctors']) == 8, j3['proctors'])
    for v in sv:
        r = c.get(U, headers=H(v)); j = r.get_json()
        want = {pa.student_id for pa in s.query(ProctorAssignment).filter_by(exam_id=ex.id, proctor_id=v.id) if pa.student_id in {a.student_id for a in s.query(ExamAttempt).filter_by(exam_id=ex.id)}}
        got = {a['student_id'] for a in j['attempts']}
        check(f"surveillant {v.id} : voit exactement son groupe ({len(want)})", r.status_code == 200 and got == want and all(a['proctor_id'] == v.id for a in j['attempts']), (got, want))
        check("… avec sa propre identité", j['my_identity'] == f'proctor-{v.id}' and j['my_role'] == 'surveillant')
    outsider = mk(UserRole.SURVEILLANT, 'hors')
    r = c.get(U, headers=H(outsider)); check("surveillant non affecté : refusé", r.status_code == 403, r.status_code)
    r = c.get(U, headers=H(st[0])); check("étudiant : refusé (jamais servi par la vue partagée)", r.status_code == 403, r.status_code)
finally:
    s.rollback()
    try:
        cache_delete(f'cei:ap_full:{made["ex"]}')
        if made['ex']:
            from models import ExamActivityLog, ExamAccessCode
            att = [a.id for a in s.query(ExamAttempt).filter_by(exam_id=made['ex'])]
            if att:
                s.query(ExamAccessCode).filter(ExamAccessCode.attempt_id.in_(att)).delete(synchronize_session=False)
                s.query(ExamActivityLog).filter(ExamActivityLog.attempt_id.in_(att)).delete(synchronize_session=False)
            s.query(ProctorAssignment).filter_by(exam_id=made['ex']).delete(); s.query(ExamAttempt).filter_by(exam_id=made['ex']).delete()
            s.query(ExamProctor).filter_by(exam_id=made['ex']).delete(); s.query(OnlineExam).filter_by(id=made['ex']).delete()
        s.query(StudentUEEnrollment).filter(StudentUEEnrollment.student_id.in_(made['users'])).delete(synchronize_session=False)
        if made['sub']: s.query(Subject).filter_by(id=made['sub']).delete()
        s.query(User).filter(User.id.in_(made['users'])).delete(synchronize_session=False); s.commit()
    except Exception as e:
        s.rollback(); print('nettoyage incomplet :', e)
    s.close()
bad = [n for n, ok in RESULTS if not ok]
print(f"\n{len(RESULTS) - len(bad)}/{len(RESULTS)} vérifications réussies" + (f" — ÉCHECS : {bad}" if bad else ''))
sys.exit(1 if bad else 0)
