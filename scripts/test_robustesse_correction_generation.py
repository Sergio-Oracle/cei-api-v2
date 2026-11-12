#!/usr/bin/env python
"""Robustesse de la correction et de la génération de sujets (retours des devoirs des 28/10 et 09/11/2026).

    cd cei-api-v2 && .venv-base/bin/python scripts/test_robustesse_correction_generation.py

1. Une réponse d'IA hostile (lignes pour des questions déjà notées, doublons, points au-dessus du plafond, « tu n'as pas répondu »
   alors que la copie contient une réponse) ne peut ni doubler les points ni afficher deux lignes pour la même question.
2. Un sujet généré dont une question est tronquée (aucune proposition, barème = modèle de format « X) — [justification courte] »)
   est refait question par question ; s'il reste défectueux, l'enseignant en est averti — jamais de clé de réponse inventée en silence.
3. Le contrôle de lisibilité d'un sujet existant signale ces défauts, le total ≠ 20 et les numéros en double.
Données temporaires, supprimées en fin de test. Aucune IA réelle n'est appelée.
"""
import os, re, sys, json, uuid
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as A
from routes import exams as R
from services import generation_check as G
from services.subject_check import analyze
from models import (get_session, User, UserRole, Subject, OnlineExam, ExamStatus, ExamAttempt, AttemptStatus, ExamActivityLog)
from werkzeug.security import generate_password_hash
from auth_paseto import create_access_token

RESULTS = []
def check(nom, ok, detail=''):
    RESULTS.append((nom, bool(ok)))
    print(('  ✔ ' if ok else '  ✘ ') + nom + (f'  — {detail!r}' if not ok else ''))

PLACEHOLDER_KEY = "  • Bonne réponse : X) — [justification courte]"

print('\n== Contrôle d\'une question générée (generation_check) ==')
ok_q = "Question 1 — T ............. (1 pts) [QCM]\nEnoncé ?\nA) a\nB) b\nC) c\nD) d"
check("QCM complet : aucun défaut", G.defects('qcm', ok_q, "  • Bonne réponse : B) — parce que") == [])
check("modèle de format « X) — [justification courte] » = défaut", any('barème' in d for d in G.defects('qcm', ok_q, PLACEHOLDER_KEY)))
check("QCM sans proposition = défaut", any('proposition' in d for d in G.defects('qcm', "Question 5 — T ............. (1 pts) [QCM]", "  • Bonne réponse : B) — x")))
check("clé hors des propositions (E) sur 4 choix) = défaut", any('parmi' in d for d in G.defects('qcm', ok_q, "  • Bonne réponse : E) — x")))
check("Vrai/Faux sans réponse au barème = défaut", bool(G.defects('vf', "Question 1 — Affirmation (1 pts) [VF]\nVrai / Faux", "  • explication seule")))
check("Vrai/Faux complet : aucun défaut", G.defects('vf', "Question 1 — Affirmation (1 pts) [VF]\nVrai / Faux", "  • Réponse : Faux — parce que") == [])
check("critère « Z pts — [Ce qui est attendu] » d'une question ouverte = défaut", bool(G.defects('open', "Question 2 — T (2 pts) [OUVERT]\nEnoncé", "  • Critère : Z pts — [Ce qui est attendu]")))
check("question ouverte avec vrai critère : aucun défaut", G.defects('open', "Question 2 — T (2 pts) [OUVERT]\nEnoncé", "  • Critère : 2 pts — définition exacte de la norme") == [])
check("association : nombre de réponses ≠ nombre d'éléments = défaut",
      bool(G.defects('association', "Question 3 — Associez (2 pts) [ASSOCIATION]\n- a\n- b\n- c\nA. x\nB. y\nC. z\nD. w", "  • Réponse : 1-A, 2-B")))
check("ordre : lettres du barème = éléments proposés → aucun défaut",
      G.defects('ordre', "Question 4 — Classez (1 pts) [ORDRE]\nA. un\nB. deux\nC. trois", "  • Ordre : C, A, B") == [])

