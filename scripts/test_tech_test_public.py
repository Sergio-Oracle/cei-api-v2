#!/usr/bin/env python
"""Test du poste SANS connexion (20/11/2026) : la transmission réelle (caméra, micro, écran) se vérifie sans compte ; l'enregistrement reste réservé à l'étudiant connecté ; la voie anonyme est bornée.

    cd cei-api-v2 && .venv-base/bin/python scripts/test_tech_test_public.py
"""
import os, sys, time, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as A
import routes.tech_test as RT
from cache import _get_client
import jwt as J

RESULTS = []
def check(nom, ok, detail=''):
    RESULTS.append((nom, bool(ok))); print(('  ✔ ' if ok else '  ✘ ') + nom + (f'  — {detail}' if detail and not ok else ''))
c = A.app.test_client(); rc = _get_client()
for k in rc.scan_iter('cei:techtest:anon:rate:*'): rc.delete(k)      # compteur propre pour ce test

print('\n== Jeton anonyme ==')
r = c.post('/api/tech_test/public/livekit_token'); j = r.get_json() or {}
check("sans aucune connexion : 200, un jeton, l'adresse du serveur vidéo et l'identifiant du test", r.status_code == 200 and j.get('token') and j.get('ws_url') and len(j.get('id', '')) == 16, (r.status_code, j))
cl = J.decode(j['token'], options={'verify_signature': False}); v = cl['video']
check("salle PROPRE à ce test (techtest-anon-<id>), jamais une salle d'examen", v['room'] == f"techtest-anon-{j['id']}" and cl['sub'] == v['room'], v)
check("publication seule : AUCUN droit d'écoute (on ne peut rien recevoir des autres)", v['canPublish'] is True and v['canSubscribe'] is False, v)
check("jeton de 3 minutes au plus", 0 < cl['exp'] - int(time.time()) <= 185, cl['exp'] - int(time.time()))
j2 = (c.post('/api/tech_test/public/livekit_token').get_json() or {})
check("chaque test a SA salle (deux tests ne partagent jamais la même)", j2.get('id') and j2['id'] != j['id'])
check("aucune donnée de compte dans le jeton ni la réponse", not any(k in json.dumps(j) for k in ('user', 'email', 'student')), j)
r = c.post('/api/tech_test/livekit_token'); check("la voie AVEC compte reste fermée aux visiteurs (401)", r.status_code == 401, r.status_code)
r = c.post('/api/tech_test/result', json={'checks': {}}); check("l'enregistrement du résultat reste réservé aux étudiants connectés (401)", r.status_code == 401, r.status_code)

print('\n== Vérification côté serveur ==')
tid = j['id']
r = c.post('/api/tech_test/public/verify', json={'id': tid}); jj = r.get_json() or {}
check("test connu, rien publié : joignable, rien reçu (connected faux)", r.status_code == 200 and jj.get('reachable') and jj.get('connected') is False and not jj.get('camera'), (r.status_code, jj))
orig = RT.list_participants
try:
    room = f'techtest-anon-{tid}'
    RT.list_participants = lambda rm: [{'identity': room, 'tracks': [{'type': 'VIDEO', 'source': 'CAMERA', 'width': 640, 'height': 480}, {'type': 'AUDIO'}, {'type': 'VIDEO', 'source': 'SCREEN_SHARE'}]}]
    r = c.post('/api/tech_test/public/verify', json={'id': tid}); jj = r.get_json() or {}
    check("caméra, micro et écran reçus par le serveur : reconnus (même lecture que pour un étudiant connecté)", jj.get('camera') and jj.get('mic') and jj.get('screen') and jj.get('connected') and jj.get('width') == 640, jj)
    RT.list_participants = lambda rm: [{'identity': 'quelquun-d-autre', 'tracks': [{'type': 'VIDEO', 'source': 'CAMERA'}]}]
    jj = c.post('/api/tech_test/public/verify', json={'id': tid}).get_json() or {}
    check("la piste d'un AUTRE participant n'est jamais attribuée à ce test", jj.get('connected') is False and not jj.get('camera'), jj)
    RT.list_participants = lambda rm: None
    r = c.post('/api/tech_test/public/verify', json={'id': tid})
    check("serveur vidéo injoignable : 503 propre, pas une erreur 500", r.status_code == 503, r.status_code)
finally:
    RT.list_participants = orig
r = c.post('/api/tech_test/public/verify', json={'id': 'a' * 16}); check("identifiant bien formé mais JAMAIS émis par le serveur : refusé (on ne peut pas sonder une salle quelconque)", r.status_code == 404, r.status_code)
for bad in ('../../etc', 'techtest-5', '', 'A' * 16, 'g' * 16, 'a' * 15):
    r = c.post('/api/tech_test/public/verify', json={'id': bad}); check(f"identifiant invalide {bad[:20]!r} : 400", r.status_code == 400, r.status_code)
r = c.post('/api/tech_test/public/verify', data='pas du json', content_type='text/plain'); check("requête invalide : 400, pas de 500", r.status_code == 400, r.status_code)
rc.delete(RT._anon_key(tid)); r = c.post('/api/tech_test/public/verify', json={'id': tid}); check("test expiré : 404 « relancez-le »", r.status_code == 404 and 'expiré' in (r.get_json() or {}).get('error', ''), r.status_code)

print('\n== Bornes contre l\'abus ==')
k = f'cei:techtest:anon:rate:{int(time.time() // 60)}'
rc.set(k, RT.ANON_PER_MINUTE); rc.expire(k, 120)
r = c.post('/api/tech_test/public/livekit_token'); check("plafond GLOBAL atteint (toute la plateforme) : 429 avec un message simple", r.status_code == 429 and 'minute' in (r.get_json() or {}).get('error', ''), (r.status_code, r.get_json()))
rc.delete(k)
check("le plafond global est modeste (≤ 300 tests par minute)", RT.ANON_PER_MINUTE <= 300)
check("la durée de vie du jeton est courte (≤ 3 min)", RT.ANON_TTL_S <= 180)
bad = [n for n, ok in RESULTS if not ok]
print(f"\n{len(RESULTS) - len(bad)}/{len(RESULTS)} vérifications réussies" + (f" — ÉCHECS : {bad}" if bad else ''))
sys.exit(1 if bad else 0)
