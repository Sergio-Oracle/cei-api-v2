"""
Routes de proctoring LiveKit
Surveillance en temps réel des examens en ligne
"""
from flask import Blueprint, jsonify, request, current_app
from auth_paseto import paseto_required, get_current_user_id, get_current_user_role
from extensions import limiter
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
from sqlalchemy.exc import IntegrityError
from models import (
    get_session, ExamAttempt, OnlineExam, ExamActivityLog, User,
    AttemptStatus, UserRole, ExamStatus, CameraLog,
    ExamProctor, ProctorAssignment, Subject, EC, UE, StudentUEEnrollment,
    ECAssignment, ProctorGroup, ProctorGroupEC, ProctorGroupMember,
    ExamAccessCode, ProctorGroupSupervisor,
)
from cache import cache_get, cache_set, cache_set_nx, cache_pop, cache_delete

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
# API : DEUXIÈME CAMÉRA VIA SMARTPHONE (angle latéral, opt-in par examen)
# ============================================================================
# Le téléphone rejoint la MÊME salle LiveKit que la caméra principale
# (exam-{exam_id}) sous une identité distincte (student-{user_id}-phone,
# publication seule — canSubscribe=False, le téléphone n'a besoin de rien
# recevoir). Couplage en deux temps car le téléphone n'est jamais connecté à
# CEI (pas de session PASETO) : l'étudiant génère un code à usage unique
# depuis sa page d'examen, affiché en QR + texte, que le téléphone échange
# contre un token via un endpoint PUBLIC dédié (pas de version alternative
# authentifiée possible côté téléphone).

import random as _random

def _phonecam_pair_key(code: str) -> str:
    return f'cei:phonecam:pair:{code}'

def _phonecam_linked_key(attempt_id: int) -> str:
    return f'cei:phonecam:linked:{attempt_id}'


@proctoring_bp.route('/api/exam_attempts/<int:attempt_id>/phone_camera/pair', methods=['POST'])
@paseto_required
def phone_camera_pair(attempt_id):
    """Génère un code de couplage à usage unique (5 min) pour la caméra secondaire."""
    user_id = get_current_user_id()
    session = get_session()
    try:
        attempt = session.query(ExamAttempt).filter_by(id=attempt_id, student_id=user_id).first()
        if not attempt:
            return jsonify({'error': 'Tentative introuvable'}), 404
        if attempt.status != AttemptStatus.IN_PROGRESS:
            return jsonify({'error': 'Tentative non active'}), 400
        exam = session.query(OnlineExam).filter_by(id=attempt.exam_id).first()
        if not exam or not exam.allow_secondary_camera:
            return jsonify({'error': "La caméra secondaire n'est pas activée pour cet examen"}), 403

        code = f'{_random.randint(0, 999999):06d}'
        cache_set(_phonecam_pair_key(code), {
            'attempt_id': attempt.id, 'user_id': user_id, 'exam_id': exam.id,
        }, ttl=300)

        app_url = os.getenv('APP_URL', 'https://dev-cei.ddns.net').rstrip('/')
        return jsonify({
            'code': code,
            'url': f'{app_url}/phone-camera?code={code}',
            'expires_in': 300,
        })
    finally:
        session.close()


# Le téléphone met plusieurs secondes à réellement publier après l'échange
# du token (chargement du script LiveKit UMD depuis un CDN, connexion à la
# room, négociation WebRTC — d'autant plus long sur un réseau mobile) alors
# que le flag Redis "linked" est posé, lui, dès l'échange du token (avant
# toute publication réelle). Sans cette marge, un sondage du frontend qui
# tombe dans cette fenêtre voit really_linked=False et SUPPRIME le flag —
# alors que le téléphone finit de se connecter une seconde plus tard,
# laissant l'écran principal bloqué indéfiniment sur "en attente de
# connexion" malgré un téléphone réellement connecté (constaté le 25/08).
_PHONECAM_LINK_GRACE_SECONDS = 20

@proctoring_bp.route('/api/exam_attempts/<int:attempt_id>/phone_camera/status', methods=['GET'])
@paseto_required
def phone_camera_status(attempt_id):
    """Le téléphone est-il RÉELLEMENT connecté maintenant — vérifié en
    direct auprès de LiveKit (ListParticipants), pas seulement via le flag
    Redis posé au moment du couplage (retour utilisateur du 24/08 : le
    badge restait "connecté" alors que le téléphone n'était plus du tout
    là — page fermée/réseau coupé côté téléphone sans que quoi que ce soit
    n'en informe CEI autrement que par ce flag, jamais réévalué depuis)."""
    user_id = get_current_user_id()
    session = get_session()
    try:
        attempt = session.query(ExamAttempt).filter_by(id=attempt_id, student_id=user_id).first()
        if not attempt:
            return jsonify({'error': 'Tentative introuvable'}), 404
        exam_id = attempt.exam_id
    finally:
        session.close()

    # Filtre rapide : si le couplage initial n'a même jamais réussi, inutile
    # d'interroger LiveKit.
    linked_data = cache_get(_phonecam_linked_key(attempt_id))
    if not linked_data:
        return jsonify({'linked': False})
    set_at = linked_data.get('set_at', 0) if isinstance(linked_data, dict) else 0

    config = get_livekit_config()
    if not all([config['url'], config['api_key'], config['api_secret']]):
        return jsonify({'linked': False})

    room_name = f'exam-{exam_id}'
    phone_identity = f'student-{user_id}-phone'
    now = int(time.time())
    admin_token = pyjwt.encode(
        {'exp': now + 60, 'iss': config['api_key'], 'nbf': now,
         'sub': 'admin', 'video': {'roomAdmin': True, 'room': room_name}},
        config['api_secret'], algorithm='HS256',
    )
    really_linked = False
    try:
        req = urlreq.Request(
            f"{config['api_url']}/twirp/livekit.RoomService/ListParticipants",
            data=json.dumps({'room': room_name}).encode(),
            headers={'Authorization': f'Bearer {admin_token}', 'Content-Type': 'application/json'},
        )
        with urlreq.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
            identities = [p.get('identity') for p in data.get('participants', [])]
            really_linked = phone_identity in identities
    except Exception:
        # LiveKit injoignable — on préfère un faux "non connecté" transitoire
        # (l'étudiant peut réessayer) à un faux "connecté" qui induirait le
        # surveillant en erreur sur une preuve de surveillance absente.
        really_linked = False

    if not really_linked:
        # Ne supprime le flag que passé le délai de grâce — sinon on
        # préempte une connexion en cours (voir commentaire au-dessus).
        if time.time() - set_at > _PHONECAM_LINK_GRACE_SECONDS:
            cache_delete(_phonecam_linked_key(attempt_id))

    return jsonify({'linked': really_linked})


