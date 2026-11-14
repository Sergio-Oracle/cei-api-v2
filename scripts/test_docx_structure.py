#!/usr/bin/env python
"""Lecteur universel Word (phase C) : structure réelle du fichier conservée, aucune perte, repli sûr.

    cd cei-api-v2 && .venv-base/bin/python scripts/test_docx_structure.py

Sans pandoc (ni binaire système, ni pypandoc_binary) le test est IGNORÉ (code 0) : le lecteur d'origine reste en service.
"""
import os, subprocess, sys, tempfile
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from services import doc_structure as D
if not D.pandoc_path():
    print("Essais lecteur universel ignorés : pandoc absent."); sys.exit(0)
import utils
from services.subject_normalizer import normalize
from routes.exams import _parse_subject_blocks_ordered as parse

FIX = os.path.join(ROOT, 'scripts', 'fixtures')
RES = []
def check(nom, ok, detail=''):
    RES.append(ok); print(('OK   ' if ok else 'ECHEC'), nom, ('— ' + detail) if (detail and not ok) else '')

def make_docx(markdown, name):
    d = tempfile.mkdtemp(); md = os.path.join(d, name + '.md'); out = os.path.join(d, name + '.docx')
    open(md, 'w').write(markdown)
    subprocess.run([D.pandoc_path(), '-f', 'markdown+fancy_lists', md, '-o', out], check=True)
    return out

def read(path, engine):
    os.environ['CEI_DOCX_ENGINE'] = engine
    return utils.extract_text_from_docx(path)

# 1. numérotation automatique de Word : l'ancien lecteur la perd, le nouveau la régénère
SUJET = """**Exercice 1. Questionnaire** (4 points)

Consigne : une seule réponse est exacte.

1. Quelle est la capitale du Sénégal ?

    (A) Thiès
    (B) Dakar
    (C) Saint-Louis
    (D) Kaolack

2. Quel est le plus long fleuve du Sénégal ?

    (A) Le Sénégal
    (B) La Gambie
    (C) Le Saloum
    (D) La Casamance

3. Combien font 2 + 2 ?

    (A) 3
    (B) 4
    (C) 5
    (D) 22
"""
p = make_docx(SUJET, 'auto')
old, new = read(p, 'legacy'), read(p, 'pandoc')
check("ancien lecteur : numéros automatiques perdus (cas de départ)", '1. Quelle' not in old and 'Quelle est la capitale' in old)
check("nouveau lecteur : numéros régénérés 1. 2. 3.", all(f'{n}. ' in new for n in (1, 2, 3)) and '1. Quelle est la capitale' in new, new[:200])
# Word enregistre le format de la liste (« A. », « A) » ou « (A) ») : on le respecte tel quel
check("nouveau lecteur : propositions A B C D régénérées", all(any(f in new for f in (f'{c}. ', f'{c}) ', f'({c}) ')) for c in 'ABCD') and 'Dakar' in new, new[:300])
bl = parse(normalize(new)[0])
q = [b for b in bl if b['type'] == 'qcm']
check("sujet auto-numéroté : 3 QCM reconnus avec 4 propositions chacun", len(q) == 3 and all(len(b.get('choices') or b.get('options') or []) == 4 for b in q), str([(b['type']) for b in bl]))
check("titre « Exercice 1 » séparé de la consigne", any(b['type'] == 'section' for b in bl))

# 2. sous-questions a) b) c) en liste lettrée
SOUS = """**Exercice 2. Cas pratique**

Une société anonyme est constituée entre trois associés.

a) Quel est le capital minimal ?

b) Qui nomme le commissaire aux comptes ?

c) Quelle est la durée maximale du mandat ?
"""
p2 = make_docx(SOUS, 'sous')
n2 = read(p2, 'pandoc')
check("liste lettrée régénérée a) b) c)", all(f'{c}) ' in n2 for c in 'abc'), n2)

# 2b. sujet SANS numéros (cas de Dr Mballo) : la frontière de paragraphe de Word sépare énoncé et question, même sans ponctuation
SANS = """**Exercice 1. Cas des employés** (4 points)

Consigne : une seule réponse est exacte.

Les faits suivants sont établis

Qui a pris la clé

A. Awa

B. Moussa

Combien d'employés ont menti

A. 1

B. 2
"""
d3 = tempfile.mkdtemp(); open(d3 + '/s.md', 'w').write(SANS)
subprocess.run([D.pandoc_path(), '-f', 'markdown-fancy_lists', '-t', 'docx', d3 + '/s.md', '-o', d3 + '/s.docx'], check=True)
from services.subject_normalizer import loose_mcq
t3 = read(d3 + '/s.docx', 'pandoc'); o3, k3, n3 = loose_mcq(t3)
b3 = [(b['type'], b.get('title') or b.get('text')) for b in parse(normalize(o3)[0])]
check("sans numéros : titre + consigne + énoncé à part, questions « Qui a pris la clé » et « Combien… » seules",
      n3 == 2 and b3[0][0] == 'section' and b3[1][1] == 'Qui a pris la clé' and b3[2][1].startswith("Combien d"), str(b3))
check("sans numéros : l'énoncé « Les faits suivants sont établis » n'est pas collé à la question", 'Les faits suivants sont établis Qui' not in o3 and 'Les faits suivants sont établis' in o3)

# 3. non-régression sur les vrais fichiers Word : mêmes questions, mêmes types, aucune perte de mots
for nom in ('epreuve_methodologie_exercices', 'corrige_methodologie_exercices'):
    f = os.path.join(FIX, nom + '.docx')
    if not os.path.exists(f):
        print('IGNORE', nom, ': fichier absent'); continue
    a, b = read(f, 'legacy'), read(f, 'pandoc')
    check(f"{nom} : couverture des mots = 100 %", D.coverage(a, b) >= 0.999, str(D.coverage(a, b)))
    ta = [x['type'] for x in parse(normalize(a)[0])]; tb = [x['type'] for x in parse(normalize(b)[0])]
    check(f"{nom} : mêmes blocs ({len(ta)}) dans le même ordre", ta == tb, f"{len(ta)} vs {len(tb)}")

# 4. garde-fous
check("moteur par défaut = ancien lecteur", (os.environ.pop('CEI_DOCX_ENGINE', None) or True) and D.engine() == 'legacy')
check("fichier illisible : extract() renvoie None, pas d'exception", D.extract('/nonexistent.docx', 'abc') is None)
check("perte de contenu détectée (couverture < 97 %) => repli", D.coverage('un deux trois quatre cinq six sept huit neuf dix', 'un deux') < 0.97)
os.environ['CEI_DOCX_ENGINE'] = 'pandoc'
open(os.path.join(tempfile.gettempdir(), 'faux.docx'), 'wb').write(b'pas un zip')
check("faux .docx avec moteur pandoc : pas d'exception", utils.extract_text_from_docx(os.path.join(tempfile.gettempdir(), 'faux.docx')) in (None, ''))
print(f"\n{sum(RES)}/{len(RES)} OK"); sys.exit(0 if all(RES) else 1)
