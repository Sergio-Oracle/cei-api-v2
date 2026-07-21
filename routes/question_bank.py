"""
Blueprint Question Bank (Banque de questions).

GET  /api/question_bank
POST /api/question_bank
DELETE /api/question_bank/<id>
POST /api/question_bank/assemble
GET  /api/question_bank/duplicates
POST /api/question_bank/duplicates/auto-clean
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

        tags = data.get('tags')
        q = QuestionBank(
            title=(data.get('title') or new_content[:80]).strip(),
            content=new_content,
            rubric=data.get('rubric', ''),
            question_type=data.get('question_type', 'open'),
            bloom_level=data.get('bloom_level', ''),
            ec_id=data.get('ec_id') or None,
            tags=','.join(t.strip() for t in tags if t.strip()) if isinstance(tags, list) else (tags or None),
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


@question_bank_bp.route('/api/question_bank/<int:q_id>', methods=['PUT'])
@paseto_required
def update_question_bank(q_id):
    """Édition en place — parité Moodle (les questions de la banque ne sont
    plus figées : titre, énoncé, barème, type, Bloom, EC, tags, statut)."""
    try:
        user_id = get_current_user_id()
        session = get_session()
        user    = session.query(User).filter_by(id=user_id).first()

        q = session.query(QuestionBank).filter_by(id=q_id).first()
        if not q: session.close(); return jsonify({'error': 'Question introuvable'}), 404
        if user.role != UserRole.ADMIN and q.created_by_id != user_id:
            session.close(); return jsonify({'error': 'Accès non autorisé'}), 403

        data = request.get_json() or {}
        if 'title' in data:
            title = (data['title'] or '').strip()
            if not title: session.close(); return jsonify({'error': 'Titre requis'}), 400
            q.title = title
        if 'content' in data:
            content = (data['content'] or '').strip()
            if not content: session.close(); return jsonify({'error': 'Contenu requis'}), 400
            q.content = content
        if 'rubric' in data:
            q.rubric = data['rubric']
        if 'question_type' in data:
            q.question_type = data['question_type']
        if 'bloom_level' in data:
            q.bloom_level = data['bloom_level']
        if 'ec_id' in data:
            q.ec_id = int(data['ec_id']) if data['ec_id'] else None
        if 'tags' in data:
            tags = data['tags']
            q.tags = ','.join(t.strip() for t in tags if t.strip()) if isinstance(tags, list) else (tags or None)
        if 'status' in data and data['status'] in ('active', 'hidden'):
            q.status = data['status']

        session.commit()
        result = q.to_dict(); session.close()
        return jsonify({'success': True, 'question': result})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@question_bank_bp.route('/api/question_bank/<int:q_id>/duplicate', methods=['POST'])
@paseto_required
def duplicate_question_bank(q_id):
    """Dupliquer une question — parité Moodle (créer une variante à partir
    d'une question existante sans repartir de zéro)."""
    try:
        user_id = get_current_user_id()
        session = get_session()
        user    = session.query(User).filter_by(id=user_id).first()
        if not user or user.role not in [UserRole.ADMIN, UserRole.PROFESSOR]:
            session.close(); return jsonify({'error': 'Accès non autorisé'}), 403

        q = session.query(QuestionBank).filter_by(id=q_id).first()
        if not q: session.close(); return jsonify({'error': 'Question introuvable'}), 404

        copy = QuestionBank(
            title=f'{q.title} (copie)',
            content=q.content,
            rubric=q.rubric,
            question_type=q.question_type,
            bloom_level=q.bloom_level,
            ec_id=q.ec_id,
            tags=q.tags,
            status='active',
            created_by_id=user_id,
        )
        session.add(copy); session.commit()
        result = copy.to_dict(); session.close()
        return jsonify({'success': True, 'question': result}), 201
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@question_bank_bp.route('/api/question_bank/bulk_move', methods=['POST'])
@paseto_required
def bulk_move_question_bank():
    """Déplacer plusieurs questions vers un autre EC en un seul appel —
    parité Moodle (bulk move entre catégories)."""
    try:
        user_id = get_current_user_id()
        session = get_session()
        user    = session.query(User).filter_by(id=user_id).first()
        if not user or user.role not in [UserRole.ADMIN, UserRole.PROFESSOR]:
            session.close(); return jsonify({'error': 'Accès non autorisé'}), 403

        data = request.get_json() or {}
        question_ids = data.get('question_ids') or []
        ec_id = data.get('ec_id')  # None autorisé = retirer l'EC
        if not question_ids:
            session.close(); return jsonify({'error': 'Aucune question sélectionnée'}), 400

        moved, skipped = 0, 0
        for qid in question_ids:
            q = session.query(QuestionBank).filter_by(id=int(qid)).first()
            if not q or (user.role != UserRole.ADMIN and q.created_by_id != user_id):
                skipped += 1; continue
            q.ec_id = int(ec_id) if ec_id else None
            moved += 1
        session.commit(); session.close()
        return jsonify({'success': True, 'moved': moved, 'skipped': skipped})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@question_bank_bp.route('/api/question_bank/bulk_delete', methods=['POST'])
@paseto_required
def bulk_delete_question_bank():
    """Supprimer plusieurs questions en un seul appel — parité Moodle (bulk
    delete), au lieu de cliquer supprimer une par une."""
    try:
        user_id = get_current_user_id()
        session = get_session()
        user    = session.query(User).filter_by(id=user_id).first()

        data = request.get_json() or {}
        question_ids = data.get('question_ids') or []
        if not question_ids:
            session.close(); return jsonify({'error': 'Aucune question sélectionnée'}), 400

        deleted, skipped = 0, 0
        for qid in question_ids:
            q = session.query(QuestionBank).filter_by(id=int(qid)).first()
            if not q:
                continue
            if user.role != UserRole.ADMIN and q.created_by_id != user_id:
                skipped += 1; continue
            session.delete(q)
            deleted += 1
        session.commit(); session.close()
        return jsonify({'success': True, 'deleted': deleted, 'skipped': skipped})
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


@question_bank_bp.route('/api/question_bank/duplicates/auto-clean', methods=['POST'])
@paseto_required
def auto_clean_duplicates():
    """Supprime automatiquement les doublons détectés (≥95% de similarité) —
    conserve la question la plus ancienne de chaque paire, supprime la plus
    récente. Répété tant que de nouvelles paires apparaissent (au cas où
    A≈B≈C), avec une limite de sécurité sur le nombre de passes."""
    try:
        user_id = get_current_user_id()
        session = get_session()
        user    = session.query(User).filter_by(id=user_id).first()
        if not user or user.role not in [UserRole.ADMIN, UserRole.PROFESSOR]:
            session.close(); return jsonify({'error': 'Accès non autorisé'}), 403

        deleted = []
        for _ in range(10):  # limite de sécurité — évite une boucle infinie
            questions = session.query(QuestionBank).order_by(QuestionBank.created_at.asc()).all()
            to_delete_id = None
            to_delete_title = None
            for i in range(len(questions)):
                if to_delete_id:
                    break
                for j in range(i + 1, len(questions)):
                    sim = _similarity(questions[i].content, questions[j].content)
                    if sim >= DUPLICATE_THRESHOLD:
                        # questions[i] est la plus ancienne (tri asc) → on garde
                        # questions[j], la plus récente → on supprime
                        to_delete_id = questions[j].id
                        to_delete_title = questions[j].title
                        break
            if not to_delete_id:
                break
            q = session.query(QuestionBank).filter_by(id=to_delete_id).first()
            if q:
                session.delete(q)
                session.commit()
                deleted.append({'id': to_delete_id, 'title': to_delete_title})

        session.close()
        return jsonify({'success': True, 'deleted_count': len(deleted), 'deleted': deleted})
    except Exception as e:
        try: session.rollback(); session.close()
        except: pass
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

        # Détection de doublons parmi la sélection — un professeur qui
        # assemble plusieurs questions de la banque peut, sans s'en rendre
        # compte, sélectionner deux variantes quasi identiques de la même
        # question (même mécanisme que pour la génération IA).
        duplicates = []
        for i in range(len(questions)):
            for j in range(i + 1, len(questions)):
                sim = _similarity(questions[i].content[:300], questions[j].content[:300])
                if sim >= DUPLICATE_THRESHOLD:
                    duplicates.append({
                        'similarity': round(sim * 100, 1),
                        'titles': [questions[i].title, questions[j].title],
                    })

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
            creator_id=user_id,
        )
        session.add(subject); session.commit()
        result = subject.to_dict(); session.close()
        return jsonify({'success': True, 'subject': result, 'duplicates': duplicates}), 201
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({'error': str(e)}), 500
