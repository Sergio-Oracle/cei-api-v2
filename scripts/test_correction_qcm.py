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
# garde-fou : une correction qui ne laisse aucune note doit échouer bruyamment, jamais « réussir »
import routes.exams as R
_orig = R._run_ai_correction_impl
try:
    R._run_ai_correction_impl = lambda a, corrected_by_id=None: 5.0          # « calcule » une note mais ne l'enregistre pas
    bad = NS(id=-1, answers='{}', score=None)
    try: R._run_ai_correction(bad, corrected_by_id=1); lev = False
    except RuntimeError: lev = True
    chk("garde-fou : une note non enregistrée lève une erreur", lev)
    R._run_ai_correction_impl = lambda a, corrected_by_id=None: None
    try: R._run_ai_correction(bad, corrected_by_id=1); lev = False
    except RuntimeError: lev = True
    chk("garde-fou : une correction sans note lève une erreur", lev)
finally:
    R._run_ai_correction_impl = _orig
print(f"{sum(RES)}/{len(RES)} vérifications réussies"); sys.exit(0 if all(RES) else 1)
