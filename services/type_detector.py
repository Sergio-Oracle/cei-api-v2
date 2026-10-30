"""Détection automatique du type de CHAQUE question d'un document (Word, PDF) — sans rien demander à l'enseignant.

Incident du 30/10/2026 : selon les cases de types cochées à l'import, des QCM devenaient « ouverts » ; les tableaux
Vrai/Faux, les associations et les textes à trous d'un PDF n'étaient pas reconnus. Ici le type vient de la STRUCTURE
du document, jamais des cases :

  QCM            ≥ 2 propositions lettrées A. B. C…           QCM_MULTI : la consigne dit « plusieurs / toutes les bonnes réponses »
  VF             tableau « Vrai ☐ Faux ☐ », titre « Vrai ou faux », ou propositions Vrai / Faux
  ASSOCIATION    consigne « Associez… » + lignes « 1. notion  a. définition »
  CLOZE          énoncé avec des blancs (………, ____) : un champ par blanc
  COURT          réponse d'un mot (« (12 lettres) », mots croisés)
  OUVERT         tout le reste (développement, cas pratique)

  detect_and_annotate(content) -> (contenu_annoté, rapport)   idempotent ; une balise déjà posée par l'enseignant est respectée
                                                              sauf si elle contredit la structure (QCM balisé « ouvert »).
"""
import re

from services.subject_normalizer import (
    normalize, loose_mcq, _Q_RE, _SECT_RE, _CHOICE_RE, _question_numbers)

_MARK = r'QCM_MULTI|QCM|VF|OUVERT|SUBOPEN|APPARIEMENT|CODE|ASSOCIATION|TRI|ORDRE|TROUS|CLOZE|ZONES|COURT|NUMERIQUE|CALCULEE'
_MARK_RE = re.compile(r'\[(%s)\]' % _MARK, re.I)
_GAP_RE = re.compile(r'(?<!\w)[….]{3,}|[….]{5,}|_{3,}')           # « ………. » ; jamais « etc... » collé à un mot
_NOISE_RE = re.compile(r'^\s*[\d\s]{1,14}$')                                  # « 1 2 3 4 5 » : grille de réponse sans texte
_FOOTER_RE = re.compile(r'^.{4,140}\s·\s.{2,140}\bpage\s+\d+\s*$', re.I)       # « Matière · Épreuve · page 2 »
_RECAP_RE = re.compile(r'^\s*R[ée]capitulatif\s+de\s+la\s+note', re.I)
_DIRECTION_RE = re.compile(r'^\s*(?:Horizontalement|Verticalement)\s*:?\s*$', re.I)
_UPLOAD_CUE_RE = re.compile(r'(?:joignez|déposez|téléversez|transmettez|envoyez|rendez|uploadez)[^.\n]{0,50}(?:fichier|document|rapport|travail|projet|code|capture|schéma|tableur)', re.I)
_UPLOAD_LINE_RE = re.compile(r'^\s*(?:Fichier|Pi[èe]ce\s+jointe|D[ée]p[ôo]t)\s*:', re.I)
_HINT_RE = re.compile(r'\s*[☐□▢]\s*indice\s*$', re.I)
_MULTI_RE = re.compile(r'plusieurs\s+(?:bonnes?\s+)?r[ée]ponses|toutes\s+les\s+(?:bonnes?\s+)?r[ée]ponses|une\s+ou\s+plusieurs|cochez\s+toutes', re.I)
_VF_TITLE_RE = re.compile(r'vrai\s*(?:ou|/|-)\s*faux|\bV\s*/\s*F\b', re.I)
_ASSOC_ROW_RE = re.compile(r'^\s*(\d{1,2})[.)]\s+(.+?)\s+([a-f])[.)]\s+(.+)$')
_LETTER_SPLIT_RE = re.compile(r'\s([a-f])[.)]\s+')
_ORDER_RE = re.compile(r"(?:remett|class|rang|ordonn|plac|numérot)\w*[^.\n]{0,60}(?:dans\s+l['’]ordre|ordre\s+(?:chronologique|croissant|décroissant|correct|logique|alphabétique)|chronolog)|(?:ordre\s+chronologique|remise\s+en\s+ordre)", re.I)
_BANK_RE = re.compile(r'^\s*(?:Mots?|Liste\s+de\s+mots|Banque\s+de\s+mots|Propositions|Expressions?)(?:\s+(?:à|a)\s+(?:utiliser|placer|choisir|insérer))?\s*:\s*(.+)$', re.I)
_CATS_LINE_RE = re.compile(r'^\s*(?:Cat[ée]gories?|Colonnes?|Groupes?)(?:\s+suivantes?)?\s*:\s*(.+)$', re.I)
_BULLET_RE = re.compile(r'^\s*[-•*▪]\s+(.+)$')
_LETTERS_LEN_RE = re.compile(r'\(\s*\d{1,2}\s+lettres?\s*\)', re.I)


