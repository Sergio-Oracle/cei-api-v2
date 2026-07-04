"""
Client ntfy non-bloquant — publie dans un Thread daemon.

Variables d'environnement :
  NTFY_URL   = http://127.0.0.1:2586          (vide = désactivé)
  NTFY_TOKEN = tk_xxx                          (optionnel)
"""
import os, logging
from threading import Thread

import requests

_log        = logging.getLogger('cei.ntfy')
_NTFY_URL   = os.getenv('NTFY_URL', '').rstrip('/')
_NTFY_TOKEN = os.getenv('NTFY_TOKEN', '')


def _push(topic: str, title: str, message: str, priority: str, tags: list[str]) -> None:
    try:
        headers: dict[str, str] = {
            'Title':    title,
            'Priority': priority,
        }
        if tags:
            headers['Tags'] = ','.join(tags)
        if _NTFY_TOKEN:
            headers['Authorization'] = f'Bearer {_NTFY_TOKEN}'
        requests.post(
            f'{_NTFY_URL}/{topic}',
            data=message.encode('utf-8'),
            headers=headers,
            timeout=3,
        )
    except Exception as exc:
        _log.warning('ntfy push failed topic=%s: %s', topic, exc)


def push(
    topic: str,
    title: str,
    message: str,
    priority: str = 'default',
    tags: list[str] | None = None,
) -> None:
    """Publie une notification ntfy sans bloquer le thread appelant."""
    if not _NTFY_URL:
        return
    Thread(
        target=_push,
        args=(topic, title, message, priority, tags or []),
        daemon=True,
    ).start()