print('\n== Réponse d\'IA hostile : rien n\'est compté deux fois ==')
hostile = [
    {'num': '1', 'type': 'open', 'max': 0.25, 'score': 0.25, 'feedback': 'ligne pour une question déjà notée'},     # déjà notée automatiquement
    {'num': '5', 'type': 'open', 'max': 2.0, 'score': 1.5, 'feedback': 'Bien.'},
    {'num': '5', 'type': 'open', 'max': 2.0, 'score': 2.0, 'feedback': 'doublon, points en plus'},                   # même question deux fois
    {'num': '6', 'type': 'open', 'max': 2.0, 'score': 3.0, 'feedback': "Tu n'as pas répondu à cette question."},      # plafond dépassé + affirmation fausse
    {'num': '7', 'type': 'open', 'max': 1.0, 'score': -2.0, 'feedback': 'Faux.'},                                    # points négatifs
]
copie = {'pq_1': 'C', 'pq_5': 'Texte A', 'pq_6': 'Texte B', 'pq_7': ''}
cl = R._clean_ai_question_scores(hostile, {'1', '2', '3', '4'}, copie)
nums = [q['num'] for q in cl]
check("aucune ligne pour une question déjà notée automatiquement", '1' not in nums, nums)
check("une seule ligne par question", len(nums) == len(set(nums)), nums)
check("la première ligne est gardée (1,5 et non 2,0)", next(q for q in cl if q['num'] == '5')['score'] == 1.5)
check("points plafonnés à ceux de la question (3,0 → 2,0)", next(q for q in cl if q['num'] == '6')['score'] == 2.0)
check("points jamais négatifs", next(q for q in cl if q['num'] == '7')['score'] == 0.0)
q6 = next(q for q in cl if q['num'] == '6')
check("« tu n'as pas répondu » alors qu'il y a une réponse : retour remplacé et question à relire", 'pas répondu' not in q6['feedback'] and q6.get('needs_review'))
q7 = next(q for q in cl if q['num'] == '7')
check("question réellement sans réponse : le retour de l'IA est gardé tel quel", 'needs_review' not in q7)


print('\n== Titres de questions : ni pointillés ni marqueur de difficulté visibles (lecteur serveur = lecteur navigateur) ==')
import subprocess, tempfile
FRONT = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), 'cei-next', 'app', 'exam', '[id]', 'page.tsx')
def js_blocks(contents):
    src = open(FRONT, encoding='utf-8').read()
    m = re.search(r'^function parseExamBlocks\(raw: string\): ParsedBlock\[\] \{.*?^\}\n', src, re.S | re.M)
    if not m: return None
    with tempfile.TemporaryDirectory() as d:
        open(os.path.join(d, 'p.ts'), 'w', encoding='utf-8').write(m.group(0)); open(os.path.join(d, 'in.json'), 'w', encoding='utf-8').write(json.dumps(contents))
        runner = ("const ts=require(process.argv[1]+'/node_modules/typescript'),fs=require('fs');"
                  "const js=ts.transpile(fs.readFileSync(process.argv[2]+'/p.ts','utf8'),{target:'es2020'})+'\\nmodule.exports={parseExamBlocks};';"
                  "fs.writeFileSync(process.argv[2]+'/p.js',js);const {parseExamBlocks}=require(process.argv[2]+'/p.js');"
                  "const ins=JSON.parse(fs.readFileSync(process.argv[2]+'/in.json','utf8'));console.log(JSON.stringify(ins.map(c=>parseExamBlocks(c))));")
        r = subprocess.run(['node', '-e', runner, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), 'cei-next'), d], capture_output=True, text=True, timeout=120)
        return json.loads(r.stdout) if r.returncode == 0 else None
titre = ("Question 1 — Adage latin ............. (1 pts) [QCM] [Facile]\nQue signifie l'adage ?\nA) a\nB) b\nC) c\nD) d\n\n"
         "Question 2 — Complétez la phrase : la capitale est ........ ............. (1 pts) [Moyen] [COURT]\n")
