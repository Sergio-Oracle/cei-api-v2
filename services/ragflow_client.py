"""
Moteur RAG (RAGFlow) relié à CEI depuis la page d'administration « Moteur RAG ».

Un moteur = une instance RAGFlow (adresse + clé API). La clé n'est jamais
stockée en clair ni renvoyée par l'API : chiffrée (Fernet, clé dérivée de
SECRET_KEY, comme les tokens Moodle), seuls ses 4 derniers caractères sont
affichés. Un seul moteur est « en service » à la fois : changer de serveur =
ajouter le nouveau (connexion vérifiée), puis le mettre en service, ce qui
retire l'ancien dans la même transaction — pas de période sans moteur.

API RAGFlow utilisées (v0.27) :
  GET  /api/v1/system/healthz                  état des composants (sans clé)
  GET  /api/v1/datasets                        validation de la clé + volumes
  GET  /api/v1/models, /api/v1/models/default  modèles disponibles / par défaut
  GET  /api/v1/datasets/<id>/documents         documents (filtre run=FAIL…)
  POST /api/v1/datasets/<id>/documents/parse   (ré)indexation
"""
import base64
import hashlib
import os
from urllib.parse import urlparse

import requests
from cryptography.fernet import Fernet, InvalidToken

TIMEOUT = 15
PAGE_SIZE = 100


class RagflowError(Exception):
    pass


# ── Clé API chiffrée en base ─────────────────────────────────────────────────

