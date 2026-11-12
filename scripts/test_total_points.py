#!/usr/bin/env python
"""Le total des points d'un sujet déposé est TOUJOURS celui demandé (incident « Test Blanc », 12/11/2026 : 10 points au lieu de 20).

    cd cei-api-v2 && .venv-base/bin/python scripts/test_total_points.py

Cause : le document Word ne donnait aucun point ; l'IA a rédigé le barème (1,5 + 1,5 + 2 + 2,5 + 2,5 = 10) et rien ne vérifiait la somme.
Ce n'était PAS un problème de lecture du document (OCR) : la lecture était exacte."""
import os, sys, io, re, uuid, random
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as A
from models import get_session, User, UserRole, Subject
from auth_paseto import create_access_token
from services import points_total as P

RESULTS = []
def check(nom, ok, detail=''):
    RESULTS.append((nom, bool(ok)))
    print(('  ✔ ' if ok else '  ✘ ') + nom + (f'  — {detail!r}' if not ok else ''))

DOC = ("--- Fichier: TEST DIAGNOSTIC.docx ---\nTEST DIAGNOSTIC\n" + ''.join(
    f"Question {i} — Titre {i} [QCM]\nÉnoncé {i} ?\nA. un\nB. deux\nC. trois\nD. quatre\nRéponse attendue : {k}\n"
    for i, k in enumerate('BABBB', 1)))
AI_RUBRIC = ("=== BARÈME DE NOTATION ===  \n" + ''.join(
    f"Question {i} ({p} pts) :  \n  Bonne réponse : {k}) — justification.  \n\n" for i, (p, k) in enumerate(zip((1.5, 1.5, 2, 2.5, 2.5), 'BABBB'), 1)) + "Total : 10 points.")

print('== Répartition exacte ==')
rnd = random.Random(7)
ok = True
for _ in range(300):
    n = rnd.randint(1, 60); tgt = rnd.choice([10, 20, 30, 40, 60, 100])
    parts = P.allocate(tgt, [rnd.choice([0.5, 1, 1.5, 2, 2.5, 3]) for _ in range(n)])
    if abs(sum(parts) - tgt) > 0.001 or (n <= tgt * 4 and min(parts) < P.GRAIN - 1e-9) or len(parts) != n:
        ok = False; print('   contre-exemple', n, tgt, parts)
check("300 répartitions aléatoires : somme EXACTE, jamais de question à 0 point", ok)
check("1,5 / 1,5 / 2 / 2,5 / 2,5 sur 20 → 3 / 3 / 4 / 5 / 5 (mêmes proportions)", P.allocate(20, [1.5, 1.5, 2, 2.5, 2.5]) == [3, 3, 4, 5, 5], P.allocate(20, [1.5, 1.5, 2, 2.5, 2.5]))
check("7 questions égales sur 20 : somme 20, écart maximal 0,25", (lambda a: abs(sum(a) - 20) < 1e-9 and max(a) - min(a) <= 0.25)(P.allocate(20, [1] * 7)))

print('\n== Cas réel « Test Blanc » ==')
tot, _ = P.current_total(DOC, AI_RUBRIC)
check("le barème de l'IA totalisait 10 points", tot == 10.0, tot)
c2, r2, changed, old = P.enforce_total(DOC, AI_RUBRIC, 20)
tot2, pm2 = P.current_total(c2, r2)
check("ramené à 20 points, mêmes proportions", changed and old == 10.0 and tot2 == 20.0 and [pm2[str(i)] for i in range(1, 6)] == [3, 3, 4, 5, 5], pm2)
check("la ligne « Total » du barème est à jour", 'Total : 20 points' in r2 and 'Total : 10' not in r2, r2[-60:])
check("les bonnes réponses ne bougent pas", [m for m in re.findall(r'Bonne réponse : ([A-D])\)', r2)] == list('BABBB'))
c3, r3, changed3, _ = P.enforce_total(c2, r2, 20)
check("deuxième passage : rien à changer (idempotent)", not changed3 and (c3, r3) == (c2, r2))

