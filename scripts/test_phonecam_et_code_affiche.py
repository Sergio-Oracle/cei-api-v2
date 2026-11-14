#!/usr/bin/env python
"""Caméra secondaire exigée (incident du 14/11 : étudiants entrés sans) + code de sortie affiché à l'étudiant lui-même.
    cd cei-api-v2 && .venv-base/bin/python scripts/test_phonecam_et_code_affiche.py   (crée puis supprime ses propres données)"""
import sys, uuid, os, json
sys.path.insert(0, '.')
os.environ['CEI_CLIENT_SECRET'] = 'secret-de-test-' + uuid.uuid4().hex
from app import app
from models import *
from routes.exams import get_session
from auth_paseto import create_access_token
from cache import cache_delete, cache_get
from datetime import datetime, timedelta
from services import phonecam

s = get_session(); sfx = uuid.uuid4().hex[:6]; now = datetime.utcnow(); c = app.test_client(use_cookies=False); RES = []; ids = {'users': [], 'exams': []}
def chk(n, ok, d=''):
    RES.append(bool(ok)); print(('  ✔ ' if ok else '  ✘ ') + n + (f'  — {str(d)[:200]}' if d and not ok else ''))
def tok(u, role): return {'Authorization': 'Bearer ' + create_access_token(u.id, role, u.email)}
def mk(role, name):
    u = User(email=f'zz_{name}_{sfx}@test.local', full_name=f'ZZ {name}', role=role, is_active=True, password_hash='x'); s.add(u); s.commit(); ids['users'].append(u.id); return u
