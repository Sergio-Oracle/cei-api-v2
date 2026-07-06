"""
Client S3/MinIO pour CEI — snapshots caméra et fichiers partagés.

Variables d'environnement (déjà présentes dans .env) :
  S3_KEY_ID            = serge
  S3_KEY_SECRET        = passer123
  S3_ENDPOINT          = http://62.171.190.6:9000
  S3_REGION            = us-east-1
  S3_SNAPSHOTS_BUCKET  = cei-snapshots   (bucket dédié aux snapshots)

Usage :
    from s3_client import upload_snapshot, get_snapshot_url

    key = upload_snapshot(exam_id=3, attempt_id=12, image_b64="data:image/jpeg;base64,...")
    url = get_snapshot_url(key)   # URL pré-signée valide 1h, ou URL locale si fallback disque

Fallback disque :
  Si MinIO est injoignable (service arrêté, panne réseau...), l'upload bascule
  automatiquement sur le disque local (UPLOAD_FOLDER/snapshots_fallback/...) au lieu
  de faire échouer toute la capture proctoring. Les admins sont alertés (Redis
  Pub/Sub + ntfy) au premier échec, avec un cooldown pour éviter le flood, puis
  re-notifiés quand MinIO redevient disponible.
"""
import os, base64, logging
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Optional

import boto3
from botocore.client import Config

_log = logging.getLogger('cei.s3')

_ENDPOINT  = os.getenv('S3_ENDPOINT',          'http://62.171.190.6:9000')
_KEY_ID    = os.getenv('S3_KEY_ID',            '')
_SECRET    = os.getenv('S3_KEY_SECRET',        '')
_REGION    = os.getenv('S3_REGION',            'us-east-1')
_SNAP_BUCKET = os.getenv('S3_SNAPSHOTS_BUCKET', 'cei-snapshots')

_URL_EXPIRY = 3600  # secondes — URL pré-signée valide 1 h

_UPLOAD_FOLDER  = os.getenv('UPLOAD_FOLDER', 'static/uploads')
_FALLBACK_DIR   = 'snapshots_fallback'
_LOCAL_PREFIX   = 'local:'

_REDIS_URL       = os.getenv('REDIS_URL', 'redis://127.0.0.1:6379/0')
_ALERT_COOLDOWN  = int(os.getenv('S3_ALERT_COOLDOWN', '300'))  # 5 min entre 2 alertes admin
_DOWN_FLAG_KEY   = 'cei:s3:down'


@lru_cache(maxsize=1)
def _client():
    """Client boto3 (singleton, thread-safe en lecture)."""
    return boto3.client(
        's3',
        endpoint_url=_ENDPOINT,
        aws_access_key_id=_KEY_ID,
        aws_secret_access_key=_SECRET,
        config=Config(signature_version='s3v4'),
        region_name=_REGION,
    )


@lru_cache(maxsize=1)
def _redis_client():
    import redis
    return redis.Redis.from_url(_REDIS_URL, decode_responses=True, socket_connect_timeout=1)


def _alert_admins_s3_down(exc: Exception) -> None:
    """Alerte admin au premier échec MinIO — cooldown Redis pour éviter le flood."""
    try:
        r = _redis_client()
        # SET NX : ne pose le flag (et n'alerte) que si absent — sinon on est déjà en cooldown
        if not r.set(_DOWN_FLAG_KEY, '1', nx=True, ex=_ALERT_COOLDOWN):
            return
    except Exception as redis_exc:
        _log.warning('s3 down + redis cooldown check failed: %s', redis_exc)
        # Redis lui-même est injoignable — on tente quand même l'alerte une fois
    try:
        from notif_bus import notify_admins
        notify_admins(
            's3_down',
            'MinIO indisponible',
            f'Les snapshots caméra basculent sur le disque local du serveur. Erreur : {exc}',
            priority='urgent',
            tags=['warning', 'floppy_disk'],
        )
    except Exception as notif_exc:
        _log.warning('admin alert (s3 down) failed: %s', notif_exc)


