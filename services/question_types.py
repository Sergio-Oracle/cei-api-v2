"""Types de questions ajoutés pour couvrir ceux de Moodle (qtype_match avec leurres, gapselect,
ordering, shortanswer, numerical, description) dans le langage de balises des sujets CEI.

Balises (titre de la question) et leur équivalent Moodle :
  [ASSOCIATION]  match avec leurres : éléments « - texte », propositions « A. texte » (plus nombreuses que les éléments)
  [TRI]          match plusieurs-vers-un : ligne « Catégories : X | Y », éléments « - texte »
  [ORDRE]        ordering : éléments « A. texte » donnés en désordre, l'étudiant donne le rang de chacun
  [TROUS]        gapselect : texte avec {1} {2}…, propositions communes « A. texte »
  [COURT]        shortanswer : une réponse libre d'une ligne, comparée aux réponses acceptées du barème
  [NUMERIQUE]    numerical : une valeur, comparée avec tolérance
  [CLOZE]        multianswer : texte avec champs {1} {2}…, chaque champ décrit par une ligne « - 1 COURT », « - 2 NUM »
                 ou « - 3 CHOIX : a | b | c » (champ libre, nombre, ou menu déroulant)
  [CALCULEE]     calculated : énoncé avec variables {a} {b} tirées au hasard PAR ÉTUDIANT (ligne « Variables : a = 1..9 | b = 2..5 pas 0,5 »),
                 réponse = formule du barème
  [ZONES]        ddimageortext/ddmarker : une [IMAGE:fichier] et « Étiquettes : A | B | C » ; l'étudiant place chaque étiquette sur l'image
  [DESCRIPTION]  description : texte d'information (énoncé commun), ni numéro ni note

Barème (ligne dans le bloc de la question) :
  association / tri / trous :  « Réponse : 1-H, 2-E, 3-K »   (élément-proposition, la proposition est une lettre)
  ordre :                      « Ordre : C, A, B, D »          (du 1er au dernier)
  court :                      « Réponses acceptées : Dakar | dakar »
  numerique :                  « Valeur : 12,5 (± 0,1) »  ou  « Valeur : 12,5 (± 2 %) »
  cloze :                      « Champs : 1=Dakar|dakar ; 2=14 (± 0,5) ; 3=B »  (réponses acceptées, nombre + tolérance, lettre du choix)
  calculee :                   « Formule : a*b*n/100 (± 1) »  (variables de l'énoncé ; tolérance absolue ou en %)
  zones :                      « Zones : 1=[x1,y1,x2,y2] ; 2=[…] »  (rectangles en % de l'image, étiquettes dans l'ordre)

Les familles « grille » (association, tri, ordre, trous) partagent le même stockage des réponses
(pq_<n>_<index de l'élément> = lettre ou rang) et la même notation proportionnelle (fraction d'éléments
justes), comme qtype_match de Moodle.
"""
import re
import unicodedata

GRID_MARKERS = ('ASSOCIATION', 'TRI', 'ORDRE', 'TROUS', 'CLOZE', 'ZONES')
TEXT_MARKERS = ('COURT', 'NUMERIQUE', 'CALCULEE')
NEW_MARKERS = GRID_MARKERS + TEXT_MARKERS
# Alternance pour les expressions régulières des lecteurs de sujet (plus longs d'abord)
MARKER_ALT = ('QCM_MULTI|QCM|VF|OUVERT|SUBOPEN|APPARIEMENT|CODE|ASSOCIATION|TRI|ORDRE|TROUS|CLOZE|ZONES|COURT|NUMERIQUE|CALCULEE')

GRID_BTYPE = {m: m.lower() for m in GRID_MARKERS}
TEXT_BTYPE = {'COURT': 'court', 'NUMERIQUE': 'numerique'}

