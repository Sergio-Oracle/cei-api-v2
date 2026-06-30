"""
Gunicorn config — CEI API v2
Optimisé pour 1000+ utilisateurs simultanés.

Formule workers : (2 × CPU_cores) + 1
Sur ce serveur (4 vCPU) → 9 workers
Chaque worker gère ~100-150 requêtes simultanées avec le timeout de 120s
→ capacité théorique : 9 × 150 = 1350 connexions simultanées
"""
import multiprocessing
import os

# ── Binding ──────────────────────────────────────────────────────────────────
bind    = "0.0.0.0:8100"
backlog = 2048              # file d'attente connexions entrantes

# ── Workers ──────────────────────────────────────────────────────────────────
workers         = (multiprocessing.cpu_count() * 2) + 1
worker_class    = "gthread"   # threads = meilleur pour I/O (DB, IA, fichiers)
threads         = 4           # 4 threads par worker → 9×4 = 36 threads total
worker_connections = 1000

# ── Timeouts ─────────────────────────────────────────────────────────────────
timeout           = 600    # requêtes IA peuvent prendre jusqu'à 8-10 min (analyse PDF 50Mo)
keepalive         = 5
graceful_timeout  = 30

# ── Logging ──────────────────────────────────────────────────────────────────
accesslog      = "/var/log/cei-api-v2/access.log"
errorlog       = "/var/log/cei-api-v2/error.log"
capture_output = True   # redirige print()/stdout vers errorlog
loglevel       = "info"
access_log_format = '%(h)s %(l)s %(u)s %(t)s "%(r)s" %(s)s %(b)s %(D)sµs'

# ── PID ──────────────────────────────────────────────────────────────────────
pidfile  = "/run/cei-api-v2.pid"

# ── Sécurité ─────────────────────────────────────────────────────────────────
limit_request_line   = 4094
limit_request_fields = 100
limit_request_field_size = 8190

# ── Performance ──────────────────────────────────────────────────────────────
preload_app  = True     # charger l'app avant le fork → économise mémoire (copy-on-write)
max_requests = 1000     # redémarrer les workers après 1000 requêtes (évite les memory leaks)
max_requests_jitter = 100
