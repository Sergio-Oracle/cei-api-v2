"""
Bus de notifications CEI — file Redis par utilisateur.

Chaque appel à notify_user / notify_exam / notify_admins ajoute un événement
à la liste Redis `cei:notif:user:{id}` de chaque destinataire. Le client
draine cette file à intervalle court via /api/notifications/poll (voir
hooks/useNotificationPoll.ts) — plus aucune connexion tenue côté serveur.

Historique :
  - un canal « ntfy » (push mobile) a existé ici, jamais déployé — retiré le 09/09.
  - la livraison se faisait ensuite par Redis Pub/Sub + long-polling 25 s
    (une connexion Gunicorn tenue par client) — remplacé le 09/09 par cette
    file Redis + poll court. Avantages : zéro connexion tenue, et un
    événement émis alors que le client n'a aucun onglet ouvert est conservé
    (TTL 1 h, 50 max) au lieu d'être perdu comme avec Pub/Sub.

Les paramètres `priority` / `tags` des fonctions publiques sont conservés
(acceptés, ignorés) pour ne pas casser les ~15 appelants et rester prêts
pour un futur canal de push (Web Push/VAPID).

Usage :
    from notif_bus import notify_user, notify_exam

    notify_user(student_id, 'correction_done', 'Copie corrigée', 'Note : 14.5/20', 'high')
    notify_exam(exam_id, 'student_banned', 'Étudiant exclu', 'Moussa Diallo — fraude', 'urgent')
"""
import os, json, time, logging
from concurrent.futures import ThreadPoolExecutor

import redis as _redis

_log       = logging.getLogger('cei.notif_bus')
_REDIS_URL = os.getenv('REDIS_URL', 'redis://127.0.0.1:6379/0')

# File par utilisateur : on garde les N derniers événements, avec un TTL —
# une file jamais drainée (utilisateur parti) disparaît d'elle-même.
_QUEUE_MAX = 50
_QUEUE_TTL = 3600  # 1 h

# Correctif montée en charge (29/08, audit) : chaque appel notify_user/
# notify_exam créait des threads OS natifs sans limite — publier les
# résultats d'un examen à 300 étudiants créait des centaines de threads d'un
# coup. Un pool borné absorbe les rafales ; ces écritures sont courtes
# (RPUSH/LTRIM/EXPIRE en pipeline), quelques workers suffisent.
_executor = ThreadPoolExecutor(max_workers=16, thread_name_prefix='notif_bus')

# Pool dédié aux écritures (opérations courtes, max 5 connexions)
_pool = _redis.ConnectionPool.from_url(
    _REDIS_URL,
    decode_responses=True,
    max_connections=5,
    socket_connect_timeout=1,
)


def _get_redis() -> _redis.Redis:
    return _redis.Redis(connection_pool=_pool)


# ── Écriture dans les files Redis ───────────────────────────────────────────

def _enqueue(user_id, payload: dict) -> None:
    """Ajoute un événement à la file d'un utilisateur (RPUSH = plus récent en
    queue), tronque aux N derniers, rafraîchit le TTL — le tout en pipeline."""
    try:
        key = f'cei:notif:user:{user_id}'
        p = _get_redis().pipeline(transaction=True)
        p.rpush(key, json.dumps(payload))
        p.ltrim(key, -_QUEUE_MAX, -1)
        p.expire(key, _QUEUE_TTL)
        p.execute()
    except Exception as exc:
        _log.warning('Redis enqueue failed user=%s: %s', user_id, exc)


def _enqueue_many(user_ids, payload: dict) -> None:
    """Même chose pour plusieurs utilisateurs, en un seul aller-retour Redis
    (clés indépendantes → pas besoin de transaction globale)."""
    try:
        r = _get_redis()
        data = json.dumps(payload)
        p = r.pipeline(transaction=False)
        for uid in user_ids:
            key = f'cei:notif:user:{uid}'
            p.rpush(key, data)
            p.ltrim(key, -_QUEUE_MAX, -1)
            p.expire(key, _QUEUE_TTL)
        p.execute()
    except Exception as exc:
        _log.warning('Redis enqueue_many failed: %s', exc)