real_state = phonecam.live_state
try:
    adm = s.query(User).filter(User.role == UserRole.ADMIN).first()
    stu, stu2, stu3 = mk(UserRole.STUDENT, 'etu'), mk(UserRole.STUDENT, 'etu2'), mk(UserRole.STUDENT, 'etu3')
    sv, sv_other = mk(UserRole.SURVEILLANT, 'surv'), mk(UserRole.SURVEILLANT, 'surv2')
    subj = s.query(Subject).first().id
    def exam(title, secondary):
        e = OnlineExam(subject_id=subj, title=title, duration_minutes=60, start_time=now - timedelta(minutes=5), end_time=now + timedelta(minutes=55),
                       status=ExamStatus.ACTIVE, created_by_id=adm.id, require_client=True, allow_secondary_camera=secondary); s.add(e); s.commit(); ids['exams'].append(e.id); return e
    e, e_plain = exam('TEST caméra secondaire', True), exam('TEST sans caméra secondaire', False)
    def att(ex, u):
        a = ExamAttempt(exam_id=ex.id, student_id=u.id, status=AttemptStatus.IN_PROGRESS, started_at=now, answers='{}'); s.add(a); s.commit(); return a
    a, a2, a3, ap = att(e, stu), att(e, stu2), att(e, stu3), att(e_plain, stu)
    s.add(ProctorAssignment(proctor_id=sv.id, exam_id=e.id, student_id=stu.id, attempt_id=a.id)); s.commit()
    HA, HS, HS2, HS3, HV, HVO = tok(adm, 'admin'), tok(stu, 'student'), tok(stu2, 'student'), tok(stu3, 'student'), tok(sv, 'surveillant'), tok(sv_other, 'surveillant')
    for x in (a, a2, a3, ap): cache_delete(phonecam.linked_key(x.id)); cache_delete(phonecam.waived_key(x.id))

    print('== Caméra secondaire exigée : la copie ne s\'ouvre pas sans téléphone ==')
    phonecam.live_state = lambda *x: False
    r = c.post(f'/api/exam_attempts/{a.id}/seen', json={}, headers=HS)
    chk("téléphone absent : copie refusée (409 + secondary_camera_required)", r.status_code == 409 and (r.get_json() or {}).get('secondary_camera_required') is True, (r.status_code, r.get_json()))
    s.expire_all(); chk("… et l'horloge ne démarre pas (copie non comptée comme vue)", s.get(ExamAttempt, a.id).exam_seen_at is None)
    r = c.post(f'/api/exam_attempts/{ap.id}/seen', json={}, headers=HS)
    chk("examen SANS caméra secondaire : aucun changement", r.status_code == 200, r.get_json())
    phonecam.live_state = lambda *x: None
    r = c.post(f'/api/exam_attempts/{a2.id}/seen', json={}, headers=HS2)
    chk("LiveKit injoignable (état inconnu) : on ne bloque PAS l'étudiant", r.status_code == 200, r.get_json())
    phonecam.live_state = lambda *x: True
    r = c.post(f'/api/exam_attempts/{a3.id}/seen', json={}, headers=HS3)
    chk("téléphone connecté : copie ouverte", r.status_code == 200, r.get_json())

    print('\n== État renvoyé à la page ==')
    phonecam.live_state = lambda *x: False
    j = c.get(f'/api/exam_attempts/{a.id}/phone_camera/status', headers=HS).get_json()
    chk("absent : linked=false, unknown=false, waived=false", j == {'linked': False, 'unknown': False, 'waived': False}, j)
    phonecam.live_state = lambda *x: None
    j = c.get(f'/api/exam_attempts/{a.id}/phone_camera/status', headers=HS).get_json()
    chk("LiveKit injoignable : unknown=true (la page ne change rien)", j.get('unknown') is True and j.get('linked') is False, j)

    print('\n== Dispense par le personnel (téléphone en panne) ==')
    phonecam.live_state = lambda *x: False
    chk("un étudiant ne peut pas se dispenser lui-même", c.post(f'/api/exam_attempts/{a.id}/phone_camera/waive', json={}, headers=HS).status_code == 403)
    chk("un surveillant NON affecté est refusé", c.post(f'/api/exam_attempts/{a.id}/phone_camera/waive', json={}, headers=HVO).status_code == 403)
    chk("examen sans caméra secondaire : rien à dispenser (409)", c.post(f'/api/exam_attempts/{ap.id}/phone_camera/waive', json={}, headers=HA).status_code == 409)
    r = c.post(f'/api/exam_attempts/{a.id}/phone_camera/waive', json={'reason': 'batterie vide'}, headers=HV)
    chk("le surveillant affecté dispense", r.status_code == 200 and (r.get_json() or {}).get('waived') is True, (r.status_code, r.get_json()))
    ev = s.query(ExamActivityLog).filter_by(attempt_id=a.id, event_type='phonecam_waived').all()
    chk("la dispense est tracée (qui, pourquoi)", len(ev) == 1 and 'batterie vide' in (ev[0].event_data or ''), [x.event_data for x in ev])
    j = c.get(f'/api/exam_attempts/{a.id}/phone_camera/status', headers=HS).get_json()
    chk("la page reçoit waived=true", j.get('waived') is True, j)
    r = c.post(f'/api/exam_attempts/{a.id}/seen', json={}, headers=HS)
    chk("dispensé : la copie s'ouvre malgré l'absence du téléphone", r.status_code == 200, r.get_json())
    cache_delete(phonecam.waived_key(a.id))
    chk("la dispense survit à la perte de Redis (relue dans le journal)", phonecam.is_waived(s, a.id) is True)

    print('\n== Code de sortie affiché à l\'étudiant lui-même ==')
    for u in (stu, stu2): cache_delete(f'cei:exitcode:{e.id}:{u.id}'); cache_delete(f'cei:exitfail:{e.id}:{u.id}'); cache_delete(f'cei:exitcode:show:{e.id}:{u.id}')
    hb = lambda aid, h: c.post(f'/api/exam_attempts/{aid}/heartbeat', json={}, headers=h).get_json() or {}
    chk("sans code émis : le heartbeat ne contient rien", 'exit_code' not in hb(a.id, HS))
    r = c.post(f'/api/exam_attempts/{a.id}/client_exit_code', headers=HV); j = r.get_json() or {}
    code = j.get('code', '')
    chk("émission par le surveillant : code reçu, et signalé « affiché à l'étudiant »", r.status_code == 200 and j.get('shown_to_student') is True, j)
    chk("la page de l'étudiant reçoit EXACTEMENT ce code", hb(a.id, HS).get('exit_code') == code, (code, hb(a.id, HS)))
    chk("un autre étudiant ne reçoit rien", 'exit_code' not in hb(a2.id, HS2))
    chk("un étudiant ne peut pas lire le code d'un autre (heartbeat d'une autre tentative refusé)", c.post(f'/api/exam_attempts/{a.id}/heartbeat', json={}, headers=HS2).status_code == 404)
    raw = json.dumps(cache_get(f'cei:exitcode:show:{e.id}:{stu.id}'))
    chk("le code n'est pas stocké en clair dans Redis (affichage)", code.replace('-', '') not in raw, raw)
    code_b = (c.post(f'/api/exam_attempts/{a.id}/client_exit_code', headers=HV).get_json() or {}).get('code', '')
    r = c.get(f'/api/exam_attempts/{a.id}/client_exit_code/mine', headers=HS)
    chk("avant l'examen (sans heartbeat) : l'étudiant lit son code par sa propre route", r.status_code == 200 and (r.get_json() or {}).get('code') == code_b, r.get_json())
    chk("… un autre étudiant ne peut pas lire le code de cette tentative", c.get(f'/api/exam_attempts/{a.id}/client_exit_code/mine', headers=HS2).status_code == 404)
    chk("un nouveau code remplace l'ancien aussi à l'écran de l'étudiant", code_b != code and hb(a.id, HS).get('exit_code') == code_b)
    import hmac, hashlib, time
    ts = int(time.time() * 1000); sig = hmac.new(os.environ['CEI_CLIENT_SECRET'].encode(), f'exit|{e.id}|{stu.id}|{code_b}|{ts}|1.2.0|linux'.encode(), hashlib.sha256).hexdigest()
    r = c.post('/api/exam-client/exit', headers={'User-Agent': 'CEIExamClient/1.2.0'}, json={'exam_id': e.id, 'user_id': stu.id, 'code': code_b, 'ts': ts, 'version': '1.2.0', 'platform': 'linux', 'sig': sig})
    chk("l'étudiant saisit le code affiché : sortie acceptée", r.status_code == 200, r.get_json())
    chk("code utilisé : il disparaît de l'écran", 'exit_code' not in hb(a.id, HS))
    code_c = (c.post(f'/api/exam_attempts/{a.id}/client_exit_code', headers=HV).get_json() or {}).get('code', '')
    s.query(ExamAttempt).filter_by(id=a.id).update({'status': AttemptStatus.SUBMITTED}); s.commit(); s.expire_all()
    chk("copie rendue : plus aucun code affiché", 'exit_code' not in hb(a.id, HS))
finally:
    phonecam.live_state = real_state
    try:
        s.rollback()
        for eid in ids['exams']:
            aids = [x.id for x in s.query(ExamAttempt).filter_by(exam_id=eid).all()]
            for aid in aids:
                cache_delete(phonecam.linked_key(aid)); cache_delete(phonecam.waived_key(aid))
            if aids:
                s.query(ExamActivityLog).filter(ExamActivityLog.attempt_id.in_(aids)).delete(synchronize_session=False)
            s.query(ProctorAssignment).filter_by(exam_id=eid).delete(synchronize_session=False)
            s.query(ExamAttempt).filter_by(exam_id=eid).delete(synchronize_session=False)
            s.query(OnlineExam).filter_by(id=eid).delete(synchronize_session=False)
            for u in ids['users']:
                for k in ('exitcode', 'exitfail', 'exitcode:show'): cache_delete(f'cei:{k}:{eid}:{u}')
        s.query(User).filter(User.id.in_(ids['users'])).delete(synchronize_session=False); s.commit()
    except Exception as ex:
        print('nettoyage :', ex)
    s.close()
print(f"\n{sum(RES)}/{len(RES)} vérifications réussies"); sys.exit(0 if all(RES) else 1)
