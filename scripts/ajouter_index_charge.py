#!/usr/bin/env python
"""Ajoute (sans verrouiller les écritures) les index nécessaires à la charge de 10 000 étudiants (19/11/2026).  Idempotent : peut être relancé sans risque.

    cd cei-api-v2 && .venv-base/bin/python scripts/ajouter_index_charge.py [--dry]

ix_exam_activity_logs_attempt_type_ts (attempt_id, event_type, timestamp) : les interrogations des messages/appels de l'étudiant (toutes les 30 s par chaque étudiant) filtrent par copie
ET par type d'événement. Avec des index séparés, le planificateur peut choisir l'index sur l'heure : il ramène alors les événements des 2 dernières minutes de TOUS les étudiants
(≈ 100 000 lignes à 10 000 étudiants) pour n'en garder que quelques-unes. L'index composite rend le plan déterministe et minuscule.
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import app
from models import engine
from sqlalchemy import text
INDEXES = [
    ('ix_exam_activity_logs_attempt_type_ts', 'exam_activity_logs', '(attempt_id, event_type, "timestamp")'),
]
dry = '--dry' in sys.argv
with engine.connect().execution_options(isolation_level='AUTOCOMMIT') as c:
    c.execute(text("SET statement_timeout = 0"))
    for name, table, cols in INDEXES:
        exists = c.execute(text("select 1 from pg_indexes where indexname = :n"), {'n': name}).fetchone()
        print(f"{name}: {'déjà présent' if exists else ('à créer' if dry else 'création…')}")
        if not exists and not dry:
            c.execute(text(f'CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} ON {table} {cols}'))
            c.execute(text(f'ANALYZE {table}'))
            print(f'{name}: créé')