srv = [b for b in R._parse_subject_blocks_ordered(titre) if b['type'] not in ('section', 'text')]
check("serveur : « [Facile] » et « ............. » absents du titre", all('[Facile]' not in b['text'] and '....' * 3 not in b['text'] for b in srv), [b['text'] for b in srv])
check("serveur : le titre reste lisible (« Adage latin (1 pts) »)", srv[0]['text'] == 'Adage latin (1 pts)', srv[0]['text'])
check("serveur : un trou court « ........ » dans un énoncé est conservé", '........' in srv[1]['text'] and '[Moyen]' not in srv[1]['text'], srv[1]['text'])
jb = js_blocks([titre])
if jb:
    cl = [b for b in jb[0] if b['type'] not in ('section', 'text')]
    check("navigateur = serveur : mêmes titres", [b['text'] for b in cl] == [b['text'] for b in srv], ([b['text'] for b in cl], [b['text'] for b in srv]))
else:
    check("lecteur navigateur exécutable (node + typescript)", False, "node ou typescript introuvable")

# Champs à compléter : le titre ne doit pas être collé au texte à trous (capture d'aperçu du 11/11)
cloze = ("Question 1 — Champs dans un texte ............. (1 pts) [CLOZE]\nLa capitale est {1}, le pays compte {2} régions.\n- 1 COURT\n- 2 NUM\n\n"
         "Question 2 — La capitale du pays est\n(1 pts) [CLOZE]\n")
cloze_old = "Question 1 — La capitale du Sénégal est [CLOZE]\n{1} et le pays compte {2} régions.\n- 1 COURT\n- 2 NUM\n"
sc = [b for b in R._parse_subject_blocks_ordered(cloze) if b['type'] not in ('section', 'text')]
check("serveur : champs à compléter — le titre reste le titre (« Champs dans un texte (1 pts) »)", sc[0]['text'] == 'Champs dans un texte (1 pts)', sc[0]['text'])
sco = [b for b in R._parse_subject_blocks_ordered(cloze_old) if b['type'] not in ('section', 'text')]
check("serveur : ancien format (texte coupé sans points) — la suite reste jointe", '{1}' in sco[0]['text'] and 'capitale' in sco[0]['text'], sco[0]['text'])
jc = js_blocks([cloze, cloze_old])
if jc:
    a = [b for b in jc[0] if b['type'] not in ('section', 'text')][0]; o = [b for b in jc[1] if b['type'] not in ('section', 'text')][0]
    check("navigateur : titre « Champs dans un texte (1 pts) » et texte à trous dans les lignes suivantes", a['text'] == 'Champs dans un texte (1 pts)' and any('{1}' in l for l in a.get('extraLines', [])), (a['text'], a.get('extraLines')))
    check("navigateur = serveur : ancien format identique", o['text'] == sco[0]['text'], (o['text'], sco[0]['text']))
else:
    check("lecteur navigateur exécutable (cloze)", False, "node ou typescript introuvable")

c = A.app.test_client()
def H(u): return {'Authorization': 'Bearer ' + create_access_token(u.id, u.role.value, u.email)}
s = get_session(); tag = uuid.uuid4().hex[:6]; made = {'users': [], 'ex': [], 'sub': []}
def mk(role, n):
    u = User(email=f'rob.{tag}.{n}@test.invalid', full_name=f'Rob Test {n}', role=role, is_active=True, email_verified=True,
             has_email=True, password_hash=generate_password_hash('x'))
    s.add(u); s.commit(); made['users'].append(u.id); return u
