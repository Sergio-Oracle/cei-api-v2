"""
Bus de notifications CEI — combinaison Redis Pub/Sub + ntfy.

Chaque appel à notify_user / notify_exam :
  1. Publie sur un canal Redis Pub/Sub  → reçu par /api/notifications/poll (long-poll navigateur)
  2. Pousse sur ntfy                    → notification mobile / hors navigateur

Usage :
    from notif_bus import notify_user, notify_exam

    notify_user(student_id, 'correction_done', 'Copie corrigée', 'Note : 14.5/20', 'high')
    notify_exam(exam_id, 'student_banned', 'Étudiant exclu', 'Moussa Diallo — fraude', 'urgent')
"""
import os, json, logging
from concurrent.futures import ThreadPoolExecutor

import redis as _redis

from ntfy_client import push as _ntfy_push

_log       = logging.getLogger('cei.notif_bus')
_REDIS_URL = os.getenv('REDIS_URL', 'redis://127.0.0.1:6379/0')

# Correctif montée en charge (29/08, audit) : chaque appel notify_user/
# notify_exam créait 2 threads OS natifs (Thread(...).start()) sans aucune
# limite — publier les résultats d'un examen à 300 étudiants (une boucle
# notify_user par étudiant, voir publish_exam_results) créait ~600 threads
# d'un coup. Un pool borné absorbe les rafales sans faire exploser le
# nombre de threads système ; ces publications sont courtes (Redis PUBLISH,
# appel HTTP ntfy), une file d'attente sur quelques workers suffit largement.
_executor = ThreadPoolExecutor(max_workers=16, thread_name_prefix='notif_bus')

# Pool dédié aux publications (opérations courtes, max 5 connexions)
_pool = _redis.ConnectionPool.from_url(
    _REDIS_URL,
    decode_responses=True,
    max_connections=5,
    socket_connect_timeout=1,
)


def _get_redis() -> _redis.Redis:
    return _redis.Redis(connection_pool=_pool)


# ── Publication Redis ────────────────────────────────────────────────────────

def _redis_publish(channel: str, payload: dict) -> None:
    try:
        _get_redis().publish(channel, json.dumps(payload))
    except Exception as exc:
        _log.warning('Redis publish failed channel=%s: %s', channel, exc)


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
    Canal Redis : cei:notif:user:{user_id}
    Topic ntfy  : student-{user_id}

    `extra` : champs additionnels fusionnés dans le payload (ex: exam_id/
    attempt_id pour un lien profond côté frontend) — jamais utilisé pour du
    contenu affiché tel quel, seulement pour du routage/deep-linking.
    """
    payload = {'type': event_type, 'title': title, 'message': message}
    if extra:
        payload.update(extra)
    _executor.submit(_redis_publish, f'cei:notif:user:{user_id}', payload)
    try:
        _executor.submit(_ntfy_push, f'student-{user_id}', title, message, priority, tags)
    except Exception as exc:
        _log.warning('Failed to submit ntfy task for user %s: %s', user_id, exc)


def notify_exam(
    exam_id: int,
    event_type: str,
    title: str,
    message: str,
    priority: str = 'default',
    tags: list[str] | None = None,
) -> None:
    """
    Notifie tous les superviseurs d'un examen (prof + surveillants).
    Canal Redis : cei:notif:exam:{exam_id}
    Topic ntfy  : exam-{exam_id}
    """
    payload = {'type': event_type, 'title': title, 'message': message}
    _executor.submit(_redis_publish, f'cei:notif:exam:{exam_id}', payload)
    try:
        _executor.submit(_ntfy_push, f'exam-{exam_id}', title, message, priority, tags)
    except Exception as exc:
        _log.warning('Failed to submit ntfy task for exam %s: %s', exam_id, exc)


def _publish_to_admins(payload: dict) -> None:
    """Publie sur le canal Redis individuel de chaque administrateur (pour le long-poll)."""
    try:
        from models import get_session, User, UserRole
        session = get_session()
        try:
            admin_ids = [u.id for u in session.query(User).filter_by(role=UserRole.ADMIN).all()]
        finally:
            session.close()
        r = _get_redis()
        for admin_id in admin_ids:
            r.publish(f'cei:notif:user:{admin_id}', json.dumps(payload))
    except Exception as exc:
        _log.warning('notify_admins redis publish failed: %s', exc)


def notify_admins(
    event_type: str,
    title: str,
    message: str,
    priority: str = 'default',
    tags: list[str] | None = None,
) -> None:
    """
    Notifie tous les administrateurs (alertes infra : panne MinIO, etc.).
    Canal Redis : cei:notif:user:{admin_id} (un par admin, pour le badge Header)
    Topic ntfy  : admin-alerts
    """
    payload = {'type': event_type, 'title': title, 'message': message}
    _executor.submit(_publish_to_admins, payload)
    try:
        _executor.submit(_ntfy_push, 'admin-alerts', title, message, priority, tags)
    except Exception as exc:
        _log.warning('Failed to submit ntfy task for admins: %s', exc)
