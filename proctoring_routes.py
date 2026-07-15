"""
Routes de proctoring LiveKit
Surveillance en temps réel des examens en ligne
"""
from flask import Blueprint, jsonify, request, current_app
from auth_paseto import paseto_required, get_current_user_id, get_current_user_role
import jwt as pyjwt
import time
import json
import os
import urllib.request as urlreq
import urllib.error
from datetime import datetime, timezone, timedelta
import boto3
from botocore.exceptions import ClientError
from botocore.config import Config

from sqlalchemy import update as _sa_update
from sqlalchemy.orm import joinedload
from models import (
    get_session, ExamAttempt, OnlineExam, ExamActivityLog, User,
    AttemptStatus, UserRole, ExamStatus, CameraLog,
    ExamProctor, ProctorAssignment, Subject, EC, UE, StudentUEEnrollment,
    ECAssignment
)
from cache import cache_get, cache_set

proctoring_bp = Blueprint('proctoring', __name__)


# ============================================================================
# TOKEN LIVEKIT
# ============================================================================

def generate_livekit_token(api_key, api_secret, identity, room_name,
                            can_publish=True, can_subscribe=True, ttl=3600):
    """Générer un token JWT LiveKit"""
    now = int(time.time())
    payload = {
        'exp': now + ttl,
        'iss': api_key,
        'nbf': now,
        'sub': identity,
        'video': {
            'room': room_name,
            'roomJoin': True,
            'canPublish': can_publish,
            'canSubscribe': can_subscribe,
            'canPublishData': True,
        }
    }
    return pyjwt.encode(payload, api_secret, algorithm='HS256')


def get_livekit_config():
    """Récupérer la configuration LiveKit depuis les variables d'environnement"""
    url = os.environ.get('LIVEKIT_URL', '')
    # LIVEKIT_API_URL permet d'utiliser une URL HTTP directe pour les appels serveur
    # (utile si le domaine public n'est pas accessible depuis ce serveur)
    api_url = os.environ.get('LIVEKIT_API_URL') or url.replace('wss://', 'https://').replace('ws://', 'http://')
    return {
        'url': url,
        'api_url': api_url,
        'api_key': os.environ.get('LIVEKIT_API_KEY', ''),
        'api_secret': os.environ.get('LIVEKIT_API_SECRET', ''),
    }


def compute_risk_score(attempt):
    """Calculer le score de risque basé sur les événements de l'attempt (0-100)"""
    base = 0
    base += min(attempt.tab_switches * 15, 60)
    base += min(attempt.warnings_count * 5, 40)
    return min(base, 100)


# ============================================================================
# API : TOKEN LIVEKIT ÉTUDIANT
# ============================================================================

@proctoring_bp.route('/api/exam_attempts/<int:attempt_id>/livekit_token', methods=['GET'])
@paseto_required
def get_student_livekit_token(attempt_id):
    """Retourner le token LiveKit pour l'étudiant qui passe l'examen"""
    user_id = get_current_user_id()
    role = get_current_user_role()
    

    config = get_livekit_config()
    if not all([config['url'], config['api_key'], config['api_secret']]):
        return jsonify({'error': 'LiveKit non configuré sur le serveur'}), 503

    session = get_session()
    try:
        attempt = session.query(ExamAttempt).filter_by(id=attempt_id).first()
        if not attempt:
            return jsonify({'error': 'Tentative introuvable'}), 404

        # Seul l'étudiant concerné ou un prof/admin/surveillant peut appeler cet endpoint
        if role == 'student' and attempt.student_id != user_id:
            return jsonify({'error': 'Accès refusé'}), 403
        if role == 'surveillant':
            assigned = session.query(ProctorAssignment).filter_by(
                proctor_id=user_id, attempt_id=attempt_id
            ).first()
            if not assigned:
                return jsonify({'error': 'Cet étudiant ne vous est pas affecté'}), 403

        room_name = f'exam-{attempt.exam_id}'

        if role == 'student':
            identity = f'student-{user_id}'
            ttl = attempt.exam.duration_minutes * 60 + 600
            token = generate_livekit_token(
                config['api_key'], config['api_secret'],
                identity, room_name,
                can_publish=True, can_subscribe=True,
                ttl=ttl
            )
        elif role == 'surveillant':
            identity = f'proctor-{user_id}'
            token = generate_livekit_token(
                config['api_key'], config['api_secret'],
                identity, room_name,
                can_publish=True, can_subscribe=True,
                ttl=7200
            )
        else:
            identity = f'teacher-{user_id}'
            token = generate_livekit_token(
                config['api_key'], config['api_secret'],
                identity, room_name,
                can_publish=True, can_subscribe=True,
                ttl=7200
            )

        return jsonify({
            'token': token,
            'ws_url': config['url'],
            'room': room_name,
            'identity': identity
        })
    finally:
        session.close()


# ============================================================================
# API : TOKEN LIVEKIT PROFESSEUR (accès monitoring d'un examen complet)
# ============================================================================

@proctoring_bp.route('/api/online_exams/<int:exam_id>/proctor_token', methods=['GET'])
@paseto_required
def get_teacher_proctor_token(exam_id):
    """Token LiveKit pour le professeur/admin qui monitore un examen"""
    user_id = get_current_user_id()
    role = get_current_user_role()
    

    if role not in ['professor', 'admin', 'surveillant']:
        return jsonify({'error': 'Accès réservé aux enseignants et surveillants'}), 403

    config = get_livekit_config()
    if not all([config['url'], config['api_key'], config['api_secret']]):
        return jsonify({'error': 'LiveKit non configuré sur le serveur'}), 503

    session = get_session()
    try:
        exam = session.query(OnlineExam).filter_by(id=exam_id).first()
        if not exam:
            return jsonify({'error': 'Examen introuvable'}), 404

        if role == 'surveillant':
            assigned = session.query(ExamProctor).filter_by(
                exam_id=exam_id, proctor_id=user_id
            ).first()
            if not assigned:
                return jsonify({'error': 'Vous n\'êtes pas affecté à cet examen'}), 403

        room_name = f'exam-{exam_id}'
        identity = f'proctor-{user_id}' if role == 'surveillant' else f'teacher-{user_id}'
        token = generate_livekit_token(
            config['api_key'], config['api_secret'],
            identity, room_name,
            can_publish=True, can_subscribe=True,
            ttl=7200
        )

        return jsonify({
            'token': token,
            'ws_url': config['url'],
            'room': room_name,
            'identity': identity,
            'exam_title': exam.title
        })
    finally:
        session.close()


# ============================================================================
# API : ÉVÉNEMENTS PROCTORING (caméra / détection visage)
# ============================================================================

@proctoring_bp.route('/api/exam_attempts/<int:attempt_id>/proctoring_event', methods=['POST'])
@paseto_required
def log_proctoring_event(attempt_id):
    """Logger un événement de proctoring (détection visage, caméra, etc.)"""
    user_id = get_current_user_id()

    session = get_session()
    try:
        attempt = session.query(ExamAttempt).filter_by(
            id=attempt_id, student_id=user_id
        ).first()
        if not attempt:
            return jsonify({'error': 'Tentative introuvable'}), 404

        if attempt.status != AttemptStatus.IN_PROGRESS:
            return jsonify({'error': 'Tentative non active'}), 400

        data = request.get_json() or {}
        event_type = data.get('event_type', 'proctoring_event')
        event_data = data.get('event_data', '')

        # Enregistrer dans les logs d'activité
        log = ExamActivityLog(
            attempt_id=attempt_id,
            event_type=event_type,
            event_data=event_data if isinstance(event_data, str) else json.dumps(event_data)
        )
        session.add(log)

        # Augmenter le score de risque selon le type d'événement
        proctoring_risk_map = {
            'no_face_detected': 10,
            'multiple_faces': 20,
            'face_covered': 15,
            'camera_blocked': 25,
            'audio_suspicious': 10,
            'session_end': 0,
        }
        risk_increment = proctoring_risk_map.get(event_type, 5)

        if event_type != 'session_end':
            # Incrémentation atomique avec plafonnement à 100 via LEAST (évite race condition)
            from sqlalchemy import func as _sa_func
            session.execute(
                _sa_update(ExamAttempt)
                .where(ExamAttempt.id == attempt_id)
                .values(risk_score=_sa_func.least(ExamAttempt.risk_score + risk_increment, 100))
            )
            session.refresh(attempt)

        session.commit()

        # Alerte fraude si le score de risque vient de franchir 75
        if event_type != 'session_end' and attempt.risk_score >= 75:
            try:
                from notif_bus import notify_exam
                _student = attempt.student if hasattr(attempt, 'student') and attempt.student else None
                _sname   = _student.full_name if _student else f'Étudiant #{attempt.student_id}'
                notify_exam(
                    attempt.exam_id,
                    'high_risk',
                    'Alerte fraude détectée',
                    f'{_sname} — score risque : {attempt.risk_score}/100',
                    priority='urgent',
                    tags=['rotating_light'],
                )
            except Exception as _nb_err:
                import logging as _lg
                _lg.getLogger('cei.proctoring').warning('notif_bus risk error: %s', _nb_err)

        return jsonify({
            'success': True,
            'risk_score': attempt.risk_score,
            'banned': attempt.status == AttemptStatus.BANNED
        })
    finally:
        session.close()


# ============================================================================
# API : SNAPSHOT CAMÉRA (capture périodique + violations)
# ============================================================================

@proctoring_bp.route('/api/exam_attempts/<int:attempt_id>/camera_snapshot', methods=['POST'])
@paseto_required
def save_camera_snapshot(attempt_id):
    """Sauvegarder un snapshot caméra (base64 JPEG) depuis la page étudiant."""
    user_id = get_current_user_id()
    session = get_session()
    try:
        attempt = session.query(ExamAttempt).filter_by(id=attempt_id, student_id=user_id).first()
        if not attempt or attempt.status != AttemptStatus.IN_PROGRESS:
            return jsonify({'error': 'Tentative non active'}), 400

        data       = request.get_json() or {}
        image_b64  = data.get('image_data', '')
        exam_id    = attempt.exam_id

        # Upload vers MinIO — stocker la clé S3 dans image_filename
        # image_data reste NULL pour les nouvelles entrées (rétrocompat : anciens = base64)
        from s3_client import upload_snapshot
        s3_key = upload_snapshot(exam_id, attempt_id, image_b64) if image_b64 else None

        snap = CameraLog(
            attempt_id=attempt_id,
            event_type=data.get('event_type', 'periodic'),
            image_filename=s3_key,          # clé S3 (ex: snapshots/3/12/20260704T...)
            image_data=None,                # NULL pour les nouvelles entrées
            face_detected=data.get('face_detected'),
            faces_count=data.get('faces_count'),
            confidence_score=data.get('confidence_score'),
        )
        session.add(snap)
        session.commit()
        if s3_key and s3_key.startswith('local:'):
            stored = 'local_fallback'
        elif s3_key:
            stored = 's3'
        else:
            stored = 'none'
        return jsonify({'success': True, 'snapshot_id': snap.id, 'stored': stored})
    finally:
        session.close()


# ============================================================================
# API : STATUT DE RISQUE EN TEMPS RÉEL
# ============================================================================

@proctoring_bp.route('/api/exam_attempts/<int:attempt_id>/risk_status', methods=['GET'])
@paseto_required
def get_risk_status(attempt_id):
    """Retourner le score de risque et le statut de bannissement de l'étudiant"""
    user_id = get_current_user_id()
    role = get_current_user_role()
    

    session = get_session()
    try:
        attempt = session.query(ExamAttempt).filter_by(id=attempt_id).first()
        if not attempt:
            return jsonify({'error': 'Tentative introuvable'}), 404

        if role == 'student' and attempt.student_id != user_id:
            return jsonify({'error': 'Accès refusé'}), 403

        return jsonify({
            'success': True,
            'risk_score': attempt.risk_score or 0,
            'warnings_count': attempt.warnings_count,
            'tab_switches': attempt.tab_switches,
            'banned': attempt.status == AttemptStatus.BANNED,
            'ban_reason': attempt.ban_reason
        })
    finally:
        session.close()


# ============================================================================
# API : ENVOYER UN AVERTISSEMENT (prof → étudiant via réponse polling)
# ============================================================================

