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
    m = re.search(r'^const DOC_HEADER_CUES.*?^function parseExamBlocks\(raw: string\): ParsedBlock\[\] \{.*?^\}\n', src, re.S | re.M)
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

# 4. Rien ne disparaît en silence : contrôle de couverture
from services.subject_coverage import coverage_issues
check("sujet sain : aucun avertissement", coverage_issues(BON) == [], coverage_issues(BON))
SEPT = "1. Q1. Choisir ? [QCM]\nA. a1\nB. b1\nC. c1\nD. d1\nE. e1\nF. f1\nG. g1\nH. h1\n"
bs = [x for x in parse_py(SEPT) if x['type'] == 'qcm']
check("QCM à 8 propositions (A à H) : toutes lues, aucune collée à la précédente (serveur)", bs and [c['letter'] for c in bs[0]['choices']] == list('ABCDEFGH') and bs[0]['choices'][5]['text'] == 'f1', bs and bs[0]['choices'])
js3 = js_blocks([SEPT])
if js3:
    jb = [x for x in js3[0] if x['type'] == 'qcm']
    check("QCM à 8 propositions : même résultat dans le navigateur", jb and [c['letter'] for c in jb[0]['choices']] == list('ABCDEFGH') and jb[0]['choices'][5]['text'] == 'f1')
check("QCM à 8 propositions : aucun avertissement", coverage_issues(SEPT) == [], coverage_issues(SEPT))
FIN = BON + "\nNB : justifiez chaque réponse sur votre copie.\n"
iss = coverage_issues(FIN)
check("texte placé après la dernière question (affiché nulle part) : signalé", iss and 'justifiez chaque' in iss[0]['message'], iss)
check("« Fin de l'épreuve » / « Bon courage » : pas d'alerte", coverage_issues(BON + "\nBon courage !\nFin de l'épreuve\n") == [])
HYP = "1. Q1. La conclusion est-elle une conséquence des trois hypothèses ? [QCM]\nA. oui\nB. non\n"
iss = coverage_issues(HYP)
check("question qui cite « les trois hypothèses » absentes : signalée", iss and 'hypoth' in iss[0]['message'], iss)
check("…mais pas si l'énoncé de la partie les donne", coverage_issues("Partie A — X\nHypothèses : H1 = p ; H2 = q ; H3 = r.\n\n" + HYP) == [], coverage_issues("Partie A — X\nHypothèses : H1 = p ; H2 = q ; H3 = r.\n\n" + HYP))
check("« propositions ci-dessous » (les choix suivent) : pas d'alerte", coverage_issues("1. Q1. Choisissez parmi les propositions ci-dessous. [QCM]\nA. a\nB. b\n") == [])
CLOZE = "1. Dans la dissertation comme dans le commentaire, l'annonce du plan fait apparaître le [CLOZE]\n(I) et le {1}\n- 1 COURT\n"
bc = [x for x in parse_py(CLOZE) if x['type'] == 'cloze']
check("texte à trous coupé sur deux lignes : suite jointe au texte (serveur)", bc and '(I) et le {1}' in bc[0]['text'] and not bc[0]['options'], bc and bc[0]['options'])
js2 = js_blocks([CLOZE])
if js2:
    jc = [x for x in js2[0] if x['type'] == 'cloze']
    check("texte à trous coupé sur deux lignes : même résultat dans le navigateur", jc and jc[0]['text'] == bc[0]['text'] and not jc[0]['options'], jc and jc[0]['text'])

# 5. Correction : l'énoncé d'une partie qui suit une question n'est pas lu comme ses propositions (incident du 02/11/2026, Devoir2-IA, Q6)
from routes.exams import _parse_subject_questions_for_grading as parse_grading
SUITE = ("1. Q1. Qui ment ? [QCM]\nA. Paul\nB. Marie\nC. Luc\nD. Anne\n\n"
         "Partie B — Les témoins\nRappel des déclarations :\nA : « Je n'ai rien effacé »\nB : « Si C est coupable, D l'est aussi »\n\n"
         "2. Q2. Autre question ? [QCM]\nA. oui\nB. non\n")