real_ai = R.call_claude
try:
    adm = mk(UserRole.ADMIN, 'adm'); st = [mk(UserRole.STUDENT, f'e{i}') for i in range(3)]
    now = datetime.utcnow()
    def qcm(n, pts='0,25'): return f"Question {n} — Énoncé {n} ? ({pts} pts) [QCM]\nA) a\nB) b\nC) c\nD) d\n"
    def bar(n, pts='0,25', k='C'): return f"Question {n} ({pts} pts) :\n  • Bonne réponse : {k}) c\n\n"

    print('\n== Correction avec une IA hostile (bout en bout) ==')
    content = ''.join(qcm(i) for i in range(1, 5)) + "Question 5 — Expliquez A. (2 pts) [OUVERT]\n\nQuestion 6 — Expliquez B. (2 pts) [OUVERT]\n"
    rub = '=== BARÈME ===\n\n' + ''.join(bar(i) for i in range(1, 5)) + "Question 5 (2 pts) :\n  • Critère : définition\n\nQuestion 6 (2 pts) :\n  • Critère : exemple\n"
    sub = Subject(title='rob 1', content=content, rubric=rub, creator_id=adm.id); s.add(sub); s.commit(); made['sub'].append(sub.id)
    ex = OnlineExam(subject_id=sub.id, title='rob 1', duration_minutes=60, start_time=now - timedelta(hours=3), end_time=now - timedelta(hours=1),
                    status=ExamStatus.CLOSED, created_by_id=adm.id, auto_correct=True); s.add(ex); s.commit(); made['ex'].append(ex.id)
    a = ExamAttempt(exam_id=ex.id, student_id=st[0].id, status=AttemptStatus.SUBMITTED, submitted_at=now - timedelta(hours=1),
                    answers=json.dumps({'pq_1': 'C', 'pq_2': 'C', 'pq_3': 'C', 'pq_4': 'A', 'pq_5': 'Réponse A', 'pq_6': 'Réponse B'}))
    s.add(a); s.commit()
    HOSTILE_AI = ("### Question 1\nPoints : 0.25/0.25\nBonne réponse.\n\n"        # question déjà notée automatiquement
                  "### Question 5\nPoints : 1.50/2.00\nBonne définition.\n\n"
                  "### Question 5\nPoints : 2.00/2.00\nDoublon.\n\n"
                  "### Question 6\nPoints : 3.00/2.00\nTu n'as pas répondu à cette question.\n\n"
                  "Points obtenus : 6.75")
    R.call_claude = lambda *a, **k: HOSTILE_AI
    R._run_auto_correction(a.id); s.expire_all(); a = s.get(ExamAttempt, a.id)
    qs = json.loads(a.question_scores or '[]'); nm = [q['num'] for q in qs]
    check("une seule ligne par question (6 lignes)", len(nm) == 6 and len(set(nm)) == 6, nm)
    check("les 4 QCM restent notés automatiquement", sum(1 for q in qs if q['type'] == 'qcm') == 4)
    check("note = (0,75 + 1,5 + 2,0) / 5 × 20 = 17/20 (points de l'IA plafonnés, jamais doublés)", a.score == 17.0, a.score)
    check("aucune ligne « tu n'as pas répondu » pour une question répondue", not any('pas répondu' in (q.get('feedback') or '') for q in qs))
    check("la question contradictoire est marquée à relire", any(q.get('needs_review') for q in qs))

    print('\n== Génération : question tronquée refaite seule ==')
    def fake(system_prompt, user_message, **kw):
        n = int(re.search(r'EXACTEMENT (\d+) questions', user_message).group(1))
        if n == 1:   # petite réponse : complète
            return "Question 1 — Notion refaite ............. (1 pt) [QCM]\nÉnoncé complet ?\nA) un\nB) deux\nC) trois\nD) quatre\n\nBARÈME:\n  • Bonne réponse : B) — car deux"
        qs_ = ''.join(f"Question {i} — Titre {i} ............. (1 pt) [QCM]\nÉnoncé {i} ?\n" + ("A) a\nB) b\nC) c\nD) d\n\n" if i < 5 else "\n") for i in range(1, n + 1))
        return qs_            # réponse tronquée : question 5 sans propositions et AUCUN « BARÈME: »
    calls = []
    R.call_claude = lambda sp, um, **kw: (calls.append(1) or fake(sp, um, **kw))
    body = {'suggestion': {'title': 'Test', 'question_types': 'QCM (une seule réponse)', 'question_count': 5, 'total_points': 5,
                           'key_points': ['a'], 'difficulty': 'Moyen', 'duration': 30, 'student_level': 'Licence 1'}}
    r = c.post('/api/subjects/generate-full-exam', headers=H(adm), json=body)
    j = r.get_json() or {}
    check("génération réussie", r.status_code == 200 and j.get('success'), (r.status_code, str(j)[:200]))
    rub_out, cont = j.get('rubric', ''), j.get('content', '')
    check("plus aucun modèle de format dans le barème", not G.placeholder_in(rub_out), G.placeholder_in(rub_out))
    check("5 questions avec une vraie clé de réponse", len(re.findall(r'Bonne réponse : [A-D]\)', rub_out)) == 5, rub_out[:300])
    check("chaque QCM a ses propositions", len(G._CHOICE_RE.findall(cont)) >= 5 * 4)
    check("aucun avertissement : tout a été refait", not j.get('type_warnings'), j.get('type_warnings'))
    rep = analyze(cont, rub_out)
    check("le sujet généré passe le contrôle de lisibilité, barème compris", rep['ok'], [i['message'] for i in rep['issues'] if i['level'] == 'error'])

    print('\n== Génération : IA qui ne s\'en sort jamais → aucun sujet défectueux n\'est rendu ==')
    R.call_claude = lambda sp, um, **kw: "Question 1 — Titre ............. (1 pt) [QCM]\nÉnoncé ?\n"          # toujours incomplet
    r = c.post('/api/subjects/generate-full-exam', headers=H(adm), json=body)
    j = r.get_json() or {}
    check("aucun sujet rendu, message clair à l'enseignant", r.status_code == 500 and 'aucune question' in (j.get('error') or ''), (r.status_code, str(j)[:200]))

    print('\n== Génération : une question impossible à produire est RETIRÉE, les points sont redistribués (examen 170, 12/11/2026) ==')
    def fake2(system_prompt, user_message, **kw):
        n = int(re.search(r'EXACTEMENT (\d+) questions', user_message).group(1))
        if n == 1:      # les reprises une par une échouent toujours : question sans propositions
            return "Question 1 — Titre ............. (1 pt) [QCM]\nÉnoncé ?\n\nBARÈME:\n  • Bonne réponse : B) — car"
        out = ''.join(f"Question {i} — Titre {i} ............. (1 pt) [QCM]\nÉnoncé {i} ?\n" + ("A) a\nB) b\nC) c\nD) d\n\n" if i < n else "\n") for i in range(1, n + 1))
        return out.rstrip() + "\n\nBARÈME:\n" + '\n\n'.join(f"  • Bonne réponse : B) — car b{i}" for i in range(1, n + 1))
    R.call_claude = fake2
    r = c.post('/api/subjects/generate-full-exam', headers=H(adm), json=body)
    j = r.get_json() or {}; cont, rub_out = j.get('content', ''), j.get('rubric', '')
    check("génération réussie malgré la question impossible", r.status_code == 200 and j.get('success'), (r.status_code, str(j)[:200]))
    check("la question défectueuse n'est PAS dans le sujet (4 questions)", len(re.findall(r'(?m)^Question \d+ —', cont)) == 4, re.findall(r'(?m)^Question \d+ —.*', cont))
    check("chaque question gardée a ses propositions", len(G._CHOICE_RE.findall(cont)) == 16)
    check("le total reste la note demandée (5 points sur 4 questions)", 'TOTAL : 5 / 5 points' in rub_out and cont.count('(1.25 pts)') == 4, rub_out[-120:])
    check("l'enseignant est prévenu du retrait", any('retirées' in w for w in (j.get('type_warnings') or [])), j.get('type_warnings'))
    rep = analyze(cont, rub_out)
    check("le sujet rendu passe le contrôle sans erreur ni avertissement de total", rep['ok'] and not any('totalisent' in i['message'] for i in rep['issues']) or '5 points' in str(rep['issues']), rep['issues'])

    print('\n== Clé de barème recopiée du modèle de format (examen 170) ==')
    tq = "Question 1 — Complétez (1 pts) [TROUS]\nIl faut activer les {1} du {2}.\nA. mises à jour\nB. système\nC. sauvegardes\n"
    check("« 1-A, 2-C  (numéro du trou, tiret, lettre…) » = exemple recopié, question défectueuse", G.defects('trous', tq, "• Réponse : 1-A, 2-C  (numéro du trou, tiret, lettre de la proposition)"))
    check("une vraie clé « 1-A, 2-B » est acceptée", not G.defects('trous', tq, "• Réponse : 1-A, 2-B"), G.defects('trous', tq, "• Réponse : 1-A, 2-B"))
    check("clé hors des propositions refusée", G.defects('trous', tq, "• Réponse : 1-A, 2-F"))
    check("clé qui ne couvre pas tous les trous refusée", G.defects('trous', tq, "• Réponse : 1-A"))
    check("texte à trous sans propositions refusé", G.defects('trous', "Question 1 — C (1 pts) [TROUS]\nIl faut {1} l'ordinateur\n", "• Réponse : 1-A"))

    print('\n== Sujet enregistré : texte à trous défectueux bloqué à l\'activation (examen 170) ==')
    c170 = ("QUESTIONS\n\nQuestion 1 — Mises à jour (1 pts) [TROUS]\nIl faut activer les {1} du {2}.\nA. mises à jour\nB. système\nC. sauvegardes\n\n"
            "Question 2 — Nettoyage (1 pts) [TROUS]\nAvant d'ouvrir le boîtier, il faut {1} l'ordinateur\n")
    r170 = ("BARÈME DE NOTATION\n\nQuestion 1 — Mises à jour (1 pts)\n• Réponse : 1-A, 2-C  (numéro du trou, tiret, lettre de la proposition)\n\n"
            "Question 2 — Nettoyage (1 pts)\n• Réponse : 1-A, 2-C  (numéro du trou, tiret, lettre de la proposition)\n\nTOTAL : 2 / 2 points")
    rep = analyze(c170, r170); msgs = ' | '.join(i['message'] for i in rep['issues'] if i['level'] == 'error')
    check("le sujet est refusé", not rep['ok'])
    check("clé recopiée du modèle signalée aux deux questions", msgs.count('exemple de format recopié') == 2, msgs[:300])
    check("question sans proposition signalée", "Question 2 : aucune proposition" in msgs, msgs[:400])
    check("clé qui ne correspond pas aux trous signalée", "Question 2 : le barème donne 2 réponse(s) mais la question a 1" in msgs, msgs[:500])
    ok_c = c170.replace("il faut {1} l'ordinateur\n", "il faut {1} l'ordinateur et {2} le câble.\nA. éteindre\nB. débrancher\nC. redémarrer\n")
    ok_r = r170.replace("1-A, 2-C  (numéro du trou, tiret, lettre de la proposition)", "1-A, 2-B")
    rep = analyze(ok_c, ok_r)
    check("le même sujet corrigé est accepté", rep['ok'], [i['message'] for i in rep['issues'] if i['level'] == 'error'])

    print('\n== L\'étudiant ne reçoit pas l\'en-tête généré (durée, consignes automatiques) ==')
    gen = ("══════════\nTITRE\n══════════\nDurée : 90 minutes | Note totale : 20 points\n══════════\n\nINSTRUCTIONS AUX ÉTUDIANTS\n──────\n"
           "Vous disposez de 90 minutes pour composer.\n\n══════════\nQUESTIONS\n══════════\n\nQuestion 1 — Un trajet dure 90 minutes ? (1 pts) [VF]\nVrai / Faux\n")
    out = R._align_subject_duration(gen)
    check("« Durée : 90 minutes » et « Vous disposez de 90 minutes » retirés", 'Durée : 90' not in out and 'Vous disposez' not in out and 'INSTRUCTIONS' not in out, out[:120])
    check("les questions sont intactes (énoncé compris)", out.startswith('Question 1 — Un trajet dure 90 minutes ?'), out[:80])
    check("mêmes questions lues avec ou sans l'en-tête", [b_['type'] for b_ in R._parse_subject_blocks_ordered(out)] == [b_['type'] for b_ in R._parse_subject_blocks_ordered(gen)])
    doc = "DEVOIR DE DROIT\nDurée : 2 heures\n\n1. Question déposée ?\nA. oui\nB. non\n"
    check("un document déposé (sans en-tête généré) est rendu tel quel", R._align_subject_duration(doc) == doc)

    print('\n== « Générer d\'autres questions » : vraies clés, question défectueuse refaite ou écartée ==')
    base_c = "QUESTIONS\n\n" + ''.join(f"Question {i} — Titre {i} ............. (1 pts) [QCM]\nÉnoncé {i} ?\nA) a\nB) b\nC) c\nD) d\n\n" for i in (1, 2))
    base_r = "BARÈME DE NOTATION\n\n" + ''.join(f"Question {i} — Titre {i} (1 pts)\n  • Bonne réponse : B) — b\n\n" for i in (1, 2)) + "──────────\nTOTAL : 2 / 2 points"
    def more_ai(prompt):
        if 'enseignant universitaire rigoureux' in prompt:      # vérification de fond : en désaccord sur la question 3
            return "3: A\n4: C"
        if 'EXACTEMENT 1 NOUVELLE' in prompt:                    # reprise : toujours sans propositions
            return "Question 9 — Refaite ............. (1 pt) [QCM]\nÉnoncé ?\n\nBARÈME:\n  • Bonne réponse : A) — a"
        return ("Question 3 — Nouvelle 3 ............. (1 pt) [QCM]\nÉnoncé 3 ?\nA) un\nB) deux\nC) trois\nD) quatre\n\n"
                "Question 4 — Nouvelle 4 ............. (1 pt) [QCM]\nÉnoncé 4 ?\nA) un\nB) deux\nC) trois\nD) quatre\n\n"
                "Question 5 — Tronquée ............. (1 pt) [QCM]\nÉnoncé 5 ?\n\n"
                "BARÈME:\n  • Bonne réponse : C) — trois\n\n  • Bonne réponse : C) — trois\n\n  • Bonne réponse : B) — deux")
    real_simple = R.call_ai_simple; R.call_ai_simple = more_ai
    try:
        r = c.post('/api/subjects/generate-more-questions', headers=H(adm), json={'existing_content': base_c, 'existing_rubric': base_r, 'count': 3,
                   'question_type': 'QCM', 'title': 'T', 'total_points': 4})
    finally:
        R.call_ai_simple = real_simple
    j = r.get_json() or {}
    check("ajout réussi", r.status_code == 200 and j.get('success'), (r.status_code, str(j)[:200]))
    check("2 questions ajoutées, la tronquée est écartée", j.get('count_generated') == 2 and j.get('dropped') == 1 and 'Tronquée' not in j.get('full_content', ''), (j.get('count_generated'), j.get('dropped')))
    check("le barème contient la VRAIE clé des nouvelles questions", j.get('full_rubric', '').count('Bonne réponse : C) — trois') == 2 and 'Réponse attendue' not in j.get('full_rubric', ''), j.get('full_rubric', '')[-300:])
    check("l'enseignant est prévenu du retrait", any("n'ont pas été ajoutées" in w for w in j.get('warnings') or []), j.get('warnings'))
    check("vérification de fond : désaccord signalé sur la question 3 seulement", sum('Question 3 : à relire' in w for w in j.get('warnings') or []) == 1 and not any('Question 4' in w for w in j.get('warnings') or []), j.get('warnings'))
    rep = analyze(j.get('full_content', ''), j.get('full_rubric', ''))
    check("le sujet complété passe le contrôle (4 questions, 4 points)", rep['ok'] and rep['questions'] == 4, [i['message'] for i in rep['issues']])

    print('\n== Vérification de fond des réponses (examen 170 : clé 2-C au lieu de 2-B) ==')
    from services.answer_verify import verify_keys
    vq = "Question 1 — MAJ (1 pts) [TROUS]\nActiver les {1} du {2}.\nA. mises à jour\nB. système\nC. sauvegardes\n\nQuestion 2 — V (1 pts) [VF]\nVrai / Faux\n\nQuestion 3 — Q (1 pts) [QCM]\nA) a\nB) b\n"
    vr = "Question 1 — MAJ (1 pts)\n• Réponse : 1-A, 2-C\n\nQuestion 2 — V (1 pts)\n• Réponse : Faux — x\n\nQuestion 3 — Q (1 pts)\n• Bonne réponse : B) — b\n"
    w = verify_keys(vq, vr, ask=lambda p: "[TROUS] « 1: 1-A, 2-B »\n2: Faux\n3: ?")
    check("la clé fausse est signalée « à relire » avec les deux réponses", len(w) == 1 and '1-A, 2-C' in w[0] and '1-A, 2-B' in w[0], w)
    check("réponse concordante ou incertaine : aucune alerte", not verify_keys(vq, vr, ask=lambda p: "1: 1-A, 2-C\n2: Faux\n3: ?"))
    def boom(p): raise RuntimeError('IA indisponible')
    check("IA indisponible : aucune alerte, jamais d'erreur", verify_keys(vq, vr, ask=boom) == [])

    print('\n== Barème qui ne totalise pas 20 : information sobre, pas un défaut (« Test Blanc », 5 QCM de 2 points) ==')
    tb_c = ''.join(f"Question {i} — Titre {i} ............. (2 pts) [QCM]\nÉnoncé {i} ?\nA) a\nB) b\nC) c\nD) d\n\n" for i in range(1, 6))
    tb_r = "BARÈME DE NOTATION\n\n" + ''.join(f"Question {i} — Titre {i} (2 pts)\n  • Bonne réponse : B) — b\n\n" for i in range(1, 6)) + "TOTAL : 10 / 10 points"
    rep = analyze(tb_c, tb_r)
    check("sujet accepté, aucun avertissement ni erreur", rep['ok'] and not any(i['level'] in ('warn', 'error') for i in rep['issues']), rep['issues'])
    check("une information sobre indique le barème sur 10 ramené sur 20", [i['level'] for i in rep['issues']] == ['info'] and 'ramenées automatiquement sur 20' in rep['issues'][0]['message'], rep['issues'])

    print('\n== Contrôle d\'un sujet déjà enregistré (cas du 09/11/2026) ==')
    bad_c = ''.join(qcm(i, '1') for i in range(1, 5)) + "Question 5 — Architecture (1 pts) [QCM]\n"
    bad_r = '=== BARÈME ===\n\n' + ''.join(f"Question {i} (1 pts) :\n{PLACEHOLDER_KEY}\n\n" for i in range(1, 6)) + "TOTAL : 5 / 5 points"
    rep = analyze(bad_c, bad_r); msgs = ' | '.join(i['message'] for i in rep['issues'])
    check("sujet refusé à l'activation sans confirmation", not rep['ok'])
    check("la question sans proposition est signalée", 'Question 5 : 0 proposition' in msgs, msgs[:200])
    check("le barème « X) — [justification courte] » est signalé", 'barème non rempli' in msgs, msgs[:200])
    dup_c = qcm(1) + qcm(2) + qcm(1)
    check("numéros de question en double = erreur (les réponses s'écraseraient)", not analyze(dup_c)['ok'])
    sub14 = ''.join(qcm(i) for i in range(1, 57)); rub14 = '=== BARÈME ===\n\n' + ''.join(bar(i) for i in range(1, 57)) + ''.join(bar(i) for i in range(57, 69))
    msgs = ' | '.join(i['message'] for i in analyze(sub14, rub14)['issues'])
    check("barème de 68 questions pour 56 posées (devoir du 28/10) : signalé avant l'examen", 'absentes du sujet' in msgs and 'ramenées automatiquement sur 20' in msgs, msgs[:250])
finally:
    R.call_claude = real_ai
    s.rollback()
    try:
        if made['ex']:
            att = [x.id for x in s.query(ExamAttempt).filter(ExamAttempt.exam_id.in_(made['ex']))]
            if att: s.query(ExamActivityLog).filter(ExamActivityLog.attempt_id.in_(att)).delete(synchronize_session=False)
            s.query(ExamAttempt).filter(ExamAttempt.exam_id.in_(made['ex'])).delete(synchronize_session=False)
            s.query(OnlineExam).filter(OnlineExam.id.in_(made['ex'])).delete(synchronize_session=False)
        if made['sub']: s.query(Subject).filter(Subject.id.in_(made['sub'])).delete(synchronize_session=False)
        if made['users']: s.query(User).filter(User.id.in_(made['users'])).delete(synchronize_session=False)
        s.commit()
    except Exception as e:
        s.rollback(); print('nettoyage incomplet :', e)
    s.close()
bad = [n for n, ok in RESULTS if not ok]
print(f"\n{len(RESULTS) - len(bad)}/{len(RESULTS)} vérifications réussies" + (f" — ÉCHECS : {bad}" if bad else ''))
sys.exit(1 if bad else 0)
