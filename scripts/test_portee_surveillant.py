#!/usr/bin/env python
"""Périmètre du surveillant (18/11/2026) : alertes, « lu » et notifications limités à SES étudiants ; seuil de changements de contexte jamais à 0 sans exclusion automatique.

    cd cei-api-v2 && .venv-base/bin/python scripts/test_portee_surveillant.py     (données temporaires, supprimées)

Rapport des surveillants : « le surveillant voit les alertes de tous les étudiants, et non uniquement de ceux qui lui sont affectés » ; « messages impossibles à envoyer à un étudiant
affecté à un autre surveillant : la plateforme le refuse, alors que cet étudiant apparaît dans la liste » ; « notifications envahissantes » ; « seuil de changement de contexte fixé à 0 ».
"""
import os, sys, uuid, json
from datetime import datetime, timedelta
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as A
import proctoring_routes as P
import notif_bus as NB
from cache import _get_client, cache_delete
from models import (get_session, User, UserRole, Subject, OnlineExam, ExamStatus, EC, StudentUEEnrollment, ExamAttempt, AttemptStatus, ExamActivityLog,
                    ExamProctor, ProctorAssignment)
from auth_paseto import create_access_token
from werkzeug.security import generate_password_hash

RESULTS = []
def check(nom, ok, detail=''):
    RESULTS.append((nom, bool(ok))); print(('  ✔ ' if ok else '  ✘ ') + nom + (f'  — {detail}' if detail and not ok else ''))
s = get_session(); tag = uuid.uuid4().hex[:6]; made = {'users': [], 'ex': [], 'sub': None}
c = A.app.test_client(); rc = _get_client()
def mk(role, n):
    u = User(email=f'por.{tag}.{n}@test.invalid', full_name=f'Portee {n}', role=role, is_active=True, email_verified=True, has_email=True, password_hash=generate_password_hash('x'))
    s.add(u); s.commit(); made['users'].append(u.id); return u
def H(u): return {'Authorization': 'Bearer ' + create_access_token(u.id, u.role.value, u.email)}
try:
    adm = mk(UserRole.ADMIN, 'adm'); svA, svB, svC = mk(UserRole.SURVEILLANT, 'A'), mk(UserRole.SURVEILLANT, 'B'), mk(UserRole.SURVEILLANT, 'C')
    st = [mk(UserRole.STUDENT, f'e{i}') for i in range(4)]
    ec = s.query(EC).filter(EC.ue_id.isnot(None)).first()
    sub = Subject(title='por', content='1. Q ? [QCM]\nA. a\nB. b\n', rubric='Question 1 (1 pt) : A', creator_id=adm.id, ec_id=ec.id); s.add(sub); s.commit(); made['sub'] = sub.id
    now = datetime.utcnow()
    ex = OnlineExam(subject_id=sub.id, title='por', duration_minutes=60, start_time=now - timedelta(minutes=5), end_time=now + timedelta(hours=2), status=ExamStatus.ACTIVE, created_by_id=adm.id,
                    max_tab_switches=0, auto_ban_enabled=False)
    s.add(ex); s.commit(); made['ex'].append(ex.id)
    for u in st: s.add(StudentUEEnrollment(student_id=u.id, ue_id=ec.ue_id))
    for sv in (svA, svB, svC): s.add(ExamProctor(exam_id=ex.id, proctor_id=sv.id, assigned_by_id=adm.id))
    att = [ExamAttempt(exam_id=ex.id, student_id=u.id, status=AttemptStatus.IN_PROGRESS, started_at=now, exam_seen_at=now, answers='{}') for u in st]
    s.add_all(att); s.commit()
    # e0, e1 → A ; e2 → B (par tentative) ; e3 → personne
    s.add(ProctorAssignment(exam_id=ex.id, proctor_id=svA.id, attempt_id=att[0].id, student_id=st[0].id))
    s.add(ProctorAssignment(exam_id=ex.id, proctor_id=svA.id, student_id=st[1].id))      # pré-affectation (sans tentative liée)
    s.add(ProctorAssignment(exam_id=ex.id, proctor_id=svB.id, attempt_id=att[2].id, student_id=st[2].id)); s.commit()
    P._agent_auth = lambda: True
    # nettoyage préalable des alertes de test
    r_al = P._redis_alerts()
    before = r_al.llen(P._ALERTS_KEY); r_al.close()
    for i, a in enumerate(att):
        c.post('/api/agent/alerts', json={'exam_id': ex.id, 'attempt_id': a.id, 'student_name': f'Étudiant {i}', 'risk_score': 70, 'level': 'ALERTE'})
    names = lambda u, q='': sorted(x['student_name'] for x in (c.get(f'/api/agent/alerts{q}', headers=H(u)).get_json() or {}).get('alerts', []) if x.get('exam_id') == ex.id)

    print('\n== Alertes : chacun voit les siennes ==')
    check("surveillant A : voit ses 2 étudiants (une affectation par tentative, une par pré-affectation), pas les autres", names(svA) == ['Étudiant 0', 'Étudiant 1'], names(svA))
    check("surveillant B : voit uniquement le sien", names(svB) == ['Étudiant 2'], names(svB))
    check("surveillant C (aucun étudiant) : ne voit AUCUNE alerte", names(svC) == [], names(svC))
    check("administrateur : voit toutes les alertes de l'examen", len(names(adm)) == 4, names(adm))
    tot = (c.get(f'/api/agent/alerts?exam_id={ex.id}', headers=H(svA)).get_json() or {}).get('total_unread')
    check("le compteur de non lues est celui de SON périmètre (2, pas 4)", tot == 2, tot)
    check("le filtre par examen exclut les alertes des autres examens", all(x.get('exam_id') == ex.id for x in (c.get(f'/api/agent/alerts?exam_id={ex.id}', headers=H(adm)).get_json() or {}).get('alerts', [])))

    print('\n== « Lu » : on ne marque que ses propres alertes ==')
    c.post('/api/agent/alerts/read', json={'attempt_ids': [att[2].id]}, headers=H(svA))               # A tente de marquer l'alerte de l'étudiant de B
    check("A ne peut pas faire disparaître l'alerte de l'étudiant de B", names(svB) == ['Étudiant 2'], names(svB))
    c.post('/api/agent/alerts/read', json={'attempt_ids': [att[0].id]}, headers=H(svA))
    check("A marque la sienne comme lue : elle disparaît pour lui", names(svA) == ['Étudiant 1'], names(svA))

    print('\n== Notifications : seulement les surveillants de l\'étudiant ==')
    ids = lambda a: NB._exam_staff_ids(ex.id, a.id)
    check("alerte de l'étudiant de A : A est prévenu, ni B ni C", svA.id in ids(att[0]) and svB.id not in ids(att[0]) and svC.id not in ids(att[0]), ids(att[0]))
    check("alerte de l'étudiant de B : B seul parmi les surveillants", svB.id in ids(att[2]) and svA.id not in ids(att[2]) and svC.id not in ids(att[2]))
    check("étudiant pré-affecté à A (tentative non liée) : A est prévenu", svA.id in ids(att[1]) and svB.id not in ids(att[1]))
    check("étudiant sans surveillant : TOUS les surveillants sont prévenus (aucune alerte perdue)", {svA.id, svB.id, svC.id} <= ids(att[3]))
    check("l'enseignant / l'administrateur créateur reste toujours prévenu", adm.id in ids(att[0]))
    check("sans tentative précisée : comportement d'origine (tous les surveillants)", {svA.id, svB.id, svC.id} <= NB._exam_staff_ids(ex.id))

    print('\n== Seuil de changements de contexte ==')
    a0 = att[0]
    def switch():
        cache_delete(f'cei:examact:dedup:{a0.id}:tab_switch')
        return c.post(f'/api/exam_attempts/{a0.id}/log_activity', json={'event_type': 'tab_switch', 'event_data': 'x'}, headers=H(st[0])).get_json() or {}
    res = [switch() for _ in range(4)]
    check("seuil réglé à 0 sans exclusion automatique : 3 changements d'onglet ne déclenchent AUCUNE alerte", not any(r.get('alert_sent') for r in res[:3]), res[:3])
    check("… le 4e (au-delà de 3) déclenche l'alerte pour intervention manuelle", res[3].get('alert_sent') is True, res[3])
    ex2 = s.get(OnlineExam, ex.id); ex2.auto_ban_enabled = True; s.commit()
    s.query(ExamAttempt).filter_by(id=a0.id).update({'tab_switches': 0}); s.commit()
    r = switch(); r1 = switch()
    check("avec l'exclusion automatique ACTIVÉE, le seuil de l'enseignant (0) reste appliqué tel quel", r1.get('banned') is True or r.get('banned') is True, (r, r1))
