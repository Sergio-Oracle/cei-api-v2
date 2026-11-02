"""Barème ↔ sujet : ce que la CORRECTION fera réellement de chaque question, et modification guidée du barème.

La correction automatique (routes/exams.py `_deterministic_grade`) lit la bonne réponse et les points DANS LE BARÈME, par numéro de question,
et passe en silence les questions qu'elle ne sait pas noter. Quand un enseignant modifie le sujet (propositions déplacées, question ajoutée…),
le barème peut ne plus correspondre : mauvaise note ou question non notée, sans aucun signe. Ce module :

  grading_report(content, rubric)      -> une ligne par question : bonne réponse lue, points, mode de correction (automatique / IA), problèmes
  apply_edits(content, rubric, edits)  -> barème mis à jour (bonne réponse et/ou points de questions précises), sans toucher au reste
"""
import re

_LETTERS = 'ABCDEFGHIJKL'
_AUTO_OTHER = ('APPARIEMENT', 'ASSOCIATION', 'TRI', 'ORDRE', 'TROUS', 'CLOZE', 'ZONES', 'COURT', 'NUMERIQUE', 'CALCULEE')
_TOTAL_RE = re.compile(r'TOTAL\s*:\s*([\d.,]+)\s*/\s*([\d.,]+)\s*points?', re.I)
_POINTS_IN_HEAD_RE = re.compile(r'\(\s*(\d+(?:[.,]\d+)?)\s*pts?\s*\)', re.I)
_ANSWER_TEXT_RE = re.compile(r'[Bb]onnes?\s+r[ée]ponses?\s*:\s*\(?([A-La-l])\)?\s*[.):-]?\s*(.*)$')


def _norm(s: str) -> str:
    return re.sub(r'[\W_]+', '', (s or '').lower(), flags=re.UNICODE)


def _same(written: str, choice: str) -> bool:
    """Le texte écrit dans le barème désigne-t-il cette proposition ? Égalité, ou l'un contient l'autre (explication qui reprend la proposition,
    texte tronqué). Les très courts textes (« oui », « non ») ne comptent que s'ils sont identiques."""
    a, b = _norm(written), _norm(choice)
    if not a or not b:
        return False
    return a == b or (len(b) >= 4 and b in a) or (len(a) >= 4 and a in b)


def _fmt_pts(x: float) -> str:
    return str(int(x)) if float(x).is_integer() else str(round(x, 2)).replace('.', ',')


def _blocks(rubric: str) -> dict:
    from routes.exams import _RUBRIC_Q_BLOCK_RE
    return {m.group(1): m for m in _RUBRIC_Q_BLOCK_RE.finditer(rubric or '')}


def grading_report(content: str, rubric: str) -> dict:
    from routes.exams import (_parse_subject_questions_for_grading, _question_points_map, _extract_correct_answers)
    questions = _parse_subject_questions_for_grading(content or '')
    points = _question_points_map(content or '', rubric or '')
    keys = _extract_correct_answers(rubric or '')
    blocks = _blocks(rubric or '')
    rows, issues = [], []

    def add(level, num, msg):
        issues.append({'level': level, 'num': num, 'message': f"Question {num} : {msg}"})

    for num in sorted(questions, key=lambda n: int(n)):
        q = questions[num]
        marker = q.get('marker')
        choices = [{'letter': l, 'text': t} for l, t in sorted((q.get('choices') or {}).items())]
        key = keys.get(num)
        pts = points.get(num)
        kind = {'QCM': 'qcm', 'QCM_MULTI': 'qcm_multi', 'VF': 'vf'}.get(marker) or ('auto' if marker in _AUTO_OTHER else 'ai')
        row = {'num': num, 'kind': kind, 'marker': marker, 'text': (q.get('text') or '')[:160], 'choices': choices,
               'points': pts, 'letters': sorted(key['letters']) if key and key.get('letters') else [],
               'value': key.get('value') if key and key.get('type') == 'vf' else None, 'status': 'ok', 'mode': 'auto' if kind != 'ai' else 'ia'}
        if kind in ('qcm', 'qcm_multi', 'vf'):
            if pts is None:
                row['status'] = 'error'; add('error', num, "aucun nombre de points dans le barème : elle ne sera pas notée automatiquement.")
            if kind == 'vf':
                if not key or key.get('type') != 'vf':
                    row['status'] = 'error'; add('error', num, "aucune réponse Vrai/Faux dans le barème : elle ne sera pas notée automatiquement.")
            else:
                if not key or key.get('type') != 'qcm' or not key.get('letters'):
                    row['status'] = 'error'; add('error', num, "aucune bonne réponse dans le barème : elle ne sera pas notée automatiquement.")
                else:
                    have = {c['letter'] for c in choices}
                    bad = sorted(set(key['letters']) - have)
                    if bad:
                        row['status'] = 'error'; add('error', num, f"la bonne réponse du barème ({', '.join(bad)}) n'existe pas parmi les propositions du sujet.")
                    if kind == 'qcm' and len(key['letters']) != 1:
                        row['status'] = 'error'; add('error', num, "un QCM à une réponse a plusieurs bonnes réponses dans le barème.")
                    # Le barème recopie parfois le texte de la bonne proposition (« B) non »). S'il ne correspond plus à la proposition de CETTE lettre
                    # mais correspond clairement à une AUTRE, les propositions ont été déplacées : la lettre du barème est fausse.
                    # Un barème qui écrit une explication après la lettre (« B) — Le pooling max résume… ») n'est PAS une copie : on ne compare pas.
                    m = blocks.get(num)
                    am = _ANSWER_TEXT_RE.search(m.group(0)) if m else None
                    if am and kind == 'qcm' and am.group(2).strip() and len(key['letters']) == 1:
                        L, written = am.group(1).upper(), am.group(2).strip()
                        now = next((c['text'] for c in choices if c['letter'] == L), None)
                        if now is not None and not _same(written, now):
                            moved = next((c['letter'] for c in choices if c['letter'] != L and _same(written, c['text'])), None)
                            if moved:
                                row['status'] = 'error'; row['suggest'] = moved
                                add('error', num, f"le barème attend {L}) « {written[:60]} » mais la proposition {L} du sujet est maintenant « {now[:60]} » ; "
                                                  f"ce texte est devenu la proposition {moved}.")
        elif kind == 'auto':
            if not key:
                row['status'] = 'warn'; add('warn', num, "pas de réponse attendue lisible dans le barème : elle sera corrigée par l'IA.")
        else:   # question rédigée : corrigée par l'IA d'après les critères du barème
            if num not in blocks:
                row['status'] = 'warn'; add('warn', num, "aucun critère de correction dans le barème : l'IA la notera sans barème.")
            if pts is None:
                row['status'] = 'warn'; add('warn', num, "aucun nombre de points dans le barème.")
        rows.append(row)

    extra = sorted(int(n) for n in blocks if n not in questions)
    if extra and questions:
        issues.append({'level': 'warn', 'num': None, 'message': f"Le barème décrit des questions absentes du sujet : {', '.join(map(str, extra))} (elles ne comptent pas dans la note)."})
    total = round(sum(r['points'] for r in rows if r['points'] is not None), 2)
    tm = _TOTAL_RE.search(rubric or '')
    declared = float(tm.group(1).replace(',', '.')) if tm else None
    if declared is not None and abs(declared - total) > 0.01 and rows:
        issues.append({'level': 'warn', 'num': None, 'message': f"Le total écrit dans le barème ({_fmt_pts(declared)}) ne correspond pas à la somme des points des questions ({_fmt_pts(total)})."})
    return {'rows': rows, 'total_points': total, 'declared_total': declared, 'issues': issues,
            'auto_count': sum(1 for r in rows if r['mode'] == 'auto'), 'ai_count': sum(1 for r in rows if r['mode'] == 'ia')}


