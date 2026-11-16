#!/usr/bin/env python
"""Épreuve + corrigé Word avec DEUX tableaux d'association (8 notions, 10 définitions a→j) : rien ne doit disparaître, le barème est lu depuis le corrigé.
Constat du 16/11/2026 (sujet « Épreuve de synthèse », examen de test du 17/11) : les lignes 7 et 8 (lettres g, h) devenaient des questions ouvertes, les définitions i, j
étaient perdues, et le corrigé (« Réponse : C. », « FAUX », « Définition exacte ») n'était pas lu : barème vide.
    cd cei-api-v2 && .venv-base/bin/python scripts/test_association_corrige.py   (fabrique ses propres fichiers dans /tmp, aucune donnée créée)"""
import os, sys, tempfile, collections
os.environ['CEI_DOCX_ENGINE'] = 'pandoc'
sys.path.insert(0, '.')
import docx
from app import app  # noqa: F401
from utils import extract_text_from_file
from services.exam_normalizer import normalize_exam_text
from services.type_detector import detect_and_annotate
from services.corrige_reader import read_corrige_file, merge, rubric_from_exercise_key
from routes.exams import _parse_subject_blocks_ordered as P

RES = []
def chk(n, ok, d=''):
    RES.append(bool(ok)); print(('  ✔ ' if ok else '  ✘ ') + n + (f'  — {str(d)[:220]}' if d and not ok else ''))

NOTIONS_A = ['Auteur', 'Date', 'Nature', 'Plan', 'Idée', 'Linéaire', 'Composé', 'Liminaire']
NOTIONS_B = ['Entonnoir', 'Intérêt', 'Chapeau', 'Idées', 'Explicitation', 'Paraphrase', 'Plané', 'Discussion']
LET = 'abcdefghij'
KEY_A = [4, 7, 5, 9, 0, 6, 1, 2]           # indices (0 = a) : la définition juste de chaque notion ; 3 et 8 = leurres
KEY_B = [3, 6, 4, 7, 1, 0, 8, 5]
def defs(prefix): return [f"{prefix} définition numéro {i + 1} rédigée pour le test, assez longue pour ressembler à un vrai énoncé." for i in range(10)]
DA, DB = defs('Alpha'), defs('Bêta')

def ex_table(d, notions, dd, head):
    t = d.add_table(rows=11, cols=2); t.rows[0].cells[0].text, t.rows[0].cells[1].text = head
    for i in range(10):
        t.rows[i + 1].cells[0].text = f"{i + 1}. {notions[i]}" if i < 8 else ''
        t.rows[i + 1].cells[1].text = f"{LET[i]}. {dd[i]}"

d = docx.Document()
d.add_paragraph('Évaluation de synthèse'); d.add_paragraph('Épreuve')
d.add_paragraph('Exercice 1. Questionnaire à choix multiples (2 points)')
d.add_paragraph('Consigne : une seule réponse est exacte. 1 point par bonne réponse.')
for i in range(1, 3):
    p = d.add_paragraph(f'{i}. Question numéro {i} du questionnaire ?')
    for L in 'ABCD': d.add_paragraph(f'{L}. Proposition {L} de la question {i}')
d.add_paragraph('Exercice 2. Vrai ou faux (2 points)')
t = d.add_table(rows=3, cols=4); [setattr(t.rows[0].cells[k], 'text', v) for k, v in enumerate(['N°', 'Affirmation', 'Vrai', 'Faux'])]
for i in (1, 2): t.rows[i].cells[0].text = str(i); t.rows[i].cells[1].text = f'Affirmation numéro {i} à juger.'; t.rows[i].cells[2].text = '☐'; t.rows[i].cells[3].text = '☐'
d.add_paragraph('Exercice 3. Appariement : les étapes (4 points)')
d.add_paragraph('Consigne : Associez chaque élément de la colonne de gauche à la définition qui lui correspond. Deux définitions ne correspondent à aucun élément. 0,5 point par association.')
ex_table(d, NOTIONS_A, DA, ('Éléments', 'Définitions'))
d.add_paragraph('Exercice 4. Texte à trous (2 points)')
d.add_paragraph('Consigne : Complétez chaque phrase. 1 point par réponse.')
d.add_paragraph('1. Le mot « introduction » vient du latin intro ducere, qui signifie conduire ……….')
d.add_paragraph('2. Le système en entonnoir va du général au ……….')
d.add_paragraph('Exercice 5. Association : dissertation ou commentaire (4 points)')
d.add_paragraph('Consigne : Associez chaque notion à sa définition. Deux définitions ne correspondent à aucune notion. 0,5 point par association.')
ex_table(d, NOTIONS_B, DB, ('Notions', 'Définitions'))
tmp = tempfile.mkdtemp(); fe = os.path.join(tmp, 'E.docx'); d.save(fe)

c = docx.Document()
c.add_paragraph('Corrigé')
c.add_paragraph('Exercice 1. Corrigé du questionnaire à choix multiples (2 points)')
for i, L in ((1, 'C'), (2, 'A')):
    c.add_paragraph(f'{i}. Question numéro {i} du questionnaire ?'); c.add_paragraph(f'Réponse : {L}. Proposition {L} de la question {i}'); c.add_paragraph('Justification du corrigé.')