@proctoring_bp.route('/api/phone_camera/token', methods=['POST'])
@limiter.limit("20 per minute")
def phone_camera_token():
    """PUBLIC (pas de session CEI côté téléphone) — échange un code de
    couplage à usage unique contre un token LiveKit publish-only."""
    data = request.get_json(silent=True) or {}
    code = (data.get('code') or '').strip()
    if not code:
        return jsonify({'error': 'Code requis'}), 400

    pairing = cache_pop(_phonecam_pair_key(code))
    if not pairing:
        return jsonify({'error': 'Code invalide ou expiré — régénérez un nouveau code depuis la page d\'examen'}), 404

    config = get_livekit_config()
    if not all([config['url'], config['api_key'], config['api_secret']]):
        return jsonify({'error': 'LiveKit non configuré sur le serveur'}), 503

    session = get_session()
    try:
        attempt = session.query(ExamAttempt).filter_by(id=pairing['attempt_id']).first()
        if not attempt or attempt.status != AttemptStatus.IN_PROGRESS:
            return jsonify({'error': "La tentative n'est plus active"}), 400
        exam = session.query(OnlineExam).filter_by(id=pairing['exam_id']).first()
        if not exam:
            return jsonify({'error': 'Examen introuvable'}), 404

        room_name = f'exam-{exam.id}'
        identity  = f'student-{pairing["user_id"]}-phone'
        # TTL généreux mais borné à la fin réelle de l'examen (+ marge),
        # même calcul que get_student_livekit_token — pas de token qui
        # survivrait indéfiniment à la fin de la tentative.
        remaining_min = max(1, exam.duration_minutes + (attempt.extra_minutes or 0))
        ttl = remaining_min * 60 + 600
        token = generate_livekit_token(
            config['api_key'], config['api_secret'],
            identity, room_name,
            can_publish=True, can_subscribe=False,
            ttl=ttl,
        )
        cache_set(_phonecam_linked_key(attempt.id), {'set_at': time.time()}, ttl=ttl)

        return jsonify({
            'token': token,
            'ws_url': config['url'],
            'room': room_name,
            'exam_title': exam.title,
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

        if role == 'professor' and exam.created_by_id != user_id:
            return jsonify({'error': 'Vous ne pouvez surveiller que vos propres examens'}), 403

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
            # Retour DFIP — luminosité insuffisante/excessive rend la détection
            # peu fiable : signal informatif seulement, ne doit jamais faire
            # monter le score de risque comme une vraie absence.
            'no_face_low_light': 0,
            'multiple_faces': 20,
            'face_covered': 15,
            'camera_blocked': 25,
            'audio_suspicious': 10,
            'session_end': 0,
            # Scan environnement 360° (Phase 1)
            'env_scan_completed': 0,
            'env_scan_person_detected': 30,
            'env_scan_unavailable': 0,
            # Signaux MediaPipe temps réel (Phases 3-4-5)
            'gaze_away': 5,
            'head_turned': 5,
            'talking_detected': 8,
            # Modèle léger (efficientdet_lite0), sujet à confusion main/objet
            # (ex. main posée près de la tête classée "téléphone") — ramené de
            # 25 (le plus haut du barème) à 12 : reste un signal significatif,
            # sans qu'un faux positif isolé ne pèse plus que tout le reste.
            'suspect_object_detected': 12,
            # Vivacité (Phase 7) — signal informatif, ne bloque jamais l'examen
            'liveness_check_failed': 0,
            # Audio (Phase 6) et multi-écran (Phase 8)
            'sustained_audio_detected': 10,
            'multi_screen_detected': 20,
            # Retour DFIP #10 — fermeture d'onglet/navigateur pendant l'examen,
            # envoyée via un canal résistant à la fermeture brutale de page
            # (fetch keepalive) ; sévérité alignée sur tab_switch/window_blur.
            'tab_closed': 10,
            # Ces trois événements retombaient auparavant sur le poids par
            # défaut (5), trop faible : ce sont les principaux moyens de
            # quitter le contexte surveillé de l'examen (consulter une autre
            # fenêtre/application) — alignés explicitement ici, sortie du
            # plein écran pondérée un peu plus haut car plus délibérée
            # qu'une simple perte de focus (notification OS, Alt+Tab reflexe).
            'tab_switch': 10,
            'window_blur': 10,
            'fullscreen_exit': 15,
            # Chuchotement (seuil audio bas, distinct de sustained_audio_detected)
            'whisper_detected': 6,
            # Presse-papiers — journalisés qu'ils soient bloqués ou autorisés
            # (voir exam/[id]/page.tsx clipEvent). Coller pèse plus lourd que
            # copier/couper : injecter du contenu externe dans une réponse est
            # un signal de triche plus direct que simplement copier son propre
            # texte pour le reformater.
            'copy_attempt': 8,
            'cut_attempt': 8,
            'paste_attempt': 15,
            # Signal faible et bruyant (un curseur sort de la fenêtre pour
            # mille raisons anodines) — journalisé comme preuve, jamais noté.
            'mouse_left_window': 0,
            # Motifs comportementaux corrélés (voir checkBehaviorPatterns côté
            # frontend) — plusieurs signaux indépendants réunis dans la même
            # fenêtre glissante, donc nettement plus fiables qu'un signal
            # isolé : pondération volontairement plus haute que n'importe quel
            # événement brut individuel ci-dessus.
            'pattern_gaze_talk_mouth': 30,
            'pattern_multi_face_audio': 30,
            'pattern_object_gaze_away': 25,
            'pattern_mouth_covered_audio': 25,
            'pattern_head_turned_talking': 20,
            'pattern_whisper_gaze': 20,
            # Correctif sécurité (27/08) — remplace l'ancien comportement qui
            # recapturait SILENCIEUSEMENT une nouvelle référence faciale après
            # 5 échecs de reconnaissance consécutifs (~25s), sans validation
            # humaine : une substitution de personne en cours d'examen aurait
            # pu être acceptée automatiquement. Poids volontairement élevé —
            # un seul signal ici est déjà une confirmation forte (5
            # vérifications consécutives), cohérent avec les patterns
            # composites ci-dessus (25-30) mais au-dessus car non un simple
            # pattern probable, une preuve de mismatch soutenu. Voir
            # identity_manual_verify ci-dessous pour la levée du signalement.
            'identity_mismatch_sustained': 38,
        }
        risk_increment = proctoring_risk_map.get(event_type, 5)

        # Garde-fou de plausibilité (hygiène de données, pas anti-triche) —
        # un doublon du même event_type pour la même tentative reçu à moins
        # de quelques secondes d'intervalle n'incrémente pas de nouveau le
        # score (protège contre un flood/bug côté client), mais reste
        # TOUJOURS journalisé ci-dessus pour l'audit. cache_should_score
        # (pas cache_set_nx) est fail-open : une panne Redis ne doit jamais
        # désactiver silencieusement tout le scoring de risque de la
        # plateforme — voir cache.py pour le détail de cette distinction.
        from cache import cache_should_score
        _dedup_window = {
            'gaze_away': 3, 'head_turned': 5, 'talking_detected': 3,
            'no_face_detected': 3, 'multiple_faces': 3, 'whisper_detected': 3,
        }.get(event_type, 2)
        if event_type not in ('session_end',) and not cache_should_score(
            f'cei:proctev:dedup:{attempt_id}:{event_type}', _dedup_window
        ):
            risk_increment = 0

        if event_type != 'session_end':
            # Incrémentation atomique avec plafonnement à 100 via LEAST (évite race condition)
            from sqlalchemy import func as _sa_func
            session.execute(
                _sa_update(ExamAttempt)
                .where(ExamAttempt.id == attempt_id)
                .values(risk_score=_sa_func.least(ExamAttempt.risk_score + risk_increment, 100))
            )
            session.refresh(attempt)

        # Déterminer si l'on doit notifier, récupérer les données utiles.
        # IMPORTANT : capturer toutes les valeurs scalaires dont on a besoin
        # AVANT le commit+close ci-dessous — session.commit() expire par
        # défaut tous les attributs SQLAlchemy déjà chargés (expire_on_commit),
        # et re-toucher attempt.xxx après session.close() lève
        # DetachedInstanceError (l'objet ne peut plus recharger depuis une
        # session fermée). Bug préexistant qui faisait échouer CET ENDPOINT EN
        # ENTIER avec un 500 sur absolument tout event_type — trouvé en testant
        # les nouveaux types d'événements du moteur de corrélation, mais ne
        # leur est pas spécifique.
        notify_high_risk = (event_type != 'session_end' and attempt.risk_score >= 75)
        # Signal d'identité soutenue — notifie TOUJOURS immédiatement le
        # surveillant, indépendamment du score cumulé (qui peut ne pas
        # encore atteindre 75 malgré un mismatch confirmé) : voir A dans le
        # plan Phase X, ce signal exige une action humaine sans délai.
        notify_identity = (event_type == 'identity_mismatch_sustained')
        risk_score_val = attempt.risk_score
        banned_val = (attempt.status == AttemptStatus.BANNED)
        exam_id_val = attempt.exam_id
        if notify_high_risk or notify_identity:
            try:
                _student = attempt.student if hasattr(attempt, 'student') and attempt.student else None
                _sname = _student.full_name if _student else f'Étudiant #{attempt.student_id}'
            except Exception:
                _sname = f'Étudiant #{attempt.student_id}'

        session.commit()
        session.close()

        # Alerte fraude si nécessaire (hors transaction)
        if notify_high_risk:
            try:
                from notif_bus import notify_exam
                notify_exam(
                    exam_id_val,
                    'high_risk',
                    'Alerte fraude détectée',
                    f'{_sname} — score risque : {risk_score_val}/100',
                    priority='urgent',
                    tags=['rotating_light'],
                )
            except Exception as _nb_err:
                import logging as _lg
                _lg.getLogger('cei.proctoring').warning('notif_bus risk error: %s', _nb_err)

        if notify_identity:
            try:
                from notif_bus import notify_exam
                notify_exam(
                    exam_id_val,
                    'identity_mismatch',
                    'Identité non confirmée — action requise',
                    f'{_sname} — vérification manuelle requise (5 échecs de reconnaissance faciale consécutifs)',
                    priority='urgent',
                    tags=['warning'],
                )
            except Exception as _nb_err:
                import logging as _lg
                _lg.getLogger('cei.proctoring').warning('notif_bus identity error: %s', _nb_err)

        return jsonify({
            'success': True,
            'risk_score': risk_score_val,
            'banned': banned_val
        })
    finally:
        session.close()


# ============================================================================
# API : SNAPSHOT CAMÉRA (capture périodique + violations)
# ============================================================================

@proctoring_bp.route('/api/exam_attempts/<int:attempt_id>/camera_snapshot', methods=['POST'])
@paseto_required
@limiter.exempt
def save_camera_snapshot(attempt_id):
    """Sauvegarder un snapshot caméra (base64 JPEG) depuis la page étudiant."""
    user_id = get_current_user_id()
    # Vérifier la tentative et récupérer exam_id, puis fermer la session
    # AVANT l'upload S3 (I/O réseau qui n'a rien à faire avec une connexion
    # DB tenue ouverte pendant potentiellement plusieurs centaines de ms).
    session = get_session()
    try:
        attempt = session.query(ExamAttempt).filter_by(id=attempt_id, student_id=user_id).first()
        if not attempt or attempt.status != AttemptStatus.IN_PROGRESS:
            return jsonify({'error': 'Tentative non active'}), 400
        exam_id = attempt.exam_id
    finally:
        session.close()

    data      = request.get_json() or {}
    image_b64 = data.get('image_data', '')

    # Upload vers MinIO — stocker la clé S3 dans image_filename
    # image_data reste NULL pour les nouvelles entrées (rétrocompat : anciens = base64)
    from s3_client import upload_snapshot
    try:
        s3_key = upload_snapshot(exam_id, attempt_id, image_b64) if image_b64 else None
    except Exception:
        s3_key = None

    # Insertion via une courte session dédiée, ouverte seulement pour l'écriture
    s2 = get_session()
    try:
        snap = CameraLog(
            attempt_id=attempt_id,
            event_type=data.get('event_type', 'periodic'),
            image_filename=s3_key,          # clé S3 (ex: snapshots/3/12/20260704T...)
            image_data=None,                # NULL pour les nouvelles entrées
            face_detected=data.get('face_detected'),
            faces_count=data.get('faces_count'),
            confidence_score=data.get('confidence_score'),
            frame_analysis=data.get('frame_analysis'),
        )
        s2.add(snap)
        s2.commit()
        if s3_key and s3_key.startswith('local:'):
            stored = 'local_fallback'
        elif s3_key:
            stored = 's3'
        else:
            stored = 'none'
        return jsonify({'success': True, 'snapshot_id': snap.id, 'stored': stored})
    finally:
        s2.close()


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

        if role == 'professor' and attempt.exam.created_by_id != user_id:
            return jsonify({'error': 'Vous ne pouvez agir que sur vos propres examens'}), 403

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
        # N'incrémenter les avertissements que pour les types graves.
        # Correctif fiabilité (29/08, audit) : incrément Python (lire puis
        # écrire) plutôt qu'un UPDATE SQL atomique — deux avertissements
        # envoyés à quelques millisecondes d'écart (prof + surveillant, ou
        # double-clic) pouvaient en perdre un. Même pattern atomique déjà
        # utilisé ailleurs dans ce fichier pour risk_score.
        if warning_type not in ('message', 'private_call', 'end_call'):
            from sqlalchemy import update as _sa_update_warn
            session.execute(
                _sa_update_warn(ExamAttempt).where(ExamAttempt.id == attempt_id)
                .values(warnings_count=ExamAttempt.warnings_count + 1)
            )
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

        if role == 'professor' and attempt.exam.created_by_id != user_id:
            return jsonify({'error': 'Vous ne pouvez agir que sur vos propres examens'}), 403

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

        # Préparer la notification puis committer et notifier hors session
        try:
            student_obj = session.query(User).filter_by(id=attempt.student_id).first()
            student_name = student_obj.full_name if student_obj else f'Étudiant #{attempt.student_id}'
        except Exception:
            student_name = f'Étudiant #{attempt.student_id}'

        session.commit()
        # Correctif fiabilité (29/08, audit de montée en charge) : attempt.exam_id
        # est expiré par commit() — capturer avant close(), pas après. Avant ce
        # correctif, cette ligne levait un DetachedInstanceError avalé
        # silencieusement par le except ci-dessous : le bannissement
        # fonctionnait bien, mais la notification "urgent" au professeur
        # n'était JAMAIS envoyée.
        exam_id_val = attempt.exam_id
        session.close()

        try:
            from notif_bus import notify_exam
            notify_exam(
                exam_id_val,
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
# API : VALIDATION MANUELLE D'IDENTITÉ (prof/surveillant, suite à
# identity_mismatch_sustained) — correctif sécurité 27/08, voir Phase X du
# plan : remplace l'ancienne recapture automatique silencieuse. Le frontend
# gèle refDescRef dès l'événement identity_mismatch_sustained et attend ce
# verdict avant de reprendre toute reconnaissance faciale (voir
# identityFrozenRef, exam/[id]/page.tsx).
# ============================================================================

@proctoring_bp.route('/api/exam_attempts/<int:attempt_id>/identity_manual_verify', methods=['POST'])
@paseto_required
def identity_manual_verify(attempt_id):
    """Prof/surveillant confirme ou infirme l'identité après un signalement
    identity_mismatch_sustained. 'confirmed' débloque la reconnaissance
    faciale côté client (nouvelle référence recapturée EXPLICITEMENT, plus
    jamais silencieusement) ; 'rejected' exclut directement la tentative."""
    user_id = get_current_user_id()
    role = get_current_user_role()

    if role not in ['professor', 'admin', 'surveillant']:
        return jsonify({'error': 'Accès réservé aux enseignants et surveillants'}), 403

    session = get_session()
    try:
        attempt = session.query(ExamAttempt).filter_by(id=attempt_id).first()
        if not attempt:
            return jsonify({'error': 'Tentative introuvable'}), 404

        if role == 'professor' and attempt.exam.created_by_id != user_id:
            return jsonify({'error': 'Vous ne pouvez agir que sur vos propres examens'}), 403

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
        verdict = data.get('verdict')
        if verdict not in ('confirmed', 'rejected'):
            return jsonify({'error': "verdict requis : 'confirmed' ou 'rejected'"}), 400

        proctor = session.query(User).filter_by(id=user_id).first()
        proctor_name = proctor.full_name if proctor else f'Surveillant #{user_id}'

        review_log = ExamActivityLog(
            attempt_id=attempt_id,
            event_type='identity_manual_review',
            event_data=json.dumps({
                'verdict': verdict, 'by_id': user_id, 'by_role': role,
                'by_name': proctor_name, 'timestamp': datetime.utcnow().isoformat()
            })
        )
        session.add(review_log)

        if verdict == 'rejected':
            attempt.status = AttemptStatus.BANNED
            attempt.banned_at = datetime.utcnow()
            attempt.ban_reason = f'Identité non confirmée par {proctor_name} après signalement soutenu'
        else:
            # Message routé par le polling existant (pending_messages) — le
            # frontend traite 'identity_cleared' comme un débloquage
            # explicite du gel de reconnaissance (jamais silencieux).
            clear_log = ExamActivityLog(
                attempt_id=attempt_id,
                event_type='teacher_identity_cleared',
                event_data=json.dumps({
                    'message': f'Identité confirmée par {proctor_name}',
                    'timestamp': datetime.utcnow().isoformat()
                })
            )
            session.add(clear_log)

        session.commit()
        return jsonify({'success': True, 'banned': verdict == 'rejected'})
    finally:
        session.close()


# ============================================================================
# API : LISTE DES ÉTUDIANTS ACTIFS (pour dashboard prof)
# ============================================================================

@proctoring_bp.route('/api/online_exams/<int:exam_id>/active_proctoring', methods=['GET'])
@paseto_required
@limiter.exempt
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

        # Lecture pure — l'affectation elle-même se fait au démarrage de la
        # tentative (routes/exams.py::start_exam_attempt →
        # services.proctor_service.assign_single_attempt), pas ici. Avant ce
        # correctif, ce GET recalculait et écrivait les affectations à chaque
        # appel — répété à chaque rafraîchissement de chaque surveillant
        # connecté, un coût inutile et un anti-pattern (écriture sur un GET).
        all_exam_proctors = session.query(ExamProctor).filter_by(exam_id=exam_id).all()

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
                'last_seen_at': a.last_seen_at.isoformat() if a.last_seen_at else None,
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
            })

        # Filtrer la vue du surveillant (ne montrer que son groupe)
        if role == 'surveillant':
            result = [r for r in result if r['proctor_id'] == user_id]

        # Pour l'enseignant : infos détaillées par groupe
        proctors_info = []
        if role in ['professor', 'admin']:
            # Groupes Surveillants rattachés à l'EC du sujet — un surveillant peut
            # venir de plusieurs groupes couvrant le même EC, d'où une liste.
            group_names_by_proctor = {}
            subject = session.query(Subject).filter_by(id=exam.subject_id).first()
            if subject and subject.ec_id:
                group_ids = [ge.group_id for ge in session.query(ProctorGroupEC).filter_by(ec_id=subject.ec_id).all()]
                if group_ids:
                    group_name_by_id = {
                        g.id: g.name for g in session.query(ProctorGroup).filter(ProctorGroup.id.in_(group_ids)).all()
                    }
                    for m in session.query(ProctorGroupMember).filter(ProctorGroupMember.group_id.in_(group_ids)).all():
                        group_names_by_proctor.setdefault(m.proctor_id, []).append(group_name_by_id.get(m.group_id, '?'))

            # Recalculer les counts depuis result (qui a les nouvelles assignations)
            for ep in all_exam_proctors:
                group_attempts = [r for r in result if r['proctor_id'] == ep.proctor_id]
                proctors_info.append({
                    'proctor_id': ep.proctor_id,
                    'proctor_name': ep.proctor.full_name if ep.proctor else '?',
                    'proctor_email': ep.proctor.email if ep.proctor else '',
                    'proctor_identity': f'proctor-{ep.proctor_id}',
                    'student_count': len(group_attempts),
                    'group_names': group_names_by_proctor.get(ep.proctor_id, []),
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
# API : MESSAGES PROF EN ATTENTE (polling côté étudiant)
# ============================================================================

@proctoring_bp.route('/api/exam_attempts/<int:attempt_id>/pending_messages', methods=['GET'])
@paseto_required
@limiter.exempt
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
            ExamActivityLog.event_type.in_(['teacher_warning', 'teacher_message', 'teacher_ban', 'teacher_private_call', 'teacher_end_call', 'teacher_identity_cleared'])
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
@limiter.exempt
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
            ExamActivityLog.event_type.in_(['student_message', 'student_call_request'])
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
                    'log_id': log.id,
                    'type': 'call_request' if log.event_type == 'student_call_request' else 'message',
                })
            except Exception:
                pass
        return jsonify({'success': True, 'messages': messages})
    finally:
        session.close()


