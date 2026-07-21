"""
Blueprint Subjects — thin HTTP controller.

All business logic lives in services/subject_service.py.
All SQL lives in repositories/subject_repository.py.
Input validation uses schemas/subject_schemas.py (Pydantic v2).

GET    /api/subjects
GET    /api/subjects/<id>
DELETE /api/subjects/<id>
POST   /api/subjects            ← création manuelle (JSON, sans fichier)
POST   /api/subjects/upload     ← upload fichier + IA
POST   /api/subjects/<id>/upload_image
"""
from flask import Blueprint, request, jsonify
from pydantic import ValidationError

from auth_paseto import paseto_required, get_current_user_id
from models import get_session, User, UserRole
from schemas.subject_schemas import validate_upload_form, SubjectCreateInput
from services.subject_service import SubjectService

subjects_bp = Blueprint('subjects', __name__)


def _get_user(user_id: int):
    session = get_session()
    try:
        return session.query(User).filter_by(id=user_id).first()
    finally:
        session.close()


# ── List ───────────────────────────────────────────────────────────────────────

@subjects_bp.route('/api/subjects', methods=['GET'])
@paseto_required
def get_subjects():
    try:
        user_id = get_current_user_id()
        user    = _get_user(user_id)
        result  = SubjectService.list_for_user(user_id, user.role)
        return jsonify(result)
    except Exception as e:
        print(f'ERROR get_subjects: {e}')
        return jsonify({'error': str(e)}), 500


# ── Detail ─────────────────────────────────────────────────────────────────────

@subjects_bp.route('/api/subjects/<int:subject_id>', methods=['GET'])
@paseto_required
def get_subject_detail(subject_id):
    try:
        user_id = get_current_user_id()
        user    = _get_user(user_id)
        result  = SubjectService.get_detail(subject_id, user_id, user.role)
        return jsonify(result)
    except LookupError as e:
        return jsonify({'error': str(e)}), 404
    except PermissionError as e:
        return jsonify({'error': str(e)}), 403
    except Exception as e:
        print(f'ERROR get_subject_detail: {e}')
        import traceback; traceback.print_exc()
        return jsonify({'error': str(e)}), 500


# ── Update ─────────────────────────────────────────────────────────────────────

@subjects_bp.route('/api/subjects/<int:subject_id>', methods=['PUT'])
@paseto_required
def update_subject(subject_id):
    """Édite un sujet déjà validé (titre/contenu/barème) — bloqué si un
    examen lié est déjà actif/clôturé ou a reçu des tentatives."""
    try:
        user_id = get_current_user_id()
        user    = _get_user(user_id)
        data = request.get_json(silent=True) or {}
        result = SubjectService.update(
            subject_id, user_id, user.role,
            title=data.get('title'), content=data.get('content'), rubric=data.get('rubric'),
        )
        return jsonify({'success': True, 'subject': result})
    except LookupError as e:
        return jsonify({'error': str(e)}), 404
    except PermissionError as e:
        return jsonify({'error': str(e)}), 403
    except Exception as e:
        print(f'ERROR update_subject: {e}')
        import traceback; traceback.print_exc()
        return jsonify({'error': str(e)}), 500


# ── Delete ─────────────────────────────────────────────────────────────────────

@subjects_bp.route('/api/subjects/<int:subject_id>', methods=['DELETE'])
@paseto_required
def delete_subject(subject_id):
    try:
        user_id = get_current_user_id()
        user    = _get_user(user_id)
        SubjectService.delete(subject_id, user_id, user.role)
        return jsonify({'success': True, 'message': 'Sujet et toutes ses dépendances supprimés avec succès'})
    except LookupError as e:
        return jsonify({'success': False, 'error': str(e)}), 404
    except PermissionError as e:
        return jsonify({'success': False, 'error': str(e)}), 403
    except Exception as e:
        print(f'ERROR delete_subject: {e}')
        import traceback; traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


# ── Create manual (JSON body, no file) ────────────────────────────────────────

@subjects_bp.route('/api/subjects', methods=['POST'])
@paseto_required
def create_subject():
    """Création manuelle d'un sujet : titre + contenu + barème saisis à la main."""
    try:
        raw = request.get_json(silent=True) or {}
        try:
            data = SubjectCreateInput(**raw)
        except Exception as ve:
            msgs = str(ve)
            return jsonify({'error': msgs}), 422

        user_id = get_current_user_id()
        user    = _get_user(user_id)

        result = SubjectService.create_manual(
            title=data.title,
            content=data.content,
            rubric=data.rubric,
            creator_id=user_id,
            role=user.role,
            ec_id=data.ec_id,
        )
        return jsonify({'success': True, 'subject': result}), 201

    except PermissionError as e:
        return jsonify({'error': str(e)}), 403
    except Exception as e:
        print(f'ERROR create_subject: {e}')
        import traceback; traceback.print_exc()
        return jsonify({'error': str(e)}), 500


# ── Upload ─────────────────────────────────────────────────────────────────────

@subjects_bp.route('/api/subjects/upload', methods=['POST'])
@paseto_required
def upload_subject():
    try:
        # 1. Validate input
        try:
            data = validate_upload_form(request.form)
        except ValidationError as ve:
            msgs = '; '.join(e['msg'] for e in ve.errors())
            return jsonify({'error': msgs}), 422

        file    = request.files.get('file')
        user_id = get_current_user_id()
        user    = _get_user(user_id)

        # 2. Delegate to service
        result = SubjectService.upload(
            title=data.title,
            file=file,
            creator_id=user_id,
            role=user.role,
            ec_id=data.ec_id,
            question_types=data.question_types,
            rubric_mode=data.rubric_mode,
            total_points=data.total_points,
        )

        # 3. Return response
        duplicates = result.pop('duplicates', [])
        return jsonify({'success': True, 'subject': result, 'duplicates': duplicates})

    except (ValueError, TypeError) as e:
        return jsonify({'error': str(e)}), 400
    except PermissionError as e:
        return jsonify({'error': str(e)}), 403
    except LookupError as e:
        return jsonify({'error': str(e)}), 404
    except Exception as e:
        print(f'ERROR upload_subject: {e}')
        import traceback; traceback.print_exc()
        return jsonify({'error': str(e)}), 500


# ── Image upload ───────────────────────────────────────────────────────────────

@subjects_bp.route('/api/subjects/<int:subject_id>/upload_image', methods=['POST'])
@paseto_required
def upload_subject_image(subject_id):
    try:
        img     = request.files.get('image')
        user_id = get_current_user_id()
        user    = _get_user(user_id)

        if not img:
            return jsonify({'error': 'Aucune image fournie'}), 400

        image_url = SubjectService.upload_image(subject_id, img, user_id, user.role)
        return jsonify({'success': True, 'image_url': image_url})

    except (ValueError, TypeError) as e:
        return jsonify({'error': str(e)}), 400
    except PermissionError as e:
        return jsonify({'error': str(e)}), 403
    except LookupError as e:
        return jsonify({'error': str(e)}), 404
    except Exception as e:
        print(f'ERROR upload_subject_image: {e}')
        return jsonify({'error': str(e)}), 500
