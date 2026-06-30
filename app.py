"""
Application Flask — Système de Notation Avancé.

Ce fichier est le point d'entrée de l'application. Toutes les routes API
sont définies dans routes/ (blueprints MVC). Ce fichier ne contient que :
  - La création et configuration de l'app Flask
  - L'enregistrement des blueprints
  - Les routes de pages statiques (HTML)
  - Le hook @after_request (cache / sécurité)
"""
from flask import Flask, request, send_file
from flask_cors import CORS
from flask_compress import Compress
from pathlib import Path
import os

from dotenv import load_dotenv

load_dotenv(override=True)

app = Flask(__name__)

_FRONTEND_ORIGINS = [
    "http://62.171.190.6:5173",
    "http://localhost:5173",
    "http://127.0.0.1:5173",
]
CORS(app,
     resources={r"/api/*": {"origins": _FRONTEND_ORIGINS}},
     supports_credentials=True)

# ── Configuration ─────────────────────────────────────────────────────────────
app.config['SECRET_KEY']           = os.getenv('SECRET_KEY', 'dev-secret-key-change-in-production')
app.config['MAX_CONTENT_LENGTH']   = int(os.getenv('MAX_FILE_SIZE', 50 * 1024 * 1024))
app.config['UPLOAD_FOLDER']        = os.getenv('UPLOAD_FOLDER', 'static/uploads')
# Compression gzip/brotli des réponses (réduit la bande passante de 60-80%)
app.config['COMPRESS_REGISTER'] = True
app.config['COMPRESS_LEVEL']    = 6
app.config['COMPRESS_MIN_SIZE'] = 500
Compress(app)

# ── Initialisation PASETO ─────────────────────────────────────────────────────
from auth_paseto import init_paseto
init_paseto()

# ── Bcrypt (extensions.py — instance partagée par les blueprints) ─────────────
from extensions import bcrypt as _bcrypt_ext
_bcrypt_ext.init_app(app)

# ── Service IA (Anthropic → Gemini → DeepSeek → Ollama) ──────────────────────
from services.ai_service import init_ai_clients
init_ai_clients()

# ── Dossiers requis ───────────────────────────────────────────────────────────
Path(app.config['UPLOAD_FOLDER']).mkdir(exist_ok=True, parents=True)
Path('exports').mkdir(exist_ok=True)

# ── Base de données ───────────────────────────────────────────────────────────
from models import init_db
try:
    init_db()
    print("✅ Base de données initialisée")
except Exception as e:
    print(f"⚠️ Attention lors de l'initialisation de la base: {e}")

# ── Blueprints (MVC — Contrôleurs) ────────────────────────────────────────────
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

# Routes export PDF et CSV (registrées via fonctions utilitaires)
from export_route    import register_export_route
from csv_import_routes import register_csv_routes
register_export_route(app)
register_csv_routes(app)

# ── En-têtes de sécurité et cache ─────────────────────────────────────────────
@app.after_request
def add_cache_headers(response):
    path = request.path
    response.headers.setdefault('X-Content-Type-Options', 'nosniff')
    response.headers.setdefault('X-Frame-Options', 'DENY')
    response.headers.setdefault('Referrer-Policy', 'strict-origin-when-cross-origin')
    if path.startswith('/static/'):
        response.headers['Cache-Control'] = 'public, max-age=604800, immutable'
        return response
    if path.startswith('/api/') or response.content_type.startswith('text/html'):
        response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        response.headers['Pragma'] = 'no-cache'
    return response

@app.route('/favicon.ico')
def favicon():
    return send_file('static/favicon.svg', mimetype='image/svg+xml')


if __name__ == '__main__':
    debug_mode = os.getenv('FLASK_DEBUG', 'False').lower() in ('true', '1')
    app.run(debug=debug_mode, host='0.0.0.0', port=7000)
