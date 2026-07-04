"""
Application Flask — CEI API v2.
Point d'entrée : création de l'app, blueprints, middlewares sécurité/cache/logging.
"""
import logging
import os
import time
import uuid

from flask import Flask, g, jsonify, request, send_file
from flask_cors import CORS
from flask_compress import Compress
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(override=True)

# ── FAIL FAST — variables critiques obligatoires ──────────────────────────────
_REQUIRED_ENVS = ['SECRET_KEY', 'DATABASE_URL', 'PASETO_PRIVATE_KEY', 'PASETO_PUBLIC_KEY']
_missing = [v for v in _REQUIRED_ENVS if not os.getenv(v)]
if _missing:
    raise RuntimeError(
        f"Variables d'environnement manquantes — l'application ne peut pas démarrer : {', '.join(_missing)}"
    )

# ── Logging structuré ─────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s [%(name)s] %(message)s',
    datefmt='%Y-%m-%dT%H:%M:%S',
)
logger = logging.getLogger('cei.api')

# ── Application Flask ─────────────────────────────────────────────────────────
app = Flask(__name__)

# CORS — origines lues depuis ALLOWED_ORIGINS dans .env
_raw_origins = os.getenv('ALLOWED_ORIGINS', 'https://dev-cei.ddns.net,http://localhost:5173,http://localhost:3000')
_FRONTEND_ORIGINS = [o.strip() for o in _raw_origins.split(',') if o.strip()]
CORS(app,
     resources={r"/api/*": {"origins": _FRONTEND_ORIGINS}},
     supports_credentials=True)

# ── Configuration ─────────────────────────────────────────────────────────────
app.config['SECRET_KEY']          = os.getenv('SECRET_KEY', 'dev-secret-key-change-in-production')
app.config['MAX_CONTENT_LENGTH']  = int(os.getenv('MAX_FILE_SIZE', 50 * 1024 * 1024))
app.config['UPLOAD_FOLDER']       = os.getenv('UPLOAD_FOLDER', 'static/uploads')
app.config['COMPRESS_REGISTER']   = True
app.config['COMPRESS_LEVEL']      = 6
app.config['COMPRESS_MIN_SIZE']   = 500
Compress(app)

# ── PASETO ────────────────────────────────────────────────────────────────────
from auth_paseto import init_paseto
init_paseto()

# ── Extensions partagées (bcrypt + rate limiter Redis) ────────────────────────
from extensions import bcrypt as _bcrypt_ext, limiter as _limiter_ext
_bcrypt_ext.init_app(app)
_limiter_ext.init_app(app)

# ── Service IA ────────────────────────────────────────────────────────────────
from services.ai_service import init_ai_clients
init_ai_clients()

# ── Dossiers requis ───────────────────────────────────────────────────────────
Path(app.config['UPLOAD_FOLDER']).mkdir(exist_ok=True, parents=True)
Path('exports').mkdir(exist_ok=True)

# ── Base de données ───────────────────────────────────────────────────────────
from models import init_db
try:
    init_db()
    logger.info('Base de données initialisée')
except Exception as exc:
    logger.warning('Initialisation DB partielle : %s', exc)

# ── Blueprints ────────────────────────────────────────────────────────────────
from proctoring_routes import proctoring_bp
from swagger_docs      import swagger_bp
app.register_blueprint(proctoring_bp)
app.register_blueprint(swagger_bp)

from routes.notifications  import notifications_bp;   app.register_blueprint(notifications_bp)
from routes.auth           import auth_bp;            app.register_blueprint(auth_bp)
from routes.formations     import formations_bp;      app.register_blueprint(formations_bp)
from routes.admin_users    import admin_users_bp;     app.register_blueprint(admin_users_bp)
from routes.subjects       import subjects_bp;        app.register_blueprint(subjects_bp)
from routes.papers         import papers_bp;          app.register_blueprint(papers_bp)
from routes.statistics     import statistics_bp;      app.register_blueprint(statistics_bp)
from routes.reclamations   import reclamations_bp;    app.register_blueprint(reclamations_bp)
from routes.professor      import professor_bp;       app.register_blueprint(professor_bp)
from routes.question_bank  import question_bank_bp;   app.register_blueprint(question_bank_bp)
from routes.exams          import exams_bp;           app.register_blueprint(exams_bp)
from routes.transcripts    import transcripts_bp;     app.register_blueprint(transcripts_bp)

from export_route      import register_export_route
from csv_import_routes import register_csv_routes
register_export_route(app)
register_csv_routes(app)

# ── Middleware : request-ID + timing ─────────────────────────────────────────
@app.before_request
def _before_request():
    g.t0         = time.monotonic()
    g.request_id = request.headers.get('X-Request-ID', uuid.uuid4().hex[:8])

