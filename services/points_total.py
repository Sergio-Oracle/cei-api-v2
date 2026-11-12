"""Total des points d'un sujet : toujours la note demandée, jamais un total inventé par l'IA.

Constat (12/11/2026, sujet « Test Blanc ») : le document Word déposé ne contenait AUCUN point. La plateforme demande à l'IA de rédiger le
barème « avec EXACTEMENT 20 points », mais ne vérifiait jamais la somme rendue : l'IA a écrit 1,5 + 1,5 + 2 + 2,5 + 2,5 = 10 et « Total : 10
points ». Sur 34 sujets de production, 18 ne totalisaient pas 20 (16 téléversés). Ce n'est pas un défaut de lecture du document (OCR) :
la lecture est exacte, c'est le barème écrit par l'IA qui n'était pas contrôlé.

Ce module règle le problème à la source :
  enforce_total()          après la rédaction du barème par l'IA : somme ramenée EXACTEMENT au total demandé, en gardant les poids
                           relatifs choisis par l'IA (pas de 0,25 pour des notes lisibles) ; sans effet si le document donne lui-même
                           ses points « (2 pts) » (barème officiel de l'enseignant : jamais modifié).
  rescale_points()         même opération à la demande (bouton « Ramener sur 20 » de l'éditeur), points du document compris.
  reconcile_inline_keys()  la bonne réponse écrite dans le document (« Réponse attendue : B ») fait foi : le barème recopié par l'IA
                           est aligné dessus s'il diverge.
"""
from __future__ import annotations

import re

GRAIN = 0.25
_NUM = r'\d+(?:[.,]\d+)?'
_Q_HEAD_RE = re.compile(r'^\s*Question\s+(\d{1,3})\s*(?:[—\-–:.]|\()', re.I)
_TITLE_PTS_RE = re.compile(r'\(\s*' + _NUM + r'\s*pts?\s*\)', re.I)
_CRIT_PTS_RE = re.compile(r'(' + _NUM + r')(\s*pts?\b)', re.I)
_TOTAL_SLASH_RE = re.compile(r'(TOTAL\s*:\s*)[\d.,_]+(\s*/\s*)[\d.,_]+(\s*points?)', re.I)
_TOTAL_PLAIN_RE = re.compile(r'(Total\s*:\s*)[\d.,_]+(\s*points?)', re.I)
_SECTION_RE = re.compile(r'^(\s*Partie\b[^\n]*?\()\s*' + _NUM + r'(\s*pts?,\s*)(\d+)(\s*questions?\s*\))', re.I)
_NOTE_TOTALE_RE = re.compile(r'(Note\s+totale\s*:\s*)\d+(?:[.,]\d+)?(\s*points?)', re.I)
_NOTE_SUR_RE = re.compile(r'(not[ée]e?\s+sur\s*)\d+(?:[.,]\d+)?(\s*points?)', re.I)
_INLINE_KEY_RE = re.compile(r'^\s*(?:Bonne\s+)?R[ée]ponse(?:\s+(?:correcte|attendue|exacte))?\s*:?\s*\(?([A-La-l])\)?\s*[.)]?\s*$', re.I)
_QCM_KEY_RE = re.compile(r'(Bonne\s+r[ée]ponse\s*:\s*)\(?[A-La-l]\)?', re.I)


def fmt(v: float) -> str:
    """12.0 → « 12 », 1.25 → « 1.25 » (point décimal, comme le reste des barèmes générés)."""
    s = f"{float(v):.2f}".rstrip('0').rstrip('.')
    return s or '0'


def _num(s: str) -> float:
    return float(str(s).replace(',', '.'))


def allocate(target: float, weights: list, grain: float = GRAIN) -> list:
    """Répartit `target` sur autant de parts que de poids, proportionnellement, en multiples de `grain`, somme EXACTE.
    Plus fort reste ; chaque part vaut au moins un pas quand le total le permet."""
    n = len(weights)
    if n == 0:
        return []
    units = int(round(float(target) / grain))
    w = [max(float(x), 0.0) for x in weights]
    tw = sum(w)
    if tw <= 0:
        w, tw = [1.0] * n, float(n)
    raw = [units * x / tw for x in w]
    base = [int(r) for r in raw]
    if units >= n:
        base = [max(1, b) for b in base]
    left = units - sum(base)
    order = sorted(range(n), key=lambda i: (-(raw[i] - int(raw[i])), i))
    k = 0
    while left > 0:
        base[order[k % n]] += 1
        left -= 1
        k += 1
    while left < 0:                                   # le minimum d'un pas a pu faire dépasser : retire aux plus gros
        i = max(range(n), key=lambda j: (base[j], -j))
        if base[i] <= 1:
            break
        base[i] -= 1
        left += 1
    return [round(b * grain, 2) for b in base]


