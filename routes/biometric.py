"""
Blueprint Biométrie — inscription et vérification d'un facteur biométrique
(reconnaissance faciale via face-api.js, ou empreinte digitale/Face ID via
WebAuthn) exigé à chaque accès à un examen en ligne.

Un seul facteur actif par utilisateur (BiometricEnrollment.method). La preuve
de vérification est un flag Redis à usage unique (cei:biometric:verify:{user_id},
TTL 180s, consommé atomiquement par start_exam_attempt dans routes/exams.py).

En cas d'échec répété de la reconnaissance faciale ou d'absence d'authenticator
WebAuthn, repli sur un appel LiveKit vers le surveillant/superviseur/professeur
pour une validation manuelle (routes /fallback/*).
"""
import json
import math
from datetime import datetime
from urllib.parse import urlparse

from flask import Blueprint, request, jsonify
from webauthn import (
    generate_registration_options, verify_registration_response,
    generate_authentication_options, verify_authentication_response,
    options_to_json, base64url_to_bytes,
)
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria, AuthenticatorAttachment,
    UserVerificationRequirement, ResidentKeyRequirement,
    PublicKeyCredentialDescriptor,
)

from auth_paseto import paseto_required, get_current_user_id, get_current_user_role
from models import (
    get_session, User, UserRole, OnlineExam, ExamProctor,
    ProctorGroup, ProctorGroupEC, BiometricEnrollment, BiometricMethod,
    WebauthnCredential,
)
from cache import cache_get, cache_set, cache_pop
from s3_client import upload_biometric_photo, get_snapshot_url

biometric_bp = Blueprint('biometric', __name__)

# Même seuil que exam/[id]/page.tsx (RECOG_THRESHOLD) — distance euclidienne
# entre deux descripteurs face-api.js (128 floats) en dessous de laquelle on
# considère qu'il s'agit du même visage. Garder les deux valeurs synchronisées.
RECOG_THRESHOLD = 0.55

_VERIFY_TTL = 180  # secondes de validité du flag "vérifié" avant consommation


def _rp_id_and_origin():
    """Dérive le rp_id (domaine nu) et l'origin WebAuthn depuis APP_URL."""
    import os
    app_url = os.getenv('APP_URL', 'https://dev-cei.ddns.net').rstrip('/')
    parsed = urlparse(app_url)
    return parsed.hostname or 'localhost', app_url


def _verify_key(user_id):
    return f'cei:biometric:verify:{user_id}'