@app.after_request
def _after_request(response):
    rid = g.get('request_id', '-')
    response.headers['X-Request-ID'] = rid

    # Log toutes les requêtes API sauf les assets statiques
    if not request.path.startswith('/static/') and request.path not in ('/favicon.ico',):
        ms = int((time.monotonic() - g.get('t0', time.monotonic())) * 1000)
        level = logging.WARNING if response.status_code >= 500 else logging.INFO
        logger.log(level, '[%s] %s %s → %d  %dms',
                   rid, request.method, request.path, response.status_code, ms)
    return response

# ── Sécurité : headers HTTP + cache ──────────────────────────────────────────
@app.after_request
def _security_headers(response):
    path = request.path
    response.headers.setdefault('X-Content-Type-Options', 'nosniff')
    response.headers.setdefault('X-Frame-Options', 'DENY')
    response.headers.setdefault('Referrer-Policy', 'strict-origin-when-cross-origin')
    response.headers.setdefault('X-XSS-Protection', '1; mode=block')
    # Les pages docs Swagger/ReDoc chargent swagger-ui-dist depuis jsdelivr.net (CDN)
    if path.startswith('/api/docs'):
        csp = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' 'unsafe-eval' https://cdn.jsdelivr.net; "
            "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://fonts.googleapis.com; "
            "font-src 'self' data: https://fonts.gstatic.com; "
            "img-src 'self' data: blob:; "
            "connect-src 'self'; "
            "frame-ancestors 'none'"
        )
    else:
        csp = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' 'unsafe-eval'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: blob:; "
            "font-src 'self' data:; "
            "connect-src 'self'; "
            "frame-ancestors 'none'"
        )
    response.headers.setdefault('Content-Security-Policy', csp)
    response.headers.setdefault(
        'Permissions-Policy',
        'camera=(self), microphone=(self), geolocation=(), payment=()'
    )
    if request.is_secure or request.headers.get('X-Forwarded-Proto') == 'https':
        response.headers.setdefault(
            'Strict-Transport-Security',
            'max-age=63072000; includeSubDomains'
        )
    if path.startswith('/static/'):
        response.headers['Cache-Control'] = 'public, max-age=604800, immutable'
        return response
    if path.startswith('/api/') or response.content_type.startswith('text/html'):
        response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        response.headers['Pragma']        = 'no-cache'
    return response

# ── Gestionnaire d'erreurs global ─────────────────────────────────────────────
@app.errorhandler(404)
def _not_found(_):
    return jsonify({'error': 'Ressource introuvable'}), 404

@app.errorhandler(405)
def _method_not_allowed(_):
    return jsonify({'error': 'Méthode HTTP non autorisée'}), 405

@app.errorhandler(413)
def _too_large(_):
    max_mb = app.config['MAX_CONTENT_LENGTH'] // (1024 * 1024)
    return jsonify({'error': f'Fichier trop volumineux (max {max_mb} Mo)'}), 413

@app.errorhandler(429)
def _rate_limited(e):
    return jsonify({'error': 'Trop de tentatives. Réessayez dans quelques instants.'}), 429

@app.errorhandler(500)
def _server_error(e):
    logger.error('Erreur interne [%s]: %s', g.get('request_id', '-'), e)
    return jsonify({'error': 'Erreur interne du serveur'}), 500

# ── Health check (sans authentification — pour load balancer / monitoring) ────
@app.route('/api/health')
@_limiter_ext.exempt
def health_check():
    from sqlalchemy import text
    checks = {}
    all_ok = True

    try:
        from models import engine
        with engine.connect() as conn:
            conn.execute(text('SELECT 1'))
        checks['database'] = 'ok'
    except Exception as exc:
        checks['database'] = 'error'
        all_ok = False
        logger.error('Health DB: %s', exc)

    try:
        from cache import _get_client
        client = _get_client()
        if client:
            client.ping()
            checks['redis'] = 'ok'
        else:
            checks['redis'] = 'unavailable'
    except Exception:
        checks['redis'] = 'unavailable'

    http_status = 200 if all_ok else 503
    return jsonify({
        'status': 'ok' if all_ok else 'degraded',
        'checks': checks,
    }), http_status

# ── Favicon ───────────────────────────────────────────────────────────────────
@app.route('/favicon.ico')
def favicon():
    return send_file('static/favicon.svg', mimetype='image/svg+xml')


if __name__ == '__main__':
    debug_mode = os.getenv('FLASK_DEBUG', 'False').lower() in ('true', '1')
    app.run(debug=debug_mode, host='0.0.0.0', port=7000)
