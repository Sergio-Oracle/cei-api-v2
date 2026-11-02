"""Rien ne doit disparaître en silence : contrôle de COUVERTURE d'un sujet (incident du 02/11/2026, Devoir2-IA).

Les lecteurs de sujet (routes/exams.py `_parse_subject_blocks_ordered` côté serveur, `parseExamBlocks` côté navigateur) ne montrent que ce
qu'ils reconnaissent. Une ligne qu'ils ne reconnaissent pas (énoncé d'une partie, texte collé à une question…) est simplement absente de l'écran
de l'étudiant, sans la moindre alerte. Ce module relit le sujet comme l'étudiant le verra et signale :

  hidden_lines(content)      -> lignes du sujet (après la 1re question) qui n'apparaîtraient nulle part à l'écran
  dangling_references(...)   -> questions qui renvoient à un élément absent du sujet (« les trois hypothèses », « le tableau ci-dessous »…)
  coverage_issues(content)   -> les deux, sous forme d'avertissements {level:'warn', message}
"""
import re

from services import question_types as QT

_MARKER = re.compile(r'\[(?:QCM_MULTI|QCM|VF|OUVERT[ES]*|SUBOPEN|APPARIEMENT|CODE|ASSOCIATION|TRI|ORDRE|TROUS|CLOZE|ZONES|COURT|NUMERIQUE|CALCULEE|Facile|Moyen|Difficile)\]', re.I)
_LEAD = re.compile(r'^\s*(?:(?:Question|Q)\.?\s*)?\(?\d{1,3}\s*[.:)–—-]?\s*(?:(?:Q|Question)\s*\d{1,3}\s*[.:)–—-]?\s*)?', re.I)
_CHOICE_LEAD = re.compile(r'^\s*\(?[A-La-l]\)?\s*[.):-]\s+')
_NOISE = re.compile(r'^(?:[-=*─═▬]{3,}|---.*---|\[(?:IMAGE|AUDIO|VIDEO):.+\])$', re.I)
_DIRECTIVE = (QT.TIRAGE_RE, QT.UPLOAD_RE, QT.GRILLE_RE, QT.CATS_RE, QT.LABELS_RE, QT.VARS_RE, QT.DESC_RE, QT._CLOZE_ITEM_RE, QT.ITEM_RE)
_CLOSING = re.compile(r'^\W*(?:fin\b|bon courage|bonne chance|merci|good luck)', re.I)
_ANSWER_LINE = re.compile(r'^\s*(?:Bonne\s+)?R[ée]ponses?(?:\s+\w+)?\s*:', re.I)


def _norm(s: str) -> str:
    """Minuscules, sans balises ni ponctuation ni espaces : comparaison tolérante à la mise en forme."""
    s = _MARKER.sub('', s or '').lower()
    return re.sub(r'[\W_]+', '', s, flags=re.UNICODE)


def _displayed_blob(blocks: list) -> str:
    """Tout le texte qu'un étudiant peut voir, bout à bout."""
    out = []
    for b in blocks:
        for k in ('title', 'text', 'content'):
            if isinstance(b.get(k), str):
                out.append(b[k])
        out += [l for l in (b.get('extraLines') or []) if isinstance(l, str)]
        out += [c.get('text', '') for c in (b.get('choices') or [])]
        out += [f"{p.get('left', '')} {p.get('right', '')}" for p in (b.get('pairs') or [])]
        out += [i.get('text', '') for i in (b.get('items') or [])]
        out += [o.get('text', '') for o in (b.get('options') or [])]
        ctx = b.get('context') or {}
        out += [ctx.get('title', ''), ctx.get('text', '')]
    return _norm(' '.join(out))