print('\n== Les points écrits par l\'enseignant dans son document ne sont JAMAIS modifiés ==')
OFFICIEL = DOC.replace('[QCM]', '(2 pts) [QCM]')
c4, r4, changed4, _ = P.enforce_total(OFFICIEL, AI_RUBRIC, 20)
check("document avec « (2 pts) » : total 10 conservé", not changed4 and r4 == AI_RUBRIC)
check("le même document peut être ramené à 20 À LA DEMANDE (bouton de l'éditeur)", P.current_total(*P.rescale_points(OFFICIEL, AI_RUBRIC, 20)[:2])[0] == 20.0)

print('\n== Questions à critères : les critères suivent ==')
OPEN_C = "Question 1 — Définissez (3 pts) [OUVERT]\nDéfinissez.\n\nQuestion 2 — Citez (1 pts) [OUVERT]\nCitez.\n"
OPEN_R = ("BARÈME DE NOTATION\n\nQuestion 1 (3 pts) :\n  • Critère 1 : 1 pt — définition\n  • Critère 2 : 2 pts — exemple\n\n"
          "Question 2 (1 pts) :\n  • Critère : 1 pt — mot juste\n\n──────────\nTOTAL : 4 / 4 points")
c5, r5, ch5, _ = P.enforce_total(OPEN_C.replace(' (3 pts)', '').replace(' (1 pts)', ''), OPEN_R, 20)
check("barème de 4 → 20 points : Q1 = 15, Q2 = 5", ch5 and (lambda m: m['1'] == 15 and m['2'] == 5)(P.current_total(c5, r5)[1]), P.current_total(c5, r5))
q1 = r5.split('Question 2')[0]
crit = [float(x.replace(',', '.')) for x in re.findall(r'Critère \d : ([\d.,]+) pts?', q1)]
check("les critères de la question 1 totalisent ses points (15)", abs(sum(crit) - 15) < 0.01, crit)
check("TOTAL : 20 / 20 points", 'TOTAL : 20 / 20 points' in r5, r5[-40:])

print('\n== Sujet généré (parties, note totale) ==')
GEN_C = ("Durée : 90 minutes | Note totale : 20 points\nVous disposez de 90 minutes, noté sur 20 points.\nQUESTIONS\n\nPartie — QCM (2 pts, 2 questions)\n\n"
         "Question 1 — A (1 pts) [QCM]\nÉ ?\nA) a\nB) b\n\nQuestion 2 — B (1 pts) [QCM]\nÉ ?\nA) a\nB) b\n")
GEN_R = ("BARÈME DE NOTATION\n\nQuestion 1 — A (1 pts)\n  • Bonne réponse : A) — x\n\nQuestion 2 — B (1 pts)\n  • Bonne réponse : B) — y\n\n──────────\nTOTAL : 2 / 2 points")
c6, r6, _ = P.rescale_points(GEN_C, GEN_R, 20)
check("titres, partie, « Note totale » et TOTAL tous à 20", '(10 pts) [QCM]' in c6 and 'Partie — QCM (20 pts, 2 questions)' in c6 and 'Note totale : 20 points' in c6 and 'noté sur 20 points' in c6 and 'TOTAL : 20 / 20 points' in r6, (c6[:260], r6[-40:]))

print('\n== La bonne réponse écrite dans le document fait foi ==')
bad = AI_RUBRIC.replace('Question 3 (2 pts) :  \n  Bonne réponse : B)', 'Question 3 (2 pts) :  \n  Bonne réponse : C)')
r7, fixes = P.reconcile_inline_keys(DOC, bad)
check("barème de l'IA divergent (Q3 : C au lieu de B) → aligné sur le document", fixes == [('3', 'C', 'B')] and re.findall(r'Bonne réponse : ([A-D])\)', r7) == list('BABBB'), (fixes, r7[:80]))
check("barème conforme : aucune correction", P.reconcile_inline_keys(DOC, AI_RUBRIC)[1] == [])
check("document sans réponse écrite : barème laissé tel quel", P.reconcile_inline_keys(re.sub(r'Réponse attendue : .\n', '', DOC), bad)[1] == [])

