"""
Lightweight Redis cache layer.

Usage:
    from cache import cache_get, cache_set, cache_delete, make_key

All functions degrade gracefully: if Redis is unavailable, cache_get
returns None (miss) and cache_set is a no-op — the app keeps working.
"""
from __future__ import annotations
import hashlib
import json
import os
from typing import Any, Optional

import redis

_REDIS_URL = os.getenv('REDIS_URL', 'redis://127.0.0.1:6379/0')
_DEFAULT_TTL = int(os.getenv('CACHE_TTL_SECONDS', '3600'))   # 1 hour

_client: Optional[redis.Redis] = None


def _get_client() -> Optional[redis.Redis]:
    global _client
    if _client is None:
        try:
            _client = redis.from_url(_REDIS_URL, decode_responses=True, socket_connect_timeout=1)
            _client.ping()
        except Exception as e:
            print(f'[cache] Redis unavailable ({e}) — running without cache')
            _client = None
    return _client


def make_key(*parts: str) -> str:
    """Hash arbitrary parts into a deterministic cache key."""
    raw = ':'.join(str(p) for p in parts)
    return 'cei:' + hashlib.sha256(raw.encode()).hexdigest()[:24]


def cache_get(key: str) -> Optional[Any]:
    """Return deserialised value or None on miss / error."""
    client = _get_client()
    if client is None:
        return None
    try:
        raw = client.get(key)
        return json.loads(raw) if raw else None
    except Exception:
        return None


def cache_set(key: str, value: Any, ttl: int = _DEFAULT_TTL) -> None:
    """Serialise and store value; silently skip on error."""
    client = _get_client()
    if client is None:
        return
    try:
        client.setex(key, ttl, json.dumps(value, ensure_ascii=False))
    except Exception:
        pass


def cache_delete(key: str) -> None:
    client = _get_client()
    if client is None:
        return
    try:
        client.delete(key)
    except Exception:
        pass


def cache_delete_pattern(pattern: str) -> None:
    """Delete all keys matching a glob pattern (use sparingly — SCAN-based)."""
    client = _get_client()
    if client is None:
        return
    try:
        keys = list(client.scan_iter(pattern))
        if keys:
            client.delete(*keys)
    except Exception:
        pass
