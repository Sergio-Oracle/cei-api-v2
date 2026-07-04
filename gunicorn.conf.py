"""
Gunicorn config — CEI API v2
Optimisé pour scalabilité et sécurité.

Serveur : 4 vCPU / 8 GB RAM
Formule workers : (2 × CPU) + 1 = 9 workers gthread
Threads par worker : 4  →  36 threads simultanés
Pool SQLAlchemy : 3 persistantes + 7 burst = 10/worker × 9 = 90 conns max
PostgreSQL max_connections : 100  →  marge de 10 pour admin/monitoring
"""
import multiprocessing
import os

# ── Binding ───────────────────────────────────────────────────────────────────
bind    = "unix:/run/cei-api-v2.sock"
backlog = 2048

# ── Workers ───────────────────────────────────────────────────────────────────
workers         = (multiprocessing.cpu_count() * 2) + 1
worker_class    = "gthread"
threads         = 4
worker_connections = 1000

# ── Timeouts ──────────────────────────────────────────────────────────────────
# 600 s pour les routes IA (analyse PDF jusqu'à 50 Mo + appel LLM)
timeout           = 600
keepalive         = 5
graceful_timeout  = 30

# ── Logging ───────────────────────────────────────────────────────────────────
accesslog      = "/var/log/cei-api-v2/access.log"
errorlog       = "/var/log/cei-api-v2/error.log"
capture_output = True
loglevel       = "info"
access_log_format = '%(h)s %(l)s %(u)s %(t)s "%(r)s" %(s)s %(b)s %(D)sµs'

# ── PID ───────────────────────────────────────────────────────────────────────
pidfile = "/run/cei-api-v2.pid"

# ── Sécurité ──────────────────────────────────────────────────────────────────
limit_request_line        = 4094
limit_request_fields      = 100
limit_request_field_size  = 8190

# ── Performance ───────────────────────────────────────────────────────────────
preload_app         = True   # copy-on-write → économie mémoire
max_requests        = 1000   # redémarrage périodique pour éviter les memory leaks
max_requests_jitter = 100    # décalage aléatoire pour éviter les redémarrages simultanés

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
