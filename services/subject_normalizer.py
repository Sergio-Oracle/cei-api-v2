"""Remise au format CEI d'un sujet issu d'un document (PDF, Word) AVANT son enregistrement.

Incident du 30/10/2026 (examen 142) : un sujet extrait d'un PDF contenait un exercice « Vrai ou faux » en tableau
(« 1 Affirmation ☐ ☐ ») et une numérotation qui repart de 1 à chaque exercice. Le lecteur de questions ne voyait
aucune des affirmations (page vide pour l'étudiant) et, comme les réponses sont rangées par numéro, les questions de
même numéro d'exercices différents s'écrasaient entre elles (réponses mélangées, notation faussée).

  normalize(content) -> (contenu, [modifications])   idempotent : un sujet déjà au bon format ressort inchangé
  install()                                           branche la remise au format sur tout enregistrement de sujet
"""
import re

_BOX = '☐□▢◻⬜'
# Ligne d'un tableau Vrai/Faux : « 3 En première année, … ☐ ☐ »
_VF_ROW_RE = re.compile(r'^\s*(\d{1,2})\s+(.+?)\s*[%s]\s*[%s]\s*$' % (_BOX, _BOX))
_VF_HEAD_RE = re.compile(r'^\s*N°?\s+Affirmations?\s+Vrai\s+Faux\s*$', re.I)
# Début de question reconnu par les lecteurs : « 3. texte », « 3) texte », « Question 3 : texte »
_Q_RE = re.compile(r'^(\s*(?:(?:Question|Q)\.?\s+)?)(\d{1,3})(\s*[.:)–—-]\s*|\s+(?=\[))(.*)$', re.I)
_CHOICE_RE = re.compile(r'^\s*\(?[A-Fa-f]\)?\s*[.):\s-]\s+')
_SECT_RE = re.compile(r'^\s*(?:Partie|Section|Exercice|Part)\s+(?:[IVX]+\b|\d+\b|[-–—:])', re.I)
_SECT_LETTER_RE = re.compile(r"^\s*(?:Partie|Section|Exercice|Part)\s+[A-Z]\b(?![’'a-zà-ÿ])")   # « Partie A », jamais « Partie de … »
# « 6. Exercice 2 : Logique du premier ordre Prédicats : … Q6. Pourquoi … [QCM] » : le TITRE de la partie suivante est collé au début de la question
# (numéro identique devant « Qn. », forme produite par l'import Word/PDF). Le lecteur ne reconnaît un titre que s'il commence sa ligne : ici il
# restait invisible et l'énoncé de l'exercice PRÉCÉDENT s'affichait sur toutes les questions suivantes (Devoir-IA-Master IL, 13/11/2026).
_GLUED_SECT_RE = re.compile(
    r"^(\d{1,3})\.\s+((?i:Partie|Section|Exercice|Part)\s+(?:[IVX]+\b|\d+\b|[A-Z]\b(?![’'a-zà-ÿ])).*?)\s+(?:Q|Question)\s*\1\s*[.:)]\s+(.*)$")
_SECT_LABEL_RE = re.compile(r"^((?i:Partie|Section|Exercice|Part)\s+\S+?)\s*(?:[:\-–—.]\s*|\s+)(.*)$")
_STATEMENT_LABEL_RE = re.compile(r"(?:^|\s)(\S+)\s*:\s")


def _split_section_head(head):
    """« Exercice 2 : Logique du premier ordre Prédicats : étudiant(x)… » -> (« Exercice 2 : Logique du premier ordre », « Prédicats : étudiant(x)… »).
    Le sous-titre s'arrête avant le mot qui précède le deuxième « : » (« Prédicats : », « Données : », « Rappel : »). Sans deuxième « : » : un
    texte court sans ponctuation finale est un TITRE (« Exercice 3 : Graphes conceptuels »), un texte long est un énoncé sous « Exercice N ».
    Rien n'est jamais perdu ni inventé : titre + énoncé = texte d'origine, séparateurs d'origine conservés."""
    head = head.strip()
    m = _SECT_LABEL_RE.match(head)
    if not m:
        return head, ''
    start = m.start(2)
    rest = head[start:]
    if not rest.strip():
        return head.rstrip(' :-–—.'), ''
    lab = _STATEMENT_LABEL_RE.search(rest)
    if lab and lab.start(1) > 0:
        return head[:start + lab.start(1)].strip(), rest[lab.start(1):].strip()
    if len(rest) <= 70 and not re.search(r'[.!?»)]\s*$|\.\s', rest):
        return head, ''
    return head[:start].strip(' :-–—.'), rest.strip()


