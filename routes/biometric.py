"""
Blueprint Biométrie — inscription et vérification par reconnaissance faciale
(face-api.js) exigée à chaque accès à un examen en ligne.

Le support WebAuthn (empreinte digitale/Face ID) a été retiré le 24/08 sur
demande utilisateur (« trop encombrant pour les étudiants ») — seule la
reconnaissance faciale reste. La preuve de vérification est un flag Redis à
usage unique (cei:biometric:verify:{user_id}, TTL 180s, consommé
atomiquement par start_exam_attempt dans routes/exams.py).

En cas d'échec répété de la reconnaissance faciale, repli sur un appel
LiveKit vers le surveillant/superviseur/professeur pour une validation
manuelle (routes /fallback/*).
"""
import json
import math
from datetime import datetime

from flask import Blueprint, request, jsonify

from auth_paseto import paseto_required, get_current_user_id, get_current_user_role
from models import (
    get_session, User, OnlineExam, ExamProctor,
    ProctorGroup, ProctorGroupEC, BiometricEnrollment, BiometricMethod,
)
from cache import cache_set
from s3_client import upload_biometric_photo, get_snapshot_url

biometric_bp = Blueprint('biometric', __name__)

# Même seuil que exam/[id]/page.tsx (RECOG_THRESHOLD) — distance euclidienne
# entre deux descripteurs face-api.js (128 floats) en dessous de laquelle on
# considère qu'il s'agit du même visage. Garder les deux valeurs synchronisées.
RECOG_THRESHOLD = 0.55

_VERIFY_TTL = 180  # secondes de validité du flag "vérifié" avant consommation


def _verify_key(user_id):
    return f'cei:biometric:verify:{user_id}'


def _euclidean_distance(a, b):
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)))


def _resolve_recipients(exam, session):
    """Chaîne de résolution des destinataires du repli biométrique : surveillant(s)
    affecté(s) à l'examen → superviseur couvrant l'EC → professeur créateur.
    Même logique que _get_covering_supervisor_ids (proctoring_routes.py) et
    _notify_resume (routes/exams.py), dupliquée ici car aucun ExamAttempt
    n'existe encore à ce stade (le gate biométrique précède sa création)."""
    recipient_ids = {
        ep.proctor_id for ep in
        session.query(ExamProctor).filter_by(exam_id=exam.id).all()
    }
    if not recipient_ids and exam.subject and exam.subject.ec_id:
        groups = (
            session.query(ProctorGroup)
            .join(ProctorGroupEC, ProctorGroupEC.group_id == ProctorGroup.id)
            .filter(ProctorGroupEC.ec_id == exam.subject.ec_id)
            .all()
        )
        recipient_ids = {s.supervisor_id for g in groups for s in g.supervisors}
    if not recipient_ids and exam.created_by_id:
        recipient_ids.add(exam.created_by_id)
    return recipient_ids


# ============================================================================
# STATUT
# ============================================================================

@biometric_bp.route('/api/biometric/status', methods=['GET'])
@paseto_required
def biometric_status():
    user_id = get_current_user_id()
    session = get_session()
    try:
        enrollment = session.query(BiometricEnrollment).filter_by(user_id=user_id).first()
        photo_url = get_snapshot_url(enrollment.photo_s3_key) if enrollment and enrollment.photo_s3_key else None
        return jsonify({
            'enrolled': enrollment is not None,
            'method': enrollment.method.value if enrollment else None,
            'photo_url': photo_url,
        })
    finally:
        session.close()


# ============================================================================
# INSCRIPTION — VISAGE
# ============================================================================

@biometric_bp.route('/api/biometric/enroll/face', methods=['POST'])
@paseto_required
def enroll_face():
    user_id = get_current_user_id()
    data = request.get_json(silent=True) or {}
    descriptor = data.get('descriptor')
    image_data = data.get('image_data')
    if not isinstance(descriptor, list) or len(descriptor) != 128:
        return jsonify({'error': 'Descripteur facial invalide'}), 400

    photo_key = upload_biometric_photo(user_id, image_data) if image_data else None

    session = get_session()
    try:
        enrollment = session.query(BiometricEnrollment).filter_by(user_id=user_id).first()
        if enrollment is None:
            enrollment = BiometricEnrollment(user_id=user_id)
            session.add(enrollment)
        enrollment.method = BiometricMethod.FACE
        enrollment.face_descriptor = json.dumps(descriptor)
        if photo_key:
            enrollment.photo_s3_key = photo_key
        enrollment.updated_at = datetime.utcnow()
        session.commit()
        return jsonify({'success': True, 'method': 'face'})
    except Exception as e:
        session.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        session.close()


