"""
Gunicorn config — CEI API v2
Optimisé pour scalabilité et sécurité.

Tous les paramètres dépendant du matériel (workers, threads, bind, pool DB
dans models.py) sont pilotés par variables d'environnement — la formule
ci-dessous n'est qu'un défaut, pas une valeur figée : le nombre réel de
workers/threads dépend du serveur (ex. production = 10 vCPU → 21 workers,
voir .env de chaque serveur, PAS ce commentaire).

Ce fichier sert aussi à l'instance dédiée cei-api-v2-notif (même app, port
séparé) qui isole /api/notifications/poll du reste du trafic — voir
GUNICORN_BIND ci-dessous.
"""
import multiprocessing
import os

# ── Binding ───────────────────────────────────────────────────────────────────
# Permet de démarrer une seconde instance (notifications) sur un bind séparé
# sans dupliquer ce fichier — cf. GUNICORN_BIND dans l'unit systemd dédiée.
bind    = os.getenv('GUNICORN_BIND', "unix:/run/cei-api-v2.sock")
backlog = 2048

# ── Workers ───────────────────────────────────────────────────────────────────
_default_workers = (multiprocessing.cpu_count() * 2) + 1
# Permet de surcharger via `GUNICORN_WORKERS` (défaut : formule ci-dessus)
workers = int(os.getenv('GUNICORN_WORKERS', str(_default_workers)))
worker_class    = "gthread"
threads         = int(os.getenv('GUNICORN_THREADS', '4'))
worker_connections = 1000

# ── Timeouts ──────────────────────────────────────────────────────────────────
# 600 s pour les routes IA (analyse PDF jusqu'à 50 Mo + appel LLM)
timeout           = 600
keepalive         = 5
graceful_timeout  = 30

# ── Logging ───────────────────────────────────────────────────────────────────
# Env-configurables pour permettre une seconde instance (notifications) avec
# ses propres fichiers, sans jamais écraser ceux de l'instance principale.
accesslog      = os.getenv('GUNICORN_ACCESSLOG', "/var/log/cei-api-v2/access.log")
errorlog       = os.getenv('GUNICORN_ERRORLOG',  "/var/log/cei-api-v2/error.log")
capture_output = True
loglevel       = "info"
access_log_format = '%(h)s %(l)s %(u)s %(t)s "%(r)s" %(s)s %(b)s %(D)sµs'

# ── PID ───────────────────────────────────────────────────────────────────────
pidfile = os.getenv('GUNICORN_PIDFILE', "/run/cei-api-v2.pid")

# ── Sécurité ──────────────────────────────────────────────────────────────────
limit_request_line        = 4094
limit_request_fields      = 100
limit_request_field_size  = 8190

# ── Performance ───────────────────────────────────────────────────────────────
preload_app         = True   # copy-on-write → économie mémoire
# 1000 était beaucoup trop bas sous charge réelle (~500 req/s) : recyclage
# de workers toutes les ~30s, observé comme cause directe d'erreurs EOF
# pendant les tests de charge (17 août 2026).
max_requests        = int(os.getenv('GUNICORN_MAX_REQUESTS', '20000'))
max_requests_jitter = int(os.getenv('GUNICORN_MAX_REQUESTS_JITTER', '2000'))

# ── Hooks : gestion du pool SQLAlchemy après fork ─────────────────────────────
def post_fork(server, worker):
    """
    Dispose le pool SQLAlchemy hérité du processus maître.
    Sans ce hook, plusieurs workers partageraient les mêmes connexions PostgreSQL
    (connexion réseau TCP non partageable entre processus) → corruptions silencieuses.
    Chaque worker recrée son propre pool au premier accès DB.
    """
    try:
        from models import engine
        engine.dispose()
    except Exception:
        pass

def worker_exit(server, worker):
    """Libérer proprement les connexions DB quand un worker s'arrête."""
    try:
        from models import engine
        engine.dispose()
    except Exception:
        pass
