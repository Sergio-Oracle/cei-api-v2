#!/usr/bin/env python
"""L'inscription publique est fermée par défaut (16/11/2026) ; rouverte seulement par ALLOW_PUBLIC_REGISTRATION.
    cd cei-api-v2 && .venv-base/bin/python scripts/test_inscription_publique.py   (aucune donnée créée)"""
import os, sys, uuid
sys.path.insert(0, '.')
from app import app
from extensions import limiter
from models import get_session, User

limiter.enabled = False      # la limite « 10 par heure » de la route ne doit pas fausser le test (ni être consommée par lui)

c = app.test_client(use_cookies=False); RES = []
def chk(n, ok, d=''):
    RES.append(bool(ok)); print(('  ✔ ' if ok else '  ✘ ') + n + (f'  — {str(d)[:200]}' if d and not ok else ''))
mail = f'zz_inscription_{uuid.uuid4().hex[:8]}@test.local'
body = {'email': mail, 'password': 'Motdepasse#2026', 'full_name': 'ZZ Inscription'}
os.environ.pop('ALLOW_PUBLIC_REGISTRATION', None)
r = c.post('/api/auth/register', json=body)
chk("fermée par défaut : 403", r.status_code == 403, (r.status_code, r.get_data(as_text=True)))
s = get_session()
chk("… et aucun compte n'est créé", s.query(User).filter_by(email=mail).count() == 0); s.close()
chk("corps vide : toujours 403 (pas de 500 ni d'erreur interne)", c.post('/api/auth/register', json={}).status_code == 403)
chk("la réponse n'expose aucune erreur interne", "'password'" not in c.post('/api/auth/register', json={}).get_data(as_text=True))
os.environ['ALLOW_PUBLIC_REGISTRATION'] = '1'
try:
    r = c.post('/api/auth/register', json={})
    chk("rouverte par le réglage : corps vide → 400 clair (et non 500)", r.status_code == 400 and 'requis' in r.get_data(as_text=True), (r.status_code, r.get_data(as_text=True)))
    r = c.post('/api/auth/register', json={'email': mail, 'password': 'x'})
    chk("rouverte : nom manquant → 400", r.status_code == 400)
finally:
    os.environ.pop('ALLOW_PUBLIC_REGISTRATION', None)
print(f"\n{sum(RES)}/{len(RES)} vérifications réussies"); sys.exit(0 if all(RES) else 1)