ITEM_RE = re.compile(r'^\s*[-•*▪]\s+(.+)$')                      # élément « - texte »
CATS_RE = re.compile(r'^\s*Cat[ée]gories?\s*:\s*(.+)$', re.I)    # « Catégories : A | B »
GAP_RE = re.compile(r'\{(\d{1,2})\}')                             # trou {1}
DESC_RE = re.compile(r'^\s*\[DESCRIPTION\]\s*(.*)$', re.I)
TIRAGE_RE = re.compile(r'^\s*Tirage\s*:\s*(\d{1,2})\b', re.I)       # appariement aléatoire : « Tirage : 5 » = 5 paires tirées au hasard par étudiant
UPLOAD_RE = re.compile(r'^\s*(?:Fichier|Pi[èe]ce\s+jointe|D[ée]p[ôo]t)\s*:\s*(oui|obligatoire|facultatif|autoris[ée])\s*$', re.I)  # réponse rédigée + fichier joint
GRILLE_RE = re.compile(r'^\s*Grille\s*:\s*(.+)$', re.I)          # mots croisés : « 1:3:H:4:0:12 ; 2:… » = champ:numéro:sens:ligne:colonne:longueur


def parse_grille(spec: str) -> list:
    out = []
    for part in re.split(r'\s*;\s*', spec.strip()):
        m = re.match(r'^(\d{1,2}):(\d{1,2}):([HV]):(\d{1,2}):(\d{1,2}):(\d{1,2})$', part.strip(), re.I)
        if m:
            out.append({'k': int(m.group(1)), 'num': int(m.group(2)), 'dir': m.group(3).upper(),
                        'row': int(m.group(4)), 'col': int(m.group(5)), 'len': int(m.group(6))})
    return out


def norm_text(s: str) -> str:
    """Comparaison tolérante : minuscules, sans accents, sans ponctuation de fin, espaces simples."""
    s = unicodedata.normalize('NFKD', str(s or ''))
    s = ''.join(c for c in s if not unicodedata.combining(c)).casefold()
    s = re.sub(r'[\s ]+', ' ', s).strip()
    return s.strip(' .;:,!?«»"\'’')


def parse_number(s):
    """'12,5' / '1 250.75' / '−3' → float, sinon None."""
    t = str(s or '').strip().replace(' ', '').replace(' ', '').replace('−', '-').replace(',', '.')
    t = re.sub(r'[^0-9eE+\-.]', '', t)
    try:
        return float(t)
    except ValueError:
        return None


# ── Clés du barème ────────────────────────────────────────────────────────────

_PAIRS_RE = re.compile(r'R[ée]ponses?\s*:\s*((?:\d{1,2}\s*[-=:→>]+\s*[A-Za-z0-9]{1,2}\s*[,;]?\s*)+)', re.I)
_PAIR_ITEM_RE = re.compile(r'(\d{1,2})\s*[-=:→>]+\s*([A-Za-z0-9]{1,2})')
_ORDER_RE = re.compile(r'Ordre(?:\s+correct)?\s*:\s*([A-Za-z](?:\s*[,;>→-]\s*[A-Za-z])+)', re.I)
_ACCEPT_RE = re.compile(r'R[ée]ponses?\s+(?:accept[ée]es?|attendues?)\s*:\s*(.+)', re.I)
_VALUE_RE = re.compile(r'Valeur(?:\s+attendue)?\s*:\s*([-−+]?\d[\d\s .,]*)(?:\s*\(\s*[±+]\s*/?\s*-?\s*([\d.,]+)\s*(%?)\s*\))?', re.I)


def extract_keys_from_block(block: str) -> dict:
    """Clés des nouveaux types lues dans UN bloc de barème (texte d'une question)."""
    out = {}
    m = _PAIRS_RE.search(block)
    if m:
        pairs = {int(a): b.upper() for a, b in _PAIR_ITEM_RE.findall(m.group(1))}
        if pairs:
            out['pairs'] = pairs
    m = _ORDER_RE.search(block)
    if m:
        out['order'] = [x.upper() for x in re.findall(r'[A-Za-z]', m.group(1))]
    m = _ACCEPT_RE.search(block)
    if m:
        out['accepted'] = [a.strip() for a in re.split(r'\s*[|;]\s*', m.group(1).strip()) if a.strip()]
    m = _VALUE_RE.search(block)
    if m:
        v = parse_number(m.group(1))
        if v is not None:
            tol = parse_number(m.group(2)) if m.group(2) else 0.0
            out['number'] = {'value': v, 'tol': abs(tol or 0.0), 'percent': m.group(3) == '%'}
    m = _FIELDS_RE.search(block)
    if m:
        fields = {}
        for part in re.split(r'\s*;\s*', m.group(1).strip()):
            fm = re.match(r'(\d{1,2})\s*=\s*(.+)$', part.strip())
            if fm:
                fields[int(fm.group(1))] = fm.group(2).strip()
        if fields:
            out['fields'] = fields
    m = _FORMULA_RE.search(block)
    if m:
        tol = parse_number(m.group(2)) if m.group(2) else 0.0
        out['formula'] = {'expr': m.group(1).strip(), 'tol': abs(tol or 0.0), 'percent': m.group(3) == '%'}
    m = _ZONES_RE.search(block)
    if m:
        zones = {}
        for zm in re.finditer(r'(\d{1,2})\s*=\s*\[\s*([\d.,\s]+?)\s*\]', m.group(1)):
            vals = [float(x) for x in re.findall(r'\d+(?:[.]\d+)?', zm.group(2))]
            if len(vals) == 4:
                zones[int(zm.group(1))] = vals
        if zones:
            out['zones'] = zones
    return out


