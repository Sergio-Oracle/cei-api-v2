"""Lecteur universel de documents Word (phase C1 + C2, 14/11/2026).

Pourquoi : `utils._docx_lines` ne garde que `paragraph.text`. Tout ce que Word « sait » mais n'écrit pas dans le texte est
perdu : numérotation automatique (1. 2. 3. / a) b) c)), listes, niveaux de titre, retours de ligne dans un paragraphe,
frontière exacte entre deux paragraphes. Un professeur qui numérote avec la fonction « Liste numérotée » de Word obtenait
donc un sujet sans numéros de question.

Principe : on laisse pandoc (lecteur de référence, maintenu, qui comprend numbering.xml) produire l'ARBRE du document
(JSON), puis on le réécrit nous-mêmes en texte canonique CEI — celui que `_parse_subject_blocks_ordered` et le normaliseur
connaissent déjà :
  - listes ordonnées : le numéro/lettre est RÉGÉNÉRÉ depuis les attributs de la liste (début, style, délimiteur) ;
  - un paragraphe qui est un titre de section (Exercice / Partie / Section / titre Word) est isolé par une ligne vide :
    il ne peut plus être collé à l'énoncé ou à la question qui suit ;
  - tableaux : mêmes règles que l'ancien lecteur (une colonne = ses paragraphes, sinon une ligne par rangée), pour que
    les normaliseurs de tableaux Vrai/Faux et association restent valables ;
  - rien n'est perdu : si le texte produit couvre moins de 97 % des mots du lecteur d'origine, on le garde lui.

Aucune dépendance obligatoire : sans pandoc (binaire système ou paquet `pypandoc_binary`) ou en cas d'erreur / délai,
`extract()` renvoie None et l'appelant garde l'ancien lecteur.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess

log = logging.getLogger(__name__)

PANDOC_TIMEOUT_S = 60
COVERAGE_MIN = 0.97

_SECTION_TITLE_RE = re.compile(
    r"^\s*(?:exercice|partie|section|probl[èe]me|cas pratique|chapitre|th[èe]me|volet)\s*[\dIVXivx]*\b", re.I)


# ── pandoc ─────────────────────────────────────────────────────────────────────

def pandoc_path() -> str | None:
    p = shutil.which('pandoc')
    if p:
        return p
    try:
        import pypandoc  # paquet pypandoc_binary : embarque pandoc
        return pypandoc.get_pandoc_path()
    except Exception:
        return None


def _run_pandoc(filepath: str) -> dict | None:
    exe = pandoc_path()
    if not exe:
        return None
    try:
        r = subprocess.run([exe, '-f', 'docx', '-t', 'json', '--wrap=none', '--track-changes=accept', filepath],
                           capture_output=True, timeout=PANDOC_TIMEOUT_S)
    except Exception as e:
        log.warning("doc_structure: pandoc a échoué (%s)", e)
        return None
    if r.returncode != 0:
        log.warning("doc_structure: pandoc code %s : %s", r.returncode, r.stderr[:200])
        return None
    try:
        return json.loads(r.stdout.decode('utf-8'))
    except Exception as e:
        log.warning("doc_structure: JSON pandoc illisible (%s)", e)
        return None


# ── inlines ────────────────────────────────────────────────────────────────────

def _inlines(items) -> str:
    out = []
    for it in items or []:
        t, c = it.get('t'), it.get('c')
        if t == 'Str':
            out.append(c)
        elif t in ('Space', 'SoftBreak'):
            out.append(' ')
        elif t == 'LineBreak':
            out.append('\n')
        elif t in ('Emph', 'Strong', 'Underline', 'Strikeout', 'Superscript', 'Subscript', 'SmallCaps'):
            out.append(_inlines(c))
        elif t == 'Quoted':
            q = '« %s »' if c[0].get('t') == 'DoubleQuote' else '‘%s’'
            out.append(q % _inlines(c[1]))
        elif t == 'Cite':
            out.append(_inlines(c[1]))
        elif t == 'Code':
            out.append(c[1])
        elif t == 'Math':
            out.append(c[1])
        elif t == 'Link':
            out.append(_inlines(c[1]))
        elif t == 'Span':
            out.append(_inlines(c[1]))
        elif t == 'RawInline':
            pass
        # Image, Note : ignorés (aucun texte de l'énoncé)
    return ''.join(out)


def _clean(line: str) -> str:
    return re.sub(r'[ \t]{2,}', ' ', line).strip()


# ── numérotation ──────────────────────────────────────────────────────────────

def _roman(n: int) -> str:
    vals = [(1000, 'm'), (900, 'cm'), (500, 'd'), (400, 'cd'), (100, 'c'), (90, 'xc'), (50, 'l'), (40, 'xl'),
            (10, 'x'), (9, 'ix'), (5, 'v'), (4, 'iv'), (1, 'i')]
    s = ''
    for v, r in vals:
        while n >= v:
            s += r
            n -= v
    return s


def _alpha(n: int) -> str:
    s = ''
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(97 + r) + s
    return s


def _marker(n: int, style: str, delim: str) -> str:
    if style == 'LowerAlpha':
        body = _alpha(n)
    elif style == 'UpperAlpha':
        body = _alpha(n).upper()
    elif style == 'LowerRoman':
        body = _roman(n)
    elif style == 'UpperRoman':
        body = _roman(n).upper()
    else:
        body = str(n)
    if delim == 'OneParen':
        return f"{body})"
    if delim == 'TwoParens':
        return f"({body})"
    return f"{body}."


# ── blocs ─────────────────────────────────────────────────────────────────────

def _is_section_title(block, text: str) -> bool:
    """Titre Word, ou paragraphe court dont toute la mise en forme est en gras et qui dit « Exercice 2 », « Partie B »…"""
    if block.get('t') == 'Header':
        return True
    if len(text) > 160 or '\n' in text:
        return False
    return bool(_SECTION_TITLE_RE.match(text))


def _cell_blocks(cell):
    """Contenu d'une case, ancien format (liste de blocs) ou nouveau (attr, align, rowspan, colspan, blocs)."""
    if cell and isinstance(cell[-1], list) and len(cell) == 5:
        return cell[4], cell[3]          # (blocs, colspan)
    return cell, 1