@proctoring_bp.route('/api/exam_attempts/<int:attempt_id>/send_warning', methods=['POST'])
@paseto_required
def send_proctoring_warning(attempt_id):
    """Prof envoie un avertissement à un étudiant (stocké en BDD, récupéré par polling)"""
    user_id = get_current_user_id()
    role = get_current_user_role()
    

    if role not in ['professor', 'admin', 'surveillant']:
        return jsonify({'error': 'Accès réservé aux enseignants et surveillants'}), 403

    session = get_session()
    try:
        attempt = session.query(ExamAttempt).filter_by(id=attempt_id).first()
        if not attempt:
            return jsonify({'error': 'Tentative introuvable'}), 404

        if role == 'surveillant':
            assigned = session.query(ProctorAssignment).filter_by(
                proctor_id=user_id, exam_id=attempt.exam_id
            ).filter(
                (ProctorAssignment.attempt_id == attempt_id) |
                (ProctorAssignment.student_id == attempt.student_id)
            ).first()
            if not assigned:
                return jsonify({'error': 'Cet étudiant ne vous est pas affecté'}), 403

        data = request.get_json() or {}
        message = data.get('message', 'Avertissement du surveillant')
        warning_type = data.get('type', 'warning')  # 'warning', 'message', 'private_call', 'end_call'

        log = ExamActivityLog(
            attempt_id=attempt_id,
            event_type=f'teacher_{warning_type}',
            event_data=json.dumps({'message': message, 'from_teacher': True,
                                   'timestamp': datetime.utcnow().isoformat()})
        )
        session.add(log)
        # N'incrémenter les avertissements que pour les types graves
        if warning_type not in ('message', 'private_call', 'end_call'):
            attempt.warnings_count += 1
        session.commit()

        return jsonify({'success': True, 'message': 'Avertissement envoyé'})
    finally:
        session.close()


# ============================================================================
# API : BANNIR UN ÉTUDIANT (prof)
# ============================================================================

@proctoring_bp.route('/api/exam_attempts/<int:attempt_id>/proctor_ban', methods=['POST'])
@paseto_required
def proctor_ban_student(attempt_id):
    """Bannir un étudiant (enseignant direct, surveillant direct + notification enseignant)"""
    user_id = get_current_user_id()
    role = get_current_user_role()
    

    if role not in ['professor', 'admin', 'surveillant']:
        return jsonify({'error': 'Accès réservé aux enseignants et surveillants'}), 403

    session = get_session()
    try:
        attempt = session.query(ExamAttempt).filter_by(id=attempt_id).first()
        if not attempt:
            return jsonify({'error': 'Tentative introuvable'}), 404

        if role == 'surveillant':
            assigned = session.query(ProctorAssignment).filter_by(
                proctor_id=user_id, exam_id=attempt.exam_id
            ).filter(
                (ProctorAssignment.attempt_id == attempt_id) |
                (ProctorAssignment.student_id == attempt.student_id)
            ).first()
            if not assigned:
                return jsonify({'error': 'Cet étudiant ne vous est pas affecté'}), 403

        data = request.get_json() or {}
        reason = data.get('reason', 'Exclu par le surveillant')

        attempt.status = AttemptStatus.BANNED
        attempt.banned_at = datetime.utcnow()
        attempt.ban_reason = reason

        actor = 'teacher_ban' if role in ['professor', 'admin'] else 'proctor_ban'
        log = ExamActivityLog(
            attempt_id=attempt_id,
            event_type=actor,
            event_data=json.dumps({
                'reason': reason,
                'banned_by_role': role,
                'banned_by_id': user_id,
                'timestamp': datetime.utcnow().isoformat()
            })
        )
        session.add(log)

        # Notification à l'enseignant si banni par un surveillant
        if role == 'surveillant':
            proctor = session.query(User).filter_by(id=user_id).first()
            proctor_name = proctor.full_name if proctor else f'Surveillant #{user_id}'
            notify_log = ExamActivityLog(
                attempt_id=attempt_id,
                event_type='teacher_message',
                event_data=json.dumps({
                    'message': f'[INFO BANNISSEMENT] {proctor_name} a exclu cet étudiant. Motif : {reason}',
                    'from_teacher': True,
                    'timestamp': datetime.utcnow().isoformat()
                })
            )
            session.add(notify_log)

        session.commit()

        # Notification temps réel : Redis + ntfy
        try:
            student_obj = session.query(User).filter_by(id=attempt.student_id).first()
            student_name = student_obj.full_name if student_obj else f'Étudiant #{attempt.student_id}'
            from notif_bus import notify_exam
            notify_exam(
                attempt.exam_id,
                'student_banned',
                'Étudiant exclu',
                f'{student_name} — motif : {reason}',
                priority='urgent',
                tags=['warning', 'skull'],
            )
        except Exception as _nb_err:
            import logging as _lg
            _lg.getLogger('cei.proctoring').warning('notif_bus ban error: %s', _nb_err)

        return jsonify({'success': True, 'message': f'Étudiant banni: {reason}'})
    finally:
        session.close()


# ============================================================================
# API : LISTE DES ÉTUDIANTS ACTIFS (pour dashboard prof)
# ============================================================================

@proctoring_bp.route('/api/online_exams/<int:exam_id>/active_proctoring', methods=['GET'])
@paseto_required
def get_active_proctoring(exam_id):
    """Liste des tentatives actives — filtrée par groupe si surveillant"""
    user_id = get_current_user_id()
    role = get_current_user_role()
    

    if role not in ['professor', 'admin', 'surveillant']:
        return jsonify({'error': 'Accès réservé aux enseignants et surveillants'}), 403

    session = get_session()
    try:
        exam = session.query(OnlineExam).filter_by(id=exam_id).first()
        if not exam:
            return jsonify({'error': 'Examen introuvable'}), 404

        if role == 'surveillant':
            # Vérifier que ce surveillant est bien affecté à cet examen
            ep_check = session.query(ExamProctor).filter_by(
                exam_id=exam_id, proctor_id=user_id
            ).first()
            if not ep_check:
                return jsonify({'error': 'Vous n\'êtes pas affecté à cet examen'}), 403

            # Récupérer les assignments (attempt_id direct OU student_id pré-affecté)
            assignments = session.query(ProctorAssignment).filter_by(
                exam_id=exam_id, proctor_id=user_id
            ).all()

            attempt_ids_direct  = [pa.attempt_id for pa in assignments if pa.attempt_id]
            student_ids_preassign = [pa.student_id for pa in assignments if pa.student_id and not pa.attempt_id]

            # Tentatives directement liées par attempt_id (joinedload évite N+1 sur .student)
            attempts_by_id = session.query(ExamAttempt).options(
                joinedload(ExamAttempt.student)
            ).filter(ExamAttempt.id.in_(attempt_ids_direct)).all() if attempt_ids_direct else []

            # Tentatives démarrées par les étudiants pré-affectés
            attempts_by_student = session.query(ExamAttempt).options(
                joinedload(ExamAttempt.student)
            ).filter(
                ExamAttempt.exam_id == exam_id,
                ExamAttempt.student_id.in_(student_ids_preassign)
            ).all() if student_ids_preassign else []

            # Fusionner sans doublons
            seen_ids = {a.id for a in attempts_by_id}
            attempts = list(attempts_by_id)
            for a in attempts_by_student:
                if a.id not in seen_ids:
                    attempts.append(a)
        else:
            attempts = session.query(ExamAttempt).options(
                joinedload(ExamAttempt.student)
            ).filter_by(exam_id=exam_id).all()

        # ── Auto-assignation des nouveaux étudiants non encore affectés ──────
        # Cas : étudiant qui a démarré l'examen sans être dans les pré-affectations
        all_exam_proctors = session.query(ExamProctor).filter_by(exam_id=exam_id).all()
        proctor_ids_list  = [ep.proctor_id for ep in all_exam_proctors]

        if proctor_ids_list and attempts:
            all_pa_now = session.query(ProctorAssignment).filter_by(exam_id=exam_id).all()
            by_attempt_id_now = {pa.attempt_id for pa in all_pa_now if pa.attempt_id}
            by_student_id_now = {pa.student_id for pa in all_pa_now if pa.student_id}

            # Compter les étudiants déjà affectés par surveillant
            proctor_counts = {pid: 0 for pid in proctor_ids_list}
            for pa in all_pa_now:
                if pa.proctor_id in proctor_counts:
                    proctor_counts[pa.proctor_id] += 1

            new_assignments = False
            for a in attempts:
                already = (a.id in by_attempt_id_now) or (a.student_id in by_student_id_now)
                if not already:
                    # Affecter au surveillant le moins chargé
                    min_pid = min(proctor_counts, key=proctor_counts.get)
                    pa_new = ProctorAssignment(
                        exam_id=exam_id,
                        proctor_id=min_pid,
                        student_id=a.student_id,
                        attempt_id=a.id,
                    )
                    session.add(pa_new)
                    proctor_counts[min_pid] += 1
                    by_student_id_now.add(a.student_id)
                    new_assignments = True

            # Mettre à jour les attempt_id manquants dans les pré-affectations
            for pa in all_pa_now:
                if pa.student_id and not pa.attempt_id:
                    found = next((a for a in attempts if a.student_id == pa.student_id), None)
                    if found:
                        pa.attempt_id = found.id
                        new_assignments = True

            if new_assignments:
                session.commit()

        # ── Reconstruire les maps après auto-assignation ───────────────────
        all_pa = session.query(ProctorAssignment).filter_by(exam_id=exam_id).all()
        by_attempt_id = {pa.attempt_id: pa.proctor_id for pa in all_pa if pa.attempt_id}
        by_student_id = {pa.student_id: pa.proctor_id for pa in all_pa if pa.student_id}

        proctor_names = {
            ep.proctor_id: ep.proctor.full_name
            for ep in all_exam_proctors
            if ep.proctor
        }

        result = []
        for a in attempts:
            pid = by_attempt_id.get(a.id) or by_student_id.get(a.student_id)
            result.append({
                'attempt_id': a.id,
                'student_id': a.student_id,
                'student_name': a.student.full_name if a.student else '?',
                'student_email': a.student.email if a.student else '',
                'status': a.status.value,
                'risk_score': a.risk_score or 0,
                'warnings_count': a.warnings_count,
                'tab_switches': a.tab_switches,
                'no_face_count': a.no_face_count or 0,
                'started_at': a.started_at.isoformat() if a.started_at else None,
                'submitted_at': a.submitted_at.isoformat() if a.submitted_at else None,
                'score': a.score,
                'banned': a.status == AttemptStatus.BANNED,
                'ban_reason': a.ban_reason if hasattr(a, 'ban_reason') else None,
                'duration_minutes': (
                    int((a.submitted_at - a.started_at).total_seconds() / 60)
                    if a.submitted_at and a.started_at else None
                ),
                'livekit_identity': f'student-{a.student_id}',
                'current_egress_id': a.current_egress_id,
                'proctor_id': pid,
                'proctor_name': proctor_names.get(pid, 'Non affecté') if pid else 'Non affecté',
                'proctor_identity': f'proctor-{pid}' if pid else None,
                'has_pre_sig': bool(a.pre_exam_signature_data),
                'pre_sig_meta': a.pre_exam_signature_meta,
                'has_post_sig': bool(a.signature_data),
            })

        # Filtrer la vue du surveillant (ne montrer que son groupe)
        if role == 'surveillant':
            result = [r for r in result if r['proctor_id'] == user_id]

        # Pour l'enseignant : infos détaillées par groupe
        proctors_info = []
        if role in ['professor', 'admin']:
            # Recalculer les counts depuis result (qui a les nouvelles assignations)
            for ep in all_exam_proctors:
                group_attempts = [r for r in result if r['proctor_id'] == ep.proctor_id]
                proctors_info.append({
                    'proctor_id': ep.proctor_id,
                    'proctor_name': ep.proctor.full_name if ep.proctor else '?',
                    'proctor_email': ep.proctor.email if ep.proctor else '',
                    'proctor_identity': f'proctor-{ep.proctor_id}',
                    'student_count': len(group_attempts),
                })

        my_identity = f'proctor-{user_id}' if role == 'surveillant' else f'teacher-{user_id}'

        return jsonify({
            'success': True,
            'exam_title': exam.title,
            'exam_status': exam.status.value,
            'attempts': result,
            'total': len(result),
            'proctors': proctors_info,
            'my_role': role,
            'my_identity': my_identity,
        })
    finally:
        session.close()


# ============================================================================
# API : SIGNATURE IMAGE (enseignant/admin uniquement)
# ============================================================================