# ── Lecture de l'énoncé (blocs) ───────────────────────────────────────────────

def grid_from_lines(marker: str, extra_lines: list, choices: list, items: list, cats: list, text: str):
    """Construit (éléments, propositions) d'une question « grille » à partir des lignes lues.
    items : [{'text'}], choices : [{'letter','text'}] (propositions lettrées ou éléments d'ordre)."""
    if marker == 'ORDRE':
        its = [{'text': c['text'], 'letter': c['letter']} for c in choices]
        opts = [{'letter': str(i + 1), 'text': str(i + 1)} for i in range(len(its))]
        return its, opts
    if marker == 'TROUS':
        gaps = sorted({int(g) for g in GAP_RE.findall(' '.join([text] + list(extra_lines)))})
        its = [{'text': f'Trou {g}', 'gap': g} for g in gaps]
        return its, [{'letter': c['letter'], 'text': c['text']} for c in choices]
    if marker == 'CLOZE':
        return cloze_items(items), []
    if marker == 'ZONES':
        return [{'text': t, 'zone': i + 1} for i, t in enumerate(items)], []
    if marker == 'TRI':
        opts = [{'letter': chr(65 + i), 'text': t} for i, t in enumerate(cats)]
        return [{'text': t} for t in items], opts
    return [{'text': t} for t in items], [{'letter': c['letter'], 'text': c['text']} for c in choices]


# ── Notation ──────────────────────────────────────────────────────────────────

def grade_grid(marker: str, q: dict, key: dict, answers: dict, num: str):
    """→ (fraction 0..1, détail lisible, réponse donnée résumée) ou None si pas de clé."""
    items = q.get('items') or []
    n = len(items)
    if not n:
        return None
    given = {}
    for i in range(n):
        v = str(answers.get(f'pq_{num}_{i}') or '').strip().upper()
        if v:
            given[i] = v
    if marker == 'CLOZE':
        fields = key.get('fields')
        if not fields or any(it.get('gap') not in fields for it in items):
            return None
        good, parts = 0, []
        for i, it in enumerate(items):
            raw = str(answers.get(f'pq_{num}_{i}') or '').strip()
            spec = fields[it['gap']]
            ok = False
            if it.get('kind') == 'num':
                m = re.match(r'(-?[\d\s.,]+?)\s*(?:\(\s*[±+]\s*/?\s*-?\s*([\d.,]+)\s*(%?)\s*\))?\s*$', spec)
                v, want = parse_number(raw), (parse_number(m.group(1)) if m else None)
                if v is not None and want is not None:
                    tol = parse_number(m.group(2)) if m.group(2) else 0.0
                    tol = abs(want) * tol / 100.0 if m.group(3) == '%' else (tol or 0.0)
                    ok = abs(v - want) <= tol + 1e-9
            elif it.get('kind') == 'choix':
                ok = raw.upper() == spec.strip().upper()
            else:
                ok = bool(norm_text(raw)) and norm_text(raw) in [norm_text(a) for a in spec.split('|')]
            good += ok
            parts.append(f"{it['gap']}-{raw or '·'}")
        return good / len(items), good, len(items), ', '.join(parts), {}
    if marker == 'ZONES':
        zones = key.get('zones')
        if not zones or any((it.get('zone') or 0) not in zones for it in items):
            return None
        good, parts = 0, []
        for i, it in enumerate(items):
            raw = str(answers.get(f'pq_{num}_{i}') or '').strip()
            ok = bool(raw) and point_in_rect(raw, zones[it['zone']])
            good += ok
            parts.append(f"{it['text']}:{'ok' if ok else ('hors zone' if raw else 'non placé')}")
        return good / len(items), good, len(items), ', '.join(parts), {}
    if marker == 'ORDRE':
        order = key.get('order')
        letters = [it.get('letter') for it in items]
        if not order or sorted(order) != sorted(letters):
            return None
        want = {letters.index(l): str(p + 1) for p, l in enumerate(order)}
    else:
        pairs = key.get('pairs')
        if not pairs:
            return None
        want = {k - 1: v for k, v in pairs.items() if 1 <= k <= n}
        if len(want) < n:
            return None
    good = sum(1 for i in range(n) if given.get(i) == want.get(i))
    resume = ', '.join(f"{i + 1}-{given[i]}" for i in sorted(given)) or '(aucune réponse)'
    return good / n, good, n, resume, want


