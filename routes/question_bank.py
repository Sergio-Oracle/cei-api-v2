"""
Blueprint Question Bank (Banque de questions).

GET  /api/question_bank
POST /api/question_bank
DELETE /api/question_bank/<id>
POST /api/question_bank/assemble
GET  /api/question_bank/duplicates
POST /api/question_bank/check_duplicate
"""
from difflib import SequenceMatcher
from flask import Blueprint, request, jsonify

from auth_paseto import paseto_required, get_current_user_id
from models import (
    get_session, User, UserRole, QuestionBank, Subject, EC,
)

DUPLICATE_THRESHOLD = 0.95


def _similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower().strip(), b.lower().strip()).ratio()

question_bank_bp = Blueprint('question_bank', __name__)


@question_bank_bp.route('/api/question_bank', methods=['GET'])
@paseto_required
def list_question_bank():
    try:
        user_id = get_current_user_id()
        session = get_session()
        user    = session.query(User).filter_by(id=user_id).first()

        if user.role not in [UserRole.ADMIN, UserRole.PROFESSOR]:
            session.close(); return jsonify({'error': 'Accès non autorisé'}), 403

        questions = session.query(QuestionBank).order_by(QuestionBank.created_at.desc()).all()
        result = [q.to_dict() for q in questions]
        session.close()
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@question_bank_bp.route('/api/question_bank', methods=['POST'])
@paseto_required
def save_question_bank():
    try:
        user_id = get_current_user_id()
        session = get_session()
        user    = session.query(User).filter_by(id=user_id).first()

        if user.role not in [UserRole.ADMIN, UserRole.PROFESSOR]:
            session.close(); return jsonify({'error': 'Accès non autorisé'}), 403

        data = request.get_json() or {}
        if not data.get('content'):
            session.close(); return jsonify({'error': 'Contenu requis'}), 400

        new_content = data['content'].strip()

        # Vérifier les doublons avant la sauvegarde
        existing = session.query(QuestionBank).all()
        duplicates = []
        for ex in existing:
            sim = _similarity(new_content, ex.content)
            if sim >= DUPLICATE_THRESHOLD:
                duplicates.append({'id': ex.id, 'title': ex.title, 'similarity': round(sim * 100, 1)})

        q = QuestionBank(
            title=(data.get('title') or new_content[:80]).strip(),
            content=new_content,
            rubric=data.get('rubric', ''),
            question_type=data.get('question_type', 'open'),
            bloom_level=data.get('bloom_level', ''),
            ec_id=data.get('ec_id') or None,
            created_by_id=user_id,
        )
        session.add(q); session.commit()
        result = q.to_dict(); session.close()
        return jsonify({'success': True, 'question': result, 'duplicates': duplicates}), 201
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@question_bank_bp.route('/api/question_bank/<int:q_id>', methods=['DELETE'])
@paseto_required
def delete_question_bank(q_id):
    try:
        user_id = get_current_user_id()
        session = get_session()
        user    = session.query(User).filter_by(id=user_id).first()

        q = session.query(QuestionBank).filter_by(id=q_id).first()
        if not q: session.close(); return jsonify({'error': 'Question introuvable'}), 404
        if user.role != UserRole.ADMIN and q.created_by_id != user_id:
            session.close(); return jsonify({'error': 'Accès non autorisé'}), 403

        session.delete(q); session.commit(); session.close()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@question_bank_bp.route('/api/question_bank/duplicates', methods=['GET'])
@paseto_required
def find_duplicates():
    """Retourne tous les paires de questions avec similarité ≥ DUPLICATE_THRESHOLD."""
    try:
        user_id = get_current_user_id()
        session = get_session()
        user    = session.query(User).filter_by(id=user_id).first()
        if not user or user.role not in [UserRole.ADMIN, UserRole.PROFESSOR]:
            session.close(); return jsonify({'error': 'Accès non autorisé'}), 403

        questions = session.query(QuestionBank).order_by(QuestionBank.created_at.desc()).all()
        pairs = []
        for i in range(len(questions)):
            for j in range(i + 1, len(questions)):
                sim = _similarity(questions[i].content, questions[j].content)
                if sim >= DUPLICATE_THRESHOLD:
                    pairs.append({
                        'q1': {'id': questions[i].id, 'title': questions[i].title},
                        'q2': {'id': questions[j].id, 'title': questions[j].title},
                        'similarity': round(sim * 100, 1),
                    })
        session.close()
        return jsonify({'duplicates': pairs, 'count': len(pairs)})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@question_bank_bp.route('/api/question_bank/check_duplicate', methods=['POST'])
