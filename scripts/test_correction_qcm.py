"""Un sujet entièrement noté automatiquement (QCM) doit voir sa note ENREGISTRÉE par la correction.
Régression constatée le 03/11/2026 (examen 149 : 52 copies « corrigées » sans note). Données : sujet 145 copié de la prod."""
import os, sys, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from types import SimpleNamespace as NS
from routes.exams import _run_ai_correction
d = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fixtures_sujet145.json')))
RES = []
def chk(n, ok, det=''):
    RES.append(bool(ok)); print(('  ✔ ' if ok else '  ✘ ') + n + (f'  — {det}' if det and not ok else ''))
for aid, ans in d['att']:
    subj = NS(content=d['content'], rubric=d['rubric'], title='145')
    att = NS(id=aid, answers=ans, score=None, feedback=None, question_scores=None, corrected_at=None, corrected_by_id=None,
             exam=NS(subject=subj, title='t', duration_minutes=40), student=NS(full_name='X'))
    sc = _run_ai_correction(att, corrected_by_id=88)
    chk(f"copie {aid} : note enregistrée ({sc}/20)", att.score is not None and att.score == sc and att.corrected_by_id == 88 and att.corrected_at is not None, (att.score, sc))
    chk(f"copie {aid} : détail par question enregistré", json.loads(att.question_scores) != [])
print(f"{sum(RES)}/{len(RES)} vérifications réussies"); sys.exit(0 if all(RES) else 1)
