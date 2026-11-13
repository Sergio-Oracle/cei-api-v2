#!/usr/bin/env python
"""Nettoyage ponctuel des sujets (12/11/2026) : sujet 142 remis sur 20 points ; sujets défectueux jamais utilisés archivés.

    python scripts/nettoyage_sujets_12nov.py            # simulation, n'écrit rien
    python scripts/nettoyage_sujets_12nov.py --apply    # écrit (une seule transaction, annulée au moindre contrôle en échec)

Garde-fous, revérifiés juste avant l'écriture : aucun examen, aucune copie, aucune tentative liés au sujet ; le sujet 142 n'a pas
de points donnés par le document ; après recalcul le total est EXACTEMENT 20 et le contrôle du sujet n'a aucune erreur."""
import os, sys, json, datetime
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.env'))
import psycopg2
from services.points_total import current_total, rescale_points, document_gives_points
from services import subject_check

APPLY = '--apply' in sys.argv
RESCALE = [142]
ARCHIVE = [28, 29, 39, 61, 65, 77, 90, 114, 115]
conn = psycopg2.connect(os.environ['DATABASE_URL'].replace('postgresql+psycopg2', 'postgresql'))
cur = conn.cursor()

def liens(sid):
    cur.execute("select count(*) from online_exams where subject_id=%s", (sid,)); e = cur.fetchone()[0]
    cur.execute("select count(*) from student_papers where subject_id=%s", (sid,)); p = cur.fetchone()[0]
    return e, p

def erreurs(c, r):
    a = subject_check.analyze(c, r)
    return [i for i in (a.get('issues') if isinstance(a, dict) else a) if i.get('level') == 'error']

cur.execute("select id,title,content,rubric,is_active,creator_id,ec_id,created_at from subjects where id = any(%s) order by id", (RESCALE + ARCHIVE,))
rows = {r[0]: r for r in cur.fetchall()}
backup = {str(k): {'title': v[1], 'content': v[2], 'rubric': v[3], 'is_active': v[4]} for k, v in rows.items()}
plan = []
for sid in RESCALE + ARCHIVE:
    if sid not in rows:
        sys.exit(f"ERREUR : sujet {sid} introuvable")
    e, p = liens(sid)
    if e or p:
        sys.exit(f"ARRÊT : sujet {sid} lié à {e} examen(s) / {p} copie(s) — on n'y touche pas")
for sid in RESCALE:
    _, title, c, r, act, *_ = rows[sid]
    if document_gives_points(c):
        sys.exit(f"ARRÊT : sujet {sid} a ses points dans le document")
    before, _ = current_total(c, r)
    c2, r2, new = rescale_points(c, r, 20)
    after, _ = current_total(c2, r2)
    err0, err1 = erreurs(c, r), erreurs(c2, r2)
    print(f"sujet {sid} : total {before} -> {after} ; erreurs de contrôle {len(err0)} -> {len(err1)} ; questions {len(new)}")
    if abs(after - 20) > 0.001 or err1:
        sys.exit(f"ARRÊT : sujet {sid} ne passe pas les contrôles après recalcul")
    plan.append(('rescale', sid, c2, r2))
for sid in ARCHIVE:
    print(f"sujet {sid} : archivé ({rows[sid][1][:45]!r}) — actif={rows[sid][4]}")
    plan.append(('archive', sid))
if not APPLY:
    print("\nSimulation terminée : rien n'a été écrit."); sys.exit(0)

bak = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'backups')
os.makedirs(bak, exist_ok=True)
path = os.path.join(bak, f"sujets_avant_nettoyage_{datetime.datetime.utcnow():%Y%m%d_%H%M%S}.json")
with open(path, 'w', encoding='utf-8') as f:
    json.dump(backup, f, ensure_ascii=False, indent=1)
print("Sauvegarde :", path)
try:
    for step in plan:
        if step[0] == 'rescale':
            cur.execute("update subjects set content=%s, rubric=%s where id=%s", (step[2], step[3], step[1]))
        else:
            cur.execute("update subjects set is_active=false where id=%s", (step[1],))
        assert cur.rowcount == 1
    # contrôle final dans la même transaction
    for sid in RESCALE:
        cur.execute("select content,rubric from subjects where id=%s", (sid,)); c, r = cur.fetchone()
        t, _ = current_total(c, r)
        assert abs(t - 20) <= 0.001 and not erreurs(c, r), f"sujet {sid} : contrôle final KO ({t})"
    for sid in ARCHIVE:
        cur.execute("select is_active from subjects where id=%s", (sid,)); assert cur.fetchone()[0] is False
    conn.commit(); print("OK : transaction validée.")
except Exception as ex:
    conn.rollback(); sys.exit(f"ANNULÉ : {ex}")
