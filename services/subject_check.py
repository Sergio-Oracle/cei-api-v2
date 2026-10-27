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


def analyze(content: str) -> dict:
    from routes.exams import _parse_subject_blocks_ordered   # import tardif : évite le cycle routes ↔ services
    content = content or ''
    issues = []
    if not content.strip():
        return {'ok': False, 'questions': 0, 'by_type': {}, 'issues': [
            {'level': 'error', 'message': "Le sujet est vide : les étudiants n'auront aucune question."}]}

    blocks = [b for b in _parse_subject_blocks_ordered(content) if b.get('type') not in ('section', 'text')]
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
            issues.append({'level': 'warn', 'message': f"Numéro(s) de question en double : {', '.join(map(str, dup))}."})
        try:
            ints = sorted({int(n) for n in nums})
            missing = [i for i in range(ints[0], ints[-1] + 1) if i not in ints]
            if missing:
                issues.append({'level': 'warn', 'message':
                    f"Numéro(s) manquant(s) dans la suite : {', '.join(map(str, missing[:10]))}."})
        except ValueError:
            pass

    ok = not any(i['level'] == 'error' for i in issues)
    return {'ok': ok, 'questions': len(blocks), 'by_type': by_type, 'issues': issues}


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
