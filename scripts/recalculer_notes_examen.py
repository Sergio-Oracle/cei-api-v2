#!/usr/bin/env python
"""Recalcule les notes d'un examen (voir services/grade_recompute.py). Sans option : simulation, rien n'est écrit.

    cd cei-api-v2 && .venv-base/bin/python scripts/recalculer_notes_examen.py 140            # simulation
    cd cei-api-v2 && .venv-base/bin/python scripts/recalculer_notes_examen.py 140 --appliquer # écrit (sauvegarde avant)
"""
import os, sys, collections
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.env'))
from models import get_session
from services.grade_recompute import recompute

exam_id = int(sys.argv[1]); apply = '--appliquer' in sys.argv
s = get_session()
r = recompute(s, exam_id, apply=apply)
b, a = r['before'], r['after']
print(f"Examen {exam_id} : {r['copies']} copies notées — {'APPLIQUÉ' if apply else 'SIMULATION (rien écrit)'}")
print(f"  recalculées : {len(a)} (modifiées {r['changed']} : {r['lower']} à la baisse, {r['higher']} à la hausse ; inchangées {r['unchanged']})")
print(f"  laissées telles quelles : {r['skipped_empty']} copies vides, {r['skipped_manual']} notes saisies à la main, {r['skipped_edited']} détails retouchés, {r['skipped_unverifiable']} non vérifiables")
if a:
    print(f"  moyenne {sum(b)/len(b):.2f} → {sum(a)/len(a):.2f} | 20/20 : {sum(1 for x in b if x >= 19.995)} → {sum(1 for x in a if x >= 19.995)}")
if r['backup']:
    print('  sauvegarde des anciennes valeurs :', r['backup'])
s.close()
