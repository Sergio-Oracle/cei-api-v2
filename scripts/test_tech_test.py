#!/usr/bin/env python
"""Test technique avant l'examen (18/11/2026) : règles, vérification CÔTÉ SERVEUR, résultat, tableau « prêts / non prêts » du surveillant.

    cd cei-api-v2 && .venv-base/bin/python scripts/test_tech_test.py     (données temporaires, supprimées)
"""
import os, sys, uuid, json, time
from datetime import datetime, timedelta
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as A
from services import tech_test as TT
from cache import _get_client
from models import (get_session, User, UserRole, Subject, OnlineExam, ExamStatus, EC, StudentUEEnrollment, ExamProctor)
from auth_paseto import create_access_token
from werkzeug.security import generate_password_hash

RESULTS = []
def check(nom, ok, detail=''):
    RESULTS.append((nom, bool(ok))); print(('  ✔ ' if ok else '  ✘ ') + nom + (f'  — {detail}' if detail and not ok else ''))

GOOD = {'camera': {'ok': True, 'brightness': 120, 'face': True, 'width': 640, 'height': 480}, 'micro': {'ok': True, 'level': 0.2}, 'screen': {'ok': True},
        'network': {'latency_ms': 60, 'down_mbps': 12}, 'client': {'is_client': False}}
LIVE = {'camera': True, 'mic': True, 'screen': True, 'width': 640, 'height': 480}
def mod(**kw):
    d = json.loads(json.dumps(GOOD)); [d[k].update(v) if isinstance(v, dict) else d.__setitem__(k, v) for k, v in kw.items()]; return d
def codes(ps): return {p['code'] for p in ps}

print('\n== Règles ==')
st, ps = TT.evaluate(GOOD, LIVE); check("tout est bon : prêt, aucun problème", st == 'ready' and not ps, ps)
st, ps = TT.evaluate(GOOD, None); check("envoi vers le serveur vidéo NON vérifié : jamais « prêt » (le navigateur ne peut pas se déclarer prêt seul)", st == 'not_ready' and 'diffusion' in codes(ps))
st, ps = TT.evaluate(GOOD, {**LIVE, 'camera': False}); check("caméra non reçue par le serveur vidéo (cas du 17/11) : non prêt, message sur le réseau/pare-feu", st == 'not_ready' and 'diffusion_camera' in codes(ps))
st, ps = TT.evaluate(GOOD, {**LIVE, 'screen': False}); check("écran non reçu par le serveur : non prêt", st == 'not_ready' and 'diffusion_ecran' in codes(ps))
st, ps = TT.evaluate(GOOD, {**LIVE, 'mic': False}); check("micro non reçu : à surveiller seulement", st == 'warn' and 'diffusion_micro' in codes(ps))
st, ps = TT.evaluate(mod(camera={'brightness': 20}), LIVE); check("image presque noire : non prêt (cause des « visage absent »)", st == 'not_ready' and 'lumiere' in codes(ps))
st, ps = TT.evaluate(mod(camera={'brightness': 55}), LIVE); check("éclairage faible : à surveiller, avec conseil", st == 'warn' and any('lumière' in p['message'].lower() for p in ps))
st, ps = TT.evaluate(mod(camera={'brightness': 250}), LIVE); check("contre-jour / trop clair : à surveiller", st == 'warn')
st, ps = TT.evaluate(mod(camera={'face': False}), LIVE); check("aucun visage détecté : à surveiller", st == 'warn' and 'visage' in codes(ps))
st, ps = TT.evaluate(mod(camera={'ok': False}), LIVE); check("caméra inaccessible : non prêt", st == 'not_ready' and 'camera' in codes(ps))
st, ps = TT.evaluate(mod(micro={'ok': False}), LIVE); check("micro inaccessible : non prêt", st == 'not_ready')
st, ps = TT.evaluate(mod(micro={'level': 0.001}), LIVE); check("micro silencieux : à surveiller", st == 'warn')
st, ps = TT.evaluate(mod(screen={'ok': False}), LIVE); check("écran non partagé : non prêt", st == 'not_ready')
st, ps = TT.evaluate(mod(network={'down_mbps': 0.4}), LIVE); check("débit 0,4 Mbit/s : non prêt", st == 'not_ready' and 'reseau' in codes(ps))
st, ps = TT.evaluate(mod(network={'latency_ms': 1500}), LIVE); check("latence 1,5 s : non prêt", st == 'not_ready')
st, ps = TT.evaluate(mod(network={'down_mbps': 2.0}), LIVE); check("débit juste suffisant : à surveiller", st == 'warn')
st, ps = TT.evaluate(mod(client={'is_client': True, 'version': '1.7.0'}), LIVE); check("application CEI Examen 1.7.0 : non prêt (le surveillant ne verrait pas l'écran)", st == 'not_ready' and 'version' in codes(ps))
st, ps = TT.evaluate(mod(client={'is_client': True, 'version': '1.8.0'}), LIVE); check("application 1.8.0 : prête", st == 'ready')
st, ps = TT.evaluate({}, LIVE); check("relevés vides ou absents : non prêt, sans erreur", st == 'not_ready'); TT.evaluate(None, None); TT.evaluate({'camera': 'x'}, {})
check("les messages sont en français et actionnables", all(len(p['message']) > 30 for p in TT.evaluate(mod(camera={'brightness': 5}), {**LIVE, 'camera': False})[1]))