_DIR_HEAD_RE = re.compile(r'^\s*(Horizontalement|Verticalement)\s*:?\s*$', re.I)
_CLUE_RE = re.compile(r'^\s*(\d{1,2})[.)]\s+(.+)$')
_NLET_RE = re.compile(r'\(\s*(\d{1,2})\s+lettres?\s*\)', re.I)


def _collapse_crossword(lines, grids, report):
    """« Horizontalement / Verticalement » + définitions « N. … (K lettres) » + grille lue dans le PDF → UNE question [CLOZE]
    à grille (un champ par mot). N'agit que si numéros, directions ET longueurs concordent exactement avec la grille."""
    first = next((i for i, l in enumerate(lines) if _DIR_HEAD_RE.match(l)), None)
    if first is None:
        return lines
    clues, cur_dir, k, last = {}, None, first, first
    while k < len(lines):
        l = lines[k]
        if _DIR_HEAD_RE.match(l):
            cur_dir = 'H' if l.strip().lower().startswith('h') else 'V'
            k += 1; last = k; continue
        if _SECT_RE.match(l.strip()) or _RECAP_RE.match(l):
            break
        m = _CLUE_RE.match(l)
        if m and cur_dir:
            txt = m.group(2).strip()
            j = k + 1
            while j < len(lines) and lines[j].strip() and not _CLUE_RE.match(lines[j]) and not _DIR_HEAD_RE.match(lines[j]) \
                    and not _SECT_RE.match(lines[j].strip()) and not _RECAP_RE.match(lines[j]):
                txt += ' ' + lines[j].strip(); j += 1
            clues[(cur_dir, int(m.group(1)))] = txt
            k = j; last = k; continue
        if not l.strip():
            k += 1; continue
        break
    if not clues:
        return lines
    for g in grids or []:
        words = {(w['dir'], w['num']): w for w in g['words']}
        if set(words) != set(clues):
            continue
        if any((m := _NLET_RE.search(clues[key])) and int(m.group(1)) != words[key]['len'] for key in clues):
            continue
        order = sorted(words, key=lambda kk: (0 if kk[0] == 'H' else 1, kk[1]))
        spec = ' ; '.join(f"{i + 1}:{words[kk]['num']}:{kk[0]}:{words[kk]['row']}:{words[kk]['col']}:{words[kk]['len']}"
                          for i, kk in enumerate(order))
        num = _first_free_number(lines[:first])
        block = [f"{num}. Complétez la grille de mots croisés à partir des définitions. [CLOZE]", f"Grille : {spec}"]
        block += [f"{'Horizontalement' if kk[0] == 'H' else 'Verticalement'} {kk[1]} — {_NLET_RE.sub(lambda mo: '(' + mo.group(1) + ' lettres)', clues[kk])}"
                  for kk in order]
        block += [f"- {i + 1} COURT" for i in range(len(order))]
        report['converted'].append(f"grille de mots croisés {g['rows']}×{g['cols']} ({len(order)} mots)")
        return lines[:first] + block + lines[last:]
    report['warnings'].append("Mots croisés : la grille du document n'a pas pu être reliée aux définitions (numéros ou longueurs différents) — "
                              "les définitions sont posées comme des réponses courtes.")
    return lines


def _first_free_number(before):
    nums = [n for _, n in _question_numbers(before)]
    return (max(nums) + 1) if nums else 1


def _is_q(l):
    s = l.strip()
    if not s or _CHOICE_RE.match(s) or _SECT_RE.match(s):
        return False
    m = _Q_RE.match(l)
    return bool(m and m.group(4).strip())


def _clean_noise(lines):
    """Pieds de page, grilles de chiffres seuls et cases « indice » : du bruit de mise en page, jamais du sujet."""
    out = []
    for l in lines:
        if _RECAP_RE.match(l):
            break           # récapitulatif de notation et pages d'indices : jamais destinés à l'étudiant
        if _DIRECTION_RE.match(l):
            continue
        if _FOOTER_RE.match(l) or (l.strip() and _NOISE_RE.match(l)):
            continue
        out.append(_HINT_RE.sub('', l))
    return out