def hidden_lines(content: str, blocks: list = None) -> list:
    """Lignes du sujet, de la 1re question à la fin, dont le texte n'est affiché nulle part."""
    from routes.exams import _parse_subject_blocks_ordered, _strip_bareme_from_content
    content = _strip_bareme_from_content(content or '')
    blocks = blocks if blocks is not None else _parse_subject_blocks_ordered(content)
    if not any(b.get('type') not in ('section', 'text', 'description') for b in blocks):
        return []
    blob = _displayed_blob(blocks)
    lines, started, hidden = content.split('\n'), False, []
    q_start = re.compile(r'^\s*(?:(?:Question|Q)\.?\s+)?\d{1,3}\s*[.:)–—-]|^\s*(?:Question|Q)\.?\s*\d')
    for l in lines:
        t = l.strip()
        if not started:
            started = bool(q_start.match(t))
        if not started or not t or _NOISE.match(t) or _ANSWER_LINE.match(t):
            continue
        if _CLOSING.match(t) or any(rx.match(t) for rx in _DIRECTIVE):
            continue
        body = _CHOICE_LEAD.sub('', _LEAD.sub('', t, count=1), count=1)
        # Texte à trous : « {1} » devient un champ de saisie, les morceaux de texte autour doivent être affichés
        parts = [_norm(x) for x in re.split(r'\{\d{1,2}\}', body)]
        parts = [x for x in parts if len(x) >= 4]
        if parts and any(x not in blob for x in parts):
            hidden.append(t)
    return hidden


# Un énoncé qui en cite un autre sans que celui-ci figure dans le sujet : l'étudiant ne peut pas répondre.
_REFS = re.compile(
    r"\b(?:les|ces|des)\s+(?:trois|deux|quatre|cinq|six|sept|huit|\d+)\s+(hypoth[eè]ses|donn[ée]es|documents|textes|tableaux|figures|sch[ée]mas|annexes|pr[ée]misses|affirmations|d[ée]clarations)\b"
    r"|\b(?:les|ces)\s+(hypoth[eè]ses|pr[ée]misses)\b"
    r"|\b(?:le|ce|du|au)\s+(texte|tableau|document|sch[ée]ma|graphique|diagramme|code|programme|extrait|cas)\s+(?:ci-dessus|ci-dessous|suivant|pr[ée]c[ée]dent|ci-joint)\b"
    r"|\b(?:la|cette|de la|à la)\s+(figure|image|carte|courbe)\s+(?:ci-dessus|ci-dessous|suivante|pr[ée]c[ée]dente|ci-jointe)\b", re.I)


def dangling_references(blocks: list) -> list:
    """Questions qui renvoient à « les trois hypothèses », « le tableau ci-dessous »… alors que rien de tel n'est affiché avec elles.
    Avertissement seulement : on ne peut pas savoir si l'élément manque ou s'il est dans une image jointe."""
    out = []
    for b in blocks:
        if b.get('type') in ('section', 'text', 'description') or not b.get('num'):
            continue
        own = ' '.join([b.get('text') or '', *(b.get('extraLines') or [])])
        m = _REFS.search(own)
        if not m:
            continue
        if b.get('media'):                       # une image / un fichier est joint à la question : la référence est probablement satisfaite
            continue
        word = next((g for g in m.groups() if g), m.group(0)).lower()
        ctx = (b.get('context') or {}).get('text', '').lower()
        stem_words = re.sub(r'[\W_]+', ' ', ' '.join(c.get('text', '') for c in (b.get('choices') or [])).lower())
        # L'élément cité apparaît-il dans l'énoncé de la partie ? (« hypothèse », « tableau »…)
        root = re.sub(r'(?:s|x)$', '', word)[:6]
        if root and root in ctx:
            continue
        out.append((b['num'], m.group(0)))
    return out


def coverage_issues(content: str) -> list:
    from routes.exams import _parse_subject_blocks_ordered, _strip_bareme_from_content
    stu = _strip_bareme_from_content(content or '')
    blocks = _parse_subject_blocks_ordered(stu)
    issues = []
    hid = hidden_lines(content, blocks)
    if hid:
        shown = ' ; '.join(f"« {h[:70]}{'…' if len(h) > 70 else ''} »" for h in hid[:4])
        issues.append({'level': 'warn', 'message':
            f"{len(hid)} ligne(s) du sujet ne seraient affichées nulle part aux étudiants : {shown}"
            f"{' …' if len(hid) > 4 else ''}. Vérifiez le format du sujet (énoncé placé avant la question, numérotation)."})
    refs = dangling_references(blocks)
    if refs:
        nums = ', '.join(f"{n}" for n, _ in refs[:8])
        issues.append({'level': 'warn', 'message':
            f"Question(s) {nums} : elles renvoient à un élément (« {refs[0][1]} »…) qui n'apparaît pas dans le sujet. "
            "Ajoutez-le avant la question : sans lui l'étudiant ne peut pas répondre."})
    return issues
