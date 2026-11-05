#!/usr/bin/env python
"""Application CEI Examen : code de sortie d'urgence ALÉATOIRE, à usage unique, propre à un étudiant et un examen, vérifié par le serveur ;
refus des sessions à distance / machines virtuelles ; réglages « sites et applications autorisés » de l'examen.
    cd cei-api-v2 && .venv-base/bin/python scripts/test_code_sortie_client.py   (crée puis supprime ses propres données)"""
import sys, uuid, os, time, json, hmac, hashlib
sys.path.insert(0, '.')
os.environ['CEI_CLIENT_SECRET'] = 'secret-de-test-' + uuid.uuid4().hex
from app import app
from models import *
from routes.exams import get_session
from auth_paseto import create_access_token
from cache import cache_delete
from datetime import datetime, timedelta

SECRET = os.environ['CEI_CLIENT_SECRET']
s = get_session(); sfx = uuid.uuid4().hex[:6]; now = datetime.utcnow(); c = app.test_client(use_cookies=False); RES = []; ids = {'users': [], 'exams': []}
def chk(n, ok, d=''):
    RES.append(bool(ok)); print(('  ✔ ' if ok else '  ✘ ') + n + (f'  — {str(d)[:200]}' if d and not ok else ''))
def tok(u, role): return {'Authorization': 'Bearer ' + create_access_token(u.id, role, u.email)}
def mk(role, name):
    u = User(email=f'zz_{name}_{sfx}@test.local', full_name=f'ZZ {name}', role=role, is_active=True, password_hash='x'); s.add(u); s.commit(); ids['users'].append(u.id); return u
def exit_call(exam_id, uid, code, ts=None, ua='CEIExamClient/1.2.0', ver='1.2.0', sig=None):
    ts = ts or int(time.time() * 1000)
    sig = sig or hmac.new(SECRET.encode(), f'exit|{exam_id}|{uid}|{code}|{ts}|{ver}|linux'.encode(), hashlib.sha256).hexdigest()
    return c.post('/api/exam-client/exit', headers={'User-Agent': ua}, json={'exam_id': exam_id, 'user_id': uid, 'code': code, 'ts': ts, 'version': ver, 'platform': 'linux', 'sig': sig})
