#!/usr/bin/env python
"""Garde-fou des lecteurs de sujet — À LANCER AVANT TOUT DÉPLOIEMENT touchant exams.py ou app/exam/[id]/page.tsx.

    cd cei-api-v2 && .venv-base/bin/python scripts/test_parseur_sujets.py

Contexte : le 19/10/2026 (examen 127, 113 étudiants), un sujet issu d'un PDF (« Question 1 [QCM] » collé, propositions
coupées sur plusieurs lignes) n'était pas lu : les étudiants n'avaient aucune question. Ce test vérifie, sur des sujets
réels ou typiques, que
  1. le lecteur du serveur (pagination) reconnaît toutes les questions et toutes les propositions ;
  2. le lecteur de la notation (barème) voit les mêmes questions ;
  3. le lecteur du NAVIGATEUR (parseExamBlocks, app/exam/[id]/page.tsx) donne le même résultat que celui du serveur
     (une divergence silencieuse entre les deux est exactement ce qui a caché le défaut) ;
  4. le contrôle de lisibilité (services/subject_check) signale les sujets défectueux et laisse passer les bons.
Aucune écriture en base.
"""
import json, os, subprocess, sys, tempfile, re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import app  # noqa: F401  (initialise les modèles)
from routes.exams import _parse_subject_blocks_ordered, _parse_subject_questions_for_grading
from services.subject_check import analyze

FRONT = os.path.join(os.path.dirname(ROOT), 'cei-next', 'app', 'exam', '[id]', 'page.tsx')
RESULTS = []


def check(nom, ok, detail=''):
    RESULTS.append((nom, bool(ok)))
    print(('  ✔ ' if ok else '  ✘ ') + nom + (f'  — {detail}' if detail and not ok else ''))


def questions(blocks):
    return [b for b in blocks if b.get('type') not in ('section', 'text', 'description')]


