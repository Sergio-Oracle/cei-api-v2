"""Correction IA ancrée dans le cours (moteur RAG) : seul un sujet issu de « Générer Suggestions » sur des documents
indexés reçoit les passages du cours ; tout autre sujet est corrigé exactement comme avant ; le moteur arrêté ne bloque jamais."""
import os, sys, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from types import SimpleNamespace as NS
import routes.exams as R
from services import rag_correction as RC

RES = []
def chk(n, ok, det=''):
    RES.append(bool(ok)); print(('  ✔ ' if ok else '  ✘ ') + n + (f'  — {det}' if det and not ok else ''))

CONTENT = """Question 1 — Définissez la mémoire cache ............. (4 pts) [OUVERT]
Expliquez son rôle.

Question 2 — Citez deux types d'ordonnancement ............. (6 pts) [OUVERT]
"""
RUBRIC = """BARÈME DE NOTATION
Question 1 (4 pts) : la cache accélère l'accès à la mémoire [S1]
Question 2 (6 pts) : FIFO, Round Robin [S2]
TOTAL : 10 / 10 points
"""
PASSAGES = [
    {'id': 'S1', 'filename': 'cours_os.pdf', 'module': 'Chap 1', 'fileurl': 'https://m/x/cours_os.pdf',
     'content': 'La mémoire cache est une mémoire rapide placée entre le processeur et la RAM.'},
    {'id': 'S2', 'filename': 'cours_os.pdf', 'module': 'Chap 2', 'fileurl': 'https://m/x/cours_os.pdf',
     'content': "L'ordonnancement FIFO sert les processus dans l'ordre d'arrivée ; Round Robin donne un quantum."},
]
SEEN = {}
def fake_claude(system_prompt, user_message, temperature=0.15, **k):
    SEEN['msg'] = user_message; SEEN['n'] = SEEN.get('n', 0) + 1
    return "### Question 1\nPoints : 3.00/4.00\nBien.\n\n### Question 2\nPoints : 6.00/6.00\nParfait.\n\nPoints obtenus : 9.00"
R.call_claude = fake_claude

def subject(sid, grounded, passages=None):
    return NS(id=sid, ec_id=5, title='OS', content=CONTENT, rubric=RUBRIC, rag_grounded=grounded,
              rag_passages=json.dumps(passages) if passages else None)

def attempt(subj):
    ans = json.dumps({'pq_1': 'La cache est une mémoire rapide.', 'pq_2': 'FIFO et Round Robin'})
    return NS(id=7, answers=ans, score=None, feedback=None, question_scores=None, corrected_at=None, corrected_by_id=None,
              exam=NS(subject=subj, title='Examen OS', duration_minutes=60), student=NS(full_name='Étudiant Test'))

# cache Redis : repartir de zéro pour ces sujets de test
try:
    from cache import cache_delete_pattern
    cache_delete_pattern('cei:ragref:9990*'); cache_delete_pattern('cei:ragref:9991*'); cache_delete_pattern('cei:ragref:9992*')
except Exception:
    pass

# 1. sujet NON ancré : message strictement sans passages ni règles d'ancrage
SEEN.clear()
a = attempt(subject(9990, False))
SC = R._run_ai_correction_impl(a, corrected_by_id=1)
chk("sujet ordinaire : note enregistrée", SC is not None and SC > 0)
chk("sujet ordinaire : aucun passage ni règle d'ancrage dans la demande à l'IA",
    'PASSAGES DU COURS' not in SEEN['msg'] and 'ANCRÉE DANS LE COURS' not in SEEN['msg'])

# 2. sujet ancré : règles + passages enregistrés + passages retrouvés par question
live = {1: [{'filename': 'cours_os.pdf', 'content': 'Le cache L1 est plus rapide que L2 (passage retrouvé).'}]}
RC._live_by_question = lambda subj, stored: live
SEEN.clear()
a = attempt(subject(9991, True, PASSAGES))
SC = R._run_ai_correction_impl(a, corrected_by_id=1)
m = SEEN['msg']
chk("sujet ancré : note enregistrée", SC is not None and SC > 0)
chk("sujet ancré : règles d'ancrage transmises", 'RÈGLES DE CORRECTION ANCRÉE DANS LE COURS' in m)
chk("sujet ancré : passages enregistrés transmis", 'mémoire rapide placée entre le processeur' in m and "ordre d'arrivée" in m)
chk("sujet ancré : passage retrouvé pour la question transmis", 'passage retrouvé' in m)
chk("sujet ancré : le barème reste transmis", 'BARÈME DE NOTATION' in m and 'FIFO, Round Robin' in m)
chk("sujet ancré : la copie reste délimitée", '###COPIE_ETUDIANT_DEBUT###' in m and 'FIFO et Round Robin' in m)
chk("sujet ancré : interdiction d'écrire des codes de passage à l'étudiant", "n'écris" in m and '[S2]' in m)