c.add_paragraph('Exercice 2. Corrigé du vrai ou faux (2 points)')
t = c.add_table(rows=3, cols=4); [setattr(t.rows[0].cells[k], 'text', v) for k, v in enumerate(['N°', 'Affirmation', 'Réponse', 'Justification'])]
for i, v in ((1, 'FAUX'), (2, 'VRAI')): t.rows[i].cells[0].text = str(i); t.rows[i].cells[1].text = f'Affirmation numéro {i} à juger.'; t.rows[i].cells[2].text = v; t.rows[i].cells[3].text = 'Justification.'
c.add_paragraph("Exercice 3. Corrigé de l'appariement (4 points)")
t = c.add_table(rows=9, cols=2); t.rows[0].cells[0].text, t.rows[0].cells[1].text = 'Élément', 'Définition exacte'
for i in range(8): t.rows[i + 1].cells[0].text = f"{i + 1}. {NOTIONS_A[i]}"; t.rows[i + 1].cells[1].text = f"{LET[KEY_A[i]]}. {DA[KEY_A[i]]}"
c.add_paragraph('Grille : ' + ', '.join(f'{i + 1} {LET[KEY_A[i]]}' for i in range(8)) + '.')
c.add_paragraph('Exercice 4. Corrigé du texte à trous (2 points)')
for num, before, ans, after in ((1, '1. Le mot « introduction » vient du latin intro ducere, qui signifie ', 'dedans', ''), (2, '2. Le système en entonnoir va du général au ', 'particulier', '')):
    p = c.add_paragraph(); p.add_run(before); r = p.add_run(ans); r.bold = True      # la réponse est EN GRAS dans le corrigé Word
    p.add_run(after)
c.add_paragraph("Exercice 5. Corrigé de l'association (4 points)")
t = c.add_table(rows=9, cols=2); t.rows[0].cells[0].text, t.rows[0].cells[1].text = 'Notion', 'Définition exacte'
for i in range(8): t.rows[i + 1].cells[0].text = f"{i + 1}. {NOTIONS_B[i]}"; t.rows[i + 1].cells[1].text = f"{LET[KEY_B[i]]}. {DB[KEY_B[i]]}"
c.add_paragraph('Grille : ' + ', '.join(f'{i + 1} {LET[KEY_B[i]]}' for i in range(8)) + '.')
fc = os.path.join(tmp, 'Corrige.docx'); c.save(fc)

E_txt = extract_text_from_file(fe); C_txt = extract_text_from_file(fc)
part, nrep = normalize_exam_text(E_txt)
annotated, rep = detect_and_annotate("--- Fichier: E.docx ---\n" + part, [])
bl = P(annotated); cnt = collections.Counter(b['type'] for b in bl)
assoc = [b for b in bl if b['type'] == 'association']
chk("deux questions d'association reconnues", len(assoc) == 2, dict(cnt))
chk("chaque association garde ses 8 éléments et ses 10 définitions (a à j)", all(len(b.get('items') or []) == 8 and len(b.get('options') or b.get('choices') or []) == 10 for b in assoc), [(len(b.get('items') or []), len(b.get('options') or [])) for b in assoc])
chk("aucune ligne d'association ne devient une question ouverte", cnt.get('open', 0) == 0, dict(cnt))
chk("la dernière définition (j) est bien dans le sujet", all(DA[9][:30] in annotated and DB[9][:30] in annotated for _ in (0,)))
key = merge([read_corrige_file(fc, C_txt)])
chk("corrigé : réponses des QCM lues (« Réponse : C. »)", key['qcm'] == ['C', 'A'], key['qcm'])
chk("corrigé : vrai/faux lus quelle que soit la casse (« FAUX », « VRAI »)", key['vf'] == ['Faux', 'Vrai'], key['vf'])
ae = [x for x in key['assoc_ex'] if x]
chk("corrigé : UNE série de clés par exercice d'association (2), lettres jusqu'à j", len(ae) == 2 and ae[0].get(4) == 'J' and ae[1].get(7) == 'I', ae)
rub, warns = rubric_from_exercise_key(annotated, key)
chk("barème écrit depuis le corrigé, sans aucune alerte", bool(rub) and not warns, warns)
exp3 = ', '.join(f"{i + 1}-{LET[KEY_A[i]].upper()}" for i in range(8)); exp5 = ', '.join(f"{i + 1}-{LET[KEY_B[i]].upper()}" for i in range(8))
chk("clés d'association du barème = Grille du corrigé (exercice 3 PUIS exercice 5, sans mélange)", f"Réponse : {exp3}" in rub and f"Réponse : {exp5}" in rub, [l for l in rub.splitlines() if 'Réponse : 1-' in l])
chk("barème : total de 14 points", 'TOTAL : 14 / 14' in rub, ' | '.join(l.strip() for l in rub.splitlines() if 'pts)' in l))
print(f"\n{sum(RES)}/{len(RES)} vérifications réussies"); sys.exit(0 if all(RES) else 1)
