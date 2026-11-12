"""Contrôle de lisibilité d'un sujet AVANT et PENDANT l'examen.

Un sujet issu d'un PDF peut avoir un format que les lecteurs de questions ne comprennent pas
(« Question 1 [QCM] » collé, propositions coupées sur plusieurs lignes…). Sans ce contrôle,
l'étudiant se retrouve devant une zone de réponse vide (incident du 19/10/2026, examen 127).

  analyze(content)             -> rapport {ok, questions, by_type, issues[]} (aucune écriture)
  alert_if_unreadable(exam,…)  -> alerte unique au personnel si les étudiants ne reçoivent AUCUNE question
"""
import logging
import re

_log = logging.getLogger('cei.subject_check')
_MARKER_RE = re.compile(r'\[(QCM_MULTI|QCM|VF|OUVERT|SUBOPEN|APPARIEMENT|CODE|ASSOCIATION|TRI|ORDRE|TROUS|COURT|NUMERIQUE)\]', re.I)
_QUESTION_WORD_RE = re.compile(r'^\s*(?:Question|Q)\.?\s*\d+', re.I | re.M)


def analyze(content: str, rubric: str = None) -> dict:
    from routes.exams import _parse_subject_blocks_ordered   # import tardif : évite le cycle routes ↔ services
    content = content or ''
    issues = []
    if not content.strip():
        return {'ok': False, 'questions': 0, 'by_type': {}, 'issues': [
            {'level': 'error', 'message': "Le sujet est vide : les étudiants n'auront aucune question."}]}

    all_blocks = _parse_subject_blocks_ordered(content)
    blocks = [b for b in all_blocks if b.get('type') not in ('section', 'text')]
    # Un exercice / une partie sans AUCUNE question lue (tableau Vrai/Faux, grille…) : l'étudiant verrait un titre vide
    # (incident du 30/10/2026, examen 142). Sections consécutives = la première est vide.
    for idx, b in enumerate(all_blocks):
        if b.get('type') != 'section':
            continue
        nxt = all_blocks[idx + 1] if idx + 1 < len(all_blocks) else None
        # (un récapitulatif de fin de document donne aussi des titres consécutifs : seuls comptent ceux suivis plus loin d'une question)
        if nxt is not None and nxt.get('type') == 'section' and any(x.get('type') != 'section' for x in all_blocks[idx + 1:]):
            issues.append({'level': 'error', 'message':
                f"« {b.get('title')} » : aucune question reconnue sous ce titre, l'étudiant verrait un exercice vide. "
                "Mettez les questions au format « 1. énoncé » avec les propositions « A. … » (un tableau n'est pas lu)."})
    by_type = {}
    for b in blocks:
        by_type[b['type']] = by_type.get(b['type'], 0) + 1

    n_markers = len(_MARKER_RE.findall(content))
    n_headers = len(_QUESTION_WORD_RE.findall(content))
    expected = max(n_markers, n_headers)

    if not blocks:
        issues.append({'level': 'error', 'message':
            "Aucune question reconnue dans le sujet : les étudiants ne verraient qu'une zone de réponse vide. "
            "Vérifiez la numérotation (« Question 1. », « 1) »…) et les marqueurs [QCM] / [OUVERT]."})
    elif expected and len(blocks) < expected:
        issues.append({'level': 'error', 'message':
            f"{expected} question(s) annoncée(s) dans le sujet mais seulement {len(blocks)} reconnue(s) : "
            "certaines questions ne s'afficheraient pas."})

    for b in blocks:
        if b['type'] in ('qcm', 'qcm_multi') and len(b.get('choices') or []) < 2:
            issues.append({'level': 'error', 'message':
                f"Question {b.get('num')} : {len(b.get('choices') or [])} proposition(s) reconnue(s) sur un QCM "
                "(propositions coupées ou mal numérotées A., B., C. ?)."})

    # Propositions incohérentes : la plupart des QCM en ont N et certains moins → propositions probablement perdues
    # (ligne coupée, lettre mal formée). Seulement un avertissement : un QCM à 3 propositions peut être voulu.
    counts = [len(b.get('choices') or []) for b in blocks if b['type'] in ('qcm', 'qcm_multi')]
    if len(counts) >= 3:
        mode = max(set(counts), key=counts.count)
        if counts.count(mode) >= len(counts) * 0.6:
            odd = [b.get('num') for b in blocks if b['type'] in ('qcm', 'qcm_multi') and 2 <= len(b.get('choices') or []) < mode]
            if odd:
                issues.append({'level': 'warn', 'message':
                    f"Question(s) {', '.join(map(str, odd))} : moins de propositions que les autres QCM ({mode}) — "
                    "vérifiez qu'aucune proposition n'a été perdue."})

    nums = [b.get('num') for b in blocks if b.get('num')]
    if nums:
        dup = sorted({n for n in nums if nums.count(n) > 1}, key=lambda x: int(x) if str(x).isdigit() else 0)
        if dup:
            # Les réponses sont rangées par numéro (pq_<n>) : deux questions de même numéro s'écrasent, et la notation
            # lit une question de moins que l'affichage (sujet 102 : 30 notées pour 37 affichées).
            issues.append({'level': 'error', 'message':
                f"Numéro(s) de question en double : {', '.join(map(str, dup))} — deux questions de même numéro "
                "partageraient la même réponse. Renumérotez-les de 1 à N sans répétition."})
        try:
            ints = sorted({int(n) for n in nums})
            missing = [i for i in range(ints[0], ints[-1] + 1) if i not in ints]
            if missing:
                issues.append({'level': 'warn', 'message':
                    f"Numéro(s) manquant(s) dans la suite : {', '.join(map(str, missing[:10]))}."})
        except ValueError:
            pass

    if rubric is not None and (rubric or '').strip():
        try:
            issues += _rubric_issues(content, rubric, blocks)
        except Exception as e:                   # le contrôle du barème ne doit jamais empêcher l'examen
            _log.warning('contrôle du barème indisponible: %s', e)

    try:
        from services.subject_coverage import coverage_issues
        issues += coverage_issues(content)
    except Exception as e:                       # le contrôle de couverture ne doit jamais empêcher l'examen
        _log.warning('contrôle de couverture indisponible: %s', e)

    ok = not any(i['level'] == 'error' for i in issues)
    return {'ok': ok, 'questions': len(blocks), 'by_type': by_type, 'issues': issues}


