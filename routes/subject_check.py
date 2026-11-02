"""Contrôle de lisibilité du sujet d'un examen (voir services/subject_check.py)."""
from flask import Blueprint, jsonify, request

from auth_paseto import paseto_required, get_current_user_id
from models import get_session, User, UserRole, OnlineExam
from services.ec_rights import can_view_exam
from services.subject_check import analyze

subject_check_bp = Blueprint('subject_check', __name__)


@subject_check_bp.route('/api/online_exams/<int:exam_id>/subject_check', methods=['GET'])
@paseto_required
def exam_subject_check(exam_id):
    """Rapport « les étudiants verront-ils bien toutes les questions ? » — lecture seule, personnel uniquement."""
    session = get_session()
    try:
        user = session.get(User, get_current_user_id())
        exam = session.get(OnlineExam, exam_id)
        if not user or not exam:
            return jsonify({'error': 'Examen non trouvé'}), 404
        if user.role == UserRole.STUDENT or user.role == UserRole.SURVEILLANT:
            return jsonify({'error': 'Accès non autorisé'}), 403
        if user.role == UserRole.PROFESSOR and not can_view_exam(session, exam, user.id):
            return jsonify({'error': 'Accès non autorisé'}), 403
        return jsonify(analyze(exam.subject.content if exam.subject else ''))
    finally:
        session.close()


@subject_check_bp.route('/api/subjects/check', methods=['POST'])
@paseto_required
def check_subject_draft():
    """Contrôle d'un TEXTE DE SUJET en cours de rédaction (éditeur de « Prévisualiser ») — aucune écriture.
    Donne ce que les étudiants verraient : questions lues, lignes jamais affichées, références à un élément absent,
    et l'accord avec le barème du sujet (`subject_id`) quand il en a un."""
    import re
    from routes.exams import _parse_subject_blocks_ordered, _strip_bareme_from_content
    from services.subject_normalizer import normalize
    from services.subject_coverage import hidden_lines
    session = get_session()
    try:
        user = session.get(User, get_current_user_id())
        if not user or user.role not in (UserRole.ADMIN, UserRole.RESPONSABLE_EXAMEN, UserRole.PROFESSOR):
            return jsonify({'error': 'Accès non autorisé'}), 403
        data = request.get_json(silent=True) or {}
        content = data.get('content')
        if not isinstance(content, str) or len(content) > 400_000:
            return jsonify({'error': 'Contenu invalide'}), 400
        content = normalize(content)[0]             # même remise en forme qu'à l'enregistrement
        report = analyze(content)
        blocks = _parse_subject_blocks_ordered(_strip_bareme_from_content(content))
        qs = [b for b in blocks if b.get('type') not in ('section', 'text', 'description')]
        out = {**report, 'hidden_lines': hidden_lines(content, blocks),
               'blocks': [{'num': b.get('num'), 'type': b['type'], 'has_context': bool((b.get('context') or {}).get('text'))} for b in qs]}
        rubric_issue = None
        if data.get('subject_id'):
            from models import Subject
            subj = session.get(Subject, int(data['subject_id']))
            if subj and (subj.rubric or '').strip():
                in_rubric = {int(n) for n in re.findall(r'^\s*Question\s+(\d{1,3})\b', subj.rubric, re.M)}
                in_subject = {int(b['num']) for b in qs if str(b.get('num', '')).isdigit()}
                if in_rubric and in_rubric != in_subject:
                    missing, extra = sorted(in_subject - in_rubric), sorted(in_rubric - in_subject)
                    rubric_issue = {'missing_in_rubric': missing, 'missing_in_subject': extra}
                    out['issues'] = list(out['issues']) + [{'level': 'warn', 'message':
                        "Le barème ne correspond plus aux questions du sujet"
                        + (f" : question(s) {', '.join(map(str, missing))} sans barème" if missing else '')
                        + (f" ; barème pour des question(s) absentes du sujet : {', '.join(map(str, extra))}" if extra else '')
                        + ". Mettez le barème à jour (Sujets, puis Modifier) avant l'examen."}]
        out['rubric'] = rubric_issue
        return jsonify(out)
    finally:
        session.close()
