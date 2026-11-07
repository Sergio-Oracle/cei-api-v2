"""Nettoyage du commentaire de correction avant affichage à l'étudiant ou à l'enseignant.

La correction par l'IA produit un texte de travail : marqueurs markdown « ### Question N » et ligne technique
« Points obtenus : X » (total des seules questions rédigées, que l'on prendrait à tort pour la note finale).
Ces éléments ne sont utiles qu'au calcul de la note ; ils ne doivent jamais être affichés tels quels.
"""
import re

_POINTS_LINE = re.compile(r'(?mi)^[ \t]*Points\s+obtenus\s*:\s*[\d.,]+[ \t]*$\n?')
_MD_HEADING = re.compile(r'(?m)^[ \t]*#{1,6}[ \t]*')
_MANY_BLANKS = re.compile(r'\n{3,}')


def clean_ai_feedback(text):
    if not text or not isinstance(text, str):
        return text
    out = _POINTS_LINE.sub('', text)
    out = _MD_HEADING.sub('', out)
    return _MANY_BLANKS.sub('\n\n', out).strip()


_FIRST_BLOCK = re.compile(r'(?m)^[ \t]*#{1,6}[ \t]*Question[ \t]+\d')


def strip_ai_preamble(text):
    """La consigne interdit tout texte avant le premier bloc « ### Question N » ; certains modèles y écrivent quand même
    leur raisonnement (« Regardons le JSON fourni… », « Le format demande… »), qui se retrouvait dans le retour affiché à
    l'étudiant (6 copies de l'examen 140). On le retire : le retour commence au premier bloc de question. Sans bloc
    reconnu, le texte est laissé tel quel (jamais de perte silencieuse d'un retour au format inattendu)."""
    if not text or not isinstance(text, str):
        return text
    m = _FIRST_BLOCK.search(text)
    return text[m.start():] if m and m.start() > 0 else text


# ── Filet à la source : jamais de raisonnement de l'IA ni de bloc pour une question déjà notée automatiquement ──────────
_LEAK = re.compile(r'pq_\d+|JSON|Regardons|Relisons|Vérifions|Attendez|Format\s*:|Je vais générer|Note interne|Re-correction', re.I)
_NEUTRAL = "Retour détaillé indisponible pour cette question : la note de la question est conservée, à vérifier par l'enseignant."
_AI_HEAD = re.compile(r'(?m)^[ \t]*#{1,6}[ \t]*Question[ \t]+(\d{1,3})[ \t]*\n[ \t]*Points\s*:\s*[\d.,]+\s*/\s*[\d.,]+[ \t]*\n')
_AI_FINAL = re.compile(r'(?m)^[ \t]*Points\s+obtenus\s*:\s*[\d.,]+[ \t]*$')


def sanitize_ai_result(text, excluded=()):
    """Nettoie la réponse brute de l'IA AVANT d'en extraire les notes et de l'enregistrer :
    1. tout texte avant le premier bloc « ### Question N » est retiré (strip_ai_preamble) ;
    2. un bloc écrit pour une question déjà notée automatiquement (`excluded`) est supprimé : l'IA n'a pas à la noter, et son
       « 0.00/0.25 » contredisait le texte (« bonne réponse ») et faussait le détail par question (examen 140) ;
    3. un bloc qui contient du raisonnement (« Attendez », « Regardons », « Note interne », JSON de la copie…) est remplacé par une
       phrase neutre — sauf s'il s'agit d'une simple note interne finale : le retour qui la précède est alors gardé.
    La ligne finale « Points obtenus : X » est conservée. Sans bloc reconnu, le texte est laissé tel quel."""
    text = strip_ai_preamble(text)
    if not text or not isinstance(text, str):
        return text
    excluded = {str(x) for x in (excluded or ())}
    marks = list(_AI_HEAD.finditer(text))
    if not marks:
        return text
    out, dropped, cleaned = [text[:marks[0].start()]], [], []
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        body, tail = text[m.end():end], ''
        f = _AI_FINAL.search(body)
        if f:
            body, tail = body[:f.start()], body[f.start():]
        if m.group(1) in excluded:
            dropped.append(m.group(1)); out.append(tail); continue
        if _LEAK.search(body):
            keep, first = [], ''
            for ln in body.split('\n'):
                if _LEAK.search(ln):
                    first = ln; break
                keep.append(ln)
            kept = '\n'.join(keep).strip()
            only_note = bool(re.match(r'^\W*Note interne', first.strip(), re.I)) and len(kept) >= 40 and not _LEAK.search(kept)
            body = (kept if only_note else _NEUTRAL) + "\n\n"
            cleaned.append(m.group(1))
        out += [m.group(0), body, tail]
    if dropped or cleaned:
        print(f"[retour IA] blocs supprimés (questions déjà notées) : {dropped} ; blocs nettoyés (raisonnement) : {cleaned}")
    return ''.join(out)