def _collapse_associations(lines, report):
    """« Associez… » + lignes « N. notion  a. définition » (plusieurs lettres possibles sur une ligne) → UNE question [ASSOCIATION]."""
    out, i, n = [], 0, len(lines)
    while i < n:
        m = _ASSOC_ROW_RE.match(lines[i])
        if not m:
            out.append(lines[i]); i += 1; continue
        # remonte : la consigne doit parler d'association dans les 8 lignes précédentes
        recent = ' '.join(out[-8:])
        if not re.search(r'associ|relie|apparie|corresp', recent, re.I):
            out.append(lines[i]); i += 1; continue
        j, rows, defs = i, [], {}
        first_num = m.group(1)
        while j < n:
            mm = _ASSOC_ROW_RE.match(lines[j])
            if not mm:
                break
            notion = mm.group(2).strip()
            tail = ' '.join(filter(None, [mm.group(3) + '. ' + mm.group(4)]))
            parts = re.split(_LETTER_SPLIT_RE, ' ' + tail)       # ['', 'a', 'texte', 'e', 'texte', ...]
            for k in range(1, len(parts) - 1, 2):
                defs[parts[k].upper()] = parts[k + 1].strip()
            rows.append(notion)
            j += 1
        letters = sorted(defs)
        if len(rows) >= 2 and len(letters) >= 2 and letters[0] == 'A':
            out.append(f"{first_num}. Associez chaque notion à sa définition. [ASSOCIATION]")
            out += [f"- {r}" for r in rows]
            out += [f"{L}. {defs[L]}" for L in letters]
            report['converted'].append(f"association de {len(rows)} notions ({len(letters)} définitions)")
            i = j
        else:
            out.append(lines[i]); i += 1
    return out


def _blocks(lines):
    """[(index_début, index_fin_exclu)] de chaque question (jusqu'à la suivante ou au titre de partie suivant)."""
    starts = [k for k, l in enumerate(lines) if _is_q(l)]
    res = []
    for idx, s in enumerate(starts):
        e = starts[idx + 1] if idx + 1 < len(starts) else len(lines)
        for k in range(s + 1, e):
            if _SECT_RE.match(lines[k].strip()):
                e = k
                break
        res.append((s, e))
    return res


def _section_context(lines):
    """Pour chaque ligne : (titre, consigne) de la partie qui la contient."""
    ctx, title, consigne = [], '', ''
    seen_q = False
    for l in lines:
        if _SECT_RE.match(l.strip()):
            title, consigne, seen_q = l.strip(), '', False
        elif _is_q(l):
            seen_q = True
        elif not seen_q and l.strip():
            consigne += ' ' + l.strip()
        ctx.append((title, consigne))
    return ctx


