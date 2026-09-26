"""
Synchronisation Moodle — plusieurs plateformes (Phase AA/AB).

Admin — plateformes (ajoutées depuis la page Moodle, sans changement de code) :
  GET    /api/admin/moodle/instances
  POST   /api/admin/moodle/instances               ajoute (connexion vérifiée avant enregistrement)
  PUT    /api/admin/moodle/instances/<id>          modifie (token facultatif : conservé si absent)
  DELETE /api/admin/moodle/instances/<id>
  POST   /api/admin/moodle/instances/<id>/test     diagnostic complet

Admin — synchronisation :
  GET  /api/admin/moodle/courses                   correspondance cours Moodle ↔ EC CEI
  POST /api/admin/moodle/sync/course               synchronisation complète d'UN cours par appel

Professeur (ou admin) :
  GET  /api/moodle/ecs                             mes EC disposant d'un cours Moodle
  GET  /api/moodle/ecs/<ec_id>/materials           fichiers de cours exploitables par l'IA

L'extraction de la matière pour l'IA passe par POST /api/ai/generate-exam-suggestions
(champs moodle_ec_id + moodle_files), pour réutiliser tout le pipeline existant.
"""
from flask import Blueprint, jsonify, request
from sqlalchemy.orm import joinedload

from auth_paseto import paseto_required, get_current_user_id
from helpers import require_admin
from models import (get_session, User, UserRole, EC, UE, ECAssignment, StudentUEEnrollment,
                    MoodleInstance, Pole)
from services import moodle_sync
from services.moodle_sync import MoodleClient, MoodleError
from services.provisioning import sync_course

moodle_bp = Blueprint('moodle', __name__)


def _disabled():
    return jsonify({'error': 'Synchronisation Moodle désactivée (MOODLE_SYNC_ENABLED)'}), 503


def _moodle_error(e):
    return jsonify({'error': str(e)}), 502


def resolve_ec_for_user(session, ec_id, user):
    """EC accessible par l'utilisateur : admin → tous, professeur → seulement
    ceux qui lui sont affectés (ECAssignment). Lève PermissionError/LookupError."""
    ec = session.query(EC).filter_by(id=ec_id).first()
    if not ec:
        raise LookupError('EC non trouvé')
    if user.role == UserRole.ADMIN:
        return ec
    if user.role == UserRole.PROFESSOR and session.query(ECAssignment).filter_by(
            ec_id=ec_id, professor_id=user.id).first():
        return ec
    raise PermissionError("Vous n'êtes pas responsable de cet EC")


def _instance_or_404(session, instance_id):
    inst = session.query(MoodleInstance).filter_by(id=instance_id).first()
    if not inst:
        raise LookupError('Plateforme Moodle introuvable')
    return inst


# ── Admin : plateformes ──────────────────────────────────────────────────────

@moodle_bp.route('/api/admin/moodle/instances', methods=['GET'])
@paseto_required
def moodle_instances_list():
    session = get_session()
    if not require_admin(session):
        return jsonify({'error': 'Accès réservé aux administrateurs'}), 403
    try:
        if not moodle_sync.is_enabled():
            return _disabled()
        moodle_sync.active_instances(session)  # reprise éventuelle du .env
        instances = session.query(MoodleInstance).order_by(MoodleInstance.id).all()
        return jsonify({'instances': [i.to_dict() for i in instances]})
    finally:
        session.close()


def _diagnose_or_error(base_url, token):
    """Diagnostic avec des identifiants pas encore enregistrés. Renvoie
    (diagnostic, None) si la connexion aboutit, (None, message) sinon."""
    try:
        return MoodleClient(base_url, token).diagnose(), None
    except MoodleError as e:
        return None, f'Connexion impossible avec ces paramètres : {e}'