# ============================================================================
# API : CODE DE REPRISE APRÈS DÉCONNEXION (appel + code à usage unique)
# ============================================================================

def _get_covering_supervisor_ids(exam, session):
    """Superviseur(s) responsable(s) du/des groupe(s) de surveillants rattaché(s)
    à l'EC de cet examen (même logique que get_vigilance_level). Utilisé comme
    palier intermédiaire — le superviseur supervise déjà les surveillants,
    c'est donc lui le relais naturel en leur absence, pas le professeur."""
    if not exam.subject or not exam.subject.ec_id:
        return set()
    groups = (
        session.query(ProctorGroup)
        .join(ProctorGroupEC, ProctorGroupEC.group_id == ProctorGroup.id)
        .filter(ProctorGroupEC.ec_id == exam.subject.ec_id)
        .all()
    )
    return {s.supervisor_id for g in groups for s in g.supervisors}


@proctoring_bp.route('/api/exam_attempts/<int:attempt_id>/call_request', methods=['POST'])
@paseto_required
def request_proctor_call(attempt_id):
    """Étudiant (hors page d'examen, depuis son dashboard) demande un appel
    vocal/vidéo à son surveillant ou à défaut son professeur, en vue d'obtenir
    un code de reprise après déconnexion. Notifie individuellement chaque
    destinataire via le bus de notifications déjà utilisé pour le badge
    temps réel (long-polling /api/notifications/poll)."""
    user_id = get_current_user_id()
    session = get_session()
    try:
        attempt = session.query(ExamAttempt).filter_by(id=attempt_id, student_id=user_id).first()
        if not attempt:
            return jsonify({'error': 'Tentative introuvable'}), 404
        if attempt.status != AttemptStatus.IN_PROGRESS:
            return jsonify({'error': 'Cet examen n\'est plus en cours'}), 400

        exam = attempt.exam
        student = attempt.student
        student_name = student.full_name if student else f'Étudiant #{user_id}'

        log = ExamActivityLog(
            attempt_id=attempt_id,
            event_type='student_call_request',
            event_data=json.dumps({
                'message': f'{student_name} demande un appel — reprise après déconnexion',
                'student_name': student_name,
                'timestamp': datetime.utcnow().isoformat(),
            })
        )
        session.add(log)

        # Destinataires — déterminer AVANT le commit pour pouvoir fermer la
        # session ensuite et effectuer les notifications hors transaction.
        assignments = session.query(ProctorAssignment).filter_by(
            exam_id=exam.id
        ).filter(
            (ProctorAssignment.attempt_id == attempt_id) |
            (ProctorAssignment.student_id == user_id)
        ).all()
        recipient_ids = {pa.proctor_id for pa in assignments if pa.proctor_id}
        if not recipient_ids:
            recipient_ids = _get_covering_supervisor_ids(exam, session)
        if not recipient_ids and exam.created_by_id:
            recipient_ids.add(exam.created_by_id)

        session.commit()
        session.close()

        try:
            from notif_bus import notify_user
            for rid in recipient_ids:
                try:
                    notify_user(
                        rid, 'call_request',
                        'Appel entrant — reprise après déconnexion',
                        f'{student_name} demande un appel pour reprendre « {exam.title} »',
                        priority='urgent', tags=['phone'],
                        extra={'exam_id': exam.id, 'attempt_id': attempt_id},
                    )
                except Exception:
                    pass
        except Exception:
            pass

        return jsonify({'success': True, 'notified': len(recipient_ids)})
    except Exception as e:
        try: session.rollback()
        except Exception: pass
        return jsonify({'error': str(e)}), 500
    finally:
        session.close()


