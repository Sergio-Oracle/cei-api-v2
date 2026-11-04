#!/usr/bin/env python
"""Renouvellement de session (/api/auth/refresh) — limite par jeton, rejeu d'une réponse perdue, journal des refus.
Devoir du 03/11/2026 : des étudiants renvoyés à l'écran de connexion (429 : 30/min PAR IP pour toute une salle ;
401 : ancien jeton rejoué après une réponse perdue).
    cd cei-api-v2 && .venv-base/bin/python scripts/test_renouvellement_session.py   (crée puis supprime ses propres données)"""
import sys, uuid, logging
sys.path.insert(0, '.')
from app import app
from models import *
from routes.exams import get_session
from extensions import bcrypt
from auth_paseto import COOKIE_NAME, create_refresh_token, hash_token
from cache import cache_delete
from routes.auth import _refresh_grace_key

s = get_session(); sfx = uuid.uuid4().hex[:6]; c = app.test_client(use_cookies=False); RES = []; ids = []
def chk(n, ok, d=''):
    RES.append(bool(ok)); print(('  ✔ ' if ok else '  ✘ ') + n + (f'  — {d}' if d and not ok else ''))

class Cap(logging.Handler):
    def __init__(self): super().__init__(); self.msgs = []
    def emit(self, r): self.msgs.append(r.getMessage())
cap = Cap(); logging.getLogger('cei.api').addHandler(cap); logging.getLogger('cei.api').setLevel(logging.INFO)

def cookie_of(resp):
    for h in resp.headers.getlist('Set-Cookie'):
        if h.startswith(COOKIE_NAME + '='): return h.split(';', 1)[0].split('=', 1)[1]
def refresh(tok, **kw):
    h = {'Cookie': f'{COOKIE_NAME}={tok}'} if tok else {}
    return c.post('/api/auth/refresh', headers=h, **kw)

try:
    pw = 'Test-' + sfx + '-xY9'
    u = User(email=f'zz_rt_{sfx}@test.local', full_name='ZZ renouvellement', role=UserRole.STUDENT, is_active=True,
             password_hash=bcrypt.generate_password_hash(pw).decode()); s.add(u); s.commit(); ids.append(u.id); uid = u.id
    r = c.post('/api/auth/login', json={'email': u.email, 'password': pw}); rt1 = cookie_of(r)
    chk('connexion : cookie de renouvellement posé', r.status_code == 200 and rt1, (r.status_code, r.get_json()))

    print('Renouvellement normal et rejeu d\'une réponse perdue')
    r = refresh(rt1); rt2 = cookie_of(r); j = r.get_json() or {}
    chk('renouvellement : 200, nouveau cookie, jeton d\'accès, profil', r.status_code == 200 and rt2 and rt2 != rt1 and j.get('access_token') and j.get('user', {}).get('id') == uid, (r.status_code, j))
    r = refresh(rt1); j = r.get_json() or {}
    chk('réponse perdue : l\'ancien jeton rejoué reçoit le MÊME nouveau cookie (200, pas de déconnexion)', r.status_code == 200 and cookie_of(r) == rt2 and j.get('access_token'), (r.status_code, j))
    r = refresh(rt2); rt3 = cookie_of(r)
    chk('le nouveau jeton fonctionne ensuite', r.status_code == 200 and rt3 and rt3 != rt2, r.status_code)
    r = refresh(rt1); chk('le client est passé au jeton suivant : rejouer le tout premier est refusé (401)', r.status_code == 401, r.status_code)
    r = refresh(rt3); rt4 = cookie_of(r)
    cache_delete(_refresh_grace_key(hash_token(rt3)))
    r = refresh(rt3); chk('fenêtre de rejeu écoulée : ancien jeton refusé (401)', r.status_code == 401, r.status_code)
    r = refresh(None); chk('sans cookie (visiteur non connecté) : 401', r.status_code == 401, r.status_code)
    r = refresh('v4.local.jeton-invalide'); chk('jeton illisible : 401', r.status_code == 401, r.status_code)

    print('Limite : par jeton et non par IP')
    toks = [create_refresh_token(uid) for _ in range(45)]
    codes = [refresh(t).status_code for t in toks]
    chk('45 étudiants d\'une même IP, 45 jetons distincts en une minute : aucun 429 (avant : 429 au-delà de 30)', 429 not in codes and codes.count(200) == 45, sorted(set(codes)))
    t = create_refresh_token(uid); codes = [refresh(t).status_code for _ in range(26)]
    chk('un même jeton martelé : bloqué par la limite par jeton (429 avant la 26e)', 429 in codes, codes)

    print('Journal des refus')
    m = [x for x in cap.msgs if 'refresh refusé' in x]
    chk('refus journalisé avec raison et compte (jeton déjà utilisé)', any('raison=jeton_deja_utilise' in x and f'user={uid}' in x for x in m), m[:3])
    chk('refus journalisé avec raison (jeton illisible)', any('raison=jeton_expire_ou_invalide' in x for x in m), m[:5])
    chk('rejeu journalisé', any('refresh rejoué' in x and f'user={uid}' in x for x in cap.msgs))
finally:
    s.close(); s2 = get_session()
    s2.query(TokenBlocklist).filter(TokenBlocklist.user_id.in_(ids)).delete(synchronize_session=False)
    for i in ids:
        x = s2.get(User, i)
        if x: s2.delete(x)
    s2.commit(); print('compte de test supprimé')
ok = sum(RES); print(f'\n{ok}/{len(RES)} vérifications réussies'); sys.exit(0 if ok == len(RES) else 1)
