"""Questions « Maths / Programmation » avec tests automatiques — côté étudiant (24/11/2026).

  GET  /api/exam_attempts/<id>/code_info   langage et tests VISIBLES de chaque question de code de l'examen (jamais les tests cachés)
  POST /api/exam_attempts/<id>/code_run    {num, code} exécute les tests visibles dans le bac à sable (Piston) et rend le résultat de chacun

La note, elle, est calculée à la correction avec TOUS les tests (visibles et cachés) : routes/exams.py `_deterministic_grade` → services/code_sandbox.py.
"""
from flask import Blueprint, jsonify, request

from auth_paseto import paseto_required, get_current_user_id
from extensions import limiter
from models import get_session, ExamAttempt, AttemptStatus
from cache import cache_set_nx_value, cache_delete
from services import code_sandbox as SB

code_run_bp = Blueprint('code_run', __name__)


def _code_keys(session, attempt):
    """{numéro: clé « code » du barème} des questions de code de l'examen de cette tentative."""
    from routes.exams import _extract_correct_answers
    sub = attempt.exam.subject if attempt.exam else None
    keys = _extract_correct_answers((sub.rubric if sub else '') or '')
    return {num: k['code'] for num, k in keys.items() if k.get('type') == 'ext' and k.get('code')}


def _own_attempt(session, attempt_id):
    return session.query(ExamAttempt).filter_by(id=attempt_id, student_id=get_current_user_id()).first()


@code_run_bp.route('/api/exam_attempts/<int:attempt_id>/code_info', methods=['GET'])
@paseto_required
@limiter.exempt
def code_info(attempt_id):
    session = get_session()
    try:
        attempt = _own_attempt(session, attempt_id)
        if not attempt:
            return jsonify({'error': 'Tentative introuvable'}), 404
        out = {}
        for num, c in _code_keys(session, attempt).items():
            out[num] = {
                'lang': c['lang'],
                'tests': [{'n': t['n'], 'kind': t['kind'], 'input': t['input'], 'expected': t['expected']} for t in c['tests'] if not t['hidden']],
                'hidden': sum(1 for t in c['tests'] if t['hidden']),
            }
        return jsonify({'enabled': SB.enabled(), 'questions': out})
    finally:
        session.close()


@code_run_bp.route('/api/exam_attempts/<int:attempt_id>/code_run', methods=['POST'])
@paseto_required
@limiter.exempt
def code_run(attempt_id):
    data = request.get_json(silent=True) or {}
    num, code = str(data.get('num') or ''), str(data.get('code') or '')
    if len(code) > SB.MAX_CODE_CHARS:
        return jsonify({'success': False, 'error': 'too_long'}), 413
    session = get_session()
    try:
        attempt = _own_attempt(session, attempt_id)
        if not attempt:
            return jsonify({'error': 'Tentative introuvable'}), 404
        if attempt.status != AttemptStatus.IN_PROGRESS:
            return jsonify({'success': False, 'error': 'closed'}), 400
        c = _code_keys(session, attempt).get(num)
        if not c:
            return jsonify({'success': False, 'error': 'no_tests'}), 404
        tests = [t for t in c['tests'] if not t['hidden']]
        hidden = sum(1 for t in c['tests'] if t['hidden'])
    finally:
        session.close()
    if not SB.enabled():
        return jsonify({'success': False, 'error': 'unavailable'}), 503
    # Une exécution à la fois par étudiant (aucun quota : il peut tester autant de fois qu'il veut, l'une après l'autre).
    lock = f'cei:coderun:{attempt_id}'
    if cache_set_nx_value(lock, 1, 40) is False:
        return jsonify({'success': False, 'error': 'busy'}), 429
    try:
        if not code.strip():
            return jsonify({'success': True, 'results': [], 'passed': 0, 'total': len(tests), 'hidden': hidden, 'empty': True})
        res = SB.run_tests(c['lang'], code, tests)
    finally:
        cache_delete(lock)
    if any(r['status'] == 'unavailable' for r in res):
        return jsonify({'success': False, 'error': 'unavailable'}), 503
    by_n = {t['n']: t for t in tests}
    results = [{'n': r['n'], 'ok': r['ok'], 'status': r['status'], 'kind': by_n[r['n']]['kind'], 'input': by_n[r['n']]['input'][:2000],
                'expected': r['expected'][:2000], 'got': r['got'][:2000], 'stderr': (r['stderr'] or '')[:1500]} for r in res]
    return jsonify({'success': True, 'results': results, 'passed': sum(1 for r in res if r['ok']), 'total': len(res), 'hidden': hidden})
