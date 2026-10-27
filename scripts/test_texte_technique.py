#!/usr/bin/env python
"""Garde-fou : aucun texte technique (« ### », « Points obtenus : X ») ne doit atteindre l'écran.
    cd cei-api-v2 && .venv-base/bin/python scripts/test_texte_technique.py"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from text_clean import clean_ai_feedback as c
cas = [
    ("### Question 1\nPoints : 1.00/1.00\nOK\n\nPoints obtenus : 18.00\n\nNote totale: 19.00/20", ["###", "Points obtenus"], ["Question 1", "Points : 1.00/1.00", "Note totale: 19.00/20"]),
    ("  ## Titre\ntexte\n  Points obtenus : 0,5  \n", ["##", "Points obtenus"], ["Titre", "texte"]),
    (None, [], []), ("", [], []),
]
ok = 0
for texte, absent, present in cas:
    r = c(texte)
    bon = all(a not in (r or '') for a in absent) and all(p in (r or '') for p in present)
    ok += bon
    print(('  ✔ ' if bon else '  ✘ ') + repr((texte or '')[:40]))
print(f'{ok}/{len(cas)} cas réussis')
sys.exit(0 if ok == len(cas) else 1)