@paseto_required
def check_duplicate():
    """Vérifie si un contenu est similaire à ≥95% d'une question existante."""
    try:
        user_id = get_current_user_id()
        session = get_session()
        user    = session.query(User).filter_by(id=user_id).first()
        if not user or user.role not in [UserRole.ADMIN, UserRole.PROFESSOR]:
            session.close(); return jsonify({'error': 'Accès non autorisé'}), 403

        data    = request.get_json() or {}
        content = (data.get('content') or '').strip()
        exclude_id = data.get('id')  # Pour exclure la question elle-même si on édite
        if not content:
            session.close(); return jsonify({'duplicates': []})

        existing = session.query(QuestionBank).all()
        found = []
        for ex in existing:
            if exclude_id and ex.id == int(exclude_id):
                continue
            sim = _similarity(content, ex.content)
            if sim >= DUPLICATE_THRESHOLD:
                found.append({'id': ex.id, 'title': ex.title, 'similarity': round(sim * 100, 1)})
        session.close()
        return jsonify({'duplicates': found, 'is_duplicate': len(found) > 0})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@question_bank_bp.route('/api/question_bank/assemble', methods=['POST'])
@paseto_required
def assemble_from_bank():
    try:
        user_id = get_current_user_id()
        session = get_session()
        user    = session.query(User).filter_by(id=user_id).first()

        if not user or user.role not in [UserRole.ADMIN, UserRole.PROFESSOR]:
            session.close(); return jsonify({'error': 'Accès non autorisé'}), 403

        data           = request.get_json() or {}
        question_ids   = data.get('question_ids', [])
        title          = data.get('title', 'Examen').strip()
        duration       = int(data.get('duration', 60))
        student_level  = data.get('student_level', 'Licence')
        ec_id          = data.get('ec_id') or None
        exam_type_hint = data.get('exam_type', '')

        if not question_ids:
            session.close(); return jsonify({'error': 'Aucune question sélectionnée'}), 400

        questions = [q for qid in question_ids for q in [session.query(QuestionBank).filter_by(id=int(qid)).first()] if q]
        if not questions:
            session.close(); return jsonify({'error': 'Questions introuvables'}), 404

        types_present  = {q.question_type for q in questions}
        has_qcm  = 'qcm' in types_present
        has_vf   = 'vf'  in types_present
        has_open = 'open' in types_present or 'subopen' in types_present

        if exam_type_hint:
            exam_type_label = exam_type_hint
        elif has_qcm and has_open:
            exam_type_label = 'Mixte (QCM + Questions ouvertes)'
        elif has_qcm and has_vf:
            exam_type_label = 'Mixte (QCM + Vrai/Faux)'
        elif has_qcm:
            exam_type_label = 'QCM'
        elif has_vf:
            exam_type_label = 'Vrai / Faux'
        else:
            exam_type_label = 'Questions ouvertes'

        TYPE_MARKER = {'qcm': '[QCM]', 'vf': '[VF]', 'open': '[OUVERT]', 'subopen': '[SUBOPEN]'}
        n = len(questions)
        base_pts = 20 // n
        remainder = 20 - base_pts * n

        sep = '══════════════════════════════════════'
        content_lines = [
            sep, title.upper(), sep,
            f"Type d'examen : {exam_type_label}",
            f"Niveau : {student_level} | Durée : {duration} minutes | Note totale : 20 points",
            sep, '',
            "INSTRUCTIONS AUX ÉTUDIANTS", "──────────────────────────",
            "Lisez attentivement chaque question avant de répondre. Respectez le barème indiqué pour chaque question.",
            '', sep, 'QUESTIONS', sep, '',
        ]
        rubric_lines = [sep, 'BARÈME DE NOTATION', sep, '']

        for i, q in enumerate(questions):
            num  = i + 1
            pts  = base_pts + (1 if i < remainder else 0)
            mark = TYPE_MARKER.get(q.question_type, '[OUVERT]')
            content_lines.append(f'Question {num} — {q.title} ............. ({pts} pt{"s" if pts > 1 else ""}) {mark}')
            content_lines.append(q.content.strip()); content_lines.append('')
            rubric_lines.append(f'Question {num} — {q.title} ({pts} pt{"s" if pts > 1 else ""})')
            if q.rubric and q.rubric.strip():
                for line in q.rubric.strip().split('\n'):
                    rubric_lines.append(f'  {line}')
            else:
                rubric_lines.append(f'  • Réponse attendue : {pts} pt{"s" if pts > 1 else ""}')
            rubric_lines.append('')

        rubric_lines += ['──────────────────────────', 'TOTAL : 20 / 20 points', sep]

        subject = Subject(
            title=title,
            content='\n'.join(content_lines).strip(),
            rubric='\n'.join(rubric_lines).strip(),
            ec_id=int(ec_id) if ec_id else None,
            created_by_id=user_id,
        )
        session.add(subject); session.commit()
        result = subject.to_dict(); session.close()
        return jsonify({'success': True, 'subject': result}), 201
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({'error': str(e)}), 500