@moodle_bp.route('/api/admin/moodle/instances', methods=['POST'])
@paseto_required
def moodle_instances_create():
    session = get_session()
    admin = require_admin(session)
    if not admin:
        return jsonify({'error': 'Accès réservé aux administrateurs'}), 403
    try:
        if not moodle_sync.is_enabled():
            return _disabled()
        data = request.get_json(silent=True) or {}
        name = (data.get('name') or '').strip()
        token = (data.get('token') or '').strip()
        pole_id = data.get('pole_id') or None
        try:
            base_url = moodle_sync.normalize_base_url(data.get('base_url'))
        except ValueError as e:
            return jsonify({'error': str(e)}), 400
        if not name or not token:
            return jsonify({'error': 'Nom, adresse et token requis'}), 400
        if session.query(MoodleInstance).filter_by(base_url=base_url).first():
            return jsonify({'error': 'Cette plateforme est déjà enregistrée'}), 409
        if pole_id and not session.query(Pole).filter_by(id=pole_id).first():
            return jsonify({'error': 'Pôle introuvable'}), 400

        # Connexion vérifiée AVANT enregistrement : une adresse ou un token
        # faux est refusé. Une plateforme joignable mais incomplète (fonction
        # absente, téléchargement non autorisé) est enregistrée avec la liste
        # précise de ce qu'il reste à régler dans Moodle.
        diagnosis, error = _diagnose_or_error(base_url, token)
        if error:
            return jsonify({'error': error}), 400
        inst = MoodleInstance(name=name, base_url=base_url, token_encrypted=moodle_sync.encrypt_token(token),
                              token_last4=token[-4:], pole_id=pole_id, created_by_admin_id=admin.id)
        session.add(inst)
        session.flush()
        moodle_sync.record_check(session, inst, diagnosis)
        return jsonify({'instance': inst.to_dict(), 'diagnosis': diagnosis}), 201
    except Exception as e:
        session.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        session.close()


@moodle_bp.route('/api/admin/moodle/instances/<int:instance_id>', methods=['PUT'])
@paseto_required
def moodle_instances_update(instance_id):
    session = get_session()
    if not require_admin(session):
        return jsonify({'error': 'Accès réservé aux administrateurs'}), 403
    try:
        if not moodle_sync.is_enabled():
            return _disabled()
        inst = _instance_or_404(session, instance_id)
        data = request.get_json(silent=True) or {}
        if 'name' in data:
            if not (data['name'] or '').strip():
                return jsonify({'error': 'Le nom ne peut pas être vide'}), 400
            inst.name = data['name'].strip()
        if 'pole_id' in data:
            if data['pole_id'] and not session.query(Pole).filter_by(id=data['pole_id']).first():
                return jsonify({'error': 'Pôle introuvable'}), 400
            inst.pole_id = data['pole_id'] or None
        if 'is_active' in data:
            inst.is_active = bool(data['is_active'])

        new_url = inst.base_url
        if data.get('base_url'):
            try:
                new_url = moodle_sync.normalize_base_url(data['base_url'])
            except ValueError as e:
                return jsonify({'error': str(e)}), 400
            clash = session.query(MoodleInstance).filter(
                MoodleInstance.base_url == new_url, MoodleInstance.id != inst.id).first()
            if clash:
                return jsonify({'error': 'Cette adresse est déjà utilisée par une autre plateforme'}), 409
        new_token = (data.get('token') or '').strip()

        diagnosis = None
        if new_url != inst.base_url or new_token:
            token = new_token or moodle_sync.decrypt_token(inst.token_encrypted)
            diagnosis, error = _diagnose_or_error(new_url, token)
            if error:
                return jsonify({'error': error}), 400
            inst.base_url = new_url
            if new_token:
                inst.token_encrypted = moodle_sync.encrypt_token(new_token)
                inst.token_last4 = new_token[-4:]
            moodle_sync.record_check(session, inst, diagnosis)
        else:
            session.commit()
        return jsonify({'instance': inst.to_dict(), 'diagnosis': diagnosis})
    except LookupError as e:
        return jsonify({'error': str(e)}), 404
    except MoodleError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        session.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        session.close()


@moodle_bp.route('/api/admin/moodle/instances/<int:instance_id>', methods=['DELETE'])
@paseto_required
def moodle_instances_delete(instance_id):
    session = get_session()
    if not require_admin(session):
        return jsonify({'error': 'Accès réservé aux administrateurs'}), 403
    try:
        inst = _instance_or_404(session, instance_id)
        session.delete(inst)
        session.commit()
        return jsonify({'success': True})
    except LookupError as e:
        return jsonify({'error': str(e)}), 404
    finally:
        session.close()