def detect_and_annotate(content, crosswords=None):
    report = {'counts': {}, 'converted': [], 'warnings': [], 'changed': 0}
    if not content or not isinstance(content, str):
        return content, report
    lines = content.split('\n')
    lines = _collapse_crossword(lines, crosswords, report)   # avant le nettoyage : il retire « Horizontalement / Verticalement »
    lines = _clean_noise(lines)
    lines = _collapse_associations(lines, report)
    content2, _notes = normalize('\n'.join(lines))             # tableaux Vrai/Faux, numérotation continue
    lines = content2.split('\n')
    ctx = _section_context(lines)

    for (s, e) in reversed(_blocks(lines)):          # de la fin vers le début : les remplacements ne décalent pas les blocs à venir
        head = lines[s]
        title, consigne = ctx[s]
        body = lines[s + 1:e]
        choices = [l for l in body if _CHOICE_RE.match(l.strip())]
        has_marker = bool(_MARK_RE.search(head))
        cur = _MARK_RE.search(head).group(1).upper() if has_marker else None
        text_all = ' '.join([head] + [b for b in body if b.strip()])
        want = None
        if cur in ('ASSOCIATION', 'TRI', 'ORDRE', 'TROUS', 'CLOZE', 'ZONES', 'COURT', 'NUMERIQUE', 'CALCULEE', 'VF', 'APPARIEMENT', 'CODE', 'SUBOPEN') \
                and not (cur == 'VF' and len(choices) >= 2 and not _VF_TITLE_RE.search(text_all + title)):
            want = cur                                         # structure déjà exprimée : respectée
        elif len(choices) >= 2 and len(_GAP_RE.findall(text_all)) >= 2:
            want = 'TROUS'                                      # texte à plusieurs trous + liste de mots lettrée (gapselect / ddwtos)
        elif len(choices) >= 3 and _ORDER_RE.search(consigne + ' ' + text_all):
            want = 'ORDRE'                                      # « remettez dans l'ordre » (ordering)
        elif len(choices) >= 2:
            want = 'QCM_MULTI' if _MULTI_RE.search(consigne + ' ' + text_all) else 'QCM'
        elif any(_BANK_RE.match(b) for b in body) and _GAP_RE.search(text_all):
            want = 'TROUS'                                      # trous + « Mots : a, b, c » (banque de mots)
        elif any(_CATS_LINE_RE.match(b) for b in body) and sum(1 for b in body if _BULLET_RE.match(b)) >= 2:
            want = 'TRI'                                        # classement dans des catégories
        elif _VF_TITLE_RE.search(title) or _VF_TITLE_RE.search(head):
            want = 'VF'
        elif _GAP_RE.search(text_all) and not _LETTERS_LEN_RE.search(text_all):
            want = 'CLOZE'
        elif _LETTERS_LEN_RE.search(text_all):
            want = 'COURT'
        else:
            want = cur or 'OUVERT'
        if want == 'TROUS' and cur != 'TROUS':
            k = [0]
            def _fill_t(mo):
                k[0] += 1
                return '{%d}' % k[0]
            new_head = _GAP_RE.sub(_fill_t, head)
            new_body, opts = [], [b for b in body if _CHOICE_RE.match(b.strip())]
            for b in body:
                if not b.strip():
                    continue
                bm = _BANK_RE.match(b)
                if bm:
                    words = [w.strip() for w in re.split(r'\s*[;,/·|]\s*', bm.group(1)) if w.strip()]
                    opts = [f"{chr(65 + i)}. {w}" for i, w in enumerate(words)]
                    continue
                if _CHOICE_RE.match(b.strip()):
                    continue
                new_body.append(_GAP_RE.sub(_fill_t, b))
            if k[0] and len(opts) >= 2:
                lines[s:e] = [_MARK_RE.sub('', new_head).rstrip() + ' [TROUS]'] + new_body + opts
                report['converted'].append(f"texte à trous avec liste de mots : {k[0]} trou(s), {len(opts)} mots")
                continue
            want = 'QCM'
        if want == 'TRI' and cur != 'TRI':
            cats = next((_CATS_LINE_RE.match(b) for b in body if _CATS_LINE_RE.match(b)), None)
            items = [b.strip() for b in body if _BULLET_RE.match(b)]
            names = [c.strip() for c in re.split(r'\s*[|;,/]\s*', cats.group(1)) if c.strip()] if cats else []
            if len(names) >= 2 and len(items) >= 2:
                rest = [b for b in body if b.strip() and not _CATS_LINE_RE.match(b) and not _BULLET_RE.match(b)]
                lines[s:e] = [_MARK_RE.sub('', head).rstrip() + ' [TRI]'] + rest + ['Catégories : ' + ' | '.join(names)] + items
                report['converted'].append(f"classement : {len(items)} éléments, {len(names)} catégories")
                continue
            want = 'OUVERT'
        if want == 'CLOZE' and cur != 'CLOZE':
            k = [0]
            def _fill(mo):
                k[0] += 1
                return '{%d}' % k[0]
            new_head = _GAP_RE.sub(_fill, head)
            new_body = [(_GAP_RE.sub(_fill, b)) for b in body if b.strip()]
            fields = [f"- {i} COURT" for i in range(1, k[0] + 1)]
            if not fields:
                want = 'OUVERT'
            else:
                lines[s:e] = [_MARK_RE.sub('', new_head).rstrip() + ' [CLOZE]'] + new_body + fields
                report['converted'].append(f"texte à trous : {k[0]} champ(s)")
                continue
        if cur != want:
            lines[s] = (_MARK_RE.sub('', head).rstrip() + f' [{want}]')
            report['changed'] += 1
        if want in ('OUVERT', 'CODE', 'SUBOPEN') and _UPLOAD_CUE_RE.search(text_all) and not any(_UPLOAD_LINE_RE.match(b) for b in body):
            lines.insert(s + 1, 'Fichier : oui')                       # « joignez / déposez votre fichier » → réponse rédigée + fichier joint
            report['converted'].append(f"question {head.split('.')[0].strip()[:3]} : dépôt de fichier")
    out = '\n'.join(lines)
    from routes.exams import _parse_subject_blocks_ordered
    for b in _parse_subject_blocks_ordered(out):
        if b['type'] != 'section':
            report['counts'][b['type']] = report['counts'].get(b['type'], 0) + 1
    if _LETTERS_LEN_RE.search(out) and re.search(r'mots?\s+crois', out, re.I) and not any('grille de mots' in c for c in report['converted']):
        report['warnings'].append("Mots croisés : la grille dessinée n'existe pas dans le texte du document — les définitions sont posées "
                                  "comme des réponses courtes ; ajoutez la grille en image si elle est nécessaire.")
    return out, report
