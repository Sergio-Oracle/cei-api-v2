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

print('\n== autres types lus depuis Word/PDF : ordre, trous avec liste de mots, classement ==')
def _types_of(txt):
    return [b['type'] for b in questions(_parse_subject_blocks_ordered(detect_and_annotate(txt)[0]))]
check("« remettez dans l'ordre chronologique » + A. B. C. D. → remise en ordre",
      _types_of("1. Remettez dans l'ordre chronologique les étapes :\nA. Le vote\nB. La promulgation\nC. Le dépôt\nD. La publication\n") == ['ordre'])
check("trous + « Mots : a, b, c » → texte à trous avec 2 trous et 3 mots",
      (lambda b: b['type'] == 'trous' and len(b['items']) == 2 and len(b['options']) == 3)(
          questions(_parse_subject_blocks_ordered(detect_and_annotate("1. Le droit ……… est un ensemble de règles ……… par l'État.\nMots : public, positif, édictées\n")[0]))[0]))
check("deux trous + propositions lettrées → texte à trous (et non QCM)",
      _types_of("1. Le ……… est le siège du ……… exécutif.\nA. palais\nB. pouvoir\nC. code\n") == ['trous'])
check("un seul trou + propositions → reste un QCM", _types_of("1. La capitale est ………\nA. Dakar\nB. Thiès\n") == ['qcm'])
check("catégories + éléments à puces → classement",
      _types_of("1. Classez ces éléments.\nCatégories : Droit public, Droit privé\n- Le droit pénal\n- Le droit civil\n") == ['tri'])

print('\n== QCM calculé (calculatedmulti de Moodle) : valeurs propres à chaque étudiant ==')
from routes.exams import _build_exam_pages
class _Sub:   # faux sujet / examen minimal pour la pagination
    def __init__(self, c): self.content = c
class _Ex:
    def __init__(self, c): self.subject = _Sub(c); self.randomize_questions = False; self.questions_per_page = 0