@proctoring_bp.route('/api/exam_attempts/<int:attempt_id>/access_code', methods=['POST'])
@paseto_required
def generate_access_code(attempt_id):
    """Génère un code de reprise à usage unique pour cette tentative, après
    vérification d'identité par appel. Un seul rôle habilité par étudiant, par
    ordre de priorité — surveillant assigné, sinon superviseur du groupe
    couvrant cet EC (il supervise déjà les surveillants, relais naturel en
    leur absence), sinon le professeur en tout dernier repli — jamais deux à
    la fois, pour éviter qu'une génération invalide sans le savoir le code
    qu'un autre interlocuteur vient de communiquer. L'admin peut toujours
    agir (supervision globale)."""
    user_id = get_current_user_id()
    role = get_current_user_role()
    if role not in ('surveillant', 'superviseur', 'professor', 'admin'):
        return jsonify({'error': 'Accès réservé aux surveillants, superviseurs et enseignants'}), 403

    session = get_session()
    try:
        attempt = session.query(ExamAttempt).filter_by(id=attempt_id).first()
        if not attempt:
            return jsonify({'error': 'Tentative introuvable'}), 404
        exam = attempt.exam

        any_surveillant_assigned = session.query(ProctorAssignment).filter_by(
            exam_id=exam.id
        ).filter(
            (ProctorAssignment.attempt_id == attempt_id) |
            (ProctorAssignment.student_id == attempt.student_id)
        ).first() is not None
        covering_supervisor_ids = _get_covering_supervisor_ids(exam, session) if not any_surveillant_assigned else set()

        if role == 'surveillant':
            assigned = session.query(ProctorAssignment).filter_by(
                proctor_id=user_id, exam_id=exam.id
            ).filter(
                (ProctorAssignment.attempt_id == attempt_id) |
                (ProctorAssignment.student_id == attempt.student_id)
            ).first()
            if not assigned:
                return jsonify({'error': 'Cet étudiant ne vous est pas affecté'}), 403
        elif role == 'superviseur':
            if any_surveillant_assigned:
                return jsonify({'error': "Un surveillant est assigné à cet étudiant — seul lui peut générer le code."}), 403
            if user_id not in covering_supervisor_ids:
                return jsonify({'error': "Vous ne supervisez pas le groupe couvrant cet étudiant."}), 403
        elif role == 'professor':
            if exam.created_by_id != user_id:
                return jsonify({'error': 'Vous ne pouvez agir que sur vos propres examens'}), 403
            if any_surveillant_assigned:
                return jsonify({'error': "Un surveillant est assigné à cet étudiant — seul lui peut générer le code."}), 403
            if covering_supervisor_ids:
                return jsonify({'error': "Un superviseur est responsable de cet étudiant en l'absence de surveillant — seul lui peut générer le code."}), 403

        # Invalider les codes précédents encore valides pour cette tentative
        now = datetime.utcnow()
        session.query(ExamAccessCode).filter(
            ExamAccessCode.attempt_id == attempt_id,
            ExamAccessCode.used_at.is_(None),
        ).update({'expires_at': now}, synchronize_session=False)

        import random
        code = f'{random.randint(0, 999999):06d}'
        access_code = ExamAccessCode(
            attempt_id=attempt_id,
            code=code,
            generated_by_id=user_id,
            expires_at=now + timedelta(minutes=10),
        )
        session.add(access_code)
        session.commit()
        result = access_code.to_dict()
        return jsonify({'success': True, **result})
    except Exception as e:
        try: session.rollback()
        except Exception: pass
        return jsonify({'error': str(e)}), 500
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
        if role == 'professor' and exam.created_by_id != user_id:
            return jsonify({'error': 'Vous ne pouvez affecter des surveillants qu\'à vos propres examens'}), 403

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

        # Préparer message puis committer et notifier hors session
        try:
            exam_title = exam.title if exam and hasattr(exam, 'title') else ''
        except Exception:
            exam_title = ''

        session.commit()
        # Correctif fiabilité (29/08, audit de montée en charge) : ep.to_dict()
        # accède à des colonnes ET à des relations (proctor, assigned_by) qui
        # sont toutes expirées par commit() — les calculer ICI, avant
        # session.close(), pendant que la session est encore vivante pour
        # recharger ce qui est expiré/lazy. Appeler to_dict() APRÈS close()
        # (comme avant) levait un DetachedInstanceError à 100% des appels —
        # l'affectation était bien enregistrée mais l'endpoint renvoyait un 500.
        ep_dict = ep.to_dict()
        session.close()

        try:
            from notif_bus import notify_user
            try:
                notify_user(proctor_id, 'proctor_assigned', 'Nouvel examen à surveiller',
                             f'Vous surveillez « {exam_title} ».', priority='default', tags=['eyes'])
            except Exception:
                pass
        except Exception:
            pass

        return jsonify({'success': True, 'proctor': ep_dict}), 201
    finally:
        session.close()


