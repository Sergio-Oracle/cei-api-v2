"""Questions « Maths / Programmation » avec tests automatiques (24/11/2026) — l'exécution se fait DANS LE NAVIGATEUR, jamais sur nos serveurs.

Étudiant :
  GET  /api/exam_attempts/<id>/code_info      langage et tests VISIBLES de chaque question de code (jamais les tests cachés) ; il les exécute lui-même (« Tester mon code »)
Enseignant / responsable d'examen :
  GET  /api/exam_attempts/<id>/code_tests     TOUS les tests (visibles + cachés) + la réponse de l'étudiant + le dernier résultat enregistré
  POST /api/exam_attempts/<id>/code_results   sorties obtenues par le navigateur → verdict recalculé ICI, enregistré, note de la question mise à jour
  GET  /api/online_exams/<id>/code_pending    copies de l'examen dont des questions de code n'ont pas encore été testées (exécution groupée)

La note : routes/exams.py `_deterministic_grade` utilise les résultats enregistrés (services/code_results.py) ; sans résultat la question reste confiée à l'IA / au professeur.
"""
import json

from flask import Blueprint, jsonify, request

from auth_paseto import paseto_required, get_current_user_id
from extensions import limiter
from models import get_session, ExamAttempt, OnlineExam, AttemptStatus, User, UserRole
from services import question_types as QT, code_results as CR

code_run_bp = Blueprint('code_run', __name__)
MAX_CODE_CHARS = 65536


def _code_keys(subject):
    """{numéro: clé « code » du barème} des questions de code du sujet."""
    from routes.exams import _extract_correct_answers
    keys = _extract_correct_answers((subject.rubric if subject else '') or '')
    return {num: k['code'] for num, k in keys.items() if k.get('type') == 'ext' and k.get('code')}


def _answers(attempt) -> dict:
    try:
        d = json.loads(attempt.answers) if attempt.answers else {}
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _staff(session, attempt_id):
    """(utilisateur, tentative, None) si l'utilisateur courant peut corriger cette copie, sinon (None, None, réponse d'erreur)."""
    from routes.exams import can_view_exam
    from sqlalchemy.orm import joinedload
    uid = get_current_user_id()
    user = session.query(User).filter_by(id=uid).first()
    if not user or user.role not in (UserRole.PROFESSOR, UserRole.ADMIN, UserRole.RESPONSABLE_EXAMEN):
        return None, None, (jsonify({'error': 'Accès non autorisé'}), 403)
    attempt = session.query(ExamAttempt).options(joinedload(ExamAttempt.exam).joinedload(OnlineExam.subject)).filter_by(id=attempt_id).first()
    if not attempt:
        return None, None, (jsonify({'error': 'Tentative non trouvée'}), 404)
    if user.role == UserRole.PROFESSOR and not can_view_exam(session, attempt.exam, uid):
        return None, None, (jsonify({'error': 'Accès non autorisé'}), 403)
    return user, attempt, None


@code_run_bp.route('/api/exam_attempts/<int:attempt_id>/code_info', methods=['GET'])
@paseto_required
@limiter.exempt
def code_info(attempt_id):
    session = get_session()
    try:
        attempt = session.query(ExamAttempt).filter_by(id=attempt_id, student_id=get_current_user_id()).first()
        if not attempt:
            return jsonify({'error': 'Tentative introuvable'}), 404
        out = {}
        for num, c in _code_keys(attempt.exam.subject if attempt.exam else None).items():
            out[num] = {
                'lang': c['lang'],
                'runnable': c['lang'] in QT.CODE_RUNNABLE,
                'tests': [{'n': t['n'], 'kind': t['kind'], 'input': t['input'], 'expected': t['expected']} for t in c['tests'] if not t['hidden']],
                'hidden': sum(1 for t in c['tests'] if t['hidden']),
            }
        return jsonify({'questions': out})
    finally:
        session.close()


@code_run_bp.route('/api/exam_attempts/<int:attempt_id>/code_tests', methods=['GET'])
@paseto_required
@limiter.exempt
def code_tests(attempt_id):
    session = get_session()
    try:
        _u, attempt, err = _staff(session, attempt_id)
        if err:
            return err
        ans = _answers(attempt)
        saved = CR.load_detail(session, attempt.id)
        out = []
        for num, c in sorted(_code_keys(attempt.exam.subject).items(), key=lambda kv: int(kv[0]) if str(kv[0]).isdigit() else 0):
            code = str(ans.get(f'pq_{num}') or '')
            sha = QT.code_sha(code)
            s = saved.get(num)
            out.append({
                'num': num, 'lang': c['lang'], 'runnable': c['lang'] in QT.CODE_RUNNABLE, 'all_or_nothing': bool(c.get('all_or_nothing')),
                'code': code[:MAX_CODE_CHARS], 'code_sha': sha, 'empty': not code.strip(), 'tests': c['tests'],
                'saved': ({'fraction': s['fraction'], 'summary': s['summary'], 'detail': s['detail'], 'run_at': s['run_at']} if s and s['sha'] == sha else None),
            })
        return jsonify({'attempt_id': attempt.id, 'questions': out})
    finally:
        session.close()


