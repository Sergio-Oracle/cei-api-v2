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
# balises de type déjà écrites par l'enseignant dans son document (« … ? [QCM] ») : la normalisation pose les siennes
_TAG = re.compile(r'\s*\[(?:QCM|QCM_MULTI|VF|OUVERT|SUBOPEN|APPARIEMENT|ASSOCIATION|TRI|ORDRE|TROUS|CLOZE|ZONES|COURT|NUMERIQUE|CALCULEE|CODE)\]', re.I)


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


def _untag(text: str) -> str:
    return re.sub(r'\s+', ' ', _TAG.sub(' ', text)).strip()


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
        if re.match(r'^N°\s', ln):  # en-tête du tableau, répété en haut de page
            continue
        m = re.match(r'^(\d{1,2})[.)]?(?:\s+(.*))?$', ln)
        if m and int(m.group(1)) == nxt:
            rows.append((m.group(2) or '').strip())
            nxt += 1
        elif rows:
            rows[-1] = (rows[-1] + ' ' + ln).strip()
    return [re.sub(r'\s+', ' ', r).strip() for r in rows], nxt


def _consigne_split(body: list) -> tuple:
    """(consigne, reste) — la consigne commence par « Consigne. » et se termine à la parenthèse « (N …, X point chacune) ».
    Elle peut suivre une ou deux lignes d'introduction (« Compétence visée : … »), retirées avec elle."""
    start = next((k for k, ln in enumerate(body[:3]) if ln.lower().startswith('consigne')), None)
    if start is None:
        return '', body
    acc = []
    for k, ln in enumerate(body[start:], start):
        acc.append(ln)
        if _PER_ITEM.search(_join(acc)):
            return _join(acc), body[k + 1:]
        if k - start >= 4:
            break
    return _join(acc[:1]), body[start + 1:]


def _facts(body: list) -> tuple:
    """(lignes de [DESCRIPTION] des faits, reste) — « Les faits … Les questions » d'un cas pratique."""
    i_f = next((i for i, l in enumerate(body) if re.match(r'^Les faits\s*$', l, re.I)), None)
    i_q = next((i for i, l in enumerate(body) if re.match(r'^Les questions\s*$', l, re.I)), None)
    if i_f is None or i_q is None or i_q <= i_f:
        return [], body
    return ['[DESCRIPTION] Les faits'] + _paragraphs(body[i_f + 1:i_q]), body[i_q + 1:]


def _qcm(body, per, g, rep):
    out, body = _facts(body)
    cur, ch = None, []
    nxt = 1

    def flush():
        nonlocal cur, ch
        if cur is not None:
            rep['qinfo'].append({'num': cur[0], 'kind': 'qcm', 'local': cur[2], 'pts': per, 'choices': {l: t for l, t in ch}})
            out.append(f"Question {cur[0]} — {_untag(cur[1])} ({_fmt(per)} pts) [QCM]")
            out.extend(f"{l}. {t}" for l, t in ch)
            if len(ch) < 2:
                rep['warnings'].append(f"QCM {cur[0]} : moins de 2 propositions reconnues")
        cur, ch = None, []

    for ln in body:
        if re.match(r"^Fin de l'épreuve", ln, re.I):
            break
        m = re.match(r'^(\d{1,2})\.\s+(.*)$', ln)
        if m and int(m.group(1)) == nxt and (cur is None or ch):
            flush()
            nxt += 1
            g[0] += 1
            cur = (g[0], m.group(2).strip(), nxt - 1)
            continue
        c = re.match(r'^([A-F])\.\s+(.*)$', ln)
        if c and cur is not None:
            ch.append([c.group(1), c.group(2).strip()])
        elif cur is not None:
            if ch:
                ch[-1][1] = (ch[-1][1] + ' ' + ln).strip()
            else:
                cur = (cur[0], (cur[1] + ' ' + ln).strip(), cur[2])
    flush()
    rep['counts']['qcm'] = rep['counts'].get('qcm', 0) + nxt - 1
    return out


