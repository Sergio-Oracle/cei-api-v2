"""Application « CEI Examen » (client kiosque équivalent de Safe Exam Browser) : poignée de main signée et distribution.

  GET  /api/exam-client/challenge?exam_id=N     étudiant connecté : nonce à faire signer par le client
  POST /api/exam-client/verify                  vérifie la signature HMAC du client officiel, ouvre la porte « application » pour CET examen
  GET  /api/exam-client/releases                public : versions téléchargeables (dossier CLIENT_DIST_DIR/latest.json)
  GET  /api/exam-client/download/<fichier>      public : téléchargement d'un fichier listé dans latest.json (jamais d'autre chemin)

Honnêteté sur la sécurité : la signature prouve que l'appelant connaît le secret embarqué dans le client (CEI_CLIENT_SECRET),
comme la clé de navigateur d'examen de SEB : elle relève le niveau d'effort d'une usurpation, elle ne la rend pas impossible.
"""
import hashlib
import hmac
import json
import os
import re
import secrets
from urllib.parse import quote

from flask import Blueprint, jsonify, request, send_from_directory

from auth_paseto import paseto_required, get_current_user_id
from cache import cache_get, cache_set, cache_delete

exam_client_bp = Blueprint('exam_client', __name__)

# Dossier SERVI DIRECTEMENT PAR NGINX (location /static/) : les installateurs (~120 Mo) ne passent jamais par les processus de l'API.
DIST_DIR = os.getenv('CLIENT_DIST_DIR', os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'static', 'downloads'))
_UA_RE = re.compile(r'CEIExamClient/(\d+(?:\.\d+)*)')


def _secret():
    return os.getenv('CEI_CLIENT_SECRET', '')


def _vtuple(v):
    return tuple(int(x) for x in re.findall(r'\d+', v or '0')[:4])


@exam_client_bp.route('/api/exam-client/challenge', methods=['GET'])
@paseto_required
def exam_client_challenge():
    user_id = get_current_user_id()
    nonce = secrets.token_hex(16)
    cache_set(f'cei:clientnonce:{user_id}:{nonce}', 1, 300)
    return jsonify({'nonce': nonce, 'user_id': user_id})


@exam_client_bp.route('/api/exam-client/verify', methods=['POST'])
@paseto_required
def exam_client_verify():
    user_id = get_current_user_id()
    data = request.get_json(silent=True) or {}
    nonce, sig = str(data.get('nonce') or ''), str(data.get('sig') or '')
    version, platform = str(data.get('version') or ''), str(data.get('platform') or '')
    try:
        exam_id = int(data.get('exam_id'))
    except (TypeError, ValueError):
        return jsonify({'error': 'Examen manquant'}), 400
    secret = _secret()
    if not secret:
        return jsonify({'error': "L'application CEI Examen n'est pas encore configurée sur ce serveur."}), 503
    m = _UA_RE.search(request.headers.get('User-Agent', ''))
    if not m:
        return jsonify({'error': "Ce navigateur n'est pas l'application CEI Examen.", 'client_required': True}), 403
    key = f'cei:clientnonce:{user_id}:{nonce}'
    if not nonce or not cache_get(key):
        return jsonify({'error': 'Défi expiré, réessayez.'}), 400
    cache_delete(key)                                   # usage unique
    want = hmac.new(secret.encode(), f'{nonce}|{user_id}|{version}|{platform}'.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(want, sig) or m.group(1) != version:
        return jsonify({'error': "Signature de l'application invalide.", 'client_required': True}), 403
    minv = os.getenv('CEI_CLIENT_MIN_VERSION', '')
    if minv and _vtuple(version) < _vtuple(minv):
        return jsonify({'error': f"Version {version} trop ancienne : installez la version {minv} ou plus récente.", 'client_outdated': True,
                        'download_url': '/telecharger-client'}), 426
    cache_set(f'cei:client_ok:{user_id}:{exam_id}', {'version': version, 'platform': platform}, 14 * 3600)
    return jsonify({'ok': True, 'version': version, 'vm': bool(data.get('vm'))})


def _latest():
    try:
        with open(os.path.join(DIST_DIR, 'latest.json'), encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {'version': None, 'files': []}


@exam_client_bp.route('/api/exam-client/releases', methods=['GET'])
def exam_client_releases():
    d = _latest()
    files = []
    for x in d.get('files', []):
        p = os.path.join(DIST_DIR, x.get('name', ''))
        if os.path.isfile(p):
            files.append({'name': x['name'], 'platform': x.get('platform', ''), 'arch': x.get('arch', ''), 'kind': x.get('kind', ''), 'label': x.get('label', x['name']), 'size': os.path.getsize(p),
                          'sha256': x.get('sha256', ''), 'url': '/static/downloads/' + quote(x['name'])})
    return jsonify({'version': d.get('version'), 'files': files, 'notes': d.get('notes', '')})


@exam_client_bp.route('/api/exam-client/download/<path:name>', methods=['GET'])
def exam_client_download(name):
    listed = {x.get('name') for x in _latest().get('files', [])}
    if name not in listed or '/' in name or name.startswith('.'):
        return jsonify({'error': 'Fichier introuvable'}), 404
    return send_from_directory(DIST_DIR, name, as_attachment=True)
