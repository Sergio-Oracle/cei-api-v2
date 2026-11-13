#!/usr/bin/env python
"""Non-régression du lecteur de sujets sur les VRAIS sujets (corpus privé, jamais versionné).

    cd cei-api-v2 && .venv-base/bin/python scripts/test_corpus_reel.py            # compare à la référence
    cd cei-api-v2 && .venv-base/bin/python scripts/test_corpus_reel.py --update   # accepte les écarts (après les avoir relus !)

Pourquoi : à chaque incident (énoncé manquant, titre collé, en-tête répété…) on corrigeait le lecteur pour UN sujet sans savoir ce que le
changement faisait aux autres. Ici, tout changement du lecteur est mesuré sur l'ensemble des sujets réels :
  1. serveur et navigateur lisent exactement la même chose (même questions, mêmes énoncés) — une divergence est un défaut ;
  2. la normalisation à l'enregistrement est idempotente et ne perd jamais une question ;
  3. la structure (questions, types, propositions, groupes d'énoncés) est identique à la référence enregistrée : tout écart est listé,
     à relire par une personne avant d'être accepté (--update).
Corpus : scripts/fixtures/corpus_sujets.json (copie privée /root/cei-fixtures-prives). Absent => test ignoré, comme les autres essais sur sujets réels.
"""
import hashlib, json, os, re, subprocess, sys, tempfile
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
CORPUS = os.path.join(ROOT, 'scripts', 'fixtures', 'corpus_sujets.json')
GOLDEN = os.path.join(ROOT, 'scripts', 'fixtures', 'corpus_reference.json')
if not os.path.exists(CORPUS):
    print("Essais sur le corpus réel ignorés : scripts/fixtures/corpus_sujets.json absent (copie privée : /root/cei-fixtures-prives)."); sys.exit(0)
import app  # noqa: F401
from routes.exams import _parse_subject_blocks_ordered as parse_py
from services.subject_normalizer import normalize
from services.subject_structure import groups

FRONT = os.path.join(os.path.dirname(ROOT), 'cei-next', 'app', 'exam', '[id]', 'page.tsx')
update = '--update' in sys.argv
RES = []
def check(nom, ok, detail=''):
    RES.append(bool(ok)); print(('  ✔ ' if ok else '  ✘ ') + nom + (f'  — {detail}' if detail and not ok else ''))

def questions(blocks): return [b for b in blocks if b.get('type') not in ('section', 'description', 'text')]
def ckey(b):
    c = b.get('context') or {}
    return hashlib.sha1(f"{c.get('title','')}|{(c.get('text') or '').strip()}".encode()).hexdigest()[:8] if (c.get('title') or c.get('text')) else ''
def signature(blocks):
    return [[str(q.get('num')), q.get('type'), len(q.get('choices') or []), ckey(q)] for q in questions(blocks)]

def js_blocks(contents):
    src = open(FRONT, encoding='utf-8').read()
    m = re.search(r'^const DOC_HEADER_CUES.*?^function parseExamBlocks\(raw: string\): ParsedBlock\[\] \{.*?^\}\n', src, re.S | re.M)
    if not m: return None
    with tempfile.TemporaryDirectory() as d:
        open(os.path.join(d, 'p.ts'), 'w', encoding='utf-8').write(m.group(0)); open(os.path.join(d, 'in.json'), 'w', encoding='utf-8').write(json.dumps(contents))
        runner = ("const ts=require(process.argv[1]+'/node_modules/typescript'),fs=require('fs');"
                  "const js=ts.transpile(fs.readFileSync(process.argv[2]+'/p.ts','utf8'),{target:'es2020'})+'\\nmodule.exports={parseExamBlocks};';"
                  "fs.writeFileSync(process.argv[2]+'/p.js',js);const {parseExamBlocks}=require(process.argv[2]+'/p.js');"
                  "const ins=JSON.parse(fs.readFileSync(process.argv[2]+'/in.json','utf8'));console.log(JSON.stringify(ins.map(c=>parseExamBlocks(c))));")
        r = subprocess.run(['node', '-e', runner, os.path.join(os.path.dirname(ROOT), 'cei-next'), d], capture_output=True, text=True, timeout=300)
        return json.loads(r.stdout) if r.returncode == 0 else None

subjects = json.load(open(CORPUS, encoding='utf-8'))
print(f"{len(subjects)} sujets réels dans le corpus")
srv = {s['id']: parse_py(s['content']) for s in subjects}

