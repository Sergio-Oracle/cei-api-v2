"""Le barème suit le sujet (14/11/2026).

Incident (examen 181) : le responsable d'examen a supprimé les questions 16 à 20 dans l'éditeur ; le barème, lui, est resté à 20 questions.
Quand le texte d'un sujet change, le barème doit changer avec lui, sans que personne n'ait à y penser :

  - question supprimée          -> son bloc de barème disparaît
  - questions renumérotées      -> chaque bloc suit SA question (retrouvée par son énoncé et ses propositions, jamais par son numéro)
  - propositions déplacées      -> la lettre de la bonne réponse est recalculée d'après le TEXTE de la bonne proposition
  - total                       -> ramené au total d'avant (20 sur 20) quand le sujet ne donne pas lui-même ses points
  - question nouvelle ou trop modifiée pour être reconnue -> signalée (« à renseigner »), jamais devinée

Appelé à l'enregistrement d'un sujet dont le TEXTE change alors que le barème n'a pas été touché dans le même geste : un barème
retouché à la main par l'enseignant n'est jamais réécrit. Sans effet quand rien d'utile ne change (identité) : le barème est alors
renvoyé tel quel, au caractère près.
"""
from __future__ import annotations

import difflib
import re

_LOWER = re.compile(r'[\W_]+', re.U)
_TOTAL_LINE_RE = re.compile(r'(?im)^[ \t*#_]*(?:TOTAL|Total)\s*:[^\n]*\n?')
_KEY_LETTER_RE = re.compile(r'(Bonnes?\s+r[ée]ponses?\s*:\s*\(?)([A-La-l])(\)?)')
MATCH_MIN = 0.80


def _n(s: str) -> str:
    return _LOWER.sub('', (s or '').lower())


def _ratio(a: str, b: str) -> float:
    a, b = _n(a), _n(b)
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()


def _sig(q: dict) -> str:
    # indépendante de l'ORDRE des propositions : des propositions déplacées ne doivent pas faire perdre la question
    return '|'.join(sorted(_n(t) for t in (q.get('choices') or {}).values()))


def _score(old_q: dict, new_q: dict) -> float:
    if (old_q.get('marker') or '') != (new_q.get('marker') or ''):
        return 0.0
    stem = _ratio(old_q.get('text'), new_q.get('text'))
    if not _sig(old_q) and not _sig(new_q):
        return stem
    return 0.6 * stem + 0.4 * _ratio(_sig(old_q), _sig(new_q))


def _match(old_qs: dict, new_qs: dict) -> dict:
    """{numéro nouveau: numéro ancien}, appariement glouton par meilleur score (chaque ancienne question sert une seule fois)."""
    pairs = sorted(((_score(o, n), nn, on) for nn, n in new_qs.items() for on, o in old_qs.items()), key=lambda t: (-t[0], t[1], t[2]))
    used_new, used_old, out = set(), set(), {}
    for sc, nn, on in pairs:
        if sc < MATCH_MIN:
            break
        if nn in used_new or on in used_old:
            continue
        used_new.add(nn); used_old.add(on); out[nn] = on
    return out


def _fmt(v: float) -> str:
    s = f"{float(v):.2f}".rstrip('0').rstrip('.')
    return (s or '0').replace('.', ',') if False else (s or '0')


