"""Fidélité de la reproduction d'un sujet importé : rien ne doit disparaître, rien ne doit être inventé.

Deux contrôles complémentaires, jamais bloquants (ils informent l'enseignant AVANT l'examen) :
  lost_lines(original, final)   -> lignes du document d'origine absentes du sujet reconstruit (contrôle exact, sans IA)
  ai_review(original, final)    -> relecture COMPLÈTE par l'IA : questions manquantes, type erroné, propositions perdues,
                                   texte ajouté (une IA seule ne décide jamais : elle signale, l'enseignant tranche)
"""
import json
import re

from services.type_detector import _FOOTER_RE, _RECAP_RE

_WORD_RE = re.compile(r'[\w’\'-]{3,}', re.U)
_IGNORED_RE = re.compile(r'^\s*(?:---.*---|N°?\s+Affirmations?\s+Vrai\s+Faux|Notions?\s+D[ée]finitions?|Bonne\s+r[ée]ponse.*|R[ée]ponse\s*:.*|Corrig[ée].*|\[?(?:IMAGE|AUDIO|VIDEO):.*)\s*$', re.I)


def _tokens(s):
    return [w.lower().strip("’'-") for w in _WORD_RE.findall(s or '')]


def _shingles(s, n=2):
    t = _tokens(s)
    return {' '.join(t[i:i + n]) for i in range(len(t) - n + 1)} if len(t) >= n else set(t)


def lost_lines(original, final, limit=8):
    """Lignes de l'original dont plus de 40 % des suites de deux mots n'existent plus dans le sujet reconstruit
    (comparer des mots isolés laisserait passer une ligne disparue dont les mots reviennent ailleurs)."""
    have = _shingles(final)
    out = []
    skip_rest = False
    for raw in (original or '').split('\n'):
        l = re.sub(r'\s*[☐□▢]\s*indice\s*', ' ', raw).strip()
        if _RECAP_RE.match(l):
            skip_rest = True                    # récapitulatif de notation et pages d'indices : retirés volontairement
        if skip_rest or not l or _FOOTER_RE.match(l) or _IGNORED_RE.match(l):
            continue
        sh = _shingles(l)
        if len(_tokens(l)) < 3 or not sh:
            continue
        missing = [x for x in sh if x not in have]
        if len(missing) / len(sh) > 0.4:
            out.append(l[:160])
            if len(out) >= limit:
                break
    return out


_SYSTEM = (
    "Tu es un relecteur rigoureux d'épreuves d'examen universitaire. On te donne le TEXTE ORIGINAL d'un sujet (extrait d'un Word ou d'un PDF) "
    "et le SUJET RECONSTRUIT par la plateforme (questions numérotées, balises de type entre crochets : [QCM], [QCM_MULTI], [VF], [OUVERT], "
    "[ASSOCIATION], [TRI], [ORDRE], [TROUS], [CLOZE], [COURT]…). Compare-les question par question et signale UNIQUEMENT les vrais défauts : "
    "question de l'original absente ou fusionnée, type erroné (ex. un QCM balisé [OUVERT]), proposition perdue ou tronquée, énoncé modifié ou inventé, "
    "partie du sujet manquante. Ne signale PAS : la renumérotation, les pieds de page, le récapitulatif de notation, les lignes « Bonne réponse » retirées "
    "volontairement, ni les différences de mise en forme, ni le tableau récapitulatif de l'original et ses étiquettes, ni la page d'indices des mots croisés (retirée volontairement : elle donnait les réponses). Une grille de mots croisés devient une question [CLOZE] avec une ligne « Grille : ». Un tableau Vrai/Faux devient des questions [VF]. "
    "Réponds UNIQUEMENT par un JSON : {\"ok\": true|false, \"issues\": [{\"question\": \"numéro ou titre\", \"probleme\": \"phrase courte en français\"}]}. "
    "Si tout est fidèle : {\"ok\": true, \"issues\": []}."
)


def ai_review(original, final, call_ai, max_chars=26000):
    """Relecture complète par l'IA (best effort : toute panne rend {'ok': None})."""
    try:
        user = f"=== TEXTE ORIGINAL ===\n{(original or '')[:max_chars]}\n\n=== SUJET RECONSTRUIT ===\n{(final or '')[:max_chars]}"
        raw = call_ai(_SYSTEM, user, temperature=0.0, max_tokens=1500, label='subject-review')
        m = re.search(r'\{.*\}', raw or '', re.S)
        txt = re.sub(r',\s*([}\]])', r'\1', m.group(0)) if m else ''      # virgules finales tolérées
        data = json.loads(txt) if txt else {}
        issues = [{'question': str(i.get('question', ''))[:60], 'probleme': str(i.get('probleme', ''))[:200]}
                  for i in (data.get('issues') or []) if isinstance(i, dict) and i.get('probleme')][:12]
        return {'ok': (not issues) if data else None, 'issues': issues}
    except Exception as e:                       # jamais bloquer un import à cause de la relecture
        return {'ok': None, 'issues': [], 'error': str(e)[:120]}
