"""Normalisation d'une épreuve importée (PDF/Word) vers le langage de balises des sujets CEI.

Une épreuve « papier » est découpée en parties numérotées en chiffres romains avec leurs points
(« I. Questionnaire à choix multiples (5 points) », « II. Vrai ou faux (4 points) »…), dont la
numérotation des questions repart de 1 à chaque partie et dont les tableaux (vrai/faux, tri,
association) sortent de l'extraction sous forme de lignes cassées. CEI a besoin d'une numérotation
unique et de balises de type : ce module fait la conversion, partie par partie, selon le titre.

    texte_normalisé, rapport = normalize_exam_text(texte)

Si le document n'a pas cette structure (moins de deux parties reconnues), il est rendu inchangé :
le comportement historique (balisage simple par annotate_markers) reste celui des autres sujets.
Le rapport liste ce qui a été reconnu et les points d'attention, à montrer à l'enseignant.
"""
import re

_GLYPHS = re.compile(r'[☐□☑☒✓✔■▢◻◯○]')
_FOOTER = re.compile(r'^UN-CHK\b.*\bPage\s+\d+\s*/\s*\d+\s*$', re.I)
_SECTION = re.compile(
    r'^(?P<rom>[IVX]{1,5})\.\s+(?P<title>[^(]{4,}?)\s*\(\s*(?P<pts>\d+(?:[.,]\d+)?)\s*(?:points?|pts?)\s*\)\s*$', re.I)
_PER_ITEM = re.compile(r'\(\s*(?P<n>\d+)\s+[A-Za-zéèêàùç\' ]+?\s*,\s*(?P<pts>\d+(?:[.,]\d+)?)\s*points?\s*chacune\s*\)', re.I)
_END_SENT = re.compile(r'[.:;?!»)]$')


def _num(s) -> float:
    return float(str(s).replace(',', '.'))


def _fmt(v: float) -> str:
    v = round(v + 1e-9, 2)
    return (str(int(v)) if v == int(v) else f'{v:g}').replace('.', ',')


def _clean(raw: str) -> list:
    out = []
    for ln in raw.replace('\r', '').split('\n'):
        ln = _GLYPHS.sub('', ln).replace(' ', ' ')
        ln = re.sub(r'[ \t]+', ' ', ln).strip()
        if not ln or _FOOTER.match(ln):
            continue
        out.append(ln)
    return out


def _join(lines: list) -> str:
    return re.sub(r'\s+', ' ', ' '.join(lines)).strip()


def _paragraphs(lines: list) -> list:
    """Recolle les lignes coupées par l'extraction : une ligne qui ne finit pas une phrase continue."""
    out = []
    for ln in lines:
        if out and not _END_SENT.search(out[-1]):
            out[-1] += ' ' + ln
        else:
            out.append(ln)
    return out


def _kind(title: str) -> str:
    t = title.lower()
    if 'vrai' in t and 'faux' in t:
        return 'vf'
    if 'association' in t or 'appariement' in t:
        return 'association'
    if re.search(r'\btri\b|classement|catégor', t):
        return 'tri'
    if 'choix multiple' in t or 'qcm' in t or 'qcu' in t:
        return 'qcm'
    if 'cas pratique' in t or 'ouvert' in t or 'dissertation' in t or 'commentaire' in t or 'rédaction' in t:
        return 'open'
    return 'other'


def _rows(lines: list, expected_first: int) -> tuple:
    """Lignes d'un tableau → (liste de textes, prochain numéro attendu). Une ligne commence une ligne de
    tableau si elle débute par le numéro attendu ; sinon elle prolonge la précédente (ou est un
    en-tête, ignoré tant qu'aucune ligne n'a commencé)."""
    rows, nxt = [], expected_first
    for ln in lines:
        m = re.match(r'^(\d{1,2})(?:\s+(.*))?$', ln)
        if m and int(m.group(1)) == nxt:
            rows.append((m.group(2) or '').strip())
            nxt += 1
        elif rows:
            rows[-1] = (rows[-1] + ' ' + ln).strip()
    return [re.sub(r'\s+', ' ', r).strip() for r in rows], nxt


def _consigne_split(body: list) -> tuple:
    """(consigne, reste) — la consigne commence par « Consigne. » et se termine à la parenthèse « (N …, X point chacune) »."""
    if not body or not body[0].lower().startswith('consigne'):
        return '', body
    acc = []
    for k, ln in enumerate(body):
        acc.append(ln)
        if _PER_ITEM.search(_join(acc)):
            return _join(acc), body[k + 1:]
        if k >= 4:
            break
    return _join(acc[:1]), body[1:]