@proctoring_bp.route('/api/exam_attempts/<int:attempt_id>/signature/<sig_type>', methods=['GET'])
@paseto_required
def get_attempt_signature(attempt_id, sig_type):
    """Retourne l'image de signature pré ou post examen (prof/admin)."""
    role = get_current_user_role()
    if role not in ['professor', 'admin']:
        return jsonify({'error': 'Accès non autorisé'}), 403
    if sig_type not in ('pre', 'post'):
        return jsonify({'error': 'Type de signature invalide'}), 400
    session = get_session()
    try:
        attempt = session.query(ExamAttempt).filter_by(id=attempt_id).first()
        if not attempt:
            return jsonify({'error': 'Tentative non trouvée'}), 404
        if sig_type == 'pre':
            return jsonify({'data': attempt.pre_exam_signature_data, 'meta': attempt.pre_exam_signature_meta})
        return jsonify({'data': attempt.signature_data, 'meta': None})
    finally:
        session.close()


# ============================================================================
# API : MESSAGES PROF EN ATTENTE (polling côté étudiant)
# ============================================================================

@proctoring_bp.route('/api/exam_attempts/<int:attempt_id>/pending_messages', methods=['GET'])
@paseto_required
def get_pending_messages(attempt_id):
    """Récupérer les messages/avertissements prof non encore lus (polling étudiant)"""
    user_id = get_current_user_id()

    session = get_session()
    try:
        attempt = session.query(ExamAttempt).filter_by(
            id=attempt_id, student_id=user_id
        ).first()
        if not attempt:
            return jsonify({'error': 'Tentative introuvable'}), 404

        # Lire les messages depuis les logs (depuis un timestamp donné)
        since_str = request.args.get('since')
        query = session.query(ExamActivityLog).filter(
            ExamActivityLog.attempt_id == attempt_id,
            ExamActivityLog.event_type.in_(['teacher_warning', 'teacher_message', 'teacher_ban', 'teacher_private_call', 'teacher_end_call'])
        )
        if since_str:
            try:
                since = datetime.fromisoformat(since_str)
                query = query.filter(ExamActivityLog.timestamp > since)
            except ValueError:
                pass

        logs = query.order_by(ExamActivityLog.timestamp.asc()).all()
        messages = []
        for log in logs:
            try:
                data = json.loads(log.event_data)
                messages.append({
                    'type': log.event_type.replace('teacher_', ''),
                    'message': data.get('message', ''),
                    'timestamp': log.timestamp.isoformat() if log.timestamp else None
                })
            except Exception:
                pass

        return jsonify({
            'success': True,
            'messages': messages,
            'banned': attempt.status == AttemptStatus.BANNED,
            'risk_score': attempt.risk_score or 0
        })
    finally:
        session.close()


# ============================================================================
# API : MESSAGE ÉTUDIANT → ENSEIGNANT
# ============================================================================

@proctoring_bp.route('/api/exam_attempts/<int:attempt_id>/student_message', methods=['POST'])
@paseto_required
def send_student_message(attempt_id):
    """Étudiant envoie un message à l'enseignant pendant l'examen"""
    user_id = get_current_user_id()
    session = get_session()
    try:
        attempt = session.query(ExamAttempt).filter_by(
            id=attempt_id, student_id=user_id
        ).first()
        if not attempt:
            return jsonify({'error': 'Tentative introuvable'}), 404

        if attempt.status.value not in ['in_progress']:
            return jsonify({'error': 'Examen non actif'}), 400

        data = request.get_json() or {}
        message = (data.get('message', '') or '').strip()
        if not message:
            return jsonify({'error': 'Message vide'}), 400

        log = ExamActivityLog(
            attempt_id=attempt_id,
            event_type='student_message',
            event_data=json.dumps({
                'message': message,
                'student_name': attempt.student.full_name if attempt.student else '?',
                'timestamp': datetime.utcnow().isoformat()
            })
        )
        session.add(log)
        session.commit()
        return jsonify({'success': True})
    finally:
        session.close()


@proctoring_bp.route('/api/online_exams/<int:exam_id>/student_messages', methods=['GET'])
@paseto_required
def get_student_messages(exam_id):
    """Enseignant/surveillant récupère les messages étudiants — filtrés par groupe si surveillant"""
    user_id = get_current_user_id()
    role = get_current_user_role()

    if role not in ['professor', 'admin', 'surveillant']:
        return jsonify({'error': 'Accès réservé aux enseignants et surveillants'}), 403

    session = get_session()
    try:
        since_str = request.args.get('since')
        query = session.query(ExamActivityLog).join(
            ExamAttempt, ExamActivityLog.attempt_id == ExamAttempt.id
        ).filter(
            ExamAttempt.exam_id == exam_id,
            ExamActivityLog.event_type == 'student_message'
        )

        # Surveillants ne voient que les messages de leur groupe
        if role == 'surveillant':
            assignments = session.query(ProctorAssignment).filter_by(
                exam_id=exam_id, proctor_id=user_id
            ).all()
            attempt_ids_direct = [pa.attempt_id for pa in assignments if pa.attempt_id]
            student_ids_pre    = [pa.student_id  for pa in assignments if pa.student_id and not pa.attempt_id]

            # Tenter d'élargir avec les tentatives des étudiants pré-affectés
            if student_ids_pre:
                extra = session.query(ExamAttempt.id).filter(
                    ExamAttempt.exam_id == exam_id,
                    ExamAttempt.student_id.in_(student_ids_pre)
                ).all()
                attempt_ids_direct += [r.id for r in extra]

            if not attempt_ids_direct:
                return jsonify({'success': True, 'messages': []})
            query = query.filter(ExamActivityLog.attempt_id.in_(attempt_ids_direct))

        if since_str:
            try:
                since = datetime.fromisoformat(since_str)
                query = query.filter(ExamActivityLog.timestamp > since)
            except ValueError:
                pass

        logs = query.order_by(ExamActivityLog.timestamp.desc()).limit(50).all()
        messages = []
        for log in logs:
            try:
                d = json.loads(log.event_data)
                messages.append({
                    'attempt_id': log.attempt_id,
                    'student_name': d.get('student_name', '?'),
                    'message': d.get('message', ''),
                    'timestamp': log.timestamp.isoformat() if log.timestamp else None,
                    'log_id': log.id
                })
            except Exception:
                pass
        return jsonify({'success': True, 'messages': messages})
    finally:
        session.close()


# ============================================================================
# API : GESTION DES SURVEILLANTS (pool + répartition)
# ============================================================================

@proctoring_bp.route('/api/online_exams/<int:exam_id>/proctors', methods=['GET'])
@paseto_required
def list_exam_proctors(exam_id):
    """Lister les surveillants affectés à un examen"""
    role = get_current_user_role()
    
    if role not in ['professor', 'admin']:
        return jsonify({'error': 'Accès réservé aux enseignants'}), 403

    session = get_session()
    try:
        exam = session.query(OnlineExam).filter_by(id=exam_id).first()
        if not exam:
            return jsonify({'error': 'Examen introuvable'}), 404

        proctors = session.query(ExamProctor).filter_by(exam_id=exam_id).all()
        # Compter les étudiants assignés (pré-affectation ou attempt)
        counts = {}
        for pa in session.query(ProctorAssignment).filter_by(exam_id=exam_id).all():
            counts[pa.proctor_id] = counts.get(pa.proctor_id, 0) + 1

        result = []
        for ep in proctors:
            d = ep.to_dict()
            d['student_count'] = counts.get(ep.proctor_id, 0)
            result.append(d)

        # Total : attempts si existent, sinon pré-affectations
        total_attempts = session.query(ExamAttempt).filter_by(exam_id=exam_id).count()
        total_preassigned = session.query(ProctorAssignment).filter_by(exam_id=exam_id).count()
        total_students = total_attempts if total_attempts > 0 else total_preassigned
        assigned_count = sum(counts.values())
        unassigned = max(0, total_students - assigned_count)

        return jsonify({
            'success': True,
            'proctors': result,
            'total_students': total_students,
            'unassigned_students': unassigned,
        })
    finally:
        session.close()


@proctoring_bp.route('/api/online_exams/<int:exam_id>/proctors', methods=['POST'])
@paseto_required
def add_exam_proctor(exam_id):
    """Affecter un surveillant à un examen"""
    user_id = get_current_user_id()
    role = get_current_user_role()
    
    if role not in ['professor', 'admin']:
        return jsonify({'error': 'Accès réservé aux enseignants'}), 403

    session = get_session()
    try:
        exam = session.query(OnlineExam).filter_by(id=exam_id).first()
        if not exam:
            return jsonify({'error': 'Examen introuvable'}), 404

        data = request.get_json() or {}
        proctor_id = data.get('proctor_id')
        if not proctor_id:
            return jsonify({'error': 'proctor_id requis'}), 400

        proctor = session.query(User).filter_by(
            id=proctor_id, role=UserRole.SURVEILLANT
        ).first()
        if not proctor:
            return jsonify({'error': 'Utilisateur introuvable ou n\'est pas un surveillant'}), 404

        existing = session.query(ExamProctor).filter_by(
            exam_id=exam_id, proctor_id=proctor_id
        ).first()
        if existing:
            return jsonify({'error': 'Ce surveillant est déjà affecté à cet examen'}), 409

        ep = ExamProctor(
            exam_id=exam_id,
            proctor_id=proctor_id,
            assigned_by_id=user_id
        )
        session.add(ep)
        session.commit()
        try:
            from notif_bus import notify_user
            notify_user(proctor_id, 'proctor_assigned', 'Nouvel examen à surveiller',
                         f'Vous surveillez « {exam.title} ».', priority='default', tags=['eyes'])
        except Exception:
            pass
        return jsonify({'success': True, 'proctor': ep.to_dict()}), 201
    finally:
        session.close()


@proctoring_bp.route('/api/online_exams/<int:exam_id>/proctors/<int:proctor_id>', methods=['DELETE'])
@paseto_required
def remove_exam_proctor(exam_id, proctor_id):
    """Retirer un surveillant d'un examen"""
    role = get_current_user_role()
    
    if role not in ['professor', 'admin']:
        return jsonify({'error': 'Accès réservé aux enseignants'}), 403

    session = get_session()
    try:
        ep = session.query(ExamProctor).filter_by(
            exam_id=exam_id, proctor_id=proctor_id
        ).first()
        if not ep:
            return jsonify({'error': 'Affectation introuvable'}), 404

        # Supprimer aussi les assignments de groupe
        session.query(ProctorAssignment).filter_by(
            exam_id=exam_id, proctor_id=proctor_id
        ).delete()
        session.delete(ep)
        session.commit()
        return jsonify({'success': True})
    finally:
        session.close()


# ── Bascule dynamique si un surveillant se déconnecte (Notes point 11) ─────────
# Chaque page de monitoring surveillant envoie un heartbeat périodique. Si un
# surveillant précédemment actif cesse d'en envoyer pendant HEARTBEAT_TTL
# secondes, ses étudiants sont automatiquement redistribués aux surveillants
# encore en ligne sur le même examen — sans action manuelle d'un admin.
HEARTBEAT_TTL = 90          # secondes sans heartbeat avant de considérer "déconnecté"
REDISTRIBUTE_COOLDOWN = 600  # évite de redéclencher en boucle pour le même surveillant


def _redistribute_attempts_excluding(exam_id, session, exclude_proctor_ids):
    """Réaffecte les tentatives en cours aux surveillants encore actifs sur cet
    examen (exclut ceux de exclude_proctor_ids). Retourne True si effectué."""
    proctors = session.query(ExamProctor).filter_by(exam_id=exam_id).all()
    active_ids = [ep.proctor_id for ep in proctors if ep.proctor_id not in exclude_proctor_ids]
    if not active_ids:
        return False
    attempts = session.query(ExamAttempt).filter_by(exam_id=exam_id, status=AttemptStatus.IN_PROGRESS).all()
    if not attempts:
        return False
    session.query(ProctorAssignment).filter_by(exam_id=exam_id).delete()
    for i, attempt in enumerate(attempts):
        pid = active_ids[i % len(active_ids)]
        session.add(ProctorAssignment(exam_id=exam_id, proctor_id=pid, student_id=attempt.student_id, attempt_id=attempt.id))
    session.commit()
    return True


