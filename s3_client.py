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
    url = get_snapshot_url(key)   # URL pré-signée valide 1h
"""
import os, base64, logging
from datetime import datetime
from functools import lru_cache
from typing import Optional

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError

_log = logging.getLogger('cei.s3')

_ENDPOINT  = os.getenv('S3_ENDPOINT',          'http://62.171.190.6:9000')
_KEY_ID    = os.getenv('S3_KEY_ID',            '')
_SECRET    = os.getenv('S3_KEY_SECRET',        '')
_REGION    = os.getenv('S3_REGION',            'us-east-1')
_SNAP_BUCKET = os.getenv('S3_SNAPSHOTS_BUCKET', 'cei-snapshots')

_URL_EXPIRY = 3600  # secondes — URL pré-signée valide 1 h


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


def upload_snapshot(exam_id: int, attempt_id: int, image_b64: str) -> Optional[str]:
    """
    Décode le base64, uploade vers MinIO et retourne la clé S3.

    La clé a le format : snapshots/{exam_id}/{attempt_id}/{ts}.jpg
    Retourne None si l'upload échoue (dégradation silencieuse).
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
        return key
    except ClientError as exc:
        _log.warning('S3 upload failed key=%s: %s', key, exc)
        return None


def get_snapshot_url(key: str) -> Optional[str]:
    """Génère une URL pré-signée (1 h) à partir de la clé S3."""
    if not key or not _KEY_ID:
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