g = parse_grading(SUITE)
check("correction : les propositions de Q1 sont les siennes (pas les déclarations de la partie B)", g['1']['choices'] == {'A': 'Paul', 'B': 'Marie', 'C': 'Luc', 'D': 'Anne'}, g['1']['choices'])
check("correction : Q2 garde ses propositions", g['2']['choices'] == {'A': 'oui', 'B': 'non'}, g['2']['choices'])

# 6. Titre de partie COLLÉ à une question (incident du 13/11/2026, Devoir-IA-Master IL) : l'énoncé de l'exercice 1 s'affichait sur les 20 questions
#    car « 6. Exercice 2 : … Q6. … » n'était pas reconnu comme titre. Sujet fabriqué (même structure, aucun contenu réel).
def _qcm(n, txt): return f"{n}. Q{n}. {txt} [QCM]\n{PROPS}"
EX1 = ("Exercice 1- Les trois voisins\nOn note A = « A est chez lui ». Rappel des déclarations :\nA : « Je suis chez moi »\nB : « Si A est chez lui, C aussi »\n\n"
       + "\n".join(_qcm(i, f"Question {i} de l'exercice 1 ?") for i in range(1, 6)))
GLUE6 = ("6. Exercice 2 : Logique des prédicats Prédicats : livre(x), lu(x, y). La constante C désigne le cours de Chimie. "
         f"Q6. Pourquoi cette formule est-elle fausse ? [QCM]\n{PROPS}")
SUITE = "\n".join(_qcm(i, f"Question {i} ?") for i in range(7, 11)) + "\n\n\n" + "\n".join(_qcm(i, f"Question {i} d'une autre partie ?") for i in range(11, 21))
IA = EX1 + "\n" + GLUE6 + "\n" + SUITE
ctx_of = lambda blocks: {q['num']: (q.get('context') or {}) for q in blocks if q['type'] not in ('section', 'description', 'text')}

old = ctx_of(parse_py(IA))
check("AVANT correction (cause de l'incident) : l'énoncé de l'exercice 1 est collé aux 20 questions", all('Exercice 1' in (c.get('title') or '') for c in old.values()) and len(old) == 20, {k: old[k].get('title') for k in ('6', '11')})
fixed, notes = normalize(IA)
check("titre collé : détecté et remis sur sa propre ligne (note lisible)", any('titre(s) de partie collé' in x for x in notes), notes)
check("normalisation idempotente (titre collé)", normalize(fixed)[0] == fixed)
check("la question 6 garde son texte et ses 4 propositions", 'Pourquoi cette formule est-elle fausse' in fixed and '6. Q6. Pourquoi' in fixed)
new = ctx_of(parse_py(fixed))
check("20 questions toujours lues, aucune perdue", sorted(new, key=int) == [str(i) for i in range(1, 21)], sorted(new, key=int))
check("Q1 à Q5 : énoncé de l'exercice 1", all(new[str(i)].get('title', '').startswith('Exercice 1') and 'Rappel des déclarations' in new[str(i)].get('text', '') for i in range(1, 6)), new['1'])
check("Q6 à Q10 : titre « Exercice 2 : Logique des prédicats » et SON énoncé (plus celui de l'exercice 1)",
      all(new[str(i)].get('title') == 'Exercice 2 : Logique des prédicats' and new[str(i)].get('text', '').startswith('Prédicats : livre(x)') for i in range(6, 11)), new['6'])
check("l'énoncé de l'exercice 1 ne s'affiche plus au-delà de Q5", not any('Rappel des déclarations' in new[str(i)].get('text', '') for i in range(6, 21)))
check("lignes ordinaires jamais coupées : « Partie de … », « Exercice de style », numéros différents",
      normalize("6. Partie de cartes Q6. Une question ? [QCM]\nA. a\nB. b\n")[0] == "6. Partie de cartes Q6. Une question ? [QCM]\nA. a\nB. b\n"
      and normalize("6. Exercice de style Q6. Une question ? [QCM]\nA. a\nB. b\n")[0] == "6. Exercice de style Q6. Une question ? [QCM]\nA. a\nB. b\n"
      and normalize("3. Exercice 2 : x Q4. Une question ? [QCM]\nA. a\nB. b\n")[0] == "3. Exercice 2 : x Q4. Une question ? [QCM]\nA. a\nB. b\n")
