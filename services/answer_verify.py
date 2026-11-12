"""Vérification de FOND des réponses du barème : une seconde lecture indépendante.

Les contrôles de forme (generation_check, subject_check) garantissent qu'une question a ses propositions et une clé lisible ; ils
ne disent pas si la clé est JUSTE. Examen 170 (12/11/2026) : « activer les {1} du {2} » avait pour clé 1-A, 2-C (« sauvegardes
manuelles ») alors que la bonne réponse est 2-B (« système d'exploitation »).

Ici, l'IA répond à chaque question fermée SANS voir le barème ; toute divergence avec la clé est signalée à l'enseignant
(« à relire »). Rien n'est modifié automatiquement : en cas de désaccord, c'est l'enseignant qui tranche. Jamais bloquant : si
la vérification échoue (IA indisponible, réponse illisible), aucune alerte n'est produite.
"""
import re

_Q_RE = re.compile(r'(?ms)^[ \t]*Question\s+(\d{1,3})\s*[—\-–:.](.*?)(?=^[ \t]*Question\s+\d{1,3}\s*[—\-–:.]|^[ \t]*Partie\s+[—\-–]|^[ \t]*[═=─]{5,}|\Z)')
_MARK_RE = re.compile(r'\[(QCM_MULTI|QCM|VF|TROUS|ASSOCIATION)\]', re.I)
_ANS_RE = re.compile(r'(?m)^[^\d\n]*?(\d{1,3})\s*[:=]\s*([^»\n]+?)\s*»?\s*$')      # tolère « [QCM] « 12: B » »
MAX_QUESTIONS = 80


def _closed_questions(content: str) -> list:
    out = []
    for m in _Q_RE.finditer(content or ''):
        body = m.group(2)
        mk = _MARK_RE.search(body.split('\n', 1)[0])
        if mk:
            out.append((m.group(1), mk.group(1).upper(), ('Question ' + m.group(1) + ' —' + body).strip()))
    return out[:MAX_QUESTIONS]


def _norm_key(marker: str, key: dict):
    """Clé du barème sous une forme comparable (ou None si absente / illisible)."""
    if not key:
        return None
    if marker in ('QCM', 'QCM_MULTI') and key.get('type') == 'qcm':
        return frozenset(str(x).upper() for x in key.get('letters') or [])
    if marker == 'VF' and key.get('type') == 'vf':
        return str(key.get('value', '')).strip().lower()
    if marker in ('TROUS', 'ASSOCIATION') and key.get('pairs'):
        return tuple(sorted((int(k), str(v).upper()) for k, v in key['pairs'].items()))
    return None


def _norm_answer(marker: str, raw: str):
    raw = (raw or '').strip()
    if not raw or '?' in raw:
        return None
    if marker in ('QCM', 'QCM_MULTI'):
        letters = re.findall(r'\b([A-H])\b', raw.upper())
        return frozenset(letters) if letters else None
    if marker == 'VF':
        m = re.search(r'\b(vrai|faux)\b', raw, re.I)
        return m.group(1).lower() if m else None
    pairs = re.findall(r'(\d{1,2})\s*[-–—=:]\s*([A-Ha-h])\b', raw)
    return tuple(sorted((int(a), b.upper()) for a, b in pairs)) if pairs else None


def _show(marker: str, v) -> str:
    if marker in ('QCM', 'QCM_MULTI'):
        return ', '.join(sorted(v))
    if marker == 'VF':
        return v.capitalize()
    return ', '.join(f"{a}-{b}" for a, b in v)


def verify_keys(content: str, rubric: str, ask=None, only_nums=None) -> list:
    """Phrases d'alerte pour l'enseignant (liste vide si tout concorde ou si la vérification n'a pas pu se faire)."""
    try:
        from routes.exams import _extract_correct_answers
        questions = _closed_questions(content)
        if only_nums is not None:
            only = {str(n) for n in only_nums}
            questions = [q for q in questions if q[0] in only]
        keys = _extract_correct_answers(rubric or '')
        todo = [(n, mk, txt) for n, mk, txt in questions if _norm_key(mk, keys.get(n)) is not None]
        if not todo:
            return []
        if ask is None:
            from services.ai_service import call_ai_simple as ask
        prompt = (
            "Tu es un enseignant universitaire rigoureux. Réponds à chaque question ci-dessous, sans commentaire.\n"
            "FORMAT STRICT : une ligne par question, dans l'ordre, sous la forme NUMÉRO: RÉPONSE — sans guillemets ni crochets.\n"
            "Exemples de lignes :\n"
            "12: B\n"
            "13: A, C\n"
            "14: Vrai\n"
            "15: 1-A, 2-C\n"
            "(question à choix : la lettre ; à choix multiples : les lettres ; Vrai/Faux : Vrai ou Faux ; texte à trous ou association : "
            "numéro du trou ou de l'élément, tiret, lettre de la proposition.)\n"
            "Si tu n'es pas certain d'une réponse, écris par exemple « 16: ? ». N'écris rien d'autre.\n\n"
            + '\n\n'.join(txt for _, _, txt in todo))
        raw = ask(prompt) or ''
        given = {m.group(1): m.group(2) for m in _ANS_RE.finditer(raw)}
        out = []
        for n, mk, _ in todo:
            a = _norm_answer(mk, given.get(n))
            k = _norm_key(mk, keys.get(n))
            if a is None or k is None or a == k:
                continue
            out.append(f"Question {n} : à relire — le barème indique « {_show(mk, k)} », une seconde vérification trouve « {_show(mk, a)} ». "
                       "Vérifiez la bonne réponse avant d'enregistrer.")
        return out
    except Exception as e:
        print(f"[answer_verify] vérification non faite : {e}")
        return []