def _qcm(body, per, g, rep):
    out, cur, ch = [], None, []
    nxt = 1

    def flush():
        nonlocal cur, ch
        if cur is not None:
            out.append(f"Question {cur[0]} — {cur[1]} ({_fmt(per)} pts) [QCM]")
            out.extend(f"{l}. {t}" for l, t in ch)
            if len(ch) < 2:
                rep['warnings'].append(f"QCM {cur[0]} : moins de 2 propositions reconnues")
        cur, ch = None, []

    for ln in body:
        m = re.match(r'^(\d{1,2})\.\s+(.*)$', ln)
        if m and int(m.group(1)) == nxt and (cur is None or ch):
            flush()
            nxt += 1
            g[0] += 1
            cur = (g[0], m.group(2).strip())
            continue
        c = re.match(r'^([A-F])\.\s+(.*)$', ln)
        if c and cur is not None:
            ch.append([c.group(1), c.group(2).strip()])
        elif cur is not None:
            if ch:
                ch[-1][1] = (ch[-1][1] + ' ' + ln).strip()
            else:
                cur = (cur[0], (cur[1] + ' ' + ln).strip())
    flush()
    rep['counts']['qcm'] = nxt - 1
    return out


def _vf(body, per, g, rep):
    rows, nxt = _rows(body, 1)
    out = []
    for r in rows:
        g[0] += 1
        out.append(f"Question {g[0]} — {r} ({_fmt(per)} pts) [VF]")
        out.append('Vrai / Faux')
    rep['counts']['vf'] = len(rows)
    return out


def _tri(body, per, g, rep):
    plateaux, cur = [], None
    for ln in body:
        m = re.match(r'^Plateau\s+(\d+)\.?\s*(.+?)\s*\??\s*$', ln, re.I)
        if m:
            cur = {'n': m.group(1), 'title': m.group(2).strip(' ?'), 'lines': []}
            plateaux.append(cur)
        elif cur is not None:
            cur['lines'].append(ln)
    out, nxt, total = [], 1, 0
    for p in plateaux:
        rows, nxt = _rows(p['lines'], nxt)
        cats = [c.strip(' ?').strip() for c in re.split(r'\s+ou\s+', p['title'], flags=re.I) if c.strip(' ?').strip()]
        cats = [c[:1].upper() + c[1:] for c in cats]
        if len(cats) < 2 or not rows:
            rep['warnings'].append(f"Plateau {p['n']} : catégories ou situations non reconnues")
            continue
        g[0] += 1
        total += len(rows)
        out.append(f"Question {g[0]} — Plateau {p['n']} : {p['title']} ? ({_fmt(per * len(rows))} pts) [TRI]")
        out.append('Catégories : ' + ' | '.join(cats))
        out.extend(f"- {r}" for r in rows)
    rep['counts']['tri'] = total
    return out


def _association(body, pts_total, n_expected, g, rep):
    skip = re.compile(r'^(Notions?\s+D[ée]finitions?|Vos\s+r[ée]ponses|(?:Notion\s+\d+\s*)+)$', re.I)
    flat = ' '.join(l for l in body if not skip.match(l))
    flat = re.sub(r'\s+', ' ', flat)
    pos, items, defs = 0, [], []
    n = 0
    while True:
        k = n + 1
        mn = re.compile(r'(?:(?<=\s)|^)%d\.\s' % k).search(flat, pos)
        if not mn:
            break
        letter = chr(64 + k)
        ml = re.compile(r'(?:(?<=\s)|^)%s\.\s' % letter).search(flat, mn.end())
        if not ml:
            break
        items.append((mn.start(), mn.end(), ml.start(), ml.end()))
        pos = ml.end()
        n += 1
        if n_expected and n >= n_expected:
            break
    if not items:
        rep['warnings'].append("Association : aucune notion reconnue")
        return []
    # propositions : une par notion, puis les lettres suivantes (leurres) sans numéro
    markers = [(it[0], 'N', i + 1) for i, it in enumerate(items)] + [(it[2], 'L', i + 1) for i, it in enumerate(items)]
    extra_letter = len(items) + 1
    tail_pos = items[-1][3]
    while extra_letter <= 12:
        ml = re.compile(r'(?:(?<=\s)|^)%s\.\s' % chr(64 + extra_letter)).search(flat, tail_pos)
        if not ml:
            break
        markers.append((ml.start(), 'L', extra_letter))
        tail_pos = ml.end()
        extra_letter += 1
    markers.sort()
    texts = {}
    for idx, (start, kind, k) in enumerate(markers):
        mm = re.compile(r'^(?:\d{1,2}|[A-L])\.\s').match(flat[start:])
        body_start = start + (mm.end() if mm else 0)
        end = markers[idx + 1][0] if idx + 1 < len(markers) else len(flat)
        texts[(kind, k)] = flat[body_start:end].strip()
    labels = [texts[('N', i + 1)] for i in range(len(items))]
    defs = [(chr(64 + k), texts[('L', k)]) for k in sorted(k for (kd, k) in texts if kd == 'L')]
    if n_expected and len(items) != n_expected:
        rep['warnings'].append(f"Association : {len(items)} notions reconnues au lieu de {n_expected}")
    g[0] += 1
    out = [f"Question {g[0]} — Associez chaque notion à sa définition ({_fmt(pts_total)} pts) [ASSOCIATION]"]
    if len(defs) > len(items):
        out.append(f"{len(defs) - len(items)} proposition(s) ne correspondent à aucune notion.")
    out.extend(f"- {t}" for t in labels)
    out.extend(f"{l}. {t}" for l, t in defs)
    rep['counts']['association'] = len(items)
    return out