allp = []
for kw in (dict(), dict(camera={'brightness': 5}), dict(network={'down_mbps': 0.3, 'latency_ms': 2000}), dict(client={'is_client': True, 'version': '1.7.0'}), dict(micro={'ok': False})):
    allp += TT.evaluate(mod(**kw), {'camera': False, 'mic': False, 'screen': False})[1] + TT.evaluate(mod(**kw), None)[1]
BAN = ('serveur', 'pare-feu', 'livekit', 'mbit', 'latence', ' ms', 'bloquant', 'vidéo (', 'webrtc', 'ice')
check("AUCUN message montré à l'étudiant ne contient de terme technique (serveur, pare-feu, Mbit/s, ms…)", allp and not [p['message'] for p in allp if any(b in p['message'].lower() for b in BAN)], [p['message'] for p in allp if any(b in p['message'].lower() for b in BAN)][:2])
check("AUCUN texte montré au SURVEILLANT n'en contient non plus", not [p['staff'] for p in allp if any(b in p['staff'].lower() for b in BAN)], [p['staff'] for p in allp if any(b in p['staff'].lower() for b in BAN)][:2])
check("le texte du surveillant dit ce qui se passe ET ce qu'il peut demander à l'étudiant (3e personne)", all(p.get('staff') and ' vous ' not in (' ' + p['staff'].lower() + ' ').replace("jusqu'à vous", '') for p in allp), [p['staff'] for p in allp if p.get('staff') and ' vous ' in (' ' + p['staff'].lower() + ' ').replace("jusqu'à vous", '')][:2])
check("… ex. caméra non transmise : « Sa caméra n'arrive pas jusqu'à vous »", any("Sa caméra n'arrive pas" in p['staff'] for p in allp))