print('\n== Route « Ramener sur 20 » de l\'éditeur ==')
s = get_session(); made = []
try:
    tag = uuid.uuid4().hex[:6]
    adm = User(email=f'tot.{tag}.a@test.invalid', full_name='Tot A', role=UserRole.ADMIN, is_active=True, email_verified=True, has_email=True, password_hash='x')
    st = User(email=f'tot.{tag}.s@test.invalid', full_name='Tot S', role=UserRole.STUDENT, is_active=True, email_verified=True, has_email=True, password_hash='x')
    s.add_all([adm, st]); s.commit(); made = [adm.id, st.id]
    c = A.app.test_client()
    H = lambda u: {'Authorization': 'Bearer ' + create_access_token(u.id, u.role.value, u.email)}
    r = c.post('/api/subjects/rescale_points', json={'content': DOC, 'rubric': AI_RUBRIC, 'total_points': 20}, headers=H(adm)); j = r.get_json() or {}
    check("enseignant/admin : 10 → 20, sans rien enregistrer", r.status_code == 200 and j.get('previous_total') == 10.0 and j.get('total') == 20.0 and j.get('changed'), (r.status_code, str(j)[:200]))
    check("étudiant : refusé", c.post('/api/subjects/rescale_points', json={'content': DOC, 'rubric': AI_RUBRIC}, headers=H(st)).status_code == 403)
    check("sujet ou barème vide : refusé proprement", c.post('/api/subjects/rescale_points', json={'content': DOC, 'rubric': ''}, headers=H(adm)).status_code == 400)

    print('\n== Dépôt complet d\'un fichier : l\'IA rend 10 points, le sujet enregistré en fait 20 ==')
    import services.subject_service as SS
    from werkzeug.datastructures import FileStorage
    import services.subject_fidelity as FID
    real_ai, real_review = SS._call_ai, FID.ai_review
    import services.ai_service as AIS
    real_simple = AIS.call_ai_simple
    AIS.call_ai_simple = lambda *a, **k: '1: B\n2: A\n3: B\n4: B\n5: D'      # seconde lecture : en désaccord sur la question 5
    SS._call_ai = lambda *a, **k: AI_RUBRIC
    FID.ai_review = lambda *a, **k: {'ok': True, 'issues': []}
    try:
        up = SS.SubjectService.upload('ZZ total points', [FileStorage(io.BytesIO(DOC.replace('--- Fichier: TEST DIAGNOSTIC.docx ---\n', '').encode('utf-8')), filename=f'zz_total_{tag}.txt', content_type='text/plain')],
                                      adm.id, UserRole.ADMIN, None, 'QCM', 'ai', 20)
    finally:
        SS._call_ai, FID.ai_review, AIS.call_ai_simple = real_ai, real_review, real_simple
    made_sub = up.get('id')
    s.expire_all(); sub = s.get(Subject, made_sub)
    tot_saved, pm_saved = P.current_total(sub.content, sub.rubric)
    check("sujet enregistré : 20 points, proportions de l'IA conservées", tot_saved == 20.0 and [pm_saved[str(i)] for i in range(1, 6)] == [3, 3, 4, 5, 5], pm_saved)
    check("l'enseignant en est informé", 'ramené à 20 points' in (up.get('detection', {}).get('points_note') or ''), up.get('detection', {}).get('points_note'))
    w = up.get('detection', {}).get('warnings') or []
    check("seconde lecture des réponses : désaccord signalé « à relire » (question 5 seulement)", sum('Question 5 : à relire' in x for x in w) == 1 and not any('Question 1 : à relire' in x for x in w), w)
    check("le contrôle du sujet est accepté, sans information de total", __import__('services.subject_check', fromlist=['analyze']).analyze(sub.content, sub.rubric)['issues'] == [], '')
    s.query(Subject).filter(Subject.id == made_sub).delete(synchronize_session=False); s.commit()
finally:
    try:
        s.query(Subject).filter(Subject.title == 'ZZ total points').delete(synchronize_session=False)
        if made: s.query(User).filter(User.id.in_(made)).delete(synchronize_session=False)
        s.commit()
    except Exception as e:
        s.rollback(); print('nettoyage incomplet :', e)
    s.close()
bad = [n for n, ok in RESULTS if not ok]
print(f"\n{len(RESULTS) - len(bad)}/{len(RESULTS)} vérifications réussies" + (f" — ÉCHECS : {bad}" if bad else ''))
sys.exit(1 if bad else 0)
