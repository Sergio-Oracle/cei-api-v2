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
        if not s or _CHOICE_RE.match(s) or _SECT_RE.match(s):
            continue
        m = _Q_RE.match(l)
        if m and m.group(4).strip():
            res.append((idx, int(m.group(2))))
    return res


def _renumber(lines):
    """Si la numérotation repart de 1 (un exercice = une série 1..N), la rend continue 1..N sur tout le sujet."""
    qs = _question_numbers(lines)
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


def normalize(content):
    """Retourne (contenu_normalisé, liste de modifications lisibles). Aucune écriture ailleurs."""
    if not content or not isinstance(content, str):
        return content, []
    lines = content.split('\n')
    notes = []
    lines, n = _vf_tables(lines)
    if n:
        notes.append(f"{n} ligne(s) de tableau Vrai/Faux converties en questions [VF]")
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