def _fernet() -> Fernet:
    secret = os.getenv('SECRET_KEY')
    if not secret:
        raise RuntimeError('[ragflow] SECRET_KEY manquante — impossible de chiffrer la clé RAGFlow')
    digest = hashlib.sha256(f'cei-ragflow-key:{secret}'.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_key(key: str) -> str:
    return _fernet().encrypt(key.encode()).decode()


def decrypt_key(encrypted: str) -> str:
    try:
        return _fernet().decrypt(encrypted.encode()).decode()
    except InvalidToken as e:
        raise RagflowError('Clé illisible (SECRET_KEY modifiée ?) — ressaisir la clé de ce moteur') from e


def normalize_base_url(url: str) -> str:
    url = (url or '').strip().rstrip('/')
    if url.endswith('/api/v1'):
        url = url[:-len('/api/v1')]
    parsed = urlparse(url)
    if parsed.scheme not in ('https', 'http') or not parsed.netloc:
        raise ValueError("Adresse invalide (attendu : http(s)://hôte:port, ex. http://127.0.0.1:19380)")
    return url


# ── Client ───────────────────────────────────────────────────────────────────

class RagflowClient:
    def __init__(self, base_url: str, api_key: str):
        self.base_url = normalize_base_url(base_url)
        self.api_key = api_key

    def _request(self, method, path, auth=True, **kw):
        headers = {'Authorization': f'Bearer {self.api_key}'} if auth else {}
        try:
            r = requests.request(method, f'{self.base_url}/api/v1{path}', headers=headers,
                                 timeout=kw.pop('timeout', TIMEOUT), **kw)
        except requests.RequestException as e:
            raise RagflowError(f'Moteur injoignable : {e.__class__.__name__}') from e
        try:
            body = r.json()
        except ValueError:
            raise RagflowError(f'Réponse inattendue du moteur (HTTP {r.status_code}) — est-ce bien RAGFlow ?')
        return r.status_code, body

    def _call(self, method, path, **kw):
        status, body = self._request(method, path, **kw)
        # RAGFlow répond 200 avec code != 0 en cas d'erreur applicative.
        if not isinstance(body, dict) or body.get('code') != 0:
            msg = body.get('message') if isinstance(body, dict) else None
            if status in (401, 403) or (isinstance(body, dict) and body.get('code') in (109, 401)):
                raise RagflowError('Clé API refusée par le moteur')
            raise RagflowError(msg or f'Erreur du moteur (HTTP {status})')
        return body

    def health(self) -> dict:
        _, body = self._request('GET', '/system/healthz', auth=False)
        return body if isinstance(body, dict) else {}

    def datasets(self) -> list:
        out, page = [], 1
        while True:
            body = self._call('GET', '/datasets', params={'page': page, 'page_size': PAGE_SIZE})
            batch = body.get('data') or []
            out.extend(batch)
            if len(batch) < PAGE_SIZE:
                return out
            page += 1

    def models(self) -> list:
        return self._call('GET', '/models').get('data') or []

    def default_models(self) -> list:
        return (self._call('GET', '/models/default').get('data') or {}).get('models') or []

    def documents(self, dataset_id: str, run=None) -> list:
        out, page = [], 1
        params = {'page_size': PAGE_SIZE}
        if run:
            params['run'] = run
        while True:
            params['page'] = page
            body = self._call('GET', f'/datasets/{dataset_id}/documents', params=params)
            batch = (body.get('data') or {}).get('docs') or []
            out.extend(batch)
            if len(batch) < PAGE_SIZE:
                return out
            page += 1

    def parse(self, dataset_id: str, document_ids: list) -> None:
        self._call('POST', f'/datasets/{dataset_id}/documents/parse',
                   json={'document_ids': document_ids}, timeout=60)

    # ── Vues pour la page d'administration ──────────────────────────────────

    def diagnose(self) -> dict:
        """Connexion + clé + composants + modèles. Lève RagflowError si le
        moteur est injoignable ou la clé refusée ; renvoie la liste précise
        des problèmes restants sinon (ex. aucun modèle d'embedding)."""
        health = self.health()
        datasets = self.datasets()        # valide la clé
        models = self.models()
        defaults = self.default_models()
        components = {k: v for k, v in health.items() if k != 'status'}
        problems = [f'Composant « {k} » en défaut' for k, v in components.items() if v != 'ok']
        embeddings = [m for m in models if 'embedding' in (m.get('model_type') or [])]
        default_embd = next((d for d in defaults if d.get('model_type') == 'embedding'), None)
        if not embeddings:
            problems.append("Aucun modèle d'embedding disponible : l'indexation des documents est impossible")
        elif not default_embd:
            problems.append("Aucun modèle d'embedding par défaut défini dans RAGFlow")
        return {
            'ok': not problems,
            'problems': problems,
            'components': components,
            'models': [{'name': m.get('name'), 'provider': m.get('provider_name'),
                        'types': m.get('model_type') or []} for m in models],
            'default_models': [{'type': d.get('model_type'), 'name': d.get('model_name'),
                                'provider': d.get('model_provider')} for d in defaults],
            'datasets': len(datasets),
            'documents': sum(d.get('document_count') or 0 for d in datasets),
            'chunks': sum(d.get('chunk_count') or 0 for d in datasets),
        }

    def index_status(self) -> list:
        """Par dataset : documents, fragments, en cours, en échec."""
        rows = []
        for d in self.datasets():
            running = self.documents(d['id'], run=['RUNNING', 'SCHEDULE'])
            failed = self.documents(d['id'], run=['FAIL'])
            rows.append({
                'id': d['id'], 'name': d.get('name'),
                'documents': d.get('document_count') or 0, 'chunks': d.get('chunk_count') or 0,
                'embedding_model': d.get('embedding_model'),
                'running': len(running), 'failed': len(failed),
                'failed_docs': [{'id': f['id'], 'name': f.get('name'),
                                 'error': (f.get('progress_msg') or '')[-300:]} for f in failed[:20]],
            })
        return rows

    def reindex(self, scope: str = 'failed') -> dict:
        """scope='failed' : documents en échec, annulés ou jamais indexés.
        scope='all' : tout ré-indexer (long : chaque fragment est recalculé)."""
        runs = None if scope == 'all' else ['FAIL', 'CANCEL', 'UNSTART']
        launched, datasets = 0, 0
        for d in self.datasets():
            ids = [doc['id'] for doc in self.documents(d['id'], run=runs)
                   if doc.get('run') not in ('RUNNING', 'SCHEDULE')]
            for i in range(0, len(ids), PAGE_SIZE):
                self.parse(d['id'], ids[i:i + PAGE_SIZE])
            if ids:
                launched += len(ids)
                datasets += 1
        return {'documents': launched, 'datasets': datasets}


def client_for(engine) -> RagflowClient:
    return RagflowClient(engine.base_url, decrypt_key(engine.api_key_encrypted))


def active_engine(session):
    """Moteur en service, ou None (RAG désactivé). Point d'entrée des futures
    fonctions RAG (indexation des documents Moodle, génération ancrée)."""
    from models import RagEngine
    return session.query(RagEngine).filter_by(is_active=True).first()