def _alert_admins_s3_recovered() -> None:
    """Notifie les admins quand MinIO redevient joignable après une panne."""
    try:
        r = _redis_client()
        if not r.delete(_DOWN_FLAG_KEY):
            return  # pas de panne enregistrée — pas de notification de retour à la normale
    except Exception as redis_exc:
        _log.warning('s3 recovered + redis flag clear failed: %s', redis_exc)
        return
    try:
        from notif_bus import notify_admins
        notify_admins(
            's3_recovered',
            'MinIO rétabli',
            'Les nouveaux snapshots caméra sont de nouveau envoyés vers MinIO.',
            priority='default',
            tags=['white_check_mark'],
        )
    except Exception as notif_exc:
        _log.warning('admin alert (s3 recovered) failed: %s', notif_exc)


def _save_snapshot_locally(exam_id: int, attempt_id: int, raw: bytes, ts: str) -> Optional[str]:
    """Écrit le JPEG sur le disque local. Retourne la clé préfixée 'local:' ou None."""
    try:
        rel_dir = os.path.join(_FALLBACK_DIR, str(exam_id), str(attempt_id))
        abs_dir = os.path.join(_UPLOAD_FOLDER, rel_dir)
        Path(abs_dir).mkdir(parents=True, exist_ok=True)
        filename = f'{ts}.jpg'
        with open(os.path.join(abs_dir, filename), 'wb') as f:
            f.write(raw)
        return f'{_LOCAL_PREFIX}{rel_dir}/{filename}'
    except Exception as exc:
        _log.error('local snapshot fallback write failed attempt=%s: %s', attempt_id, exc)
        return None


def upload_snapshot(exam_id: int, attempt_id: int, image_b64: str) -> Optional[str]:
    """
    Décode le base64, uploade vers MinIO et retourne la clé S3.

    La clé a le format : snapshots/{exam_id}/{attempt_id}/{ts}.jpg
    Si MinIO est injoignable, bascule sur un fallback disque local
    (clé préfixée 'local:') et alerte les admins au lieu de perdre le snapshot.
    Retourne None seulement si le décodage base64 échoue ou si le fallback
    disque échoue aussi (cas extrême : disque plein/inaccessible).
    """
    if not _KEY_ID or not image_b64:
        return None
    try:
        # Gérer "data:image/jpeg;base64,..." et le base64 brut
        if ',' in image_b64:
            image_b64 = image_b64.split(',', 1)[1]
        raw = base64.b64decode(image_b64)
    except Exception as exc:
        _log.warning('snapshot decode failed attempt=%s: %s', attempt_id, exc)
        return None

    ts  = datetime.utcnow().strftime('%Y%m%dT%H%M%S')
    key = f'snapshots/{exam_id}/{attempt_id}/{ts}.jpg'

    try:
        _client().put_object(
            Bucket=_SNAP_BUCKET,
            Key=key,
            Body=raw,
            ContentType='image/jpeg',
        )
        _alert_admins_s3_recovered()  # no-op silencieux si aucune panne n'était en cours
        return key
    except Exception as exc:
        # Capture large intentionnelle : ClientError (erreur S3) ET les pannes de
        # connexion (EndpointConnectionError, timeout...) qui n'héritent PAS de
        # ClientError et remonteraient sinon jusqu'à la route Flask (500, snapshot perdu).
        _log.warning('S3 upload failed key=%s: %s — fallback disque local', key, exc)
        _alert_admins_s3_down(exc)
        return _save_snapshot_locally(exam_id, attempt_id, raw, ts)


def get_snapshot_url(key: str) -> Optional[str]:
    """
    Génère l'URL d'accès à un snapshot à partir de sa clé.
    - Clé S3 ('snapshots/...')   → URL pré-signée MinIO valide 1 h.
    - Clé locale ('local:...')   → chemin relatif servi par
      GET /api/proctoring/snapshot_local/<path> (voir proctoring_routes.py).
    """
    if not key:
        return None
    if key.startswith(_LOCAL_PREFIX):
        return f'/api/proctoring/snapshot_local/{key[len(_LOCAL_PREFIX):]}'
    if not _KEY_ID:
        return None
    try:
        return _client().generate_presigned_url(
            'get_object',
            Params={'Bucket': _SNAP_BUCKET, 'Key': key},
            ExpiresIn=_URL_EXPIRY,
        )
    except Exception as exc:
        _log.warning('presigned URL failed key=%s: %s', key, exc)
        return None