def _posed(content: str) -> list:
    from routes.exams import _parse_subject_blocks_ordered
    out, seen = [], set()
    for b in _parse_subject_blocks_ordered(content or ''):
        if b.get('type') in ('section', 'text') or not b.get('num'):
            continue
        n = str(b['num'])
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


def current_total(content: str, rubric: str) -> tuple:
    """(somme des points des questions POSÉES, {numéro: points}) — la même lecture que la notation."""
    from routes.exams import _question_points_map
    posed = set(_posed(content))
    pm = _question_points_map(content or '', rubric or '')
    return round(sum(v for n, v in pm.items() if n in posed), 2), pm


def document_gives_points(content: str) -> bool:
    """Le document déposé indique lui-même les points de ses questions : « Question 3 — … (2 pts) »."""
    return bool(re.search(r'(?m)^\s*Question\s+\d{1,3}[^\n]*\(\s*' + _NUM + r'\s*pts?\s*\)', content or '', re.I))


def rescale_points(content: str, rubric: str, target: float = 20) -> tuple:
    """Ramène la somme des points des questions posées à EXACTEMENT `target`, en gardant les poids relatifs.
    Retourne (content, rubric, {numéro: nouveaux points}). Sujet ou barème illisible : renvoyé inchangé, dict vide."""
    nums = _posed(content)
    if not nums or not (rubric or '').strip():
        return content, rubric, {}
    _, pm = current_total(content, rubric)
    known = [pm[n] for n in nums if pm.get(n)]
    mean = (sum(known) / len(known)) if known else 1.0
    new = dict(zip(nums, allocate(target, [pm.get(n) or mean for n in nums])))
    T = fmt(target)

    # ── barème : en-têtes de question, critères de chaque question (somme = points de la question), ligne TOTAL
    lines = (rubric or '').split('\n')
    heads = [(i, _Q_HEAD_RE.match(l).group(1)) for i, l in enumerate(lines) if _Q_HEAD_RE.match(l)]
    out = list(lines)
    for idx, (i, num) in enumerate(heads):
        end = heads[idx + 1][0] if idx + 1 < len(heads) else len(lines)
        for j in range(i, end):                       # ne dépasse pas la ligne TOTAL de fin de barème
            if re.search(r'\bTOTAL\s*:|\bTotal\s*:', lines[j]) and j > i:
                end = j
                break
        if num not in new:
            continue
        q, p_old = new[num], pm.get(num)
        out[i] = _TITLE_PTS_RE.sub(f"({fmt(q)} pts)", lines[i], count=1)
        toks = [(j, m) for j in range(i + 1, end) for m in _CRIT_PTS_RE.finditer(lines[j])]
        if not toks:
            continue
        vals = [_num(m.group(1)) for _, m in toks]
        if p_old and abs(sum(vals) - p_old) <= 0.011:
            newv = allocate(q, vals)                  # les critères continuent de totaliser les points de la question
        else:
            f = (q / p_old) if p_old else 1.0
            newv = [round(v * f, 2) for v in vals]
        it = iter(newv)
        for j in range(i + 1, end):
            out[j] = _CRIT_PTS_RE.sub(lambda m: f"{fmt(next(it))}{m.group(2)}", lines[j])
    out = [_TOTAL_PLAIN_RE.sub(lambda m: f"{m.group(1)}{T}{m.group(2)}", _TOTAL_SLASH_RE.sub(lambda m: f"{m.group(1)}{T}{m.group(2)}{T}{m.group(3)}", l)) for l in out]
    new_rubric = '\n'.join(out)

    # ── énoncé : points dans les titres, totaux de parties, mentions de la note totale
    cl = (content or '').split('\n')
    cur = None
    for i, l in enumerate(cl):
        m = _Q_HEAD_RE.match(l)
        if m and m.group(1) in new:
            cl[i] = _TITLE_PTS_RE.sub(f"({fmt(new[m.group(1)])} pts)", l, count=1)
    # totaux de parties = somme des questions qui les suivent
    sec_idx = [i for i, l in enumerate(cl) if _SECTION_RE.match(l)]
    for k, i in enumerate(sec_idx):
        stop = sec_idx[k + 1] if k + 1 < len(sec_idx) else len(cl)
        nq = [_Q_HEAD_RE.match(cl[j]).group(1) for j in range(i + 1, stop) if _Q_HEAD_RE.match(cl[j])]
        tot = sum(new.get(n, 0) for n in nq)
        cl[i] = _SECTION_RE.sub(lambda m: f"{m.group(1)}{fmt(tot)}{m.group(2)}{m.group(3)}{m.group(4)}", cl[i], count=1)
    new_content = '\n'.join(cl)
    new_content = _NOTE_TOTALE_RE.sub(lambda m: f"{m.group(1)}{T}{m.group(2)}", new_content)
    new_content = _NOTE_SUR_RE.sub(lambda m: f"{m.group(1)}{T}{m.group(2)}", new_content)
    return new_content, new_rubric, new