def _payload(event_type: str, title: str, message: str, extra: dict | None = None) -> dict:
    d = {'type': event_type, 'title': title, 'message': message, 'ts': int(time.time() * 1000)}
    if extra:
        d.update(extra)
    return d


# ── API publique ─────────────────────────────────────────────────────────────

def notify_user(
    user_id: int,
    event_type: str,
    title: str,
    message: str,
    priority: str = 'default',
    tags: list[str] | None = None,
    extra: dict | None = None,
) -> None:
    """
    Notifie un utilisateur précis (étudiant, professeur).
    File Redis : cei:notif:user:{user_id}

    `extra` : champs additionnels fusionnés dans le payload (ex: exam_id/
    attempt_id pour un lien profond côté frontend) — jamais utilisé pour du
    contenu affiché tel quel, seulement pour du routage/deep-linking.
    """
    _executor.submit(_enqueue, user_id, _payload(event_type, title, message, extra))


def _exam_staff_ids(exam_id: int) -> set:
    """Ensemble des identifiants du personnel couvrant un examen :
    surveillants assignés (ExamProctor) + superviseur(s) du/des groupe(s)
    rattaché(s) à l'EC + professeur créateur. Best-effort, session isolée."""
    ids: set = set()
    try:
        from models import (
            get_session, OnlineExam, ExamProctor,
            ProctorGroup, ProctorGroupEC,
        )
        session = get_session()
        try:
            exam = session.query(OnlineExam).get(exam_id)
            if not exam:
                return ids

            for ep in session.query(ExamProctor).filter_by(exam_id=exam_id).all():
                if ep.proctor_id:
                    ids.add(ep.proctor_id)

            ec_id = exam.subject.ec_id if exam.subject else None
            if ec_id:
                groups = (
                    session.query(ProctorGroup)
                    .join(ProctorGroupEC, ProctorGroupEC.group_id == ProctorGroup.id)
                    .filter(ProctorGroupEC.ec_id == ec_id)
                    .all()
                )
                for g in groups:
                    for s in g.supervisors:
                        if s.supervisor_id:
                            ids.add(s.supervisor_id)

            if exam.created_by_id:
                ids.add(exam.created_by_id)
        finally:
            session.close()
    except Exception as exc:
        _log.warning('_exam_staff_ids(%s) failed: %s', exam_id, exc)
    return ids


def notify_exam(
    exam_id: int,
    event_type: str,
    title: str,
    message: str,
    priority: str = 'default',
    tags: list[str] | None = None,
) -> None:
    """
    Notifie tout le personnel couvrant un examen (surveillants assignés +
    superviseur(s) du groupe + professeur créateur), sur la file individuelle
    de chacun.

    Auparavant cette fonction publiait sur cei:notif:exam:{id}, un canal
    Pub/Sub que *personne* n'abonnait : toutes les alertes surveillant
    (bannissement, risque élevé, surveillant déconnecté…) partaient dans le
    vide (corrigé le 09/09, en même temps que le retrait de ntfy).
    """
    payload = _payload(event_type, title, message, {'exam_id': exam_id})

    def _fan_out() -> None:
        staff_ids = _exam_staff_ids(exam_id)
        if staff_ids:
            _enqueue_many(staff_ids, payload)

    _executor.submit(_fan_out)


def _enqueue_admins(payload: dict) -> None:
    """Ajoute l'événement à la file de chaque administrateur."""
    try:
        from models import get_session, User, UserRole
        session = get_session()
        try:
            admin_ids = [u.id for u in session.query(User).filter_by(role=UserRole.ADMIN).all()]
        finally:
            session.close()
        if admin_ids:
            _enqueue_many(admin_ids, payload)
    except Exception as exc:
        _log.warning('notify_admins enqueue failed: %s', exc)


def notify_admins(
    event_type: str,
    title: str,
    message: str,
    priority: str = 'default',
    tags: list[str] | None = None,
) -> None:
    """
    Notifie tous les administrateurs (alertes infra : panne MinIO, etc.).
    File Redis : cei:notif:user:{admin_id} (une par admin, pour le badge Header)
    """
    _executor.submit(_enqueue_admins, _payload(event_type, title, message))