print('\n== Lecture de ce que reçoit le serveur vidéo ==')
p = {'identity': 'techtest-1', 'tracks': [{'type': 'VIDEO', 'source': 'CAMERA', 'width': 640, 'height': 480}, {'type': 'AUDIO', 'source': 'MICROPHONE'}, {'type': 'VIDEO', 'source': 'SCREEN_SHARE', 'width': 1366, 'height': 768}]}
l = TT.summarize_participant(p); check("caméra + micro + écran reconnus, taille de la caméra relevée", l['camera'] and l['mic'] and l['screen'] and l['width'] == 640, l)
check("l'écran partagé n'est jamais pris pour la caméra", not TT.summarize_participant({'tracks': [{'type': 'VIDEO', 'source': 'SCREEN_SHARE'}]})['camera'])
check("piste coupée (muted) = absente", not TT.summarize_participant({'tracks': [{'type': 'VIDEO', 'source': 'CAMERA', 'muted': True}]})['camera'])
check("participant absent : rien reçu", TT.summarize_participant(None) == {'camera': False, 'mic': False, 'screen': False, 'width': 0, 'height': 0})
now = time.time()
check("statut vu du surveillant : jamais / récent / ancien (> 24 h)", TT.status_of_record(None) == 'never' and TT.status_of_record({'ts': now - 100, 'status': 'ready'}, now) == 'ready' and TT.status_of_record({'ts': now - 90000, 'status': 'ready'}, now) == 'stale')

print('\n== Routes ==')
s = get_session(); tag = uuid.uuid4().hex[:6]; made = {'users': [], 'ex': [], 'sub': None}
c = A.app.test_client(); rc = _get_client()
def mk(role, n):
    u = User(email=f'tt.{tag}.{n}@test.invalid', full_name=f'TT {n}', role=role, is_active=True, email_verified=True, has_email=True, password_hash=generate_password_hash('x'))
    s.add(u); s.commit(); made['users'].append(u.id); return u
