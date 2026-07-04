"""
Extensions Flask partagées — pattern Factory.

Toutes les extensions sont instanciées ici SANS app, puis initialisées
via extension.init_app(app) dans app.py. Les blueprints importent
depuis ce module au lieu d'importer depuis app.py (évite les imports
circulaires).
"""
import os
from flask_bcrypt import Bcrypt
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

bcrypt = Bcrypt()

# Redis DB 1 réservé au rate limiting (DB 0 = cache applicatif)
# Fallback memory:// si Redis indisponible — l'app reste fonctionnelle mais
# chaque worker aura son compteur indépendant (dégradation acceptable)
_LIMITER_STORAGE = os.getenv('REDIS_LIMITER_URL', 'redis://127.0.0.1:6379/1')

limiter = Limiter(
    key_func=get_remote_address,
    storage_uri=_LIMITER_STORAGE,
    default_limits=["300 per hour", "60 per minute"],
    headers_enabled=True,               # X-RateLimit-* dans les réponses
    swallow_errors=True,                # si Redis tombe, ne pas bloquer les requêtes
)