def _vf(body, per, g, rep):
    """Tableau numéroté (PDF) ; à défaut, affirmations non numérotées : une par balise [VF] de
    l'enseignant, sinon une par phrase recollée (document Word : un paragraphe par affirmation)."""
    rows, _ = _rows(body, 1)
    if not rows:
        if any(_TAG.search(l) for l in body):
            acc = []
            for ln in body:
                acc.append(ln)
                if _TAG.search(ln):
                    rows.append(_join(acc))
                    acc = []
        else:
            rows = [l for l in _paragraphs(body) if re.search(r'[A-Za-zÀ-ÿ]{3}', l)]
    out = []
    for k, r in enumerate(rows):
        r = _untag(r)
        g[0] += 1
        rep['qinfo'].append({'num': g[0], 'kind': 'vf', 'local': k + 1, 'pts': per, 'text': r})
        out.append(f"Question {g[0]} — {r} ({_fmt(per)} pts) [VF]")
        out.append('Vrai / Faux')
    rep['counts']['vf'] = rep['counts'].get('vf', 0) + len(rows)
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
        rep['qinfo'].append({'num': g[0], 'kind': 'tri', 'plateau': p['n'], 'first': nxt - len(rows), 'items': rows, 'cats': cats})
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
    rep['qinfo'].append({'num': g[0], 'kind': 'association', 'items': labels, 'defs': defs})
    out = [f"Question {g[0]} — Associez chaque notion à sa définition ({_fmt(pts_total)} pts) [ASSOCIATION]"]
    if len(defs) > len(items):
        out.append(f"{len(defs) - len(items)} proposition(s) ne correspondent à aucune notion.")
    out.extend(f"- {t}" for t in labels)
    out.extend(f"{l}. {t}" for l, t in defs)
    rep['counts']['association'] = len(items)
    return out


def _open(body, per, g, rep):
    out, txt = _facts(list(body))
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
    for k, q in enumerate(qs):
        g[0] += 1
        rep['qinfo'].append({'num': g[0], 'kind': 'open', 'local': k + 1, 'pts': per})
        out.append(f"Question {g[0]} — {_untag(_join(q))} ({_fmt(per)} pts) [OUVERT]")
    rep['counts']['open'] = rep['counts'].get('open', 0) + len(qs)
    if not qs:
        rep['warnings'].append("Partie rédactionnelle : aucune question « Question N. » reconnue")
    return out


def normalize_exam_text(raw: str) -> tuple:
    rep = {'structured': False, 'counts': {}, 'warnings': [], 'sections': [], 'qinfo': []}
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
        before, q_before = g[0], len(rep['qinfo'])
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
        for q in rep['qinfo'][q_before:]:
            q['sec'] = m.group('rom').upper()  # numéros locaux : le corrigé donne une clé par partie
        if n_items and kind in ('qcm', 'vf', 'open') and g[0] - before != n_items:
            rep['warnings'].append(f"Partie {m.group('rom')} : {g[0] - before} question(s) reconnue(s) au lieu de {n_items}")
        out.extend(block)
    rep['questions'] = g[0]
    return '\n'.join(out), rep


# ── Corrigé officiel → barème CEI (sans IA) ───────────────────────────────────────────────────────────────────────

_CORR_SECTION = re.compile(r'^(?P<rom>[IVX]{1,5})\.\s+(?P<title>.+?)\s*\(\s*(?P<pts>\d+(?:[.,]\d+)?)\s*points?\b[^)]*\)\s*$', re.I)


def _flex(text: str) -> str:
    """Motif qui retrouve `text` malgré des retours à la ligne / espaces différents."""
    return r'\s+'.join(re.escape(w) for w in text.split())


