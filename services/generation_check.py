"""Contrôle de ce que l'IA vient de générer : une question complète a son énoncé, ses propositions ET une clé de réponse réelle.

Incident constaté le 09/11/2026 (sujet « Réseaux de neurones », 18 questions) : quand la réponse de l'IA est tronquée, le serveur
complétait le barème avec le MODÈLE du format (« Bonne réponse : X) — [justification courte] ») comme si c'était une vraie
clé, et une question QCM restait sans aucune proposition. Aucune erreur n'était signalée : la correction automatique ne
pouvait pas noter ces questions et l'enseignant ne voyait le défaut qu'en lisant tout le sujet.

`defects()` ne signale que des défauts CERTAINS (placeholder, proposition absente, clé hors des propositions, format de clé
illisible) : jamais de jugement sur le fond de la question. Utilisé (1) pendant la génération, pour refaire une question
défectueuse, et (2) par le contrôle de lisibilité d'un sujet existant.
"""
import re

# Fragments du gabarit de format que l'IA ne doit jamais laisser tels quels
_PLACEHOLDER_RE = re.compile(
    r"\[(?:justification courte|Ce qui est attendu|Titre court|Énoncé[^\]]*|Premier choix[^\]]*|Deuxième choix[^\]]*|"
    r"Troisième choix[^\]]*|Quatrième choix[^\]]*|Choix court|Proposition[^\]]*|Notion \d|Affirmation à évaluer|"
    r"Situation[^\]]*|Catégorie \d|Étape[^\]]*|Terme ou élément[^\]]*|Définition[^\]]*)\]", re.I)
# Notes explicatives du GABARIT de barème (« Réponse : 1-A, 2-C  (numéro du trou, tiret, lettre de la proposition) ») : si elles figurent
# dans un barème, la ligne est l'EXEMPLE de format recopié tel quel, pas une vraie clé (examen 170 du 12/11/2026 : deux textes à
# trous notés avec la clé de l'exemple ; un étudiant qui répondait juste aurait été noté faux).
_TEMPLATE_NOTE_RE = re.compile(
    r"\(\s*(?:numéro d[ue] (?:l['’]élément|trou)\s*,\s*tiret|lettres des éléments du premier au dernier|"
    r"valeur attendue et tolérance|COURT\s*:\s*réponses acceptées|formule avec les variables de l['’]énoncé)[^)]*\)"
    r"|réponse principale\s*\|\s*variante acceptable|Vrai\s*\(ou Faux\)|\[critère attendu\]|\[critère[^\]]*\]", re.I)
_KEY_X_RE = re.compile(r'(?:Bonnes?\s+réponses?)\s*:\s*X\)', re.I)
_CRITERE_Z_RE = re.compile(r'Critère\s*:\s*Z\s*pts', re.I)

_CHOICE_RE = re.compile(r'(?m)^\s*([A-H])\s*[\).]\s+\S')
_KEY_QCM_RE = re.compile(r'Bonne\s+réponse\s*:\s*([A-H])\s*[\).]', re.I)
_KEY_MULTI_RE = re.compile(r'Bonnes?\s+réponses?\s*:\s*((?:[A-H]\s*\)\s*,?\s*)+)', re.I)
_KEY_VF_RE = re.compile(r'Réponse\s*:\s*(Vrai|Faux)', re.I)
_KEY_PAIRS_RE = re.compile(r'Réponse\s*:\s*((?:\d+\s*-\s*[A-H]\s*,?\s*)+)', re.I)
_KEY_ORDER_RE = re.compile(r'Ordre\s*:\s*([A-H](?:\s*,\s*[A-H])*)', re.I)
_ITEM_RE = re.compile(r'(?m)^\s*-\s+\S')
_HOLE_RE = re.compile(r'\{(\d+)\}')


def placeholder_in(text: str):
    """Fragment de gabarit laissé tel quel dans `text` (ou None)."""
    m = _PLACEHOLDER_RE.search(text or '')
    if m:
        return m.group(0)
    if _KEY_X_RE.search(text or ''):
        return 'Bonne réponse : X)'
    if _CRITERE_Z_RE.search(text or ''):
        return 'Critère : Z pts'
    m = _TEMPLATE_NOTE_RE.search(text or '')
    if m:
        return "exemple de format recopié"
    return None


