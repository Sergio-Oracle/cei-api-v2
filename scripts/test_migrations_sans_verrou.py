#!/usr/bin/env python
"""init_db() n'émet AUCUNE instruction de schéma (ALTER/CREATE/DROP/LOCK) quand la base est à jour (19/11/2026).

    cd cei-api-v2 && .venv-base/bin/python scripts/test_migrations_sans_verrou.py

Constat (test de charge à 10 000 étudiants) : chaque `import app` rejouait « ALTER TABLE proctor_assignments … DROP NOT NULL », qui prend un verrou EXCLUSIF sur la table et met en file
d'attente toutes les lectures et écritures suivantes — 72 requêtes bloquées derrière lui. Un outil de contrôle lancé toutes les 10 s suffisait à geler la plateforme par à-coups.
"""
import os, re, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
RESULTS = []
def check(nom, ok, detail=''):
    RESULTS.append((nom, bool(ok))); print(('  ✔ ' if ok else '  ✘ ') + nom + (f'  — {detail}' if detail and not ok else ''))
from sqlalchemy import event
import models
models.init_db()                                   # amène la base à jour (premier passage : peut émettre des instructions)
ddl = []
@event.listens_for(models.engine, 'before_cursor_execute')
def _b(conn, cur, st, par, ctx, many):
    if re.match(r'\s*(ALTER|CREATE|DROP|LOCK|TRUNCATE|REINDEX)', st, re.I): ddl.append(st[:100])
for k in range(3): models.init_db()                # chaque démarrage, script ou outil qui importe l'application
check("base à jour : 3 appels de init_db() n'émettent aucune instruction de schéma", not ddl, ddl[:3])
_g = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'scripts/charge_prod/guard.py')
if os.path.exists(_g):                             # absent quand le test est lancé depuis un dossier temporaire sur un serveur
    src = open(_g, encoding='utf-8').read()
    check("le garde-fou du test de charge n'importe pas l'application (pas de init_db toutes les 10 s)", not re.search(r'^\s*(from app import|import app\b)', src, re.M))
bad = [n for n, ok in RESULTS if not ok]
print(f"\n{len(RESULTS) - len(bad)}/{len(RESULTS)} vérifications réussies" + (f" — ÉCHECS : {bad}" if bad else ''))
sys.exit(1 if bad else 0)
