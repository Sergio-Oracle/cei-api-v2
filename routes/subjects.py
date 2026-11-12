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
from werkzeug.exceptions import RequestEntityTooLarge

from auth_paseto import paseto_required, get_current_user_id
from models import get_session, User, UserRole
from schemas.subject_schemas import validate_upload_form, SubjectCreateInput
from services.subject_service import SubjectService
from extensions import limiter

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


# ── Assistant de création par discussion (chat) ────────────────────────────────

def _chat_user():
    user = _get_user(get_current_user_id())
    if not user or user.role not in (UserRole.PROFESSOR, UserRole.ADMIN, UserRole.RESPONSABLE_EXAMEN):
        return None
    return user


@subjects_bp.route('/api/subjects/chat/files', methods=['POST'])
@paseto_required
@limiter.exempt
def subject_chat_files():
    """Joint des fichiers à la discussion : lus, convertis au format CEI, gardés 2 h pour la conversation."""
    try:
        user = _chat_user()
        if not user:
            return jsonify({'error': 'Accès non autorisé'}), 403
        files = request.files.getlist('files')
        if not files:
            return jsonify({'error': "Aucun fichier"}), 400
        from services.subject_chat import store_files
        return jsonify({'success': True, 'files': store_files(user.id, files)})
    except Exception as e:
        print(f'ERROR subject_chat_files: {e}')
        return jsonify({'error': 'Lecture des fichiers impossible'}), 500


@subjects_bp.route('/api/subjects/chat', methods=['POST'])
@paseto_required
@limiter.exempt
def subject_chat():
    """Un tour de discussion : {messages, file_ids, context, draft} → {reply, draft?}."""
    try:
        user = _chat_user()
        if not user:
            return jsonify({'error': 'Accès non autorisé'}), 403
        data = request.get_json(silent=True) or {}
        messages = [m for m in (data.get('messages') or []) if isinstance(m, dict) and m.get('content')]
        if not messages or messages[-1].get('role') != 'user':
            return jsonify({'error': 'Message vide'}), 400
        from services.subject_chat import chat
        out = chat(user.id, messages, data.get('file_ids') or [], str(data.get('context') or ''), data.get('draft') if isinstance(data.get('draft'), dict) else None)
        return jsonify({'success': True, **out})
    except Exception as e:
        print(f'ERROR subject_chat: {e}')
        import traceback; traceback.print_exc()
        return jsonify({'error': "L'assistant est momentanément indisponible, réessayez dans un instant"}), 500


# ── Upload ─────────────────────────────────────────────────────────────────────

def _expected_questions(form):
    """Nombre de questions que l'enseignant attend (facultatif) : sert à CONTRÔLER la lecture du document."""
    try:
        n = int(form.get('expected_questions') or 0)
    except (TypeError, ValueError):
        return None
    return n if 1 <= n <= 300 else None


@subjects_bp.route('/api/subjects/count-questions', methods=['POST'])
@paseto_required
def count_subject_questions():
    """Compte les questions des fichiers déposés SANS rien enregistrer (bouton « Compter » de la page de création)."""
    try:
        user = _get_user(get_current_user_id())
        if user.role not in (UserRole.PROFESSOR, UserRole.ADMIN, UserRole.RESPONSABLE_EXAMEN):
            return jsonify({'error': 'Accès non autorisé'}), 403
        from services.subject_count import count_from_files
        files = request.files.getlist('files')
        if not files:
            return jsonify({'error': 'Ajoutez d\'abord votre fichier'}), 400
        return jsonify({'success': True, **count_from_files(files)})
    except Exception as e:
        print(f'ERROR count_subject_questions: {e}')
        return jsonify({'error': 'Comptage impossible, vérifiez le fichier'}), 500


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

        files   = request.files.getlist('files')
        user_id = get_current_user_id()
        user    = _get_user(user_id)

        # 2. Delegate to service
        result = SubjectService.upload(
            title=data.title,
            files=files,
            creator_id=user_id,
            role=user.role,
            ec_id=data.ec_id,
            question_types=data.question_types,
            rubric_mode=data.rubric_mode,
            total_points=data.total_points,
            expected_questions=_expected_questions(request.form),
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
    except RequestEntityTooLarge:
        raise  # laisser remonter au handler 413 global (app.py) — pas de message générique
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
    except RequestEntityTooLarge:
        raise  # laisser remonter au handler 413 global (app.py) — pas de message générique
    except Exception as e:
        print(f'ERROR upload_subject_image: {e}')
        return jsonify({'error': str(e)}), 500


# ── Ramener le barème sur un total (bouton « Ramener sur 20 » de l'éditeur de sujet) ─────────────────────────────────────

@subjects_bp.route('/api/subjects/rescale_points', methods=['POST'])
@paseto_required
def rescale_subject_points():
    """Calcule (sans rien enregistrer) le sujet et le barème avec une somme de points EXACTEMENT égale à `total_points` (20 par
    défaut), en gardant les proportions des points actuels. L'enseignant relit puis enregistre comme pour toute modification.
    Corps : {content, rubric, total_points?}. Réponse : {content, rubric, total, previous_total, changed}."""
    try:
        user = _get_user(get_current_user_id())
        if not user or user.role not in (UserRole.PROFESSOR, UserRole.ADMIN, UserRole.RESPONSABLE_EXAMEN):
            return jsonify({'error': 'Accès non autorisé'}), 403
        data = request.get_json(silent=True) or {}
        content, rubric = data.get('content') or '', data.get('rubric') or ''
        try:
            target = max(1.0, min(200.0, float(data.get('total_points') or 20)))
        except (TypeError, ValueError):
            target = 20.0
        if not content.strip() or not rubric.strip():
            return jsonify({'error': "Le sujet et son barème sont nécessaires pour ajuster les points."}), 400
        from services.points_total import rescale_points, current_total
        before, _ = current_total(content, rubric)
        c2, r2, new = rescale_points(content, rubric, target)
        if not new:
            return jsonify({'error': "Aucune question reconnue : les points ne peuvent pas être ajustés automatiquement."}), 400
        after, _ = current_total(c2, r2)
        return jsonify({'success': True, 'content': c2, 'rubric': r2, 'total': after, 'previous_total': before,
                        'changed': abs(before - after) > 0.01 or c2 != content or r2 != rubric})
    except Exception as e:
        print(f'ERROR rescale_subject_points: {e}')
        return jsonify({'error': 'Ajustement impossible'}), 500