try:
    adm = s.query(User).filter(User.role == UserRole.ADMIN).first()
    stu = mk(UserRole.STUDENT, 'etu'); stu2 = mk(UserRole.STUDENT, 'etu2'); sv = mk(UserRole.SURVEILLANT, 'surv')
    e = OnlineExam(subject_id=s.query(Subject).first().id, title='TEST code sortie', duration_minutes=60, start_time=now - timedelta(minutes=5), end_time=now + timedelta(minutes=55),
                   status=ExamStatus.ACTIVE, created_by_id=adm.id, require_client=True); s.add(e); s.commit(); ids['exams'].append(e.id)
    a = ExamAttempt(exam_id=e.id, student_id=stu.id, status=AttemptStatus.IN_PROGRESS, started_at=now); s.add(a); s.commit()
    a2 = ExamAttempt(exam_id=e.id, student_id=stu2.id, status=AttemptStatus.IN_PROGRESS, started_at=now); s.add(a2); s.commit()
    for u in (stu, stu2):
        cache_delete(f'cei:exitcode:{e.id}:{u.id}'); cache_delete(f'cei:exitfail:{e.id}:{u.id}')
    HA, HS, HV = tok(adm, 'admin'), tok(stu, 'student'), tok(sv, 'surveillant')

    # — génération : qui a le droit
    r = c.post(f'/api/exam_attempts/{a.id}/client_exit_code', headers=HS)
    chk("un étudiant ne peut pas générer de code", r.status_code == 403, r.status_code)
    r = c.post(f'/api/exam_attempts/{a.id}/client_exit_code', headers=HV)
    chk("un surveillant non affecté à cet étudiant est refusé", r.status_code == 403, r.get_json())
    s.add(ProctorAssignment(proctor_id=sv.id, exam_id=e.id, student_id=stu.id, attempt_id=a.id)); s.commit()
    r = c.post(f'/api/exam_attempts/{a.id}/client_exit_code', headers=HV); j = r.get_json() or {}
    chk("le surveillant affecté obtient un code", r.status_code == 200 and len(j.get('code', '').replace('-', '')) == 8 and j.get('expires_in') == 900, j)
    code1 = j.get('code', '')
    r = c.post(f'/api/exam_attempts/{a.id}/client_exit_code', headers=HA); code2 = (r.get_json() or {}).get('code', '')
    chk("un nouveau code remplace l'ancien (un seul actif) et il est aléatoire", code2 and code2 != code1, (code1, code2))
    chk("le code n'est pas stocké en clair dans Redis", all(code2.replace('-', '') not in str(v) for v in [__import__('cache').cache_get(f'cei:exitcode:{e.id}:{stu.id}')]))

    # — vérification en ligne
    r = exit_call(e.id, stu.id, code1)
    chk("l'ancien code (remplacé) est refusé", r.status_code == 403, r.get_json())
    r = exit_call(e.id, stu.id, code2, sig='0' * 64)
    chk("signature invalide refusée (hors application officielle)", r.status_code == 403)
    r = exit_call(e.id, stu.id, code2, ua='Mozilla/5.0')
    chk("sans l'identifiant du client dans l'agent utilisateur : refusé", r.status_code == 403)
    r = exit_call(e.id, stu.id, code2, ts=int((time.time() - 600) * 1000))
    chk("horodatage périmé (rejeu) refusé", r.status_code == 403)
    r = exit_call(e.id, stu2.id, code2)
    chk("le code d'un étudiant ne marche pas pour un autre", r.status_code == 403)
    r = exit_call(e.id, stu.id, code2)
    chk("le bon code est accepté", r.status_code == 200 and (r.get_json() or {}).get('ok') is True, r.get_json())
    r = exit_call(e.id, stu.id, code2)
    chk("USAGE UNIQUE : le même code est refusé la seconde fois", r.status_code == 403, r.get_json())
    ev = [x.event_type for x in s.query(ExamActivityLog).filter_by(attempt_id=a.id).all()]
    chk("émission, essai raté et usage sont journalisés pour le surveillant", 'client_exit_code_issued' in ev and 'client_quit_code_used' in ev and 'client_quit_code_failed' in ev, ev)

    # — verrouillage après trop d'essais
    r = c.post(f'/api/exam_attempts/{a.id}/client_exit_code', headers=HA); good = (r.get_json() or {}).get('code', '')
    codes = [exit_call(e.id, stu.id, '00000000').status_code for _ in range(5)]
    chk("5 mauvais codes de suite", codes == [403] * 5, codes)
    r = exit_call(e.id, stu.id, good)
    chk("après 5 essais ratés le code est détruit, même le bon", r.status_code == 429, (r.status_code, r.get_json()))
    r = c.post(f'/api/exam_attempts/{a.id}/client_exit_code', headers=HA)
    chk("un nouveau code remet le compteur à zéro", r.status_code == 200 and exit_call(e.id, stu.id, r.get_json()['code']).status_code == 200)

    # — étudiant plus en cours d'examen
    a2.status = AttemptStatus.SUBMITTED; s.commit()
    r = c.post(f'/api/exam_attempts/{a2.id}/client_exit_code', headers=HA)
    chk("pas de code pour une copie déjà rendue", r.status_code == 409, r.status_code)

    # — session à distance / machine virtuelle à l'entrée
    def verify(vm=False, remote=False):
        ch = c.get(f'/api/exam-client/challenge?exam_id={e.id}', headers=HS).get_json()
        sig = hmac.new(SECRET.encode(), f"{ch['nonce']}|{stu.id}|1.2.0|linux".encode(), hashlib.sha256).hexdigest()
        return c.post('/api/exam-client/verify', headers={**HS, 'User-Agent': 'CEIExamClient/1.2.0'}, json={'exam_id': e.id, 'nonce': ch['nonce'], 'sig': sig, 'version': '1.2.0', 'platform': 'linux', 'vm': vm, 'remote': remote})
    chk("poste normal : accepté", verify().status_code == 200)
    r = verify(remote=True)
    chk("bureau à distance détecté : refusé", r.status_code == 403 and (r.get_json() or {}).get('client_remote'), r.get_json())
    chk("machine virtuelle : acceptée si l'examen ne l'interdit pas", verify(vm=True).status_code == 200)
    e2 = s.get(OnlineExam, e.id); e2.client_block_vm = True; s.commit()
    r = verify(vm=True)
    chk("machine virtuelle : refusée si l'examen l'interdit", r.status_code == 403 and (r.get_json() or {}).get('client_vm_refused'), r.get_json())

    # — réglages de l'examen : sites et applications autorisés
    r = c.put(f'/api/admin/online_exams/{e.id}', headers=HA, json={'client_allowed_sites': ['https://terangameet.unchk.sn/xuy-mbwu-tih', 'https://jokkomeet.unchk.sn'], 'client_allowed_apps': 'WhatsApp.exe, teams'})
    j = (r.get_json() or {}).get('exam') or {}
    chk("sites et applications enregistrés (modifiables même examen en cours)", r.status_code == 200 and j.get('client_allowed_sites') == ['https://terangameet.unchk.sn/xuy-mbwu-tih', 'https://jokkomeet.unchk.sn/'] and j.get('client_allowed_apps') == ['WhatsApp', 'teams'], j.get('client_allowed_sites'))
    r = c.put(f'/api/admin/online_exams/{e.id}', headers=HA, json={'client_allowed_sites': ['http://pas-securise.sn']})
    chk("site en http refusé", r.status_code == 400, r.get_json())
    r = c.put(f'/api/admin/online_exams/{e.id}', headers=HA, json={'client_allowed_sites': ['https://127.0.0.1/']})
    chk("adresse IP brute refusée", r.status_code == 400)
    r = c.put(f'/api/admin/online_exams/{e.id}', headers=HA, json={'client_block_vm': False})
    chk("refus des machines virtuelles verrouillé une fois l'examen ouvert", r.status_code == 409, r.status_code)
    j = c.get(f'/api/online_exams/{e.id}/details', headers=HS).get_json() or {}
    chk("l'étudiant reçoit la liste des sites autorisés avec l'examen", (j.get('exam') or j).get('client_allowed_sites'), str(j)[:150])
finally:
    s.close(); s2 = get_session()
    for i in ids['exams']:
        for x in s2.query(ExamAttempt).filter_by(exam_id=i).all():
            s2.query(ExamActivityLog).filter_by(attempt_id=x.id).delete(synchronize_session=False); s2.delete(x)
        s2.query(ProctorAssignment).filter_by(exam_id=i).delete(synchronize_session=False)
        s2.flush(); x = s2.get(OnlineExam, i)
        if x: s2.delete(x)
    s2.flush()
    for uid in ids['users']:
        for ex in ids['exams']: cache_delete(f'cei:exitcode:{ex}:{uid}'); cache_delete(f'cei:exitfail:{ex}:{uid}')
        u = s2.get(User, uid)
        if u: s2.delete(u)
    s2.commit(); print('données de test supprimées')
ok = sum(RES); print(f'\n{ok}/{len(RES)} vérifications réussies'); sys.exit(0 if ok == len(RES) else 1)