_cq = "1. Un capital de {a} F placé à {b} % pendant un an rapporte : [QCM]\nVariables : a = 1000..5000 pas 1000 | b = 2..8\nA. {={a}*{b}/100} F\nB. {={a}*{b}} F\nC. {={a}/{b}} F\nBonne réponse : A\n"
_p1 = _build_exam_pages(_Ex(_cq), 11)['p1_blocks'][0]
_p2 = _build_exam_pages(_Ex(_cq), 12)['p1_blocks'][0]
check("type QCM affiché normalement avec 3 propositions", _p1['type'] == 'qcm' and len(_p1['choices']) == 3)
check("aucune formule ni « Variables » visible par l'étudiant", '{' not in _p1['text'] and all('{' not in c['text'] for c in _p1['choices']) and not any('Variables' in l for l in (_p1.get('extraLines') or [])))
check("valeurs différentes selon l'étudiant", _p1['text'] != _p2['text'] or _p1['choices'][0]['text'] != _p2['choices'][0]['text'], (_p1['text'], _p2['text']))
import re as _re
_a = int(_re.search(r'de ([\d\u202f]+) F', _p1['text']).group(1).replace('\u202f', '')); _b = int(_re.search(r'à (\d+) %', _p1['text']).group(1))
check("proposition A = a × b / 100 pour CET étudiant", _p1['choices'][0]['text'].replace('\u202f', '').startswith(str(_a * _b // 100) if (_a * _b) % 100 == 0 else str(round(_a * _b / 100, 6)).replace('.', ',')), (_a, _b, _p1['choices'][0]['text']))

print('\n== appariement aléatoire et fichier joint (Moodle : randomsamatch, essay avec pièces jointes) ==')
from routes.exams import _deterministic_grade, _attachment_list
_ap = "1. Associez chaque pays à sa capitale. [APPARIEMENT]\nTirage : 3\nA. Sénégal → Dakar\nB. Mali → Bamako\nC. Niger → Niamey\nD. Guinée → Conakry\nE. Ghana → Accra\nF. Togo → Lomé\n"
_sets = {tuple(p['left'] for p in _build_exam_pages(_Ex(_ap), sd)['p1_blocks'][0]['pairs']) for sd in range(1, 12)}
check("3 paires tirées parmi 6, différentes selon l'étudiant", all(len(x) == 3 for x in _sets) and len(_sets) > 1, _sets)
_b = _build_exam_pages(_Ex(_ap), 5)['p1_blocks'][0]
check("même tirage à l'affichage et à la correction (copie juste = 6/6)",
      _deterministic_grade(_ap, "Question 1 (6 pts) :\n  • Réponse : par paires\n", {f'pq_1_{i}': p['right'] for i, p in enumerate(_b['pairs'])}, seed=5)[0] == 6.0)
check("ligne « Tirage » jamais montrée à l'étudiant", not any('Tirage' in l for l in (_b.get('extraLines') or [])) and 'draw' in _b)
_up = "3. Rédigez votre rapport puis joignez-le. [OUVERT]\nFichier : obligatoire\nConsigne : 2 pages.\n"
_ub = _build_exam_pages(_Ex(_up), 1)['p2_items'][0]
check("question avec fichier joint reconnue (obligatoire), ligne de réglage masquée", _ub.get('upload') == 'required' and not any('Fichier' in l for l in (_ub.get('extraLines') or [])))
_ud, _ur = detect_and_annotate("1. Rédigez une note de synthèse puis joignez votre rapport au format PDF.\n")
check("« joignez votre rapport » → réponse rédigée avec dépôt de fichier", questions(_parse_subject_blocks_ordered(_ud))[0].get('upload') == 'allowed', _ud)
check("liste des fichiers joints lue dans la copie", _attachment_list({'pq_3_files': '[{"key":"k1","name":"a.pdf","size":10}]'})[0]['name'] == 'a.pdf')

print('\n== épreuve et corrigé Word du Pr Corréa (exercices, tableaux, grille de mots croisés) ==')
from services.crossword_pdf import extract_crosswords as _xw
from services.corrige_reader import read_corrige_file, rubric_from_exercise_key
from services.subject_count import count_from_files
from utils import extract_text_from_file as _etf
from werkzeug.datastructures import FileStorage as _FS
_FX = os.path.join(ROOT, 'scripts', 'fixtures')
_ep, _co = os.path.join(_FX, 'epreuve_methodologie_exercices.docx'), os.path.join(_FX, 'corrige_methodologie_exercices.docx')
_g = _xw(_ep)
check("grille lue dans le tableau Word (10 × 12, 5 mots)", len(_g) == 1 and (_g[0]['rows'], _g[0]['cols'], len(_g[0]['words'])) == (10, 12, 5), _g)
_wt, _wr = detect_and_annotate(_etf(_ep), _g)
_wq = questions(_parse_subject_blocks_ordered(_wt))
check("Word : 5 QCM, 5 Vrai/Faux, 1 association, 5 trous, 1 grille", [b['type'] for b in _wq].count('qcm') == 5 and [b['type'] for b in _wq].count('vf') == 5
      and [b['type'] for b in _wq].count('association') == 1 and [b['type'] for b in _wq].count('cloze') == 6 and sum(1 for b in _wq if b.get('grid')) == 1, _wr['counts'])
_key = read_corrige_file(_co, _etf(_co))
check("corrigé Word : 5 QCM, 5 Vrai/Faux, 5 associations, 5 trous (gras), 5 mots croisés",
      (len(_key['qcm']), len(_key['vf']), len(_key['assoc']), len(_key['trous']), len(_key['cw'])) == (5, 5, 5, 5, 5), {k: v for k, v in _key.items() if k != 'warnings'})
_rub, _rw = rubric_from_exercise_key(_wt, _key)
check("barème écrit depuis le corrigé : complet, 20 points, sans avertissement", bool(_rub) and not _rw and 'TOTAL : 20 / 20' in _rub, _rw)
_ans = {}
for _i, _l in enumerate(_key['qcm'], 1): _ans[f'pq_{_i}'] = _l
for _i, _v in enumerate(_key['vf'], 6): _ans[f'pq_{_i}'] = _v
for _i in range(1, 6): _ans[f'pq_11_{_i - 1}'] = _key['assoc'][_i]
for _i, _a in enumerate(_key['trous'], 12): _ans[f'pq_{_i}_0'] = _a[0].strip('()')
for _j, _o in enumerate([('H', 3), ('H', 4), ('H', 5), ('V', 1), ('V', 2)]): _ans[f'pq_17_{_j}'] = _key['cw'][_o]
check("copie parfaite = 20 / 20", _deterministic_grade(_wt, _rub, _ans, seed=1)[0] == 20.0)
_bad = dict(_ans, pq_1='A', pq_6='Vrai', pq_17_0='AUTRE')
check("trois erreurs = 17,6 / 20", abs(_deterministic_grade(_wt, _rub, _bad, seed=1)[0] - 17.6) < 1e-6)
check("copie vide = 0", _deterministic_grade(_wt, _rub, {}, seed=1)[0] == 0.0)
_cnt = count_from_files([_FS(open(_ep, 'rb'), filename='ep.docx'), _FS(open(_co, 'rb'), filename='Corrigé.docx')])
check("comptage : 25 questions, comme annoncé par le document", _cnt['questions'] == 25 and _cnt['declared'] == 25, _cnt)

try:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas as _cv
    import tempfile as _tf
    _tmp = os.path.join(_tf.mkdtemp(), 'corrige.pdf')
    _c = _cv.Canvas(_tmp, pagesize=A4)
    _y = [800]
    def _ln(parts):
        _x = 40
        for _t, _b in parts:
            _f = 'Helvetica-Bold' if _b else 'Helvetica'
            _c.setFont(_f, 11); _c.drawString(_x, _y[0], _t); _x += _c.stringWidth(_t, _f, 11)
        _y[0] -= 18
    _ln([("Exercice 4. Texte à trous (4 points)", True)])
    _ln([("1. Le latin signifie conduire ", False), ("dedans", True), (" L'introduction conduit.", False)])
    _ln([("2. Dans la dissertation, la ", False), ("problématique", True), (" est la question centrale.", False)])
    _c.save()
    check("corrigé PDF : réponses des trous lues dans le gras", read_corrige_file(_tmp, '')['trous'] == [['dedans'], ['problématique']])
except ImportError:
    pass

print('\n== assistant de création par discussion (chat) ==')
import services.ai_service as _AI
from services import subject_chat as _SC
_files = _SC.store_files(4242, [_FS(open(_ep, 'rb'), filename='Epreuve.docx'), _FS(open(_co, 'rb'), filename='Corrigé.docx')])
check("fichiers joints lus et gardés (épreuve + corrigé reconnu)", len(_files) == 2 and all(f.get('id') for f in _files) and _files[1]['corrige'] is True, _files)
_orig_call = _AI.call_ai
try:
    _AI.call_ai = lambda *a, **k: "Je reproduis ce sujet à l'identique.\n<<<REPRODUIRE 1>>>"
    _r = _SC.chat(4242, [{'role': 'user', 'content': 'Reprends ce sujet tel quel'}], [f['id'] for f in _files])
    check("« reproduire » : sujet exact (25 questions, grille) sans que l'IA ne réécrive", _r['draft'] and _r['draft']['report']['questions'] == 25 and 'Grille :' in _r['draft']['content'])
    check("« reproduire » : barème écrit depuis le corrigé (20 points)", _r['draft'] and 'TOTAL : 20 / 20' in _r['draft']['rubric'])
    _AI.call_ai = lambda *a, **k: ("Voici le brouillon.\n<<<SUJET>>>\nExamen test\nQuestion 1 — Offre (2 pts)\nQu'est-ce qu'une offre ?\nA) Un engagement ferme\nB) Une opinion\nC) Un fait\nD) Une coutume\n\n"
                                   "Question 2 — Capacité (1 pt) [VF]\nUn mineur peut contracter seul.\n\nQuestion 3 — Dol (1 pt) [OUVERT]\nDéfinissez le dol.\n"
                                   "<<<BAREME>>>\nQuestion 1 — Offre (2 pts)\n  • QCM : « Bonne réponse : A) Un engagement ferme »\nQuestion 2 — Capacité (1 pt)\n  • Réponse : Faux\nQuestion 3 — Dol (1 pt)\n  • Critère : 1 pt — définition\nTOTAL : 4 / 4 points\n<<<FIN>>>")
    _r2 = _SC.chat(4242, [{'role': 'user', 'content': 'Crée un petit examen'}], [])
    _d = _r2['draft']
    check("brouillon de l'IA : titre, 3 questions, types garantis (QCM même sans balise)", _d and _d['title'] == 'Examen test' and _d['report']['by_type'] == {'qcm': 1, 'vf': 1, 'open': 1}, _d and _d['report'])
    check("barème de l'IA nettoyé (« QCM : « … » » retiré) et lisible par le correcteur", _d and 'QCM :' not in _d['rubric'] and 'Bonne réponse : A)' in _d['rubric'])
    check("brouillon de l'IA corrigé par le correcteur automatique : copie juste = 3/3", _d and _deterministic_grade(_d['content'], _d['rubric'], {'pq_1': 'A', 'pq_2': 'Faux'}, seed=1)[0] == 3.0)
    _calls = []
    def _fake(system, user_msg, **k):
        _calls.append(user_msg)
        if len(_calls) == 1:
            return "Je dois reproduire le sujet existant.\n<<<REPRODUIRE 1>>>"       # l'IA se trompe d'intention
        return ("Voici de nouvelles questions.\n<<<SUJET>>>\nNouvel examen\n" + "\n".join(
            f"Question {i} — Notion {i} ({'1'} pt) [QCM]\nÉnoncé nouveau {i}\nA) a\nB) b\nC) c\nD) d\n" for i in range(1, 7))
            + "<<<BAREME>>>\n" + "\n".join(f"Question {i} — Notion {i} (1 pt)\n  • Bonne réponse : A) a" for i in range(1, 7)) + "\nTOTAL : 6 / 6 points\n<<<FIN>>>")
    _AI.call_ai = _fake
    _r3 = _SC.chat(4242, [{'role': 'user', 'content': 'Je veux que tu me crées 6 questions à partir de ces fichiers.'}], [f['id'] for f in _files])
    check("« créer 6 questions » : l'IA qui répond « reproduire » est recadrée → nouveau brouillon de 6 questions",
          len(_calls) == 2 and _r3['draft'] and _r3['draft']['report']['blocks'] == 6 and _r3['draft']['title'] == 'Nouvel examen', (len(_calls), _r3['draft'] and _r3['draft']['report']))
    _AI.call_ai = lambda *a, **k: "Combien de questions souhaitez-vous ?"
    check("question de l'IA : pas de brouillon", _SC.chat(4242, [{'role': 'user', 'content': 'Fais un examen'}], [])['draft'] is None)
finally:
    _AI.call_ai = _orig_call

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