@code_run_bp.route('/api/exam_attempts/<int:attempt_id>/code_results', methods=['POST'])
@paseto_required
@limiter.exempt
def code_results(attempt_id):
    data = request.get_json(silent=True) or {}
    num = str(data.get('num') or '')
    results = data.get('results')
    if not isinstance(results, list) or len(results) > 200:
        return jsonify({'error': 'Résultats invalides'}), 400
    session = get_session()
    try:
        user, attempt, err = _staff(session, attempt_id)
        if err:
            return err
        c = _code_keys(attempt.exam.subject).get(num)
        if not c:
            return jsonify({'error': 'Cette question n\'a pas de tests'}), 404
        code = str(_answers(attempt).get(f'pq_{num}') or '')
        sha = QT.code_sha(code)
        if str(data.get('code_sha') or '') != sha:
            return jsonify({'error': 'La réponse a changé depuis l\'exécution : relancez les tests'}), 409
        if code.strip():
            judged = QT.judge_code(c, results)
            if judged is None:
                return jsonify({'error': 'Exécution incomplète : tous les tests doivent avoir été exécutés'}), 400
            fraction, summary, detail = judged
        else:
            fraction, summary, detail = 0.0, 'aucune réponse', []
        CR.save(session, attempt.id, num, sha, fraction, summary, detail, user.id)
        new_score, scores = _apply_to_correction(session, attempt, num, fraction, summary, user.id)
        return jsonify({'success': True, 'fraction': fraction, 'summary': summary, 'detail': detail, 'score': new_score, 'question_scores': scores, 'feedback': attempt.feedback})
    finally:
        session.close()


def _apply_to_correction(session, attempt, num: str, fraction: float, summary: str, user_id):
    """Si la copie est déjà corrigée question par question, remplace la note de CETTE question par celle des tests et recalcule le total
    (même calcul que la correction manuelle question par question ; l'enseignant garde la main ensuite). Sinon : rien (le résultat sera lu à la correction)."""
    from routes.exams import _question_points_map, _push_grades_if_published, _notify_grade_change
    from helpers import utcnow
    try:
        existing = json.loads(attempt.question_scores) if attempt.question_scores else []
    except Exception:
        existing = []
    if not existing or attempt.score is None:
        return attempt.score, existing
    sub = attempt.exam.subject
    pts = (_question_points_map(sub.content or '', sub.rubric or '') or {}).get(num)
    updated, found = [], False
    for q in existing:
        if str(q.get('num')) == num:
            found = True
            qmax = float(q.get('max', pts or 0) or 0)
            q = {**q, 'type': 'code', 'max': qmax, 'score': round(qmax * fraction, 2), 'correct': fraction == 1.0, 'given': summary,
                 'feedback': f"Tests exécutés : {summary}."}
        updated.append(q)
    if not found:
        if not pts:
            return attempt.score, existing
        updated.append({'num': num, 'type': 'code', 'max': float(pts), 'score': round(float(pts) * fraction, 2), 'correct': fraction == 1.0, 'given': summary,
                        'feedback': f"Tests exécutés : {summary}."})
    total_max = sum(float(q.get('max', 0) or 0) for q in updated)
    total_score = sum(float(q.get('score', 0) or 0) for q in updated)
    new_score = round(total_score / total_max * 20, 2) if total_max > 0.01 else 0.0
    lines = []
    for q in updated:
        fb = (q.get('feedback') or '').strip()
        lines.append(f"Question {q.get('num')} : {float(q.get('score', 0) or 0):.2f}/{float(q.get('max', 0) or 0):.2f} pt" + (f"\n{fb}" if fb else ''))
    old = attempt.score
    attempt.question_scores = json.dumps(updated)
    attempt.score = new_score
    attempt.feedback = "\n\n".join(lines) + f"\n\nNote totale: {new_score:.2f}/20"
    attempt.corrected_at = utcnow()
    attempt.corrected_by_id = user_id
    session.commit()
    try:
        _push_grades_if_published(attempt.exam)
        _notify_grade_change(attempt.exam, attempt.id, old)
    except Exception as e:
        print(f"[code_results] notification de changement de note {attempt.id} : {e}")
    return new_score, updated


@code_run_bp.route('/api/online_exams/<int:exam_id>/code_pending', methods=['GET'])
@paseto_required
@limiter.exempt
def code_pending(exam_id):
    """Copies rendues de l'examen dont au moins une question de code exécutable n'a pas de résultat à jour."""
    from routes.exams import can_view_exam, _never_seen
    session = get_session()
    try:
        uid = get_current_user_id()
        user = session.query(User).filter_by(id=uid).first()
        if not user or user.role not in (UserRole.PROFESSOR, UserRole.ADMIN, UserRole.RESPONSABLE_EXAMEN):
            return jsonify({'error': 'Accès non autorisé'}), 403
        exam = session.query(OnlineExam).filter_by(id=exam_id).first()
        if not exam:
            return jsonify({'error': 'Examen introuvable'}), 404
        if user.role == UserRole.PROFESSOR and not can_view_exam(session, exam, uid):
            return jsonify({'error': 'Accès non autorisé'}), 403
        keys = {n: c for n, c in _code_keys(exam.subject).items() if c['lang'] in QT.CODE_RUNNABLE}
        if not keys:
            return jsonify({'has_code': False, 'attempts': [], 'total': 0})
        attempts = session.query(ExamAttempt).filter(
            ExamAttempt.exam_id == exam_id, ExamAttempt.status.in_([AttemptStatus.SUBMITTED, AttemptStatus.AUTO_SUBMITTED, AttemptStatus.BANNED])).all()
        out = []
        for a in attempts:
            if _never_seen(a):
                continue
            ans = _answers(a)
            saved = CR.load(session, a.id)
            todo = [n for n in keys if str(ans.get(f'pq_{n}') or '').strip() and (saved.get(n) or {}).get('sha') != QT.code_sha(ans.get(f'pq_{n}'))]
            if todo:
                out.append({'attempt_id': a.id, 'student_name': (a.student.full_name if getattr(a, 'student', None) and hasattr(a.student, 'full_name') else f'Étudiant {a.student_id}'), 'questions': todo})
        return jsonify({'has_code': True, 'attempts': out, 'total': len(out)})
    finally:
        session.close()