finally:
    s.rollback()
    try:
        ids_ = made['ex']
        if ids_:
            att_ids = [a.id for a in s.query(ExamAttempt).filter(ExamAttempt.exam_id.in_(ids_))]
            try:
                ra = P._redis_alerts()
                for raw in ra.lrange(P._ALERTS_KEY, 0, -1):
                    try:
                        if json.loads(raw).get('exam_id') in ids_: ra.lrem(P._ALERTS_KEY, 0, raw)
                    except Exception: pass
                for i in att_ids: ra.srem(P._READ_KEY, str(i))
                ra.close()
            except Exception: pass
            s.query(ProctorAssignment).filter(ProctorAssignment.exam_id.in_(ids_)).delete(synchronize_session=False)
            if att_ids: s.query(ExamActivityLog).filter(ExamActivityLog.attempt_id.in_(att_ids)).delete(synchronize_session=False)
            s.query(ExamAttempt).filter(ExamAttempt.exam_id.in_(ids_)).delete(synchronize_session=False)
            s.query(ExamProctor).filter(ExamProctor.exam_id.in_(ids_)).delete(synchronize_session=False)
            s.query(OnlineExam).filter(OnlineExam.id.in_(ids_)).delete(synchronize_session=False)
        if made['users']: s.query(StudentUEEnrollment).filter(StudentUEEnrollment.student_id.in_(made['users'])).delete(synchronize_session=False)
        if made['sub']: s.query(Subject).filter_by(id=made['sub']).delete()
        if made['users']: s.query(User).filter(User.id.in_(made['users'])).delete(synchronize_session=False)
        s.commit()
    except Exception as ex_:
        s.rollback(); print('nettoyage incomplet :', ex_)
    s.close()
bad = [n for n, ok in RESULTS if not ok]
print(f"\n{len(RESULTS) - len(bad)}/{len(RESULTS)} vérifications réussies" + (f" — ÉCHECS : {bad}" if bad else ''))
sys.exit(1 if bad else 0)
