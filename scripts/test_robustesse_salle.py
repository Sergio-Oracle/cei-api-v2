#!/usr/bin/env python
"""Robustesse de la session pour une SALLE d'examen derrière une seule IP (devoir du 03/11/2026 : déconnexions).
 1. 300 étudiants renouvellent leur session ensemble (jetons expirant à la même heure), plusieurs cycles, avec 20 %
    de réponses perdues (l'ancien jeton est rejoué) et 10 % de doubles requêtes simultanées (deux onglets) :
    aucun ne doit finir déconnecté (401) ni limité (429).
 2. Le trafic authentifié courant d'une salle (2 500 requêtes en une minute depuis la même IP) ne doit jamais
    recevoir de 429 : la limite générale est par COMPTE, plus par IP.
    cd cei-api-v2 && .venv-base/bin/python scripts/test_robustesse_salle.py   (crée puis supprime ses propres comptes)"""
import sys, uuid, random, threading, time
sys.path.insert(0, '.')
from concurrent.futures import ThreadPoolExecutor
from app import app
from models import *
from routes.exams import get_session
from auth_paseto import COOKIE_NAME, create_refresh_token, create_access_token

N = 300; CYCLES = 5; random.seed(7)
s = get_session(); sfx = uuid.uuid4().hex[:6]; RES = []; uids = []; profs = []
def chk(n, ok, d=''):
    RES.append(bool(ok)); print(('  ✔ ' if ok else '  ✘ ') + n + (f'  — {d}' if d and not ok else ''))
def cookie_of(resp):
    for h in resp.headers.getlist('Set-Cookie'):
        if h.startswith(COOKIE_NAME + '='): return h.split(';', 1)[0].split('=', 1)[1]
def call(tok):
    return app.test_client(use_cookies=False).post('/api/auth/refresh', headers={'Cookie': f'{COOKIE_NAME}={tok}'})

fails = []; codes = {}; lock = threading.Lock()
def bump(c):
    with lock: codes[c] = codes.get(c, 0) + 1

def renew(tok):
    """Comportement du client : 409 → réessaie avec le cookie à jour ; sinon ce que le serveur répond."""
    for _ in range(4):
        r = call(tok); bump(r.status_code)
        if r.status_code == 200: return cookie_of(r)
        if r.status_code == 409: time.sleep(0.2); continue
        return None
    return None

def student(i):
    tok = create_refresh_token(uids[i]); rnd = random.Random(i)
    for cyc in range(CYCLES):
        if rnd.random() < 0.10:                       # deux onglets en même temps avec le même jeton
            out = []
            ts = [threading.Thread(target=lambda: out.append(renew(tok))) for _ in range(2)]
            [t.start() for t in ts]; [t.join() for t in ts]
            good = [o for o in out if o]
            if not good: fails.append((i, cyc, 'deux onglets')); return
            tok = good[0]
        else:
            new = renew(tok)
            if not new: fails.append((i, cyc, 'normal')); return
            if rnd.random() < 0.20:                   # la réponse s'est perdue : le client rejoue l'ANCIEN jeton
                again = renew(tok)
                if not again: fails.append((i, cyc, 'rejeu')); return
                new = again
            tok = new

try:
    for i in range(N):
        u = User(email=f'zz_salle_{sfx}_{i}@test.local', full_name=f'ZZ salle {i}', role=UserRole.STUDENT, is_active=True, password_hash='x'); s.add(u)
    for i in range(40):
        u = User(email=f'zz_salle_p{sfx}_{i}@test.local', full_name=f'ZZ salle prof {i}', role=UserRole.PROFESSOR, is_active=True, password_hash='x'); s.add(u)
    s.commit()
    rows = s.query(User).filter(User.email.like(f'zz_salle_%{sfx}%')).all()
    uids = [u.id for u in rows if u.role == UserRole.STUDENT]; profs = [u for u in rows if u.role == UserRole.PROFESSOR]
    assert len(uids) == N and len(profs) == 40

    print(f'Renouvellement simultané : {N} étudiants, {CYCLES} cycles, réponses perdues et doubles onglets')
    t0 = time.time()
    with ThreadPoolExecutor(64) as ex: list(ex.map(student, range(N)))
    print(f'   {sum(codes.values())} requêtes en {time.time()-t0:.1f} s, codes : {dict(sorted(codes.items()))}')
    chk(f'{N} étudiants × {CYCLES} cycles : aucun ne finit déconnecté', not fails, fails[:5])
    chk('aucun 429 (limite) pendant le renouvellement de la salle', codes.get(429, 0) == 0, codes)
    chk('aucun 401 inattendu (ni jeton rejoué refusé, ni session perdue)', codes.get(401, 0) == 0, codes)
    chk('aucun 409 : deux onglets simultanés reçoivent tous deux une réponse valide', codes.get(409, 0) == 0, codes)

    print('Trafic courant d\'une salle depuis une seule IP : 2 500 requêtes en une minute')
    toks = [create_access_token(u.id, u.role.value, u.email) for u in profs]
    c = app.test_client(use_cookies=False); got = {}
    def hit(k):
        r = c.get('/api/auth/me', headers={'Authorization': 'Bearer ' + toks[k % len(toks)]}); return r.status_code
    with ThreadPoolExecutor(32) as ex:
        for code in ex.map(hit, range(2500)): got[code] = got.get(code, 0) + 1
    print(f'   codes : {got}')
    chk('2 500 requêtes authentifiées d\'une même IP : aucune limitée (429)', got.get(429, 0) == 0 and got.get(200, 0) == 2500, got)
finally:
    s.close(); s2 = get_session()
    ids = [u.id for u in s2.query(User).filter(User.email.like(f'zz_salle_%{sfx}%')).all()]
    s2.query(TokenBlocklist).filter(TokenBlocklist.user_id.in_(ids)).delete(synchronize_session=False)
    s2.query(User).filter(User.id.in_(ids)).delete(synchronize_session=False)
    s2.commit(); print('comptes de test supprimés')
ok = sum(RES); print(f'\n{ok}/{len(RES)} vérifications réussies'); sys.exit(0 if ok == len(RES) else 1)