def sync(old_content: str, old_rubric: str, new_content: str, target: float | None = None) -> tuple:
    """Retourne (nouveau barème, rapport). Rapport : changed, removed, added, renumbered, key_changes, review, old_total, new_total, summary."""
    from routes.exams import _parse_subject_questions_for_grading, _extract_correct_answers, _RUBRIC_Q_BLOCK_RE
    from services.points_total import current_total, document_gives_points, rescale_points
    rep = {'changed': False, 'removed': [], 'added': [], 'renumbered': {}, 'key_changes': [], 'review': [],
           'old_total': None, 'new_total': None, 'summary': ''}
    try:
        old_qs = _parse_subject_questions_for_grading(old_content or '')
        new_qs = _parse_subject_questions_for_grading(new_content or '')
        blocks = {m.group(1): m for m in _RUBRIC_Q_BLOCK_RE.finditer(old_rubric or '')}
        if not old_qs or not new_qs or not blocks:
            return old_rubric, rep                      # rien de lisible à suivre : on ne touche à rien
        mapping = _match(old_qs, new_qs)               # nouveau -> ancien
        removed = sorted((n for n in old_qs if n not in mapping.values()), key=int)
        added = sorted((n for n in new_qs if n not in mapping), key=int)
        renum = {o: n for n, o in mapping.items() if o != n}
        # lettres de bonne réponse : recalculées d'après le TEXTE de la bonne proposition
        old_keys = _extract_correct_answers(old_rubric or '')
        new_letter = {}
        for nn, on in mapping.items():
            k = old_keys.get(on)
            if not k or k.get('type') != 'qcm' or len(k.get('letters') or ()) != 1:
                continue
            L = next(iter(k['letters']))
            oc, nc = old_qs[on].get('choices') or {}, new_qs[nn].get('choices') or {}
            if oc.get(L) is None or not nc:
                continue
            if nc.get(L) is not None and _n(oc[L]) == _n(nc[L]):
                continue                                # même lettre, même texte exactement
            exact = [l for l, t in nc.items() if _n(t) == _n(oc[L])]
            if len(exact) == 1:
                best_letter = exact[0]
            else:                                       # texte légèrement retouché : la proposition la plus proche, si elle se détache nettement
                ranked = sorted(((_ratio(oc[L], t), l) for l, t in nc.items()), reverse=True)
                best_letter = ranked[0][1] if ranked[0][0] >= 0.9 and (len(ranked) < 2 or ranked[0][0] - ranked[1][0] >= 0.03) else None
            if best_letter is None:
                rep['review'].append(f"Question {nn} : la bonne proposition a changé de texte, vérifiez la bonne réponse du barème.")
            elif best_letter != L:
                new_letter[nn] = best_letter
                rep['key_changes'].append({'num': nn, 'old': L, 'new': best_letter})
        if not removed and not added and not renum and not new_letter and not rep['review']:
            return old_rubric, rep                      # identité : barème renvoyé tel quel
        # ── réassemblage : en-tête d'origine, un bloc par question posée, dans l'ordre du nouveau sujet
        first = min(m.start() for m in blocks.values())
        header = re.sub(r'[*#]+', '', (old_rubric or '')[:first]).rstrip()
        parts = []
        for nn in sorted(new_qs, key=int):
            on = mapping.get(nn)
            if on is None or on not in blocks:
                continue
            b = _TOTAL_LINE_RE.sub('', blocks[on].group(0)).rstrip()
            b = re.sub(r'\*+', '', b)
            if on != nn:
                b = re.sub(r'Question\s+' + re.escape(on), f'Question {nn}', b, count=1)
            if nn in new_letter:
                b = _KEY_LETTER_RE.sub(lambda m: f"{m.group(1)}{new_letter[nn]}{m.group(3)}", b, count=1)
            parts.append(b)
        old_total = current_total(old_content or '', old_rubric or '')[0]
        want = float(target) if target else (old_total if old_total > 0 else 20.0)
        new_rubric = (header + '\n\n' if header else '') + '\n\n'.join(parts) + f"\n\nTotal : {_fmt(want)} points"
        if not document_gives_points(new_content or ''):
            new_total, pm = current_total(new_content or '', new_rubric)
            posed = [pm[n] for n in new_qs if pm.get(n)]
            uniform = bool(posed) and max(posed) - min(posed) < 0.011
            exact = bool(posed) and abs((want / len(posed)) * 4 - round((want / len(posed)) * 4)) < 1e-9     # want / n est un multiple de 0,25
            if abs(new_total - want) > 0.01 and parts and (exact or not uniform):
                _c, new_rubric, _ = rescale_points(new_content or '', new_rubric, want)
            elif abs(new_total - want) > 0.01 and parts:
                # Questions de même poids dont le total visé ne se partage pas en quarts de point (20 / 15 = 1,333…) : on garde le poids d'origine
                # (aucune question favorisée) et on écrit le vrai total ; la note finale est de toute façon ramenée sur 20 (points obtenus / total × 20).
                new_rubric = re.sub(r'(?m)^Total\s*:[^\n]*$', f"Total : {_fmt(new_total)} points", new_rubric)
                rep['normalised_on_20'] = True
        rep['new_total'] = current_total(new_content or '', new_rubric)[0]
        rep['old_total'] = old_total
        for n in added:
            rep['review'].append(f"Question {n} : nouvelle ou trop modifiée pour être reconnue — renseignez sa bonne réponse dans le barème.")
        rep.update(changed=True, removed=removed, added=added, renumbered=renum)
        bits = []
        if removed:
            bits.append(f"{len(removed)} question(s) retirée(s) du barème (anciennes n° {', '.join(removed)})")
        if renum:
            bits.append(f"{len(renum)} question(s) renumérotée(s) avec leur bonne réponse")
        if rep['key_changes']:
            bits.append(f"{len(rep['key_changes'])} bonne(s) réponse(s) recalculée(s) après déplacement des propositions")
        if abs((rep['new_total'] or 0) - (old_total or 0)) > 0.01 or removed:
            bits.append(f"total des points : {_fmt(rep['new_total'] or 0)} (avant : {_fmt(old_total or 0)})")
        if rep.get('normalised_on_20'):
            bits.append("poids identiques conservés : la note est ramenée sur 20 automatiquement")
        rep['summary'] = "Barème ajusté automatiquement : " + '; '.join(bits) + '.' + (f" À vérifier : {len(rep['review'])} point(s)." if rep['review'] else '')
        return new_rubric, rep
    except Exception as e:                              # jamais bloquant : l'enregistrement du sujet doit toujours aboutir
        print(f"[rubric_sync] barème non ajusté : {e}")
        return old_rubric, rep