@biometric_bp.route('/api/biometric/verify/face', methods=['POST'])
@paseto_required
def verify_face():
    user_id = get_current_user_id()
    data = request.get_json(silent=True) or {}
    descriptor = data.get('descriptor')
    if not isinstance(descriptor, list) or len(descriptor) != 128:
        return jsonify({'error': 'Descripteur facial invalide'}), 400

    session = get_session()
    try:
        enrollment = session.query(BiometricEnrollment).filter_by(
            user_id=user_id, method=BiometricMethod.FACE
        ).first()
        if not enrollment or not enrollment.face_descriptor:
            return jsonify({'error': 'Aucune inscription faciale trouvée', 'enrolled': False}), 404

        reference = json.loads(enrollment.face_descriptor)
        distance = _euclidean_distance(descriptor, reference)
        match = distance <= RECOG_THRESHOLD
        if match:
            cache_set(_verify_key(user_id), {'method': 'face', 'verified_at': datetime.utcnow().isoformat()}, ttl=_VERIFY_TTL)
        return jsonify({'match': match, 'distance': distance})
    finally:
        session.close()


# ============================================================================
# REPLI — APPEL SURVEILLANT (échec répété de la reconnaissance faciale)
# ============================================================================

@biometric_bp.route('/api/biometric/fallback/call_request', methods=['POST'])
@paseto_required
def biometric_call_request():
    user_id = get_current_user_id()
    data = request.get_json(silent=True) or {}
    exam_id = data.get('exam_id')
    if not exam_id:
        return jsonify({'error': 'exam_id requis'}), 400

    session = get_session()
    try:
        exam = session.query(OnlineExam).filter_by(id=exam_id).first()
        student = session.query(User).filter_by(id=user_id).first()
        if not exam or not student:
            return jsonify({'error': 'Introuvable'}), 404

        student_name = student.full_name
        recipient_ids = _resolve_recipients(exam, session)
        exam_title = exam.title
        session.close()

        try:
            from notif_bus import notify_user
            for rid in recipient_ids:
                try:
                    notify_user(
                        rid, 'biometric_call_request',
                        'Appel entrant — vérification d\'identité',
                        f"{student_name} n'a pas pu être reconnu automatiquement pour « {exam_title} » et demande une vérification manuelle.",
                        priority='urgent', tags=['phone'],
                        extra={'exam_id': exam_id, 'student_id': user_id, 'student_name': student_name, 'exam_title': exam_title},
                    )
                except Exception:
                    pass
        except Exception:
            pass

        return jsonify({'success': True, 'notified': len(recipient_ids)})
    finally:
        session.close()


@biometric_bp.route('/api/biometric/fallback/private_token', methods=['GET'])
@paseto_required
def biometric_private_token():
    from proctoring_routes import generate_livekit_token, get_livekit_config

    user_id = get_current_user_id()
    role = get_current_user_role()
    exam_id = request.args.get('exam_id', type=int)
    student_id = request.args.get('student_id', type=int) or (user_id if role == 'student' else None)
    if not exam_id or not student_id:
        return jsonify({'error': 'exam_id et student_id requis'}), 400
    if role == 'student' and user_id != student_id:
        return jsonify({'error': 'Accès refusé'}), 403
    if role not in ('student', 'surveillant', 'superviseur', 'professor', 'admin'):
        return jsonify({'error': 'Accès refusé'}), 403

    config = get_livekit_config()
    if not all([config['url'], config['api_key'], config['api_secret']]):
        return jsonify({'error': 'LiveKit non configuré'}), 503

    room_name = f'biometric-{student_id}-{exam_id}'
    identity = f'{role}-{user_id}'
    token = generate_livekit_token(
        config['api_key'], config['api_secret'],
        identity, room_name,
        can_publish=True, can_subscribe=True, ttl=600,
    )
    return jsonify({'token': token, 'ws_url': config['url'], 'room': room_name, 'identity': identity})


@biometric_bp.route('/api/biometric/fallback/manual_verify', methods=['POST'])
@paseto_required
def biometric_manual_verify():
    role = get_current_user_role()
    if role not in ('surveillant', 'superviseur', 'professor', 'admin'):
        return jsonify({'error': 'Accès refusé'}), 403

    data = request.get_json(silent=True) or {}
    exam_id = data.get('exam_id')
    student_id = data.get('student_id')
    if not exam_id or not student_id:
        return jsonify({'error': 'exam_id et student_id requis'}), 400

    session = get_session()
    try:
        exam = session.query(OnlineExam).filter_by(id=exam_id).first()
        if not exam:
            return jsonify({'error': 'Examen introuvable'}), 404
        recipient_ids = _resolve_recipients(exam, session)
    finally:
        session.close()

    verifier_id = get_current_user_id()
    if verifier_id not in recipient_ids and role != 'admin':
        return jsonify({'error': "Vous n'êtes pas habilité à valider l'identité pour cet examen"}), 403

    cache_set(_verify_key(student_id), {'method': 'manual', 'verified_by': verifier_id, 'verified_at': datetime.utcnow().isoformat()}, ttl=_VERIFY_TTL)
    return jsonify({'success': True})