def defects(type_key: str, content_block: str, rubric_block: str) -> list:
    """Défauts certains d'une question générée (liste de phrases pour l'enseignant, vide si la question est complète)."""
    out = []
    c, r = content_block or '', rubric_block or ''
    ph = placeholder_in(c)
    if ph:
        out.append(f"l'énoncé contient encore du texte de gabarit ({ph})")
    ph = placeholder_in(r)
    if ph:
        out.append(f"le barème n'est pas rempli ({ph})")
    letters = [m.group(1).upper() for m in _CHOICE_RE.finditer(c)]
    if type_key in ('qcm', 'qcm_multi'):
        if len(set(letters)) < 2:
            out.append("moins de 2 propositions : l'énoncé est incomplet")
        if type_key == 'qcm':
            k = _KEY_QCM_RE.search(r)
            if not k:
                out.append("aucune bonne réponse (lettre) dans le barème")
            elif letters and k.group(1).upper() not in letters:
                out.append(f"la bonne réponse {k.group(1).upper()}) n'est pas parmi les propositions")
        else:
            k = _KEY_MULTI_RE.search(r)
            keys = re.findall(r'([A-H])\s*\)', k.group(1)) if k else []
            if not keys:
                out.append("aucune bonne réponse (lettres) dans le barème")
            elif letters and any(x.upper() not in letters for x in keys):
                out.append("une bonne réponse du barème n'est pas parmi les propositions")
    elif type_key == 'vf':
        if not _KEY_VF_RE.search(r):
            out.append("la réponse « Vrai » ou « Faux » manque dans le barème")
    elif type_key in ('association', 'tri', 'trous'):
        pairs = _KEY_PAIRS_RE.search(r)
        n_expected = len(_ITEM_RE.findall(c)) if type_key != 'trous' else len(set(_HOLE_RE.findall(c)))
        if not pairs:
            out.append("la clé « Réponse : 1-A, 2-B… » manque dans le barème")
        elif n_expected and len(re.findall(r'\d+\s*-\s*[A-H]', pairs.group(1))) != n_expected:
            out.append("le nombre de réponses du barème ne correspond pas au nombre d'éléments")
        if type_key == 'trous' and not _HOLE_RE.search(c):
            out.append("aucun trou {1} {2}… dans le texte")
        if type_key in ('association', 'trous') and len(set(letters)) < 2:
            out.append("propositions lettrées absentes")
        if pairs and type_key in ('association', 'trous') and letters:
            used = set(x.upper() for x in re.findall(r'\d+\s*-\s*([A-H])', pairs.group(1)))
            if used - set(letters):
                out.append("une réponse du barème n'est pas parmi les propositions")
        if pairs and n_expected:
            nums = sorted(int(x) for x in re.findall(r'(\d+)\s*-\s*[A-H]', pairs.group(1)))
            if nums != list(range(1, n_expected + 1)):
                out.append("les numéros du barème ne correspondent pas aux éléments de la question")
    elif type_key == 'ordre':
        if len(set(letters)) < 3:
            out.append("moins de 3 éléments à remettre en ordre")
        o = _KEY_ORDER_RE.search(r)
        if not o:
            out.append("la ligne « Ordre : C, A, B… » manque dans le barème")
        elif letters and sorted(set(re.findall(r'[A-H]', o.group(1).upper()))) != sorted(set(letters)):
            out.append("l'ordre du barème ne contient pas exactement les éléments proposés")
    elif type_key == 'appariement':
        if c.count('→') < 2:
            out.append("moins de 2 paires « gauche → droite »")
    elif type_key == 'court':
        if not re.search(r'Réponses?\s+acceptées?\s*:\s*\S', r, re.I):
            out.append("« Réponses acceptées : … » manque dans le barème")
    elif type_key == 'numerique':
        if not re.search(r'Valeur\s*:\s*-?\d', r, re.I):
            out.append("« Valeur : … » manque dans le barème")
    elif type_key == 'cloze':
        if not re.search(r'Champs\s*:\s*\d\s*=', r, re.I):
            out.append("« Champs : 1=… » manque dans le barème")
    elif type_key == 'calculee':
        if not re.search(r'Formule\s*:\s*\S', r, re.I) or not re.search(r'Variables\s*:\s*\S', c, re.I):
            out.append("la formule ou la ligne « Variables : … » manque")
    elif type_key in ('open', 'code', 'subopen'):
        if len(re.sub(r'\W+', '', r)) < 12:
            out.append("barème vide")
    return out


def rubric_issues(content: str, rubric: str) -> list:
    """Défauts d'un barème déjà enregistré, question par question (pour le contrôle de lisibilité d'un sujet existant).
    Renvoie [(numéro, phrase)]."""
    out = []
    for m in re.finditer(r'(?ms)^\s*Question\s+(\d{1,3})\b[^\n]*\n(.*?)(?=^\s*Question\s+\d{1,3}\b|^\s*[─═]{5,}|\Z)', rubric or ''):
        num, body = m.group(1), m.group(2)
        ph = placeholder_in(body)
        if ph:
            out.append((num, f"barème non rempli ({ph})"))
    return out