def _rubric_issues(content: str, rubric: str, blocks: list) -> list:
    """Barème d'un sujet enregistré : ce que le contrôle de l'éditeur (rubric_check) vérifie déjà (bonne réponse absente ou hors
    propositions, points absents, total différent), plus le modèle de format laissé tel quel et le total ≠ 20 (note ramenée sur 20)."""
    from routes.exams import _question_points_map
    from services.generation_check import rubric_issues
    from services.rubric_check import grading_report
    issues = []
    for num, msg in rubric_issues(content, rubric):
        issues.append({'level': 'error', 'message': f"Question {num} : {msg} — la correction automatique ne pourra pas la noter."})
    seen = {i['message'] for i in issues}
    for i in grading_report(content, rubric)['issues']:
        if i['message'] not in seen and not (i['level'] == 'warn' and 'ne correspond pas à la somme' in i['message']):
            issues.append({'level': i['level'], 'message': i['message']})
    issues += _grid_issues(rubric, blocks, {i['message'] for i in issues})
    points = _question_points_map(content, rubric)
    posed = {str(b.get('num')) for b in blocks if b.get('num')}
    total = round(sum(v for n, v in points.items() if n in posed), 2)
    if points and abs(total - 20) > 0.01:
        issues.append({'level': 'warn', 'message':
            f"Les questions posées totalisent {total:g} points, pas 20 : la note affichée sera ramenée sur 20 "
            f"({total:g} points = 20/20). Ajustez les points si ce n'est pas voulu."})
    return issues


def _grid_issues(rubric: str, blocks: list, seen: set) -> list:
    """Questions à grille (texte à trous, association, classement) d'un sujet enregistré : propositions présentes et clé du barème
    cohérente avec la question. Examen 170 (12/11/2026) : un texte à trous sans aucune proposition et une clé qui ne
    correspondait pas aux trous étaient passés avec un simple avertissement sur le total des points."""
    from services import question_types as QT
    from services.rubric_check import _blocks
    rblocks = _blocks(rubric or '')
    out = []

    def err(num, msg):
        m = f"Question {num} : {msg}"
        if m not in seen:
            out.append({'level': 'error', 'message': m})

    for b in blocks:
        t, num = b.get('type'), str(b.get('num'))
        if t not in ('trous', 'association', 'tri'):
            continue
        items = b.get('items') or []
        options = b.get('options') or []
        letters = {str(o.get('letter', '')).upper() for o in options if isinstance(o, dict)}
        if t in ('trous', 'association') and len(letters) < 2:
            err(num, "aucune proposition à choisir n'a été reconnue : l'étudiant ne pourrait pas répondre. "
                     "Ajoutez les propositions « A. … », « B. … » sous l'énoncé.")
        if not items:
            err(num, "aucun élément à compléter n'a été reconnu (trou {1}, {2}… ou ligne « - élément »).")
        rm = rblocks.get(num)
        pairs = QT.extract_keys_from_block(rm.group(0)).get('pairs') if rm else None
        if pairs and items:
            if sorted(pairs) != list(range(1, len(items) + 1)):
                err(num, f"le barème donne {len(pairs)} réponse(s) mais la question a {len(items)} élément(s) à compléter : "
                         "la correction automatique serait fausse.")
            elif t in ('trous', 'association') and letters and set(pairs.values()) - letters:
                bad = ', '.join(sorted(set(pairs.values()) - letters))
                err(num, f"la réponse du barème ({bad}) n'existe pas parmi les propositions du sujet.")
    return out


def alert_if_unreadable(exam, pages: dict) -> None:
    """Appelé à chaque chargement des pages par un étudiant : si le sujet n'a AUCUNE question lisible,
    prévient le personnel une seule fois (verrou Redis 6 h) au lieu de laisser les étudiants le découvrir seuls."""
    try:
        if pages.get('p1_blocks') or pages.get('p2_items'):
            return
        content = (exam.subject.content if exam and exam.subject else '') or ''
        if not content.strip():
            return
        from cache import cache_set_nx
        if not cache_set_nx(f'cei:subject_unreadable:{exam.id}', 6 * 3600):
            return
        import notif_bus
        notif_bus.notify_exam(
            exam.id, 'subject_unreadable', 'Sujet illisible pour les étudiants',
            f"Examen « {exam.title} » : aucune question n'est reconnue dans le sujet, les étudiants voient une zone de "
            "réponse vide. Corrigez le sujet ou prévenez-les immédiatement.", priority='urgent')
        _log.error('sujet illisible : examen %s (%s)', exam.id, exam.title)
    except Exception as e:   # ne jamais gêner l'étudiant pour une alerte
        _log.warning('alert_if_unreadable: %s', e)
