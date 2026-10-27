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
