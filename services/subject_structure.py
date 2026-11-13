"""Structure lisible d'un sujet : groupes (titre + énoncé + questions). Chaque question appartient à UN seul groupe.

Sert (1) à montrer à l'enseignant ce que verront les étudiants (« Exercice 1 : Q1 à Q5 · Exercice 2 : Q6 à Q20 »), (2) de signature
de non-régression sur les vrais sujets (scripts/test_corpus_reel.py). Aucune écriture, aucune dépendance au navigateur."""


def groups(blocks: list) -> list:
    """`blocks` : questions lues (sans sections), dans l'ordre. Une suite de questions au même couple (titre, énoncé) forme un groupe."""
    out, key = [], object()
    for b in blocks:
        ctx = b.get('context') or {}
        k = (ctx.get('title') or '', (ctx.get('text') or '').strip())
        if k != key:
            out.append({'title': k[0], 'statement': k[1][:160], 'statement_chars': len(k[1]), 'questions': []})
            key = k
        out[-1]['questions'].append(str(b.get('num')))
    return out


def label(g: dict) -> str:
    """« Exercice 1 : Q1 à Q5 » — libellé court d'un groupe."""
    qs = g['questions']
    span = f"Q{qs[0]}" if len(qs) == 1 else f"Q{qs[0]} à Q{qs[-1]}"
    name = g['title'] or ('énoncé commun' if g['statement_chars'] else 'sans énoncé')
    return f"{name} : {span}"