# 3. moteur arrêté : la correction se fait quand même, avec les passages enregistrés
def down(subj, stored): return {}
RC._live_by_question = down
SEEN.clear()
a = attempt(subject(9992, True, PASSAGES))
SC = R._run_ai_correction_impl(a, corrected_by_id=1)
chk("moteur arrêté : copie corrigée quand même", SC is not None and SC > 0)
chk("moteur arrêté : passages enregistrés toujours transmis", 'mémoire rapide placée entre le processeur' in SEEN['msg'])

# 4. mise en cache : le moteur n'est interrogé qu'une fois par sujet, pas une fois par copie
calls = {'n': 0}
def counting(subj, stored):
    calls['n'] += 1; return live
RC._live_by_question = counting
try:
    from cache import cache_delete_pattern
    cache_delete_pattern('cei:ragref:9993*')
    s4 = subject(9993, True, PASSAGES)
    for _ in range(3):
        R._run_ai_correction_impl(attempt(s4), corrected_by_id=1)
    chk("3 copies du même sujet : une seule recherche dans le moteur", calls['n'] == 1, calls['n'])
except Exception as e:
    print('  (cache Redis indisponible, contrôle ignoré :', e, ')')

# 5. passages reçus du navigateur : bornés et nettoyés
raw = [{'id': 'S%d' % i, 'filename': 'f.pdf', 'content': 'x' * 9000} for i in range(30)] + ['pas un dict', {'content': '  '}]
cl = RC.clean_passages(raw)
chk("enregistrement : 12 passages au plus, 3000 caractères au plus", len(cl) == 12 and all(len(p['content']) <= 3000 for p in cl))
chk("enregistrement : entrées invalides ignorées", RC.clean_passages(['x', {'content': ''}, None]) == [])
chk("sujet sans passage : jamais « ancré »", RC.stored_passages(subject(1, True, None)) == [] and RC.reference_for(subject(1, True, None)) == '')
chk("sujet non ancré mais avec passages : ignoré", RC.reference_for(subject(1, False, PASSAGES)) == '')
chk("passages illisibles : ignorés sans erreur", RC.stored_passages(NS(rag_grounded=True, rag_passages='{pas du json')) == [])
# 6. raisonnement interne de l'IA avant le premier bloc : jamais affiché (6 copies de l'examen 140)
from text_clean import strip_ai_preamble as SP
leak = "Regardons le JSON fourni ? Le format demande : \"### Question {num}\".\n\n### Question 1\nPoints : 3.00/4.00\nBien.\n\nPoints obtenus : 3.00"
out = SP(leak)
chk("raisonnement avant le premier bloc retiré", out.startswith('### Question 1') and 'Regardons' not in out)
chk("retour déjà conforme : inchangé", SP("### Question 1\nPoints : 1/1\nok") == "### Question 1\nPoints : 1/1\nok")
chk("retour sans bloc reconnu : laissé tel quel", SP("Note totale 12") == "Note totale 12" and SP(None) is None)
SEEN.clear()
R.call_claude = lambda sp, um, temperature=0.15, **k: "Je réfléchis au JSON {pq_1}.\n\n### Question 1\nPoints : 3.00/4.00\nBien.\n\n### Question 2\nPoints : 6.00/6.00\nOk.\n\nPoints obtenus : 9.00"
a2 = attempt(subject(9990, False))
captured = {}
import routes.exams as _R
_orig_parse = _R._parse_ai_question_scores
_R._parse_ai_question_scores = lambda t: (captured.setdefault('t', t), _orig_parse(t))[1]
_R._run_ai_correction_impl(a2, corrected_by_id=1)
_R._parse_ai_question_scores = _orig_parse
chk("correction : le texte retenu commence au premier bloc", captured.get('t', '').startswith('### Question 1'), captured.get('t', '')[:40])
print(f"{sum(RES)}/{len(RES)} vérifications réussies"); sys.exit(0 if all(RES) else 1)
