"""Types de questions ajoutés pour couvrir ceux de Moodle (qtype_match avec leurres, gapselect,
ordering, shortanswer, numerical, description) dans le langage de balises des sujets CEI.

Balises (titre de la question) et leur équivalent Moodle :
  [ASSOCIATION]  match avec leurres : éléments « - texte », propositions « A. texte » (plus nombreuses que les éléments)
  [TRI]          match plusieurs-vers-un : ligne « Catégories : X | Y », éléments « - texte »
  [ORDRE]        ordering : éléments « A. texte » donnés en désordre, l'étudiant donne le rang de chacun
  [TROUS]        gapselect : texte avec {1} {2}…, propositions communes « A. texte »
  [COURT]        shortanswer : une réponse libre d'une ligne, comparée aux réponses acceptées du barème
  [NUMERIQUE]    numerical : une valeur, comparée avec tolérance
  [DESCRIPTION]  description : texte d'information (énoncé commun), ni numéro ni note

Barème (ligne dans le bloc de la question) :
  association / tri / trous :  « Réponse : 1-H, 2-E, 3-K »   (élément-proposition, la proposition est une lettre)
  ordre :                      « Ordre : C, A, B, D »          (du 1er au dernier)
  court :                      « Réponses acceptées : Dakar | dakar »
  numerique :                  « Valeur : 12,5 (± 0,1) »  ou  « Valeur : 12,5 (± 2 %) »

Les familles « grille » (association, tri, ordre, trous) partagent le même stockage des réponses
(pq_<n>_<index de l'élément> = lettre ou rang) et la même notation proportionnelle (fraction d'éléments
justes), comme qtype_match de Moodle.
"""
import re
import unicodedata

GRID_MARKERS = ('ASSOCIATION', 'TRI', 'ORDRE', 'TROUS')
TEXT_MARKERS = ('COURT', 'NUMERIQUE')
NEW_MARKERS = GRID_MARKERS + TEXT_MARKERS
# Alternance pour les expressions régulières des lecteurs de sujet (plus longs d'abord)
MARKER_ALT = ('QCM_MULTI|QCM|VF|OUVERT|SUBOPEN|APPARIEMENT|CODE|ASSOCIATION|TRI|ORDRE|TROUS|COURT|NUMERIQUE')

GRID_BTYPE = {m: m.lower() for m in GRID_MARKERS}
TEXT_BTYPE = {'COURT': 'court', 'NUMERIQUE': 'numerique'}

ITEM_RE = re.compile(r'^\s*[-•*▪]\s+(.+)$')                      # élément « - texte »
CATS_RE = re.compile(r'^\s*Cat[ée]gories?\s*:\s*(.+)$', re.I)    # « Catégories : A | B »
GAP_RE = re.compile(r'\{(\d{1,2})\}')                             # trou {1}
DESC_RE = re.compile(r'^\s*\[DESCRIPTION\]\s*(.*)$', re.I)


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
