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