def _check_disconnected_proctors(exam_id, session):
    """Détecte, parmi les surveillants affectés à l'examen, ceux dont le
    heartbeat a expiré alors qu'ils avaient déjà été vus en ligne, et
    déclenche automatiquement la redistribution de leurs étudiants."""
    proctors = session.query(ExamProctor).filter_by(exam_id=exam_id).all()
    for p in proctors:
        seen_key     = f'cei:proctor_seen:{exam_id}:{p.proctor_id}'
        live_key     = f'cei:proctor_live:{exam_id}:{p.proctor_id}'
        cooldown_key = f'cei:proctor_redistributed:{exam_id}:{p.proctor_id}'
        if cache_get(seen_key) and not cache_get(live_key) and not cache_get(cooldown_key):
            cache_set(cooldown_key, '1', ttl=REDISTRIBUTE_COOLDOWN)
            redistributed = _redistribute_attempts_excluding(exam_id, session, {p.proctor_id})
            if redistributed:
                try:
                    from notif_bus import notify_exam
                    notify_exam(exam_id, 'proctor_disconnected',
                                'Surveillant déconnecté',
                                f'{p.proctor.full_name if p.proctor else "Un surveillant"} semble déconnecté — ses étudiants ont été réaffectés automatiquement.',
                                priority='high', tags=['warning'])
                except Exception:
                    pass


@proctoring_bp.route('/api/online_exams/<int:exam_id>/proctor_heartbeat', methods=['POST'])
@paseto_required
def proctor_heartbeat(exam_id):
    """Appelé périodiquement (ex. toutes les 30s) par la page de monitoring
    d'un surveillant tant qu'elle reste ouverte. Sert aussi de déclencheur
    pour détecter si D'AUTRES surveillants de ce même examen ont disparu."""
    try:
        proctor_id = get_current_user_id()
        cache_set(f'cei:proctor_live:{exam_id}:{proctor_id}', '1', ttl=HEARTBEAT_TTL)
        cache_set(f'cei:proctor_seen:{exam_id}:{proctor_id}', '1', ttl=86400)
        session = get_session()
        try:
            _check_disconnected_proctors(exam_id, session)
        finally:
            session.close()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@proctoring_bp.route('/api/online_exams/<int:exam_id>/distribute_proctors', methods=['POST'])
@paseto_required
def distribute_proctors(exam_id):
    """Répartir automatiquement les étudiants entre les surveillants.
    - Si des ExamAttempt existent → répartition par attempt (examen en cours)
    - Sinon → répartition par inscription UE (pré-affectation avant l'examen)
    """
    role = get_current_user_role()
    
    if role not in ['professor', 'admin']:
        return jsonify({'error': 'Accès réservé aux enseignants'}), 403

    session = get_session()
    try:
        exam = session.query(OnlineExam).filter_by(id=exam_id).first()
        if not exam:
            return jsonify({'error': 'Examen introuvable'}), 404

        proctors = session.query(ExamProctor).filter_by(exam_id=exam_id).all()
        if not proctors:
            return jsonify({'error': 'Aucun surveillant affecté à cet examen'}), 400

        proctor_ids = [ep.proctor_id for ep in proctors]
        nb_proctors = len(proctor_ids)

        # ── Chercher les étudiants ──────────────────────────────────────────
        # Priorité 1 : ExamAttempts existants (examen déjà actif)
        attempts = session.query(ExamAttempt).join(
            User, ExamAttempt.student_id == User.id
        ).filter(ExamAttempt.exam_id == exam_id).order_by(User.full_name).all()

        if attempts:
            # Répartition par attempt (mode examen en cours)
            session.query(ProctorAssignment).filter_by(exam_id=exam_id).delete()
            summary = {}
            for i, attempt in enumerate(attempts):
                pid = proctor_ids[i % nb_proctors]
                pa = ProctorAssignment(
                    exam_id=exam_id,
                    proctor_id=pid,
                    student_id=attempt.student_id,
                    attempt_id=attempt.id,
                )
                session.add(pa)
                summary[pid] = summary.get(pid, 0) + 1
            total_students = len(attempts)
            mode = 'attempt'

        else:
            # Priorité 2 : étudiants inscrits à l'UE du sujet (pré-affectation)
            subject = session.query(Subject).filter_by(id=exam.subject_id).first()
            enrolled_students = []

            # Voie A : via ec_id du sujet → UE → étudiants inscrits
            if subject and subject.ec_id:
                ec = session.query(EC).filter_by(id=subject.ec_id).first()
                if ec:
                    enrolled_students = session.query(User).join(
                        StudentUEEnrollment, User.id == StudentUEEnrollment.student_id
                    ).filter(
                        StudentUEEnrollment.ue_id == ec.ue_id,
                        User.role == UserRole.STUDENT
                    ).order_by(User.full_name).all()

            # Voie B (fallback) : via ECAssignments du créateur de l'examen
            if not enrolled_students:
                creator = session.query(User).filter_by(id=exam.created_by_id).first()
                if creator and creator.role == UserRole.PROFESSOR:
                    assignments = session.query(ECAssignment).filter_by(professor_id=creator.id).all()
                    ue_ids = list({a.ec.ue_id for a in assignments if a.ec and a.ec.ue_id})
                    if ue_ids:
                        enrolled_students = session.query(User).join(
                            StudentUEEnrollment, User.id == StudentUEEnrollment.student_id
                        ).filter(
                            StudentUEEnrollment.ue_id.in_(ue_ids),
                            User.role == UserRole.STUDENT
                        ).distinct().order_by(User.full_name).all()

            if not enrolled_students:
                subject_info = f"sujet n°{exam.subject_id}" if exam.subject_id else "sujet inconnu"
                session.close()
                return jsonify({
                    'warning': f'Aucun étudiant pré-inscrit trouvé ({subject_info} — aucun EC/UE lié au sujet ni aux ECs du professeur). '
                               'Les surveillants sont bien affectés : la répartition se fera automatiquement quand les étudiants démarrent.',
                    'mode': 'lazy',
                    'proctors': nb_proctors,
                    'total_students': 0
                }), 200

            # Supprimer les anciennes pré-affectations
            session.query(ProctorAssignment).filter_by(exam_id=exam_id).delete()
            summary = {}
            for i, student in enumerate(enrolled_students):
                pid = proctor_ids[i % nb_proctors]
                pa = ProctorAssignment(
                    exam_id=exam_id,
                    proctor_id=pid,
                    student_id=student.id,
                    attempt_id=None,  # sera mis à jour quand l'étudiant démarre
                )
                session.add(pa)
                summary[pid] = summary.get(pid, 0) + 1
            total_students = len(enrolled_students)
            mode = 'pre_assignment'

        session.commit()

        proctor_summary = []
        for ep in proctors:
            proctor_summary.append({
                'proctor_id': ep.proctor_id,
                'proctor_name': ep.proctor.full_name if ep.proctor else '?',
                'student_count': summary.get(ep.proctor_id, 0),
            })

        msg_suffix = ' (pré-affectation — se confirme au démarrage de l\'examen)' if mode == 'pre_assignment' else ''
        return jsonify({
            'success': True,
            'total_students': total_students,
            'total_proctors': nb_proctors,
            'distribution': proctor_summary,
            'mode': mode,
            'message': f'{total_students} étudiants répartis entre {nb_proctors} surveillant(s){msg_suffix}',
        })
    finally:
        session.close()


@proctoring_bp.route('/api/surveillant/exams', methods=['GET'])
@paseto_required
def get_surveillant_exams():
    """Retourner les examens auxquels un surveillant est affecté"""
    user_id = get_current_user_id()
    role = get_current_user_role()
    
    if role != 'surveillant':
        return jsonify({'error': 'Réservé aux surveillants'}), 403

    session = get_session()
    try:
        exam_proctors = session.query(ExamProctor).filter_by(proctor_id=user_id).all()
        exams = []
        for ep in exam_proctors:
            if not ep.exam:
                continue
            d = ep.exam.to_dict()

            # Récupérer les affectations de cet examen pour ce surveillant
            assignments = session.query(ProctorAssignment).filter_by(
                exam_id=ep.exam_id, proctor_id=user_id
            ).all()

            students = []
            for pa in assignments:
                s_info = {
                    'student_id':    pa.student_id,
                    'student_name':  pa.student.full_name if pa.student else '—',
                    'student_email': pa.student.email    if pa.student else '—',
                    'attempt_id':    pa.attempt_id,
                    'status':        'not_started',
                    'risk_score':    0,
                }
                if pa.attempt:
                    s_info['attempt_id']  = pa.attempt.id
                    s_info['status']      = pa.attempt.status.value
                    s_info['risk_score']  = pa.attempt.risk_score or 0
                elif pa.student_id:
                    attempt = session.query(ExamAttempt).filter_by(
                        exam_id=ep.exam_id, student_id=pa.student_id
                    ).first()
                    if attempt:
                        s_info['attempt_id'] = attempt.id
                        s_info['status']     = attempt.status.value
                        s_info['risk_score'] = attempt.risk_score or 0
                students.append(s_info)

            d['my_students']      = students
            d['my_student_count'] = len(students)
            exams.append(d)

        return jsonify({'success': True, 'exams': exams})
    finally:
        session.close()


# ============================================================================
# API : ENREGISTREMENT VIDÉO (LiveKit Egress → S3)
# ============================================================================

