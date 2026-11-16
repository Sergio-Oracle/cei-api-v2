#!/usr/bin/env python
"""Aucune limite par IP ne doit bloquer des étudiants qui partagent une adresse (réseau mobile, centre d'examen) — 16/11/2026.
Les protections restent : par COMPTE à la connexion, par ADRESSE E-MAIL pour « mot de passe oublié ».
    cd cei-api-v2 && .venv-base/bin/python scripts/test_limites_etudiants.py   (aucune donnée créée : comptes et jetons inexistants)"""
import sys, uuid, random
sys.path.insert(0, '.')
from app import app
from extensions import limiter, ENTRY_LIMIT

RES = []
def chk(n, ok, d=''):
    RES.append(bool(ok)); print(('  ✔ ' if ok else '  ✘ ') + n + (f'  — {str(d)[:200]}' if d and not ok else ''))
limiter.enabled = True
ip = f'10.{random.randint(1, 250)}.{random.randint(1, 250)}.{random.randint(1, 250)}'      # adresse neuve : aucun compteur résiduel
c = app.test_client(use_cookies=False)
def post(url, body, addr=None): return c.post(url, json=body, environ_base={'REMOTE_ADDR': addr or ip})

n = int(ENTRY_LIMIT.split()[0])
chk("limite d'entrée par IP au moins 5 000 par minute (réglable ENTRY_RATE_LIMIT)", n >= 5000 and 'minute' in ENTRY_LIMIT, ENTRY_LIMIT)
codes = [post('/api/auth/oidc/force-login', {'retry_token': 'inexistant-' + uuid.uuid4().hex}).status_code for _ in range(400)]
chk("400 connexions SSO depuis UNE même IP en quelques secondes : aucun refus 429 (avant : bloqué après 30)", 429 not in codes and set(codes) == {400}, sorted(set(codes)))
codes = [c.get('/api/auth/oidc/login', environ_base={'REMOTE_ADDR': ip}).status_code for _ in range(120)]
chk("120 démarrages SSO depuis une même IP : aucun 429 (avant : bloqué après 30)", 429 not in codes, sorted(set(codes)))
codes = [post('/api/auth/forgot-password', {'email': f'inexistant_{uuid.uuid4().hex[:10]}@test.local'}).status_code for _ in range(60)]
chk("60 « mot de passe oublié » (adresses différentes) depuis une même IP : aucun 429 (avant : bloqué après 5)", 429 not in codes, sorted(set(codes)))
mail = f'inexistant_{uuid.uuid4().hex[:10]}@test.local'
codes = [post('/api/auth/forgot-password', {'email': mail}, addr=f'10.8.{random.randint(1, 250)}.{i + 1}').status_code for i in range(8)]
chk("la MÊME adresse e-mail ne peut pas être inondée (6e demande de l'heure refusée), même depuis des IP différentes", codes[:5] == [200] * 5 and 429 in codes[5:], codes)
mail2 = f'inexistant_{uuid.uuid4().hex[:10]}@test.local'
codes = [post('/api/auth/login', {'email': mail2, 'password': 'x'}).status_code for _ in range(13)]
chk("connexion : la protection PAR COMPTE reste en place (devinage de mot de passe limité)", 429 in codes and codes[0] == 401, codes)
codes = [post('/api/auth/login', {'email': f'inexistant_{uuid.uuid4().hex[:8]}@test.local', 'password': 'x'}).status_code for _ in range(150)]
chk("150 comptes DIFFÉRENTS qui se connectent depuis une même IP : aucun 429 (le blocage ne vise que l'adresse e-mail)", 429 not in codes, sorted(set(codes)))
print(f"\n{sum(RES)}/{len(RES)} vérifications réussies"); sys.exit(0 if all(RES) else 1)
