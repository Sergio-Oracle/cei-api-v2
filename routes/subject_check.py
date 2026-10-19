"""Contrôle de lisibilité du sujet d'un examen (voir services/subject_check.py)."""
from flask import Blueprint, jsonify

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