# 1. serveur = navigateur sur CHAQUE sujet
js = js_blocks([s['content'] for s in subjects])
check("lecteur du navigateur exécutable", js is not None)
if js:
    diverge = [s['id'] for s, jb in zip(subjects, js) if signature(srv[s['id']]) != signature(jb)]
    check(f"serveur et navigateur lisent la même chose sur les {len(subjects)} sujets (questions, types, propositions, énoncés)", not diverge, f"divergence sur les sujets {diverge[:12]}")

# 2. normalisation : idempotente, aucune question perdue ni ajoutée
nonidem, lost, gained = [], [], []
for s in subjects:
    n1, _ = normalize(s['content']); n2, _ = normalize(n1)
    if n1 != n2: nonidem.append(s['id'])
    a, b = [str(q.get('num')) for q in questions(srv[s['id']])], [str(q.get('num')) for q in questions(parse_py(n1))]
    if len(b) < len(a): lost.append((s['id'], len(a), len(b)))
    elif len(b) > len(a): gained.append((s['id'], len(a), len(b)))
check("normalisation idempotente sur tous les sujets", not nonidem, nonidem[:10])
check("la normalisation ne FAIT JAMAIS DISPARAÎTRE une question", not lost, lost[:10])
from services.subject_normalizer import _renumber
rewritten = []
for s in subjects:
    L = s['content'].split('\n'); out, k = _renumber(L)
    if k and any(re.match(r'^\s*\d{1,3}\s*[.:)–—-]+\s*[\d.,)]', a) for a, b in zip(L, out) if a != b):
        rewritten.append(s['id'])
check("la renumérotation ne réécrit jamais une ligne de référence/norme/page (« 001-2023-… », « 802.11ax », « 308). »)", not rewritten, rewritten[:10])
from helpers import strip_bareme_from_content as _strip_student
_LEAK = re.compile(r"R[ée]ponses?\s+(?:correctes?|attendues?|exactes?)\s*:|Bonnes?\s+r[ée]ponses?\s*:\s*\(?[A-F]\b|^\s*Corrig[ée]s?\s*(?:exercice|:|de\\b|type)|^\s*R[ée]ponse\s*:\s*\S.{25,}", re.I | re.M)
leaky = [s['id'] for s in subjects if _LEAK.search(_strip_student(s['content']))]
check("AUCUN sujet réel ne laisse voir une marque de corrigé à l'étudiant (texte servi après filtrage)", not leaky, leaky[:10])
check("la renumérotation ne touche aucun document de cours (listes numérotées sans questions)", all(_renumber(s['content'].split('\n'))[1] == 0 for s in subjects if s['id'] in (30, 32, 33, 40)))
if gained:
    print(f"  (information : à l'enregistrement la normalisation ferait lire PLUS de questions sur {len(gained)} ancien(s) sujet(s) {[g[0] for g in gained]} — "
          "constaté le 13/11/2026 : lignes de bibliographie ou de références numérotées promues en questions par la renumérotation ; aucun de ces sujets n'a d'examen)")

# 3. structure identique à la référence
def _norm_sig(content):
    n, _ = normalize(content)
    return signature(parse_py(n)), hashlib.sha1(n.encode()).hexdigest()[:10]       # structure ET empreinte du texte normalisé (tout texte réécrit se voit)
cur = {str(s['id']): {'sig': signature(srv[s['id']]), 'groups': [(g['title'], g['statement_chars'], g['questions'][0], g['questions'][-1]) for g in groups(questions(srv[s['id']]))],
                      'norm': _norm_sig(s['content'])} for s in subjects}
if update or not os.path.exists(GOLDEN):
    json.dump(cur, open(GOLDEN, 'w'), ensure_ascii=False); os.chmod(GOLDEN, 0o600)
    print(f"  (référence {'mise à jour' if update else 'créée'} : {len(cur)} sujets — à relire avant de la garder)")
else:
    ref = json.load(open(GOLDEN, encoding='utf-8'))
    cur = json.loads(json.dumps(cur, ensure_ascii=False))          # mêmes types que la référence relue (listes, pas tuples)
    changed = [k for k in cur if k in ref and cur[k] != ref[k]]
    new = [k for k in cur if k not in ref]
    check(f"structure identique à la référence sur les {len(cur) - len(new)} sujets connus", not changed, f"{len(changed)} sujet(s) changent : {changed[:15]}")
    if changed:
        for k in changed[:8]:
            a, b = ref[k]['groups'], cur[k]['groups']
            print(f"      sujet {k} : groupes {len(a)} -> {len(b)} | questions {len(ref[k]['sig'])} -> {len(cur[k]['sig'])}")
    if new: print(f"  (nouveaux sujets absents de la référence : {len(new)} — lancer --update après relecture)")

ok = sum(RES); print(f"\n{ok}/{len(RES)} vérifications réussies")
sys.exit(0 if ok == len(RES) else 1)