def _table_lines(c) -> list[str]:
    if len(c) == 6:                      # pandoc ≥ 2.10 : attr, caption, colspecs, head, bodies, foot
        ncols = len(c[2])
        rows = list(c[3][1])
        for body in c[4]:
            rows += list(body[2]) + list(body[3])
        rows += list(c[5][1])
        grid = [row[1] for row in rows]
    else:                                # pandoc ≤ 2.9 : caption, aligns, widths, header, rows
        ncols = len(c[1])
        grid = ([c[3]] if c[3] else []) + list(c[4])
    lines: list[str] = []
    if ncols == 1:
        for row in grid:
            for cell in row:
                lines += _render_blocks(_cell_blocks(cell)[0], in_cell=True)
        return lines
    for row in grid:
        parts = []
        for cell in row:
            blocks, _ = _cell_blocks(cell)
            txt = ' '.join(l.strip() for l in _render_blocks(blocks, in_cell=True) if l.strip())
            if txt:
                parts.append(txt)
        if parts:
            lines.append(' '.join(parts))
    return lines


def _render_list_item(blocks, marker: str, depth: int) -> list[str]:
    inner = _render_blocks(blocks, depth=depth + 1, in_cell=True)
    if not inner:
        return [marker]
    # le marqueur se colle à la première ligne non vide ; les autres lignes restent telles quelles
    for k, l in enumerate(inner):
        if l.strip():
            inner[k] = f"{marker} {l.strip()}"
            break
    return inner


def _render_blocks(blocks, depth: int = 0, in_cell: bool = False) -> list[str]:
    out: list[str] = []
    for b in blocks or []:
        t, c = b.get('t'), b.get('c')
        if t in ('Para', 'Plain'):
            text = '\n'.join(_clean(x) for x in _inlines(c).split('\n'))
            if not text.strip():
                continue
            out += text.split('\n')
            if not in_cell and _is_section_title(b, text.strip()):
                out.append('')
        elif t == 'Header':
            text = _clean(_inlines(c[2]))
            if text:
                out += ['', text, '']
        elif t == 'BlockQuote':
            out += _render_blocks(c, depth, in_cell)
        elif t == 'Div':
            out += _render_blocks(c[1], depth, in_cell)
        elif t == 'OrderedList':
            (start, style, delim), items = c[0], c[1]
            style = style.get('t') if isinstance(style, dict) else style
            delim = delim.get('t') if isinstance(delim, dict) else delim
            for k, item in enumerate(items):
                out += _render_list_item(item, _marker(start + k, style, delim), depth)
        elif t == 'BulletList':
            for item in c:
                out += _render_list_item(item, '-', depth)
        elif t == 'DefinitionList':
            for term, defs in c:
                out.append(_clean(_inlines(term)))
                for d in defs:
                    out += _render_blocks(d, depth + 1, True)
        elif t == 'LineBlock':
            for ln in c:
                out.append(_clean(_inlines(ln)))
        elif t == 'CodeBlock':
            out += c[1].split('\n')
        elif t == 'Table':
            out += _table_lines(c)
            if not in_cell:
                out.append('')
        elif t == 'Figure':
            out += _render_blocks(c[2], depth, in_cell)
        # HorizontalRule, RawBlock, Null : rien
    return out


def render(ast: dict) -> str:
    lines = _render_blocks(ast.get('blocks', []))
    text = '\n'.join(lines)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


# ── contrôle de non-perte ─────────────────────────────────────────────────────

def _tokens(s: str) -> dict:
    d: dict = {}
    for w in re.findall(r'\w+', s.lower()):
        d[w] = d.get(w, 0) + 1
    return d


def coverage(reference: str, candidate: str) -> float:
    """Part (0-1) des mots de `reference` retrouvés dans `candidate` (avec multiplicité)."""
    ref, cand = _tokens(reference), _tokens(candidate)
    total = sum(ref.values())
    if not total:
        return 1.0
    kept = sum(min(n, cand.get(w, 0)) for w, n in ref.items())
    return kept / total


def extract(filepath: str, legacy_text: str | None = None) -> str | None:
    """Texte canonique d'un .docx via pandoc, ou None (pandoc absent / erreur / perte de contenu) : l'appelant garde alors
    l'ancien lecteur. `legacy_text` = sortie de l'ancien lecteur, sert de garde-fou de non-perte."""
    ast = _run_pandoc(filepath)
    if not ast:
        return None
    try:
        text = render(ast)
    except Exception as e:
        log.warning("doc_structure: rendu impossible (%s)", e)
        return None
    if not text:
        return None
    if legacy_text:
        cov = coverage(legacy_text, text)
        if cov < COVERAGE_MIN:
            log.warning("doc_structure: couverture %.1f %% < %.0f %% — ancien lecteur conservé", cov * 100, COVERAGE_MIN * 100)
            return None
    return text


def engine() -> str:
    """Moteur demandé par l'environnement : 'legacy' (défaut) ou 'pandoc'."""
    return (os.getenv('CEI_DOCX_ENGINE') or 'legacy').strip().lower()
