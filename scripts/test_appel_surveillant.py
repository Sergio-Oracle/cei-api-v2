#!/usr/bin/env python
"""Appel d'un surveillant par un responsable d'examen / administrateur / enseignant (14/11/2026) — en plus du superviseur.
    cd cei-api-v2 && .venv-base/bin/python scripts/test_appel_surveillant.py   (crée puis supprime ses propres données)"""
import sys, uuid
sys.path.insert(0, '.')
from app import app
import proctoring_routes as PR
from models import *
from routes.exams import get_session
from auth_paseto import create_access_token
from datetime import datetime, timedelta
import notif_bus

s = get_session(); sfx = uuid.uuid4().hex[:6]; now = datetime.utcnow(); c = app.test_client(use_cookies=False); RES = []; ids = {'users': [], 'ex': [], 'sub': []}
def chk(n, ok, d=''):
    RES.append(bool(ok)); print(('  ✔ ' if ok else '  ✘ ') + n + (f'  — {str(d)[:200]}' if d and not ok else ''))
def mk(role, name):
    u = User(email=f'zz_{name}_{sfx}@test.local', full_name=f'ZZ {name}', role=role, is_active=True, password_hash='x'); s.add(u); s.commit(); ids['users'].append(u.id); return u
def H(u): return {'Authorization': 'Bearer ' + create_access_token(u.id, u.role.value, u.email)}
real_cfg, real_notify = PR.get_livekit_config, notif_bus.notify_user
PR.get_livekit_config = lambda: {'url': 'wss://lk.test', 'api_url': 'https://lk.test', 'api_key': 'k', 'api_secret': 's' * 40}
sent = []
notif_bus.notify_user = lambda uid, ev, title, msg, priority='default', tags=None, extra=None: sent.append((uid, ev, title, msg, extra))
try:
    adm = s.query(User).filter(User.role == UserRole.ADMIN).first()
    sv, sv2 = mk(UserRole.SURVEILLANT, 'sv'), mk(UserRole.SURVEILLANT, 'sv2')
    resp, prof, prof_other, sup, stu = mk(UserRole.RESPONSABLE_EXAMEN, 'resp'), mk(UserRole.PROFESSOR, 'prof'), mk(UserRole.PROFESSOR, 'prof2'), mk(UserRole.SUPERVISEUR, 'sup'), mk(UserRole.STUDENT, 'stu')
    sub = Subject(title='ZZ appel', content='Question 1. a ? [QCM]\nA. a\nB. b\n', rubric='', creator_id=prof.id); s.add(sub); s.commit(); ids['sub'].append(sub.id)
    ex = OnlineExam(subject_id=sub.id, title='ZZ appel', duration_minutes=60, start_time=now, end_time=now + timedelta(hours=1), status=ExamStatus.ACTIVE, created_by_id=prof.id); s.add(ex); s.commit(); ids['ex'].append(ex.id)
    s.add(ExamProctor(exam_id=ex.id, proctor_id=sv.id, assigned_by_id=prof.id)); s.commit()
    url = lambda p: f'/api/superviseur/proctor_call/{p.id}/request'
    tok = lambda p: f'/api/superviseur/proctor_call/{p.id}/token'

    print('== Demande d\'appel ==')
    r = c.post(url(sv), headers=H(resp)); j = r.get_json() or {}
    chk("le responsable d'examen peut appeler un surveillant", r.status_code == 200 and j.get('success'), (r.status_code, j))
    ev = [x for x in sent if x[0] == sv.id]
    chk("le surveillant reçoit « Appel entrant — le responsable d'examen » avec le nom de l'appelant", ev and 'responsable' in ev[-1][2].lower() and 'ZZ resp' in ev[-1][3] and ev[-1][4].get('caller_label') == "Le responsable d'examen", ev)
    chk("l'identité de l'appelant est transmise pour répondre", ev[-1][4].get('supervisor_id') == resp.id)
    r = c.post(url(sv2), headers=H(adm)); chk("l'administrateur peut appeler n'importe quel surveillant", r.status_code == 200, r.get_json())
    r = c.post(url(sv), headers=H(prof)); chk("l'enseignant de l'examen peut appeler le surveillant de SON examen", r.status_code == 200, r.get_json())
    r = c.post(url(sv), headers=H(prof_other)); chk("un autre enseignant ne le peut pas", r.status_code == 403, r.get_json())
    r = c.post(url(sv2), headers=H(prof)); chk("…ni appeler un surveillant qui n'est pas sur ses examens", r.status_code == 403, r.get_json())
    r = c.post(url(sv), headers=H(sup)); chk("un superviseur qui ne supervise pas ce surveillant : refusé (comme avant)", r.status_code == 403, r.get_json())
    r = c.post(url(sv), headers=H(stu)); chk("un étudiant ne peut pas", r.status_code == 403)
    r = c.post(url(sv), headers=H(sv2)); chk("un surveillant ne peut pas en appeler un autre", r.status_code == 403)
    r = c.post(url(stu), headers=H(resp)); chk("on ne peut pas « appeler » quelqu'un qui n'est pas surveillant", r.status_code == 403, r.get_json())

    print('\n== Jeton de la salle d\'appel ==')
    r = c.get(tok(sv), headers=H(resp)); j = r.get_json() or {}
    chk("l'appelant obtient son jeton (salle supcall-<surveillant>)", r.status_code == 200 and j.get('room') == f'supcall-{sv.id}' and j.get('token'), (r.status_code, j))
    r = c.get(tok(sv), headers=H(sv)); j2 = r.get_json() or {}
    chk("le surveillant appelé obtient le jeton de la MÊME salle", r.status_code == 200 and j2.get('room') == j.get('room'))
    chk("identités distinctes (pas de collision dans la salle)", j.get('identity') != j2.get('identity'), (j.get('identity'), j2.get('identity')))
    chk("un autre surveillant n'a pas accès à cette salle", c.get(tok(sv), headers=H(sv2)).status_code == 403)
    chk("un enseignant étranger à l'examen n'y accède pas", c.get(tok(sv), headers=H(prof_other)).status_code == 403)
    chk("l'enseignant de l'examen y accède", c.get(tok(sv), headers=H(prof)).status_code == 200)
    chk("un étudiant n'y accède pas", c.get(tok(sv), headers=H(stu)).status_code == 403)
finally:
    PR.get_livekit_config, notif_bus.notify_user = real_cfg, real_notify
    try:
        s.rollback()
        s.query(ExamProctor).filter(ExamProctor.exam_id.in_(ids['ex'])).delete(synchronize_session=False)
        s.query(OnlineExam).filter(OnlineExam.id.in_(ids['ex'])).delete(synchronize_session=False)
        s.query(Subject).filter(Subject.id.in_(ids['sub'])).delete(synchronize_session=False)
        s.query(User).filter(User.id.in_(ids['users'])).delete(synchronize_session=False); s.commit()
    except Exception as e:
        print('nettoyage :', e)
    s.close()
print(f"\n{sum(RES)}/{len(RES)} vérifications réussies"); sys.exit(0 if all(RES) else 1)