@moodle_bp.route('/api/admin/moodle/instances/<int:instance_id>/test', methods=['POST'])
@paseto_required
def moodle_instances_test(instance_id):
    session = get_session()
    if not require_admin(session):
        return jsonify({'error': 'Accès réservé aux administrateurs'}), 403
    try:
        if not moodle_sync.is_enabled():
            return _disabled()
        inst = _instance_or_404(session, instance_id)
        try:
            diagnosis = moodle_sync.client_for(inst).diagnose()
        except MoodleError as e:
            moodle_sync.record_check(session, inst, None, str(e))
            return jsonify({'instance': inst.to_dict(), 'diagnosis': {'ok': False, 'problems': [str(e)]}})
        moodle_sync.record_check(session, inst, diagnosis)
        return jsonify({'instance': inst.to_dict(), 'diagnosis': diagnosis})
    except LookupError as e:
        return jsonify({'error': str(e)}), 404
    finally:
        session.close()


# ── Admin : synchronisation ──────────────────────────────────────────────────

@moodle_bp.route('/api/admin/moodle/courses', methods=['GET'])
@paseto_required
def moodle_courses_mapping():
    """Correspondance d'une plateforme (instance_id) ou de toutes les
    plateformes actives ; signale les codes présents sur plusieurs plateformes."""
    session = get_session()
    if not require_admin(session):
        return jsonify({'error': 'Accès réservé aux administrateurs'}), 403
    try:
        if not moodle_sync.is_enabled():
            return _disabled()
        instance_id = request.args.get('instance_id', type=int)
        if instance_id:
            try:
                instances = [_instance_or_404(session, instance_id)]
            except LookupError as e:
                return jsonify({'error': str(e)}), 404
        else:
            instances = moodle_sync.active_instances(session)

        ecs = {ec.code: ec for ec in session.query(EC).options(joinedload(EC.ue)).all()}
        matched, unmatched, errors, seen = [], [], [], {}
        for inst in instances:
            try:
                courses = moodle_sync.client_for(inst).list_courses()
            except MoodleError as e:
                errors.append({'instance_id': inst.id, 'instance': inst.name, 'error': str(e)})
                continue
            for c in sorted(courses, key=lambda c: c['shortname']):
                seen.setdefault(c['shortname'], []).append(inst.name)
                entry = {'instance_id': inst.id, 'instance': inst.name, 'moodle_course_id': c['id'],
                         'shortname': c['shortname'], 'fullname': c['fullname']}
                ec = ecs.get(c['shortname'])
                if ec:
                    entry.update({'ec_id': ec.id, 'ec_code': ec.code, 'ec_name': ec.name,
                                  'ue_id': ec.ue_id, 'ue_code': ec.ue.code if ec.ue else None})
                    matched.append(entry)
                else:
                    unmatched.append(entry)
        duplicates = {code: names for code, names in seen.items() if len(names) > 1}
        ecs_without_course = sorted(code for code in ecs if code not in seen)
        return jsonify({
            'matched': matched,
            'moodle_courses_without_ec': unmatched,
            'ecs_without_moodle_course': ecs_without_course,
            'duplicate_codes': duplicates,
            'errors': errors,
            'counts': {'instances': len(instances), 'matched': len(matched),
                       'moodle_courses_without_ec': len(unmatched),
                       'ecs_without_moodle_course': len(ecs_without_course),
                       'duplicate_codes': len(duplicates)},
        })
    finally:
        session.close()