def rubric_from_corrige(corrige_raw: str, rep: dict) -> tuple:
    """Barème CEI (texte) écrit directement depuis le corrigé officiel, partie par partie, grâce à la
    numérotation mémorisée par normalize_exam_text. Retourne (barème, avertissements). Si un avertissement
    est renvoyé, l'appelant garde le barème partiel et laisse l'IA compléter à partir du corrigé."""
    warns = []
    lines = _clean(corrige_raw or '')
    heads = [(i, _CORR_SECTION.match(l)) for i, l in enumerate(lines)]
    heads = [(i, m) for i, m in heads if m]
    if not heads or not rep.get('qinfo'):
        return '', ['corrigé non structuré (parties « I. … (N points) » introuvables)']
    sec = {}
    for k, (i, m) in enumerate(heads):
        end = heads[k + 1][0] if k + 1 < len(heads) else len(lines)
        sec[m.group('rom').upper()] = (_kind(m.group('title')), lines[i + 1:end])
    # le dernier bloc contient la grille de notation individuelle : à retirer
    for rom, (kd, body) in list(sec.items()):
        cut = next((j for j, l in enumerate(body) if l.lower().startswith('grille de notation')), len(body))
        sec[rom] = (kd, body[:cut])

    def kind_sec(kind):
        for rom, (kd, body) in sec.items():
            if kd == kind:
                return body
        return None

    # Clés par partie : « Clé de correction rapide » (en tête du corrigé, une ligne « I. QCM : 1 C · 2 A… » par
    # partie, ou dans la partie elle-même), sinon colonne « Rép. » du tableau de la partie (« 1 C Là où… »).
    _tok = r'(\d{1,2})\s*(Vrai|Faux|[A-F]|V)\b'
    quick = {}
    for ln in lines:
        mq = re.match(r'^([IVX]{1,5})\.\s+[^:()]{2,40}:\s*(\d.*)$', ln)
        if mq and not _CORR_SECTION.match(ln):
            quick.setdefault(mq.group(1).upper(), {}).update((int(a), b) for a, b in re.findall(_tok, mq.group(2)))

    def sec_key(rom, kind):
        if rom not in sec:  # épreuve et corrigé numérotés autrement : première partie de ce type
            rom = next((r for r, (kd, _) in sec.items() if kd == kind), rom)
        key = dict(quick.get(rom, {}))
        body = sec.get(rom, (None, []))[1]
        mk = re.search(r'Cl[ée] de correction rapide\s*:(.*?)(?:N°\s+R[ée]p|$)', ' '.join(body), re.I)
        if mk:
            key.update((int(a), b) for a, b in re.findall(_tok, mk.group(1)))
        for ln in body:
            mr = re.match(r'^' + _tok, ln)
            if mr:
                key.setdefault(int(mr.group(1)), mr.group(2))
        return key

    out = ['=== BARÈME DE NOTATION (écrit depuis le corrigé officiel) ===', '']
    total = 0.0
    sections_info = {sc['kind']: sc for sc in rep['sections']}
    per_by_kind = {}
    for sc in rep['sections']:
        per_by_kind[sc['kind']] = (sc['points'] / sc['items']) if sc.get('items') else sc['points']

    # QCM
    for q in [q for q in rep['qinfo'] if q['kind'] == 'qcm']:
        letter = sec_key(q.get('sec'), 'qcm').get(q['local'])
        if not letter or letter not in q['choices']:
            warns.append(f"QCM {q['local']} (partie {q.get('sec', '?')}) : réponse introuvable dans le corrigé")
            continue
        per = q.get('pts', per_by_kind.get('qcm', 0.25)); total += per
        out += [f"Question {q['num']} ({_fmt(per)} pts) :", f"  • Bonne réponse : {letter}) {q['choices'][letter]}", '']
    # Vrai / faux
    for q in [q for q in rep['qinfo'] if q['kind'] == 'vf']:
        v = {'V': 'Vrai', 'Vrai': 'Vrai', 'F': 'Faux', 'Faux': 'Faux'}.get(sec_key(q.get('sec'), 'vf').get(q['local']))
        if not v:
            warns.append(f"Vrai/Faux {q['local']} : réponse introuvable dans le corrigé")
            continue
        per = q.get('pts', per_by_kind.get('vf', 0.25)); total += per
        out += [f"Question {q['num']} ({_fmt(per)} pts) :", f"  • Réponse : {v}", '']
    # Association
    body = kind_sec('association')
    for q in [q for q in rep['qinfo'] if q['kind'] == 'association']:
        flat = re.sub(r'\s+', ' ', ' '.join(body or []))
        pairs, pos = [], 0
        for k, label in enumerate(q['items'], 1):
            m = re.compile(r'(?:^|\s)%d\s+%s\s+([A-L])(?=\s)' % (k, _flex(label))).search(flat, pos)
            if not m:
                warns.append(f"Association : notion {k} « {label} » introuvable dans le corrigé")
                continue
            pairs.append(f"{k}-{m.group(1)}"); pos = m.end()
        if len(pairs) == len(q['items']):
            sc = sections_info.get('association', {})
            pts = sc.get('points', 0); total += pts
            out += [f"Question {q['num']} ({_fmt(pts)} pts) :", "  • Réponse : " + ', '.join(pairs), '']
    # Tri
    body = kind_sec('tri')
    tri = [q for q in rep['qinfo'] if q['kind'] == 'tri']
    if tri:
        flat = re.sub(r'\s+', ' ', ' '.join(body or []))
        pos = 0
        for q in tri:
            res = []
            for idx, item in enumerate(q['items']):
                n = q['first'] + idx
                m = re.compile(r'(?:^|\s)%d\s+%s\s+(.{0,40}?)(?=\s+[A-ZÉ])' % (n, _flex(item))).search(flat, pos)
                cat = None
                if m:
                    tail = flat[m.end(1) - len(m.group(1)):m.end(1) + 40]
                    for ci, c in enumerate(q['cats']):
                        if re.match(r'\s*' + _flex(c), tail, re.I):
                            cat = chr(65 + ci); break
                    pos = m.start() + 1
                if not cat:
                    # repli : première catégorie nommée juste après la situation
                    m2 = re.compile(_flex(item) + r'\s+(' + '|'.join(_flex(c) for c in q['cats']) + r')', re.I).search(flat, pos)
                    if m2:
                        for ci, c in enumerate(q['cats']):
                            if re.fullmatch(_flex(c), m2.group(1), re.I):
                                cat = chr(65 + ci); break
                if not cat:
                    warns.append(f"Tri : situation {n} introuvable ou catégorie non reconnue dans le corrigé")
                    break
                res.append(f"{idx + 1}-{cat}")
            if len(res) == len(q['items']):
                per = per_by_kind.get('tri', 0.25) * len(q['items']); total += per
                out += [f"Question {q['num']} ({_fmt(per)} pts) :", "  • Réponse : " + ', '.join(res), '']
    # Cas pratique / questions ouvertes
    body = kind_sec('open')
    for q in [q for q in rep['qinfo'] if q['kind'] == 'open']:
        txt = re.sub(r'\s+', ' ', ' '.join(body or []))
        m = re.search(r'Question\s+%d\.(.*?)(?=Question\s+%d\.|\Z)' % (q['local'], q['local'] + 1), txt)
        if not m:
            warns.append(f"Question ouverte {q['local']} : introuvable dans le corrigé")
            continue
        seg = m.group(1)
        seg = re.split(r'\bSource\s*:', seg)[0]
        bm = re.search(r'Bar[èe]me\s+(.*)$', seg)
        parts = []
        if bm:
            parts = [p.strip(' .;') for p in bm.group(1).split(';') if p.strip(' .;')]
        def grab(label, nxt):
            mm = re.search(label + r'\s+(.*?)(?=\s+(?:' + nxt + r')\s+|$)', seg)
            return mm.group(1).strip() if mm else ''
        maj = grab('Majeure', 'Mineure'); mi = grab('Mineure', 'Conclusion'); co = grab('Conclusion', 'Bar[èe]me')
        per = q['pts']; total += per
        out.append(f"Question {q['num']} ({_fmt(per)} pts) :")
        for k, ptxt in enumerate(parts, 1):
            pm = re.match(r'(\d+(?:[.,]\d+)?)\s*points?\s*(?:pour\s+)?(.*)', ptxt, re.I)
            out.append(f"  • Critère {k} : {pm.group(1)} pt — {pm.group(2)}" if pm else f"  • Critère {k} : {ptxt}")
        if not parts:
            out.append(f"  • Critère : {_fmt(per)} pt — voir corrigé ci-dessous")
            warns.append(f"Question ouverte {q['local']} : barème « Barème … » introuvable")
        out.append(f"  • Corrigé attendu — Majeure : {maj} Mineure : {mi} Conclusion : {co}")
        out.append('')
    out += ['──────────────────────────', f'TOTAL : {_fmt(total)} / {_fmt(sum(sc["points"] for sc in rep["sections"]))} points']
    return '\n'.join(out), warns