from services.subject_normalizer import _split_section_head
check("titre seul (« Exercice 3 : Graphes conceptuels ») : reste un titre complet", _split_section_head('Exercice 3 : Graphes conceptuels') == ('Exercice 3 : Graphes conceptuels', ''))
check("séparateur d'origine conservé (« Partie B — Les témoins »)", _split_section_head('Partie B — Les témoins Rappel : A dit non') == ('Partie B — Les témoins', 'Rappel : A dit non'))
check("énoncé long sans sous-titre : titre = « Exercice 4 », énoncé intact", _split_section_head('Exercice 4 On considère un réseau de 5 machines. Chaque machine est reliée à deux autres.') == ('Exercice 4', 'On considère un réseau de 5 machines. Chaque machine est reliée à deux autres.'))
jsia = js_blocks([fixed])
if jsia:
    jn = ctx_of(jsia[0])
    check("navigateur : mêmes énoncés que le serveur sur le sujet corrigé", jn == new, {k: (jn[k].get('title'), new[k].get('title')) for k in jn if jn[k] != new.get(k)})

from services.subject_check import analyze
legacy = [i['message'] for i in analyze(IA)['issues']]
check("sujet ancien (titre collé, non normalisé) : avertissement « collé au début de la question 6 »", any('collé au début de la question 6' in m for m in legacy), legacy)
fx = [i for i in analyze(fixed)['issues']]
check("sujet corrigé : plus d'avertissement « titre collé »", not any('collé au début' in i['message'] for i in fx), fx)
check("même énoncé sur Q6 à Q20 (15 questions, pas de titre avant Q11) : signalé, jamais bloquant",
      any(i['level'] == 'warn' and 'de la 6 à la 20' in i['message'] for i in fx) and analyze(fixed)['ok'], fx)
check("Q1 à Q5 (5 questions) : pas d'alerte d'énoncé répété", not any('de la 1 à la 5' in i['message'] for i in fx))
AVEC_TITRE = fixed.replace("11. Q11.", "Exercice 3 : Autre partie\n\n11. Q11.")
check("avec un titre « Exercice 3 » avant Q11 : l'énoncé de l'exercice 2 s'arrête à Q10, plus d'alerte de répétition",
      all(ctx_of(parse_py(AVEC_TITRE))[str(i)].get('title') == 'Exercice 3 : Autre partie' for i in range(11, 21)) and not any('s\'affiche sur' in i['message'] for i in analyze(AVEC_TITRE)['issues']))

