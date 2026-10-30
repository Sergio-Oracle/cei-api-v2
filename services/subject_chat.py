"""Assistant de création de sujets par DISCUSSION : l'enseignant joint ses fichiers, dit ce qu'il veut, l'IA propose, il ajuste.

  store_files(user_id, files)      -> lit chaque fichier (Word, PDF, texte), le convertit au format CEI, le garde 2 h (Redis)
  chat(user_id, messages, file_ids, context, draft) -> {'reply', 'draft': {title, content, rubric, report} | None}

Règles de sûreté :
  • reproduire un fichier « tel quel » ne passe JAMAIS par la plume de l'IA : l'IA écrit <<<REPRODUIRE n>>> et le serveur insère
    le sujet reconnu par la plateforme (types détectés, grille de mots croisés, barème du corrigé) — exact, sans réécriture ;
  • tout brouillon écrit par l'IA repasse par la détection de types, la numérotation continue et le contrôle de lisibilité,
    et le rapport (nombre de questions, types, défauts) est renvoyé à l'enseignant avant qu'il n'enregistre.
"""
import os
import re
import tempfile
import uuid

from werkzeug.utils import secure_filename

_TTL = 2 * 3600
_MAX_FILES = 6
_MAX_CHARS_FILE = 45000
_MAX_CHARS_TOTAL = 90000


def _key(user_id, fid):
    return f'cei:subchat:{user_id}:{fid}'


def store_files(user_id, files):
    from cache import cache_set
    from utils import extract_text_from_file
    from services.exam_normalizer import normalize_exam_text
    from services.subject_normalizer import loose_mcq
    from services.type_detector import detect_and_annotate
    from services.crossword_pdf import extract_crosswords
    from services.corrige_reader import read_corrige_file
    out = []
    with tempfile.TemporaryDirectory() as d:
        for f in (files or [])[:_MAX_FILES]:
            if not f or not f.filename:
                continue
            name = secure_filename(f.filename) or 'fichier'
            path = os.path.join(d, name)
            f.save(path)
            text = extract_text_from_file(path) or ''
            if not text.strip():
                out.append({'id': None, 'name': f.filename, 'error': "Impossible de lire ce fichier"})
                continue
            is_corrige = bool(re.search(r'corrig|correction|bar[eè]me|solution', f.filename, re.I))
            rec = {'name': f.filename, 'corrige': is_corrige, 'raw': text[:_MAX_CHARS_FILE]}
            if is_corrige:
                key = read_corrige_file(path, text)
                rec['key'] = {'qcm': key['qcm'], 'vf': key['vf'], 'assoc': {str(k): v for k, v in key['assoc'].items()},
                              'trous': key['trous'], 'cw': {f'{a}{b}': v for (a, b), v in key['cw'].items()}}
                rec['annotated'] = text[:_MAX_CHARS_FILE]
            else:
                part, rep = normalize_exam_text(text)
                if rep.get('structured'):
                    annotated = part
                else:
                    part, _k, _n = loose_mcq(part, 1)
                    annotated, _r = detect_and_annotate(part, extract_crosswords(path))
                rec['annotated'] = annotated[:_MAX_CHARS_FILE]
            fid = uuid.uuid4().hex[:12]
            cache_set(_key(user_id, fid), rec, _TTL)
            out.append({'id': fid, 'name': f.filename, 'chars': len(text), 'corrige': is_corrige})
    return out


