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
    """Build a human-readable cache key so cache_delete_pattern glob matching works.
    For large/arbitrary content keys (e.g. AI prompts), use make_content_key instead."""
    return 'cei:' + ':'.join(str(p) for p in parts)


def make_content_key(*parts: str) -> str:
    """Hash-based key for large content (AI prompts, etc.) where glob invalidation is not needed."""
    raw = ':'.join(str(p) for p in parts)
    return 'cei:content:' + hashlib.sha256(raw.encode()).hexdigest()[:24]


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


def cache_set_nx(key: str, ttl: int) -> bool:
    """Pose un verrou atomique : True si CE call vient de le poser (à agir),
    False s'il existait déjà (un autre appelant l'a déjà pris) ou si Redis est
    indisponible (par prudence, on n'agit pas sans verrou fiable)."""
    client = _get_client()
    if client is None:
        return False
    try:
        return bool(client.set(key, '1', nx=True, ex=ttl))
    except Exception:
        return False


def cache_should_score(key: str, ttl: int) -> bool:
    """Verrou de déduplication pour la notation de risque (proctoring) —
    PAS le même contrat que cache_set_nx. Ici, contrairement à un verrou de
    sécurité, on ne veut JAMAIS bloquer la notation à cause d'une panne
    Redis : cache_set_nx retourne False si Redis est indisponible (bon
    réflexe pour un verrou fiable, ex. session unique), mais réutilisé tel
    quel ici cela désactiverait silencieusement TOUT le scoring de risque de
    la plateforme pendant une panne — inacceptable pour une fonctionnalité
    de confort (anti-flood/anti-doublon). Donc : Redis down ou erreur ⇒
    True (on note, comme si de rien n'était) ; Redis dispo ⇒ comportement
    normal (True seulement si la clé vient d'être posée)."""
    client = _get_client()
    if client is None:
        return True
    try:
        return bool(client.set(key, '1', nx=True, ex=ttl))
    except Exception:
        return True


_POP_SCRIPT = "local v = redis.call('GET', KEYS[1]); redis.call('DEL', KEYS[1]); return v"


def cache_pop(key: str) -> Optional[Any]:
    """Lit puis supprime la clé de façon atomique — évite une race entre
    cache_get et cache_delete pour une preuve à usage unique (ex. flag de
    vérification biométrique consommé par start_exam_attempt). Implémenté en
    Lua (EVAL) plutôt qu'avec la commande GETDEL : ce projet tourne encore sur
    Redis 6.0, GETDEL n'existe qu'à partir de 6.2 (confirmé — GETDEL échouait
    silencieusement avec ResponseError, avalé par le except générique)."""
    client = _get_client()
    if client is None:
        return None
    try:
        raw = client.eval(_POP_SCRIPT, 1, key)
        return json.loads(raw) if raw else None
    except Exception:
        return None


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
