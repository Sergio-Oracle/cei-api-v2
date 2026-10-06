"""
Moteur RAG (RAGFlow) — page d'administration « Moteur RAG ».

  GET    /api/admin/rag/engines                    moteurs enregistrés
  POST   /api/admin/rag/engines                    ajoute (connexion vérifiée avant enregistrement)
  PUT    /api/admin/rag/engines/<id>               modifie (clé facultative : conservée si absente)
  DELETE /api/admin/rag/engines/<id>               supprime (refusé s'il est en service)
  POST   /api/admin/rag/engines/<id>/test          diagnostic (composants, clé, modèles)
  POST   /api/admin/rag/engines/<id>/activate      met en service (l'ancien est retiré dans la même transaction)
  POST   /api/admin/rag/engines/<id>/deactivate    retire du service (RAG désactivé dans CEI)
  GET    /api/admin/rag/engines/<id>/status        état de l'indexation par base documentaire
  POST   /api/admin/rag/engines/<id>/reindex       relance l'indexation (échecs, ou tout)
"""
from flask import Blueprint, jsonify, request

from auth_paseto import paseto_required
from helpers import require_admin
from models import get_session, RagEngine
from services import ragflow_client
from services.ragflow_client import RagflowClient, RagflowError

rag_engine_bp = Blueprint('rag_engine', __name__)

_FORBIDDEN = ({'error': 'Accès réservé aux administrateurs'}, 403)


def _engine_or_404(session, engine_id):
    engine = session.query(RagEngine).filter_by(id=engine_id).first()
    if not engine:
        raise LookupError('Moteur introuvable')
    return engine


def _record(session, engine, diagnosis, error=None):
    import json
    from datetime import datetime, timezone
    engine.last_check_at = datetime.now(timezone.utc)
    engine.last_check_ok = bool(diagnosis and diagnosis['ok'])
    engine.last_check_info = json.dumps(diagnosis if diagnosis else {'ok': False, 'problems': [error]})
    session.commit()


def _diagnose_or_error(base_url, key):
    try:
        return RagflowClient(base_url, key).diagnose(), None
    except RagflowError as e:
        return None, f'Connexion impossible avec ces paramètres : {e}'


@rag_engine_bp.route('/api/admin/rag/engines', methods=['GET'])
@paseto_required
def rag_engines_list():
    session = get_session()
    if not require_admin(session):
        return jsonify(_FORBIDDEN[0]), _FORBIDDEN[1]
    try:
        engines = session.query(RagEngine).order_by(RagEngine.id).all()
        return jsonify({'engines': [e.to_dict() for e in engines]})
    finally:
        session.close()


@rag_engine_bp.route('/api/admin/rag/engines', methods=['POST'])
@paseto_required
def rag_engines_create():
    session = get_session()
    admin = require_admin(session)
    if not admin:
        return jsonify(_FORBIDDEN[0]), _FORBIDDEN[1]
    try:
        data = request.get_json(silent=True) or {}
        name = (data.get('name') or '').strip()
        key = (data.get('api_key') or '').strip()
        try:
            base_url = ragflow_client.normalize_base_url(data.get('base_url'))
        except ValueError as e:
            return jsonify({'error': str(e)}), 400
        if not name or not key:
            return jsonify({'error': 'Nom, adresse et clé API requis'}), 400
        if session.query(RagEngine).filter_by(base_url=base_url).first():
            return jsonify({'error': 'Ce moteur est déjà enregistré'}), 409

        # Adresse ou clé fausse : refusé. Moteur joignable mais incomplet
        # (pas de modèle d'embedding…) : enregistré avec la liste des problèmes.
        diagnosis, error = _diagnose_or_error(base_url, key)
        if error:
            return jsonify({'error': error}), 400
        engine = RagEngine(name=name, base_url=base_url, api_key_encrypted=ragflow_client.encrypt_key(key),
                           api_key_last4=key[-4:], created_by_admin_id=admin.id,
                           # Premier moteur prêt : mis en service directement.
                           is_active=bool(diagnosis['ok'] and not session.query(RagEngine).filter_by(is_active=True).first()))
        session.add(engine)
        session.flush()
        _record(session, engine, diagnosis)
        return jsonify({'engine': engine.to_dict(), 'diagnosis': diagnosis}), 201
    except Exception as e:
        session.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        session.close()


@rag_engine_bp.route('/api/admin/rag/engines/<int:engine_id>', methods=['PUT'])
@paseto_required
def rag_engines_update(engine_id):
    session = get_session()
    if not require_admin(session):
        return jsonify(_FORBIDDEN[0]), _FORBIDDEN[1]
    try:
        engine = _engine_or_404(session, engine_id)
        data = request.get_json(silent=True) or {}
        if 'name' in data:
            if not (data['name'] or '').strip():
                return jsonify({'error': 'Le nom ne peut pas être vide'}), 400
            engine.name = data['name'].strip()
        new_url = engine.base_url
        if data.get('base_url'):
            try:
                new_url = ragflow_client.normalize_base_url(data['base_url'])
            except ValueError as e:
                return jsonify({'error': str(e)}), 400
            if session.query(RagEngine).filter(RagEngine.base_url == new_url, RagEngine.id != engine.id).first():
                return jsonify({'error': 'Cette adresse est déjà utilisée par un autre moteur'}), 409
        new_key = (data.get('api_key') or '').strip()

        diagnosis = None
        if new_url != engine.base_url or new_key:
            key = new_key or ragflow_client.decrypt_key(engine.api_key_encrypted)
            diagnosis, error = _diagnose_or_error(new_url, key)
            if error:
                return jsonify({'error': error}), 400
            engine.base_url = new_url
            if new_key:
                engine.api_key_encrypted = ragflow_client.encrypt_key(new_key)
                engine.api_key_last4 = new_key[-4:]
            _record(session, engine, diagnosis)
        else:
            session.commit()
        return jsonify({'engine': engine.to_dict(), 'diagnosis': diagnosis})
    except LookupError as e:
        return jsonify({'error': str(e)}), 404
    except RagflowError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        session.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        session.close()