@moodle_bp.route('/api/admin/moodle/sync/course', methods=['POST'])
@paseto_required
def moodle_sync_course():
    """Synchronisation complète d'UN cours (≈10 s pour ~3 400 inscrits) :
    l'appelant boucle sur la liste de /api/admin/moodle/courses et affiche la
    progression. Mêmes règles que la connexion (services/provisioning.py) ;
    uniquement additif ; dry_run vaut true par défaut."""
    session = get_session()
    if not require_admin(session):
        return jsonify({'error': 'Accès réservé aux administrateurs'}), 403
    try:
        if not moodle_sync.is_enabled():
            return _disabled()
        data = request.get_json(silent=True) or {}
        ec_code = (data.get('ec_code') or '').strip()
        dry_run = data.get('dry_run', True) is not False
        if not ec_code:
            return jsonify({'error': 'ec_code requis'}), 400
        ec = session.query(EC).filter_by(code=ec_code).first()
        if not ec:
            return jsonify({'error': f'EC {ec_code} introuvable dans CEI'}), 404
        try:
            if data.get('instance_id'):
                inst = _instance_or_404(session, data['instance_id'])
                client = moodle_sync.client_for(inst)
                course = client.find_course_by_code(ec_code)
            else:
                found = moodle_sync.find_course_for_ec(session, ec_code)
                inst, client, course = found if found else (None, None, None)
            if not course:
                return jsonify({'error': f'Aucun cours Moodle avec le code {ec_code}'}), 404
            report = sync_course(session, ec, client, course, dry_run=dry_run)
        except LookupError as e:
            return jsonify({'error': str(e)}), 404
        except MoodleError as e:
            session.rollback()
            return _moodle_error(e)
        ue = session.query(UE).filter_by(id=ec.ue_id).first()
        return jsonify({'dry_run': dry_run, 'instance_id': inst.id, 'instance': inst.name,
                        'ec_code': ec.code, 'ue_code': ue.code if ue else None,
                        'moodle_course_id': course['id'], **report})
    except Exception as e:
        session.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        session.close()


# ── Professeur ───────────────────────────────────────────────────────────────

@moodle_bp.route('/api/moodle/ecs', methods=['GET'])
@paseto_required
def moodle_my_ecs():
    """EC du professeur connecté (admin : tous) qui ont un cours Moodle du
    même code, toutes plateformes actives confondues."""
    session = get_session()
    try:
        user = session.query(User).filter_by(id=get_current_user_id()).first()
        if not user or user.role not in (UserRole.PROFESSOR, UserRole.ADMIN):
            return jsonify({'error': 'Accès non autorisé'}), 403
        if not moodle_sync.is_enabled():
            return _disabled()
        q = session.query(EC)
        if user.role == UserRole.PROFESSOR:
            q = q.join(ECAssignment, ECAssignment.ec_id == EC.id).filter(ECAssignment.professor_id == user.id)
        ecs = q.order_by(EC.code).all()
        courses = {}
        for inst in moodle_sync.active_instances(session):
            try:
                for c in moodle_sync.client_for(inst).list_courses():
                    courses.setdefault(c['shortname'], (inst, c))
            except MoodleError:
                continue  # une plateforme injoignable ne masque pas les autres
        result = []
        for ec in ecs:
            if ec.code in courses:
                inst, c = courses[ec.code]
                result.append({'ec_id': ec.id, 'ec_code': ec.code, 'ec_name': ec.name,
                               'instance': inst.name, 'moodle_course_id': c['id'],
                               'moodle_course_name': c['fullname']})
        return jsonify({'ecs': result})
    finally:
        session.close()


@moodle_bp.route('/api/moodle/ecs/<int:ec_id>/materials', methods=['GET'])
@paseto_required
def moodle_ec_materials(ec_id):
    session = get_session()
    try:
        user = session.query(User).filter_by(id=get_current_user_id()).first()
        if not user:
            return jsonify({'error': 'Accès non autorisé'}), 403
        if not moodle_sync.is_enabled():
            return _disabled()
        try:
            ec = resolve_ec_for_user(session, ec_id, user)
        except PermissionError as e:
            return jsonify({'error': str(e)}), 403
        except LookupError as e:
            return jsonify({'error': str(e)}), 404
        try:
            found = moodle_sync.find_course_for_ec(session, ec.code)
            if not found:
                return jsonify({'error': f'Aucun cours Moodle avec le code {ec.code}'}), 404
            inst, client, course = found
            materials = client.course_materials(course['id'])
        except MoodleError as e:
            return _moodle_error(e)
        return jsonify({
            'ec_id': ec.id, 'ec_code': ec.code, 'instance': inst.name,
            'moodle_course_id': course['id'], 'moodle_course_name': course['fullname'],
            'max_total_mb': moodle_sync.MAX_MATERIALS_MB, 'materials': materials,
        })
    finally:
        session.close()
