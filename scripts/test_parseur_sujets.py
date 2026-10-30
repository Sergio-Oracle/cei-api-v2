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
# Épreuves réelles du 27/10 : extraction PDF avec tableaux relus case par case (utils._table_lines : lignes coupées,
# association à deux colonnes) et Word aux affirmations vrai/faux non numérotées — passées par la normalisation.
from services.exam_normalizer import normalize_exam_text
with open(os.path.join(ROOT, 'scripts', 'fixtures', 'epreuve_droit_pdf_tableaux.txt'), encoding='utf-8') as f:
    EPREUVE_PDF, _rep_pdf = normalize_exam_text(f.read())
with open(os.path.join(ROOT, 'scripts', 'fixtures', 'epreuve_droit_docx_vf_sans_numero.txt'), encoding='utf-8') as f:
    EPREUVE_DOCX, _rep_docx = normalize_exam_text(f.read())
CORPUS = {
    'épreuve PDF à tableaux (20 QCM, 16 V/F, association, 4 plateaux de tri, cas pratique)': (EPREUVE_PDF, 47, None),
    'épreuve Word (28 QCM, 28 V/F sans numéro, QCM sur cas pratique)': (EPREUVE_DOCX, 68, None),
    'types Moodle (association, tri, ordre, trous, réponse courte, numérique, description)': (SUJET_TYPES, 10, None),
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

print('\n== normalisation des épreuves réelles ==')
for nom, txt, rep_n in (('PDF', EPREUVE_PDF, _rep_pdf), ('Word', EPREUVE_DOCX, _rep_docx)):
    total = sum(float(p.replace(',', '.')) for p in re.findall(r'^Question \d+ — .*\((\d+(?:,\d+)?) pts\)', txt, re.M))
    check(f"{nom} : barème total 20 points", abs(total - 20) < 1e-6, f"{total}")
    check(f"{nom} : aucun avertissement", not rep_n['warnings'], rep_n['warnings'])
check("PDF : situation du plateau 1 entière",
      "- [phrase de sujet retirée]" in EPREUVE_PDF)
check("PDF : affirmation coupée par le tableau recollée",
      "[phrase de sujet retirée] (0,25 pts) [VF]" in EPREUVE_PDF)
check("Word : balises de l'enseignant non répétées", '[VF] (' not in EPREUVE_DOCX and '[QCM] (' not in EPREUVE_DOCX)

print('\n== barème écrit depuis le corrigé (sans IA) ==')
from services.exam_normalizer import rubric_from_corrige
def _fx(n):
    with open(os.path.join(ROOT, 'scripts', 'fixtures', n), encoding='utf-8') as f:
        return f.read()
for nom, ep, co in (('PDF à tableaux', 'epreuve_droit_pdf_tableaux.txt', 'corrige_droit_pdf_tableaux.txt'),
                    ('Word à tableaux (clé rapide en tête, 2 parties QCM)', 'epreuve_droit_docx_tableaux.txt', 'corrige_droit_docx_tableaux.txt')):
    txt, rep_n = normalize_exam_text(_fx(ep))
    rub, warns = rubric_from_corrige(_fx(co), rep_n)
    total = sum(float(p.replace(',', '.')) for p in re.findall(r'^Question \d+ \((\d+(?:,\d+)?) pts\)', rub, re.M))
    check(f"{nom} : barème 20/20 sans avertissement", abs(total - 20) < 1e-6 and not warns, f"{total} pts, {warns[:3]}")

print('\n== sujet de PDF à exercices (examen 142, 30/10/2026) : rien ne doit disparaître ==')
from services.subject_normalizer import normalize as _normalize_subject
_brut = _fx('epreuve_exercices_tableau_vf.txt')
_rep = analyze(_brut)
check("avant remise au format : le contrôle signale l'exercice Vrai/Faux vide",
      (not _rep['ok']) and any('Vrai ou faux' in i['message'] for i in _rep['issues']), _rep['issues'])
_net, _notes = _normalize_subject(_brut)
_q = questions(_parse_subject_blocks_ordered(_net))
check("après remise au format : les 25 questions annoncées sont lues", len(_q) == 25, f"{len(_q)}")
check("5 affirmations Vrai/Faux lues", sum(1 for b in _q if b['type'] == 'vf') == 5)
check("numéros uniques (les réponses sont rangées par numéro)", len({b['num'] for b in _q}) == len(_q))
check("sujet accepté par le contrôle", analyze(_net)['ok'], analyze(_net)['issues'])
check("remise au format idempotente", _normalize_subject(_net) == (_net, []))
_ok_deja = CORPUS[next(iter(CORPUS))][0]
check("un sujet déjà au bon format n'est pas touché", _normalize_subject(_ok_deja)[0] == _ok_deja)

print('\n== énoncés Word sans numéros « A. B. C. D. / Bonne réponse : d » (30/10/2026) ==')
from services.subject_normalizer import loose_mcq, rubric_from_key
from services.subject_service import annotate_markers
from helpers import strip_bareme_from_content
_w = "--- Fichier: Enonce 1.docx ---\nEnonce 1\nA. je suis\nB. tu es\nC. il est\nD. il a\n\nBonne réponse : d\n\nEnonce 2\nA. a\nB. b\nBonne réponse : B\n"
_wn, _wk, _wc = loose_mcq(_w)
_wq = questions(_parse_subject_blocks_ordered(annotate_markers(_wn, True, False)))
check("2 QCM reconnus (et non une zone de réponse libre)", len(_wq) == 2 and all(b['type'] == 'qcm' for b in _wq), [(b['type'], b['num']) for b in _wq])
check("4 propositions puis 2 propositions lues", [len(b['choices']) for b in _wq] == [4, 2])
check("clés de réponse extraites", _wk == {1: 'D', 2: 'B'}, _wk)
check("la ligne « Bonne réponse » n'est plus dans l'énoncé", 'Bonne réponse' not in _wn)
check("barème écrit depuis les clés", 'Bonne réponse : D) il a' in rubric_from_key(_wn, _wk, 20))
check("jamais montrée à l'étudiant même si elle reste dans un sujet", 'Bonne réponse' not in strip_bareme_from_content(_w))
_ex = "Exercice 1\nEnoncé du problème.\na) Définir X\nb) Calculer Y\n"
check("sous-questions a) b) d'un exercice ouvert non prises pour un QCM", loose_mcq(_ex)[0] == _ex)

print('\n== détection automatique des types, sans cocher aucune case (30/10/2026) ==')
from services.type_detector import detect_and_annotate
_dt, _dr = detect_and_annotate(_fx('epreuve_exercices_tableau_vf.txt'))
_dq = questions(_parse_subject_blocks_ordered(_dt))
_types = [b['type'] for b in _dq]
check("5 QCM, 5 Vrai/Faux, 1 association, 5 textes à trous, 5 réponses courtes",
      (_types.count('qcm'), _types.count('vf'), _types.count('association'), _types.count('cloze'), _types.count('court')) == (5, 5, 1, 5, 5), _dr['counts'])
_a = next(b for b in _dq if b['type'] == 'association')
check("association : 5 notions et 6 définitions lues", len(_a.get('items') or []) == 5 and len(_a.get('options') or []) == 6)
check("textes à trous : un champ chacun, blancs remplacés", all(len(b.get('items') or []) == 1 for b in _dq if b['type'] == 'cloze') and '……' not in _dt)
check("récapitulatif de notation et indices retirés du sujet", 'Récapitulatif' not in _dt and 'Page d\'indices' not in _dt)
check("numéros uniques", len({b['num'] for b in _dq}) == len(_dq))
check("détection idempotente", detect_and_annotate(_dt)[0] == _dt)
for _nom, (_c, _n, _ch) in CORPUS.items():
    _o, _ = detect_and_annotate(_c)
    _av = [(b['type'], b.get('num'), len(b.get('choices') or [])) for b in _parse_subject_blocks_ordered(_c) if b['type'] not in ('section', 'description')]
    _ap = [(b['type'], b.get('num'), len(b.get('choices') or [])) for b in _parse_subject_blocks_ordered(_o) if b['type'] not in ('section', 'description')]
    check(f"sujet déjà correct inchangé : {_nom[:40]}", _av == _ap)
_wd, _ = detect_and_annotate(loose_mcq(_w)[0])
check("QCM Word sans numéros : 2 QCM", [b['type'] for b in questions(_parse_subject_blocks_ordered(_wd))] == ['qcm', 'qcm'])
_multi = "Exercice 1. Plusieurs réponses possibles\nCochez toutes les bonnes réponses.\n1. Quels sont des fleuves ?\nA. Sénégal\nB. Gambie\nC. Everest\n"
check("consigne « toutes les bonnes réponses » → QCM à réponses multiples",
      [b['type'] for b in questions(_parse_subject_blocks_ordered(detect_and_annotate(_multi)[0]))] == ['qcm_multi'])
_etc = "1. Citez les sources du droit : loi, coutume, etc... et expliquez-les.\n"
check("« etc... » dans une phrase n'est pas un trou", [b['type'] for b in questions(_parse_subject_blocks_ordered(detect_and_annotate(_etc)[0]))] == ['open'])

print('\n== mots croisés : la grille du PDF est reconnue et reproduite ==')
from services.crossword_pdf import extract_crosswords
_grids = extract_crosswords(os.path.join(ROOT, 'scripts', 'fixtures', 'epreuve_exercices_mots_croises.pdf'))
check("grille lue dans le PDF : 5 mots, longueurs 12/8/10/10/7",
      len(_grids) == 1 and sorted((w['dir'], w['num'], w['len']) for w in _grids[0]['words']) == [('H', 3, 12), ('H', 4, 8), ('H', 5, 10), ('V', 1, 10), ('V', 2, 7)],
      _grids)
_gt, _gr = detect_and_annotate(_fx('epreuve_exercices_tableau_vf.txt'), _grids)
_gq = questions(_parse_subject_blocks_ordered(_gt))
_cw = [b for b in _gq if b.get('grid')]
check("une seule question à grille, 5 champs", len(_cw) == 1 and len(_cw[0]['items']) == 5 and len(_cw[0]['grid']) == 5, [(b['type'], b['num']) for b in _gq][-3:])
check("définitions conservées avec leur sens", any('Horizontalement 3' in l for l in _cw[0]['extraLines']) and any('Verticalement 2' in l for l in _cw[0]['extraLines']))
check("sans la grille : repli propre sur des réponses courtes", [b['type'] for b in questions(_parse_subject_blocks_ordered(detect_and_annotate(_fx('epreuve_exercices_tableau_vf.txt'))[0]))].count('court') == 5)
_bad = [dict(_grids[0], words=[dict(w, len=w['len'] + 1) if w['num'] == 2 else w for w in _grids[0]['words']])]
_bt, _br = detect_and_annotate(_fx('epreuve_exercices_tableau_vf.txt'), _bad)
check("grille incohérente avec les définitions : jamais de grille fausse", not any(b.get('grid') for b in questions(_parse_subject_blocks_ordered(_bt))) and _br['warnings'])
_jsb = js_blocks([_gt])
if _jsb is not None:
    _jg = [b for b in _jsb[0] if b.get('grid')]
    check("navigateur = serveur : même grille", len(_jg) == 1 and len(_jg[0]['grid']) == 5 and len(_jg[0].get('items') or []) == 5)

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