_SYSTEM = """Tu es l'assistant de création d'examens du Centre d'Examen Intelligent (université, enseignement à distance, francophone).
Tu discutes avec un enseignant. Il peut joindre ses documents (cours, ancien sujet, corrigé) et te dire ce qu'il veut : créer un sujet à partir d'un cours,
reprendre fidèlement un sujet existant, transformer des questions (ouvertes → QCM…), en ajouter, en retirer, changer le barème, adapter le niveau.

COMMENT RÉPONDRE
- En français, court et concret. Si un point ESSENTIEL manque (nombre de questions, types de questions, total de points) ET qu'on ne peut pas raisonnablement le déduire, pose UNE seule question courte. Sinon, produis le brouillon.
- DEUX INTENTIONS À NE JAMAIS CONFONDRE :
  1) REPRODUIRE / REPRENDRE / IMPORTER un fichier (mots : « reprends », « reproduis », « tel quel », « à l'identique », « importe », « garde ce sujet ») :
     écris exactement <<<REPRODUIRE n>>> (n = numéro du fichier « Fichier n ») sur sa propre ligne, rien d'autre dans le brouillon : le serveur insère alors le sujet et le barème exacts,
     déjà reconnus par la plateforme. Tu ne réécris jamais ce sujet toi-même.
  2) CRÉER de nouvelles questions « à partir de » fichiers (mots : « crée », « génère », « fais », « construis », « N questions », « différents types ») : les fichiers sont ta MATIÈRE
     (cours, ancien sujet, corrigé). Tu écris alors un NOUVEAU sujet complet, du nombre EXACT de questions demandé, sur les mêmes notions, avec la variété de types demandée
     (si on te demande « différents types » ou « tous les types » : répartis sur au moins 6 types parmi QCM, QCM à réponses multiples, vrai/faux, association, remise en ordre,
     texte à trous, champs à compléter, réponse courte, question ouverte). Tu peux reprendre des questions du fichier, mais tu dois atteindre le nombre demandé avec du contenu NOUVEAU.
     N'écris JAMAIS <<<REPRODUIRE>>> dans ce cas.
     Les questions du fichier ne sont qu'un MODÈLE de style et de contenu : n'en recopie AUCUNE mot pour mot (reformule, change les situations, les propositions, les exemples).
     Le TITRE (première ligne du cadre) est un titre court de ton cru (80 caractères maximum), jamais l'en-tête ni le nom du fichier.
     Le total des points vaut 20 sauf si l'enseignant en demande un autre : répartis les points pour que la somme soit exactement ce total.
- Pour créer ou modifier un sujet, donne TOUJOURS le sujet COMPLET (jamais seulement les changements), dans ce cadre exact :
<<<SUJET>>>
(titre sur la première ligne, puis les questions)
<<<BAREME>>>
(barème)
<<<FIN>>>
- Ne mets rien d'autre dans le cadre. Ton message d'explication va AVANT ou APRÈS le cadre.
- Quand l'enseignant parle d'un cours joint, ancre chaque question dans ce cours (aucune notion absente du document).

FORMAT DU SUJET (obligatoire) — questions numérotées 1, 2, 3… de façon CONTINUE sur tout le sujet, un titre de partie « Partie — … » optionnel :
  Question N — Titre court ............. (X pts) [TYPE]
  Énoncé de la question
  puis selon le type :
  [QCM] une seule bonne réponse        propositions « A) … » « B) … » « C) … » « D) … » (4 propositions courtes)
  [QCM_MULTI] plusieurs bonnes réponses  propositions A) … E) …
  [VF] vrai/faux                       une affirmation
  [OUVERT] réponse rédigée             (ajoute la ligne « Fichier : oui » si l'étudiant doit joindre un fichier)
  [SUBOPEN] sous-questions liées       propositions « a) … » = sous-questions
  [APPARIEMENT] paires                 « A. gauche → droite » ; ajoute « Tirage : N » pour tirer N paires au hasard par étudiant
  [ASSOCIATION] éléments « - notion » puis propositions « A. définition » (plus de propositions que d'éléments)
  [TRI] ligne « Catégories : X | Y » puis éléments « - élément »
  [ORDRE] éléments « A. … » donnés en désordre
  [TROUS] texte avec {1} {2}… puis propositions communes « A. mot »
  [CLOZE] texte avec {1} {2}… puis « - 1 COURT » (champ libre), « - 2 NUM » (nombre), « - 3 CHOIX : a | b | c » (liste)
  [COURT] réponse d'un mot            [NUMERIQUE] une valeur
  [CALCULEE] énoncé avec variables {a} {b} + ligne « Variables : a = 1..9 | b = 2..5 pas 0,5 » (valeurs différentes par étudiant)
  [QCM] calculé : mêmes variables + propositions « A. {={a}*{b}/100} F » (formules évaluées par étudiant)
  [CODE] programmation
FORMAT DU BARÈME : une entrée par question, mêmes numéros et mêmes points, puis une ligne de puce selon le type (écris la puce EXACTEMENT ainsi, sans nom de type ni guillemets) :
  Question N — Titre (X pts)
    • Bonne réponse : B) texte de la proposition                      (QCM)
    • Bonnes réponses : A), C)                                          (QCM_MULTI)
    • Réponse : Vrai     ou     • Réponse : Faux                        (VF)
    • Réponse : 1-C, 2-A, 3-B                                           (ASSOCIATION, TRI, TROUS : élément-lettre)
    • Ordre : C, A, B, D                                                (ORDRE)
    • Réponses acceptées : mot1 | mot2                                  (COURT)
    • Valeur : 12,5 (± 0,1)                                             (NUMERIQUE)
    • Champs : 1=Dakar|dakar ; 2=14 (± 0,5) ; 3=B                       (CLOZE)
    • Formule : a*b/100 (± 1)                                           (CALCULEE)
    • Critère : X pts — attendu précis   (une ou plusieurs lignes)      (OUVERT, CODE, SUBOPEN)
  Termine par « TOTAL : T / T points ». La somme des points des questions = T. Si l'enseignant ne dit pas le total, vise 20.
Les types demandés par l'enseignant sont ceux à produire (il peut en mélanger plusieurs). Respecte EXACTEMENT le nombre de questions demandé."""