def enforce_total(content: str, rubric: str, target: float = 20) -> tuple:
    """Après la rédaction du barème par l'IA : total ramené à `target`. Sans effet si le document donne lui-même ses points
    (barème officiel de l'enseignant) ou si le total est déjà bon. Retourne (content, rubric, changed, ancien_total)."""
    try:
        if not (rubric or '').strip() or document_gives_points(content):
            return content, rubric, False, None
        total, _ = current_total(content, rubric)
        if abs(total - float(target)) <= 0.01:
            return content, rubric, False, total
        c2, r2, new = rescale_points(content, rubric, target)
        if not new:
            return content, rubric, False, total
        return c2, r2, True, total
    except Exception as e:                            # jamais bloquant : le contrôle du sujet signalera un total inattendu
        print(f"[points_total] total non ajusté : {e}")
        return content, rubric, False, None


def reconcile_inline_keys(content: str, rubric: str) -> tuple:
    """La bonne réponse écrite DANS le document (QCM : « Réponse attendue : B ») fait foi. Si le barème rédigé par l'IA diverge,
    il est aligné sur le document. Retourne (rubric, [(numéro, lettre du barème, lettre du document)])."""
    try:
        from routes.exams import _extract_correct_answers
        keys = _extract_correct_answers(rubric or '')
        lines = (content or '').split('\n')
        heads = [(i, _Q_HEAD_RE.match(l).group(1)) for i, l in enumerate(lines) if _Q_HEAD_RE.match(l)]
        fixes = {}
        for idx, (i, num) in enumerate(heads):
            end = heads[idx + 1][0] if idx + 1 < len(heads) else len(lines)
            if not re.search(r'\[QCM\]', lines[i], re.I):
                continue
            doc = [m.group(1).upper() for m in (_INLINE_KEY_RE.match(l) for l in lines[i + 1:end]) if m]
            k = keys.get(num)
            if len(doc) != 1 or not k or k.get('type') != 'qcm' or len(k.get('letters') or []) != 1:
                continue
            old = next(iter(k['letters'])).upper()
            if old != doc[0]:
                fixes[num] = (old, doc[0])
        if not fixes:
            return rubric, []
        rl = (rubric or '').split('\n')
        rheads = [(i, _Q_HEAD_RE.match(l).group(1)) for i, l in enumerate(rl) if _Q_HEAD_RE.match(l)]
        for idx, (i, num) in enumerate(rheads):
            if num not in fixes:
                continue
            end = rheads[idx + 1][0] if idx + 1 < len(rheads) else len(rl)
            for j in range(i + 1, end):
                if _QCM_KEY_RE.search(rl[j]):
                    rl[j] = _QCM_KEY_RE.sub(lambda m: f"{m.group(1)}{fixes[num][1]})", rl[j], count=1)
                    break
        return '\n'.join(rl), [(n, a, b) for n, (a, b) in sorted(fixes.items(), key=lambda kv: int(kv[0]))]
    except Exception as e:
        print(f"[points_total] clés du document non comparées : {e}")
        return rubric, []