@rag_engine_bp.route('/api/admin/rag/engines/<int:engine_id>', methods=['DELETE'])
@paseto_required
def rag_engines_delete(engine_id):
    session = get_session()
    if not require_admin(session):
        return jsonify(_FORBIDDEN[0]), _FORBIDDEN[1]
    try:
        engine = _engine_or_404(session, engine_id)
        if engine.is_active:
            return jsonify({'error': "Moteur en service : mettez-en un autre en service ou retirez-le du service d'abord"}), 409
        session.delete(engine)
        session.commit()
        return jsonify({'success': True})
    except LookupError as e:
        return jsonify({'error': str(e)}), 404
    finally:
        session.close()


@rag_engine_bp.route('/api/admin/rag/engines/<int:engine_id>/test', methods=['POST'])
@paseto_required
def rag_engines_test(engine_id):
    session = get_session()
    if not require_admin(session):
        return jsonify(_FORBIDDEN[0]), _FORBIDDEN[1]
    try:
        engine = _engine_or_404(session, engine_id)
        try:
            diagnosis = ragflow_client.client_for(engine).diagnose()
        except RagflowError as e:
            _record(session, engine, None, str(e))
            return jsonify({'engine': engine.to_dict(), 'diagnosis': {'ok': False, 'problems': [str(e)]}})
        _record(session, engine, diagnosis)
        return jsonify({'engine': engine.to_dict(), 'diagnosis': diagnosis})
    except LookupError as e:
        return jsonify({'error': str(e)}), 404
    finally:
        session.close()


@rag_engine_bp.route('/api/admin/rag/engines/<int:engine_id>/activate', methods=['POST'])
@paseto_required
def rag_engines_activate(engine_id):
    session = get_session()
    if not require_admin(session):
        return jsonify(_FORBIDDEN[0]), _FORBIDDEN[1]
    try:
        engine = _engine_or_404(session, engine_id)
        # Jamais de bascule vers un moteur en panne : on vérifie d'abord, et
        # l'ancien moteur reste en service tant que le nouveau n'est pas prêt.
        try:
            diagnosis = ragflow_client.client_for(engine).diagnose()
        except RagflowError as e:
            _record(session, engine, None, str(e))
            return jsonify({'error': f'Mise en service refusée : {e}'}), 400
        if not diagnosis['ok']:
            _record(session, engine, diagnosis)
            return jsonify({'error': 'Mise en service refusée : ' + ' ; '.join(diagnosis['problems']),
                            'diagnosis': diagnosis}), 400
        session.query(RagEngine).filter(RagEngine.id != engine.id).update({'is_active': False})
        engine.is_active = True
        _record(session, engine, diagnosis)   # commit unique : bascule atomique
        return jsonify({'engine': engine.to_dict(), 'diagnosis': diagnosis})
    except LookupError as e:
        return jsonify({'error': str(e)}), 404
    except Exception as e:
        session.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        session.close()


@rag_engine_bp.route('/api/admin/rag/engines/<int:engine_id>/deactivate', methods=['POST'])
@paseto_required
def rag_engines_deactivate(engine_id):
    session = get_session()
    if not require_admin(session):
        return jsonify(_FORBIDDEN[0]), _FORBIDDEN[1]
    try:
        engine = _engine_or_404(session, engine_id)
        engine.is_active = False
        session.commit()
        return jsonify({'engine': engine.to_dict()})
    except LookupError as e:
        return jsonify({'error': str(e)}), 404
    finally:
        session.close()


@rag_engine_bp.route('/api/admin/rag/engines/<int:engine_id>/status', methods=['GET'])
@paseto_required
def rag_engines_status(engine_id):
    session = get_session()
    if not require_admin(session):
        return jsonify(_FORBIDDEN[0]), _FORBIDDEN[1]
    try:
        engine = _engine_or_404(session, engine_id)
        try:
            return jsonify({'datasets': ragflow_client.client_for(engine).index_status()})
        except RagflowError as e:
            return jsonify({'error': str(e)}), 502
    except LookupError as e:
        return jsonify({'error': str(e)}), 404
    finally:
        session.close()


@rag_engine_bp.route('/api/admin/rag/engines/<int:engine_id>/reindex', methods=['POST'])
@paseto_required
def rag_engines_reindex(engine_id):
    session = get_session()
    if not require_admin(session):
        return jsonify(_FORBIDDEN[0]), _FORBIDDEN[1]
    try:
        engine = _engine_or_404(session, engine_id)
        scope = (request.get_json(silent=True) or {}).get('scope', 'failed')
        if scope not in ('failed', 'all'):
            return jsonify({'error': "scope doit valoir 'failed' ou 'all'"}), 400
        try:
            return jsonify(ragflow_client.client_for(engine).reindex(scope))
        except RagflowError as e:
            return jsonify({'error': str(e)}), 502
    except LookupError as e:
        return jsonify({'error': str(e)}), 404
    finally:
        session.close()