@proctoring_bp.route('/api/online_exams/<int:exam_id>/proctors/<int:proctor_id>', methods=['DELETE'])
@paseto_required
def remove_exam_proctor(exam_id, proctor_id):
    """Retirer un surveillant d'un examen"""
    user_id = get_current_user_id()
    role = get_current_user_role()

    if role not in ['professor', 'admin']:
        return jsonify({'error': 'Accès réservé aux enseignants'}), 403

    session = get_session()
    try:
        if role == 'professor':
            exam = session.query(OnlineExam).filter_by(id=exam_id).first()
            if not exam or exam.created_by_id != user_id:
                return jsonify({'error': 'Vous ne pouvez retirer des surveillants que de vos propres examens'}), 403

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

# ── Niveaux de vigilance superviseur (A/B/C) ────────────────────────────────
# Ces clés sont volontairement SÉPARÉES de cei:proctor_live:* : ce dernier ne
# doit rester qu'un signal de connectivité brute (tab ouvert/joignable), car
# la redistribution automatique des étudiants en dépend — on ne veut jamais
# arracher les étudiants d'un surveillant juste parce qu'il a lâché sa souris
# 2 minutes. Les clés ci-dessous n'alimentent QUE l'affichage superviseur.
ENGAGED_TTL = 90   # Tier A : interaction souris/clavier + onglet visible au premier plan
VIEWED_TTL  = 300  # Tier B : a effectivement ouvert un flux étudiant récemment
FACE_TTL    = 300  # Tier C : dernière vérification de présence caméra positive

_VIGILANCE_ORDER = {'A': 0, 'B': 1, 'C': 2}


def get_vigilance_level(exam_id, proctor_id, session):
    """Niveau de vigilance (A/B/C) applicable à ce surveillant pour cet examen,
    déterminé par le(s) groupe(s) de surveillants auquel il appartient et qui
    sont rattachés à l'EC de cet examen. 'A' par défaut si aucun groupe ne
    correspond (surveillant ajouté manuellement, sans groupe)."""
    exam = session.query(OnlineExam).filter_by(id=exam_id).first()
    if not exam or not exam.subject or not exam.subject.ec_id:
        return 'A'
    groups = (
        session.query(ProctorGroup)
        .join(ProctorGroupEC, ProctorGroupEC.group_id == ProctorGroup.id)
        .join(ProctorGroupMember, ProctorGroupMember.group_id == ProctorGroup.id)
        .filter(ProctorGroupEC.ec_id == exam.subject.ec_id, ProctorGroupMember.proctor_id == proctor_id)
        .all()
    )
    if not groups:
        return 'A'
    return max((g.vigilance_level or 'A' for g in groups), key=lambda l: _VIGILANCE_ORDER.get(l, 0))


def get_proctor_engagement_status(exam_id, proctor_id, level):
    """Statut d'engagement pour un (examen, surveillant) donné, en fonction du
    niveau de vigilance exigé : 'engaged' si tous les signaux requis par le
    palier sont frais, sinon 'idle' (présent mais signal(s) manquant(s))."""
    engaged = bool(cache_get(f'cei:proctor_engaged:{exam_id}:{proctor_id}'))
    if level == 'A':
        return 'engaged' if engaged else 'idle'
    viewed = bool(cache_get(f'cei:proctor_viewed:{exam_id}:{proctor_id}'))
    if level == 'B':
        return 'engaged' if (engaged and viewed) else 'idle'
    face = bool(cache_get(f'cei:proctor_face:{exam_id}:{proctor_id}'))
    return 'engaged' if (engaged and viewed and face) else 'idle'


def get_proctor_signals(exam_id, proctor_id, level):
    """Détail des signaux bruts (interaction, consultation, caméra) pour un
    (examen, surveillant) — utilisé par le dashboard superviseur pour
    expliquer PRÉCISÉMENT pourquoi un surveillant reste "inactif" plutôt que
    de laisser un badge binaire opaque (ex: caméra jamais autorisée pour le
    palier C, alors que le surveillant est réellement en train de travailler)."""
    signals = {'engaged': bool(cache_get(f'cei:proctor_engaged:{exam_id}:{proctor_id}'))}
    if level in ('B', 'C'):
        signals['viewed'] = bool(cache_get(f'cei:proctor_viewed:{exam_id}:{proctor_id}'))
    if level == 'C':
        signals['face'] = bool(cache_get(f'cei:proctor_face:{exam_id}:{proctor_id}'))
    return signals


def get_proctor_status(proctor_id, session):
    """Statut global d'un surveillant, tous examens surveillés en ce moment
    confondus : ('engaged'|'idle'|'disconnected', exam_id le plus favorable ou
    None). Utilisé par le dashboard superviseur.

    Correctif montée en charge (29/08, audit) : utilisait auparavant
    `client.scan_iter('cei:proctor_live:*:{proctor_id}')` — un SCAN de TOUT
    le keyspace 'cei:proctor_live:*' à chaque appel (le motif met exam_id
    avant proctor_id, donc aucun lookup direct possible), appelé une fois
    par surveillant depuis professor_dashboard → amplification N (surveillants)
    x taille du keyspace, sur un endpoint sans limite de fréquence. Remplacé
    par un index inversé ('cei:proctor_exams:{proctor_id}', alimenté par
    proctor_heartbeat) donnant directement les examens candidats de CE
    surveillant — un GET direct par candidat (typiquement 1-3) au lieu d'un
    balayage complet. Une entrée obsolète dans l'index (examen quitté depuis)
    échoue simplement au GET et est nettoyée au passage (SREM), sans
    conséquence fonctionnelle."""
    from cache import _get_client
    client = _get_client()
    if client is None:
        return 'disconnected', None
    rank = {'disconnected': 0, 'idle': 1, 'engaged': 2}
    best_status, best_exam = 'disconnected', None
    try:
        candidate_ids = client.smembers(f'cei:proctor_exams:{proctor_id}') or set()
        for raw in candidate_ids:
            exam_id_str = raw.decode() if isinstance(raw, bytes) else raw
            try:
                exam_id = int(exam_id_str)
            except (TypeError, ValueError):
                continue
            if not client.exists(f'cei:proctor_live:{exam_id}:{proctor_id}'):
                # Entrée obsolète (heartbeat expiré depuis) — nettoyage opportuniste.
                try:
                    client.srem(f'cei:proctor_exams:{proctor_id}', raw)
                except Exception:
                    pass
                continue
            level = get_vigilance_level(exam_id, proctor_id, session)
            status = get_proctor_engagement_status(exam_id, proctor_id, level)
            if rank[status] > rank[best_status]:
                best_status, best_exam = status, exam_id
    except Exception:
        return 'disconnected', None
    return best_status, best_exam


