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
import resource

# 19/11/2026 — Limite de descripteurs de fichiers : la limite SOUPLE du service était de 1 024 alors que la limite maximale autorisée est de 524 288. Chaque connexion ouverte, chaque
# connexion à la base et chaque attente de données consomment un descripteur : au test de charge de 5 000 étudiants, « OSError: [Errno 24] Too many open files » a produit des erreurs 500
# et 502. On relève la limite souple jusqu'à 65 535 (sans droits administrateur : elle ne dépasse jamais la limite maximale), avant la création des workers qui l'héritent.
try:
    _soft, _hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    _want = 65535 if _hard == resource.RLIM_INFINITY else min(65535, _hard)
    if _soft < _want:
        resource.setrlimit(resource.RLIMIT_NOFILE, (_want, _hard))
except Exception:
    pass

# ── Binding ───────────────────────────────────────────────────────────────────
# Permet de démarrer une seconde instance (notifications) sur un bind séparé
# sans dupliquer ce fichier — cf. GUNICORN_BIND dans l'unit systemd dédiée.
bind    = os.getenv('GUNICORN_BIND', "unix:/run/cei-api-v2.sock")
backlog = 4096

# ── Workers ───────────────────────────────────────────────────────────────────
_default_workers = (multiprocessing.cpu_count() * 2) + 1
# Permet de surcharger via `GUNICORN_WORKERS` (défaut : formule ci-dessus)
workers = int(os.getenv('GUNICORN_WORKERS', str(_default_workers)))
worker_class    = "gthread"
threads         = int(os.getenv('GUNICORN_THREADS', '4'))
# 19/11/2026 — NOMBRE DE CONNEXIONS GARDÉES PAR WORKER. Avec 1 000, chaque worker acceptait tout ce qui arrivait et le gardait en file pour ses 4 fils, pendant que d'autres workers avaient
# des fils libres : mesuré à 10 000 étudiants, jusqu'à 800 connexions sur UN worker (moyenne 146), soit ≈ 8 s d'attente alors que le traitement d'une requête dure 40 ms.
# Un worker qui atteint cette limite cesse d'accepter ; la connexion suivante reste dans la file du noyau (backlog) et revient au premier worker libre (équitable, premier arrivé premier servi).
# Pour le service principal (4 fils) : 2 × fils = 8 (4 en cours + 4 d'avance) ; le service de notifications (100 fils, connexions longues) garde 1 000. Surcharge : GUNICORN_WORKER_CONNECTIONS.
worker_connections = int(os.getenv('GUNICORN_WORKER_CONNECTIONS', '0')) or (1000 if threads >= 50 else max(8, threads * 2))

# ── Timeouts ──────────────────────────────────────────────────────────────────
# 600 s pour les routes IA (analyse PDF jusqu'à 50 Mo + appel LLM)
timeout           = 600
# Pas de connexion persistante pour le service principal : une connexion inactive garderait un des emplacements ci-dessus (et la requête suivante resterait attachée à CE worker, occupé ou non).
# Une connexion locale (nginx → API, boucle locale) coûte quelques dizaines de microsecondes. Le service de notifications (threads ≥ 50) garde 5 s. Surcharge : GUNICORN_KEEPALIVE.
keepalive         = int(os.getenv('GUNICORN_KEEPALIVE', '5' if threads >= 50 else '0'))
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
# Un fichier PID resté d'avant un redémarrage du serveur désigne souvent un
# processus étranger (PID réutilisé, p. ex. un worker nginx) : gunicorn refuse
# alors de démarrer (« Already running on PID … ») et systemd relance en boucle.
# Observé le 03/11/2026 : service des notifications en échec ~1 900 fois après
# le redémarrage du serveur (notifications du tableau de bord coupées).
def _purge_stale_pidfile(path):
    try:
        with open(path) as f:
            pid = int(f.read().strip())
    except (OSError, ValueError):
        return
    try:
        with open(f'/proc/{pid}/cmdline', 'rb') as f:
            if b'gunicorn' in f.read():
                return  # vrai gunicorn : on ne touche à rien
    except OSError:
        pass  # processus disparu : fichier périmé
    try:
        os.remove(path)
    except OSError:
        pass

_purge_stale_pidfile(os.getenv('GUNICORN_PIDFILE', "/run/cei-api-v2.pid"))
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