def _open(body, per, g, rep):
    out = []
    txt = list(body)
    i_f = next((i for i, l in enumerate(txt) if re.match(r'^Les faits\s*$', l, re.I)), None)
    i_q = next((i for i, l in enumerate(txt) if re.match(r'^Les questions\s*$', l, re.I)), None)
    if i_f is not None and i_q is not None and i_q > i_f:
        out.append('[DESCRIPTION] Les faits')
        out.extend(_paragraphs(txt[i_f + 1:i_q]))
        txt = txt[i_q + 1:]
    qs, cur = [], None
    for ln in txt:
        if re.match(r"^Fin de l'épreuve", ln, re.I):
            break
        m = re.match(r'^Question\s+(\d{1,2})\.\s*(.*)$', ln, re.I)
        if m:
            cur = [m.group(2).strip()]
            qs.append(cur)
        elif cur is not None:
            cur.append(ln)
    for q in qs:
        g[0] += 1
        out.append(f"Question {g[0]} — {_join(q)} ({_fmt(per)} pts) [OUVERT]")
    rep['counts']['open'] = len(qs)
    if not qs:
        rep['warnings'].append("Partie rédactionnelle : aucune question « Question N. » reconnue")
    return out


def normalize_exam_text(raw: str) -> tuple:
    rep = {'structured': False, 'counts': {}, 'warnings': [], 'sections': []}
    lines = _clean(raw or '')
    heads = [(i, _SECTION.match(l)) for i, l in enumerate(lines)]
    heads = [(i, m) for i, m in heads if m]
    if len(heads) < 2:
        return raw, rep
    rep['structured'] = True

    pre = lines[:heads[0][0]]
    cut = next((i for i, l in enumerate(pre) if l.lower().startswith('répartition des points')), len(pre))
    out = list(pre[:cut])
    g = [0]
    for k, (i, m) in enumerate(heads):
        end = heads[k + 1][0] if k + 1 < len(heads) else len(lines)
        title, pts = m.group('title').strip(), _num(m.group('pts'))
        kind = _kind(title)
        body = lines[i + 1:end]
        consigne, rest = _consigne_split(body)
        pi = _PER_ITEM.search(consigne)
        n_items = int(pi.group('n')) if pi else 0
        per = _num(pi.group('pts')) if pi else (pts / n_items if n_items else pts)
        rep['sections'].append({'rom': m.group('rom'), 'title': title, 'kind': kind, 'points': pts, 'items': n_items})
        before = g[0]
        if kind == 'qcm':
            block = _qcm(rest, per, g, rep)
        elif kind == 'vf':
            block = _vf(rest, per, g, rep)
        elif kind == 'tri':
            block = _tri(rest, per, g, rep)
        elif kind == 'association':
            block = _association(rest, pts, n_items, g, rep)
        elif kind == 'open':
            out.append(f"Partie {m.group('rom')} — {title} ({_fmt(pts)} pts)")
            block = _open(rest, per, g, rep)
        else:
            rep['warnings'].append(f"Partie {m.group('rom')} « {title} » : type non reconnu, texte laissé tel quel")
            block = rest
        if n_items and kind in ('qcm', 'vf', 'open') and rep['counts'].get(kind, 0) != n_items:
            rep['warnings'].append(f"Partie {m.group('rom')} : {rep['counts'].get(kind, 0)} question(s) reconnue(s) au lieu de {n_items}")
        out.extend(block)
    rep['questions'] = g[0]
    return '\n'.join(out), rep
