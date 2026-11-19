#!/usr/bin/env python
"""Rééquilibrage des surveillants : le nouvel algorithme (O(n log n)) donne EXACTEMENT les mêmes résultats que l'ancien (O(n²)) et reste rapide à 5 000 étudiants (19/11/2026).

    cd cei-api-v2 && .venv-base/bin/python scripts/test_equilibrage_surveillants.py     (calcul pur, aucune donnée)

Au test de charge de 5 000 étudiants, le battement du surveillant qui déclenchait le rééquilibrage restait bloqué 23 s (n² : 25 millions d'opérations Python).
"""
import os, sys, random, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from services.proctor_service import plan_balance

RESULTS = []
def check(nom, ok, detail=''):
    RESULTS.append((nom, bool(ok))); print(('  ✔ ' if ok else '  ✘ ') + nom + (f'  — {detail}' if detail and not ok else ''))

def ancien(owners, present):
    """Copie fidèle de l'ancienne boucle (référence)."""
    items = [[o] for o in owners]
    load = {pid: 0 for pid in present}
    for (o,) in items:
        if o in load: load[o] += 1
    moved = orphans = 0
    for it in items:
        if it[0] not in load:
            t = min(load, key=load.get); it[0] = t; load[t] += 1; orphans += 1; moved += 1
    for _ in range(len(items)):
        hi = max(load, key=load.get); lo = min(load, key=load.get)
        if load[hi] - load[lo] < 2: break
        it = next((x for x in reversed(items) if x[0] == hi), None)
        if it is None: break
        it[0] = lo; load[hi] -= 1; load[lo] += 1; moved += 1
    return [x[0] for x in items], orphans, moved

random.seed(19112026)
diff = 0; n_cas = 3000
for k in range(n_cas):
    P = random.randint(1, 12); allp = list(range(100, 100 + P + random.randint(0, 4)))
    present = random.sample(allp, random.randint(1, len(allp)))
    owners = [random.choice(allp) for _ in range(random.randint(0, 80))]
    if plan_balance(owners, present) != ancien(owners, present):
        diff += 1
check(f"{n_cas} cas aléatoires : résultats identiques à l'ancien algorithme (propriétaires, orphelins, déplacements)", diff == 0, f'{diff} différences')

print('\n== Propriétés ==')
for k in range(500):
    allp = list(range(10)); present = random.sample(allp, random.randint(1, 10)); owners = [random.choice(allp) for _ in range(random.randint(1, 300))]
    new, orph, mv = plan_balance(owners, present)
    cnt = [new.count(p) for p in present]
    if not (set(new) <= set(present) and max(cnt) - min(cnt) <= 1): check("équilibre ≤ 1 et seuls des présents", False, (present, cnt)); break
else:
    check("500 cas : plus aucun étudiant chez un absent, écart de charge ≤ 1", True)
check("liste vide : rien à faire", plan_balance([], [1, 2]) == ([], 0, 0))

print('\n== Vitesse ==')
owners = [random.choice(range(100)) for _ in range(5000)]; present = list(range(30))      # 100 surveillants affectés, 30 seulement présents
t = time.time(); new, orph, mv = plan_balance(owners, present); dt = time.time() - t
check(f"5 000 étudiants, 100 surveillants dont 30 présents : {dt*1000:.0f} ms (< 500 ms)", dt < 0.5, dt)
check("… et le résultat est équilibré", max(new.count(p) for p in present) - min(new.count(p) for p in present) <= 1)
bad = [n for n, ok in RESULTS if not ok]
print(f"\n{len(RESULTS) - len(bad)}/{len(RESULTS)} vérifications réussies" + (f" — ÉCHECS : {bad}" if bad else ''))
sys.exit(1 if bad else 0)
