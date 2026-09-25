"""
Synchronisation Moodle — Phase AA.

Admin :
  GET  /api/admin/moodle/status              connectivité + infos du service
  GET  /api/admin/moodle/courses             correspondance cours Moodle ↔ EC CEI
  POST /api/admin/moodle/sync/enrollments    inscriptions Moodle → CEI, UN cours par appel

Professeur (ou admin) :
  GET  /api/moodle/ecs                       mes EC disposant d'un cours Moodle
  GET  /api/moodle/ecs/<ec_id>/materials     fichiers de cours exploitables par l'IA

L'extraction de la matière pour l'IA passe par POST /api/ai/generate-exam-suggestions
(champs moodle_ec_id + moodle_files), pour réutiliser tout le pipeline existant.
"""
from flask import Blueprint, jsonify, request
from sqlalchemy.orm import joinedload

from auth_paseto import paseto_required, get_current_user_id
from helpers import require_admin
from models import get_session, User, UserRole, EC, UE, ECAssignment, StudentUEEnrollment
from services import moodle_sync

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


# ── Admin ────────────────────────────────────────────────────────────────────

@moodle_bp.route('/api/admin/moodle/status', methods=['GET'])
@paseto_required
def moodle_status():
    session = get_session()
    if not require_admin(session):
        return jsonify({'error': 'Accès réservé aux administrateurs'}), 403
    session.close()
    if not moodle_sync.is_enabled():
        return jsonify({'enabled': False})
    try:
        return jsonify({'enabled': True, 'connected': True, 'site': moodle_sync.site_info()})
    except (moodle_sync.MoodleError, RuntimeError) as e:
        return jsonify({'enabled': True, 'connected': False, 'error': str(e)})


@moodle_bp.route('/api/admin/moodle/courses', methods=['GET'])
@paseto_required
def moodle_courses_mapping():
    session = get_session()
    if not require_admin(session):
        return jsonify({'error': 'Accès réservé aux administrateurs'}), 403
    try:
        if not moodle_sync.is_enabled():
            return _disabled()
        try:
            courses = moodle_sync.list_courses()
        except moodle_sync.MoodleError as e:
            return _moodle_error(e)
        ecs = {ec.code: ec for ec in session.query(EC).options(joinedload(EC.ue)).all()}
        matched, unmatched = [], []
        for c in sorted(courses, key=lambda c: c['shortname']):
            ec = ecs.get(c['shortname'])
            entry = {'moodle_course_id': c['id'], 'shortname': c['shortname'], 'fullname': c['fullname']}
            if ec:
                entry.update({'ec_id': ec.id, 'ec_code': ec.code, 'ec_name': ec.name,
                              'ue_id': ec.ue_id, 'ue_code': ec.ue.code if ec.ue else None})
                matched.append(entry)
            else:
                unmatched.append(entry)
        moodle_codes = {c['shortname'] for c in courses}
        ecs_without_course = sorted(code for code in ecs if code not in moodle_codes)
        return jsonify({
            'matched': matched,
            'moodle_courses_without_ec': unmatched,
            'ecs_without_moodle_course': ecs_without_course,
            'counts': {'moodle_courses': len(courses), 'matched': len(matched),
                       'moodle_courses_without_ec': len(unmatched),
                       'ecs_without_moodle_course': len(ecs_without_course)},
        })
    finally:
        session.close()


@moodle_bp.route('/api/admin/moodle/sync/enrollments', methods=['POST'])
@paseto_required
def moodle_sync_enrollments():
    """Un cours par appel (~11 s pour ~3 400 inscrits côté Moodle) : l'appelant
    boucle sur la liste de /api/admin/moodle/courses. Uniquement additif —
    n'ajoute que les inscriptions UE manquantes, ne crée jamais de compte et
    ne retire jamais d'inscription. dry_run vaut true par défaut."""
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
            course = moodle_sync.find_course_by_code(ec_code)
            if not course:
                return jsonify({'error': f'Aucun cours Moodle avec le code {ec_code}'}), 404
            students = moodle_sync.enrolled_students(course['id'])
        except moodle_sync.MoodleError as e:
            return _moodle_error(e)

        emails = {s['email'] for s in students if s['email']}
        cei_students = {}
        email_list = list(emails)
        for i in range(0, len(email_list), 1000):
            for uid, email in session.query(User.id, User.email).filter(
                    User.role == UserRole.STUDENT,
                    User.email.in_(email_list[i:i + 1000])).all():
                cei_students[email.lower()] = uid
        already = {sid for (sid,) in session.query(StudentUEEnrollment.student_id).filter_by(ue_id=ec.ue_id).all()}

        matched_ids = set(cei_students.values())
        to_create = sorted(matched_ids - already)
        unmatched = sorted(e for e in emails if e not in cei_students)

        if not dry_run and to_create:
            session.bulk_save_objects([StudentUEEnrollment(student_id=sid, ue_id=ec.ue_id) for sid in to_create])
            session.commit()

        ue = session.query(UE).filter_by(id=ec.ue_id).first()
        return jsonify({
            'dry_run': dry_run,
            'ec_code': ec.code,
            'ue_code': ue.code if ue else None,
            'moodle_course_id': course['id'],
            'moodle_students': len(students),
            'matched_in_cei': len(matched_ids),
            'already_enrolled': len(matched_ids & already),
            'to_create' if dry_run else 'created': len(to_create),
            'unmatched_count': len(unmatched),
            'unmatched_sample': unmatched[:20],
        })
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
    même code — base du sélecteur "Depuis Moodle" de Générer Suggestions."""
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
        try:
            moodle = {c['shortname']: c for c in moodle_sync.list_courses()}
        except moodle_sync.MoodleError as e:
            return _moodle_error(e)
        return jsonify({'ecs': [
            {'ec_id': ec.id, 'ec_code': ec.code, 'ec_name': ec.name,
             'moodle_course_id': moodle[ec.code]['id'], 'moodle_course_name': moodle[ec.code]['fullname']}
            for ec in ecs if ec.code in moodle
        ]})
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
            course = moodle_sync.find_course_by_code(ec.code)
            if not course:
                return jsonify({'error': f'Aucun cours Moodle avec le code {ec.code}'}), 404
            materials = moodle_sync.course_materials(course['id'])
        except moodle_sync.MoodleError as e:
            return _moodle_error(e)
        return jsonify({
            'ec_id': ec.id, 'ec_code': ec.code, 'moodle_course_id': course['id'],
            'moodle_course_name': course['fullname'], 'max_total_mb': moodle_sync.MAX_MATERIALS_MB,
            'materials': materials,
        })
    finally:
        session.close()