def H(u): return {'Authorization': 'Bearer ' + create_access_token(u.id, u.role.value, u.email)}
try:
    adm, sv = mk(UserRole.ADMIN, 'adm'), mk(UserRole.SURVEILLANT, 'sv'); stu = [mk(UserRole.STUDENT, f'e{i}') for i in range(4)]
    r = c.post('/api/tech_test/livekit_token', headers=H(stu[0])); j = r.get_json() or {}
    check("jeton de test : salle PRIVÉE (jamais la salle d'examen), valable 15 min, sans droit d'écoute", r.status_code == 200 and j.get('token') and j.get('ws_url'), (r.status_code, j))
    import jwt as J
    claims = J.decode(j['token'], options={'verify_signature': False}); v = claims['video']
    check("… salle techtest-<id>, identité techtest-<id>, canSubscribe faux", v['room'] == f'techtest-{stu[0].id}' and claims['sub'] == f'techtest-{stu[0].id}' and v['canSubscribe'] is False and claims['exp'] - claims['nbf'] <= 900)
    r = c.post('/api/tech_test/verify', headers=H(stu[0])); j = r.get_json() or {}
    check("vérification serveur sans rien publié : joignable, rien reçu (connected faux)", r.status_code == 200 and j.get('reachable') and not j.get('connected') and not j.get('camera'), (r.status_code, j))
    r = c.post('/api/tech_test/result', json={'checks': GOOD}, headers=H(stu[0])); j = r.get_json() or {}
    check("le client affirme que tout va bien mais le serveur n'a rien reçu : NON prêt", j.get('status') == 'not_ready', j)
    rc.setex(f'cei:techtest:verify:{stu[1].id}', 600, json.dumps(LIVE))
    r = c.post('/api/tech_test/result', json={'checks': GOOD, 'ua': 'x'}, headers=H(stu[1])); j = r.get_json() or {}
    check("diffusion vérifiée par le serveur + relevés bons : prêt", j.get('status') == 'ready', j)
    rc.setex(f'cei:techtest:verify:{stu[2].id}', 600, json.dumps(LIVE))
    r = c.post('/api/tech_test/result', json={'checks': mod(camera={'brightness': 50})}, headers=H(stu[2])); j = r.get_json() or {}
    check("éclairage faible : à surveiller, avec le conseil", j.get('status') == 'warn' and j['problems'], j)
    r = c.get('/api/tech_test/me', headers=H(stu[1])); check("l'étudiant retrouve son dernier résultat", (r.get_json() or {}).get('status') == 'ready')
    r = c.post('/api/tech_test/result', json={'checks': GOOD}, headers=H(stu[0])); j = r.get_json() or {}
    check("la réponse faite à l'étudiant ne contient JAMAIS le détail technique", j.get('problems') and all('staff' not in p for p in j['problems']), j)
    r = c.get('/api/tech_test/me', headers=H(stu[0])); j = r.get_json() or {}
    check("… ni la relecture de son dernier résultat", all('staff' not in p for p in (j.get('record') or {}).get('problems', [])), j)
    r = c.post('/api/tech_test/result', data='pas du json', content_type='text/plain', headers=H(stu[3])); check("requête invalide : pas d'erreur 500", r.status_code == 200 and (r.get_json() or {}).get('status') == 'not_ready', r.status_code)

    adm_ = adm
    sub = Subject(title='tt', content='1. Q ? [QCM]\nA. a\nB. b\n', rubric='Question 1 (1 pt) : A', creator_id=adm.id)
    ec = s.query(EC).filter(EC.ue_id.isnot(None)).first(); sub.ec_id = ec.id; s.add(sub); s.commit(); made['sub'] = sub.id
    now_ = datetime.utcnow()
    ex = OnlineExam(subject_id=sub.id, title='tt', duration_minutes=60, start_time=now_ + timedelta(hours=1), end_time=now_ + timedelta(hours=3), status=ExamStatus.SCHEDULED, created_by_id=adm.id)
    s.add(ex); s.commit(); made['ex'].append(ex.id)
    for u in stu: s.add(StudentUEEnrollment(student_id=u.id, ue_id=ec.ue_id))
    s.add(ExamProctor(exam_id=ex.id, proctor_id=sv.id, assigned_by_id=adm.id)); s.commit()
    r = c.get(f'/api/online_exams/{ex.id}/readiness', headers=H(stu[0])); check("un étudiant ne peut pas consulter le tableau", r.status_code == 403)
    r = c.get(f'/api/online_exams/{ex.id}/readiness', headers=H(sv)); j = r.get_json() or {}
    mine = {x['student_id']: x for x in j.get('students', [])}
    cnt = j.get('counts', {})
    check("le surveillant voit les compteurs (prêt 1, à surveiller 1, non prêt 2 dont celui du test sans diffusion)", r.status_code == 200 and cnt.get('warn', 0) >= 1 and cnt.get('not_ready', 0) >= 2, (r.status_code, cnt))
    check("… l'étudiant PRÊT n'est pas listé ; les autres le sont avec leur cause", stu[1].id not in mine and stu[0].id in mine and mine[stu[0].id]['problems'], list(mine)[:6])
    check("… le surveillant lit son texte à lui (3e personne, sans jargon), pas le message de l'étudiant", any("n'arrive pas jusqu'à vous" in x or 'refaire' in x or 'lui demander' in x for x in mine[stu[0].id]['problems']) and not any('serveur' in x.lower() for x in mine[stu[0].id]['problems']), mine[stu[0].id]['problems'])
    check("… les non prêts d'abord", [x['status'] for x in j['students']][:2] == ['not_ready', 'not_ready'] or j['students'][0]['status'] == 'not_ready')
    sv2 = mk(UserRole.SURVEILLANT, 'sv2')
    r = c.get(f'/api/online_exams/{ex.id}/readiness', headers=H(sv2)); check("un surveillant non affecté à l'examen est refusé", r.status_code == 403)
    r = c.get(f'/api/online_exams/{ex.id}/readiness', headers=H(adm)); check("l'administrateur voit aussi le tableau", r.status_code == 200)
finally:
    s.rollback()
    try:
        for u in made['users']:
            for k in (f'cei:techtest:{u}', f'cei:techtest:verify:{u}'):
                try: rc.delete(k)
                except Exception: pass
        if made['ex']:
            s.query(ExamProctor).filter(ExamProctor.exam_id.in_(made['ex'])).delete(synchronize_session=False)
            s.query(OnlineExam).filter(OnlineExam.id.in_(made['ex'])).delete(synchronize_session=False)
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