def _history_text(messages, limit=10):
    lines = []
    for m in (messages or [])[-limit:]:
        who = 'ENSEIGNANT' if m.get('role') == 'user' else 'ASSISTANT'
        lines.append(f"{who} : {str(m.get('content', ''))[:6000]}")
    return '\n\n'.join(lines)


_BLOCK_RE = re.compile(r'<<<SUJET>>>\s*(.*?)\s*<<<BAREME>>>\s*(.*?)\s*<<<FIN>>>', re.S)
_REPRO_RE = re.compile(r'<<<REPRODUIRE\s+(\d+)\s*>>>')
_WANTS_REPRO_RE = re.compile(r"reprend|reprod|tel\s+quel|à\s+l['’]identique|identique|importe|garde\s+(?:ce|cet|le)\s+sujet|même\s+sujet|recopie", re.I)
_WANTS_COUNT_RE = re.compile(r'(\d{1,3})\s*(?:nouvelles\s+)?questions', re.I)


_BULLET_FIX_RE = re.compile(r'^(\s*[•\-*]\s*)(?:QCM_MULTI|QCM|VF|ASSOCIATION|TRI|TROUS|ORDRE|COURT|NUMERIQUE|CLOZE|CALCULEE|OUVERT|CODE|SUBOPEN)[ \t]*:[ \t]*[«"][ \t]*(.*?)[ \t]*[»"][ \t]*(.*)$', re.M)


def _norm(t):
    return re.sub(r'[^a-z0-9]+', ' ', (t or '').lower()).strip()


def _verbatim(content, recs):
    """Nombre de questions du brouillon dont l'énoncé est recopié mot pour mot dans un fichier joint."""
    from routes.exams import _parse_subject_blocks_ordered
    src = _norm(' '.join(r.get('annotated', '') for r in recs if not r.get('corrige')))
    n = 0
    for b in _parse_subject_blocks_ordered(content):
        t = _norm(b.get('text') or '')
        if b.get('type') not in ('section', 'description') and len(t) >= 25 and t in src:
            n += 1
    return n


def clean_rubric(rubric):
    """Garde-fou : retire « QCM : « … » » si l'IA recopie le nom du type et les guillemets — le correcteur automatique lit « Bonne réponse : B) »."""
    return _BULLET_FIX_RE.sub(lambda m: (m.group(1) + m.group(2) + (' ' + m.group(3) if m.group(3).strip() else '')).rstrip(), rubric or '')


def _report(content):
    from routes.exams import _parse_subject_blocks_ordered
    from services.subject_check import analyze
    from services.subject_count import count_units
    blocks = _parse_subject_blocks_ordered(content)
    total, by_type = count_units(blocks)
    rep = analyze(content)
    n_blocks = sum(1 for b in blocks if b.get('type') not in ('section', 'description', 'text'))
    return {'questions': total, 'blocks': n_blocks, 'by_type': by_type, 'ok': rep['ok'], 'issues': [i['message'] for i in rep['issues']][:6]}