def get_active_proctor_ids():
    """Retourne l'ensemble des id de surveillants actuellement actifs (heartbeat
    valide sur au moins un examen), tous examens confondus — utilisé par le
    dashboard admin ('nb surveillants actifs') et la vue superviseur. Clé :
    cei:proctor_live:{exam_id}:{proctor_id} (voir proctor_heartbeat ci-dessous)."""
    from cache import _get_client
    client = _get_client()
    if client is None:
        return set()
    try:
        ids = set()
        for key in client.scan_iter('cei:proctor_live:*'):
            k = key.decode() if isinstance(key, bytes) else key
            parts = k.split(':')
            if len(parts) == 4:
                ids.add(int(parts[3]))
        return ids
    except Exception:
        return set()


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
    # Défense en profondeur : le verrou NX dans _check_disconnected_proctors
    # empêche déjà les redistributions concurrentes pour le MÊME surveillant
    # déconnecté, mais deux surveillants DIFFÉRENTS détectés déconnectés en
    # même temps déclencheraient encore deux redistributions simultanées sur
    # le même examen. Isole ce commit pour dégrader proprement (la
    # redistribution est réessayée au prochain heartbeat) plutôt qu'un 500.
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        return False
    return True


def _check_disconnected_proctors(exam_id, session):
    """Détecte, parmi les surveillants affectés à l'examen, ceux dont le
    heartbeat a expiré alors qu'ils avaient déjà été vus en ligne, et
    déclenche automatiquement la redistribution de leurs étudiants."""
    proctors = session.query(ExamProctor).filter_by(exam_id=exam_id).all()
    notifications = []
    for p in proctors:
        seen_key     = f'cei:proctor_seen:{exam_id}:{p.proctor_id}'
        live_key     = f'cei:proctor_live:{exam_id}:{p.proctor_id}'
        cooldown_key = f'cei:proctor_redistributed:{exam_id}:{p.proctor_id}'
        # Verrou atomique (SET NX) : sous charge, plusieurs heartbeats de
        # surveillants différents arrivent quasi simultanément et détectent
        # tous le même collègue déconnecté. Un simple get-puis-set laissait
        # passer plusieurs redistributions concurrentes sur les mêmes lignes
        # ProctorAssignment, qui se percutaient sur la contrainte unique
        # (unique_exam_student_proctor) — vécu en test de charge : 500 sur
        # /proctor_heartbeat corrélés seconde pour seconde avec ces violations
        # dans les logs Postgres. Seul le premier appelant qui pose le verrou
        # agit ; les autres le trouvent déjà pris et passent leur tour.
        if cache_get(seen_key) and not cache_get(live_key) and cache_set_nx(cooldown_key, REDISTRIBUTE_COOLDOWN):
            redistributed = _redistribute_attempts_excluding(exam_id, session, {p.proctor_id})
            if redistributed:
                notifications.append({
                    'exam_id': exam_id,
                    'title': 'Surveillant déconnecté',
                    'message': f"{p.proctor.full_name if p.proctor else 'Un surveillant'} semble déconnecté — ses étudiants ont été réaffectés automatiquement.",
                    'priority': 'high',
                    'tags': ['warning'],
                })
    return notifications


@proctoring_bp.route('/api/online_exams/<int:exam_id>/proctor_heartbeat', methods=['POST'])
@paseto_required
@limiter.exempt
def proctor_heartbeat(exam_id):
    """Appelé périodiquement (ex. toutes les 30s) par la page de monitoring
    d'un surveillant tant qu'elle reste ouverte. Sert aussi de déclencheur
    pour détecter si D'AUTRES surveillants de ce même examen ont disparu.

    Le corps JSON peut porter les signaux de vigilance (Tier A/B/C) :
    {interacting, viewed_student, face_present} — chacun n'est appliqué que
    s'il est vrai, jamais retiré explicitement : à défaut de signal positif,
    la clé correspondante expire naturellement (TTL) et retombe à "idle"."""
    try:
        proctor_id = get_current_user_id()
        cache_set(f'cei:proctor_live:{exam_id}:{proctor_id}', '1', ttl=HEARTBEAT_TTL)
        cache_set(f'cei:proctor_seen:{exam_id}:{proctor_id}', '1', ttl=86400)
        # Index inversé (29/08, audit montée en charge) : get_proctor_status
        # faisait un SCAN de tout le keyspace 'cei:proctor_live:*' à CHAQUE
        # appel pour retrouver les examens d'un surveillant donné (le motif
        # exam_id:proctor_id met exam_id en premier, donc aucun lookup direct
        # possible par proctor_id seul) — répété pour chaque surveillant du
        # dashboard professeur (N surveillants x M examens actifs). Cet
        # ensemble Redis sert de candidats ; get_proctor_status revérifie
        # ensuite l'existence réelle de chaque clé individuelle (l'ensemble
        # peut contenir des entrées expirées, sans conséquence).
        try:
            from cache import _get_client
            _rc = _get_client()
            if _rc is not None:
                _rc.sadd(f'cei:proctor_exams:{proctor_id}', exam_id)
                _rc.expire(f'cei:proctor_exams:{proctor_id}', 86400)
        except Exception:
            pass

        data = request.get_json(silent=True) or {}
        if data.get('interacting'):
            cache_set(f'cei:proctor_engaged:{exam_id}:{proctor_id}', '1', ttl=ENGAGED_TTL)
        if data.get('viewed_student'):
            cache_set(f'cei:proctor_viewed:{exam_id}:{proctor_id}', '1', ttl=VIEWED_TTL)
        if data.get('face_present'):
            cache_set(f'cei:proctor_face:{exam_id}:{proctor_id}', '1', ttl=FACE_TTL)

        session = get_session()
        try:
            level = get_vigilance_level(exam_id, proctor_id, session)
            notifications = _check_disconnected_proctors(exam_id, session)
        finally:
            session.close()

        # Envoyer les notifications hors session
        try:
            from notif_bus import notify_exam
            for n in notifications:
                try:
                    notify_exam(n['exam_id'], 'proctor_disconnected', n['title'], n['message'], priority=n.get('priority', 'default'), tags=n.get('tags', []))
                except Exception:
                    pass
        except Exception:
            pass

        return jsonify({'success': True, 'vigilance_level': level})
    except Exception as e:
        try: session.rollback(); session.close()
        except Exception: pass
        return jsonify({'error': str(e)}), 500


