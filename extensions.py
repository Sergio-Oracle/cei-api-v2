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

# La clé est l'IP (get_remote_address) — un centre d'examen entier partage
# souvent UNE SEULE IP publique (NAT campus/salle). L'ancien défaut
# (300/heure, 60/min) correspondait à peine à UN SEUL étudiant actif pendant
# un examen (mesuré ~11 req/min/étudiant lors des tests de charge du 20-21/08
# — voir project_cei_capacity_3000_students) : dès qu'une trentaine
# d'étudiants composaient derrière la même IP, la limite se déclenchait sur
# du trafic parfaitement légitime, pas sur un abus. Les tests de charge ont
# par ailleurs montré que le serveur lui-même encaisse largement plus que ça
# (~310 req/s soutenues, ~0% d'erreurs réelles) — la limite basse était donc
# le vrai goulot, pas la capacité serveur. Relevé à un seuil qui couvre
# confortablement un grand centre d'examen partagé tout en gardant un
# garde-fou contre un client réellement en boucle/abusif. Surchargeable sans
# redéploiement via les variables d'environnement ci-dessous si besoin
# d'ajuster à chaud.
_LIMIT_PER_HOUR   = os.getenv('RATE_LIMIT_PER_HOUR',   '20000')
_LIMIT_PER_MINUTE = os.getenv('RATE_LIMIT_PER_MINUTE', '2000')

limiter = Limiter(
    key_func=get_remote_address,
    storage_uri=_LIMITER_STORAGE,
    default_limits=[f"{_LIMIT_PER_HOUR} per hour", f"{_LIMIT_PER_MINUTE} per minute"],
    headers_enabled=True,               # X-RateLimit-* dans les réponses
    swallow_errors=True,                # si Redis tombe, ne pas bloquer les requêtes
)
