#!/usr/bin/env python
"""Énoncés de partie conservés et affichés (incident du 02/11/2026, sujet « Devoir2-IA »).

    cd cei-api-v2 && .venv-base/bin/python scripts/test_enonces.py

Une enseignante voyait ses questions sans l'énoncé de chaque partie (« On utilise les variables… », « Rappel des déclarations… ») :
le lecteur ignorait toute ligne qui n'était ni question ni proposition, ne reconnaissait « Partie A / B » que pour les chiffres
romains, et un énoncé recollé à « 1. Q1. » faisait disparaître la question. Sujet fabriqué ici (aucun contenu réel).
Vérifie : normalisation du texte recollé, lecteur du serveur, lecteur du navigateur (même résultat), partie sans énoncé.
"""
import json, os, re, subprocess, sys, tempfile
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import app  # noqa: F401
from routes.exams import _parse_subject_blocks_ordered as parse_py
from services.subject_normalizer import normalize

FRONT = os.path.join(os.path.dirname(ROOT), 'cei-next', 'app', 'exam', '[id]', 'page.tsx')
RES = []
def check(nom, ok, detail=''):
    RES.append(bool(ok)); print(('  ✔ ' if ok else '  ✘ ') + nom + (f'  — {detail}' if detail and not ok else ''))

PROPS = "A. oui\nB. non\nC. peut-être\nD. jamais\n"
BON = ("Partie A — Les feux\n"
       "On note r = « le feu est rouge » et v = « le feu est vert ».\n\n"
       f"1. Q1. Si r alors pas v ? [QCM]\n{PROPS}\n"
       f"2. Q2. Peut-on avoir r et v ? [QCM]\n{PROPS}\n"
       "Partie B — Les témoins\n"
       "Rappel des déclarations :\nA : « Il était vert »\nB : « Il était rouge »\n\n"
       f"3. Q3. Qui ment ? [QCM]\n{PROPS}\n"
       "Partie C — Les verdicts\n\n"
       f"4. Q4. Question sans énoncé propre ? [QCM]\n{PROPS}")
COLLE = BON.replace("v = « le feu est vert ».\n\n1. Q1.", "v = « le feu est vert ». 1. Q1.").replace("B : « Il était rouge »\n\n3. Q3.", "B : « Il était rouge » 3. Q3.")

# 1. texte recollé → remis sur ses lignes ; idempotent ; texte correct inchangé
n, notes = normalize(COLLE)
check("énoncé recollé à « 1. Q1. » : remis sur deux lignes", len(notes) == 1 and '2 question' in notes[0], notes)
check("normalisation idempotente", normalize(n)[0] == n)
check("texte déjà correct : inchangé", normalize(BON)[0] == BON)
check("une vraie phrase numérotée n'est pas coupée", normalize("Voir le point 3. Q3. est la suite")[0] == "Voir le point 3. Q3. est la suite")

# 2. lecteur du serveur
b = parse_py(n)
qs = [x for x in b if x['type'] not in ('section', 'description', 'text')]
check("4 questions lues (aucune perdue)", [q['num'] for q in qs] == ['1', '2', '3', '4'], [q['num'] for q in qs])
secs = [x['title'] for x in b if x['type'] == 'section']
check("« Partie A » et « Partie B » reconnues comme parties", secs[:2] == ['Partie A — Les feux', 'Partie B — Les témoins'], secs)
ctx = {q['num']: q.get('context') for q in qs}
check("Q1 et Q2 : énoncé de la partie A", all(ctx[k] and 'On note r' in ctx[k]['text'] and ctx[k]['title'].startswith('Partie A') for k in '12'), ctx['1'])
check("Q3 : énoncé complet de la partie B (4 lignes)", ctx['3'] and ctx['3']['text'].count('\n') == 2 and 'Il était vert' in ctx['3']['text'], ctx['3'])
check("Q4 : nouvelle partie C sans texte, l'énoncé de B ne la suit pas", ctx['4'] and ctx['4']['title'].startswith('Partie C') and not ctx['4']['text'], ctx['4'])
check("« Partie de … » n'est pas une partie : c'est du texte d'énoncé", not any(x['title'].startswith('Partie de') for x in parse_py("Partie de cartes\n\n1. Q ? [QCM]\nA. a\nB. b\n") if x['type'] == 'section'))

# 3. lecteur du navigateur = lecteur du serveur
def js_blocks(contents):
    src = open(FRONT, encoding='utf-8').read()
    m = re.search(r'^function parseExamBlocks\(raw: string\): ParsedBlock\[\] \{.*?^\}\n', src, re.S | re.M)
    if not m: return None
    with tempfile.TemporaryDirectory() as d:
        open(os.path.join(d, 'p.ts'), 'w', encoding='utf-8').write(m.group(0))
        open(os.path.join(d, 'in.json'), 'w', encoding='utf-8').write(json.dumps(contents))
        runner = ("const ts=require(process.argv[1]+'/node_modules/typescript'),fs=require('fs');"
                  "const js=ts.transpile(fs.readFileSync(process.argv[2]+'/p.ts','utf8'),{target:'es2020'})+'\\nmodule.exports={parseExamBlocks};';"
                  "fs.writeFileSync(process.argv[2]+'/p.js',js);const {parseExamBlocks}=require(process.argv[2]+'/p.js');"
                  "const ins=JSON.parse(fs.readFileSync(process.argv[2]+'/in.json','utf8'));console.log(JSON.stringify(ins.map(c=>parseExamBlocks(c))));")
        r = subprocess.run(['node', '-e', runner, os.path.join(os.path.dirname(ROOT), 'cei-next'), d], capture_output=True, text=True, timeout=120)
        return json.loads(r.stdout) if r.returncode == 0 else None
js = js_blocks([n, BON, "Examen de logique propositionnelle (durée 1 h)\n\n1. Une question ? [QCM]\nA. a\nB. b\n"])
check("lecteur navigateur exécutable", js is not None)
if js:
    jq = [x for x in js[0] if x['type'] not in ('section', 'description', 'text')]
    check("navigateur : mêmes 4 questions", [q['num'] for q in jq] == ['1', '2', '3', '4'])
    check("navigateur : mêmes contextes que le serveur", [q.get('context') for q in jq] == [q.get('context') for q in qs], [q.get('context') for q in jq])
    check("navigateur : mêmes parties", [x['title'] for x in js[0] if x['type'] == 'section'] == secs[1:3])   # le navigateur garde le préambule en bloc « text »
    pj = [x for x in parse_py("Examen de logique propositionnelle (durée 1 h)\n\n1. Une question ? [QCM]\nA. a\nB. b\n") if x['type'] == 'qcm']
    jj = [x for x in js[2] if x['type'] == 'qcm']
    check("énoncé placé juste avant Q1 (sans « Partie ») : serveur = navigateur", pj and jj and pj[0].get('context') == jj[0].get('context') and (pj[0].get('context') or {}).get('text', '').startswith('Examen de logique'), (pj[0].get('context') if pj else None))

ok = sum(RES); print(f"\n{ok}/{len(RES)} vérifications réussies")
sys.exit(0 if ok == len(RES) else 1)