@proctoring_bp.route('/api/online_exams/<int:exam_id>/distribute_proctors', methods=['POST'])
@paseto_required
def distribute_proctors(exam_id):
    """Répartir automatiquement les étudiants entre les surveillants.
    - Si des ExamAttempt existent → répartition par attempt (examen en cours)
    - Sinon → répartition par inscription UE (pré-affectation avant l'examen)
    """
    user_id = get_current_user_id()
    role = get_current_user_role()

    if role not in ['professor', 'admin']:
        return jsonify({'error': 'Accès réservé aux enseignants'}), 403

    session = get_session()
    try:
        exam = session.query(OnlineExam).filter_by(id=exam_id).first()
        if not exam:
            return jsonify({'error': 'Examen introuvable'}), 404
        if role == 'professor' and exam.created_by_id != user_id:
            return jsonify({'error': 'Vous ne pouvez répartir les surveillants que sur vos propres examens'}), 403

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
@limiter.exempt
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

    # Charger la tentative et vérifier les droits, puis fermer la session avant
    # d'effectuer les appels réseau avec LiveKit (réduit la tenue de connexions DB)
    session = get_session()
    try:
        attempt = session.query(ExamAttempt).filter_by(id=attempt_id).first()
        if not attempt:
            return jsonify({'error': 'Tentative introuvable'}), 404

        if role == 'professor' and attempt.exam.created_by_id != user_id:
            return jsonify({'error': 'Vous ne pouvez agir que sur vos propres examens'}), 403

        if role == 'surveillant':
            assigned = session.query(ProctorAssignment).filter_by(
                proctor_id=user_id, exam_id=attempt.exam_id
            ).filter(
                (ProctorAssignment.attempt_id == attempt_id) |
                (ProctorAssignment.student_id == attempt.student_id)
            ).first()
            if not assigned:
                return jsonify({'error': 'Cet étudiant ne vous est pas affecté'}), 403

        # Capturer l'état nécessaire puis fermer
        attempt_exam_id = attempt.exam_id
        attempt_student_id = attempt.student_id
        attempt_status = attempt.status
        current_egress = attempt.current_egress_id
    finally:
        session.close()

    # Token Egress
    now = int(time.time())
    egress_payload = {
        'exp': now + 3600, 'iss': config['api_key'], 'nbf': now,
        'sub': f'recorder-{attempt_id}',
        'video': {'room': f'exam-{attempt_exam_id}', 'roomRecord': True}
    }
    egress_token = pyjwt.encode(egress_payload, config['api_secret'], algorithm='HS256')

    headers = {
        'Authorization': f'Bearer {egress_token}',
        'Content-Type': 'application/json'
    }

    if action == 'start':
        if attempt_status != AttemptStatus.IN_PROGRESS:
            return jsonify({'error': 'Enregistrement impossible : cet étudiant n\'est pas en cours d\'examen.'}), 400

        room_name = f'exam-{attempt_exam_id}'
        student_identity = f'student-{attempt_student_id}'

        # Vérifier présence ET caméra active via ListParticipants
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
                if student_identity not in identities:
                    return jsonify({'error': "L'étudiant n'est pas connecté à la salle d'examen en ce moment."}), 400

                student_part = next((p for p in participants if p.get('identity') == student_identity), None)
                if student_part:
                    tracks = student_part.get('tracks', [])
                    for t in tracks:
                        if (t.get('type') in ('VIDEO', 1) and t.get('source') not in ('SCREEN_SHARE', 3) and not t.get('muted', False)):
                            video_track_id = t.get('sid')
                            break
                    for t in tracks:
                        if t.get('type') in ('AUDIO', 0) and not t.get('muted', False):
                            audio_track_id = t.get('sid')
                            break
                    if not video_track_id:
                        return jsonify({'error': "La caméra de l'étudiant n'est pas active."}), 400
        except urllib.error.HTTPError as e:
            return jsonify({'error': "Impossible de vérifier l'état de la connexion de l'étudiant."}), 503
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            return jsonify({'error': "Le service LiveKit est momentanément indisponible."}), 503

        # StartTrackCompositeEgress
        s3_cfg = {
            'access_key': os.environ.get('S3_KEY_ID', ''),
            'secret':     os.environ.get('S3_KEY_SECRET', ''),
            'region':     os.environ.get('S3_REGION', 'us-east-1'),
            'endpoint':   os.environ.get('S3_PUBLIC_ENDPOINT', os.environ.get('S3_ENDPOINT', '')),
            'bucket':     os.environ.get('S3_BUCKET', 'livekit-recordings'),
            'force_path_style': True
        }
        filepath = (f'recordings/exam-{attempt_exam_id}/'
                    f'student-{attempt_student_id}-attempt-{attempt_id}.mp4')

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
                # Persister l'egress_id dans une courte session
                s2 = get_session()
                try:
                    a = s2.query(ExamAttempt).filter_by(id=attempt_id).first()
                    if a:
                        a.current_egress_id = egress_id
                        s2.commit()
                finally:
                    s2.close()
                return jsonify({'success': True, 'egress_id': egress_id, 'filepath': filepath})
        except urllib.error.HTTPError as e:
            err_body = e.read().decode()
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
        # Utiliser l'egress_id fourni par le client, sinon celui lu plus tôt
        egress_id = data.get('egress_id') or current_egress
        if not egress_id:
            return jsonify({'error': 'Aucun enregistrement actif pour cette tentative'}), 400
        body = json.dumps({'egress_id': egress_id}).encode()
        req = urlreq.Request(
            f'{lk_http}/twirp/livekit.Egress/StopEgress',
            data=body, headers=headers
        )
        try:
            with urlreq.urlopen(req, timeout=5) as resp:
                # Effacer l'egress_id persisté dans une courte session
                s2 = get_session()
                try:
                    a = s2.query(ExamAttempt).filter_by(id=attempt_id).first()
                    if a:
                        a.current_egress_id = None
                        s2.commit()
                finally:
                    s2.close()
                return jsonify({'success': True})
        except urllib.error.HTTPError as e:
            return jsonify({'error': "L'arrêt de l'enregistrement a échoué. Vérifiez que l'enregistrement est bien actif."}), 500
        except (urllib.error.URLError, TimeoutError, OSError):
            return jsonify({'error': "Le service d'enregistrement est momentanément indisponible. Veuillez réessayer dans quelques instants."}), 503

    return jsonify({'error': 'action invalide (start|stop)'}), 400


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
        # Nous n'effectuons plus d'écritures DB ici — fermer la session avant
        # d'appeler l'API LiveKit (réduit la durée de tenue des connexions).
        session.close()

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
                    except Exception as e:
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

    # Étape 1 — vérifications + collecte des ids, dans une session à durée de
    # vie strictement bornée (finally garantit la fermeture sur TOUTE sortie,
    # y compris les `return` anticipés ci-dessous — un `try/except` seul sans
    # `finally` ne les couvre pas et fuit une connexion à chaque appel).
    session = get_session()
    try:
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

        # Capturer uniquement les données nécessaires — la session ferme juste
        # après, AVANT les appels réseau LiveKit potentiellement longs.
        active_ids  = [a.id for a in active_attempts]
        student_ids = {a.id: a.student_id for a in active_attempts}
    finally:
        session.close()

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

    try:
        results = []
        errors = []
        all_egress_ids = []

        if action == 'start':
            # Récupérer les participants LiveKit pour avoir les track IDs
            try:
                r = urlreq.Request(f'{lk_http}/twirp/livekit.RoomService/ListParticipants',
                    data=json.dumps({'room': room_name}).encode(), headers=headers)
                with urlreq.urlopen(r, timeout=5) as resp:
                    parts = {p['identity']: p for p in json.loads(resp.read()).get('participants', [])}
            except Exception:
                parts = {}

            for aid in active_ids:
                sid = student_ids.get(aid)
                if sid is None:
                    errors.append({'attempt_id': aid, 'error': 'student id missing'})
                    continue

                identity = f'student-{sid}'
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
                    errors.append({'attempt_id': aid, 'error': 'caméra non active dans LiveKit'})
                    continue

                ts = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')
                base = f'recordings/exam-{exam_id}/groupe-proctor-{user_id}'

                student_egress_ids = []

                # — Enregistrement caméra (prefixe "groupe-cam-" pour le distinguer du REC individuel)
                cam_path = f'{base}/groupe-cam-{sid}-{ts}.mp4'
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
                        all_egress_ids.append(eid)
                except Exception as e:
                    errors.append({'attempt_id': aid, 'track': 'cam', 'error': str(e)})
                    continue

                # — Enregistrement écran partagé (si présent, prefixe "groupe-ecran-")
                if screen_track:
                    scr_path = f'{base}/groupe-ecran-{sid}-{ts}.mp4'
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
                            all_egress_ids.append(eid)
                    except Exception:
                        pass  # écran optionnel — pas d'erreur bloquante

                if student_egress_ids:
                    results.append({'attempt_id': aid, 'egress_ids': student_egress_ids,
                                    'has_screen': screen_track is not None})

            # Persister current_egress_id — courte session dédiée, ouverte
            # seulement pour l'écriture, après tous les appels LiveKit.
            s2 = get_session()
            try:
                for r in results:
                    first_eid = r['egress_ids'][0] if r['egress_ids'] else None
                    if first_eid:
                        a = s2.query(ExamAttempt).filter_by(id=r['attempt_id']).first()
                        if a:
                            a.current_egress_id = first_eid
                s2.commit()
            finally:
                s2.close()

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
                s2 = get_session()
                try:
                    pairs = []
                    for aid in active_ids:
                        a = s2.query(ExamAttempt).filter_by(id=aid).first()
                        if a and a.current_egress_id:
                            pairs.append((aid, a.current_egress_id))
                finally:
                    s2.close()
                egress_ids_raw = pairs

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
                            s2 = get_session()
                            try:
                                a = s2.query(ExamAttempt).filter_by(id=att_id).first()
                                if a:
                                    a.current_egress_id = None
                                    s2.commit()
                            finally:
                                s2.close()
                except Exception:
                    pass

            return jsonify({'success': True, 'stopped': stopped})

        return jsonify({'error': 'action invalide (start|stop)'}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 500


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

            snaps_list = []
            for snap in snapshots:
                # Nouvelles entrées : image_filename = clé S3
                # Anciennes entrées : image_data = base64 (rétrocompat)
                if snap.image_filename and (
                    snap.image_filename.startswith('snapshots/') or snap.image_filename.startswith('local:')
                ):
                    # Ne pas appeler get_snapshot_url ici (I/O réseau S3) tant
                    # que la session DB est ouverte — juste garder la clé, les
                    # URLs pré-signées sont générées après session.close() ci-dessous.
                    img_url = None
                    img_data = None
                    s3_key = snap.image_filename
                else:
                    img_url = None
                    img_data = snap.image_data  # base64 legacy
                    s3_key = None
                snaps_list.append({
                    'id':           snap.id,
                    'timestamp':    snap.timestamp.isoformat() if snap.timestamp else None,
                    'event_type':   snap.event_type or snap.violation_type,
                    'image_url':    img_url,
                    'image_data':   img_data,
                    's3_key':       s3_key,
                    'face_detected': snap.face_detected,
                    'faces_count':  snap.faces_count,
                    'frame_analysis': snap.frame_analysis,
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

        # Générer les URLs pré-signées S3 hors session DB (I/O réseau)
        from s3_client import get_snapshot_url
        for r in result:
            for s in r.get('snapshots', []):
                key = s.pop('s3_key', None)
                if key:
                    try:
                        s['image_url'] = get_snapshot_url(key)
                    except Exception:
                        s['image_url'] = None

        return jsonify({'exam_id': exam_id, 'students': result})

    except Exception as e:
        try: session.rollback(); session.close()
        except Exception: pass
        print(f"Erreur get_exam_recordings: {e}")
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
    """Client boto3 pour les opérations serveur (list/head) — endpoint interne.
    Ne PAS utiliser ce client pour générer des URLs présignées : passer par
    l'endpoint public (proxifié en HTTPS) fait sortir chaque appel de liste
    sur Internet et revenir, ce qui a provoqué une RecursionError côté boto3
    (constaté en prod) au lieu de simplement lister les objets."""
    return boto3.client(
        's3',
        endpoint_url=os.environ.get('S3_ENDPOINT', ''),
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


def _get_s3_public_client():
    """Client boto3 pour générer des URLs présignées destinées au navigateur —
    endpoint public HTTPS (même domaine que le site, proxifié vers MinIO)."""
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
        if role not in ['student', 'professor', 'admin', 'surveillant', 'superviseur']:
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


# ============================================================================
# API : APPEL PRIVÉ SUPERVISEUR ↔ SURVEILLANT
# ============================================================================
# Un superviseur qui constate qu'un surveillant ne remplit pas son rôle
# (palier de vigilance A/B/C non atteint, alertes ignorées...) doit pouvoir
# lui parler directement plutôt que de simplement le sanctionner sans échange
# — même mécanisme LiveKit privé que surveillant ↔ étudiant, room dédiée par
# surveillant (pas liée à un attempt précis, un superviseur peut appeler en
# dehors de toute surveillance d'examen en cours).

def _supervises_proctor(supervisor_id, proctor_id, session):
    """Le superviseur supervise-t-il un groupe dont ce surveillant est membre ?"""
    return session.query(ProctorGroupSupervisor).join(
        ProctorGroupMember, ProctorGroupMember.group_id == ProctorGroupSupervisor.group_id
    ).filter(
        ProctorGroupSupervisor.supervisor_id == supervisor_id,
        ProctorGroupMember.proctor_id == proctor_id,
    ).first() is not None


@proctoring_bp.route('/api/superviseur/proctor_call/<int:proctor_id>/token', methods=['GET'])
@paseto_required
def get_supervisor_call_token(proctor_id):
    """Token LiveKit pour la room d'appel privée superviseur ↔ surveillant.
    Autorisé pour le superviseur qui supervise réellement ce surveillant, et
    pour le surveillant lui-même (pour répondre)."""
    user_id = get_current_user_id()
    role = get_current_user_role()
    if role not in ('superviseur', 'surveillant', 'admin'):
        return jsonify({'error': 'Accès refusé'}), 403

    config = get_livekit_config()
    if not all([config['url'], config['api_key'], config['api_secret']]):
        return jsonify({'error': 'LiveKit non configuré'}), 503

    session = get_session()
    try:
        if role == 'superviseur' and not _supervises_proctor(user_id, proctor_id, session):
            return jsonify({'error': "Vous ne supervisez pas ce surveillant"}), 403
        if role == 'surveillant' and user_id != proctor_id:
            return jsonify({'error': 'Accès refusé'}), 403

        room_name = f'supcall-{proctor_id}'
        identity  = f'{role}-{user_id}'
        token = generate_livekit_token(
            config['api_key'], config['api_secret'],
            identity, room_name,
            can_publish=True, can_subscribe=True,
            ttl=1800
        )
        return jsonify({'token': token, 'ws_url': config['url'], 'room': room_name, 'identity': identity})
    finally:
        session.close()


@proctoring_bp.route('/api/superviseur/proctor_call/<int:proctor_id>/request', methods=['POST'])
@paseto_required
def request_supervisor_call(proctor_id):
    """Superviseur demande un appel à un surveillant qu'il supervise — notifie
    le surveillant en temps réel où qu'il se trouve dans l'application (pas
    seulement sur sa page de surveillance), même mécanisme que la demande
    d'appel étudiant → surveillant."""
    user_id = get_current_user_id()
    role = get_current_user_role()
    if role != 'superviseur':
        return jsonify({'error': 'Accès réservé aux superviseurs'}), 403

    session = get_session()
    try:
        if not _supervises_proctor(user_id, proctor_id, session):
            return jsonify({'error': "Vous ne supervisez pas ce surveillant"}), 403
        supervisor = session.query(User).filter_by(id=user_id).first()
        supervisor_name = supervisor.full_name if supervisor else 'Votre superviseur'
        session.close()

        try:
            from notif_bus import notify_user
            try:
                notify_user(
                    proctor_id, 'supervisor_call_request',
                    'Appel entrant — votre superviseur',
                    f'{supervisor_name} souhaite vous parler',
                    priority='urgent', tags=['phone'],
                    extra={'supervisor_id': user_id, 'supervisor_name': supervisor_name},
                )
            except Exception:
                pass
        except Exception:
            pass
        return jsonify({'success': True})
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
            s3_public = _get_s3_public_client()
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

            # URL présignée valable 4h — endpoint public (HTTPS, même domaine)
            # pour que le navigateur puisse la charger sans blocage de
            # contenu mixte.
            try:
                url = s3_public.generate_presigned_url(
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
        try: session.rollback(); session.close()
        except Exception: pass
        print(f"Erreur get_video_recordings: {e}")
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


@proctoring_bp.route('/api/agent/claim_lock', methods=['POST'])
def agent_claim_lock():
    """Réclame atomiquement un verrou partagé (Redis SETNX) entre TOUTES les
    instances de l'agent autonome (ex. plusieurs serveurs surveillant la même
    plateforme en parallèle pour la résilience) — évite les doublons d'alerte/
    email quand plus d'un agent tourne en même temps. `key` est un identifiant
    arbitraire choisi par l'agent (ex. "alert:{attempt_id}", "summary:{exam_id}"),
    `ttl_seconds` la durée du verrou. Retourne claimed=true seulement pour le
    PREMIER agent à réclamer cette clé pendant la fenêtre ; claimed=false pour
    tout autre agent qui tente la même clé avant expiration — il doit alors
    considérer que l'action a déjà été prise ailleurs et ne rien refaire.
    En cas de panne Redis, retourne claimed=true (laisser passer plutôt que
    bloquer silencieusement toute alerte)."""
    if not _agent_auth():
        return jsonify({'error': 'Non autorisé'}), 403
    data = request.get_json(silent=True) or {}
    key = data.get('key')
    if not key:
        return jsonify({'error': 'key requis'}), 400
    try:
        ttl_seconds = max(1, min(86400, int(data.get('ttl_seconds', 600))))
    except (TypeError, ValueError):
        ttl_seconds = 600
    try:
        r = _redis_alerts()
        claimed = r.set(f'cei:agent:lock:{key}', '1', nx=True, ex=ttl_seconds)
        r.close()
        return jsonify({'claimed': bool(claimed)})
    except Exception as exc:
        _alerts_log.warning('claim_lock failed (%s): %s', key, exc)
        return jsonify({'claimed': True})


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
@limiter.exempt
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
        attempt_ids = [a.id for a in attempts]

        # Comptage réel des événements no_face_detected / multiple_faces par
        # tentative (une seule requête groupée plutôt qu'une par tentative) —
        # ces deux champs étaient auparavant codés en dur à 0 (jamais requêtés),
        # ce qui faussait silencieusement les résumés IA envoyés par email.
        face_counts = {}
        if attempt_ids:
            from sqlalchemy import func as _sa_func2
            rows = session.query(
                ExamActivityLog.attempt_id,
                ExamActivityLog.event_type,
                _sa_func2.count(ExamActivityLog.id)
            ).filter(
                ExamActivityLog.attempt_id.in_(attempt_ids),
                ExamActivityLog.event_type.in_(['no_face_detected', 'multiple_faces'])
            ).group_by(ExamActivityLog.attempt_id, ExamActivityLog.event_type).all()
            for aid, etype, cnt in rows:
                face_counts.setdefault(aid, {})[etype] = cnt

        attempts_data = []
        for a in attempts:
            student = session.query(User).filter_by(id=a.student_id).first()
            counts = face_counts.get(a.id, {})
            attempts_data.append({
                'id':                     a.id,
                'student_name':           student.full_name if student else '?',
                'status':                 a.status.value if hasattr(a.status, 'value') else str(a.status),
                'risk_score':             a.risk_score or 0,
                'tab_switches':           a.tab_switches or 0,
                'warnings_count':         a.warnings_count or 0,
                'no_face_detected_count': counts.get('no_face_detected', 0),
                'multiple_faces_count':   counts.get('multiple_faces', 0),
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
@limiter.exempt
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
        try: session.rollback(); session.close()
        except Exception: pass
        return jsonify({'error': str(e), 'alive': False}), 500
