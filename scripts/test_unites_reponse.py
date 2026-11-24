#!/usr/bin/env python
"""Unités de réponse des questions numériques / calculées (comme le « Calculated » de Moodle) — 24/11/2026.
    cd cei-api-v2 && .venv-base/bin/python scripts/test_unites_reponse.py     (calcul pur, aucune donnée)"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from services import question_types as QT
RES = []
def check(n, ok, d=''):
    RES.append(bool(ok)); print(('  ✔ ' if ok else '  ✘ ') + n + (f'  — {d}' if d and not ok else ''))

print('== Lecture du barème ==')
k = QT.extract_keys_from_block("Question 3 (1 pt) : Valeur : 5500 (± 1 %)\nUnité : W | kW = 0,001 | MW = 0,000001")
check("« Unité : W | kW = 0,001 | MW = 0,000001 » lue (principale + 2 unités acceptées)", k.get('units') == {'main': 'W', 'alts': {'kW': 0.001, 'MW': 1e-06}, 'required': False}, k.get('units'))
check("la valeur et la tolérance restent lues", k['number'] == {'value': 5500.0, 'tol': 1.0, 'percent': True})
check("« Unité obligatoire » exige l'unité", QT.extract_keys_from_block("Valeur : 12\nUnité obligatoire : m")['units']['required'] is True)
check("« Unités : m ; km = 0,001 » (séparateur ;) accepté", QT.extract_keys_from_block("Valeur : 12\nUnités : m ; km = 0,001")['units']['alts'] == {'km': 0.001})
check("sans ligne Unité : aucune clé d'unités (comportement d'avant)", 'units' not in QT.extract_keys_from_block("Valeur : 12,5 (± 0,1)"))
check("multiplicateur illisible ou nul : unité ignorée, pas d'erreur", QT.extract_keys_from_block("Valeur : 1\nUnité : m | km = abc | cm = 0")['units']['alts'] == {})

print('\n== Correction numérique (5500 W, ± 1 %) ==')
key = {'number': k['number'], 'units': k['units']}
g = lambda raw: QT.grade_numerique(key, raw)[0]
check("5500 (sans unité) : accepté (unité facultative)", g('5500') == 1.0)
check("5500 W : accepté", g('5500 W') == 1.0 and g('5500W') == 1.0)
check("5,5 kW (virgule) : converti → accepté", g('5,5 kW') == 1.0 and g('5.5kW') == 1.0)
check("0,0055 MW : converti → accepté", g('0,0055 MW') == 1.0)
check("5,6 kW : hors tolérance → refusé", g('5,6 kW') == 0.0)
check("5500 kg : unité inconnue → refusée", g('5500 kg') == 0.0)
check("5,5 KW : la casse compte (K ≠ k) → refusé", g('5,5 KW') == 0.0)
check("« abc » et vide : refusés", g('abc') == 0.0 and g('') == 0.0)
check("espaces insécables et milliers : « 5 500 W » accepté", g('5 500 W') == 1.0 and g('5 500 W') == 1.0)
req = {'number': k['number'], 'units': {**k['units'], 'required': True}}
check("unité obligatoire : « 5500 » seul refusé, « 5500 W » accepté", QT.grade_numerique(req, '5500')[0] == 0.0 and QT.grade_numerique(req, '5500 W')[0] == 1.0)
check("sans clé d'unités : comportement d'avant (« 12,5 m » lu 12,5)", QT.grade_numerique({'number': {'value': 12.5, 'tol': 0.1, 'percent': False}}, '12,5 m')[0] == 1.0)
check("unités composées « m/s », « m² », « kg·m » reconnues", QT.split_number_unit('3 m/s') == (3.0, 'm/s') and QT.split_number_unit('2,5 m²') == (2.5, 'm²') and QT.split_number_unit('4 kg·m') == (4.0, 'kgm'))
check("« 5 eV » : le « e » de l'unité n'est pas pris pour un exposant", QT.split_number_unit('5 eV') == (5.0, 'eV') and QT.split_number_unit('2,5e3 W') == (2500.0, 'W'))

print('\n== Calcul à variables ==')
vars_line = 'a = 2..2 | b = 3..3'      # valeur de la ligne « Variables : … »
kc = {'formula': {'expr': 'a*b*1000', 'tol': 0.0, 'percent': False}, 'units': {'main': 'W', 'alts': {'kW': 0.001}, 'required': False}}
r = QT.grade_calculee(kc, '6 kW', vars_line, 1, '1'); check("6000 W attendu : « 6 kW » accepté", r and r[0] == 1.0, r)
r = QT.grade_calculee(kc, '6000', vars_line, 1, '1'); check("… « 6000 » accepté", r and r[0] == 1.0, r)
r = QT.grade_calculee(kc, '6 mW', vars_line, 1, '1'); check("… « 6 mW » refusé (unité inconnue)", r and r[0] == 0.0, r)
print(f"\n{sum(RES)}/{len(RES)} vérifications réussies"); sys.exit(0 if all(RES) else 1)
