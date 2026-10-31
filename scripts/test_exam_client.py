#!/usr/bin/env python
"""Application « CEI Examen » obligatoire : poignée de main signée, porte au démarrage, versions. Crée puis supprime ses propres données.
    cd cei-api-v2 && .venv-base/bin/python scripts/test_exam_client.py"""
import sys, os, hmac, hashlib
sys.path.insert(0, '.')
os.environ['CEI_CLIENT_SECRET'] = 'secret-de-test'
from app import app
from models import *
from routes.exams import get_session
from auth_paseto import create_access_token
from datetime import datetime, timedelta

RES = []
def chk(n, ok, d=''):
    RES.append(bool(ok)); print(('  ✔ ' if ok else '  ✘ ') + n + (f'  — {d}' if d and not ok else ''))

s = get_session()
adm = s.query(User).filter(User.role == UserRole.ADMIN).first()
stu = s.query(User).filter(User.role == UserRole.STUDENT).first()
subj = s.query(Subject).first()
now = datetime.utcnow()
ex = OnlineExam(subject_id=subj.id, title='TEST client', duration_minutes=60, start_time=now - timedelta(minutes=5), end_time=now + timedelta(minutes=55),
                status=ExamStatus.ACTIVE, created_by_id=adm.id, require_client=True)
s.add(ex); s.commit(); eid = ex.id
H = lambda u: {'Authorization': 'Bearer ' + create_access_token(u.id, u.role.value, u.email)}
UA = {'User-Agent': 'Mozilla/5.0 CEIExamClient/1.0.0'}
c = app.test_client()
try:
    r = c.post(f'/api/online_exams/{eid}/start', headers=H(stu), json={})
    chk("sans l'application : démarrage refusé (client_required)", r.status_code == 403 and (r.get_json() or {}).get('client_required'), (r.status_code, r.get_json()))
    n = c.get(f'/api/exam-client/challenge?exam_id={eid}', headers=H(stu)).get_json()
    sig = lambda nonce, ver='1.0.0', plat='win32', uid=stu.id: hmac.new(b'secret-de-test', f'{nonce}|{uid}|{ver}|{plat}'.encode(), hashlib.sha256).hexdigest()
    body = lambda nonce, **k: {'exam_id': eid, 'nonce': nonce, 'sig': sig(nonce), 'version': '1.0.0', 'platform': 'win32', **k}
    r = c.post('/api/exam-client/verify', headers={**H(stu), 'User-Agent': 'Mozilla/5.0 Chrome'}, json=body(n['nonce']))
    chk("navigateur ordinaire : poignée de main refusée (même avec la bonne signature)", r.status_code == 403, r.status_code)
    n = c.get(f'/api/exam-client/challenge?exam_id={eid}', headers=H(stu)).get_json()
    r = c.post('/api/exam-client/verify', headers={**H(stu), **UA}, json={**body(n['nonce']), 'sig': 'ab' * 32})
    chk("signature fausse refusée", r.status_code == 403, r.status_code)
    n = c.get(f'/api/exam-client/challenge?exam_id={eid}', headers=H(stu)).get_json()
    r = c.post('/api/exam-client/verify', headers={**H(stu), **UA}, json=body(n['nonce']))
    chk("client officiel : poignée de main acceptée", r.status_code == 200 and r.get_json().get('ok'), r.get_json())
    r = c.post('/api/exam-client/verify', headers={**H(stu), **UA}, json=body(n['nonce']))
    chk("défi à usage unique : rejouer la même signature est refusé", r.status_code == 400, r.status_code)
    r = c.post(f'/api/online_exams/{eid}/start', headers=H(stu), json={})
    chk("après la poignée de main : plus de refus « application requise »", not (r.get_json() or {}).get('client_required'), (r.status_code, r.get_json()))
    os.environ['CEI_CLIENT_MIN_VERSION'] = '2.0.0'
    n = c.get(f'/api/exam-client/challenge?exam_id={eid}', headers=H(stu)).get_json()
    r = c.post('/api/exam-client/verify', headers={**H(stu), **UA}, json=body(n['nonce']))
    chk("version trop ancienne : refusée (426) avec lien de téléchargement", r.status_code == 426 and r.get_json().get('download_url'), r.status_code)
    os.environ.pop('CEI_CLIENT_MIN_VERSION')
    # réglages : l'option se modifie à l'état Planifié, pas en cours
    r = c.put(f'/api/admin/online_exams/{eid}', headers=H(adm), json={'require_client': False})
    chk("En cours : l'exigence de l'application est verrouillée (409)", r.status_code == 409, r.status_code)
    s.query(ExamAttempt).filter_by(exam_id=eid).delete(); ex2 = s.get(OnlineExam, eid); ex2.status = ExamStatus.SCHEDULED; s.commit()
    r = c.put(f'/api/admin/online_exams/{eid}', headers=H(adm), json={'require_client': False})
    chk("Planifié : l'exigence de l'application se modifie", r.status_code == 200 and r.get_json()['exam']['require_client'] is False, r.status_code)
    r = c.get('/api/exam-client/releases')
    chk("liste publique des téléchargements (vide sans dossier de versions)", r.status_code == 200 and r.get_json()['files'] == [])
    r = c.get('/api/exam-client/download/../../etc/passwd')
    chk("téléchargement : aucun chemin hors liste", r.status_code == 404)
finally:
    s.close(); s2 = get_session()
    for a in s2.query(ExamAttempt).filter_by(exam_id=eid): s2.delete(a)
    e = s2.get(OnlineExam, eid)
    if e: s2.delete(e)
    s2.commit(); print('données de test supprimées')
ok = sum(RES); print(f'\n{ok}/{len(RES)} vérifications réussies'); sys.exit(0 if ok == len(RES) else 1)