def grade_court(key: dict, raw: str):
    acc = key.get('accepted')
    if not acc:
        return None
    given = norm_text(raw)
    return (1.0 if given and any(given == norm_text(a) for a in acc) else 0.0), (raw or '').strip()


def grade_numerique(key: dict, raw: str):
    k = key.get('number')
    if not k:
        return None
    v = parse_number(raw)
    if v is None:
        return 0.0, (raw or '').strip()
    tol = abs(k['value']) * k['tol'] / 100.0 if k['percent'] else k['tol']
    return (1.0 if abs(v - k['value']) <= tol + 1e-9 else 0.0), (raw or '').strip()


# ── Champs de texte à trous imbriqués (cloze) ─────────────────────────────────

_CLOZE_ITEM_RE = re.compile(r'^\s*(\d{1,2})\s+(COURT|NUM|CHOIX)\b(?:\s*:\s*(.+))?$', re.I)
_FIELDS_RE = re.compile(r'Champs\s*:\s*(.+)', re.I)
_FORMULA_RE = re.compile(r'Formule\s*:\s*([^\n(]+?)\s*(?:\(\s*[±+]\s*/?\s*-?\s*([\d.,]+)\s*(%?)\s*\))?\s*$', re.I | re.M)
_ZONES_RE = re.compile(r'Zones\s*:\s*(.+)', re.I)
LABELS_RE = re.compile(r'^\s*[ÉE]tiquettes?\s*:\s*(.+)$', re.I)
VARS_RE = re.compile(r'^\s*Variables?\s*:\s*(.+)$', re.I)


def cloze_items(raw_items: list) -> list:
    out = []
    for r in raw_items:
        m = _CLOZE_ITEM_RE.match(r)
        if not m:
            continue
        kind = m.group(2).lower()
        opts = [o.strip() for o in (m.group(3) or '').split('|') if o.strip()] if kind == 'choix' else []
        out.append({'text': f'Champ {m.group(1)}', 'gap': int(m.group(1)), 'kind': kind, 'options': opts, 'spec': r})
    return out


# ── Variables aléatoires par étudiant (calculée) ─────────────────────────────

def parse_vars(line: str) -> dict:
    """« a = 100000..500000 pas 10000 | b = 3..9 » → {nom: (min, max, pas, entier)}."""
    out = {}
    for part in (line or '').split('|'):
        m = re.match(r'\s*([A-Za-z]\w*)\s*=\s*(-?[\d.,]+)\s*\.\.\s*(-?[\d.,]+)(?:\s*pas\s*([\d.,]+))?\s*$', part)
        if not m:
            continue
        lo, hi = parse_number(m.group(2)), parse_number(m.group(3))
        step = parse_number(m.group(4)) if m.group(4) else 1.0
        is_int = all(float(x).is_integer() for x in (lo, hi, step))
        if lo is None or hi is None or not step or step <= 0 or hi < lo:
            continue
        out[m.group(1)] = (lo, hi, step, is_int)
    return out


def instantiate(vars_line: str, seed, num) -> dict:
    """Valeurs des variables pour (seed, question) : stables pour un même étudiant, différentes d'un étudiant à l'autre."""
    import random
    rng = random.Random(f'{seed}:{num}')
    values = {}
    for name, (lo, hi, step, is_int) in parse_vars(vars_line).items():
        n = int(round((hi - lo) / step))
        v = lo + step * rng.randint(0, max(0, n))
        values[name] = int(round(v)) if is_int else round(v, 6)
    return values


