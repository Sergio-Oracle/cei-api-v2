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
from threading import Thread

import redis as _redis

from ntfy_client import push as _ntfy_push

_log       = logging.getLogger('cei.notif_bus')
_REDIS_URL = os.getenv('REDIS_URL', 'redis://127.0.0.1:6379/0')

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
) -> None:
    """
    Notifie un utilisateur précis (étudiant, professeur).
    Canal Redis : cei:notif:user:{user_id}
    Topic ntfy  : student-{user_id}
    """
    payload = {'type': event_type, 'title': title, 'message': message}
    Thread(target=_redis_publish, args=(f'cei:notif:user:{user_id}', payload), daemon=True).start()
    _ntfy_push(f'student-{user_id}', title, message, priority, tags)


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
    Thread(target=_redis_publish, args=(f'cei:notif:exam:{exam_id}', payload), daemon=True).start()
    _ntfy_push(f'exam-{exam_id}', title, message, priority, tags)