def chat(user_id, messages, file_ids, context='', draft=None):
    from cache import cache_get
    from services.ai_service import call_ai
    from services.type_detector import detect_and_annotate
    from services.corrige_reader import merge, rubric_from_exercise_key

    recs = []
    for fid in (file_ids or [])[:_MAX_FILES]:
        r = cache_get(_key(user_id, fid))
        if r:
            recs.append(r)
    parts, used = [], 0
    for i, r in enumerate(recs, 1):
        body = r['annotated'][:max(0, _MAX_CHARS_TOTAL - used)]
        used += len(body)
        tag = 'CORRIGÉ (réponses officielles)' if r.get('corrige') else 'déjà converti au format CEI : exact'
        parts.append(f"=== Fichier {i} : {r['name']} — {tag} ===\n{body}")
    user = ''
    if context:
        user += f"CONTEXTE DE L'ENSEIGNANT : {str(context)[:800]}\n\n"
    if parts:
        user += '\n\n'.join(parts) + '\n\n'
    if draft and str(draft.get('content', '')).strip():
        user += ("BROUILLON ACTUEL (à modifier si l'enseignant le demande ; redonne-le COMPLET) :\n<<<SUJET>>>\n"
                 f"{draft.get('title', '')}\n{str(draft['content'])[:40000]}\n<<<BAREME>>>\n{str(draft.get('rubric', ''))[:20000]}\n<<<FIN>>>\n\n")
    user += "CONVERSATION :\n" + _history_text(messages)

    last = messages[-1].get('content', '') if messages else ''
    raw = call_ai(_SYSTEM, user, temperature=0.3, max_tokens=8000, label='subject-chat') or ''

    # Garde-fou 1 : l'IA a répondu « reproduire » alors que l'enseignant demande de CRÉER des questions
    if _REPRO_RE.search(raw) and not _WANTS_REPRO_RE.search(last):
        raw = call_ai(_SYSTEM, user + "\n\nCORRECTION : l'enseignant demande de CRÉER de nouvelles questions à partir des fichiers, pas de les reproduire. "
                      "Écris maintenant le sujet COMPLET dans le cadre <<<SUJET>>> / <<<BAREME>>> / <<<FIN>>>, avec le nombre exact de questions demandé.",
                      temperature=0.3, max_tokens=8000, label='subject-chat') or raw

    # Garde-fou 1 bis : création demandée, mais le brouillon recopie les questions du fichier
    if not _WANTS_REPRO_RE.search(last) and recs and _BLOCK_RE.search(raw):
        try:
            _b0 = _BLOCK_RE.search(raw).group(1)
            _c0 = _b0 if re.match(r'^\s*(?:Question\s+)?\d', _b0) else _b0.partition('\n')[2]
            _v = _verbatim(detect_and_annotate(_c0)[0], recs)
            _tot = max(1, _report(detect_and_annotate(_c0)[0])['blocks'])
            if _v / _tot > 0.25:
                raw = call_ai(_SYSTEM, user + f"\n\nCORRECTION : {_v} question(s) de ton brouillon sont recopiées mot pour mot du fichier. L'enseignant veut de NOUVELLES questions : "
                              "réécris le sujet COMPLET avec des énoncés, situations et propositions NOUVEAUX (mêmes notions, formulations différentes), même nombre de questions.",
                              temperature=0.5, max_tokens=8000, label='subject-chat') or raw
        except Exception:
            pass

    # Garde-fou 2 : nombre de questions demandé (« 30 questions ») non respecté → une relance chiffrée
    want_n = None
    if not _WANTS_REPRO_RE.search(last):
        mn = _WANTS_COUNT_RE.search(last)
        want_n = int(mn.group(1)) if mn and 2 <= int(mn.group(1)) <= 150 else None
    if want_n and _BLOCK_RE.search(raw):
        try:
            c0 = detect_and_annotate(_BLOCK_RE.search(raw).group(1).partition('\n')[2] if not re.match(r'^\s*Question\s+\d', _BLOCK_RE.search(raw).group(1)) else _BLOCK_RE.search(raw).group(1))[0]
            r0 = _report(c0)
            if want_n not in (r0['blocks'], r0['questions']):
                raw = call_ai(_SYSTEM, user + f"\n\nTON BROUILLON PRÉCÉDENT CONTIENT {r0['blocks']} question(s) alors que l'enseignant en demande EXACTEMENT {want_n}. "
                              f"Réécris le sujet COMPLET et son barème avec exactement {want_n} questions numérotées de 1 à {want_n}.",
                              temperature=0.3, max_tokens=8000, label='subject-chat') or raw
        except Exception:
            pass

    # 1) reproduction exacte demandée
    m = _REPRO_RE.search(raw)
    if m:
        idx = int(m.group(1)) - 1
        if 0 <= idx < len(recs) and not recs[idx].get('corrige'):
            content = recs[idx]['annotated']
            keys = [r['key'] for r in recs if r.get('corrige') and r.get('key')]
            rubric = ''
            if keys:
                merged = merge([{'qcm': k['qcm'], 'vf': k['vf'], 'assoc': {int(a): b for a, b in k['assoc'].items()}, 'trous': k['trous'],
                                 'cw': {(a[0], int(a[1:])): v for a, v in k['cw'].items()}, 'warnings': []} for k in keys])
                rubric, _w = rubric_from_exercise_key(content, merged)
            reply = _REPRO_RE.sub('', raw).strip() or "Voici le sujet reproduit tel quel."
            if not rubric:
                reply += "\n\n(Aucun barème n'a pu être écrit automatiquement : joignez le corrigé, ou dites-moi les bonnes réponses.)"
            return {'reply': reply, 'draft': {'title': (re.sub(r'\.[A-Za-z0-9]+$', '', recs[idx]['name'])), 'content': content, 'rubric': rubric, 'report': _report(content)}}

    # 2) brouillon écrit par l'IA
    b = _BLOCK_RE.search(raw)
    if not b:
        return {'reply': raw.strip(), 'draft': None}
    reply = (raw[:b.start()] + ' ' + raw[b.end():]).strip()
    body, rubric = b.group(1).strip(), b.group(2).strip()
    rubric = clean_rubric(rubric)
    title, _, content = body.partition('\n')
    if re.match(r'^\s*Question\s+\d', title):        # pas de titre : la première ligne est déjà une question
        title, content = '', body
    content, _rep = detect_and_annotate(content.strip())      # marqueurs de type garantis, numérotation continue
    return {'reply': reply or "Voici le brouillon.", 'draft': {'title': title.strip()[:200], 'content': content, 'rubric': rubric, 'report': _report(content)}}