@proctoring_bp.route('/api/exam_attempts/<int:attempt_id>/recording', methods=['POST'])
@paseto_required
def toggle_recording(attempt_id):
    """Démarrer ou arrêter l'enregistrement vidéo d'un étudiant via LiveKit Egress"""
    user_id = get_current_user_id()
    role = get_current_user_role()
    
    if role not in ['professor', 'admin', 'surveillant']:
        return jsonify({'error': 'Accès réservé aux enseignants et surveillants'}), 403

    data = request.get_json() or {}
    action = data.get('action', 'start')

    config = get_livekit_config()
    if not all([config['url'], config['api_key'], config['api_secret']]):
        return jsonify({'error': 'LiveKit non configuré'}), 503

    lk_http = config['api_url']

    session = get_session()
    try:
        attempt = session.query(ExamAttempt).filter_by(id=attempt_id).first()
        if not attempt:
            return jsonify({'error': 'Tentative introuvable'}), 404

        if role == 'surveillant':
            # Vérifier affectation par attempt_id ou student_id (pré-affectation)
            assigned = session.query(ProctorAssignment).filter_by(
                proctor_id=user_id, exam_id=attempt.exam_id
            ).filter(
                (ProctorAssignment.attempt_id == attempt_id) |
                (ProctorAssignment.student_id == attempt.student_id)
            ).first()
            if not assigned:
                return jsonify({'error': 'Cet étudiant ne vous est pas affecté'}), 403

        # Token Egress
        now = int(time.time())
        egress_payload = {
            'exp': now + 3600, 'iss': config['api_key'], 'nbf': now,
            'sub': f'recorder-{attempt_id}',
            'video': {'room': f'exam-{attempt.exam_id}', 'roomRecord': True}
        }
        egress_token = pyjwt.encode(egress_payload, config['api_secret'], algorithm='HS256')

        headers = {
            'Authorization': f'Bearer {egress_token}',
            'Content-Type': 'application/json'
        }

        if action == 'start':
            if attempt.status != AttemptStatus.IN_PROGRESS:
                return jsonify({'error': 'Enregistrement impossible : cet étudiant n\'est pas en cours d\'examen.'}), 400

            room_name = f'exam-{attempt.exam_id}'
            student_identity = f'student-{attempt.student_id}'

            # ── Vérifier présence ET caméra active via ListParticipants ──
            now2 = int(time.time())
            admin_token = pyjwt.encode(
                {'exp': now2+300,'iss': config['api_key'],'nbf': now2,
                 'sub': 'admin','video': {'roomAdmin': True, 'room': room_name}},
                config['api_secret'], algorithm='HS256'
            )
            admin_headers = {
                'Authorization': f'Bearer {admin_token}',
                'Content-Type': 'application/json'
            }
            has_video = False
            # ── Récupérer les track SIDs du participant ──────────────────────
            video_track_id = None
            audio_track_id = None
            try:
                parts_req = urlreq.Request(
                    f'{lk_http}/twirp/livekit.RoomService/ListParticipants',
                    data=json.dumps({'room': room_name}).encode(),
                    headers=admin_headers
                )
                with urlreq.urlopen(parts_req, timeout=5) as presp:
                    parts_data = json.loads(presp.read())
                    participants = parts_data.get('participants', [])
                    identities = [p.get('identity') for p in participants]
                    print(f'[REC] Participants dans {room_name}: {identities}')

                    if student_identity not in identities:
                        return jsonify({
                            'error': (
                                "L'étudiant n'est pas connecté à la salle d'examen en ce moment. "
                                "L'enregistrement individuel n'est possible que pendant que "
                                "l'étudiant passe l'examen en direct avec sa caméra activée."
                            )
                        }), 400

                    student_part = next(
                        (p for p in participants if p.get('identity') == student_identity), None
                    )
                    if student_part:
                        tracks = student_part.get('tracks', [])
                        print(f'[REC] Tracks de {student_identity}: '
                              f'{[(t.get("sid"), t.get("type"), t.get("source"), t.get("muted")) for t in tracks]}')

                        # Piste vidéo caméra : VIDEO et pas SCREEN_SHARE
                        for t in tracks:
                            if (t.get('type') in ('VIDEO', 1)
                                    and t.get('source') not in ('SCREEN_SHARE', 3)
                                    and not t.get('muted', False)):
                                video_track_id = t.get('sid')
                                break
                        # Piste audio
                        for t in tracks:
                            if t.get('type') in ('AUDIO', 0) and not t.get('muted', False):
                                audio_track_id = t.get('sid')
                                break

                        if not video_track_id:
                            return jsonify({
                                'error': (
                                    "La caméra de l'étudiant n'est pas active. "
                                    "Assurez-vous que sa vidéo est visible dans le tableau de bord "
                                    "avant de lancer l'enregistrement."
                                )
                            }), 400

            except urllib.error.HTTPError as e:
                print(f'[REC] ListParticipants HTTP error: {e.code} {e.read().decode()}')
                return jsonify({'error': "Impossible de vérifier l'état de la connexion de l'étudiant."}), 503
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                print(f'[REC] ListParticipants network error: {e}')
                return jsonify({'error': "Le service LiveKit est momentanément indisponible."}), 503

            # ── StartTrackCompositeEgress (plus fiable que StartParticipantEgress) ──
            s3_cfg = {
                'access_key': os.environ.get('S3_KEY_ID', ''),
                'secret':     os.environ.get('S3_KEY_SECRET', ''),
                'region':     os.environ.get('S3_REGION', 'us-east-1'),
                'endpoint':   os.environ.get('S3_PUBLIC_ENDPOINT', os.environ.get('S3_ENDPOINT', '')),
                'bucket':     os.environ.get('S3_BUCKET', 'livekit-recordings'),
                'force_path_style': True
            }
            filepath = (f'recordings/exam-{attempt.exam_id}/'
                        f'student-{attempt.student_id}-attempt-{attempt_id}.mp4')

            egress_body = {
                'room_name':      room_name,
                'video_track_id': video_track_id,
                'file_outputs':   [{'filepath': filepath, 's3': s3_cfg}]
            }
            if audio_track_id:
                egress_body['audio_track_id'] = audio_track_id

            req = urlreq.Request(
                f'{lk_http}/twirp/livekit.Egress/StartTrackCompositeEgress',
                data=json.dumps(egress_body).encode(), headers=headers
            )
            try:
                with urlreq.urlopen(req, timeout=8) as resp:
                    result = json.loads(resp.read())
                    egress_id = result.get('egress_id')
                    print(f'[REC] TrackComposite démarré: {egress_id} | video={video_track_id} audio={audio_track_id}')
                    attempt.current_egress_id = egress_id
                    session.commit()
                    return jsonify({'success': True, 'egress_id': egress_id, 'filepath': filepath})
            except urllib.error.HTTPError as e:
                err_body = e.read().decode()
                print(f'[REC] StartTrackCompositeEgress error: {err_body}')
                try:
                    err_json = json.loads(err_body)
                    if err_json.get('code') == 6:
                        return jsonify({'error': "Un enregistrement est déjà en cours pour cet étudiant."}), 400
                except Exception:
                    pass
                return jsonify({'error': "Erreur lors du démarrage de l'enregistrement. Réessayez."}), 500
            except (urllib.error.URLError, TimeoutError, OSError):
                return jsonify({'error': "Le service d'enregistrement est momentanément indisponible."}), 503

        elif action == 'stop':
            # Utiliser l'egress_id fourni par le client, sinon récupérer depuis la BDD
            egress_id = data.get('egress_id') or attempt.current_egress_id
            if not egress_id:
                return jsonify({'error': 'Aucun enregistrement actif pour cette tentative'}), 400
            body = json.dumps({'egress_id': egress_id}).encode()
            req = urlreq.Request(
                f'{lk_http}/twirp/livekit.Egress/StopEgress',
                data=body, headers=headers
            )
            try:
                with urlreq.urlopen(req, timeout=5) as resp:
                    # Effacer l'egress_id persisté
                    attempt.current_egress_id = None
                    session.commit()
                    return jsonify({'success': True})
            except urllib.error.HTTPError as e:
                err_body = e.read().decode()
                return jsonify({'error': "L'arrêt de l'enregistrement a échoué. Vérifiez que l'enregistrement est bien actif."}), 500
            except (urllib.error.URLError, TimeoutError, OSError):
                return jsonify({'error': "Le service d'enregistrement est momentanément indisponible. Veuillez réessayer dans quelques instants."}), 503

        return jsonify({'error': 'action invalide (start|stop)'}), 400
    finally:
        session.close()


# ============================================================================
# API : ENREGISTREMENT DE LA SALLE ENTIÈRE (RoomComposite Egress)
# ============================================================================

@proctoring_bp.route('/api/online_exams/<int:exam_id>/room_recording', methods=['POST'])
@paseto_required
def toggle_room_recording(exam_id):
    """Démarrer ou arrêter l'enregistrement de toute la salle d'examen (RoomComposite)."""
    role = get_current_user_role()
    
    if role not in ['professor', 'admin']:
        return jsonify({'error': 'Accès réservé aux enseignants'}), 403

    data = request.get_json() or {}
    action = data.get('action', 'start')

    config = get_livekit_config()
    if not all([config['url'], config['api_key'], config['api_secret']]):
        return jsonify({'error': 'LiveKit non configuré'}), 503

    lk_http = config['api_url']
    room_name = f'exam-{exam_id}'

    now = int(time.time())
    egress_payload = {
        'exp': now + 3600, 'iss': config['api_key'], 'nbf': now,
        'sub': 'room-recorder',
        'video': {'room': room_name, 'roomRecord': True}
    }
    egress_token = pyjwt.encode(egress_payload, config['api_secret'], algorithm='HS256')
    headers = {
        'Authorization': f'Bearer {egress_token}',
        'Content-Type': 'application/json'
    }

    session = get_session()
    try:
        exam = session.query(OnlineExam).filter_by(id=exam_id).first()
        if not exam:
            return jsonify({'error': 'Examen introuvable'}), 404

        if action == 'start':
            # Lister les participants pour obtenir leurs track IDs
            now2 = int(time.time())
            admin_payload = {
                'exp': now2 + 300, 'iss': config['api_key'], 'nbf': now2,
                'sub': 'admin',
                'video': {'roomAdmin': True, 'room': room_name}
            }
            admin_token = pyjwt.encode(admin_payload, config['api_secret'], algorithm='HS256')
            admin_headers = {
                'Authorization': f'Bearer {admin_token}',
                'Content-Type': 'application/json'
            }
            try:
                parts_req = urlreq.Request(
                    f'{lk_http}/twirp/livekit.RoomService/ListParticipants',
                    data=json.dumps({'room': room_name}).encode(),
                    headers=admin_headers
                )
                with urlreq.urlopen(parts_req, timeout=5) as presp:
                    parts_data = json.loads(presp.read())
                    participants = parts_data.get('participants', [])
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                print(f'[REC-ROOM] ListParticipants error: {e}')
                return jsonify({'error': 'Impossible de joindre le serveur LiveKit.'}), 503

            parts_by_id = {p['identity']: p for p in participants}
            students_in_room = [p for p in participants if p.get('identity', '').startswith('student-')]
            print(f'[REC-ROOM] Participants: {[p.get("identity") for p in participants]}')
            if not students_in_room:
                return jsonify({
                    'error': "Aucun étudiant n'est actuellement connecté à la salle. "
                             "L'enregistrement n'est possible que pendant l'examen."
                }), 400

            s3_cfg = {
                'access_key': os.environ.get('S3_KEY_ID', ''),
                'secret':     os.environ.get('S3_KEY_SECRET', ''),
                'region':     os.environ.get('S3_REGION', 'us-east-1'),
                'endpoint':   os.environ.get('S3_PUBLIC_ENDPOINT', os.environ.get('S3_ENDPOINT', '')),
                'bucket':     os.environ.get('S3_BUCKET', 'livekit-recordings'),
                'force_path_style': True
            }
            ts = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')
            all_egress_ids = []
            errors = []

            for p in students_in_room:
                identity = p['identity']
                tracks = p.get('tracks', [])
                cam_track = next((t['sid'] for t in tracks
                    if t.get('type') in ('VIDEO', 1)
                    and t.get('source') not in ('SCREEN_SHARE', 3)
                    and not t.get('muted', False)), None)
                screen_track = next((t['sid'] for t in tracks
                    if t.get('type') in ('VIDEO', 1)
                    and t.get('source') in ('SCREEN_SHARE', 3)), None)
                audio_track = next((t['sid'] for t in tracks
                    if t.get('type') in ('AUDIO', 0)
                    and not t.get('muted', False)), None)

                if not cam_track:
                    errors.append({'identity': identity, 'error': 'caméra non active'})
                    continue

                sid = identity.replace('student-', '')
                cam_path = f'recordings/exam-{exam_id}/salle-cam-{sid}-{ts}.mp4'
                cam_body = {'room_name': room_name, 'video_track_id': cam_track,
                            'file_outputs': [{'filepath': cam_path, 's3': s3_cfg}]}
                if audio_track:
                    cam_body['audio_track_id'] = audio_track
                try:
                    req = urlreq.Request(f'{lk_http}/twirp/livekit.Egress/StartTrackCompositeEgress',
                        data=json.dumps(cam_body).encode(), headers=headers)
                    with urlreq.urlopen(req, timeout=8) as resp:
                        eid = json.loads(resp.read()).get('egress_id')
                        all_egress_ids.append(eid)
                        print(f'[REC-ROOM] TrackComposite cam démarré: {identity} egress={eid}')
                except Exception as e:
                    print(f'[REC-ROOM] Erreur cam {identity}: {e}')
                    errors.append({'identity': identity, 'error': str(e)})

                if screen_track:
                    scr_path = f'recordings/exam-{exam_id}/salle-ecran-{sid}-{ts}.mp4'
                    scr_body = {'room_name': room_name, 'video_track_id': screen_track,
                                'file_outputs': [{'filepath': scr_path, 's3': s3_cfg}]}
                    if audio_track:
                        scr_body['audio_track_id'] = audio_track
                    try:
                        req = urlreq.Request(f'{lk_http}/twirp/livekit.Egress/StartTrackCompositeEgress',
                            data=json.dumps(scr_body).encode(), headers=headers)
                        with urlreq.urlopen(req, timeout=8) as resp:
                            eid = json.loads(resp.read()).get('egress_id')
                            all_egress_ids.append(eid)
                    except Exception:
                        pass

            if not all_egress_ids:
                detail = f' ({len(errors)} étudiant(s) sans caméra active)' if errors else ''
                return jsonify({'error': f'Aucun enregistrement démarré{detail}. Vérifiez que les étudiants ont leur caméra active.'}), 400

            combined_id = 'multi:' + ','.join(all_egress_ids)
            print(f'[REC-ROOM] {len(all_egress_ids)} piste(s) enregistrée(s), combined_id={combined_id}')
            return jsonify({
                'success': True,
                'egress_id': combined_id,
                'started': len(students_in_room) - len(errors),
                'errors': len(errors)
            })

        elif action == 'stop':
            egress_id = data.get('egress_id')
            if not egress_id:
                return jsonify({'error': 'egress_id requis pour arrêter'}), 400

            if egress_id.startswith('multi:'):
                ids_to_stop = [e for e in egress_id[6:].split(',') if e]
            else:
                ids_to_stop = [egress_id]

            stopped = 0
            for eid in ids_to_stop:
                body = json.dumps({'egress_id': eid}).encode()
                req = urlreq.Request(
                    f'{lk_http}/twirp/livekit.Egress/StopEgress',
                    data=body, headers=headers
                )
                try:
                    with urlreq.urlopen(req, timeout=5):
                        stopped += 1
                        print(f'[REC-ROOM] Egress arrêté: {eid}')
                except Exception as e:
                    print(f'[REC-ROOM] Erreur arrêt {eid}: {e}')

            return jsonify({'success': True, 'stopped': stopped})

        return jsonify({'error': 'action invalide (start|stop)'}), 400
    finally:
        session.close()