# 7. En-tête de DOCUMENT avant la question 1 (examen 181 : « Sujet… Master SRIV / UFC… Session Normale » répété sur 20 questions) :
#    montré une seule fois (sur Q1) ; un vrai énoncé ou une consigne reste, lui, affiché sur toutes les questions du groupe.
HUIT = "\n".join(_qcm(i, f"Question {i} ?") for i in range(1, 9))
ENTETE = "Sujet de Réseaux\nMaster ABC / Université X\nDr Jean Dupont\nSession Normale\n10 questions à choix multiples, une seule réponse exacte par question\n\n" + HUIT
ce = ctx_of(parse_py(ENTETE))
check("en-tête de document : affiché sur Q1", 'Sujet de Réseaux' in (ce['1'].get('text') or ''), ce['1'])
check("en-tête de document : plus répété sur Q2 à Q8", not any(ce[str(i)] for i in range(2, 9)), {k: v for k, v in ce.items() if v and k != '1'})
CONSIGNE = "Consigne : Pour chaque situation, choisissez une seule réponse parmi les quatre propositions.\n\n" + "\n".join(_qcm(i, f"Question {i} ?") for i in range(1, 4))
cc = ctx_of(parse_py(CONSIGNE))
check("simple consigne (aucun indice d'en-tête) : reste sur toutes les questions", all('Consigne' in (cc[str(i)].get('text') or '') for i in (1, 2, 3)), cc)
NOTE = "On note r = « le feu est rouge » et v = « le feu est vert ». Rappel : r et v s'excluent.\n\n" + "\n".join(_qcm(i, f"Question {i} ?") for i in range(1, 4))
cn = ctx_of(parse_py(NOTE))
check("énoncé sans titre de partie (« On note… Rappel ») : reste sur toutes les questions (cas du 02/11)", all('On note r' in (cn[str(i)].get('text') or '') for i in (1, 2, 3)), cn)
MIXTE = "Examen — Durée : 1 heure\nSoit une suite de nombres entiers.\n\n" + "\n".join(_qcm(i, f"Question {i} ?") for i in range(1, 4))
cm = ctx_of(parse_py(MIXTE))
check("en-tête MÊLÉ à un énoncé (« Soit… ») : traité comme un énoncé, affiché partout", all(cm[str(i)].get('text') for i in (1, 2, 3)), cm)
jh = js_blocks([ENTETE, CONSIGNE, NOTE, MIXTE])
if jh:
    check("navigateur = serveur : en-tête (une fois), consigne, énoncé, mélange",
          [ctx_of(x) for x in jh] == [ce, cc, cn, cm], [k for k, (a, b) in enumerate(zip([ctx_of(x) for x in jh], [ce, cc, cn, cm])) if a != b])
else:
    check("lecteur navigateur exécutable (cas d'en-tête)", False)
check("en-tête affiché une fois : plus d'alerte « même énoncé sur N questions »", not any("s'affiche sur" in i['message'] for i in analyze(ENTETE)['issues']))

# 8. Association dont les éléments sont suivis d'une LIGNE VIDE puis des définitions « A. … » (sujet 142, trouvé par le corpus réel le 13/11/2026) :
#    le lecteur du navigateur arrêtait la question à la ligne vide et faisait des définitions un faux « énoncé » au-dessus des questions suivantes.
ASSOC = ("1. Associez le terme à sa définition : [ASSOCIATION]\n- Entonnoir\n- Chapeau\n- Plan\n\n"
         "A. Lignes en tête d'une partie\nB. Opération qui rend visible le texte\nC. Système du général au particulier\n\n"
         "2. Complétez la phrase : le latin *intro ducere* signifie conduire {1}. [CLOZE]\n- 1 COURT\n\n"
         "3. Expliquez en quelques lignes. [OUVERT]\n")
pa = parse_py(ASSOC); qa = [x for x in pa if x['type'] not in ('section', 'description', 'text')]
check("association + ligne vide + définitions (serveur) : 3 questions, aucun faux énoncé sur Q2 et Q3", [x['num'] for x in qa] == ['1', '2', '3'] and not qa[1].get('context') and not qa[2].get('context'), [x.get('context') for x in qa])
check("association (serveur) : les 3 éléments et les 3 définitions sont bien ceux de la question 1", qa[0].get('items') is not None and len(qa[0].get('items') or []) == 3 and len(qa[0].get('options') or []) == 3, (qa[0].get('items'), qa[0].get('options')))
jas = js_blocks([ASSOC])
if jas:
    qj = [x for x in jas[0] if x['type'] not in ('section', 'description', 'text')]
    check("association + ligne vide + définitions (navigateur) : mêmes 3 questions, aucun faux énoncé", [x['num'] for x in qj] == ['1', '2', '3'] and not qj[1].get('context') and not qj[2].get('context'), [x.get('context') for x in qj])
    check("association : même résultat serveur et navigateur (éléments et définitions)", (qj[0].get('items') or []) == (qa[0].get('items') or []) and [o['letter'] for o in (qj[0].get('options') or [])] == [o['letter'] for o in (qa[0].get('options') or [])], (qj[0].get('items'), qj[0].get('options')))

ok = sum(RES); print(f"\n{ok}/{len(RES)} vérifications réussies")
sys.exit(0 if ok == len(RES) else 1)