# ── Parseur navigateur exécuté via node (TypeScript transpilé à la volée) ─────────────────────────────────────────
def js_blocks(contents):
    src = open(FRONT, encoding='utf-8').read()
    m = re.search(r'^function parseExamBlocks\(raw: string\): ParsedBlock\[\] \{.*?^\}\n', src, re.S | re.M)
    if not m:
        return None
    with tempfile.TemporaryDirectory() as d:
        open(os.path.join(d, 'p.ts'), 'w', encoding='utf-8').write(m.group(0))
        open(os.path.join(d, 'in.json'), 'w', encoding='utf-8').write(json.dumps(contents))
        runner = (
            "const ts=require(process.argv[1]+'/node_modules/typescript'),fs=require('fs');"
            "const js=ts.transpile(fs.readFileSync(process.argv[2]+'/p.ts','utf8'),{target:'es2020'})+'\\nmodule.exports={parseExamBlocks};';"
            "fs.writeFileSync(process.argv[2]+'/p.js',js);const {parseExamBlocks}=require(process.argv[2]+'/p.js');"
            "const ins=JSON.parse(fs.readFileSync(process.argv[2]+'/in.json','utf8'));"
            "console.log(JSON.stringify(ins.map(c=>parseExamBlocks(c))));")
        r = subprocess.run(['node', '-e', runner, os.path.join(os.path.dirname(ROOT), 'cei-next'), d], capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            print(r.stderr[:400])
            return None
        return json.loads(r.stdout)


def signature(blocks):
    """(type, numéro, nb de propositions) de chaque question — comparable entre Python et JS."""
    out = []
    for b in questions(blocks):
        ch = b.get('choices') or []
        out.append((b.get('type'), str(b.get('num')), len(ch), len(b.get('items') or []), len(b.get('options') or [])))
    return out


# ── Corpus ────────────────────────────────────────────────────────────────────────────────────────────────────────
with open(os.path.join(ROOT, 'scripts', 'fixtures', 'sujet_pdf_question_marqueur_colle.txt'), encoding='utf-8') as f:
    SUJET_127 = f.read()

CLASSIQUE = """Partie 1 — QCM
Question 1. Quelle est la capitale du Sénégal ? [QCM]
A. Dakar
B. Thiès
C. Saint-Louis
D. Kaolack
Question 2. Citer deux sources du droit. (2 pts)
Partie 2 — Questions ouvertes
Question 3. Expliquer la notion de contrat.
"""
VF = """Question 1 - Le contrat est un accord de volontés. [VF]
Question 2 - La loi est facultative. [VF]
"""
COUPE = """Question 1 [QCM]
Énoncé sur
deux lignes.
A. Première proposition qui
continue sur la ligne suivante
B. Deuxième
C. Troisième proposition très longue
qui se termine ici
D. Quatrième
Question 2 [QCM]
Autre énoncé ?
A. a1
B. b1
C. c1
D. d1
"""
with open(os.path.join(ROOT, 'scripts', 'fixtures', 'sujet_types_moodle.txt'), encoding='utf-8') as f:
    SUJET_TYPES = f.read()
CORPUS = {
    'types Moodle (association, tri, ordre, trous, réponse courte, numérique, description)': (SUJET_TYPES, 7, None),
    'sujet réel examen 127 (PDF, marqueur collé, propositions coupées)': (SUJET_127, 20, 4),
    'format classique (Question 1. … [QCM], parties)': (CLASSIQUE, 3, None),
    'vrai/faux': (VF, 2, None),
    'propositions coupées sur plusieurs lignes': (COUPE, 2, 4),
}

contenus = [c for c, _, _ in CORPUS.values()]
js = js_blocks(contenus)
check("lecteur navigateur exécutable (node + typescript)", js is not None, "node ou typescript introuvable / code frontal introuvable")

for i, (nom, (contenu, n_attendu, n_choix)) in enumerate(CORPUS.items()):
    print(f'\n== {nom} ==')
    srv = _parse_subject_blocks_ordered(contenu)
    q = questions(srv)
    check(f"serveur : {n_attendu} question(s) reconnue(s)", len(q) == n_attendu, f"{len(q)} reconnue(s)")
    if n_choix:
        check(f"serveur : chaque QCM a {n_choix} propositions", all(len(b.get('choices') or []) == n_choix for b in q),
              [len(b.get('choices') or []) for b in q])
    g = _parse_subject_questions_for_grading(contenu)
    check("notation : mêmes questions que l'affichage", len(g) == len(q), f"notation={len(g)} affichage={len(q)}")
    if js is not None:
        s_srv, s_js = signature(srv), signature(js[i])
        check("navigateur = serveur (types, numéros, nombre de propositions)", s_srv == s_js, f"\n     serveur={s_srv[:4]}…\n     navigateur={s_js[:4]}…")
    rep = analyze(contenu)
    check("contrôle de lisibilité : sujet accepté", rep['ok'], rep['issues'])

print('\n== sujets défectueux : le contrôle doit protester ==')
VIDE = "Sujet sans aucune question numérotée.\nRépondez librement."
CASSE = "Question 1 [QCM]\nÉnoncé\nA. seule proposition\nQuestion 2 [QCM]\nÉnoncé\nA. a\nB. b\nQuestion 3 [QCM]\nÉnoncé\nA. a\nB. b\nC. c\nQuestion 4 [QCM]\nÉnoncé\nA. a\nB. b\nC. c\nD. d\n"
r = analyze(VIDE)
check("aucune question → erreur bloquante", (not r['ok']) and any('Aucune question' in i['message'] for i in r['issues']), r)
r = analyze(CASSE)
check("QCM à une seule proposition → erreur bloquante", not r['ok'] and any('Question 1' in i['message'] for i in r['issues']), r)
r = analyze("")
check("sujet vide → erreur bloquante", not r['ok'], r)

ok = sum(1 for _, v in RESULTS if v)
print(f'\n{ok}/{len(RESULTS)} vérifications réussies')
sys.exit(0 if ok == len(RESULTS) else 1)