# ============================================================================
# API : ENREGISTREMENT DU GROUPE D'UN SURVEILLANT
# ============================================================================

@proctoring_bp.route('/api/online_exams/<int:exam_id>/group_recording', methods=['POST'])
@paseto_required
def toggle_group_recording(exam_id):
    """Démarrer ou arrêter l'enregistrement de tous les étudiants du groupe du surveillant."""
    user_id = get_current_user_id()
    role = get_current_user_role()
    
    if role not in ['professor', 'admin', 'surveillant']:
        return jsonify({'error': 'Accès réservé aux enseignants et surveillants'}), 403

    data = request.get_json() or {}
    action = data.get('action', 'start')

    config = get_livekit_config()
    if not all([config['url'], config['api_key'], config['api_secret']]):
        return jsonify({'error': 'LiveKit non configuré'}), 503

    lk_http = config['api_url']

    session = get_session()
    try:
        # Vérifier affectation
        if role == 'surveillant':
            ep_check = session.query(ExamProctor).filter_by(
                exam_id=exam_id, proctor_id=user_id
            ).first()
            if not ep_check:
                return jsonify({'error': 'Vous n\'êtes pas affecté à cet examen'}), 403

        # Identifier les tentatives du groupe
        if role == 'surveillant':
            all_pa = session.query(ProctorAssignment).filter_by(
                exam_id=exam_id, proctor_id=user_id
            ).all()
            attempt_ids_direct   = [pa.attempt_id for pa in all_pa if pa.attempt_id]
            student_ids_preassign = [pa.student_id for pa in all_pa if pa.student_id and not pa.attempt_id]
            attempts_list = list(session.query(ExamAttempt).filter(
                ExamAttempt.id.in_(attempt_ids_direct)
            ).all()) if attempt_ids_direct else []
            if student_ids_preassign:
                extra = session.query(ExamAttempt).filter(
                    ExamAttempt.exam_id == exam_id,
                    ExamAttempt.student_id.in_(student_ids_preassign)
                ).all()
                seen = {a.id for a in attempts_list}
                for a in extra:
                    if a.id not in seen:
                        attempts_list.append(a)
        else:
            # Enseignant peut déclencher pour tout l'examen
            attempts_list = session.query(ExamAttempt).filter_by(exam_id=exam_id).all()

        active_attempts = [a for a in attempts_list if a.status == AttemptStatus.IN_PROGRESS]
        if not active_attempts:
            return jsonify({'error': 'Aucun étudiant actif dans votre groupe'}), 400

        room_name = f'exam-{exam_id}'

        # Token d'administration LiveKit pour lire les pistes
        now = int(time.time())
        admin_payload = {
            'exp': now + 3600, 'iss': config['api_key'], 'nbf': now,
            'sub': 'room-recorder',
            'video': {'room': room_name, 'roomRecord': True}
        }
        egress_token = pyjwt.encode(admin_payload, config['api_secret'], algorithm='HS256')
        headers = {
            'Authorization': f'Bearer {egress_token}',
            'Content-Type': 'application/json'
        }

        s3_cfg = {
            'access_key': os.environ.get('S3_KEY_ID', ''),
            'secret':     os.environ.get('S3_KEY_SECRET', ''),
            'region':     os.environ.get('S3_REGION', 'us-east-1'),
            'endpoint':   os.environ.get('S3_PUBLIC_ENDPOINT', os.environ.get('S3_ENDPOINT', '')),
            'bucket':     os.environ.get('S3_BUCKET', 'livekit-recordings'),
            'force_path_style': True
        }

        results = []
        errors = []

        if action == 'start':
            # Récupérer les participants LiveKit pour avoir les track IDs
            now2 = int(time.time())
            admin_token2 = pyjwt.encode(
                {'exp': now2+300, 'iss': config['api_key'], 'nbf': now2,
                 'sub': 'admin', 'video': {'roomAdmin': True, 'room': room_name}},
                config['api_secret'], algorithm='HS256'
            )
            admin_h2 = {'Authorization': f'Bearer {admin_token2}', 'Content-Type': 'application/json'}
            try:
                r = urlreq.Request(f'{lk_http}/twirp/livekit.RoomService/ListParticipants',
                    data=json.dumps({'room': room_name}).encode(), headers=admin_h2)
                with urlreq.urlopen(r, timeout=5) as resp:
                    parts = {p['identity']: p for p in json.loads(resp.read()).get('participants', [])}
            except Exception:
                parts = {}

            all_egress_ids = []  # tous les egress_id démarrés (caméra + écran)

            for attempt in active_attempts:
                if attempt.current_egress_id:
                    results.append({'attempt_id': attempt.id, 'skipped': True, 'reason': 'déjà en cours'})
                    continue

                identity = f'student-{attempt.student_id}'
                participant = parts.get(identity, {})
                tracks = participant.get('tracks', [])

                # type VIDEO=1, AUDIO=0 (LiveKit TrackType proto)
                cam_track = next((t['sid'] for t in tracks
                    if t.get('type') in ('VIDEO', 1)
                    and t.get('source') not in ('SCREEN_SHARE', 3)
                    and not t.get('muted', False)), None)
                screen_track = next((t['sid'] for t in tracks
                    if t.get('type') in ('VIDEO', 1)
                    and t.get('source') in ('SCREEN_SHARE', 3)), None)
                audio_track = next((t['sid'] for t in tracks
                    if t.get('type') in ('AUDIO', 0)
                    and not t.get('muted', False)), None)

                if not cam_track:
                    errors.append({'attempt_id': attempt.id, 'error': 'caméra non active dans LiveKit'})
                    continue

                ts = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')
                base = f'recordings/exam-{exam_id}/groupe-proctor-{user_id}'

                student_egress_ids = []

                # — Enregistrement caméra (prefixe "groupe-cam-" pour le distinguer du REC individuel)
                cam_path = f'{base}/groupe-cam-{attempt.student_id}-{ts}.mp4'
                cam_body = {'room_name': room_name, 'video_track_id': cam_track,
                            'file_outputs': [{'filepath': cam_path, 's3': s3_cfg}]}
                if audio_track:
                    cam_body['audio_track_id'] = audio_track
                try:
                    req = urlreq.Request(f'{lk_http}/twirp/livekit.Egress/StartTrackCompositeEgress',
                        data=json.dumps(cam_body).encode(), headers=headers)
                    with urlreq.urlopen(req, timeout=8) as resp:
                        eid = json.loads(resp.read()).get('egress_id')
                        student_egress_ids.append(eid)
                        attempt.current_egress_id = eid
                except Exception as e:
                    errors.append({'attempt_id': attempt.id, 'track': 'cam', 'error': str(e)})

                # — Enregistrement écran partagé (si présent, prefixe "groupe-ecran-")
                if screen_track:
                    scr_path = f'{base}/groupe-ecran-{attempt.student_id}-{ts}.mp4'
                    scr_body = {'room_name': room_name, 'video_track_id': screen_track,
                                'file_outputs': [{'filepath': scr_path, 's3': s3_cfg}]}
                    if audio_track:
                        scr_body['audio_track_id'] = audio_track
                    try:
                        req = urlreq.Request(f'{lk_http}/twirp/livekit.Egress/StartTrackCompositeEgress',
                            data=json.dumps(scr_body).encode(), headers=headers)
                        with urlreq.urlopen(req, timeout=8) as resp:
                            eid = json.loads(resp.read()).get('egress_id')
                            student_egress_ids.append(eid)
                    except Exception:
                        pass  # écran optionnel — pas d'erreur bloquante

                if student_egress_ids:
                    all_egress_ids.extend(student_egress_ids)
                    results.append({'attempt_id': attempt.id, 'egress_ids': student_egress_ids,
                                    'has_screen': screen_track is not None})

            session.commit()
            if len(results) == 0:
                return jsonify({
                    'success': False,
                    'error': (f"Aucun enregistrement démarré — {len(errors)} étudiant(s) non disponible(s). "
                              "Vérifiez que les étudiants ont leur caméra active."),
                    'started': 0,
                    'errors': len(errors),
                    'failed': errors
                }), 400
            screens = sum(1 for r in results if r.get('has_screen'))
            return jsonify({
                'success': True,
                'started': len(results),
                'screens_recorded': screens,
                'errors': len(errors),
                'recordings': results,
                'all_egress_ids': all_egress_ids,
                'failed': errors
            })

        elif action == 'stop':
            # all_egress_ids envoyé par le frontend (contient caméra + écran)
            egress_ids_raw = data.get('egress_ids', [])
            if not egress_ids_raw:
                egress_ids_raw = [(a.id, a.current_egress_id) for a in active_attempts if a.current_egress_id]

            # Aplatir : accepte strings, [att_id, eid] et egress_ids simples
            flat_ids = []
            for item in egress_ids_raw:
                if isinstance(item, (list, tuple)):
                    flat_ids.append((item[0], item[1]))
                else:
                    flat_ids.append((None, item))

            stopped = 0
            for att_id, eid in flat_ids:
                req = urlreq.Request(
                    f'{lk_http}/twirp/livekit.Egress/StopEgress',
                    data=json.dumps({'egress_id': eid}).encode(), headers=headers
                )
                try:
                    with urlreq.urlopen(req, timeout=5):
                        stopped += 1
                        if att_id:
                            a = next((x for x in active_attempts if x.id == att_id), None)
                            if a:
                                a.current_egress_id = None
                except Exception:
                    pass

            session.commit()
            return jsonify({'success': True, 'stopped': stopped})

        return jsonify({'error': 'action invalide (start|stop)'}), 400
    finally:
        session.close()


# ============================================================================
# ENREGISTREMENTS CAMÉRA (snapshots + métadonnées)
# ============================================================================