def _unglue_heading(lines):
    """Titre de partie collé à une question → titre, énoncé et question sur leurs propres lignes (voir _GLUED_SECT_RE)."""
    out, changed = [], 0
    for l in lines:
        m = _GLUED_SECT_RE.match(l)
        if m:
            title, stmt = _split_section_head(m.group(2))
            out += [title] + ([stmt] if stmt else []) + ['', f"{m.group(1)}. Q{m.group(1)}. {m.group(3)}"]
            changed += 1
        else:
            out.append(l)
    return out, changed


_GLUED_Q_RE = re.compile(r'^(.*\S)\s+((\d{1,3})\.\s+(?:Q|Question)\s*(\d{1,3})\s*[.:)]\s.*)$')


def _vf_tables(lines):
    """« N affirmation ☐ ☐ » → « N. affirmation [VF] » ; l'en-tête « N° Affirmation Vrai Faux » disparaît."""
    out, changed = [], 0
    for l in lines:
        if _VF_HEAD_RE.match(l):
            changed += 1
            continue
        m = _VF_ROW_RE.match(l)
        if m:
            out.append(f'{m.group(1)}. {m.group(2).strip()} [VF]')
            changed += 1
        else:
            out.append(l)
    return out, changed


def _question_numbers(lines):
    """Indices et numéros des débuts de question (hors propositions A. B. C.)."""
    res = []
    for idx, l in enumerate(lines):
        s = l.strip()
        if not s or _CHOICE_RE.match(s) or _SECT_RE.match(s) or _SECT_LETTER_RE.match(s):
            continue
        m = _Q_RE.match(l)
        if m and m.group(4).strip():
            res.append((idx, int(m.group(2))))
    return res


def _stray_number_line(line):
    """Ligne qui COMMENCE par un nombre sans être une question : le caractère qui suit le séparateur est un chiffre ou une ponctuation
    (« 001-2023-CM-UEMOA relative… », « 802.11ax Wi-Fi 6 », « 308). Grenoble : … ») ; une vraie question est suivie d'un espace ou d'une lettre
    (« 3. Quelle… », « 3.Quelle… »). Ces lignes (références, pages, normes) ne sont JAMAIS renumérotées : incident du 13/11/2026, le texte d'un sujet
    était réécrit (« 001-2023-CM… » devenait « 7-2023-CM… »)."""
    m = _Q_RE.match(line)
    if not m:
        return False
    rest = m.group(4)
    if not rest:
        return False
    c = rest[0]
    return not (c.isspace() or c.isalpha() or c in "«\"'’“(\[")


_TYPE_MARK_RE = re.compile(r'\[(?:QCM_MULTI|QCM|VF|OUVERT|SUBOPEN|APPARIEMENT|CODE|ASSOCIATION|TRI|ORDRE|TROUS|COURT|NUMERIQUE|CLOZE|CALCULEE|ZONES)\]', re.I)


def _real_question_start(lines, idx):
    """Vraie question : marqueur de type [QCM]… sur la ligne, ou au moins deux propositions « A. » « B. » juste après."""
    if _TYPE_MARK_RE.search(lines[idx]):
        return True
    near = [x.strip() for x in lines[idx + 1: idx + 12] if x.strip()][:8]
    return sum(1 for x in near if _CHOICE_RE.match(x)) >= 2


def _restarts_follow_titles(lines, qs):
    """Au moins deux reprises à 1 de la numérotation, et au moins la moitié d'entre elles, sont précédées d'un titre de partie (Exercice N, Partie B…).
    Un sujet par exercices peut garder une dernière grille sans titre (mots croisés) : d'où « la moitié » et non « toutes »."""
    restarts = [i for i, (_, n) in enumerate(qs) if i > 0 and n == 1]
    if len(restarts) < 2:
        return False
    titled = 0
    for i in restarts:
        between = lines[qs[i - 1][0] + 1: qs[i][0]]
        if any(_SECT_RE.match(l) or _SECT_LETTER_RE.match(l) for l in between):
            titled += 1
    return titled >= 2 and titled * 2 >= len(restarts)