def apply_edits(content: str, rubric: str, edits: dict) -> str:
    """Met à jour le barème pour des questions précises : {num: {'points': 2, 'letters': ['B'], 'value': 'Vrai'}}."""
    from routes.exams import (_RUBRIC_QCM_ANSWER_RE, _RUBRIC_VF_ANSWER_RE, _parse_subject_questions_for_grading, _question_points_map)
    questions = _parse_subject_questions_for_grading(content or '')
    text = rubric or ''

    def answer_line(num, ed):
        q = questions.get(num) or {}
        if ed.get('value') in ('Vrai', 'Faux'):
            return f"  • Réponse : {ed['value']}"
        letters = [l.upper() for l in (ed.get('letters') or []) if l.upper() in _LETTERS]
        if not letters:
            return None
        if len(letters) == 1:
            return f"  • Bonne réponse : {letters[0]}) {(q.get('choices') or {}).get(letters[0], '')}".rstrip()
        return f"  • Bonnes réponses : {', '.join(letters)}"

    def edit_block(block, num, ed):
        lines = block.split('\n')
        if ed.get('points') is not None:
            p = _fmt_pts(float(ed['points']))
            if _POINTS_IN_HEAD_RE.search(lines[0]):
                lines[0] = _POINTS_IN_HEAD_RE.sub(f'({p} pts)', lines[0], count=1)
            else:
                lines[0] = re.sub(r'^(Question\s+\d{1,3})', rf'\1 ({p} pts)', lines[0], count=1)
        new = answer_line(num, ed)
        if new:
            idx = next((i for i, l in enumerate(lines) if _RUBRIC_QCM_ANSWER_RE.search(l) or _RUBRIC_VF_ANSWER_RE.search(l)), None)
            if idx is not None:
                lines[idx] = new
            else:
                end = len(lines)
                while end > 1 and not lines[end - 1].strip():
                    end -= 1
                lines.insert(end, new)
        return '\n'.join(lines)

    blocks = _blocks(text)
    # blocs existants : de la fin vers le début pour garder les positions valides
    for num in sorted((n for n in edits if n in blocks), key=lambda n: -blocks[n].start()):
        m = blocks[num]
        text = text[:m.start()] + edit_block(m.group(0), num, edits[num]) + text[m.end():]
    # questions sans bloc : ajoutées avant le séparateur / TOTAL, sinon à la fin
    created = []
    for num in sorted((n for n in edits if n not in blocks), key=lambda n: int(n)):
        ed = edits[num]
        pts = ed.get('points')
        if pts is None:
            pts = _question_points_map(content, text).get(num, 1)
        head = f"Question {num} ({_fmt_pts(float(pts))} pts) :"
        created.append('\n'.join([head] + ([answer_line(num, ed)] if answer_line(num, ed) else [])) + '\n')
    if created:
        sep = re.search(r'\n─+\s*\nTOTAL', text)
        add = '\n'.join(created)
        if sep:
            text = text[:sep.start()].rstrip('\n') + '\n\n' + add + text[sep.start():]
        else:
            text = text.rstrip('\n') + ('\n\n' if text.strip() else '') + add
    # le total écrit dans le barème suit la somme des points
    if _TOTAL_RE.search(text):
        total = round(sum(v for k, v in _question_points_map(content, text).items() if k in questions), 2)
        text = _TOTAL_RE.sub(f'TOTAL : {_fmt_pts(total)} / {_fmt_pts(total)} points', text, count=1)
    return text