@proctoring_bp.route('/api/online_exams/<int:exam_id>/recordings', methods=['GET'])
@paseto_required
def get_exam_recordings(exam_id):
    """Récupérer les snapshots caméra et informations d'enregistrement pour un examen."""
    try:
        user_id = get_current_user_id()
        role = get_current_user_role()
        session = get_session()

        if role not in ['professor', 'admin', 'surveillant']:
            session.close()
            return jsonify({'error': 'Accès réservé aux enseignants et surveillants'}), 403

        exam = session.query(OnlineExam).filter_by(id=exam_id).first()
        if not exam:
            session.close()
            return jsonify({'error': 'Examen non trouvé'}), 404

        if role == 'professor':
            user = session.query(User).filter_by(id=user_id).first()
            if not user or exam.created_by_id != user_id:
                session.close()
                return jsonify({'error': 'Accès non autorisé'}), 403

        # Récupérer les tentatives selon le rôle
        if role == 'surveillant':
            ep_check = session.query(ExamProctor).filter_by(exam_id=exam_id, proctor_id=user_id).first()
            if not ep_check:
                session.close()
                return jsonify({'error': 'Vous n\'êtes pas affecté à cet examen'}), 403
            # Uniquement les étudiants du groupe
            all_pa = session.query(ProctorAssignment).filter_by(exam_id=exam_id, proctor_id=user_id).all()
            attempt_ids_direct    = [pa.attempt_id for pa in all_pa if pa.attempt_id]
            student_ids_preassign = [pa.student_id  for pa in all_pa if pa.student_id and not pa.attempt_id]
            attempts = list(session.query(ExamAttempt).filter(
                ExamAttempt.id.in_(attempt_ids_direct)
            ).all()) if attempt_ids_direct else []
            if student_ids_preassign:
                extra = session.query(ExamAttempt).filter(
                    ExamAttempt.exam_id == exam_id,
                    ExamAttempt.student_id.in_(student_ids_preassign)
                ).all()
                seen = {a.id for a in attempts}
                for a in extra:
                    if a.id not in seen:
                        attempts.append(a)
        else:
            attempts = session.query(ExamAttempt).filter_by(exam_id=exam_id).all()

        result = []
        for attempt in attempts:
            student = session.query(User).filter_by(id=attempt.student_id).first()
            student_name = student.full_name if student else f'Étudiant #{attempt.student_id}'
            student_email = student.email if student else ''

            # Récupérer les snapshots caméra
            snapshots = session.query(CameraLog).filter_by(
                attempt_id=attempt.id
            ).order_by(CameraLog.timestamp.asc()).all()

            from s3_client import get_snapshot_url
            snaps_list = []
            for snap in snapshots:
                # Nouvelles entrées : image_filename = clé S3
                # Anciennes entrées : image_data = base64 (rétrocompat)
                if snap.image_filename and (
                    snap.image_filename.startswith('snapshots/') or snap.image_filename.startswith('local:')
                ):
                    img = get_snapshot_url(snap.image_filename)
                    img_type = 'url'
                else:
                    img = snap.image_data  # base64 legacy
                    img_type = 'base64' if snap.image_data else 'none'
                snaps_list.append({
                    'id':           snap.id,
                    'timestamp':    snap.timestamp.isoformat() if snap.timestamp else None,
                    'event_type':   snap.event_type or snap.violation_type,
                    'image_url':    img if img_type == 'url' else None,
                    'image_data':   img if img_type == 'base64' else None,
                    'face_detected': snap.face_detected,
                    'faces_count':  snap.faces_count,
                })

            result.append({
                'attempt_id': attempt.id,
                'student_name': student_name,
                'student_email': student_email,
                'status': attempt.status.value if attempt.status else attempt.status,
                'started_at': attempt.started_at.isoformat() if attempt.started_at else None,
                'submitted_at': attempt.submitted_at.isoformat() if attempt.submitted_at else None,
                'snapshots_count': len(snaps_list),
                'snapshots': snaps_list,
            })

        session.close()
        return jsonify({'exam_id': exam_id, 'students': result})

    except Exception as e:
        print(f"❌ Erreur get_exam_recordings: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


# ============================================================================
# SNAPSHOT CAMÉRA — FALLBACK DISQUE LOCAL (si MinIO indisponible)
# ============================================================================

import re as _re
from flask import send_file as _send_file

_LOCAL_SNAP_RE = _re.compile(r'^snapshots_fallback/(\d+)/(\d+)/(\d{8}T\d{6})\.jpg$')


@proctoring_bp.route('/api/proctoring/snapshot_local/<path:key>', methods=['GET'])
@paseto_required
def get_local_snapshot(key):
    """
    Sert un snapshot caméra stocké en fallback local (MinIO était indisponible
    au moment de la capture). Clé attendue : snapshots_fallback/{exam_id}/{attempt_id}/{ts}.jpg
    """
    match = _LOCAL_SNAP_RE.match(key)
    if not match:
        return jsonify({'error': 'Clé de snapshot invalide'}), 400
    exam_id, attempt_id = int(match.group(1)), int(match.group(2))

    user_id = get_current_user_id()
    role = get_current_user_role()
    session = get_session()
    try:
        if role not in ['professor', 'admin', 'surveillant']:
            return jsonify({'error': 'Accès réservé aux enseignants et surveillants'}), 403

        exam = session.query(OnlineExam).filter_by(id=exam_id).first()
        if not exam:
            return jsonify({'error': 'Examen non trouvé'}), 404

        if role == 'professor' and exam.created_by_id != user_id:
            return jsonify({'error': 'Accès non autorisé'}), 403

        if role == 'surveillant':
            assigned = session.query(ExamProctor).filter_by(exam_id=exam_id, proctor_id=user_id).first()
            if not assigned:
                return jsonify({'error': "Vous n'êtes pas affecté à cet examen"}), 403

        attempt = session.query(ExamAttempt).filter_by(id=attempt_id, exam_id=exam_id).first()
        if not attempt:
            return jsonify({'error': 'Tentative non trouvée'}), 404
    finally:
        session.close()

    from s3_client import _UPLOAD_FOLDER
    abs_path = os.path.join(_UPLOAD_FOLDER, key)
    if not os.path.isfile(abs_path):
        return jsonify({'error': 'Fichier introuvable'}), 404
    return _send_file(abs_path, mimetype='image/jpeg')


# ============================================================================
# VIDÉOS D'ENREGISTREMENT S3 (LiveKit Egress)
# ============================================================================

from concurrent.futures import ThreadPoolExecutor, TimeoutError as _FuturesTimeout

def _get_s3_client():
    """Créer un client boto3 configuré pour le MinIO/S3 de l'application."""
    return boto3.client(
        's3',
        endpoint_url=os.environ.get('S3_PUBLIC_ENDPOINT', os.environ.get('S3_ENDPOINT', '')),
        aws_access_key_id=os.environ.get('S3_KEY_ID', ''),
        aws_secret_access_key=os.environ.get('S3_KEY_SECRET', ''),
        region_name=os.environ.get('S3_REGION', 'us-east-1'),
        config=Config(
            signature_version='s3v4',
            s3={'addressing_style': 'path'},
            connect_timeout=3,
            read_timeout=10,
            retries={'max_attempts': 1},
        )
    )


# ============================================================================
# API : TOKEN LIVEKIT SESSION PRIVÉE (surveillant ↔ étudiant)
# ============================================================================

@proctoring_bp.route('/api/exam_attempts/<int:attempt_id>/private_token', methods=['GET'])
@paseto_required
def get_private_session_token(attempt_id):
    """Token LiveKit pour session privée surveillant ↔ étudiant"""
    user_id = get_current_user_id()
    role = get_current_user_role()

    config = get_livekit_config()
    if not all([config['url'], config['api_key'], config['api_secret']]):
        return jsonify({'error': 'LiveKit non configuré'}), 503

    session = get_session()
    try:
        attempt = session.query(ExamAttempt).filter_by(id=attempt_id).first()
        if not attempt:
            return jsonify({'error': 'Tentative introuvable'}), 404

        if role == 'student' and attempt.student_id != user_id:
            return jsonify({'error': 'Accès refusé'}), 403
        if role not in ['student', 'professor', 'admin', 'surveillant']:
            return jsonify({'error': 'Accès refusé'}), 403

        room_name = f'private-{attempt_id}'
        identity  = f'{role}-{user_id}'

        token = generate_livekit_token(
            config['api_key'], config['api_secret'],
            identity, room_name,
            can_publish=True, can_subscribe=True,
            ttl=1800
        )

        return jsonify({
            'token': token,
            'ws_url': config['url'],
            'room': room_name,
            'identity': identity
        })
    finally:
        session.close()


@proctoring_bp.route('/api/online_exams/<int:exam_id>/video_recordings', methods=['GET'])
@paseto_required
def get_video_recordings(exam_id):
    """Lister uniquement les vidéos enregistrées par le surveillant pour cet examen.

    Format CEI : recordings/exam-{exam_id}/student-{sid}-attempt-{aid}.mp4
    Format LiveKit natif : recordings/{date}/exam-{exam_id}/*.mp4
    Aucun autre enregistrement n'est retourné.
    """
    import re as _re
    try:
        user_id = get_current_user_id()
        session = get_session()

        user = session.query(User).filter_by(id=user_id).first()
        if not user or user.role not in [UserRole.PROFESSOR, UserRole.ADMIN, UserRole.SURVEILLANT]:
            session.close()
            return jsonify({'error': 'Accès réservé aux enseignants et surveillants'}), 403

        exam = session.query(OnlineExam).filter_by(id=exam_id).first()
        if not exam:
            session.close()
            return jsonify({'error': 'Examen non trouvé'}), 404

        if user.role == UserRole.PROFESSOR and exam.created_by_id != user_id:
            session.close()
            return jsonify({'error': 'Accès non autorisé'}), 403

        # Construire index attempt_id → étudiant (surveillant = seulement ses étudiants)
        if user.role == UserRole.SURVEILLANT:
            assigned_ids = {
                pa.attempt_id for pa in session.query(ProctorAssignment).filter_by(proctor_id=user_id).all()
            }
            attempts = session.query(ExamAttempt).filter(
                ExamAttempt.exam_id == exam_id,
                ExamAttempt.id.in_(assigned_ids)
            ).all()
        else:
            attempts = session.query(ExamAttempt).filter_by(exam_id=exam_id).all()
        attempt_map = {}
        for a in attempts:
            student = session.query(User).filter_by(id=a.student_id).first()
            attempt_map[a.id] = {
                'student_id': a.student_id,
                'student_name': student.full_name if student else f'Étudiant #{a.student_id}',
                'status': a.status.value if a.status else str(a.status),
                'started_at': a.started_at.isoformat() if a.started_at else None,
                'submitted_at': a.submitted_at.isoformat() if a.submitted_at else None,
            }
        session.close()

        bucket = os.environ.get('S3_BUCKET', 'livekit-recordings')

        try:
            s3 = _get_s3_client()
        except Exception as e:
            return jsonify({'exam_id': exam_id, 'videos': [], 'error': f'Connexion S3 impossible: {e}'})

        def _list_all_objects(s3_client, bkt, eid):
            objs = []
            pag = s3_client.get_paginator('list_objects_v2')
            try:
                resp = s3_client.list_objects_v2(Bucket=bkt, Prefix=f'recordings/exam-{eid}/')
                objs.extend(resp.get('Contents', []))
            except Exception:
                pass
            lk_room = f'exam-{eid}'
            try:
                for page in pag.paginate(Bucket=bkt, Prefix='recordings/'):
                    for obj in page.get('Contents', []):
                        parts = obj['Key'].split('/')
                        if len(parts) >= 4 and parts[2] == lk_room:
                            objs.append(obj)
            except Exception:
                pass
            old_prefixes = [f'proctoring-{eid}-', f'surveillance-{eid}-']
            try:
                for page in pag.paginate(Bucket=bkt):
                    for obj in page.get('Contents', []):
                        k = obj['Key']
                        fname = k.split('/')[-1]
                        if any(fname.startswith(p) or k.startswith(p) for p in old_prefixes):
                            objs.append(obj)
            except Exception:
                pass
            return objs

        with ThreadPoolExecutor(max_workers=1) as _pool:
            _future = _pool.submit(_list_all_objects, s3, bucket, exam_id)
            try:
                all_objects = _future.result(timeout=15)
            except _FuturesTimeout:
                return jsonify({'exam_id': exam_id, 'videos': [],
                                'error': 'Délai S3 dépassé (serveur lent ou inaccessible)'})

        # Dédupliquer par clé
        seen = set()
        unique_objects = []
        for obj in all_objects:
            if obj['Key'] not in seen:
                seen.add(obj['Key'])
                unique_objects.append(obj)

        videos = []
        for obj in unique_objects:
            key = obj['Key']
            if not (key.endswith('.mp4') or key.endswith('.webm')):
                continue
            if obj['Size'] == 0:
                continue

            filename = key.split('/')[-1]
            size_mb = round(obj['Size'] / (1024 * 1024), 2)
            last_modified = obj['LastModified'].isoformat() if obj.get('LastModified') else None

            # Identifier l'étudiant selon le format du fichier
            attempt_id = None
            student_name = 'Enregistrement de salle'
            student_status = ''
            started_at = None
            submitted_at = None

            m_attempt = _re.search(r'attempt-(\d+)', key)
            m_student = _re.search(r'(?:user|utilisateur|student|groupe-cam|groupe-ecran)[_-](\d+)', key)
            m_room = _re.search(r'/room-\d{8}-\d{6}', key)

            # Détecter le type d'enregistrement pour l'étiquette
            is_group_cam    = 'groupe-cam-'   in filename
            is_group_screen = 'groupe-ecran-' in filename
            is_individual   = _re.search(r'student-\d+-attempt-', filename) is not None

            if m_attempt:
                attempt_id = int(m_attempt.group(1))
                info = attempt_map.get(attempt_id, {})
                student_name = info.get('student_name', f'Étudiant (tentative #{attempt_id})')
                student_status = info.get('status', '')
                started_at = info.get('started_at')
                submitted_at = info.get('submitted_at')
            elif m_student:
                sid = int(m_student.group(1))
                # Chercher dans attempt_map par student_id
                for aid, info in attempt_map.items():
                    if info.get('student_id') == sid:
                        attempt_id = aid
                        student_name = info.get('student_name', f'Étudiant #{sid}')
                        student_status = info.get('status', '')
                        started_at = info.get('started_at')
                        submitted_at = info.get('submitted_at')
                        break
                else:
                    student_name = f'Étudiant #{sid}'
            elif m_room:
                student_name = f'Enregistrement salle — {filename}'

            # URL présignée valable 4h
            try:
                url = s3.generate_presigned_url(
                    'get_object',
                    Params={'Bucket': bucket, 'Key': key},
                    ExpiresIn=14400
                )
            except Exception:
                url = None

            if is_group_cam:
                rec_type = 'groupe-caméra'
            elif is_group_screen:
                rec_type = 'groupe-écran'
            elif is_individual:
                rec_type = 'individuel'
            else:
                rec_type = 'salle'

            videos.append({
                'key': key,
                'filename': filename,
                'size_mb': size_mb,
                'last_modified': last_modified,
                'attempt_id': attempt_id,
                'student_name': student_name,
                'student_status': student_status,
                'started_at': started_at,
                'submitted_at': submitted_at,
                'url': url,
                'rec_type': rec_type,
            })

        # Trier par nom d'étudiant puis par date
        videos.sort(key=lambda v: (v['student_name'], v['last_modified'] or ''))

        return jsonify({
            'exam_id': exam_id,
            'videos': videos,
            'attempts_total': len(attempt_map),
            'recorded_count': len(videos),
        })

    except Exception as e:
        print(f"❌ Erreur get_video_recordings: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


# ============================================================================
# AGENT PROCTOR — Endpoints pour le service de surveillance autonome
# Authentification par X-Agent-Secret (indépendant du JWT)
# ============================================================================

import json as _json_mod
import os as _os_mod
import logging as _logging
import redis as _redis_alerts_lib

_alerts_log  = _logging.getLogger('cei.agent')
_MAX_ALERTS  = 200                          # nb max d'alertes en Redis
_ALERTS_KEY  = 'cei:agent:alerts'           # Redis List  — alertes JSON
_READ_KEY    = 'cei:agent:alerts:read'      # Redis Set   — attempt_ids lus
_REDIS_URL   = _os_mod.getenv('REDIS_URL', 'redis://127.0.0.1:6379/0')


def _redis_alerts():
    """Connexion Redis dédiée aux alertes agent (courte durée)."""
    return _redis_alerts_lib.from_url(
        _REDIS_URL, decode_responses=True, socket_connect_timeout=1)


def _agent_auth():
    """Vérifie le secret de l'agent dans le header X-Agent-Secret."""
    secret = _os_mod.getenv('AGENT_SECRET_KEY', 'changeme-agent-secret-key')
    return request.headers.get('X-Agent-Secret') == secret


def _load_alerts() -> list:
    """Charge les alertes depuis Redis (List + Set des IDs lus)."""
    try:
        r = _redis_alerts()
        raw      = r.lrange(_ALERTS_KEY, 0, -1)
        read_ids = {int(x) for x in r.smembers(_READ_KEY) if x.isdigit()}
        r.close()
        alerts = []
        for item in raw:
            try:
                a = _json_mod.loads(item)
                a['read'] = a.get('attempt_id') in read_ids
                alerts.append(a)
            except Exception:
                pass
        return alerts
    except Exception as exc:
        _alerts_log.warning('Redis load alerts failed: %s', exc)
        return []


def _push_alert(alert: dict) -> None:
    """Pousse une alerte dans la List Redis (max _MAX_ALERTS entrées)."""
    try:
        r = _redis_alerts()
        r.lpush(_ALERTS_KEY, _json_mod.dumps(alert, ensure_ascii=False))
        r.ltrim(_ALERTS_KEY, 0, _MAX_ALERTS - 1)
        r.close()
    except Exception as exc:
        _alerts_log.warning('Redis push alert failed: %s', exc)


def _mark_read(attempt_ids: set) -> None:
    """Marque des attempt_ids comme lus dans le Set Redis."""
    if not attempt_ids:
        return
    try:
        r = _redis_alerts()
        r.sadd(_READ_KEY, *[str(aid) for aid in attempt_ids])
        r.close()
    except Exception as exc:
        _alerts_log.warning('Redis mark read failed: %s', exc)


@proctoring_bp.route('/api/agent/alerts', methods=['POST'])
def agent_push_alert():
    """L'agent autonome pousse une nouvelle alerte (stockée dans Redis)."""
    if not _agent_auth():
        return jsonify({'error': 'Non autorisé'}), 403
    data = request.get_json(silent=True) or {}
    if not data.get('attempt_id') or not data.get('student_name'):
        return jsonify({'error': 'Données incomplètes'}), 400
    data['read'] = False
    _push_alert(data)

    # Notification temps réel vers le dashboard du surveillant
    try:
        exam_id = data.get('exam_id')
        if exam_id:
            from notif_bus import notify_exam
            notify_exam(
                int(exam_id),
                'agent_alert',
                'Alerte agent autonome',
                f"{data['student_name']} — {data.get('alert_type', 'anomalie détectée')}",
                priority='urgent',
                tags=['rotating_light'],
            )
    except Exception as _ne:
        _alerts_log.warning('notif_bus agent_alert: %s', _ne)

    return jsonify({'success': True})


@proctoring_bp.route('/api/agent/alerts', methods=['GET'])
@paseto_required
def agent_get_alerts():
    """Dashboard : récupère les alertes non lues (prof / surveillant)."""
    role = get_current_user_role()
    if role not in ['professor', 'admin', 'surveillant']:
        return jsonify({'error': 'Accès non autorisé'}), 403
    alerts = _load_alerts()

    # Auto-marquer comme lues les alertes d'étudiants qui ne sont plus en cours
    unread_attempt_ids = list({
        a.get('attempt_id') for a in alerts
        if not a.get('read') and a.get('attempt_id') is not None
    })
    if unread_attempt_ids:
        session = get_session()
        try:
            existing = session.query(ExamAttempt).filter(
                ExamAttempt.id.in_(unread_attempt_ids)
            ).all()
            existing_ids = {att.id for att in existing}
            # Marquer comme lues : (1) étudiants qui ne sont plus en cours
            # et (2) attempt_id inconnu en DB (alertes orphelines/obsolètes)
            stale_ids = (
                {att.id for att in existing if att.status != AttemptStatus.IN_PROGRESS}
                | (set(unread_attempt_ids) - existing_ids)
            )
        except Exception:
            stale_ids = set()
        finally:
            session.close()
        if stale_ids:
            _mark_read(stale_ids)
            for a in alerts:
                if a.get('attempt_id') in stale_ids:
                    a['read'] = True

    unread = [a for a in alerts if not a.get('read')]
    return jsonify({'alerts': unread[-50:], 'total_unread': len(unread)})


@proctoring_bp.route('/api/agent/alerts/read', methods=['POST'])
@paseto_required
def agent_mark_read():
    """Marque des alertes comme lues (stocké dans Redis Set)."""
    role = get_current_user_role()
    if role not in ['professor', 'admin', 'surveillant']:
        return jsonify({'error': 'Accès non autorisé'}), 403
    data = request.get_json(silent=True) or {}
    ids  = set(data.get('attempt_ids', []))
    _mark_read(ids)
    return jsonify({'success': True})


@proctoring_bp.route('/api/agent/active_exams', methods=['GET'])
def agent_active_exams():
    """L'agent récupère la liste des examens en cours."""
    if not _agent_auth():
        return jsonify({'error': 'Non autorisé'}), 403
    session = get_session()
    try:
        exams = session.query(OnlineExam).filter(
            OnlineExam.status == ExamStatus.ACTIVE
        ).all()
        result = [{'id': e.id, 'title': e.title} for e in exams]
        session.close()
        return jsonify({'exams': result})
    except Exception as e:
        session.close()
        return jsonify({'error': str(e)}), 500


@proctoring_bp.route('/api/agent/exam_proctoring/<int:exam_id>', methods=['GET'])
def agent_exam_proctoring(exam_id):
    """L'agent récupère les données de surveillance d'un examen (emails inclus)."""
    if not _agent_auth():
        return jsonify({'error': 'Non autorisé'}), 403
    session = get_session()
    try:
        exam = session.query(OnlineExam).filter_by(id=exam_id).first()
        if not exam:
            session.close()
            return jsonify({'error': 'Examen introuvable'}), 404

        # Email de l'enseignant propriétaire
        teacher = session.query(User).filter_by(id=exam.created_by_id).first()
        teacher_email = teacher.email if teacher else None

        # Emails des surveillants affectés
        proctors = session.query(ExamProctor).filter_by(exam_id=exam_id).all()
        proctor_emails = []
        for ep in proctors:
            u = session.query(User).filter_by(id=ep.proctor_id).first()
            if u and u.email:
                proctor_emails.append(u.email)

        # Tentatives actives
        attempts = session.query(ExamAttempt).filter_by(exam_id=exam_id).all()
        attempts_data = []
        for a in attempts:
            student = session.query(User).filter_by(id=a.student_id).first()
            attempts_data.append({
                'id':                     a.id,
                'student_name':           student.full_name if student else '?',
                'status':                 a.status.value if hasattr(a.status, 'value') else str(a.status),
                'risk_score':             a.risk_score or 0,
                'tab_switches':           a.tab_switches or 0,
                'warnings_count':         a.warnings_count or 0,
                'no_face_detected_count': 0,
                'multiple_faces_count':   0,
            })

        session.close()
        return jsonify({
            'exam_id':        exam_id,
            'title':          exam.title,
            'teacher_email':  teacher_email,
            'proctor_emails': proctor_emails,
            'attempts':       attempts_data,
        })
    except Exception as e:
        session.close()
        return jsonify({'error': str(e)}), 500


@proctoring_bp.route('/api/agent/status', methods=['GET'])
@paseto_required
def agent_status():
    """
    Statut de l'agent autonome de surveillance.
    Retourne si l'agent est actif, son dernier cycle, et les stats par examen.
    Accessible à tous les rôles authentifiés (prof, admin, surveillant).
    """
    import json as _json
    import os as _os
    from datetime import datetime, timezone, timedelta

    role = get_current_user_role()
    if role not in ['professor', 'admin', 'surveillant']:
        return jsonify({'error': 'Accès non autorisé'}), 403

    heartbeat_file = _os.path.join(_os.path.dirname(__file__), 'agent_heartbeat.json')
    exam_id        = request.args.get('exam_id', type=int)

    try:
        if not _os.path.exists(heartbeat_file):
            return jsonify({
                'alive':        False,
                'status':       'offline',
                'status_label': 'Agent hors ligne',
                'status_color': '#ef4444',
                'message':      "Le service cei-agent-proctor n'est pas démarré.",
            })

        with open(heartbeat_file, 'r') as f:
            hb = _json.load(f)

        # Vérifier si le heartbeat est récent (< 2× l'intervalle)
        last_check_str = hb.get('last_check', '')
        interval       = hb.get('interval_seconds', 30)
        alive          = False
        last_check_ago = None

        if last_check_str:
            last_check_dt  = datetime.fromisoformat(last_check_str)
            now_utc        = datetime.now(timezone.utc)
            delta          = (now_utc - last_check_dt).total_seconds()
            last_check_ago = int(delta)
            alive          = delta < (interval * 3)   # 3× l'intervalle = marge réseau

        if alive:
            status       = 'active'
            status_label = 'Agent actif — Surveillance IA en cours'
            status_color = '#10b981'
        else:
            status       = 'stale'
            status_label = 'Agent inactif (dernier signal trop ancien)'
            status_color = '#f59e0b'

        result = {
            'alive':               alive,
            'status':              status,
            'status_label':        status_label,
            'status_color':        status_color,
            'last_check':          last_check_str,
            'last_check_ago_sec':  last_check_ago,
            'interval_seconds':    interval,
            'risk_alert':          hb.get('risk_alert', 60),
            'risk_urgent':         hb.get('risk_urgent', 80),
            'exams_monitored':     hb.get('exams_monitored', 0),
            'total_alerts_session':hb.get('total_alerts_session', 0),
        }

        # Stats spécifiques à un examen si demandé
        if exam_id:
            exam_stats = hb.get('exam_stats', {}).get(str(exam_id), {})
            result['exam'] = {
                'exam_id':     exam_id,
                'students':    exam_stats.get('total', 0),
                'alerts_sent': exam_stats.get('alerts_sent', 0),
                'banned':      exam_stats.get('banned', 0),
            }

        return jsonify(result)

    except Exception as e:
        return jsonify({'error': str(e), 'alive': False}), 500