def fmt_value(v) -> str:
    if isinstance(v, int) or float(v).is_integer():
        return f'{int(v):,}'.replace(',', '\u202f')
    return str(round(v, 6)).replace('.', ',')


def substitute(text: str, values: dict) -> str:
    return re.sub(r'\{([A-Za-z]\w*)\}', lambda m: fmt_value(values[m.group(1)]) if m.group(1) in values else m.group(0), text or '')


def eval_formula(expr: str, values: dict):
    """Évalue une formule arithmétique sans eval() : nombres, variables, + - * / ** %, parenthèses, sqrt/abs/round/min/max/pi."""
    import ast, math, operator
    ops = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
           ast.Pow: operator.pow, ast.Mod: operator.mod, ast.USub: operator.neg, ast.UAdd: operator.pos}
    fns = {'sqrt': math.sqrt, 'abs': abs, 'round': round, 'min': min, 'max': max}
    consts = {'pi': math.pi}

    def ev(n):
        if isinstance(n, ast.Expression):
            return ev(n.body)
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)) and not isinstance(n.value, bool):
            return n.value
        if isinstance(n, ast.Name):
            if n.id in values:
                return values[n.id]
            if n.id in consts:
                return consts[n.id]
            raise ValueError(n.id)
        if isinstance(n, ast.BinOp) and type(n.op) in ops:
            r = ev(n.right)
            if isinstance(n.op, ast.Pow) and abs(r) > 12:
                raise ValueError('puissance trop grande')
            return ops[type(n.op)](ev(n.left), r)
        if isinstance(n, ast.UnaryOp) and type(n.op) in ops:
            return ops[type(n.op)](ev(n.operand))
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in fns and not n.keywords:
            return fns[n.func.id](*[ev(a) for a in n.args])
        raise ValueError('expression non permise')
    try:
        return float(ev(ast.parse((expr or '').replace('×', '*').replace('÷', '/').replace('^', '**').replace(',', '.').strip(), mode='eval')))
    except Exception:
        return None


def draw_pairs(pairs: list, n, seed, num) -> list:
    """Appariement aléatoire (randomsamatch de Moodle) : n paires tirées dans la réserve, différentes d'un étudiant à l'autre,
    stables pour un même étudiant (même graine à l'affichage et à la correction)."""
    import random
    try:
        n = int(n)
    except (TypeError, ValueError):
        return pairs
    if not pairs or n <= 0 or n >= len(pairs):
        return pairs
    idx = sorted(random.Random(f'{seed}:{num}:tirage').sample(range(len(pairs)), n))
    return [pairs[i] for i in idx]


def substitute_formulas(text: str, values: dict) -> str:
    """« {={a}*{b}/100} » (formule entre accolades, comme calculatedmulti de Moodle) → valeur calculée pour CET étudiant."""
    def one(m):
        v = eval_formula(re.sub(r'\{([A-Za-z]\w*)\}', r'\1', m.group(1)), values)
        return fmt_value(round(v, 6)) if v is not None else m.group(0)
    return substitute(re.sub(r'\{=\s*((?:[^{}]|\{[A-Za-z]\w*\})+?)\s*\}', one, text or ''), values)


def point_in_rect(xy: str, rect) -> bool:
    try:
        x, y = [float(t) for t in str(xy).split(',')[:2]]
    except ValueError:
        return False
    x1, y1, x2, y2 = rect
    return min(x1, x2) <= x <= max(x1, x2) and min(y1, y2) <= y <= max(y1, y2)


def grade_calculee(key: dict, raw: str, vars_line: str, seed, num):
    """→ (fraction, réponse saisie, valeur attendue) ou None (pas de formule / pas de graine)."""
    f = key.get('formula')
    if not f or seed is None or not vars_line:
        return None
    want = eval_formula(f['expr'], instantiate(vars_line, seed, num))
    if want is None:
        return None
    v = parse_number(raw)
    tol = abs(want) * f['tol'] / 100.0 if f['percent'] else f['tol']
    ok = v is not None and abs(v - want) <= tol + 1e-9
    return (1.0 if ok else 0.0), (raw or '').strip(), want