def _renumber(lines):
    """Si la numérotation repart de 1 (un exercice = une série 1..N), la rend continue 1..N sur tout le sujet.
    Les lignes numérotées « de texte » (voir _stray_number_line) sont écartées : ni renumérotées, ni comptées."""
    # Les lignes numérotées « de texte » (numérotation hiérarchique « Question 1.1 », référence « 001-2023-… », norme « 802.11ax », titre « 1). Les origines »)
    # sont écartées : ni comptées, ni jamais réécrites. Seules les vraies questions sont renumérotées.
    allq = _question_numbers(lines)
    qs = [(idx, n) for idx, n in allq if not _stray_number_line(lines[idx])]
    if not qs:
        return lines, 0
    mostly_real = sum(1 for idx, _ in qs if _real_question_start(lines, idx)) >= 0.6 * len(qs)      # marqueur [QCM]… ou propositions A. B. qui suivent
    if len(qs) != len(allq):
        # Des lignes douteuses sont présentes : il faut une PREUVE positive que c'est un sujet d'examen (majorité de vraies questions).
        # Sinon (document de cours, bibliographie, numérotation hiérarchique) on ne touche à rien.
        if not mostly_real:
            return lines, 0
    elif not (mostly_real or _restarts_follow_titles(lines, qs)):
        # Aucune ligne douteuse : un sujet par exercices (chaque reprise de la numérotation suit un titre « Exercice N ») ou une majorité de vraies questions.
        return lines, 0
    nums = [n for _, n in qs]
    # Seulement une numérotation qui repart de 1 plusieurs fois (un exercice = une série) : jamais un simple doublon isolé.
    restarts = sum(1 for i, n in enumerate(nums) if i > 0 and n == 1)
    if len(nums) < 2 or len(set(nums)) == len(nums) or restarts < 2:
        return lines, 0
    out = list(lines)
    for new, (idx, old) in enumerate(qs, 1):
        m = _Q_RE.match(out[idx])
        out[idx] = f'{m.group(1)}{new}{m.group(3)}{m.group(4)}'
    return out, len(qs)


_LOOSE_CHOICE_RE = re.compile(r'^\s*\(?([A-Fa-f])[.)]\s+(.+?)\s*$')
_ANSWER_RE = re.compile(r'^\s*(?:Bonne\s+)?R[ée]ponse(?:\s+(?:correcte|attendue|exacte))?\s*:?\s*\(?([A-Fa-f])\)?\s*[.)]?\s*$', re.I)
_FILE_HEAD_RE = re.compile(r'^\s*---\s*(?:Fichier|Corrigé)\s*:.*---\s*$')


def loose_mcq(content, start=1):
    """Sujet de Word/PDF sans numéros de question : « Énoncé / A. … B. … / Bonne réponse : d » répété.
    Incident du 30/10/2026 : tout apparaissait comme une zone de réponse libre, et la ligne « Bonne réponse : d »
    était montrée à l'étudiant. Retourne (contenu au format CEI, {numéro: lettre}, nombre de questions) ; inchangé s'il y a déjà des questions numérotées."""
    if not content or _question_numbers(content.split('\n')):
        return content, {}, 0
    lines = content.split('\n')
    out, key, stem, n = [], {}, [], start - 1
    i = 0
    while i < len(lines):
        l = lines[i]
        if _FILE_HEAD_RE.match(l):
            out.append(l); i += 1; continue
        m = _LOOSE_CHOICE_RE.match(l)
        if m and (i == 0 or not _LOOSE_CHOICE_RE.match(lines[i - 1])):
            run = []
            j = i
            while j < len(lines) and _LOOSE_CHOICE_RE.match(lines[j]):
                run.append(_LOOSE_CHOICE_RE.match(lines[j])); j += 1
            letters = [r.group(1) for r in run]
            in_order = [x.upper() for x in letters] == [chr(65 + t) for t in range(len(letters))]   # A, B, C… dans l'ordre
            k0 = j
            while k0 < len(lines) and not lines[k0].strip():
                k0 += 1
            has_answer = k0 < len(lines) and bool(_ANSWER_RE.match(lines[k0]))
            # a) b) en minuscules sans réponse donnée = sous-questions d'un exercice, pas des propositions
            if len(run) >= 2 and stem and in_order and (letters[0].isupper() or has_answer):
                n += 1
                out.append(f"{n}. {' '.join(x.strip() for x in stem)} [QCM]")
                out += [f"{r.group(1).upper()}. {r.group(2)}" for r in run]
                stem = []
                k = j
                while k < len(lines) and not lines[k].strip():
                    k += 1
                am = _ANSWER_RE.match(lines[k]) if k < len(lines) else None
                if am:
                    key[n] = am.group(1).upper()
                    j = k + 1
                out.append('')
                i = j
                continue
        if l.strip():
            stem.append(l)
        else:
            if stem:                       # ligne vide : le texte en attente n'était pas un énoncé suivi de propositions
                out += stem; out.append('')
                stem = []
            else:
                out.append(l)
        i += 1
    out += stem
    if n < start:
        return content, {}, 0
    return '\n'.join(out), key, n - start + 1