def _challenge_key(user_id, purpose):
    return f'cei:biometric:challenge:{purpose}:{user_id}'


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
        devices = session.query(WebauthnCredential).filter_by(user_id=user_id).all()
        photo_url = get_snapshot_url(enrollment.photo_s3_key) if enrollment and enrollment.photo_s3_key else None
        return jsonify({
            'enrolled': enrollment is not None,
            'method': enrollment.method.value if enrollment else None,
            'photo_url': photo_url,
            'webauthn_devices': [d.to_dict() for d in devices],
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
# INSCRIPTION — WEBAUTHN
# ============================================================================

@biometric_bp.route('/api/biometric/enroll/webauthn/options', methods=['POST'])
@paseto_required
def webauthn_enroll_options():
    user_id = get_current_user_id()
    rp_id, _ = _rp_id_and_origin()
    session = get_session()
    try:
        user = session.query(User).filter_by(id=user_id).first()
        existing = session.query(WebauthnCredential).filter_by(user_id=user_id).all()
        options = generate_registration_options(
            rp_id=rp_id,
            rp_name='CEI',
            user_id=str(user_id).encode(),
            user_name=user.email or user.full_name,
            user_display_name=user.full_name,
            authenticator_selection=AuthenticatorSelectionCriteria(
                authenticator_attachment=AuthenticatorAttachment.PLATFORM,
                user_verification=UserVerificationRequirement.REQUIRED,
                resident_key=ResidentKeyRequirement.PREFERRED,
            ),
            # credential_id est stocké en HEX en base (voir .hex() dans
            # webauthn_enroll_verify/le modèle WebauthnCredential) — décoder
            # en base64url ici produisait des octets erronés, envoyés tels
            # quels au navigateur/OS (bug corrigé le 22/08, voir aussi
            # allow_credentials plus bas qui avait le même défaut).
            exclude_credentials=[
                PublicKeyCredentialDescriptor(id=bytes.fromhex(c.credential_id))
                for c in existing
            ],
        )
        cache_set(_challenge_key(user_id, 'enroll'), {'challenge': options.challenge.hex()}, ttl=120)
        return options_to_json(options), 200, {'Content-Type': 'application/json'}
    finally:
        session.close()


@biometric_bp.route('/api/biometric/enroll/webauthn/verify', methods=['POST'])
@paseto_required
def webauthn_enroll_verify():
    user_id = get_current_user_id()
    rp_id, origin = _rp_id_and_origin()
    body = request.get_json(silent=True) or {}
    device_label = body.get('device_label')

    challenge_data = cache_pop(_challenge_key(user_id, 'enroll'))
    if not challenge_data:
        return jsonify({'error': 'Session d\'inscription expirée, recommencez'}), 400

    try:
        verification = verify_registration_response(
            credential=body.get('credential'),
            expected_challenge=bytes.fromhex(challenge_data['challenge']),
            expected_rp_id=rp_id,
            expected_origin=origin,
            require_user_verification=True,
        )
    except Exception as e:
        return jsonify({'error': f'Vérification WebAuthn échouée: {e}'}), 400

    session = get_session()
    try:
        enrollment = session.query(BiometricEnrollment).filter_by(user_id=user_id).first()
        if enrollment is None:
            enrollment = BiometricEnrollment(user_id=user_id)
            session.add(enrollment)
        enrollment.method = BiometricMethod.WEBAUTHN
        enrollment.updated_at = datetime.utcnow()

        cred = WebauthnCredential(
            user_id=user_id,
            credential_id=verification.credential_id.hex(),
            public_key=verification.credential_public_key.hex(),
            sign_count=verification.sign_count,
            device_label=device_label,
            transports=json.dumps(body.get('transports', [])),
        )
        session.add(cred)
        session.commit()
        return jsonify({'success': True, 'method': 'webauthn'})
    except Exception as e:
        session.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        session.close()


@biometric_bp.route('/api/biometric/webauthn/<credential_id>', methods=['DELETE'])
@paseto_required
def webauthn_delete_credential(credential_id):
    user_id = get_current_user_id()
    session = get_session()
    try:
        cred = session.query(WebauthnCredential).filter_by(
            user_id=user_id, credential_id=credential_id
        ).first()
        if not cred:
            return jsonify({'error': 'Appareil introuvable'}), 404

        enrollment = session.query(BiometricEnrollment).filter_by(user_id=user_id).first()
        remaining = session.query(WebauthnCredential).filter(
            WebauthnCredential.user_id == user_id,
            WebauthnCredential.id != cred.id,
        ).count()
        if enrollment and enrollment.method == BiometricMethod.WEBAUTHN and remaining == 0:
            return jsonify({'error': "Impossible de supprimer le dernier appareil actif — enregistrez d'abord une autre méthode"}), 400

        session.delete(cred)
        session.commit()
        return jsonify({'success': True})
    except Exception as e:
        session.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        session.close()


@biometric_bp.route('/api/biometric/verify/webauthn/options', methods=['POST'])
@paseto_required
def webauthn_verify_options():
    user_id = get_current_user_id()
    rp_id, _ = _rp_id_and_origin()
    session = get_session()
    try:
        creds = session.query(WebauthnCredential).filter_by(user_id=user_id).all()
        if not creds:
            return jsonify({'error': 'Aucun appareil WebAuthn enregistré'}), 404

        # Même correction que exclude_credentials ci-dessus : credential_id
        # est stocké en HEX, pas en base64url. Avec base64url_to_bytes(), les
        # IDs envoyés au navigateur ne correspondaient à AUCUN authenticator
        # réel de l'appareil — Windows/Chrome ne pouvait alors pas proposer
        # directement Windows Hello/Touch ID et retombait sur le sélecteur
        # générique "clé d'accès depuis un téléphone / clé de sécurité"
        # (retour utilisateur du 22/08 : capteur d'empreinte jamais proposé).
        options = generate_authentication_options(
            rp_id=rp_id,
            user_verification=UserVerificationRequirement.REQUIRED,
            allow_credentials=[
                PublicKeyCredentialDescriptor(id=bytes.fromhex(c.credential_id))
                for c in creds
            ],
        )
        cache_set(_challenge_key(user_id, 'verify'), {'challenge': options.challenge.hex()}, ttl=120)
        return options_to_json(options), 200, {'Content-Type': 'application/json'}
    finally:
        session.close()


@biometric_bp.route('/api/biometric/verify/webauthn/verify', methods=['POST'])
@paseto_required
def webauthn_verify_verify():
    user_id = get_current_user_id()
    rp_id, origin = _rp_id_and_origin()
    body = request.get_json(silent=True) or {}
    credential = body.get('credential') or {}
    raw_id = credential.get('rawId') or credential.get('id')

    challenge_data = cache_pop(_challenge_key(user_id, 'verify'))
    if not challenge_data:
        return jsonify({'error': 'Session de vérification expirée, recommencez'}), 400

    session = get_session()
    try:
        cred_row = session.query(WebauthnCredential).filter_by(
            user_id=user_id, credential_id=base64url_to_bytes(raw_id).hex()
        ).first()
        if not cred_row:
            return jsonify({'error': 'Appareil non reconnu'}), 404

        try:
            verification = verify_authentication_response(
                credential=credential,
                expected_challenge=bytes.fromhex(challenge_data['challenge']),
                expected_rp_id=rp_id,
                expected_origin=origin,
                credential_public_key=bytes.fromhex(cred_row.public_key),
                credential_current_sign_count=cred_row.sign_count,
                require_user_verification=True,
            )
        except Exception as e:
            return jsonify({'match': False, 'error': str(e)}), 400

        cred_row.sign_count = verification.new_sign_count
        cred_row.last_used_at = datetime.utcnow()
        session.commit()

        cache_set(_verify_key(user_id), {'method': 'webauthn', 'verified_at': datetime.utcnow().isoformat()}, ttl=_VERIFY_TTL)
        return jsonify({'match': True})
    finally:
        session.close()


# ============================================================================
# REPLI — APPEL SURVEILLANT (échec répété de la reconnaissance faciale, ou
# étudiant sans authenticator WebAuthn disponible)
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