def rubric_from_key(content, key, total_points=20):
    """Barème (format CEI) d'un QCM dont la clé est connue : « Question N (x pts) : • Bonne réponse : D) texte »."""
    from routes.exams import _parse_subject_blocks_ordered
    qs = [b for b in _parse_subject_blocks_ordered(content) if b['type'] == 'qcm']
    if not qs or any(int(b['num']) not in key for b in qs):
        return ''
    per = round(total_points / len(qs), 2)
    per_s = str(int(per)) if float(per).is_integer() else str(per).replace('.', ',')
    out = ['=== BARÈME DE NOTATION (écrit depuis les bonnes réponses du sujet) ===', '']
    for b in qs:
        L = key[int(b['num'])]
        txt = next((c['text'] for c in b['choices'] if c['letter'] == L), '')
        out += [f"Question {b['num']} ({per_s} pts) :", f"  • Bonne réponse : {L}) {txt}", '']
    out += ['──────────────────────────', f'TOTAL : {total_points} / {total_points} points']
    return '\n'.join(out)


def _unglue(lines):
    """« énoncé… » + « 7. Q7. question » sur une même ligne → deux lignes. Le lecteur ne voit une question que si elle commence sa ligne :
    collée à la fin d'un énoncé, elle disparaissait (incident du 02/11/2026, Devoir2-IA : Q1, Q7 et Q15 absentes, énoncés des parties perdus).
    Reconnu seulement quand le numéro devant « Qn. » est le même que n (forme produite par loose_mcq), donc jamais sur du texte ordinaire."""
    out, changed = [], 0
    for l in lines:
        m = _GLUED_Q_RE.match(l)
        if m and m.group(3) == m.group(4) and not _Q_RE.match(m.group(1)) and re.search(r'[.»)!?:;”"]$', m.group(1)):
            out += [m.group(1), '', m.group(2)]
            changed += 1
        else:
            out.append(l)
    return out, changed


def normalize(content):
    """Retourne (contenu_normalisé, liste de modifications lisibles). Aucune écriture ailleurs."""
    if not content or not isinstance(content, str):
        return content, []
    lines = content.split('\n')
    notes = []
    lines, n = _vf_tables(lines)
    if n:
        notes.append(f"{n} ligne(s) de tableau Vrai/Faux converties en questions [VF]")
    lines, n = _unglue_heading(lines)
    if n:
        notes.append(f"{n} titre(s) de partie collé(s) à une question remis sur leur propre ligne")
    lines, n = _unglue(lines)
    if n:
        notes.append(f"{n} question(s) recollée(s) à la fin d'un énoncé remises sur leur propre ligne")
    lines, n = _renumber(lines)
    if n:
        notes.append(f"numérotation rendue continue (1 à {n}) : les numéros repartaient de 1 d'un exercice à l'autre")
    return '\n'.join(lines), notes


def _before_save(mapper, connection, target):
    new, notes = normalize(target.content)
    if notes:
        target.content = new


def install():
    """À appeler une fois au démarrage : tout sujet créé ou modifié est remis au format avant enregistrement."""
    from sqlalchemy import event
    from models import Subject
    event.listen(Subject, 'before_insert', _before_save)
    event.listen(Subject, 'before_update', _before_save)
