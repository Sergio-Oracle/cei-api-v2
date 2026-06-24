"""
Application Flask - Système de Notation Avancé COMPLET
Avec CRUD Maquette + Gestion erreurs
"""
from flask import Flask, render_template, request, jsonify, send_file, make_response
from flask_cors import CORS
from flask_compress import Compress
from datetime import datetime, timedelta, timezone
from csv_import_routes import register_csv_routes
# PASETO — remplacé flask_jwt_extended
from auth_paseto import (
    paseto_required, get_current_user_id, get_current_user_role,
    create_access_token, create_refresh_token, init_paseto,
    set_refresh_cookie, clear_refresh_cookie, get_refresh_token_from_cookie,
    hash_token
)
from auth_paseto import decode_token as paseto_decode_token
from flask_bcrypt import Bcrypt
import os
from pathlib import Path
from dotenv import load_dotenv
from anthropic import Anthropic
from google import genai as google_genai
from google.genai import types as genai_types
from werkzeug.utils import secure_filename
import PyPDF2
import docx
from sqlalchemy import func, desc, or_ as sa_or
from sqlalchemy.orm import joinedload
import io
import re
import statistics
import json
import threading

from models import (
    User, Subject, StudentPaper, Reclamation, CorrectionHistory,
    UserRole, ReclamationStatus,
    Formation, Semester, UE, EC, ECAssignment, StudentUEEnrollment,
    OnlineExam, ExamAttempt, ExamActivityLog, GradeTranscript, CameraLog,
    ExamStatus, AttemptStatus, ExamProctor, ProctorAssignment,
    QuestionBank, TokenBlocklist,
    get_session, init_db
)

from export_route import register_export_route
from utils import (
    send_account_created_email, send_paper_corrected_email,
    send_password_reset_email, send_password_changed_email,
    send_exam_started_email,
    extract_text_from_file,
    generate_pdf_report,
    generate_corrected_paper_pdf,
    generate_statistics_chart,
    allowed_file, calculate_file_hash, extract_student_name_from_content,
    match_student_by_name
)
from proctoring_routes import proctoring_bp
from swagger_docs import swagger_bp

# ✅ AJOUTEZ CETTE FONCTION ICI
def normalize_name(name):
    """Normaliser un nom pour créer un identifiant unique"""
    import unicodedata
    import re
    
    # Supprimer les accents
    name = unicodedata.normalize('NFKD', name)
    name = name.encode('ASCII', 'ignore').decode('ASCII')
    
    # Convertir en minuscules et remplacer espaces par points
    name = name.lower().strip()
    name = re.sub(r'[^\w\s-]', '', name)  # Supprimer caractères spéciaux
    name = re.sub(r'[\s]+', '.', name)     # Remplacer espaces par points
    
    return name

# Charger les variables d'environnement — override=True pour toujours prendre le .env
load_dotenv(override=True)

app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": "*"}})

# Configuration
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'dev-secret-key-change-in-production')
app.config['MAX_CONTENT_LENGTH'] = int(os.getenv('MAX_FILE_SIZE', 16 * 1024 * 1024))
app.config['UPLOAD_FOLDER'] = os.getenv('UPLOAD_FOLDER', 'static/uploads')
app.config['TEMPLATES_AUTO_RELOAD'] = True

# Compression gzip/brotli des réponses JSON et HTML (réduit la bande passante de 60-80%)
app.config['COMPRESS_REGISTER'] = True
app.config['COMPRESS_LEVEL'] = 6
app.config['COMPRESS_MIN_SIZE'] = 500
Compress(app)

init_paseto()  # PASETO v4 Ed25519
bcrypt = Bcrypt(app)
# Initialisation des clients IA
_anthropic_key = os.getenv("ANTHROPIC_API_KEY")
anthropic_client = Anthropic(api_key=_anthropic_key) if _anthropic_key else None

# Rotation des clés Gemini — toutes les clés GEMINI_API_KEY, GEMINI_API_KEY_2, etc.
GEMINI_MODEL = "models/gemini-2.5-flash"
_gemini_keys = [v for k, v in sorted(os.environ.items())
                if k == "GEMINI_API_KEY" or (k.startswith("GEMINI_API_KEY_") and v)]
_gemini_clients = [google_genai.Client(api_key=k) for k in _gemini_keys]
_gemini_index = 0  # index courant pour la rotation

def _next_gemini_client():
    """Retourne le prochain client Gemini disponible (round-robin)."""
    global _gemini_index
    if not _gemini_clients:
        return None
    client = _gemini_clients[_gemini_index % len(_gemini_clients)]
    _gemini_index = (_gemini_index + 1) % len(_gemini_clients)
    return client

# DeepSeek — fallback si Anthropic et Gemini indisponibles
_deepseek_key = os.getenv("DEEPSEEK_API_KEY")
DEEPSEEK_API_URL = "https://api.deepseek.com/chat/completions"
DEEPSEEK_MODEL = "deepseek-chat"

# Ollama — fallback local si tous les autres fournisseurs sont indisponibles
_ollama_url = os.getenv("OLLAMA_API_URL", "").rstrip("/")
_ollama_key = os.getenv("OLLAMA_API_KEY")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen3.6:latest")          # gros modèle — corrections précises
OLLAMA_MODEL_FAST = os.getenv("OLLAMA_MODEL_FAST", "gemma3:12b")    # modèle rapide — suggestions/tâches simples

if not anthropic_client and not _gemini_clients and not _deepseek_key and not _ollama_key:
    print("⚠️  AVERTISSEMENT: Aucune clé IA configurée (ANTHROPIC_API_KEY, GEMINI_API_KEY, DEEPSEEK_API_KEY ou OLLAMA_API_KEY)")
else:
    if _deepseek_key:
        print("✅ DeepSeek configuré comme fallback IA")
    if _ollama_key and _ollama_url:
        print(f"✅ Ollama configuré comme fallback IA ({OLLAMA_MODEL} @ {_ollama_url})")

# Enregistrement des blueprints
app.register_blueprint(proctoring_bp)
app.register_blueprint(swagger_bp)

# Gestion des erreurs PASETO — gérée directement dans le décorateur @paseto_required

Path(app.config['UPLOAD_FOLDER']).mkdir(exist_ok=True, parents=True)
Path('exports').mkdir(exist_ok=True)

try:
    init_db()
    print("✅ Base de données initialisée")
except Exception as e:
    print(f"⚠️ Attention lors de l'initialisation de la base: {e}")

def _call_anthropic(system_prompt: str, user_message: str, temperature: float, max_tokens: int = 8192) -> str:
    message = anthropic_client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=max_tokens,
        temperature=temperature,
        system=system_prompt,
        messages=[{"role": "user", "content": user_message}],
        timeout=90,
    )
    return message.content[0].text

def _call_gemini(system_prompt: str, user_message: str, temperature: float) -> str:
    """Essaie toutes les clés Gemini en rotation jusqu'au premier succès."""
    if not _gemini_clients:
        raise Exception("Aucune clé Gemini configurée")
    config = genai_types.GenerateContentConfig(
        system_instruction=system_prompt if system_prompt else None,
        temperature=temperature
    )
    last_error = None
    for _ in range(len(_gemini_clients)):
        gc = _next_gemini_client()
        try:
            response = gc.models.generate_content(
                model=GEMINI_MODEL, contents=user_message, config=config)
            return response.text
        except Exception as e:
            last_error = e
            print(f"⚠️  Clé Gemini en rotation: {e}")
    raise last_error

def _call_deepseek(system_prompt: str, user_message: str, temperature: float, max_tokens: int = 8192) -> str:
    """Appel DeepSeek via Tor SOCKS5 (proxy local 127.0.0.1:9050) pour contourner le blocage réseau."""
    if not _deepseek_key:
        raise Exception("Clé DeepSeek non configurée")
    import requests
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": user_message})
    # Route via Tor SOCKS5 — contourne le blocage géographique de l'université
    proxies = {"https": "socks5h://127.0.0.1:9050", "http": "socks5h://127.0.0.1:9050"}
    resp = requests.post(
        DEEPSEEK_API_URL,
        headers={"Authorization": f"Bearer {_deepseek_key}", "Content-Type": "application/json"},
        json={"model": DEEPSEEK_MODEL, "messages": messages,
              "temperature": temperature, "max_tokens": max_tokens, "stream": False},
        proxies=proxies,
        timeout=120
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]

def _call_ollama(system_prompt: str, user_message: str, temperature: float, max_tokens: int = 8192, fast: bool = False) -> str:
    """Appel Ollama (fromager.unchk.sn) — fallback final de la chaîne IA.
    fast=True utilise OLLAMA_MODEL_FAST (gemma3:12b) pour les tâches simples/rapides.
    fast=False utilise OLLAMA_MODEL (qwen3.6) pour les corrections précises.
    """
    if not _ollama_key or not _ollama_url:
        raise Exception("Ollama non configuré")
    import requests as _req
    import re as _re
    model = OLLAMA_MODEL_FAST if fast else OLLAMA_MODEL
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": user_message})
    resp = _req.post(
        f"{_ollama_url}/api/chat",
        headers={"Authorization": f"Bearer {_ollama_key}", "Content-Type": "application/json"},
        json={"model": model, "messages": messages, "stream": False,
              "think": False,
              "options": {"temperature": temperature, "num_predict": max_tokens}},
        timeout=180
    )
    resp.raise_for_status()
    content = resp.json()["message"]["content"]
    # Supprimer les blocs <think>...</think> que Qwen3 peut générer en mode reasoning
    content = _re.sub(r'<think>.*?</think>', '', content, flags=_re.DOTALL).strip()
    return content

def _build_correction_system_prompt(title: str = "", content_preview: str = "") -> str:
    """Construit un system prompt de correction universel.
    L'IA détecte elle-même le domaine à partir du titre et du contenu du sujet.
    Fonctionne pour n'importe quelle discipline sans liste figée de mots-clés.
    """
    context = ""
    if title:
        context += f"Titre de l'examen : {title}\n"
    if content_preview:
        preview = content_preview[:500].strip()
        context += f"Début du sujet : {preview}\n"

    return f"""Tu es un correcteur d'examen universitaire EXTRÊMEMENT rigoureux et polyvalent.

{f"CONTEXTE DE L'EXAMEN :{chr(10)}{context}" if context else ""}
ÉTAPE 1 — IDENTIFICATION DU DOMAINE :
Avant de corriger, identifie silencieusement la discipline de cet examen (ex: droit civil, médecine, mathématiques, informatique, histoire, philosophie, chimie, agronomie, architecture, littérature, langues, etc.) en lisant le sujet et le barème fournis.
Adopte immédiatement le niveau d'expertise d'un professeur spécialiste de ce domaine :
- Pour les sciences exactes : vérifie chaque calcul, formule, démonstration avec rigueur mathématique
- Pour le droit : cite les articles, principes juridiques et jurisprudences pertinents
- Pour la médecine/santé : applique les protocoles cliniques et la terminologie médicale exacte
- Pour les sciences humaines : évalue la rigueur argumentative, les références théoriques, la cohérence
- Pour les langues/littérature : évalue la syntaxe, le style, la richesse lexicale, l'analyse textuelle
- Pour toute autre discipline : applique les standards académiques propres à ce domaine

IMPORTANT : Tu DOIS terminer ta correction par une ligne contenant EXACTEMENT :
Note totale: XX.XX/20

Format de correction :
=== CORRECTION DÉTAILLÉE ===
[Évaluation question par question avec justification précise selon les critères du barème]

=== RÉSUMÉ ===
Points forts : [...]
Points à améliorer : [...]

Note totale: XX.XX/20
"""

def call_claude(system_prompt: str, user_message: str, temperature: float = 0.2) -> str:
    """Appel IA avec fallback automatique Anthropic → Gemini → DeepSeek → Ollama."""
    anthropic_err = None
    gemini_err = None
    deepseek_err = None

    if anthropic_client:
        try:
            return _call_anthropic(system_prompt, user_message, temperature)
        except Exception as e:
            anthropic_err = str(e)
            if 'credit balance' in anthropic_err.lower() or 'too low' in anthropic_err.lower():
                print("⚠️  Anthropic : crédits insuffisants — basculement sur Gemini")
            else:
                print(f"⚠️  Anthropic indisponible, basculement sur Gemini: {e}")

    if _gemini_clients:
        try:
            return _call_gemini(system_prompt, user_message, temperature)
        except Exception as e:
            gemini_err = str(e)
            if 'quota' in gemini_err.lower() or 'resource_exhausted' in gemini_err.lower():
                print("⚠️  Gemini : quota épuisé — basculement sur DeepSeek")
            else:
                print(f"⚠️  Gemini indisponible — basculement sur DeepSeek: {e}")

    if _deepseek_key:
        try:
            return _call_deepseek(system_prompt, user_message, temperature)
        except Exception as e:
            deepseek_err = str(e)
            print(f"⚠️  DeepSeek indisponible — basculement sur Ollama: {e}")

    if _ollama_key and _ollama_url:
        try:
            return _call_ollama(system_prompt, user_message, temperature)
        except Exception as e:
            print(f"⚠️  Ollama indisponible: {e}")

    # Message d'erreur précis selon la cause
    if 'credit balance' in (anthropic_err or '').lower():
        raise Exception("Crédits Anthropic épuisés. Rechargez sur console.anthropic.com")
    if 'quota' in (gemini_err or '').lower() or 'resource_exhausted' in (gemini_err or '').lower():
        raise Exception("Quota Gemini épuisé (free tier). Rechargez sur aistudio.google.com")
    raise Exception("Le service d'intelligence artificielle est temporairement indisponible. Veuillez réessayer.")

def call_ai_simple(prompt: str) -> str:
    """Appel IA simple (sans system prompt) avec fallback automatique Anthropic → Gemini → DeepSeek → Ollama."""
    if anthropic_client:
        try:
            message = anthropic_client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=4000,
                messages=[{"role": "user", "content": prompt}],
                timeout=90,
            )
            return message.content[0].text
        except Exception as e:
            print(f"⚠️  Anthropic indisponible, basculement sur Gemini: {e}")

    if _gemini_clients:
        try:
            return _call_gemini("", prompt, 0.2)
        except Exception as e:
            print(f"⚠️  Gemini indisponible, basculement sur DeepSeek: {e}")

    if _deepseek_key:
        try:
            return _call_deepseek("", prompt, 0.2)
        except Exception as e:
            print(f"⚠️  DeepSeek indisponible, basculement sur Ollama: {e}")

    if _ollama_key and _ollama_url:
        try:
            return _call_ollama("", prompt, 0.2, fast=True)
        except Exception as e:
            print(f"⚠️  Ollama indisponible: {e}")

    raise Exception("Le service d'intelligence artificielle est temporairement indisponible. Veuillez réessayer.")

def extract_score_from_correction(correction_text: str) -> float:
    """Extraire la note de la correction avec de multiples patterns"""
    patterns = [
        r'Note totale\s*:\s*(\d+\.?\d*)\s*/\s*20',
        r'Note totale\s*:\s*(\d+\.?\d*)\s*/\s*(\d+)',
        r'Score\s*:\s*(\d+\.?\d*)\s*/\s*20',
        r'Total\s*:\s*(\d+\.?\d*)\s*/\s*20',
        r'Note finale\s*:\s*(\d+\.?\d*)\s*/\s*20',
        r'Note\s*:\s*(\d+\.?\d*)\s*/\s*20',
        r'(\d+\.?\d*)\s*/\s*20\s*points?',
        r'(\d+\.?\d*)\s*sur\s*20',
    ]

    for pattern in patterns:
        match = re.search(pattern, correction_text, re.IGNORECASE)
        if match:
            score = float(match.group(1))
            if len(match.groups()) > 1 and match.group(2):
                total = float(match.group(2))
                score = (score / total) * 20
            return round(score, 2)

    return 0.0

def utcnow():
    """Helper pour datetime UTC compatible Python 3.12+"""
    return datetime.now(timezone.utc)


def _strip_bareme_from_content(content):
    """Retirer la section barème du contenu (pour les étudiants).
    Ne jamais couper les lignes de séparateurs qui servent de décoration de titre.
    """
    if not content:
        return content
    # Pattern 1 : ligne de séparateurs (═══...) immédiatement suivie de "barème"
    m = re.search(r'\n[═=─]{5,}[^\n]*\n[^\n]*[Bb]ar[eè]me', content)
    if m:
        return content[:m.start()].rstrip()
    # Pattern 2 : "Barème de Notation" comme entête de section
    m = re.search(r'\n\s*[Bb]ar[eè]me\s+de\s+[Nn]otation', content, re.IGNORECASE)
    if m:
        return content[:m.start()].rstrip()
    # Pattern 3 : "BARÈME" seul sur sa ligne (tout en majuscules)
    m = re.search(r'\nBAR[ÈE]ME\s*\n', content)
    if m:
        return content[:m.start()].rstrip()
    # Pas de barème trouvé → retourner le contenu intact
    return content

@app.after_request
def add_cache_headers(response):
    path = request.path
    # En-têtes de sécurité sur toutes les réponses (OWASP A05)
    response.headers.setdefault('X-Content-Type-Options', 'nosniff')
    response.headers.setdefault('X-Frame-Options', 'DENY')
    response.headers.setdefault('Referrer-Policy', 'strict-origin-when-cross-origin')
    # Fichiers statiques : cache 7 jours (JS, CSS, images, fonts)
    if path.startswith('/static/'):
        response.headers['Cache-Control'] = 'public, max-age=604800, immutable'
        return response
    # Routes API et pages HTML : pas de cache
    if path.startswith('/api/') or response.content_type.startswith('text/html'):
        response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        response.headers['Pragma'] = 'no-cache'
    return response

@app.route('/')
def landing():
    return render_template('landing.html')

@app.route('/favicon.ico')
def favicon():
    return send_file('static/favicon.svg', mimetype='image/svg+xml')

@app.route('/app')
def app_page():
    return render_template('index.html')

@app.route('/guide-enseignant')
def guide_teacher():
    return render_template('guide_teacher.html')

@app.route('/guide-etudiant')
def guide_student():
    return render_template('guide_student.html')

@app.route('/conditions')
def terms():
    return render_template('terms.html')

@app.route('/guide-surveillant')
def guide_surveillant():
    return render_template('guide_surveillant.html')


# ============================================================================
# ROUTES AUTHENTIFICATION
# ============================================================================

@app.route('/api/auth/register', methods=['POST'])
def register():
    try:
        data = request.json
        session = get_session()

        existing_user = session.query(User).filter_by(email=data['email']).first()
        if existing_user:
            session.close()
            return jsonify({'error': 'Cet email est déjà utilisé'}), 400

        hashed_password = bcrypt.generate_password_hash(data['password']).decode('utf-8')
        role = UserRole.STUDENT

        new_user = User(
            email=data['email'],
            password_hash=hashed_password,
            full_name=data['full_name'],
            role=role
        )

        session.add(new_user)
        session.commit()
        user_dict = new_user.to_dict()
        session.close()

        # Envoi email
        send_account_created_email(data['email'], data['full_name'], 'student', data['password'])

        return jsonify({'success': True, 'message': 'Inscription réussie', 'user': user_dict}), 201
    except Exception as e:
        print(f"❌ Erreur register: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/auth/login', methods=['POST'])
def login():
    try:
        data    = request.json or {}
        email   = (data.get('email') or '').strip().lower()
        password = data.get('password', '')
        session = get_session()

        user = session.query(User).filter_by(email=email).first()
        if not user or not bcrypt.check_password_hash(user.password_hash, password):
            session.close()
            return jsonify({'error': 'Email ou mot de passe incorrect'}), 401
        if not user.is_active:
            session.close()
            return jsonify({'error': 'Compte désactivé'}), 403

        user.last_login = utcnow()
        session.commit()

        access_token  = create_access_token(user.id, user.role.value, user.email)
        refresh_token = create_refresh_token(user.id)
        user_dict     = user.to_dict()
        session.close()

        resp = make_response(jsonify({
            'success': True,
            'access_token': access_token,
            'user': user_dict
        }))
        set_refresh_cookie(resp, refresh_token)
        return resp, 200
    except Exception as e:
        print(f"❌ Erreur login: {e}")
        try: session.rollback(); session.close()
        except: pass
        return jsonify({'error': str(e)}), 500


@app.route('/api/auth/refresh', methods=['POST'])
def refresh_token_endpoint():
    """Émet un nouvel access token via le refresh token (httpOnly cookie)."""
    token = get_refresh_token_from_cookie()
    if not token:
        return jsonify({'error': 'Refresh token manquant'}), 401
    session = get_session()
    try:
        # Vérifier révocation
        token_hash = hash_token(token)
        if session.query(TokenBlocklist).filter_by(token_hash=token_hash).first():
            session.close()
            return jsonify({'error': 'Token révoqué — reconnectez-vous'}), 401
        payload = paseto_decode_token(token)
        if payload.get('type') != 'refresh':
            raise ValueError("Ce n'est pas un refresh token")
        user = session.query(User).filter_by(id=int(payload['sub'])).first()
        if not user or not user.is_active:
            session.close()
            return jsonify({'error': 'Utilisateur introuvable'}), 401
        # Rotation : révoquer l'ancien refresh token
        block = TokenBlocklist(
            token_hash=token_hash,
            user_id=user.id,
            expires_at=datetime.fromisoformat(payload['exp'])
        )
        session.add(block)
        session.commit()
        new_access  = create_access_token(user.id, user.role.value, user.email)
        new_refresh = create_refresh_token(user.id)
        session.close()
        resp = make_response(jsonify({'access_token': new_access}))
        set_refresh_cookie(resp, new_refresh)
        return resp, 200
    except ValueError as e:
        session.close()
        return jsonify({'error': str(e)}), 401
    except Exception as e:
        session.close()
        return jsonify({'error': str(e)}), 500


@app.route('/api/auth/logout', methods=['POST'])
@paseto_required
def logout():
    """Révoque le refresh token et efface le cookie."""
    token = get_refresh_token_from_cookie()
    if token:
        session = get_session()
        try:
            payload = paseto_decode_token(token)
            block = TokenBlocklist(
                token_hash=hash_token(token),
                user_id=get_current_user_id(),
                expires_at=datetime.fromisoformat(payload['exp'])
            )
            session.add(block)
            session.commit()
        except Exception:
            pass
        finally:
            session.close()
    resp = make_response(jsonify({'success': True, 'message': 'Déconnecté'}))
    clear_refresh_cookie(resp)
    return resp, 200


@app.route('/api/auth/public-key', methods=['GET'])
def auth_public_key():
    """Clé publique Ed25519 — le frontend peut vérifier les tokens lui-même."""
    import os as _os
    return jsonify({
        'public_key': _os.environ.get('PASETO_PUBLIC_KEY', ''),
        'algorithm':  'Ed25519',
        'version':    'v4.public',
        'token_ttl_minutes': int(_os.getenv('PASETO_ACCESS_TTL_MIN', '15'))
    })

@app.route('/api/auth/me', methods=['GET'])
@paseto_required
def get_current_user():
    try:
        user_id = get_current_user_id()
        session = get_session()

        user = session.query(User).filter_by(id=user_id).first()
        if not user:
            session.close()
            return jsonify({'error': 'Utilisateur non trouvé'}), 404

        user_dict = user.to_dict()
        session.close()
        return jsonify(user_dict)
    except Exception as e:
        print(f"❌ Erreur get_current_user: {e}")
        return jsonify({'error': str(e)}), 500

# ============================================================================
# PROFIL UTILISATEUR — GET / PUT infos / PUT mot de passe
# ============================================================================

@app.route('/api/profile', methods=['PUT'])
@paseto_required
def update_profile():
    try:
        user_id = get_current_user_id()
        session = get_session()
        user = session.query(User).filter_by(id=user_id).first()
        if not user:
            session.close()
            return jsonify({'error': 'Utilisateur non trouvé'}), 404

        data = request.json or {}
        full_name = data.get('full_name', '').strip()
        email     = data.get('email', '').strip()

        if full_name:
            user.full_name = full_name
        if email and email != user.email:
            existing = session.query(User).filter_by(email=email).first()
            if existing:
                session.close()
                return jsonify({'error': 'Cet email est déjà utilisé par un autre compte'}), 400
            user.email = email

        session.commit()
        result = user.to_dict()
        session.close()
        return jsonify({'success': True, 'user': result})
    except Exception as e:
        print(f"❌ Erreur update_profile: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/profile/password', methods=['PUT'])
@paseto_required
def change_password():
    try:
        user_id = get_current_user_id()
        session = get_session()
        user = session.query(User).filter_by(id=user_id).first()
        if not user:
            session.close()
            return jsonify({'error': 'Utilisateur non trouvé'}), 404

        data = request.json or {}
        current_pw = data.get('current_password', '')
        new_pw     = data.get('new_password', '')
        confirm_pw = data.get('confirm_password', '')

        if not bcrypt.check_password_hash(user.password_hash, current_pw):
            session.close()
            return jsonify({'error': 'Mot de passe actuel incorrect'}), 400
        if len(new_pw) < 6:
            session.close()
            return jsonify({'error': 'Le nouveau mot de passe doit comporter au moins 6 caractères'}), 400
        if new_pw != confirm_pw:
            session.close()
            return jsonify({'error': 'Les mots de passe ne correspondent pas'}), 400

        user.password_hash = bcrypt.generate_password_hash(new_pw).decode('utf-8')
        session.commit()
        # Email de sécurité (asynchrone — pas bloquant)
        app_url   = os.getenv('APP_URL', 'https://cei.ec2lt.sn').rstrip('/')
        reset_url = f"{app_url}/app?action=forgot"
        try:
            if user.email:
                send_password_changed_email(user.email, user.full_name, reset_url)
        except Exception:
            pass
        session.close()
        return jsonify({'success': True, 'message': 'Mot de passe modifié avec succès'})
    except Exception as e:
        print(f"Erreur change_password: {e}")
        return jsonify({'error': str(e)}), 500

# ============================================================================
# MOT DE PASSE OUBLIÉ — RÉINITIALISATION PAR EMAIL
# ============================================================================

@app.route('/api/auth/forgot-password', methods=['POST'])
def forgot_password():
    """Réinitialisation mot de passe — envoie un lien tokenisé par email (valable 1h)."""
    try:
        import secrets as _secrets
        data = request.get_json() or {}
        email = (data.get('email') or '').strip().lower()
        if not email:
            return jsonify({'error': 'Email requis'}), 400

        session = get_session()
        user = session.query(User).filter_by(email=email).first()

        # Réponse générique même si l'email n'existe pas (évite l'énumération)
        if not user or not user.has_email:
            session.close()
            return jsonify({'success': True, 'masked_email': None, 'email_sent': False})

        token = _secrets.token_urlsafe(32)
        user.reset_token = token
        user.reset_token_expires = utcnow() + timedelta(hours=1)
        session.commit()

        from flask import request as _req
        app_url = os.getenv('APP_URL', _req.host_url.rstrip('/'))
        reset_link = f"{app_url}/app?reset_token={token}"

        email_sent = False
        try:
            email_sent = send_password_reset_email(user.email, user.full_name, reset_link)
        except Exception as mail_err:
            print(f"⚠️ Erreur envoi email reset: {mail_err}")

        # Masquer partiellement l'adresse email pour l'affichage
        parts = (user.email or '').split('@')
        masked = parts[0][:2] + '***@' + parts[1] if len(parts) == 2 and len(parts[0]) > 2 else user.email

        session.close()
        return jsonify({'success': True, 'masked_email': masked, 'email_sent': email_sent})
    except Exception as e:
        print(f"❌ Erreur forgot_password: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/auth/reset-password', methods=['POST'])
def reset_password():
    """Valider le token et appliquer le nouveau mot de passe."""
    try:
        data = request.get_json() or {}
        token = (data.get('token') or '').strip()
        new_password = data.get('new_password', '')

        if not token or not new_password:
            return jsonify({'error': 'Token et nouveau mot de passe requis'}), 400
        if len(new_password) < 8:
            return jsonify({'error': 'Le mot de passe doit contenir au moins 8 caractères'}), 400

        session = get_session()
        user = session.query(User).filter_by(reset_token=token).first()

        if not user:
            session.close()
            return jsonify({'error': 'Lien invalide ou déjà utilisé'}), 400
        exp = user.reset_token_expires
        if exp is not None and exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        if exp is not None and utcnow() > exp:
            user.reset_token = None
            user.reset_token_expires = None
            session.commit()
            session.close()
            return jsonify({'error': 'Ce lien a expiré. Faites une nouvelle demande.'}), 400

        saved_email = user.email
        saved_name  = user.full_name
        user.password_hash = bcrypt.generate_password_hash(new_password).decode('utf-8')
        user.reset_token = None
        user.reset_token_expires = None
        session.commit()
        app_url   = os.getenv('APP_URL', 'https://cei.ec2lt.sn').rstrip('/')
        reset_url = f"{app_url}/app?action=forgot"
        try:
            if saved_email:
                send_password_changed_email(saved_email, saved_name, reset_url)
        except Exception:
            pass
        session.close()
        return jsonify({'success': True, 'message': 'Mot de passe mis à jour avec succès.'})
    except Exception as e:
        print(f"Erreur reset_password: {e}")
        return jsonify({'error': str(e)}), 500

# ============================================================================
# ROUTES FORMATIONS, UE, EC (LECTURE)
# ============================================================================

@app.route('/api/formations', methods=['GET'])
@paseto_required
def get_formations():
    """Récupérer toutes les formations"""
    try:
        session = get_session()
        formations = session.query(Formation).filter_by(is_active=True).all()
        formations_list = [f.to_dict() for f in formations]
        session.close()
        return jsonify(formations_list)
    except Exception as e:
        print(f"❌ Erreur get_formations: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/formations/<int:formation_id>/semesters', methods=['GET'])
@paseto_required
def get_formation_semesters(formation_id):
    """Récupérer les semestres d'une formation"""
    try:
        session = get_session()
        semesters = session.query(Semester).filter_by(formation_id=formation_id, is_active=True).all()
        semesters_list = [s.to_dict() for s in semesters]
        session.close()
        return jsonify(semesters_list)
    except Exception as e:
        print(f"❌ Erreur get_formation_semesters: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/semesters/<int:semester_id>/ues', methods=['GET'])
@paseto_required
def get_semester_ues(semester_id):
    """Récupérer les UEs d'un semestre"""
    try:
        session = get_session()
        ues = session.query(UE).filter_by(semester_id=semester_id, is_active=True).all()
        ues_list = [ue.to_dict() for ue in ues]
        session.close()
        return jsonify(ues_list)
    except Exception as e:
        print(f"❌ Erreur get_semester_ues: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/ues/<int:ue_id>/ecs', methods=['GET'])
@paseto_required
def get_ue_ecs(ue_id):
    """Récupérer les ECs d'une UE - Amélioré : Filtrer par professeur si rôle professor"""
    try:
        user_id = get_current_user_id()
        session = get_session()
        user = session.query(User).filter_by(id=user_id).first()
        
        query = session.query(EC).filter_by(ue_id=ue_id, is_active=True)
        
        if user.role == UserRole.PROFESSOR:
            # Filtrer seulement les ECs affectés au professeur
            query = query.join(ECAssignment).filter(ECAssignment.professor_id == user_id)
        
        ecs = query.all()
        ecs_list = [ec.to_dict() for ec in ecs]
        session.close()
        return jsonify(ecs_list)
    except Exception as e:
        print(f"❌ Erreur get_ue_ecs: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/ecs', methods=['GET'])
@paseto_required
def get_all_ecs():
    """Récupérer tous les ECs — filtrable par niveau (L1/L2/L3/M1/M2) et par professeur"""
    try:
        user_id = get_current_user_id()
        session = get_session()
        user = session.query(User).filter_by(id=user_id).first()

        query = session.query(EC).filter_by(is_active=True).options(joinedload(EC.ue))

        if user.role == UserRole.PROFESSOR:
            query = query.join(ECAssignment).filter(ECAssignment.professor_id == user_id)

        niveau = request.args.get('niveau')
        formation_id = request.args.get('formation_id', type=int)
        if niveau or formation_id:
            query = (query
                     .join(UE, EC.ue_id == UE.id)
                     .join(Semester, UE.semester_id == Semester.id)
                     .join(Formation, Semester.formation_id == Formation.id))
            if niveau:
                query = query.filter(Formation.level == niveau)
            if formation_id:
                query = query.filter(Formation.id == formation_id)

        ecs = query.all()
        ecs_list = [ec.to_dict() for ec in ecs]
        session.close()
        return jsonify(ecs_list)
    except Exception as e:
        print(f"❌ Erreur get_all_ecs: {e}")
        return jsonify({'error': str(e)}), 500

# ============================================================================
# ROUTES CRUD FORMATIONS (ADMIN ONLY)
# ============================================================================

@app.route('/api/admin/formations', methods=['POST'])
@paseto_required
def create_formation():
    """Créer une formation"""
    try:
        user_id = get_current_user_id()
        session = get_session()

        user = session.query(User).filter_by(id=user_id).first()
        if user.role != UserRole.ADMIN:
            session.close()
            return jsonify({'error': 'Accès non autorisé'}), 403

        data = request.json

        # Vérifier si le code existe déjà
        existing = session.query(Formation).filter_by(code=data['code']).first()
        if existing:
            session.close()
            return jsonify({'error': 'Code formation déjà utilisé'}), 400

        formation = Formation(
            code=data['code'],
            name=data['name'],
            level=data.get('level', ''),
            department=data.get('department', ''),
            description=data.get('description', '')
        )

        session.add(formation)
        session.commit()
        formation_dict = formation.to_dict()
        session.close()

        return jsonify({'success': True, 'formation': formation_dict}), 201
    except Exception as e:
        print(f"❌ Erreur create_formation: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/admin/formations/<int:formation_id>', methods=['PUT'])
@paseto_required
def update_formation(formation_id):
    """Modifier une formation"""
    try:
        user_id = get_current_user_id()
        session = get_session()

        user = session.query(User).filter_by(id=user_id).first()
        if user.role != UserRole.ADMIN:
            session.close()
            return jsonify({'error': 'Accès non autorisé'}), 403

        formation = session.query(Formation).filter_by(id=formation_id).first()
        if not formation:
            session.close()
            return jsonify({'error': 'Formation non trouvée'}), 404

        data = request.json

        if 'code' in data and data['code'] != formation.code:
            existing = session.query(Formation).filter_by(code=data['code']).first()
            if existing:
                session.close()
                return jsonify({'error': 'Code déjà utilisé'}), 400
            formation.code = data['code']

        if 'name' in data:
            formation.name = data['name']
        if 'level' in data:
            formation.level = data['level']
        if 'department' in data:
            formation.department = data['department']
        if 'description' in data:
            formation.description = data['description']
        if 'is_active' in data:
            formation.is_active = data['is_active']

        session.commit()
        formation_dict = formation.to_dict()
        session.close()

        return jsonify({'success': True, 'formation': formation_dict})
    except Exception as e:
        print(f"❌ Erreur update_formation: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/admin/formations/<int:formation_id>', methods=['DELETE'])
@paseto_required
def delete_formation(formation_id):
    """Supprimer une formation"""
    try:
        user_id = get_current_user_id()
        session = get_session()

        user = session.query(User).filter_by(id=user_id).first()
        if user.role != UserRole.ADMIN:
            session.close()
            return jsonify({'error': 'Accès non autorisé'}), 403

        formation = session.query(Formation).filter_by(id=formation_id).first()
        if not formation:
            session.close()
            return jsonify({'error': 'Formation non trouvée'}), 404

        session.delete(formation)
        session.commit()
        session.close()

        return jsonify({'success': True, 'message': 'Formation supprimée'})
    except Exception as e:
        print(f"❌ Erreur delete_formation: {e}")
        return jsonify({'error': str(e)}), 500

# ============================================================================
# ROUTES CRUD SEMESTRES (ADMIN ONLY)
# ============================================================================

@app.route('/api/admin/semesters', methods=['POST'])
@paseto_required
def create_semester():
    """Créer un semestre"""
    try:
        user_id = get_current_user_id()
        session = get_session()

        user = session.query(User).filter_by(id=user_id).first()
        if user.role != UserRole.ADMIN:
            session.close()
            return jsonify({'error': 'Accès non autorisé'}), 403

        data = request.json

        formation = session.query(Formation).filter_by(id=data['formation_id']).first()
        if not formation:
            session.close()
            return jsonify({'error': 'Formation non trouvée'}), 404

        semester = Semester(
            formation_id=data['formation_id'],
            number=data['number'],
            name=data['name'],
            total_credits=data.get('total_credits', 30)
        )

        session.add(semester)
        session.commit()
        semester_dict = semester.to_dict()
        session.close()

        return jsonify({'success': True, 'semester': semester_dict}), 201
    except Exception as e:
        print(f"❌ Erreur create_semester: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/admin/semesters/<int:semester_id>', methods=['PUT'])
@paseto_required
def update_semester(semester_id):
    """Modifier un semestre"""
    try:
        user_id = get_current_user_id()
        session = get_session()

        user = session.query(User).filter_by(id=user_id).first()
        if user.role != UserRole.ADMIN:
            session.close()
            return jsonify({'error': 'Accès non autorisé'}), 403

        semester = session.query(Semester).filter_by(id=semester_id).first()
        if not semester:
            session.close()
            return jsonify({'error': 'Semestre non trouvé'}), 404

        data = request.json

        if 'number' in data:
            semester.number = data['number']
        if 'name' in data:
            semester.name = data['name']
        if 'total_credits' in data:
            semester.total_credits = data['total_credits']
        if 'is_active' in data:
            semester.is_active = data['is_active']

        session.commit()
        semester_dict = semester.to_dict()
        session.close()

        return jsonify({'success': True, 'semester': semester_dict})
    except Exception as e:
        print(f"❌ Erreur update_semester: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/admin/semesters/<int:semester_id>', methods=['DELETE'])
@paseto_required
def delete_semester(semester_id):
    """Supprimer un semestre"""
    try:
        user_id = get_current_user_id()
        session = get_session()

        user = session.query(User).filter_by(id=user_id).first()
        if user.role != UserRole.ADMIN:
            session.close()
            return jsonify({'error': 'Accès non autorisé'}), 403

        semester = session.query(Semester).filter_by(id=semester_id).first()
        if not semester:
            session.close()
            return jsonify({'error': 'Semestre non trouvé'}), 404

        session.delete(semester)
        session.commit()
        session.close()

        return jsonify({'success': True, 'message': 'Semestre supprimé'})
    except Exception as e:
        print(f"❌ Erreur delete_semester: {e}")
        return jsonify({'error': str(e)}), 500

# ============================================================================
# ROUTES CRUD UEs (ADMIN ONLY)
# ============================================================================

@app.route('/api/admin/ues', methods=['POST'])
@paseto_required
def create_ue():
    """Créer une UE"""
    try:
        user_id = get_current_user_id()
        session = get_session()

        user = session.query(User).filter_by(id=user_id).first()
        if user.role != UserRole.ADMIN:
            session.close()
            return jsonify({'error': 'Accès non autorisé'}), 403

        data = request.json

        # Vérifier si le code existe déjà
        existing = session.query(UE).filter_by(code=data['code']).first()
        if existing:
            session.close()
            return jsonify({'error': 'Code UE déjà utilisé'}), 400

        semester = session.query(Semester).filter_by(id=data['semester_id']).first()
        if not semester:
            session.close()
            return jsonify({'error': 'Semestre non trouvé'}), 404

        ue = UE(
            semester_id=data['semester_id'],
            code=data['code'],
            name=data['name'],
            credits=data.get('credits', 6)
        )

        session.add(ue)
        session.commit()
        ue_dict = ue.to_dict()
        session.close()

        return jsonify({'success': True, 'ue': ue_dict}), 201
    except Exception as e:
        print(f"❌ Erreur create_ue: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/admin/ues/<int:ue_id>', methods=['PUT'])
@paseto_required
def update_ue(ue_id):
    """Modifier une UE"""
    try:
        user_id = get_current_user_id()
        session = get_session()

        user = session.query(User).filter_by(id=user_id).first()
        if user.role != UserRole.ADMIN:
            session.close()
            return jsonify({'error': 'Accès non autorisé'}), 403

        ue = session.query(UE).filter_by(id=ue_id).first()
        if not ue:
            session.close()
            return jsonify({'error': 'UE non trouvée'}), 404

        data = request.json

        if 'code' in data and data['code'] != ue.code:
            existing = session.query(UE).filter_by(code=data['code']).first()
            if existing:
                session.close()
                return jsonify({'error': 'Code déjà utilisé'}), 400
            ue.code = data['code']

        if 'name' in data:
            ue.name = data['name']
        if 'credits' in data:
            ue.credits = data['credits']
        if 'is_active' in data:
            ue.is_active = data['is_active']

        session.commit()
        ue_dict = ue.to_dict()
        session.close()

        return jsonify({'success': True, 'ue': ue_dict})
    except Exception as e:
        print(f"❌ Erreur update_ue: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/admin/ues/<int:ue_id>', methods=['DELETE'])
@paseto_required
def delete_ue(ue_id):
    """Supprimer une UE"""
    try:
        user_id = get_current_user_id()
        session = get_session()

        user = session.query(User).filter_by(id=user_id).first()
        if user.role != UserRole.ADMIN:
            session.close()
            return jsonify({'error': 'Accès non autorisé'}), 403

        ue = session.query(UE).filter_by(id=ue_id).first()
        if not ue:
            session.close()
            return jsonify({'error': 'UE non trouvée'}), 404

        session.delete(ue)
        session.commit()
        session.close()

        return jsonify({'success': True, 'message': 'UE supprimée'})
    except Exception as e:
        print(f"❌ Erreur delete_ue: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/ues', methods=['GET'])
@paseto_required
def list_all_ues():
    """Liste toutes les UEs — pour les modaux d'édition"""
    try:
        user_id = get_current_user_id()
        session = get_session()
        role = get_current_user_role()
        if role not in ['professor', 'admin']:
            session.close()
            return jsonify({'error': 'Accès non autorisé'}), 403
        ues = session.query(UE).order_by(UE.name).all()
        result = [u.to_dict() for u in ues]
        session.close()
        return jsonify(result)
    except Exception as e:
        print(f"❌ list_all_ues: {e}")
        try:
            session.close()
        except Exception:
            pass
        return jsonify({'error': str(e)}), 500

# ============================================================================
# ROUTES CRUD ECs (ADMIN ONLY)
# ============================================================================

@app.route('/api/admin/ecs', methods=['POST'])
@paseto_required
def create_ec():
    """Créer un EC"""
    try:
        user_id = get_current_user_id()
        session = get_session()

        user = session.query(User).filter_by(id=user_id).first()
        if user.role != UserRole.ADMIN:
            session.close()
            return jsonify({'error': 'Accès non autorisé'}), 403

        data = request.json

        # Vérifier si le code existe déjà
        existing = session.query(EC).filter_by(code=data['code']).first()
        if existing:
            session.close()
            return jsonify({'error': 'Code EC déjà utilisé'}), 400

        ue = session.query(UE).filter_by(id=data['ue_id']).first()
        if not ue:
            session.close()
            return jsonify({'error': 'UE non trouvée'}), 404

        ec = EC(
            ue_id=data['ue_id'],
            code=data['code'],
            name=data['name'],
            cm=data.get('cm', 0),
            td=data.get('td', 0),
            tp=data.get('tp', 0),
            tpe=data.get('tpe', 0),
            vht=data.get('vht', 0),
            coefficient=data.get('coefficient', 1)
        )

        session.add(ec)
        session.commit()
        ec_dict = ec.to_dict()
        session.close()

        return jsonify({'success': True, 'ec': ec_dict}), 201
    except Exception as e:
        print(f"❌ Erreur create_ec: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/admin/ecs/<int:ec_id>', methods=['PUT'])
@paseto_required
def update_ec(ec_id):
    """Modifier un EC"""
    try:
        user_id = get_current_user_id()
        session = get_session()

        user = session.query(User).filter_by(id=user_id).first()
        if user.role != UserRole.ADMIN:
            session.close()
            return jsonify({'error': 'Accès non autorisé'}), 403

        ec = session.query(EC).filter_by(id=ec_id).first()
        if not ec:
            session.close()
            return jsonify({'error': 'EC non trouvé'}), 404

        data = request.json

        if 'code' in data and data['code'] != ec.code:
            existing = session.query(EC).filter_by(code=data['code']).first()
            if existing:
                session.close()
                return jsonify({'error': 'Code déjà utilisé'}), 400
            ec.code = data['code']

        if 'name' in data:
            ec.name = data['name']
        if 'cm' in data:
            ec.cm = data['cm']
        if 'td' in data:
            ec.td = data['td']
        if 'tp' in data:
            ec.tp = data['tp']
        if 'tpe' in data:
            ec.tpe = data['tpe']
        if 'vht' in data:
            ec.vht = data['vht']
        if 'coefficient' in data:
            ec.coefficient = data['coefficient']
        if 'is_active' in data:
            ec.is_active = data['is_active']

        session.commit()
        ec_dict = ec.to_dict()
        session.close()

        return jsonify({'success': True, 'ec': ec_dict})
    except Exception as e:
        print(f"❌ Erreur update_ec: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/admin/ecs/<int:ec_id>', methods=['DELETE'])
@paseto_required
def delete_ec(ec_id):
    """Supprimer un EC"""
    try:
        user_id = get_current_user_id()
        session = get_session()

        user = session.query(User).filter_by(id=user_id).first()
        if user.role != UserRole.ADMIN:
            session.close()
            return jsonify({'error': 'Accès non autorisé'}), 403

        ec = session.query(EC).filter_by(id=ec_id).first()
        if not ec:
            session.close()
            return jsonify({'error': 'EC non trouvé'}), 404

        session.delete(ec)
        session.commit()
        session.close()

        return jsonify({'success': True, 'message': 'EC supprimé'})
    except Exception as e:
        print(f"❌ Erreur delete_ec: {e}")
        return jsonify({'error': str(e)}), 500

# ============================================================================
# NOUVEAU : ROUTES AFFECTATION EC-PROFESSEUR (ADMIN ONLY)
# ============================================================================

@app.route('/api/admin/ec_assignments', methods=['POST'])
@paseto_required
def assign_ec_to_professor():
    """Affecter un EC à un professeur - Refuse si déjà affecté"""
    try:
        user_id = get_current_user_id()
        session = get_session()

        user = session.query(User).filter_by(id=user_id).first()
        if user.role != UserRole.ADMIN:
            session.close()
            return jsonify({'error': 'Accès non autorisé'}), 403

        data = request.json
        ec_id = data.get('ec_id')
        professor_id = data.get('professor_id')

        if not ec_id or not professor_id:
            session.close()
            return jsonify({'error': 'EC et professeur requis'}), 400

        ec = session.query(EC).filter_by(id=ec_id).first()
        if not ec:
            session.close()
            return jsonify({'error': 'EC non trouvé'}), 404

        professor = session.query(User).filter_by(id=professor_id, role=UserRole.PROFESSOR).first()
        if not professor:
            session.close()
            return jsonify({'error': 'Professeur non trouvé'}), 404

        # Vérifier si cette combinaison EC+professeur existe déjà
        existing = session.query(ECAssignment).filter_by(ec_id=ec_id, professor_id=professor_id).first()
        if existing:
            session.close()
            return jsonify({'error': 'Ce professeur est déjà affecté à cet EC'}), 400

        assignment = ECAssignment(ec_id=ec_id, professor_id=professor_id)
        session.add(assignment)
        session.commit()
        session.close()

        return jsonify({'success': True, 'message': 'EC affecté avec succès'}), 201
    except Exception as e:
        print(f"❌ Erreur assign_ec_to_professor: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/admin/ecs/<int:ec_id>/assign', methods=['POST'])
@paseto_required
def assign_ec_by_id(ec_id):
    """Affecter un EC à un professeur par ID EC (endpoint alternatif pour le frontend)"""
    try:
        user_id = get_current_user_id()
        session = get_session()

        user = session.query(User).filter_by(id=user_id).first()
        if user.role != UserRole.ADMIN:
            session.close()
            return jsonify({'error': 'Accès non autorisé'}), 403

        data = request.json
        professor_id = data.get('professor_id')

        if not professor_id:
            session.close()
            return jsonify({'error': 'Professeur requis'}), 400

        ec = session.query(EC).filter_by(id=ec_id).first()
        if not ec:
            session.close()
            return jsonify({'error': 'EC non trouvé'}), 404

        professor = session.query(User).filter_by(id=professor_id, role=UserRole.PROFESSOR).first()
        if not professor:
            session.close()
            return jsonify({'error': 'Professeur non trouvé'}), 404

        # Vérifier si cette combinaison EC+professeur existe déjà
        existing_assignment = session.query(ECAssignment).filter_by(ec_id=ec_id, professor_id=professor_id).first()
        if existing_assignment:
            session.close()
            return jsonify({'error': 'Ce professeur est déjà affecté à cet EC'}), 400

        assignment = ECAssignment(ec_id=ec_id, professor_id=professor_id)
        session.add(assignment)
        session.commit()
        session.close()

        return jsonify({'success': True, 'message': 'EC affecté avec succès'}), 201
    except Exception as e:
        print(f"❌ Erreur assign_ec_by_id: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/admin/ec_assignments/<int:assignment_id>', methods=['DELETE'])
@paseto_required
def remove_ec_assignment(assignment_id):
    """Retirer l'affectation d'un EC"""
    try:
        user_id = get_current_user_id()
        session = get_session()

        user = session.query(User).filter_by(id=user_id).first()
        if user.role != UserRole.ADMIN:
            session.close()
            return jsonify({'error': 'Accès non autorisé'}), 403

        assignment = session.query(ECAssignment).filter_by(id=assignment_id).first()
        if not assignment:
            session.close()
            return jsonify({'error': 'Affectation non trouvée'}), 404

        session.delete(assignment)
        session.commit()
        session.close()

        return jsonify({'success': True, 'message': 'Affectation supprimée'})
    except Exception as e:
        print(f"❌ Erreur remove_ec_assignment: {e}")
        return jsonify({'error': str(e)}), 500

# ============================================================================
# NOUVEAU : ROUTES INSCRIPTION ÉTUDIANT-UE (ADMIN ONLY)
# ============================================================================

@app.route('/api/admin/student_enrollments', methods=['POST'])
@paseto_required
def enroll_student_to_ue():
    """Inscrire un étudiant à une UE"""
    try:
        user_id = get_current_user_id()
        session = get_session()

        user = session.query(User).filter_by(id=user_id).first()
        if user.role != UserRole.ADMIN:
            session.close()
            return jsonify({'error': 'Accès non autorisé'}), 403

        data = request.json
        student_id = data.get('student_id')
        ue_id = data.get('ue_id')

        if not student_id or not ue_id:
            session.close()
            return jsonify({'error': 'Étudiant et UE requis'}), 400

        student = session.query(User).filter_by(id=student_id, role=UserRole.STUDENT).first()
        if not student:
            session.close()
            return jsonify({'error': 'Étudiant non trouvé'}), 404

        ue = session.query(UE).filter_by(id=ue_id).first()
        if not ue:
            session.close()
            return jsonify({'error': 'UE non trouvée'}), 404

        # Vérifier si déjà inscrit
        existing_enrollment = session.query(StudentUEEnrollment).filter_by(student_id=student_id, ue_id=ue_id).first()
        if existing_enrollment:
            session.close()
            return jsonify({'error': 'Étudiant déjà inscrit à cette UE'}), 400

        enrollment = StudentUEEnrollment(
            student_id=student_id,
            ue_id=ue_id
        )

        session.add(enrollment)
        session.commit()
        session.close()

        return jsonify({'success': True, 'message': 'Étudiant inscrit avec succès'}), 201
    except Exception as e:
        print(f"❌ Erreur enroll_student_to_ue: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/admin/students/<int:student_id>/enroll', methods=['POST'])
@paseto_required
def enroll_student_by_id(student_id):
    """Inscrire un étudiant à une UE par ID (endpoint alternatif pour le frontend)"""
    try:
        user_id = get_current_user_id()
        session = get_session()

        print("=" * 80)
        print("🔍 DEBUG enroll_student_by_id:")
        print(f"   student_id: {student_id}")
        print(f"   user_id (admin): {user_id}")
        
        user = session.query(User).filter_by(id=user_id).first()
        if user.role != UserRole.ADMIN:
            session.close()
            print("❌ ERREUR: Utilisateur non admin")
            return jsonify({'error': 'Accès non autorisé'}), 403

        data = request.json
        print(f"   data reçue: {data}")
        print(f"   type(data): {type(data)}")
        
        ue_id = data.get('ue_id') if data else None
        print(f"   ue_id extrait: {ue_id}")
        print(f"   type(ue_id): {type(ue_id)}")

        if not ue_id:
            session.close()
            print("❌ ERREUR: ue_id manquant ou vide")
            return jsonify({'error': 'UE requis (ue_id manquant)'}), 400

        # Vérification étudiant
        student = session.query(User).filter_by(id=student_id, role=UserRole.STUDENT).first()
        if not student:
            session.close()
            print(f"❌ ERREUR: Étudiant {student_id} non trouvé")
            return jsonify({'error': 'Étudiant non trouvé'}), 404

        # Vérification UE
        ue = session.query(UE).filter_by(id=ue_id).first()
        if not ue:
            session.close()
            print(f"❌ ERREUR: UE {ue_id} non trouvée")
            return jsonify({'error': 'UE non trouvée'}), 404

        # Vérifier si déjà inscrit
        existing_enrollment = session.query(StudentUEEnrollment).filter_by(
            student_id=student_id, ue_id=ue_id
        ).first()
        
        if existing_enrollment:
            session.close()
            print(f"⚠️ AVERTISSEMENT: Étudiant {student_id} déjà inscrit à UE {ue_id}")
            return jsonify({'error': 'Étudiant déjà inscrit à cette UE'}), 400

        # Créer l'inscription
        enrollment = StudentUEEnrollment(
            student_id=student_id,
            ue_id=ue_id
        )

        session.add(enrollment)
        session.commit()
        session.close()

        print(f"✅ SUCCESS: Étudiant {student_id} inscrit à UE {ue_id}")
        print("=" * 80)

        return jsonify({'success': True, 'message': 'Étudiant inscrit avec succès'}), 201
        
    except Exception as e:
        print(f"❌ EXCEPTION enroll_student_by_id: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

@app.route('/api/professor/my_students', methods=['GET'])
@paseto_required
def get_professor_students():
    """Retourne les étudiants inscrits dans les UEs des ECs du professeur connecté"""
    try:
        user_id = get_current_user_id()
        session = get_session()
        user = session.query(User).filter_by(id=user_id).first()
        if not user or user.role not in [UserRole.PROFESSOR, UserRole.ADMIN]:
            session.close()
            return jsonify({'error': 'Accès non autorisé'}), 403

        # ECs du prof → UEs concernées
        assignments = session.query(ECAssignment).filter_by(professor_id=user_id).all()
        ec_ids = [a.ec_id for a in assignments]
        ecs = session.query(EC).filter(EC.id.in_(ec_ids)).all() if ec_ids else []
        ue_ids = list({ec.ue_id for ec in ecs if ec.ue_id})

        if not ue_ids:
            session.close()
            return jsonify({'ecs': [], 'students': [], 'total': 0})

        # Étudiants inscrits dans ces UEs (uniquement role=STUDENT)
        enrollments = session.query(StudentUEEnrollment).join(
            User, StudentUEEnrollment.student_id == User.id
        ).filter(
            StudentUEEnrollment.ue_id.in_(ue_ids),
            User.role == UserRole.STUDENT
        ).all()

        # Dédupliquer par étudiant, regrouper ses UEs
        student_ues = {}
        for e in enrollments:
            student_ues.setdefault(e.student_id, set()).add(e.ue_id)

        students_out = []
        for student_id_s, enrolled_ue_ids in student_ues.items():
            student = session.query(User).filter_by(id=student_id_s, role=UserRole.STUDENT).first()
            if not student:
                continue
            formation = session.query(Formation).filter_by(id=student.formation_id).first() if getattr(student, 'formation_id', None) else None
            # ECs du prof que suit cet étudiant
            student_ecs = []
            for ec in ecs:
                if ec.ue_id in enrolled_ue_ids:
                    ue = session.query(UE).filter_by(id=ec.ue_id).first()
                    student_ecs.append({'ec_code': ec.code, 'ec_name': ec.name, 'ue_code': ue.code if ue else '—'})
            students_out.append({
                'id':             student.id,
                'full_name':      student.full_name,
                'email':          student.email,
                'niveau':         student.niveau,
                'formation_code': formation.code if formation else None,
                'formation_name': formation.name if formation else None,
                'ecs':            student_ecs,
            })

        students_out.sort(key=lambda x: x['full_name'])

        # Infos sur les ECs du prof
        ecs_out = []
        for ec in ecs:
            ue = session.query(UE).filter_by(id=ec.ue_id).first()
            count = session.query(StudentUEEnrollment).filter_by(ue_id=ec.ue_id).count()
            ecs_out.append({'ec_code': ec.code, 'ec_name': ec.name, 'ue_code': ue.code if ue else '—', 'student_count': count})

        session.close()
        return jsonify({'ecs': ecs_out, 'students': students_out, 'total': len(students_out)})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/admin/students/<int:student_id>/enrollments', methods=['GET'])
@paseto_required
def get_student_enrollments(student_id):
    """Retourne les inscriptions UE actuelles d'un étudiant avec infos Formation/Semestre"""
    try:
        user_id = get_current_user_id()
        session = get_session()
        user = session.query(User).filter_by(id=user_id).first()
        if user.role != UserRole.ADMIN:
            session.close()
            return jsonify({'error': 'Accès non autorisé'}), 403
        enrollments = session.query(StudentUEEnrollment).filter_by(student_id=student_id).all()
        result = []
        for e in enrollments:
            ue = session.query(UE).filter_by(id=e.ue_id).first()
            if not ue:
                continue
            sem = session.query(Semester).filter_by(id=ue.semester_id).first() if ue.semester_id else None
            form = session.query(Formation).filter_by(id=sem.formation_id).first() if sem and sem.formation_id else None
            result.append({
                'enrollment_id': e.id,
                'ue_id':         ue.id,
                'ue_code':       ue.code,
                'ue_name':       ue.name,
                'semester_name': sem.name if sem else '—',
                'formation_name': form.name if form else '—',
                'formation_code': form.code if form else '—',
            })
        session.close()
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/admin/students/<int:student_id>/set_formation', methods=['POST'])
@paseto_required
def set_student_formation(student_id):
    """Définir la formation principale d'un étudiant et ajouter ses UEs (sans supprimer les UEs d'autres formations — double cursus)"""
    try:
        user_id = get_current_user_id()
        session = get_session()
        user = session.query(User).filter_by(id=user_id).first()
        if user.role != UserRole.ADMIN:
            session.close()
            return jsonify({'error': 'Accès non autorisé'}), 403
        data = request.get_json() or {}
        formation_id = data.get('formation_id')
        replace_all  = data.get('replace_all', False)  # True = supprimer autres formations (réaffectation simple)
        if not formation_id:
            session.close()
            return jsonify({'error': 'formation_id requis'}), 400
        student = session.query(User).filter_by(id=student_id, role=UserRole.STUDENT).first()
        if not student:
            session.close()
            return jsonify({'error': 'Étudiant non trouvé'}), 404
        formation = session.query(Formation).filter_by(id=formation_id).first()
        if not formation:
            session.close()
            return jsonify({'error': 'Formation non trouvée'}), 404

        if replace_all:
            # Réaffectation simple : supprimer toutes les UEs existantes
            session.query(StudentUEEnrollment).filter_by(student_id=student_id).delete()

        # Inscrire à toutes les UEs de la formation choisie (sans doublon)
        semesters = session.query(Semester).filter_by(formation_id=formation_id).all()
        added = 0
        for sem in semesters:
            for ue in session.query(UE).filter_by(semester_id=sem.id).all():
                exists = session.query(StudentUEEnrollment).filter_by(
                    student_id=student_id, ue_id=ue.id).first()
                if not exists:
                    session.add(StudentUEEnrollment(student_id=student_id, ue_id=ue.id))
                    added += 1

        # Mémoriser la formation principale
        student.formation_id = formation_id
        session.commit()
        session.close()
        return jsonify({
            'success': True,
            'added': added,
            'formation_name': formation.name,
            'message': f'Formation principale : {formation.name} — {added} UE(s) ajoutée(s).'
        }), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/admin/students/<int:student_id>/enroll_formation', methods=['POST'])
@paseto_required
def enroll_student_formation(student_id):
    """Ajouter les UEs d'une Formation sans supprimer les inscriptions existantes"""
    try:
        user_id = get_current_user_id()
        session = get_session()
        user = session.query(User).filter_by(id=user_id).first()
        if user.role != UserRole.ADMIN:
            session.close()
            return jsonify({'error': 'Accès non autorisé'}), 403
        data = request.get_json() or {}
        formation_id = data.get('formation_id')
        if not formation_id:
            session.close()
            return jsonify({'error': 'formation_id requis'}), 400
        student = session.query(User).filter_by(id=student_id, role=UserRole.STUDENT).first()
        if not student:
            session.close()
            return jsonify({'error': 'Étudiant non trouvé'}), 404
        formation = session.query(Formation).filter_by(id=formation_id).first()
        if not formation:
            session.close()
            return jsonify({'error': 'Formation non trouvée'}), 404
        semesters = session.query(Semester).filter_by(formation_id=formation_id).all()
        added, already = 0, 0
        for sem in semesters:
            ues = session.query(UE).filter_by(semester_id=sem.id).all()
            for ue in ues:
                existing = session.query(StudentUEEnrollment).filter_by(
                    student_id=student_id, ue_id=ue.id
                ).first()
                if existing:
                    already += 1
                else:
                    session.add(StudentUEEnrollment(student_id=student_id, ue_id=ue.id))
                    added += 1
        session.commit()
        session.close()
        return jsonify({
            'success': True,
            'added': added,
            'already': already,
            'message': f'{added} UE(s) ajoutée(s), {already} déjà inscrite(s).'
        }), 201
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/admin/student_enrollments/<int:enrollment_id>', methods=['DELETE'])
@paseto_required
def remove_student_enrollment(enrollment_id):
    """Retirer l'inscription d'un étudiant à une UE"""
    try:
        user_id = get_current_user_id()
        session = get_session()

        user = session.query(User).filter_by(id=user_id).first()
        if user.role != UserRole.ADMIN:
            session.close()
            return jsonify({'error': 'Accès non autorisé'}), 403

        enrollment = session.query(StudentUEEnrollment).filter_by(id=enrollment_id).first()
        if not enrollment:
            session.close()
            return jsonify({'error': 'Inscription non trouvée'}), 404

        session.delete(enrollment)
        session.commit()
        session.close()

        return jsonify({'success': True, 'message': 'Inscription supprimée'})
    except Exception as e:
        print(f"❌ Erreur remove_student_enrollment: {e}")
        return jsonify({'error': str(e)}), 500

# ============================================================================
# ROUTES ADMIN - Dashboard et Utilisateurs
# ============================================================================

@app.route('/api/admin/dashboard', methods=['GET'])
@paseto_required
def admin_dashboard():
    try:
        user_id = get_current_user_id()
        session = get_session()

        user = session.query(User).filter_by(id=user_id).first()
        if not user or user.role != UserRole.ADMIN:
            session.close()
            return jsonify({'error': 'Accès non autorisé'}), 403

        total_users = session.query(User).count()
        total_students = session.query(User).filter_by(role=UserRole.STUDENT).count()
        total_professors = session.query(User).filter_by(role=UserRole.PROFESSOR).count()
        total_surveillants = session.query(User).filter_by(role=UserRole.SURVEILLANT).count()
        total_subjects = session.query(Subject).count()
        total_papers = session.query(StudentPaper).count()
        pending_reclamations = session.query(Reclamation).filter_by(status=ReclamationStatus.PENDING).count()
        total_corrected_papers = session.query(StudentPaper).filter(StudentPaper.corrected_at != None).count()

        dashboard_data = {
            'total_users': total_users,
            'total_students': total_students,
            'total_professors': total_professors,
            'total_surveillants': total_surveillants,
            'total_subjects': total_subjects,
            'total_papers': total_papers,
            'pending_reclamations': pending_reclamations,
            'total_corrected_papers': total_corrected_papers
        }

        session.close()
        return jsonify(dashboard_data)
    except Exception as e:
        print(f"❌ Erreur admin_dashboard: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/admin/corrected_papers', methods=['GET'])
@paseto_required
def admin_corrected_papers():
    """Renvoie la liste des copies corrigées récentes (ADMIN ONLY)"""
    try:
        user_id = get_current_user_id()
        session = get_session()

        user = session.query(User).filter_by(id=user_id).first()
        if not user or user.role != UserRole.ADMIN:
            session.close()
            return jsonify({'error': 'Accès non autorisé'}), 403

        papers = session.query(StudentPaper).options(
            joinedload(StudentPaper.student),
            joinedload(StudentPaper.subject)
        ).filter(StudentPaper.corrected_at != None).order_by(StudentPaper.corrected_at.desc()).limit(50).all()

        papers_list = []
        for p in papers:
            papers_list.append({
                'id': p.id,
                'student_name': p.student.full_name if p.student else 'Inconnu',
                'student_email': p.student.email if p.student else 'N/A',
                'subject_title': p.subject.title if p.subject else 'N/A',
                'score': p.score,
                'corrected_at': p.corrected_at.isoformat() if p.corrected_at else None,
                'filename': p.filename
            })

        session.close()
        return jsonify({'papers': papers_list})
    except Exception as e:
        print(f"❌ Erreur admin_corrected_papers: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/users/proctors', methods=['GET'])
@paseto_required
def get_proctor_users():
    """Retourne les utilisateurs disponibles comme surveillants (professeurs + surveillants) - professor/admin"""
    try:
        role = get_current_user_role()
        # role déjà défini par get_current_user_role()
        if role not in ['professor', 'admin']:
            return jsonify({'error': 'Accès non autorisé'}), 403
        session = get_session()
        users = session.query(User).filter(
            User.role == UserRole.SURVEILLANT,
            User.is_active == True
        ).order_by(User.full_name).all()
        result = [u.to_dict() for u in users]
        session.close()
        return jsonify(result)
    except Exception as e:
        print(f'Erreur get_proctor_users: {e}')
        try:
            session.close()
        except Exception:
            pass
        return jsonify({'error': str(e)}), 500


@app.route('/api/admin/users', methods=['GET'])
@paseto_required
def get_all_users():
    """Liste tous les utilisateurs - ADMIN ONLY"""
    try:
        user_id = get_current_user_id()
        session = get_session()

        user = session.query(User).filter_by(id=user_id).first()
        if not user or user.role != UserRole.ADMIN:
            session.close()
            return jsonify({'error': 'Acces non autorise'}), 403

        search = request.args.get('search', '').strip()
        niveau = request.args.get('niveau', '').strip()
        role_filter = request.args.get('role', '').strip()

        query = session.query(User)
        if search:
            query = query.filter(
                sa_or(
                    User.full_name.ilike(f'%{search}%'),
                    User.email.ilike(f'%{search}%')
                )
            )
        if niveau:
            query = query.filter(User.niveau == niveau)
        if role_filter:
            try:
                query = query.filter(User.role == UserRole[role_filter.upper()])
            except KeyError:
                pass

        users = query.order_by(User.created_at.desc()).all()
        users_list = [u.to_dict() for u in users]
        session.close()

        return jsonify(users_list)
    except Exception as e:
        print(f'Erreur get_all_users: {e}')
        return jsonify({'error': str(e)}), 500

@app.route('/api/admin/users', methods=['POST'])
@paseto_required
def create_user():
    try:
        admin_id = get_current_user_id()
        session = get_session()

        admin = session.query(User).filter_by(id=admin_id).first()
        if admin.role != UserRole.ADMIN:
            session.close()
            return jsonify({'error': 'Accès non autorisé'}), 403

        data = request.json
        existing = session.query(User).filter_by(email=data['email']).first()
        if existing:
            session.close()
            role_label = {'professor': 'un enseignant', 'student': 'un étudiant', 'admin': 'un administrateur', 'surveillant': 'un surveillant'}.get(existing.role.value, 'un utilisateur')
            return jsonify({'error': f"Cet email est déjà utilisé par {role_label} ({existing.full_name}). Un email doit être unique dans le système."}), 400

        role_str = data.get('role', 'student').upper()
        if role_str not in ['STUDENT', 'PROFESSOR', 'ADMIN', 'SURVEILLANT']:
            session.close()
            return jsonify({'error': 'Rôle invalide'}), 400

        hashed_password = bcrypt.generate_password_hash(data['password']).decode('utf-8')

        niveau_val = data.get('niveau', '').strip().upper() or None
        if niveau_val and niveau_val not in ['L1', 'L2', 'L3', 'M1', 'M2']:
            niveau_val = None

        new_user = User(
            email=data['email'],
            password_hash=hashed_password,
            full_name=data['full_name'],
            role=UserRole[role_str],
            niveau=niveau_val
        )

        session.add(new_user)
        session.commit()
        user_dict = new_user.to_dict()

        # Envoyer email avec identifiants
        try:
            email_sent = send_account_created_email(
                user_email=data['email'],
                user_name=data['full_name'],
                role=data.get('role', 'student'),
                temp_password=data['password']
            )
            if email_sent:
                print(f"✅ Email envoyé avec succès à {data['email']}")
            else:
                print(f"⚠️ Échec envoi email à {data['email']}")
        except Exception as email_error:
            print(f"⚠️ Erreur envoi email: {email_error}")

        session.close()

        return jsonify({'success': True, 'message': 'Utilisateur créé avec succès', 'user': user_dict}), 201
    except Exception as e:
        print(f"❌ Erreur create_user: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/admin/users/<int:target_user_id>', methods=['PUT'])
@paseto_required
def update_user(target_user_id):
    try:
        admin_id = get_current_user_id()
        session = get_session()

        admin = session.query(User).filter_by(id=admin_id).first()
        if admin.role != UserRole.ADMIN:
            session.close()
            return jsonify({'error': 'Accès non autorisé'}), 403

        user = session.query(User).filter_by(id=target_user_id).first()
        if not user:
            session.close()
            return jsonify({'error': 'Utilisateur non trouvé'}), 404

        data = request.json

        if 'full_name' in data:
            user.full_name = data['full_name']
        if 'email' in data and data['email'] != user.email:
            existing = session.query(User).filter_by(email=data['email']).first()
            if existing:
                session.close()
                return jsonify({'error': 'Cet email est déjà utilisé'}), 400
            user.email = data['email']
        if 'password' in data and data['password']:
            user.password_hash = bcrypt.generate_password_hash(data['password']).decode('utf-8')
        if 'role' in data:
            role_str = data['role'].upper()
            if role_str in ['STUDENT', 'PROFESSOR', 'ADMIN', 'SURVEILLANT']:
                user.role = UserRole[role_str]
        if 'is_active' in data:
            user.is_active = data['is_active']
        if 'niveau' in data:
            niveau_val = (data['niveau'] or '').strip().upper() or None
            user.niveau = niveau_val if niveau_val in ['L1', 'L2', 'L3', 'M1', 'M2'] else None

        session.commit()
        user_dict = user.to_dict()
        session.close()

        return jsonify({'success': True, 'message': 'Utilisateur modifié avec succès', 'user': user_dict})
    except Exception as e:
        print(f"❌ Erreur update_user: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/admin/users/<int:target_user_id>', methods=['DELETE'])
@paseto_required
def delete_user(target_user_id):
    """✅ CORRECTION FINALE: Supprimer utilisateur avec TOUTES les dépendances CASCADE"""
    try:
        admin_id = get_current_user_id()
        session = get_session()

        admin = session.query(User).filter_by(id=admin_id).first()
        if admin.role != UserRole.ADMIN:
            session.close()
            return jsonify({'error': 'Accès non autorisé'}), 403

        if admin_id == target_user_id:
            session.close()
            return jsonify({'error': 'Impossible de supprimer votre propre compte'}), 400

        user = session.query(User).filter_by(id=target_user_id).first()
        if not user:
            session.close()
            return jsonify({'error': 'Utilisateur non trouvé'}), 404

        # ✅ ORDRE CORRECT: Réclamations → Historique → Copies → Sujets → User

        # 1. Récupérer les IDs des copies de l'étudiant
        papers_ids = [p.id for p in session.query(StudentPaper).filter_by(student_id=target_user_id).all()]

        if papers_ids:
            from sqlalchemy import or_

            # 2. Supprimer TOUTES les réclamations liées à ces copies
            session.query(Reclamation).filter(
                or_(
                    Reclamation.paper_id.in_(papers_ids),
                    Reclamation.student_id == target_user_id
                )
            ).delete(synchronize_session=False)

            # 3. ✅ NOUVEAU: Supprimer l'historique de correction lié à ces copies
            session.query(CorrectionHistory).filter(
                CorrectionHistory.paper_id.in_(papers_ids)
            ).delete(synchronize_session=False)
        else:
            # Supprimer les réclamations de l'étudiant même s'il n'a pas de copies
            session.query(Reclamation).filter_by(student_id=target_user_id).delete()

        # 4. Mettre à NULL responded_by_id dans les réclamations répondues par cet utilisateur
        reclamations_responded = session.query(Reclamation).filter_by(responded_by_id=target_user_id).all()
        for reclamation in reclamations_responded:
            reclamation.responded_by_id = None

        # 5. ✅ NOUVEAU: Mettre à NULL corrector_id dans l'historique
        histories_corrected = session.query(CorrectionHistory).filter_by(corrector_id=target_user_id).all()
        for history in histories_corrected:
            history.corrector_id = None

        # 6. Maintenant supprimer les copies de l'étudiant
        session.query(StudentPaper).filter_by(student_id=target_user_id).delete()

        # 7. Mettre à NULL corrected_by_id dans les copies corrigées par cet utilisateur
        papers_corrected = session.query(StudentPaper).filter_by(corrected_by_id=target_user_id).all()
        for paper in papers_corrected:
            paper.corrected_by_id = None

        # 8. Supprimer les sujets créés par l'utilisateur
        session.query(Subject).filter_by(creator_id=target_user_id).delete()

        # Nouveau: Supprimer les affectations EC pour professeurs
        session.query(ECAssignment).filter_by(professor_id=target_user_id).delete()

        # Nouveau: Supprimer les inscriptions UE pour étudiants
        session.query(StudentUEEnrollment).filter_by(student_id=target_user_id).delete()

        # Supprimer les relevés de notes de l'étudiant
        session.query(GradeTranscript).filter_by(student_id=target_user_id).delete(synchronize_session=False)

        # Supprimer les données liées aux tentatives d'examens en ligne
        attempt_ids = [a.id for a in session.query(ExamAttempt.id).filter_by(student_id=target_user_id).all()]
        if attempt_ids:
            # ProctorAssignment AVANT ExamAttempt (FK constraint)
            session.query(ProctorAssignment).filter(
                ProctorAssignment.attempt_id.in_(attempt_ids)
            ).delete(synchronize_session=False)
            session.query(CameraLog).filter(
                CameraLog.attempt_id.in_(attempt_ids)
            ).delete(synchronize_session=False)
            session.query(ExamActivityLog).filter(
                ExamActivityLog.attempt_id.in_(attempt_ids)
            ).delete(synchronize_session=False)
            session.query(Reclamation).filter(
                Reclamation.attempt_id.in_(attempt_ids)
            ).delete(synchronize_session=False)
        session.query(ExamAttempt).filter_by(student_id=target_user_id).delete(synchronize_session=False)

        # 9. Enfin, supprimer l'utilisateur
        session.delete(user)
        session.commit()
        session.close()

        return jsonify({'success': True, 'message': 'Utilisateur supprimé avec succès'})
    except Exception as e:
        print(f"❌ Erreur delete_user: {e}")
        import traceback
        traceback.print_exc()
        session.rollback()
        session.close()
        return jsonify({'error': str(e)}), 500

@app.route('/api/students/list', methods=['GET'])
@paseto_required
def get_students_list():
    try:
        user_id = get_current_user_id()
        session = get_session()

        user = session.query(User).filter_by(id=user_id).first()
        if user.role not in [UserRole.PROFESSOR, UserRole.ADMIN]:
            session.close()
            return jsonify({'error': 'Accès non autorisé'}), 403

        students = session.query(User).filter_by(role=UserRole.STUDENT).order_by(User.full_name).all()
        students_list = []
        for s in students:
            formation = session.query(Formation).filter_by(id=s.formation_id).first() if getattr(s, 'formation_id', None) else None
            students_list.append({
                'id':             s.id,
                'full_name':      s.full_name,
                'email':          s.email,
                'formation_id':   s.formation_id if hasattr(s, 'formation_id') else None,
                'formation_name': formation.name if formation else None,
                'formation_code': formation.code if formation else None,
            })
        session.close()

        return jsonify(students_list)
    except Exception as e:
        print(f"❌ Erreur get_students_list: {e}")
        return jsonify({'error': str(e)}), 500

# ============================================================================
# ROUTES SUJETS
# ============================================================================

@app.route('/api/subjects', methods=['GET'])
@paseto_required
def get_subjects():
    """Récupérer les sujets - FILTRÉS par professeur"""
    try:
        user_id = get_current_user_id()
        session = get_session()

        user = session.query(User).filter_by(id=user_id).first()

        query = session.query(Subject).options(
            joinedload(Subject.ec).joinedload(EC.ue),
            joinedload(Subject.creator)
        )

        if user.role == UserRole.STUDENT:
            subjects = query.filter_by(is_active=True).order_by(desc(Subject.created_at)).all()
        elif user.role == UserRole.PROFESSOR:
            # Montrer uniquement les sujets créés par ce professeur
            subjects = query.filter(Subject.creator_id == user_id).order_by(desc(Subject.created_at)).all()
        else:
            subjects = query.order_by(desc(Subject.created_at)).all()

        subjects_list = [subject.to_dict() for subject in subjects]
        session.close()
        return jsonify(subjects_list)
    except Exception as e:
        print(f"❌ Erreur get_subjects: {e}")
        return jsonify({'error': str(e)}), 500
    
@app.route('/api/subjects/<int:subject_id>', methods=['GET'])
@paseto_required
def get_subject_detail(subject_id):
    """Récupérer les détails d'un sujet spécifique"""
    try:
        user_id = get_current_user_id()
        session = get_session()

        user = session.query(User).filter_by(id=user_id).first()

        subject = session.query(Subject).options(
            joinedload(Subject.ec).joinedload(EC.ue),
            joinedload(Subject.creator)
        ).filter_by(id=subject_id).first()

        if not subject:
            session.close()
            return jsonify({'error': 'Sujet non trouvé'}), 404

        # Vérifier les permissions
        if user.role == UserRole.STUDENT and not subject.is_active:
            session.close()
            return jsonify({'error': 'Sujet non accessible'}), 403

        if user.role == UserRole.PROFESSOR and subject.creator_id != user_id:
            session.close()
            return jsonify({'error': 'Accès non autorisé'}), 403

        subject_dict = subject.to_dict()
        session.close()

        return jsonify(subject_dict)

    except Exception as e:
        print(f"❌ Erreur get_subject_detail: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

@app.route('/api/subjects/<int:subject_id>', methods=['DELETE'])
@paseto_required
def delete_subject(subject_id):
    """✅ CORRECTION COMPLÈTE : Supprimer sujet avec SQLAlchemy pur + gestion cascade"""
    try:
        user_id = get_current_user_id()
        session = get_session()

        user = session.query(User).filter_by(id=user_id).first()
        if not user or user.role not in [UserRole.ADMIN, UserRole.PROFESSOR]:
            session.close()
            return jsonify({'success': False, 'error': 'Non autorisé'}), 403
        
        subject = session.query(Subject).filter_by(id=subject_id).first()
        if not subject:
            session.close()
            return jsonify({'success': False, 'error': 'Sujet non trouvé'}), 404
        
        # Vérifier les permissions (admin ou créateur)
        if user.role == UserRole.PROFESSOR and subject.creator_id != user_id:
            session.close()
            return jsonify({'success': False, 'error': 'Vous ne pouvez supprimer que vos propres sujets'}), 403
        
        # ✅ Supprimer les dépendances dans l'ordre inverse
        
        # 1. Récupérer tous les examens en ligne liés au sujet
        online_exams = session.query(OnlineExam).filter_by(subject_id=subject_id).all()
        
        for exam in online_exams:
            # 2. Récupérer tous les IDs de tentatives pour cet examen
            attempt_ids = [a.id for a in session.query(ExamAttempt.id).filter_by(exam_id=exam.id).all()]

            if attempt_ids:
                # 3. Supprimer camera_logs
                session.query(CameraLog).filter(
                    CameraLog.attempt_id.in_(attempt_ids)
                ).delete(synchronize_session=False)

                # 4. Supprimer activity_logs
                session.query(ExamActivityLog).filter(
                    ExamActivityLog.attempt_id.in_(attempt_ids)
                ).delete(synchronize_session=False)

                # 5. Supprimer proctor_assignments liés aux tentatives
                session.query(ProctorAssignment).filter(
                    ProctorAssignment.attempt_id.in_(attempt_ids)
                ).delete(synchronize_session=False)

                # 6. Nullifier attempt_id dans les réclamations (nullable)
                session.query(Reclamation).filter(
                    Reclamation.attempt_id.in_(attempt_ids)
                ).update({'attempt_id': None}, synchronize_session=False)

            # 7. Supprimer proctor_assignments liés à l'examen (sans attempt)
            session.query(ProctorAssignment).filter_by(
                exam_id=exam.id
            ).delete(synchronize_session=False)

            # 8. Supprimer exam_proctors (surveillants affectés à l'examen)
            session.query(ExamProctor).filter_by(
                exam_id=exam.id
            ).delete(synchronize_session=False)

            # 9. Supprimer les tentatives
            session.query(ExamAttempt).filter_by(exam_id=exam.id).delete(synchronize_session=False)

            # 10. Supprimer l'examen
            session.delete(exam)
        
        # 7. Supprimer les copies corrigées liées au sujet
        # Ordre : réclamations → historique → copies
        papers = session.query(StudentPaper).filter_by(subject_id=subject_id).all()
        for paper in papers:
            # Supprimer réclamations
            session.query(Reclamation).filter_by(paper_id=paper.id).delete(synchronize_session=False)
            # Supprimer historique
            session.query(CorrectionHistory).filter_by(paper_id=paper.id).delete(synchronize_session=False)
            # Supprimer la copie
            session.delete(paper)
        
        # 8. Enfin, supprimer le sujet
        session.delete(subject)
        session.commit()
        session.close()
        
        return jsonify({
            'success': True,
            'message': 'Sujet et toutes ses dépendances supprimés avec succès'
        })
        
    except Exception as e:
        session.rollback()
        session.close()
        print(f"❌ Erreur delete_subject: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({
            'success': False,
            'error': f'Erreur lors de la suppression: {str(e)}'
        }), 500

@app.route('/api/subjects/upload', methods=['POST'])
@paseto_required
def upload_subject():
    """Créer un sujet - maintenant lié à un EC - Amélioré : Vérifier affectation pour prof"""
    try:
        user_id = get_current_user_id()
        session = get_session()

        user = session.query(User).filter_by(id=user_id).first()
        if user.role not in [UserRole.PROFESSOR, UserRole.ADMIN]:
            session.close()
            return jsonify({'error': 'Accès non autorisé'}), 403

        if 'file' not in request.files:
            session.close()
            return jsonify({'error': 'Aucun fichier fourni'}), 400

        file = request.files['file']
        title = request.form.get('title', 'Sans titre')
        ec_id = request.form.get('ec_id')

        if file.filename == '':
            session.close()
            return jsonify({'error': 'Aucun fichier sélectionné'}), 400

        if not allowed_file(file.filename):
            session.close()
            return jsonify({'error': 'Type de fichier non autorisé. Utilisez PDF, DOCX ou TXT'}), 400

        # Amélioré : Vérifier si prof a droit à cet EC
        if ec_id and user.role == UserRole.PROFESSOR:
            assignment = session.query(ECAssignment).filter_by(ec_id=ec_id, professor_id=user_id).first()
            if not assignment:
                session.close()
                return jsonify({'error': 'Vous n\'êtes pas responsable de cet EC'}), 403

        if ec_id:
            ec = session.query(EC).filter_by(id=ec_id).first()
            if not ec:
                session.close()
                return jsonify({'error': 'EC non trouvé'}), 404

        filename = secure_filename(file.filename)
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        filename = f"subject_{timestamp}_{filename}"
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        file.save(filepath)

        content = extract_text_from_file(filepath)

        if not content:
            os.remove(filepath)
            session.close()
            return jsonify({'error': 'Impossible d\'extraire le texte du fichier'}), 400

        system_prompt = """Tu es un expert en évaluation pédagogique.

Ton rôle: Analyser un sujet d'examen et créer un barème de notation détaillé et précis.

Format de sortie OBLIGATOIRE:
=== BARÈME DE NOTATION ===
Question 1 (X points):
- Critère 1: Y points - [description précise]
...
Total: 20 points"""

        rubric = call_claude(system_prompt, content, temperature=0.1)

        new_subject = Subject(
            title=title,
            content=content,
            rubric=rubric,
            filename=filename,
            creator_id=user_id,
            ec_id=int(ec_id) if ec_id else None
        )

        session.add(new_subject)
        session.commit()

        subject_dict = new_subject.to_dict()
        session.close()

        return jsonify({
            'success': True,
            'subject': subject_dict
        })

    except Exception as e:
        print(f"❌ Erreur upload_subject: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

# ============================================================================
# ROUTES CORRECTION DE COPIES
# ============================================================================

@app.route('/api/papers/correct', methods=['POST'])
@app.route('/api/papers/upload', methods=['POST'])
@paseto_required
def upload_paper():
    """Upload et correction de copie avec hash + email - Amélioré : Liée à EC via subject, fenêtre réclamation"""
    try:
        from utils import calculate_file_hash, extract_student_name_from_content

        user_id = get_current_user_id()
        session = get_session()

        user = session.query(User).filter_by(id=user_id).first()

        if 'file' not in request.files:
            session.close()
            return jsonify({'error': 'Aucun fichier fourni'}), 400

        file = request.files['file']
        subject_id = request.form.get('subject_id')
        student_id = request.form.get('student_id')
        student_name = request.form.get('student_name')

        if not subject_id:
            session.close()
            return jsonify({'error': 'ID du sujet requis'}), 400

        if not student_id and not student_name:
            session.close()
            return jsonify({'error': 'ID ou nom de l\'étudiant requis'}), 400

        subject = session.query(Subject).filter_by(id=subject_id).first()
        if not subject:
            session.close()
            return jsonify({'error': 'Sujet non trouvé'}), 404

        if user.role == UserRole.PROFESSOR and subject.creator_id != user_id:
            session.close()
            return jsonify({'error': 'Vous ne pouvez corriger que vos propres sujets'}), 403

        # ✅ MATCHING INTELLIGENT OBLIGATOIRE
        if student_name and not student_id:
            from utils import match_student_by_name

            # Essayer de matcher avec un étudiant existant
            matched_student = match_student_by_name(student_name, session)

            if matched_student:
                student_id = matched_student.id
                print(f"✅ Étudiant matché: {matched_student.full_name} ({matched_student.email})")
            else:
                # Si aucun match, rejeter la copie avec message clair
                session.close()
                return jsonify({
                    'error': f'Étudiant "{student_name}" non trouvé dans le système',
                    "suggestion": "Veuillez creer cet etudiant via interface Admin",
                    'extracted_name': student_name
                }), 404

        if file.filename == '':
            session.close()
            return jsonify({'error': 'Aucun fichier sélectionné'}), 400

        if not allowed_file(file.filename):
            session.close()
            return jsonify({'error': 'Type de fichier non autorisé'}), 400

        filename = secure_filename(file.filename)
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        filename = f"paper_{timestamp}_{filename}"
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        file.save(filepath)

        # ✅ CALCUL HASH POUR DÉTECTION DOUBLONS
        file_hash = calculate_file_hash(filepath)

        if file_hash:
            # Vérifier si ce fichier a déjà été uploadé
            existing_paper = session.query(StudentPaper).filter_by(file_hash=file_hash).first()
            if existing_paper:
                os.remove(filepath) # Supprimer le fichier dupliqué
                session.close()
                return jsonify({
                    'error': 'Cette copie a déjà été corrigée',
                    'duplicate': True,
                    'existing_paper_id': existing_paper.id,
                    'student_name': existing_paper.student.full_name if existing_paper.student else 'Inconnu',
                    'score': existing_paper.score
                }), 400

        paper_content = extract_text_from_file(filepath)

        if not paper_content:
            os.remove(filepath)
            session.close()
            return jsonify({'error': 'Impossible d\'extraire le texte du fichier'}), 400

        # ✅ EXTRACTION NOM ÉTUDIANT
        extracted_name = extract_student_name_from_content(paper_content)

        # ✅ MATCHING AUTOMATIQUE SI NOM EXTRAIT
        if extracted_name and not student_id:
            from utils import match_student_by_name
            matched_student = match_student_by_name(extracted_name, session)

            if matched_student:
                student_id = matched_student.id
                print(f"✅ Étudiant auto-matché via extraction: {matched_student.full_name} ({matched_student.email})")
            else:
                print(f"⚠️ Nom extrait '{extracted_name}' non matché - copie rejetée")
                os.remove(filepath)
                session.close()
                return jsonify({
                    'error': f'Étudiant "{extracted_name}" non trouvé',
                    'suggestion': 'Créez d\'abord cet étudiant via Admin -> Utilisateurs',
                    'extracted_name': extracted_name
                }), 404

        system_prompt = _build_correction_system_prompt(subject.title, subject.content)

        user_message = f"""SUJET D'EXAMEN:
{subject.content}

BARÈME DE NOTATION:
{subject.rubric}

COPIE À CORRIGER:
{paper_content}

RAPPEL: Tu DOIS finir par "Note totale: XX.XX/20" """

        result = call_claude(system_prompt, user_message, temperature=0.15)
        score = extract_score_from_correction(result)

        corrected_at = utcnow()
        new_paper = StudentPaper(
            subject_id=subject_id,
            student_id=student_id,
            content=paper_content,
            grade=result,
            score=score,
            filename=filename,
            file_hash=file_hash,
            extracted_student_name=extracted_name,
            corrected_by_id=user_id if user.role in [UserRole.PROFESSOR, UserRole.ADMIN] else None,
            corrected_at=corrected_at,
            reclamation_window_end=corrected_at + timedelta(days=7)  # Nouveau: Fenêtre de 7 jours
        )

        session.add(new_paper)
        session.commit()

        # ✅ ENVOI EMAIL AUTOMATIQUE APRÈS CORRECTION AVEC PDF EN PIÈCE JOINTE
        try:
            student_obj = session.query(User).filter_by(id=student_id).first()
            if student_obj and student_obj.email and '@temp.edu' not in student_obj.email:
                # Générer PDF de la copie corrigée
                paper_data = {
                    'student_name': student_obj.full_name,
                    'subject_title': subject.title,
                    'score': score,
                    'grade': result,
                    'corrected_at': corrected_at.isoformat()
                }
                pdf_path = f"exports/copie_{new_paper.id}.pdf"
                generate_corrected_paper_pdf(paper_data, pdf_path)

                # Envoi email avec PDF attaché
                attachments = [{'filename': f'copie_{new_paper.id}.pdf', 'path': pdf_path}]
                email_sent = send_paper_corrected_email(
                    student_email=student_obj.email,
                    student_name=student_obj.full_name,
                    subject_title=subject.title,
                    score=score,
                    paper_id=new_paper.id,
                    attachments=attachments  # Nouveau: Pièce jointe
                )
                if email_sent:
                    new_paper.email_sent = True
                    session.commit()
                    print(f"✅ Email avec PDF envoyé à {student_obj.email}")
                else:
                    print(f"⚠️ Échec envoi email à {student_obj.email}")
                # Supprimer PDF temporaire si besoin
                os.remove(pdf_path)
        except Exception as email_error:
            print(f"⚠️ Erreur envoi email: {email_error}")

        paper_dict = new_paper.to_dict()
        session.close()

        return jsonify({
            'success': True,
            'paper': paper_dict,
            'duplicate_check': 'passed',
            'extracted_name': extracted_name,
            'email_sent': new_paper.email_sent
        })

    except Exception as e:
        print(f"❌ Erreur upload_paper: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@app.route('/api/papers/upload-batch', methods=['POST'])
@paseto_required
def upload_papers_batch():
    """Correction en lot avec accès dossier - Amélioré : Fenêtre réclamation, PDF en email"""
    try:
        user_id = get_current_user_id()
        session = get_session()

        user = session.query(User).filter_by(id=user_id).first()
        if user.role not in [UserRole.PROFESSOR, UserRole.ADMIN]:
            session.close()
            return jsonify({'error': 'Accès non autorisé'}), 403

        subject_id = request.form.get('subject_id')
        if not subject_id:
            session.close()
            return jsonify({'error': 'ID du sujet requis'}), 400

        subject = session.query(Subject).filter_by(id=subject_id).first()
        if not subject:
            session.close()
            return jsonify({'error': 'Sujet non trouvé'}), 404

        if user.role == UserRole.PROFESSOR and subject.creator_id != user_id:
            session.close()
            return jsonify({'error': 'Vous ne pouvez corriger que vos propres sujets'}), 403

        files = request.files.getlist('files')
        if not files:
            session.close()
            return jsonify({'error': 'Aucun fichier fourni'}), 400

        results = []
        errors = []

        system_prompt = _build_correction_system_prompt(subject.title, subject.content)

        for idx, file in enumerate(files):
            try:
                if file.filename == '':
                    errors.append(f"Fichier {idx+1}: Nom vide")
                    continue

                if not allowed_file(file.filename):
                    errors.append(f"Fichier {idx+1}: Type non autorisé")
                    continue

                filename = secure_filename(file.filename)
                timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
                filename_saved = f"paper_{timestamp}_{idx}_{filename}"
                filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename_saved)
                file.save(filepath)

                # Calculer hash pour éviter doublons
                file_hash = calculate_file_hash(filepath)
                if file_hash:
                    existing = session.query(StudentPaper).filter_by(file_hash=file_hash).first()
                    if existing:
                        # Fichier déjà corrigé -> ignorer
                        os.remove(filepath)
                        results.append({
                            'filename': file.filename,
                            'student_name': existing.student.full_name if existing.student else 'Inconnu',
                            'score': existing.score,
                            'success': False,
                            'duplicate': True,
                            'message': 'Copie déjà corrigée (hash trouvé)'
                        })
                        continue

                paper_content = extract_text_from_file(filepath)

                if not paper_content:
                    os.remove(filepath)
                    errors.append(f"Fichier {idx+1}: Extraction impossible")
                    continue

                # Essayer d'abord d'extraire le nom depuis le contenu (meilleure précision)
                extracted_name = extract_student_name_from_content(paper_content)
                student = None
                student_name = None

                if extracted_name:
                    student = match_student_by_name(extracted_name, session)
                    student_name = extracted_name

                # Si pas de match via extraction, tenter via nom fichier
                if not student:
                    guessed_name = os.path.splitext(file.filename)[0].replace('copie_', '').replace('_', ' ').title()
                    student = match_student_by_name(guessed_name, session)
                    student_name = student_name or guessed_name

                # Si toujours pas de match, créer étudiant temporaire (adresse @temp.edu)
                if not student:
                    temp_email = f"{student_name.lower().replace(' ', '.')}@temp.edu"
                    temp_password = bcrypt.generate_password_hash('TempPassword123').decode('utf-8')
                    student = User(
                        email=temp_email,
                        password_hash=temp_password,
                        full_name=student_name,
                        role=UserRole.STUDENT
                    )
                    session.add(student)
                    session.flush()

                # Préparer prompt et corriger
                user_message = f"""SUJET: {subject.content}
BARÈME: {subject.rubric}
COPIE: {paper_content}

RAPPEL: Termine par "Note totale: XX.XX/20" """

                result = call_claude(system_prompt, user_message, temperature=0.15)
                score = extract_score_from_correction(result)

                corrected_at = utcnow()
                new_paper = StudentPaper(
                    subject_id=subject_id,
                    student_id=student.id,
                    content=paper_content,
                    grade=result,
                    score=score,
                    filename=filename_saved,
                    file_hash=file_hash,
                    corrected_by_id=user_id,
                    corrected_at=corrected_at,
                    reclamation_window_end=corrected_at + timedelta(days=7)
                )

                session.add(new_paper)
                session.flush()  # Pour avoir l'ID

                # Envoi email avec PDF si adresse valide
                if student.email and '@temp.edu' not in student.email and '@noemail.local' not in student.email and student.has_email:
                    paper_data = {
                        'student_name': student.full_name,
                        'subject_title': subject.title,
                        'score': score,
                        'grade': result,
                        'corrected_at': corrected_at.isoformat()
                    }
                    pdf_path = f"exports/copie_{new_paper.id}.pdf"
                    generate_corrected_paper_pdf(paper_data, pdf_path)

                    attachments = [{'filename': f'copie_{new_paper.id}.pdf', 'path': pdf_path}]
                    email_sent = send_paper_corrected_email(
                        student_email=student.email,
                        student_name=student.full_name,
                        subject_title=subject.title,
                        score=score,
                        paper_id=new_paper.id,
                        attachments=attachments
                    )
                    if email_sent:
                        new_paper.email_sent = True
                    os.remove(pdf_path)

                results.append({
                    'filename': file.filename,
                    'student_name': student_name,
                    'score': score,
                    'success': True
                })

            except Exception as e:
                errors.append(f"Fichier {idx+1}: {str(e)}")

        session.commit()
        session.close()

        return jsonify({
            'success': True,
            'corrected': len(results),
            'errors': len(errors),
            'results': results,
            'error_details': errors
        })

    except Exception as e:
        print(f"❌ Erreur batch: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

@app.route('/api/papers/subject/<int:subject_id>', methods=['GET'])
@paseto_required
def get_papers_by_subject(subject_id):
    """Récupérer les copies d'un sujet"""
    try:
        user_id = get_current_user_id()
        session = get_session()

        user = session.query(User).filter_by(id=user_id).first()
        if user.role not in [UserRole.PROFESSOR, UserRole.ADMIN]:
            session.close()
            return jsonify({'error': 'Accès non autorisé'}), 403

        subject = session.query(Subject).filter_by(id=subject_id).first()
        if not subject:
            session.close()
            return jsonify({'error': 'Sujet non trouvé'}), 404

        if user.role == UserRole.PROFESSOR and subject.creator_id != user_id:
            session.close()
            return jsonify({'error': 'Vous ne pouvez voir que les copies de vos propres sujets'}), 403

        papers = session.query(StudentPaper).options(
            joinedload(StudentPaper.student)
        ).filter_by(subject_id=subject_id).all()

        papers_list = []
        for paper in papers:
            papers_list.append({
                'id': paper.id,
                'student_id': paper.student_id,
                'student_name': paper.student.full_name if paper.student else 'Inconnu',
                'student_email': paper.student.email if paper.student else 'N/A',
                'score': paper.score,
                'grade': paper.grade,
                'content': paper.content,
                'filename': paper.filename,
                'corrected_at': paper.corrected_at.isoformat() if paper.corrected_at else None,
                'created_at': paper.created_at.isoformat() if paper.created_at else None
            })

        session.close()
        return jsonify(papers_list)

    except Exception as e:
        print(f"❌ Erreur get_papers_by_subject: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

@app.route('/api/papers/detail/<int:paper_id>', methods=['GET'])
@paseto_required
def get_paper_detail(paper_id):
    try:
        user_id = get_current_user_id()
        session = get_session()

        user = session.query(User).filter_by(id=user_id).first()

        paper = session.query(StudentPaper).options(
            joinedload(StudentPaper.subject),
            joinedload(StudentPaper.student)
        ).filter_by(id=paper_id).first()

        if not paper:
            session.close()
            return jsonify({'error': 'Copie non trouvée'}), 404

        if user.role == UserRole.STUDENT and paper.student_id != user_id:
            session.close()
            return jsonify({'error': 'Accès non autorisé'}), 403

        if user.role == UserRole.PROFESSOR and paper.subject.creator_id != user_id:
            session.close()
            return jsonify({'error': 'Accès non autorisé'}), 403

        paper_dict = paper.to_dict()
        session.close()

        return jsonify(paper_dict)

    except Exception as e:
        print(f"❌ Erreur get_paper_detail: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

# ============================================================================
# ROUTES STATISTIQUES
# ============================================================================

@app.route('/api/statistics/<int:subject_id>', methods=['GET'])
@paseto_required
def get_subject_statistics(subject_id):
    """Statistiques d'un sujet"""
    try:
        user_id = get_current_user_id()
        session = get_session()

        user = session.query(User).filter_by(id=user_id).first()
        if user.role not in [UserRole.PROFESSOR, UserRole.ADMIN]:
            session.close()
            return jsonify({'error': 'Accès non autorisé'}), 403

        subject = session.query(Subject).filter_by(id=subject_id).first()
        if not subject:
            session.close()
            return jsonify({'error': 'Sujet non trouvé'}), 404

        if user.role == UserRole.PROFESSOR and subject.creator_id != user_id:
            session.close()
            return jsonify({'error': 'Vous ne pouvez voir que les statistiques de vos propres sujets'}), 403

        # ── Copies papier ────────────────────────────────────────────────────────
        papers = session.query(StudentPaper).options(
            joinedload(StudentPaper.student)
        ).filter_by(subject_id=subject_id).all()

        papers_details = []
        for p in papers:
            if p.score is not None:
                papers_details.append({
                    'id': p.id,
                    'student_name':  p.student.full_name if p.student else 'Inconnu',
                    'student_email': p.student.email if p.student else 'N/A',
                    'score': p.score,
                    'corrected_at': p.corrected_at.isoformat() if p.corrected_at else None,
                    'filename': p.filename,
                    'type': 'paper'
                })

        # ── Examens en ligne liés à ce sujet ────────────────────────────────────
        online_exams = session.query(OnlineExam).filter_by(subject_id=subject_id).all()
        online_exam_ids = [e.id for e in online_exams]

        attempts_details = []
        if online_exam_ids:
            attempts = session.query(ExamAttempt).options(
                joinedload(ExamAttempt.student),
                joinedload(ExamAttempt.exam)
            ).filter(
                ExamAttempt.exam_id.in_(online_exam_ids),
                ExamAttempt.score.isnot(None)
            ).all()

            for att in attempts:
                attempts_details.append({
                    'id': att.id,
                    'student_name':  att.student.full_name if att.student else 'Inconnu',
                    'student_email': att.student.email if att.student else 'N/A',
                    'score': att.score,
                    'corrected_at': att.corrected_at.isoformat() if att.corrected_at else (att.submitted_at.isoformat() if att.submitted_at else None),
                    'exam_title': att.exam.title if att.exam else '—',
                    'type': 'online'
                })

        # ── Stats globales (papier + online) ────────────────────────────────────
        all_entries = papers_details + attempts_details
        online_exams_info = [{'id': e.id, 'title': e.title, 'status': e.status.value,
                               'start_time': e.start_time.isoformat() if e.start_time else None,
                               'attempts_count': len([a for a in attempts_details if a['exam_title'] == e.title])}
                              for e in online_exams] if online_exams else []

        if not all_entries:
            session.close()
            return jsonify({
                'subject_id': subject_id,
                'subject_title': subject.title,
                'totalStudents': 0,
                'averageScore': 0,
                'medianScore': 0,
                'minScore': 0,
                'maxScore': 0,
                'stdDeviation': 0,
                'passRate': 0,
                'scoreDistribution': {'0-5': 0, '5-10': 0, '10-15': 0, '15-20': 0},
                'papers': [],
                'attempts': [],
                'online_exams': online_exams_info,
            })

        scores = [e['score'] for e in all_entries]
        scores_sorted = sorted(scores)
        average  = sum(scores) / len(scores)
        n = len(scores_sorted)
        median   = scores_sorted[n // 2] if n % 2 == 1 else (scores_sorted[n // 2 - 1] + scores_sorted[n // 2]) / 2
        min_score = min(scores)
        max_score = max(scores)
        std_dev   = statistics.stdev(scores) if len(scores) > 1 else 0
        pass_rate = (sum(1 for s in scores if s >= 10) / len(scores)) * 100
        distribution = {
            '0-5':   sum(1 for s in scores if 0  <= s <  5),
            '5-10':  sum(1 for s in scores if 5  <= s < 10),
            '10-15': sum(1 for s in scores if 10 <= s < 15),
            '15-20': sum(1 for s in scores if 15 <= s <= 20)
        }

        session.close()
        return jsonify({
            'subject_id': subject_id,
            'subject_title': subject.title,
            'totalStudents': len(all_entries),
            'averageScore': round(average, 2),
            'medianScore': round(median, 2),
            'minScore': min_score,
            'maxScore': max_score,
            'stdDeviation': round(std_dev, 2),
            'passRate': round(pass_rate, 2),
            'scoreDistribution': distribution,
            'papers': papers_details,
            'attempts': attempts_details,
            'online_exams': online_exams_info,
        })

    except Exception as e:
        print(f"❌ Erreur get_subject_statistics: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

# ============================================================================
# ROUTES RÉCLAMATIONS - Amélioré : Limite 1 semaine, traitement IA
# ============================================================================

@app.route('/api/reclamations', methods=['GET'])
@paseto_required
def get_reclamations():
    try:
        user_id = get_current_user_id()
        session = get_session()

        user = session.query(User).filter_by(id=user_id).first()

        query = session.query(Reclamation).options(
            joinedload(Reclamation.student),
            joinedload(Reclamation.paper).joinedload(StudentPaper.subject),
            joinedload(Reclamation.attempt).joinedload(ExamAttempt.exam)
        )

        if user.role == UserRole.STUDENT:
            reclamations = query.filter_by(student_id=user_id).order_by(desc(Reclamation.created_at)).all()
        elif user.role == UserRole.PROFESSOR:
            # Un prof ne voit que les réclamations qui le concernent :
            # - copies papier qu'il a lui-même corrigées (corrected_by_id)
            # - examens en ligne qu'il a créés (created_by_id)
            paper_reclamation_ids = [
                r.id for r in session.query(Reclamation)
                .join(StudentPaper, Reclamation.paper_id == StudentPaper.id)
                .filter(StudentPaper.corrected_by_id == user_id)
                .all()
            ]
            online_reclamation_ids = [
                r.id for r in session.query(Reclamation)
                .join(ExamAttempt, Reclamation.attempt_id == ExamAttempt.id)
                .join(OnlineExam, ExamAttempt.exam_id == OnlineExam.id)
                .filter(OnlineExam.created_by_id == user_id)
                .all()
            ]
            visible_ids = list(set(paper_reclamation_ids + online_reclamation_ids))
            reclamations = query.filter(Reclamation.id.in_(visible_ids)).order_by(desc(Reclamation.created_at)).all() if visible_ids else []
        else:
            reclamations = query.order_by(desc(Reclamation.created_at)).all()

        reclamations_list = []
        for r in reclamations:
            # Résolution du titre selon le type
            if r.paper and r.paper.subject:
                subject_title = r.paper.subject.title
            elif r.attempt and r.attempt.exam:
                subject_title = r.attempt.exam.title
            else:
                subject_title = 'Sujet supprimé'

            reclamations_list.append({
                'id': r.id,
                'paper_id': r.paper_id,
                'attempt_id': r.attempt_id,
                'type': 'online_exam' if r.attempt_id else 'paper',
                'student_id': r.student_id,
                'student_name': r.student.full_name if r.student else 'Inconnu',
                'subject_title': subject_title,
                'exam_title': r.attempt.exam.title if r.attempt and r.attempt.exam else None,
                'attempt_score': r.attempt.score if r.attempt else None,
                'attempt_feedback': r.attempt.feedback if r.attempt else None,
                'reason': r.reason,
                'status': r.status.value,
                'response': r.response,
                'ia_decision': r.ia_decision,
                'ia_proposed_status': r.ia_proposed_status,
                'ia_proposed_score': r.ia_proposed_score,
                'ia_proposed_grade': r.ia_proposed_grade,
                'ia_proposed_reason': r.ia_proposed_reason,
                'ia_processed_at': r.ia_processed_at.isoformat() if r.ia_processed_at else None,
                'responded_by_id': r.responded_by_id,
                'responder_name': r.responder.full_name if r.responder else None,
                'created_at': r.created_at.isoformat() if r.created_at else None,
                'updated_at': r.updated_at.isoformat() if r.updated_at else None
            })

        session.close()
        return jsonify(reclamations_list)
    except Exception as e:
        print(f"❌ Erreur get_reclamations: {e}")
        import traceback; traceback.print_exc()
        try: session.rollback(); session.close()
        except: pass
        return jsonify({'error': str(e)}), 500

@app.route('/api/reclamations', methods=['POST'])
@paseto_required
def create_reclamation():
    try:
        user_id = get_current_user_id()
        session = get_session()

        user = session.query(User).filter_by(id=user_id).first()
        if user.role != UserRole.STUDENT:
            session.close()
            return jsonify({'error': 'Seuls les étudiants peuvent créer des réclamations'}), 403

        data = request.json
        paper_id   = data.get('paper_id')
        attempt_id = data.get('attempt_id')
        reason     = data.get('reason', '').strip()

        if not reason or (not paper_id and not attempt_id):
            session.close()
            return jsonify({'error': 'Données manquantes (reason + paper_id ou attempt_id)'}), 400

        if paper_id:
            # Réclamation sur copie papier
            paper = session.query(StudentPaper).filter_by(id=paper_id).first()
            if not paper:
                session.close()
                return jsonify({'error': 'Copie non trouvée'}), 404
            if paper.student_id != user_id:
                session.close()
                return jsonify({'error': 'Cette copie ne vous appartient pas'}), 403
            if paper.reclamation_window_end:
                rwe = paper.reclamation_window_end
                if rwe.tzinfo is None:
                    rwe = rwe.replace(tzinfo=timezone.utc)
                if rwe < utcnow():
                    session.close()
                    return jsonify({'error': 'Période de réclamation expirée (7 jours après correction)'}), 400
            existing = session.query(Reclamation).filter_by(paper_id=paper_id, status=ReclamationStatus.PENDING).first()
            if existing:
                session.close()
                return jsonify({'error': 'Une réclamation est déjà en cours pour cette copie'}), 400
            reclamation = Reclamation(paper_id=paper_id, student_id=user_id, reason=reason)

        else:
            # Réclamation sur examen en ligne
            attempt = session.query(ExamAttempt).filter_by(id=attempt_id, student_id=user_id).first()
            if not attempt:
                session.close()
                return jsonify({'error': 'Tentative non trouvée'}), 404
            if not attempt.corrected_at:
                session.close()
                return jsonify({'error': 'La copie n\'a pas encore été corrigée'}), 400
            # Fenêtre de 7 jours depuis la correction
            corrected = attempt.corrected_at
            if corrected.tzinfo is None:
                corrected = corrected.replace(tzinfo=timezone.utc)
            if utcnow() > corrected + timedelta(days=7):
                session.close()
                return jsonify({'error': 'Période de réclamation expirée (7 jours après correction)'}), 400
            existing = session.query(Reclamation).filter_by(attempt_id=attempt_id, status=ReclamationStatus.PENDING).first()
            if existing:
                session.close()
                return jsonify({'error': 'Une réclamation est déjà en cours pour cet examen'}), 400
            reclamation = Reclamation(attempt_id=attempt_id, student_id=user_id, reason=reason)

        session.add(reclamation)
        session.commit()
        reclamation_dict = reclamation.to_dict()
        session.close()
        return jsonify({'success': True, 'reclamation': reclamation_dict}), 201
    except Exception as e:
        print(f"❌ Erreur create_reclamation: {e}")
        try: session.rollback(); session.close()
        except: pass
        return jsonify({'error': str(e)}), 500

@app.route('/api/reclamations/<int:reclamation_id>', methods=['PUT'])
@paseto_required
def respond_reclamation(reclamation_id):
    try:
        user_id = get_current_user_id()
        session = get_session()

        user = session.query(User).filter_by(id=user_id).first()
        if user.role not in [UserRole.PROFESSOR, UserRole.ADMIN]:
            session.close()
            return jsonify({'error': 'Accès non autorisé'}), 403

        reclamation = session.query(Reclamation).filter_by(id=reclamation_id).first()
        if not reclamation:
            session.close()
            return jsonify({'error': 'Réclamation non trouvée'}), 404

        data = request.json
        status = data.get('status')
        response = data.get('response')
        new_score = data.get('new_score')

        if not status or status not in ['in_review', 'resolved', 'rejected']:
            session.close()
            return jsonify({'error': 'Statut invalide'}), 400

        reclamation.status = ReclamationStatus[status.upper()]
        reclamation.response = response
        reclamation.responded_by_id = user_id
        reclamation.updated_at = utcnow()

        if status == 'resolved' and new_score is not None:
            paper = session.query(StudentPaper).filter_by(id=reclamation.paper_id).first()
            if paper:
                history = CorrectionHistory(
                    paper_id=paper.id,
                    corrector_id=user_id,
                    old_score=paper.score,
                    new_score=new_score,
                    old_grade=paper.grade,
                    new_grade=f"Note modifiée suite à réclamation: {new_score}/20",
                    reason=f"Réclamation acceptée: {response}"
                )
                session.add(history)
                paper.score = new_score
                paper.corrected_at = utcnow()

        session.commit()

        reclamation_dict = {
            'id': reclamation.id,
            'status': reclamation.status.value,
            'response': reclamation.response,
            'updated_at': reclamation.updated_at.isoformat() if reclamation.updated_at else None
        }
        session.close()

        return jsonify({'success': True, 'reclamation': reclamation_dict})
    except Exception as e:
        print(f"❌ Erreur respond_reclamation: {e}")
        return jsonify({'error': str(e)}), 500

# NOUVEAU : Traitement IA des réclamations
@app.route('/api/reclamations/<int:reclamation_id>/process_ia', methods=['POST'])
@paseto_required
def process_reclamation_ia(reclamation_id):
    """Traiter une réclamation avec IA - Accepter/Rejeter + Recalcul note si acceptée"""
    try:
        user_id = get_current_user_id()
        session = get_session()

        user = session.query(User).filter_by(id=user_id).first()
        if user.role not in [UserRole.PROFESSOR, UserRole.ADMIN]:
            session.close()
            return jsonify({'error': 'Accès non autorisé'}), 403

        reclamation = session.query(Reclamation).filter_by(id=reclamation_id).first()
        if not reclamation:
            session.close()
            return jsonify({'error': 'Réclamation non trouvée'}), 404

        if reclamation.status != ReclamationStatus.PENDING:
            session.close()
            return jsonify({'error': 'Réclamation déjà traitée'}), 400

        paper = reclamation.paper
        attempt = reclamation.attempt

        if not paper and not attempt:
            session.close()
            return jsonify({'error': 'Réclamation sans copie associée — impossible d\'analyser.'}), 400

        # ── Construire le contexte selon le type de réclamation ──────────────
        if paper:
            # Réclamation sur copie papier
            subject = paper.subject
            original_score = paper.score or 0
            system_prompt = """Tu es un arbitre impartial pour les réclamations de notes d'examen.

Analyse la réclamation de l'étudiant, la copie originale, la correction originale et le barème.
Décide si la réclamation est valide.

Format de sortie OBLIGATOIRE:
=== DÉCISION ===
[RESOLVED ou REJECTED]

=== RAISON ===
[Explication détaillée]

=== NOUVELLE NOTE ===
Si RESOLVED: XX.XX/20
Si REJECTED: Note originale inchangée

=== NOUVELLE CORRECTION ===
Si RESOLVED: Correction révisée complète
Si REJECTED: Correction originale inchangée"""

            user_message = f"""SUJET: {subject.content if subject else 'N/A'}

BARÈME: {subject.rubric if subject else 'N/A'}

COPIE ÉTUDIANT: {paper.content}

CORRECTION ORIGINALE: {paper.grade} (Note: {paper.score}/20)

RÉCLAMATION ÉTUDIANT: {reclamation.reason}

Analyse et décide."""
        else:
            # Réclamation sur examen en ligne
            exam = attempt.exam
            original_score = attempt.score or 0
            answers_text = attempt.answers or 'Aucune réponse enregistrée'
            feedback_text = attempt.feedback or 'Aucune correction disponible'
            system_prompt = """Tu es un arbitre impartial pour les réclamations de notes d'examen en ligne.

Analyse la réclamation de l'étudiant, ses réponses soumises et la correction IA initiale.
Décide si la réclamation est valide.

Format de sortie OBLIGATOIRE:
=== DÉCISION ===
[RESOLVED ou REJECTED]

=== RAISON ===
[Explication détaillée]

=== NOUVELLE NOTE ===
Si RESOLVED: XX.XX/20
Si REJECTED: Note originale inchangée

=== NOUVELLE CORRECTION ===
Si RESOLVED: Commentaire de correction révisé
Si REJECTED: Correction originale inchangée"""

            user_message = f"""EXAMEN: {exam.title if exam else 'N/A'}
INSTRUCTIONS: {exam.instructions[:500] if exam and exam.instructions else 'N/A'}

RÉPONSES DE L'ÉTUDIANT: {answers_text[:3000]}

CORRECTION INITIALE: {feedback_text[:3000]} (Note: {original_score}/20)

RÉCLAMATION ÉTUDIANT: {reclamation.reason}

Analyse et décide."""

        ia_response = call_claude(system_prompt, user_message, temperature=0.1)

        # Extraire décision de l'IA
        decision_match = re.search(r'=== DÉCISION ===\n(RESOLVED|REJECTED)', ia_response)
        reason_match = re.search(r'=== RAISON ===\n(.*?)=== NOUVELLE NOTE ===', ia_response, re.DOTALL)
        new_score_match = re.search(r'=== NOUVELLE NOTE ===\n(.*?)=== NOUVELLE CORRECTION ===', ia_response, re.DOTALL)
        new_grade_match = re.search(r'=== NOUVELLE CORRECTION ===\n(.*)', ia_response, re.DOTALL)

        if not decision_match:
            session.close()
            return jsonify({'error': 'Réponse IA invalide'}), 500

        decision = decision_match.group(1)
        reason = reason_match.group(1).strip() if reason_match else ''
        new_grade = new_grade_match.group(1).strip() if new_grade_match else (paper.grade if paper else feedback_text[:200])

        new_score = original_score
        if decision == 'RESOLVED' and new_score_match:
            new_score_str = new_score_match.group(1).strip()
            new_score = extract_score_from_correction(new_score_str)

        # Stocker la proposition de l'IA sans appliquer automatiquement la décision
        reclamation.ia_decision = ia_response
        reclamation.ia_proposed_status = 'resolved' if decision == 'RESOLVED' else 'rejected'
        reclamation.ia_proposed_reason = reason
        reclamation.ia_proposed_grade = new_grade if decision == 'RESOLVED' else None
        reclamation.ia_proposed_score = new_score if decision == 'RESOLVED' else original_score
        reclamation.ia_processed_at = utcnow()
        # Ne change pas le statut (reste PENDING) — la décision finale doit être approuvée par un professeur
        reclamation.updated_at = utcnow()

        session.commit()
        session.close()

        return jsonify({'success': True, 'decision': decision, 'ia_response': ia_response})
    except Exception as e:
        print(f"❌ Erreur process_reclamation_ia: {e}")
        return jsonify({'error': str(e)}), 500

# ============================================================================
# ROUTES PROFESSEUR
# ============================================================================

# Endpoint: Appliquer la proposition IA (doit être approuvé par le professeur propriétaire du sujet)
@app.route('/api/reclamations/<int:reclamation_id>/apply_proposal', methods=['POST'])
@paseto_required
def apply_ai_proposal(reclamation_id):
    try:
        user_id = get_current_user_id()
        session = get_session()

        user = session.query(User).filter_by(id=user_id).first()
        reclamation = session.query(Reclamation).options(joinedload(Reclamation.paper).joinedload(StudentPaper.subject)).filter_by(id=reclamation_id).first()
        if not reclamation:
            session.close()
            return jsonify({'error': 'Réclamation introuvable'}), 404

        paper = reclamation.paper
        if not paper:
            session.close()
            return jsonify({'error': "Impossible d'appliquer une proposition IA sur une réclamation d'examen en ligne. Répondez manuellement."}), 400
        subject = paper.subject

        # Permissions: admin or professeur propriétaire du sujet
        if user.role != UserRole.ADMIN and not (user.role == UserRole.PROFESSOR and subject and subject.creator_id == user_id):
            session.close()
            return jsonify({'error': 'Accès non autorisé'}), 403

        # Vérifier qu'il existe une proposition IA
        if not reclamation.ia_proposed_status:
            session.close()
            return jsonify({'error': 'Aucune proposition IA disponible pour cette réclamation'}), 400

        # Appliquer la proposition IA
        old_score = paper.score
        old_grade = paper.grade
        new_score = reclamation.ia_proposed_score or old_score
        new_grade = reclamation.ia_proposed_grade or old_grade

        history = CorrectionHistory(
            paper_id=paper.id,
            corrector_id=user_id,
            old_score=old_score,
            new_score=new_score,
            old_grade=old_grade,
            new_grade=new_grade,
            reason=f"Application de la proposition IA: {reclamation.ia_proposed_reason or 'N/A'}"
        )
        session.add(history)

        paper.score = new_score
        paper.grade = new_grade
        paper.corrected_at = utcnow()

        reclamation.status = ReclamationStatus.RESOLVED
        reclamation.response = reclamation.ia_proposed_reason or 'Proposition IA acceptée'
        reclamation.responded_by_id = user_id
        reclamation.updated_at = utcnow()

        session.commit()
        session.close()

        return jsonify({'success': True, 'message': 'Proposition IA appliquée'})

    except Exception as e:
        print(f"❌ Erreur apply_ai_proposal: {e}")
        return jsonify({'error': str(e)}), 500


# Endpoint: Rejeter la proposition IA (professeur)
@app.route('/api/reclamations/<int:reclamation_id>/reject_proposal', methods=['POST'])
@paseto_required
def reject_ai_proposal(reclamation_id):
    try:
        user_id = get_current_user_id()
        session = get_session()

        user = session.query(User).filter_by(id=user_id).first()
        reclamation = session.query(Reclamation).options(joinedload(Reclamation.paper).joinedload(StudentPaper.subject)).filter_by(id=reclamation_id).first()
        if not reclamation:
            session.close()
            return jsonify({'error': 'Réclamation introuvable'}), 404

        paper = reclamation.paper
        if not paper:
            session.close()
            return jsonify({'error': "Impossible de rejeter une proposition IA sur une réclamation d'examen en ligne. Répondez manuellement."}), 400
        subject = paper.subject

        # Permissions: admin or professeur propriétaire du sujet
        if user.role != UserRole.ADMIN and not (user.role == UserRole.PROFESSOR and subject and subject.creator_id == user_id):
            session.close()
            return jsonify({'error': 'Accès non autorisé'}), 403

        # Rejeter la proposition IA
        reclamation.status = ReclamationStatus.REJECTED
        payload = request.get_json() or {}
        reclamation.response = payload.get('response', 'Proposition IA rejetée par le professeur')
        reclamation.responded_by_id = user_id
        reclamation.updated_at = utcnow()

        session.commit()
        session.close()

        return jsonify({'success': True, 'message': 'Proposition IA rejetée'})

    except Exception as e:
        print(f"❌ Erreur reject_ai_proposal: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/professor/dashboard', methods=['GET'])
@paseto_required
def professor_dashboard():
    try:
        user_id = get_current_user_id()
        session = get_session()

        user = session.query(User).filter_by(id=user_id).first()
        if user.role != UserRole.PROFESSOR:
            session.close()
            return jsonify({'error': 'Accès réservé aux professeurs'}), 403

        my_subjects = session.query(Subject).filter_by(creator_id=user_id).count()

        # Copies papier corrigées
        papers_corrected = session.query(StudentPaper).filter_by(corrected_by_id=user_id).count()

        # Examens en ligne corrigés (attempts avec score, créés par ce prof)
        online_corrected = session.query(ExamAttempt).join(
            OnlineExam, ExamAttempt.exam_id == OnlineExam.id
        ).filter(
            OnlineExam.created_by_id == user_id,
            ExamAttempt.score.isnot(None)
        ).count()

        dashboard_data = {
            'my_subjects': my_subjects,
            'papers_corrected': papers_corrected + online_corrected
        }

        session.close()
        return jsonify(dashboard_data)
    except Exception as e:
        print(f"❌ Erreur professor_dashboard: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

# ============================================================================
# ROUTES ÉTUDIANT
# ============================================================================

@app.route('/api/student/online_results', methods=['GET'])
@paseto_required
def get_student_online_results():
    """Résultats des examens en ligne corrigés pour l'étudiant connecté."""
    try:
        user_id = get_current_user_id()
        session = get_session()
        user = session.query(User).filter_by(id=user_id).first()
        if not user or user.role != UserRole.STUDENT:
            session.close()
            return jsonify([])

        attempts = session.query(ExamAttempt).options(
            joinedload(ExamAttempt.exam).joinedload(OnlineExam.subject)
        ).filter(
            ExamAttempt.student_id == user_id,
            ExamAttempt.corrected_at != None
        ).order_by(desc(ExamAttempt.corrected_at)).all()

        results = []
        for att in attempts:
            exam    = att.exam
            subject = exam.subject if exam else None
            # Vérifier s'il existe déjà une réclamation
            existing_reclamation = session.query(Reclamation).filter_by(
                attempt_id=att.id, student_id=user_id
            ).first()
            results.append({
                'attempt_id':    att.id,
                'exam_id':       att.exam_id,
                'exam_title':    exam.title if exam else '—',
                'subject_title': subject.title if subject else None,
                'score':         att.score,
                'feedback':      att.feedback,
                'corrected_at':  att.corrected_at.isoformat() if att.corrected_at else None,
                'submitted_at':  att.submitted_at.isoformat() if att.submitted_at else None,
                'auto_correct':  exam.auto_correct if exam else False,
                'has_reclamation': existing_reclamation is not None,
                'reclamation_status': existing_reclamation.status.value if existing_reclamation else None
            })

        session.close()
        return jsonify(results)
    except Exception as e:
        print(f"❌ get_student_online_results: {e}")
        return jsonify([])


@app.route('/api/student/papers', methods=['GET'])
@paseto_required
def get_student_papers():
    try:
        user_id = get_current_user_id()
        session = get_session()

        user = session.query(User).filter_by(id=user_id).first()
        if user.role != UserRole.STUDENT:
            session.close()
            return jsonify({'error': 'Accès réservé aux étudiants'}), 403

        papers = session.query(StudentPaper).options(
            joinedload(StudentPaper.subject)
        ).filter_by(student_id=user_id).order_by(desc(StudentPaper.created_at)).all()

        # Pré-charger les réclamations pour éviter N+1
        paper_ids = [p.id for p in papers]
        reclamations_by_paper = {}
        if paper_ids:
            recs = session.query(Reclamation).filter(
                Reclamation.paper_id.in_(paper_ids),
                Reclamation.student_id == user_id
            ).all()
            for r in recs:
                reclamations_by_paper[r.paper_id] = r

        papers_list = []
        for paper in papers:
            d = paper.to_dict()
            rec = reclamations_by_paper.get(paper.id)
            d['has_reclamation']     = rec is not None
            d['reclamation_status']  = rec.status.value if rec else None
            papers_list.append(d)

        session.close()
        return jsonify(papers_list)
    except Exception as e:
        print(f"❌ Erreur get_student_papers: {e}")
        return jsonify({'error': str(e)}), 500

# ============================================================================
# MAIN
# ============================================================================

# Enregistrer route export PDF
register_export_route(app)
register_csv_routes(app)

@app.route('/api/online_exams', methods=['GET'])
@paseto_required
def get_online_exams():
    """Liste des examens en ligne"""
    try:
        user_id = get_current_user_id()
        session = get_session()
        user = session.query(User).filter_by(id=user_id).first()
        
        query = session.query(OnlineExam).options(
            joinedload(OnlineExam.subject),
            joinedload(OnlineExam.creator)
        )
        
        if user.role == UserRole.STUDENT:
            # Étudiants : examens actifs/planifiés + examens terminés (participé OU fermés dans les 7 derniers jours)
            active_exams = query.filter(OnlineExam.status.in_([ExamStatus.SCHEDULED, ExamStatus.ACTIVE])).all()
            participated_ids = set(
                a.exam_id for a in session.query(ExamAttempt.exam_id)
                .filter_by(student_id=user_id).all()
            )
            recent_cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=7)
            recent_closed_ids = set(
                r.id for r in session.query(OnlineExam.id).filter(
                    OnlineExam.status == ExamStatus.CLOSED,
                    OnlineExam.end_time >= recent_cutoff
                ).all()
            )
            all_closed_ids = participated_ids | recent_closed_ids
            closed_exams = query.filter(
                OnlineExam.id.in_(list(all_closed_ids)),
                OnlineExam.status == ExamStatus.CLOSED
            ).all() if all_closed_ids else []
            exams = active_exams + closed_exams
        elif user.role == UserRole.PROFESSOR:
            # Professeurs : leurs propres examens
            exams = query.filter_by(created_by_id=user_id).all()
        else:
            # Admin : tous
            exams = query.all()

        # Pré-charger toutes les tentatives de l'étudiant en une seule requête (évite N+1)
        attempts_by_exam = {}
        if user.role == UserRole.STUDENT:
            student_attempts = session.query(ExamAttempt).filter_by(student_id=user_id).all()
            attempts_by_exam = {a.exam_id: a for a in student_attempts}

        # Auto-close exams whose end_time has passed
        now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
        needs_commit = False
        for exam in exams:
            if exam.status == ExamStatus.ACTIVE and exam.end_time and exam.end_time < now_utc:
                exam.status = ExamStatus.CLOSED
                in_progress = session.query(ExamAttempt).filter_by(
                    exam_id=exam.id, status=AttemptStatus.IN_PROGRESS
                ).all()
                for att in in_progress:
                    att.status = AttemptStatus.AUTO_SUBMITTED
                    att.submitted_at = now_utc
                needs_commit = True
                print(f"⏰ Auto-close examen #{exam.id} '{exam.title}' (end_time dépassé)")
        if needs_commit:
            session.commit()
            # Recharger les tentatives après commit
            if user.role == UserRole.STUDENT:
                student_attempts = session.query(ExamAttempt).filter_by(student_id=user_id).all()
                attempts_by_exam = {a.exam_id: a for a in student_attempts}

        exams_list = []
        for exam in exams:
            d = exam.to_dict()
            if user.role == UserRole.STUDENT:
                attempt = attempts_by_exam.get(exam.id)
                if attempt:
                    d['my_attempt'] = {
                        'id':           attempt.id,
                        'status':       attempt.status.value,
                        'score':        attempt.score,
                        'feedback':     attempt.feedback,
                        'corrected_at': attempt.corrected_at.isoformat() if attempt.corrected_at else None,
                        'submitted_at': attempt.submitted_at.isoformat() if attempt.submitted_at else None,
                    }
                else:
                    d['my_attempt'] = None
            exams_list.append(d)
        session.close()
        return jsonify(exams_list)
    except Exception as e:
        print(f"❌ Erreur get_online_exams: {e}")
        try: session.rollback(); session.close()
        except: pass
        return jsonify({'error': str(e)}), 500

# Exemple pour l'endpoint create_online_exam (ligne ~1570)
@app.route('/api/online_exams', methods=['POST'])
@paseto_required
def create_online_exam():
    """Créer un examen en ligne — le frontend envoie déjà du UTC via toISOString()"""
    try:
        user_id = get_current_user_id()
        session = get_session()

        user = session.query(User).filter_by(id=user_id).first()
        if user.role not in [UserRole.PROFESSOR, UserRole.ADMIN]:
            session.close()
            return jsonify({'error': 'Accès réservé aux professeurs et administrateurs'}), 403

        data = request.json

        # Validation
        required_fields = ['subject_id', 'title', 'start_time', 'end_time']
        for field in required_fields:
            if field not in data:
                session.close()
                return jsonify({'error': f'Le champ "{field}" est requis'}), 400

        # Vérifier le sujet
        subject = session.query(Subject).filter_by(id=data['subject_id']).first()
        if not subject:
            session.close()
            return jsonify({'error': 'Le sujet sélectionné n\'existe pas'}), 404

        # Le frontend envoie la valeur brute du datetime-local + "Z"
        # (ex: "2026-03-30T18:05:00Z"). Dakar = UTC+0, donc la valeur
        # saisie EST déjà l'heure UTC. On parse puis on stocke naïf (sans tzinfo)
        # pour éviter que psycopg2 ne convertisse en Europe/Berlin avant stockage.
        try:
            raw_start = data['start_time'].strip().replace('Z', '+00:00')
            raw_end   = data['end_time'].strip().replace('Z', '+00:00')
            if '+' not in raw_start and raw_start[-6:] != '+00:00':
                raw_start += '+00:00'
            if '+' not in raw_end and raw_end[-6:] != '+00:00':
                raw_end += '+00:00'
            # Convertir en UTC puis supprimer tzinfo → stockage naïf UTC dans PG
            start_time = datetime.fromisoformat(raw_start).astimezone(timezone.utc).replace(tzinfo=None)
            end_time   = datetime.fromisoformat(raw_end).astimezone(timezone.utc).replace(tzinfo=None)

            # Validation : Fin > Début
            if end_time <= start_time:
                session.close()
                return jsonify({'error': 'La date de fin doit être après la date de début'}), 400

            # Calcul durée auto en minutes
            duration_minutes = int((end_time - start_time).total_seconds() / 60)
            if duration_minutes <= 0 or duration_minutes > 1440:  # Max 24h
                session.close()
                return jsonify({'error': 'Durée invalide (doit être entre 1 min et 24h)'}), 400

        except ValueError as ve:
            session.close()
            return jsonify({'error': f'Format de date invalide: {str(ve)}'}), 400
       
        # Créer l'examen avec durée calculée
        exam = OnlineExam(
            subject_id=data['subject_id'],
            title=data['title'],
            instructions=data.get('instructions', ''),
            duration_minutes=duration_minutes,  # Auto-calculé
            start_time=start_time,
            end_time=end_time,
            max_tab_switches=data.get('max_tab_switches', 2),
            enable_copy_paste=data.get('enable_copy_paste', False),
            enable_right_click=data.get('enable_right_click', False),
            randomize_questions=data.get('randomize_questions', False),
            max_no_face_count=data.get('max_no_face_count', 10),
            ban_on_devtools=data.get('ban_on_devtools', True),
            auto_correct=data.get('auto_correct', False),
            status=ExamStatus.SCHEDULED,
            created_by_id=user_id
        )
       
        session.add(exam)
        session.commit()
        exam_dict = exam.to_dict()
        print(f"✅ Examen créé: {exam.title} stocké de {start_time} à {end_time} UTC (durée: {duration_minutes} min)")
        session.close()
       
        return jsonify({'success': True, 'exam': exam_dict}), 201
    except Exception as e:
        print(f"❌ Erreur create_online_exam: {e}")
        return jsonify({'error': 'Erreur lors de la création de l\'examen'}), 500

@app.route('/api/online_exams/<int:exam_id>/activate', methods=['POST'])
@paseto_required
def activate_online_exam(exam_id):
    """Activer un examen (le rendre disponible aux étudiants)"""
    try:
        user_id = get_current_user_id()
        session = get_session()
        
        user = session.query(User).filter_by(id=user_id).first()
        if user.role not in [UserRole.PROFESSOR, UserRole.ADMIN]:
            session.close()
            return jsonify({'error': 'Accès non autorisé'}), 403
        
        exam = session.query(OnlineExam).filter_by(id=exam_id).first()
        if not exam:
            session.close()
            return jsonify({'error': 'Examen non trouvé'}), 404
        
        # Vérifier propriété (prof) ou admin
        if user.role == UserRole.PROFESSOR and exam.created_by_id != user_id:
            session.close()
            return jsonify({'error': 'Vous ne pouvez activer que vos propres examens'}), 403
        
        # Passer en statut ACTIVE
        exam.status = ExamStatus.ACTIVE
        session.commit()
        
        exam_dict = exam.to_dict()

        # Notifier par email tous les étudiants inscrits à la formation de l'examen
        try:
            app_url  = os.getenv('APP_URL', 'https://cei.ec2lt.sn').rstrip('/')
            exam_url = f"{app_url}/app"
            end_str  = exam.end_time.strftime('%d/%m/%Y à %H:%M') if exam.end_time else 'voir sur la plateforme'
            from models import StudentUEEnrollment, EC as ECModel, UE as UEModel
            # Récupérer les EC liés à cet examen
            ec = session.query(ECModel).filter_by(id=exam.ec_id).first() if hasattr(exam, 'ec_id') and exam.ec_id else None
            if ec and ec.ue:
                enrollments = session.query(StudentUEEnrollment).filter_by(ue_id=ec.ue_id).all()
                for enr in enrollments:
                    student = enr.student
                    if student and student.email and student.is_active:
                        try:
                            send_exam_started_email(student.email, student.full_name, exam.title, exam_url, end_str)
                        except Exception:
                            pass
        except Exception:
            pass

        session.close()
        return jsonify({'success': True, 'exam': exam_dict})
    except Exception as e:
        print(f"Erreur activate_online_exam: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/online_exams/<int:exam_id>/extend', methods=['POST'])
@paseto_required
def extend_online_exam(exam_id):
    try:
        user_id = get_current_user_id()
        session = get_session()
        user = session.query(User).filter_by(id=user_id).first()
        if user.role not in [UserRole.PROFESSOR, UserRole.ADMIN]:
            session.close()
            return jsonify({'error': 'Accès non autorisé'}), 403
        exam = session.query(OnlineExam).filter_by(id=exam_id).first()
        if not exam:
            session.close()
            return jsonify({'error': 'Examen non trouvé'}), 404
        if user.role == UserRole.PROFESSOR and exam.created_by_id != user_id:
            session.close()
            return jsonify({'error': 'Vous ne pouvez modifier que vos propres examens'}), 403
        if exam.status not in [ExamStatus.ACTIVE, ExamStatus.SCHEDULED]:
            session.close()
            return jsonify({'error': 'Impossible de modifier un examen terminé'}), 400
        data = request.json or {}
        extra_minutes = int(data.get('extra_minutes', 0))
        if extra_minutes <= 0 or extra_minutes > 300:
            session.close()
            return jsonify({'error': 'Durée invalide (1–300 minutes)'}), 400
        exam.end_time = exam.end_time + timedelta(minutes=extra_minutes)
        exam.duration_minutes = exam.duration_minutes + extra_minutes
        session.commit()
        exam_dict = exam.to_dict()
        session.close()
        return jsonify({
            'success': True,
            'message': f'Durée prolongée de {extra_minutes} minutes',
            'new_end_time': exam_dict.get('end_time'),
            'new_duration_minutes': exam_dict.get('duration_minutes'),
        })
    except Exception as e:
        print(f"❌ Erreur extend_online_exam: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/online_exams/<int:exam_id>/close', methods=['POST'])
@paseto_required
def close_online_exam(exam_id):
    """Fermer un examen (terminer)"""
    try:
        user_id = get_current_user_id()
        session = get_session()
        
        user = session.query(User).filter_by(id=user_id).first()
        if user.role not in [UserRole.PROFESSOR, UserRole.ADMIN]:
            session.close()
            return jsonify({'error': 'Accès non autorisé'}), 403
        
        exam = session.query(OnlineExam).filter_by(id=exam_id).first()
        if not exam:
            session.close()
            return jsonify({'error': 'Examen non trouvé'}), 404
        
        if user.role == UserRole.PROFESSOR and exam.created_by_id != user_id:
            session.close()
            return jsonify({'error': 'Accès non autorisé'}), 403
        
        # Passer en statut CLOSED
        exam.status = ExamStatus.CLOSED
        session.commit()

        exam_dict      = exam.to_dict()
        prof_email     = user.email
        prof_name      = user.full_name or user.username
        exam_id_local  = exam.id

        session.close()

        # Envoyer le résumé par email en arrière-plan
        import threading as _threading
        _threading.Thread(
            target=_send_exam_closure_summary,
            args=(exam_id_local, prof_email, prof_name),
            daemon=True
        ).start()

        return jsonify({'success': True, 'exam': exam_dict})
    except Exception as e:
        print(f"❌ Erreur close_online_exam: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/admin/online_exams/<int:exam_id>', methods=['PUT'])
@paseto_required
def edit_online_exam(exam_id):
    """Modifier le titre, la date de début et la durée d'un examen (draft/scheduled)"""
    session = None
    try:
        user_id = get_current_user_id()
        session = get_session()
        user = session.query(User).filter_by(id=user_id).first()
        if user.role not in [UserRole.PROFESSOR, UserRole.ADMIN]:
            session.close()
            return jsonify({'error': 'Accès non autorisé'}), 403
        exam = session.query(OnlineExam).filter_by(id=exam_id).first()
        if not exam:
            session.close()
            return jsonify({'error': 'Examen non trouvé'}), 404
        if user.role == UserRole.PROFESSOR and exam.created_by_id != user_id:
            session.close()
            return jsonify({'error': 'Vous ne pouvez modifier que vos propres examens'}), 403
        if exam.status not in [ExamStatus.DRAFT, ExamStatus.SCHEDULED]:
            session.close()
            return jsonify({'error': 'Seuls les examens en brouillon ou planifiés peuvent être modifiés'}), 400
        data = request.get_json() or {}
        if 'title' in data and data['title']:
            exam.title = data['title'].strip()
        if 'start_time' in data and data['start_time']:
            from datetime import datetime
            try:
                exam.start_time = datetime.fromisoformat(data['start_time'])
            except ValueError:
                pass
        if 'duration_minutes' in data:
            exam.duration_minutes = max(5, int(data['duration_minutes']))
        session.commit()
        session.close()
        return jsonify({'success': True})
    except Exception as e:
        if session:
            session.rollback()
            session.close()
        print(f"❌ Erreur edit_online_exam: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/online_exams/<int:exam_id>', methods=['DELETE'])
@paseto_required
def delete_online_exam(exam_id):
    """Supprimer un examen en ligne (admin/professeur propriétaire uniquement)"""
    session = None
    try:
        user_id = get_current_user_id()
        session = get_session()
        
        user = session.query(User).filter_by(id=user_id).first()
        if user.role not in [UserRole.PROFESSOR, UserRole.ADMIN]:
            session.close()
            return jsonify({'error': 'Accès non autorisé'}), 403
        
        exam = session.query(OnlineExam).filter_by(id=exam_id).first()
        if not exam:
            session.close()
            return jsonify({'error': 'Examen non trouvé'}), 404
        
        # Vérifier propriété (prof) ou admin
        if user.role == UserRole.PROFESSOR and exam.created_by_id != user_id:
            session.close()
            return jsonify({'error': 'Vous ne pouvez supprimer que vos propres examens'}), 403

        # Suppression explicite dans l'ordre pour éviter les violations de clés
        # étrangères, notamment proctor_assignments -> exam_attempts/online_exams.
        attempt_ids = [a.id for a in session.query(ExamAttempt.id).filter_by(exam_id=exam_id).all()]
        session.query(ProctorAssignment).filter_by(exam_id=exam_id).delete(synchronize_session=False)

        if attempt_ids:
            session.query(CameraLog).filter(
                CameraLog.attempt_id.in_(attempt_ids)
            ).delete(synchronize_session=False)
            session.query(ExamActivityLog).filter(
                ExamActivityLog.attempt_id.in_(attempt_ids)
            ).delete(synchronize_session=False)
            session.query(Reclamation).filter(
                Reclamation.attempt_id.in_(attempt_ids)
            ).delete(synchronize_session=False)
            session.query(ExamAttempt).filter(
                ExamAttempt.id.in_(attempt_ids)
            ).delete(synchronize_session=False)

        session.query(ExamProctor).filter_by(exam_id=exam_id).delete(synchronize_session=False)
        session.delete(exam)
        session.commit()
        session.close()

        return jsonify({'success': True, 'message': 'Examen supprimé avec succès'})
    except Exception as e:
        if session:
            session.rollback()
            session.close()
        print(f"❌ Erreur delete_online_exam: {e}")
        import traceback; traceback.print_exc()
        return jsonify({'error': str(e)}), 500

@app.route('/api/online_exams/<int:exam_id>/details', methods=['GET'])
@paseto_required
def get_online_exam_details(exam_id):
    """Récupérer les détails complets d'un examen (avec contenu du sujet) - Pour composition étudiants"""
    try:
        user_id = get_current_user_id()
        session = get_session()
        
        user = session.query(User).filter_by(id=user_id).first()
        
        exam = session.query(OnlineExam).options(
            joinedload(OnlineExam.subject),
            joinedload(OnlineExam.creator)
        ).filter_by(id=exam_id).first()
        
        if not exam:
            session.close()
            return jsonify({'error': 'Examen non trouvé'}), 404
        
        # Vérifier les permissions
        if user.role == UserRole.STUDENT:
            # Étudiants : seulement les examens actifs dans la plage horaire
            now = utcnow()
            start_time = exam.start_time if exam.start_time.tzinfo else exam.start_time.replace(tzinfo=timezone.utc)
            end_time = exam.end_time if exam.end_time.tzinfo else exam.end_time.replace(tzinfo=timezone.utc)
            
            if exam.status != ExamStatus.ACTIVE or now < start_time or now > end_time:
                session.close()
                return jsonify({'error': 'Examen non disponible actuellement'}), 403
        
        elif user.role == UserRole.PROFESSOR:
            # Professeurs : seulement leurs propres examens
            if exam.created_by_id != user_id:
                session.close()
                return jsonify({'error': 'Accès non autorisé'}), 403
        
        # Préparer la réponse avec les détails complets
        exam_dict = exam.to_dict()
        
        # Ajouter le contenu du sujet (sans le barème pour les étudiants)
        if exam.subject:
            content = exam.subject.content or ''
            # Pour les étudiants, retirer la section barème si elle est incluse dans content
            if user.role == UserRole.STUDENT:
                content = _strip_bareme_from_content(content)
            subject_content = {
                'id': exam.subject.id,
                'title': exam.subject.title,
                'content': content,
            }
            # Barème (contient les réponses) réservé aux professeurs/admins
            if user.role in [UserRole.PROFESSOR, UserRole.ADMIN]:
                subject_content['rubric'] = exam.subject.rubric
            exam_dict['subject_content'] = subject_content
        
        session.close()
        return jsonify(exam_dict)
        
    except Exception as e:
        print(f"❌ Erreur get_online_exam_details: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

@app.route('/api/online_exams/<int:exam_id>/start', methods=['POST'])
@paseto_required
def start_exam_attempt(exam_id):
    """Démarrer une tentative d'examen (étudiant)"""
    try:
        user_id = get_current_user_id()
        session = get_session()
        
        user = session.query(User).filter_by(id=user_id).first()
        if user.role != UserRole.STUDENT:
            session.close()
            return jsonify({'error': 'Accès réservé aux étudiants'}), 403
        
        exam = session.query(OnlineExam).filter_by(id=exam_id).first()
        if not exam:
            session.close()
            return jsonify({'error': 'Examen non trouvé'}), 404
        
        now = utcnow()

        # S'assurer que les datetime sont timezone-aware pour la comparaison
        start_time = exam.start_time if exam.start_time.tzinfo else exam.start_time.replace(tzinfo=timezone.utc)
        end_time = exam.end_time if exam.end_time.tzinfo else exam.end_time.replace(tzinfo=timezone.utc)

        # Vérifier la plage horaire d'abord
        if now < start_time:
            start_str = start_time.strftime('%d/%m/%Y à %H:%M') + ' UTC'
            session.close()
            return jsonify({
                'error': f"L'examen n'a pas encore commencé. Il débutera le {start_str}.",
                'starts_at': start_time.isoformat()
            }), 400
        if now > end_time:
            session.close()
            return jsonify({'error': 'Cet examen est terminé'}), 400

        if exam.status == ExamStatus.SCHEDULED:
            session.close()
            return jsonify({'error': "Cet examen n'a pas encore été activé par votre professeur. Veuillez patienter."}), 400
        elif exam.status != ExamStatus.ACTIVE:
            session.close()
            return jsonify({'error': 'Examen non disponible actuellement'}), 400
        
        # Vérifier tentative existante
        existing = session.query(ExamAttempt).filter_by(
            exam_id=exam_id,
            student_id=user_id
        ).first()
        
        if existing:
            if existing.status == AttemptStatus.BANNED:
                session.close()
                return jsonify({'error': 'Vous êtes banni de cet examen', 'banned': True}), 403
            if existing.status in [AttemptStatus.SUBMITTED, AttemptStatus.AUTO_SUBMITTED]:
                session.close()
                return jsonify({'error': 'Vous avez déjà soumis cet examen'}), 400
            # Si IN_PROGRESS, continuer
            attempt_dict = existing.to_dict()
            session.close()
            return jsonify({'success': True, 'attempt': attempt_dict, 'continuing': True})
        
        # Signature pré-examen transmise par le frontend
        import json as _json
        body = request.get_json(silent=True) or {}
        pre_sig      = body.get('pre_exam_signature')
        pre_sig_meta = body.get('pre_exam_signature_meta')

        # Validation côté serveur de la qualité de la signature
        if pre_sig_meta:
            try:
                meta = pre_sig_meta if isinstance(pre_sig_meta, dict) else _json.loads(pre_sig_meta)
                strokes    = int(meta.get('strokes', 0))
                path_len   = float(meta.get('path_length', 0))
                duration   = int(meta.get('duration_ms', 0))
                if strokes < 2 or path_len < 80 or duration < 600:
                    session.close()
                    return jsonify({
                        'error': 'Signature non conforme. Veuillez tracer une signature complète (plusieurs traits, durée suffisante).',
                        'signature_invalid': True
                    }), 400
            except Exception:
                pass  # meta malformé → on laisse passer, le frontend a déjà validé

        meta_str = _json.dumps(pre_sig_meta) if isinstance(pre_sig_meta, dict) else pre_sig_meta

        # Créer nouvelle tentative
        attempt = ExamAttempt(
            exam_id=exam_id,
            student_id=user_id,
            status=AttemptStatus.IN_PROGRESS,
            answers='{}',
            pre_exam_signature_data=pre_sig,
            pre_exam_signature_meta=meta_str
        )
        session.add(attempt)
        session.flush()  # obtenir attempt.id avant commit

        # Lier la pré-affectation surveillant si elle existe
        pre = session.query(ProctorAssignment).filter_by(
            exam_id=exam_id, student_id=user_id, attempt_id=None
        ).first()
        if pre:
            pre.attempt_id = attempt.id

        session.commit()
        attempt_dict = attempt.to_dict()
        session.close()

        return jsonify({'success': True, 'attempt': attempt_dict}), 201
    except Exception as e:
        print(f"❌ Erreur start_exam_attempt: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

@app.route('/api/exam_attempts/<int:attempt_id>/save', methods=['POST'])
@paseto_required
def save_exam_answers(attempt_id):
    """Sauvegarder les réponses en temps réel"""
    try:
        user_id = get_current_user_id()
        session = get_session()
        
        attempt = session.query(ExamAttempt).filter_by(id=attempt_id, student_id=user_id).first()
        if not attempt:
            session.close()
            return jsonify({'error': 'Tentative non trouvée'}), 404
        
        if attempt.status != AttemptStatus.IN_PROGRESS:
            session.close()
            return jsonify({'error': 'Impossible de modifier une tentative terminée'}), 400
        
        data = request.json
        attempt.answers = data.get('answers', '{}')
        
        session.commit()
        session.close()
        
        return jsonify({'success': True, 'message': 'Réponses sauvegardées'})
    except Exception as e:
        print(f"❌ Erreur save_exam_answers: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/exam_attempts/<int:attempt_id>/log_activity', methods=['POST'])
@paseto_required
def log_exam_activity(attempt_id):
    """Logger une activité suspecte avec gestion améliorée des violations"""
    try:
        user_id = get_current_user_id()
        session = get_session()
        
        attempt = session.query(ExamAttempt).filter_by(id=attempt_id, student_id=user_id).first()
        if not attempt:
            session.close()
            return jsonify({'error': 'Tentative non trouvée'}), 404
        
        data = request.json
        event_type = data.get('event_type', 'unknown')
        event_data = data.get('event_data', '')
        
        # Logger l'activité
        log = ExamActivityLog(
            attempt_id=attempt_id,
            event_type=event_type,
            event_data=event_data
        )
        session.add(log)
        
        exam = attempt.exam
        severity_tab_events   = ['tab_switch', 'fullscreen_exit', 'window_blur']
        severity_medium_events = ['right_click', 'copy_attempt', 'paste_attempt', 'f12_attempt']

        ban_reason = None

        # ── 1. Outils développeur ─────────────────────────────────────────────
        if event_type == 'devtools_attempt':
            ban_on_dev = exam.ban_on_devtools if exam.ban_on_devtools is not None else True
            attempt.tab_switches += 1
            attempt.warnings_count += 2
            if ban_on_dev:
                ban_reason = "Ouverture des outils développeur détectée"

        # ── 2. Changements de fenêtre / onglet / plein écran ──────────────────
        elif event_type in severity_tab_events:
            attempt.tab_switches += 1
            attempt.warnings_count += 2
            max_sw = exam.max_tab_switches if exam.max_tab_switches is not None else 2
            if max_sw >= 0 and attempt.tab_switches > max_sw:
                ban_reason = f"Trop de changements de contexte : {attempt.tab_switches} (seuil : {max_sw})"

        # ── 3. Photo de référence — stocker dans CameraLog si image fournie ────
        elif event_type == 'face_reference_captured':
            # event_data peut être une chaîne texte OU un JSON avec image_data base64
            import json as _json
            photo_b64 = None
            try:
                parsed = _json.loads(event_data) if isinstance(event_data, str) else event_data
                if isinstance(parsed, dict):
                    photo_b64 = parsed.get('image_data') or parsed.get('photo')
                    log.event_data = parsed.get('label', event_data)
            except Exception:
                pass
            if photo_b64:
                cam_log = CameraLog(
                    attempt_id=attempt_id,
                    event_type='face_reference_captured',
                    violation_type='face_reference',
                    image_data=photo_b64
                )
                session.add(cam_log)

        # ── 4. Visage non détecté (face_absent = alias FaceDetector.js) ──────
        elif event_type in ('no_face_detected', 'face_absent'):
            no_face_count = (attempt.no_face_count or 0) + 1
            attempt.no_face_count = no_face_count
            attempt.warnings_count += 1
            max_nf = exam.max_no_face_count if exam.max_no_face_count is not None else 10
            if max_nf >= 0 and no_face_count >= max_nf:
                ban_reason = f"Visage absent trop souvent : {no_face_count} fois (seuil : {max_nf})"

        # ── 3b. Plusieurs visages détectés ────────────────────────────────────
        elif event_type == 'multiple_faces':
            attempt.warnings_count += 2
            no_face_count = attempt.no_face_count or 0
            attempt.tab_switches += 1  # compte comme violation grave

        # ── 4. Violations mineures ────────────────────────────────────────────
        elif event_type in severity_medium_events:
            attempt.warnings_count += 1

        # ── Appliquer le bannissement si nécessaire ───────────────────────────
        if ban_reason:
            attempt.status = AttemptStatus.BANNED
            attempt.banned_at = utcnow()
            attempt.ban_reason = ban_reason
            session.commit()
            session.close()
            return jsonify({
                'success': True,
                'banned': True,
                'ban_reason': ban_reason,
                'severity': 'high',
                'message': f"Vous avez été exclu de cet examen : {ban_reason}"
            })

        session.commit()

        response_data = {
            'success': True,
            'warnings_count': attempt.warnings_count,
            'tab_switches': attempt.tab_switches,
            'no_face_count': attempt.no_face_count or 0,
            'max_tab_switches': exam.max_tab_switches,
            'max_no_face_count': exam.max_no_face_count if exam.max_no_face_count is not None else 10,
            'severity': 'high' if event_type in (severity_tab_events + ['devtools_attempt', 'no_face_detected', 'face_absent', 'multiple_faces']) else 'medium',
            'banned': False
        }

        session.close()
        return jsonify(response_data)
        
    except Exception as e:
        print(f"❌ Erreur log_exam_activity: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

@app.route('/api/exam_attempts/<int:attempt_id>/result', methods=['GET'])
@paseto_required
def get_exam_attempt_result(attempt_id):
    """Résultat d'une tentative pour l'étudiant concerné"""
    try:
        user_id = get_current_user_id()
        session = get_session()
        attempt = session.query(ExamAttempt).filter_by(id=attempt_id, student_id=user_id).first()
        if not attempt:
            session.close()
            return jsonify({'error': 'Tentative introuvable'}), 404
        exam = session.query(OnlineExam).filter_by(id=attempt.exam_id).first()
        result = {
            'attempt_id':   attempt.id,
            'exam_title':   exam.title if exam else '',
            'score':        attempt.score,
            'feedback':     attempt.feedback,
            'corrected_at': attempt.corrected_at.isoformat() if attempt.corrected_at else None,
            'submitted_at': attempt.submitted_at.isoformat() if attempt.submitted_at else None,
            'status':       attempt.status.value,
        }
        session.close()
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/exam_attempts/<int:attempt_id>/subject', methods=['GET'])
@paseto_required
def get_exam_attempt_subject(attempt_id):
    """Récupérer le contenu du sujet pour une tentative en cours"""
    try:
        user_id = get_current_user_id()
        session = get_session()
        
        attempt = session.query(ExamAttempt).options(
            joinedload(ExamAttempt.exam).joinedload(OnlineExam.subject)
        ).filter_by(id=attempt_id, student_id=user_id).first()
        
        if not attempt:
            session.close()
            return jsonify({'error': 'Tentative non trouvée'}), 404
        
        if attempt.status != AttemptStatus.IN_PROGRESS:
            session.close()
            return jsonify({'error': 'Cette tentative n\'est plus active'}), 400
        
        subject = attempt.exam.subject
        if not subject:
            session.close()
            return jsonify({'error': 'Sujet non trouvé'}), 404
        
        # Extraire la réponse actuelle si elle existe
        current_answer = ''
        if attempt.answers:
            try:
                saved = json.loads(attempt.answers)
                current_answer = saved.get('reponse', '')
            except Exception:
                current_answer = attempt.answers

        subject_data = {
            'id': subject.id,
            'title': subject.title,
            # Retirer la section barème du contenu (elle contient les réponses)
            'content': _strip_bareme_from_content(subject.content or ''),
            # Barème NON transmis aux étudiants — contient les réponses
            # Infos exam/tentative pour la page proctorée
            'exam_title': attempt.exam.title,
            'duration_minutes': attempt.exam.duration_minutes,
            'extra_minutes': attempt.extra_minutes or 0,
            'started_at': attempt.started_at.isoformat() if attempt.started_at else None,
            'current_answer': current_answer,
        }

        session.close()
        return jsonify(subject_data)
        
    except Exception as e:
        print(f"❌ Erreur get_exam_attempt_subject: {e}")
        return jsonify({'error': str(e)}), 500

def _run_auto_correction(attempt_id: int):
    """Correction IA automatique dans un thread séparé (session DB indépendante)."""
    session = get_session()
    try:
        attempt = session.query(ExamAttempt).options(
            joinedload(ExamAttempt.exam).joinedload(OnlineExam.subject),
            joinedload(ExamAttempt.student)
        ).filter_by(id=attempt_id).first()

        if not attempt:
            print(f"⚠️  Auto-correction : tentative {attempt_id} introuvable")
            return

        exam    = attempt.exam
        subject = exam.subject

        if not subject or not subject.content:
            print(f"⚠️  Auto-correction {attempt_id} : sujet sans contenu, correction ignorée")
            return

        # Extraire les réponses (format examen en ligne : {qcm, texte} ou anciens formats)
        try:
            answers_data = json.loads(attempt.answers) if attempt.answers else {}
        except Exception:
            answers_data = {}

        student_answers = ''
        if isinstance(answers_data, dict):
            qcm_a  = answers_data.get('qcm',   {})
            text_a = answers_data.get('texte',  answers_data.get('text', {}))
            if qcm_a or text_a:
                lines = []
                all_keys = sorted(set(list(qcm_a.keys()) + list(text_a.keys())),
                                   key=lambda x: int(x) if str(x).isdigit() else 0)
                for k in all_keys:
                    if k in qcm_a:  lines.append(f"Question {k} : {qcm_a[k]}")
                    if k in text_a: lines.append(f"Question {k} : {text_a[k]}")
                student_answers = '\n'.join(lines)
            else:
                student_answers = (
                    answers_data.get('content') or answers_data.get('reponse') or
                    answers_data.get('answer')  or answers_data.get('text') or ''
                )
        if not student_answers:
            student_answers = attempt.answers or ''

        if not student_answers or not student_answers.strip():
            print(f"⚠️  Auto-correction {attempt_id} : aucune réponse, correction ignorée")
            return

        system_prompt = _build_correction_system_prompt(
            exam.title + (" — " + subject.title if subject.title else ""),
            subject.content
        )
        user_message = f"""SUJET D'EXAMEN:
{subject.content}

BARÈME DE NOTATION:
{subject.rubric or 'Barème standard sur 20 points'}

COPIE À CORRIGER (Examen en ligne — correction automatique):
Étudiant: {attempt.student.full_name}
Durée de l'examen: {exam.duration_minutes} minutes

RÉPONSES DE L'ÉTUDIANT:
{student_answers}

RAPPEL: Tu DOIS finir par "Note totale: XX.XX/20" """

        print(f"🤖 Auto-correction tentative {attempt_id} ({attempt.student.full_name}) — en cours…")
        result = call_claude(system_prompt, user_message, temperature=0.15)
        score  = extract_score_from_correction(result)

        attempt.score          = score
        attempt.feedback       = result
        attempt.corrected_at   = utcnow()
        attempt.corrected_by_id = None  # None = correction automatique
        session.commit()
        print(f"✅ Auto-correction {attempt_id} terminée : {score}/20")

        # Email à l'étudiant
        try:
            if attempt.student.email and '@temp.edu' not in attempt.student.email:
                send_paper_corrected_email(
                    student_email=attempt.student.email,
                    student_name=attempt.student.full_name,
                    subject_title=f"{exam.title} (Examen en ligne)",
                    score=score,
                    paper_id=attempt.id
                )
        except Exception as email_err:
            print(f"⚠️  Email auto-correction : {email_err}")

    except Exception as e:
        print(f"❌ Erreur auto-correction tentative {attempt_id} : {e}")
        try:
            session.rollback()
        except Exception:
            pass
    finally:
        session.close()


@app.route('/api/exam_attempts/<int:attempt_id>/submit', methods=['POST'])
@paseto_required
def submit_exam_attempt(attempt_id):
    """Soumettre l'examen (étudiant)"""
    try:
        user_id = get_current_user_id()
        session = get_session()
        
        attempt = session.query(ExamAttempt).filter_by(id=attempt_id, student_id=user_id).first()
        if not attempt:
            session.close()
            return jsonify({'error': 'Tentative non trouvée'}), 404
        
        if attempt.status != AttemptStatus.IN_PROGRESS:
            session.close()
            return jsonify({'error': 'Tentative déjà soumise ou bannie'}), 400
        
        # Sauvegarder les dernières réponses
        data = request.json
        if 'answers' in data:
            attempt.answers = data['answers']
        if 'signature_data' in data and data['signature_data']:
            attempt.signature_data = data['signature_data']

        attempt.status = AttemptStatus.SUBMITTED
        attempt.submitted_at = utcnow()

        # Charger le flag auto_correct avant de fermer la session
        exam = session.query(OnlineExam).filter_by(id=attempt.exam_id).first()
        auto_correct = exam.auto_correct if exam else False
        attempt_id_for_thread = attempt.id

        session.commit()
        session.close()

        # Lancer la correction IA en arrière-plan si activée par le prof
        if auto_correct:
            t = threading.Thread(
                target=_run_auto_correction,
                args=(attempt_id_for_thread,),
                daemon=True
            )
            t.start()
            return jsonify({'success': True, 'message': 'Examen soumis — correction automatique en cours', 'auto_correct': True})

        return jsonify({'success': True, 'message': 'Examen soumis avec succès', 'auto_correct': False})
    except Exception as e:
        print(f"Erreur submit_exam_attempt: {e}")
        try:
            session.close()
        except Exception:
            pass
        return jsonify({'error': str(e)}), 500

# ============================================================================
# ROUTES RELEVÉS DE NOTES
# ============================================================================

@app.route('/api/transcripts/generate/<int:student_id>/<int:semester_id>', methods=['POST'])
@paseto_required
def generate_transcript(student_id, semester_id):
    """Générer un relevé de notes selon la logique LMD — validation par UE avec compensation semestrielle"""
    try:
        user_id = get_current_user_id()
        session = get_session()

        user = session.query(User).filter_by(id=user_id).first()
        if user.role not in [UserRole.ADMIN, UserRole.PROFESSOR]:
            session.close()
            return jsonify({'error': 'Accès non autorisé'}), 403

        student = session.query(User).filter_by(id=student_id, role=UserRole.STUDENT).first()
        if not student:
            session.close()
            return jsonify({'error': 'Étudiant non trouvé'}), 404

        semester = session.query(Semester).filter_by(id=semester_id).first()
        if not semester:
            session.close()
            return jsonify({'error': 'Semestre non trouvé'}), 404

        # Vérification d'accès pour les professeurs : l'étudiant doit être inscrit
        # dans au moins une UE dont le prof a un EC affecté dans ce semestre.
        if user.role == UserRole.PROFESSOR:
            prof_ec_assignments = session.query(ECAssignment).filter_by(professor_id=user_id).all()
            prof_ue_ids = set()
            for asgn in prof_ec_assignments:
                ec = session.query(EC).filter_by(id=asgn.ec_id).first()
                if ec:
                    ue = session.query(UE).filter_by(id=ec.ue_id, semester_id=semester_id).first()
                    if ue:
                        prof_ue_ids.add(ue.id)
            if prof_ue_ids:
                enrolled = session.query(StudentUEEnrollment).filter(
                    StudentUEEnrollment.student_id == student_id,
                    StudentUEEnrollment.ue_id.in_(prof_ue_ids)
                ).first()
            else:
                enrolled = None
            if not enrolled:
                session.close()
                return jsonify({'error': 'Vous ne pouvez générer un relevé que pour les étudiants inscrits dans vos UEs.'}), 403

        # ── Récupérer toutes les UE du semestre avec leurs EC ──
        ues = session.query(UE).options(
            joinedload(UE.ecs)
        ).filter_by(semester_id=semester_id, is_active=True).order_by(UE.code).all()

        if not ues:
            session.close()
            return jsonify({'error': 'Aucune UE configurée pour ce semestre'}), 400

        # ── Pour chaque UE → chaque EC → agréger les notes ──
        ue_results = []
        total_notes_found = 0

        for ue in ues:
            ec_results = []
            ue_weighted_sum = 0.0
            ue_total_coef = 0

            for ec in sorted(ue.ecs, key=lambda e: e.code):
                if not ec.is_active:
                    continue

                # Toutes les évaluations de cet EC pour cet étudiant, triées par date décroissante
                # Règle : on prend la note de la dernière évaluation corrigée.
                # Si plusieurs évaluations distinctes existent (contrôle continu + examen final),
                # elles proviennent de Subjects différents → chacune a sa propre date → on prend la plus récente.
                # Cela couvre aussi le cas rattrapage : la nouvelle note remplace l'ancienne.
                papers_dated = session.query(StudentPaper.score, StudentPaper.corrected_at) \
                    .join(Subject, StudentPaper.subject_id == Subject.id) \
                    .filter(
                        StudentPaper.student_id == student_id,
                        Subject.ec_id == ec.id,
                        StudentPaper.score.isnot(None)
                    ).order_by(StudentPaper.corrected_at.desc()).all()

                attempts_dated = session.query(ExamAttempt.score,
                    ExamAttempt.corrected_at, ExamAttempt.submitted_at) \
                    .join(OnlineExam, ExamAttempt.exam_id == OnlineExam.id) \
                    .join(Subject, OnlineExam.subject_id == Subject.id) \
                    .filter(
                        ExamAttempt.student_id == student_id,
                        Subject.ec_id == ec.id,
                        ExamAttempt.score.isnot(None)
                    ).order_by(
                        ExamAttempt.corrected_at.desc(),
                        ExamAttempt.submitted_at.desc()
                    ).all()

                # Construire une liste unifiée (score, timestamp) puis prendre la plus récente
                from datetime import datetime as _dt
                _epoch = _dt(2000, 1, 1)
                unified = []
                for row in papers_dated:
                    unified.append((row[0], row[1] or _epoch))
                for row in attempts_dated:
                    ts = row[1] or row[2] or _epoch  # corrected_at sinon submitted_at
                    unified.append((row[0], ts))

                if unified:
                    # Trier par timestamp décroissant et prendre la note la plus récente
                    unified.sort(key=lambda x: x[1], reverse=True)
                    ec_note = unified[0][0]
                    ue_weighted_sum += ec_note * ec.coefficient
                    ue_total_coef += ec.coefficient
                    total_notes_found += 1
                    ec_avg_rounded = round(ec_note, 2)
                else:
                    ec_avg_rounded = None

                ec_results.append({
                    'ec_id': ec.id,
                    'ec_code': ec.code,
                    'ec_name': ec.name,
                    'coefficient': ec.coefficient,
                    'note': ec_avg_rounded,
                })

            # Moyenne UE pondérée par coefficients EC
            ue_avg = (ue_weighted_sum / ue_total_coef) if ue_total_coef > 0 else None
            ue_validated = (ue_avg >= 10) if ue_avg is not None else None

            ue_results.append({
                'ue_id': ue.id,
                'ue_code': ue.code,
                'ue_name': ue.name,
                'credits': ue.credits,
                'ecs': ec_results,
                'moyenne': round(ue_avg, 2) if ue_avg is not None else None,
                'validated': ue_validated,
                'validated_by_compensation': False,
                'credits_acquis': ue.credits if ue_validated else 0,
            })

        if total_notes_found == 0:
            session.close()
            return jsonify({'success': False, 'error': 'Aucune note disponible pour ce semestre'}), 200

        # ── Moyenne semestrielle pondérée par crédits UE (UE avec notes seulement) ──
        ues_with_grades = [u for u in ue_results if u['moyenne'] is not None]
        sem_weighted = sum(u['moyenne'] * u['credits'] for u in ues_with_grades)
        sem_credits = sum(u['credits'] for u in ues_with_grades)
        semester_avg = round(sem_weighted / sem_credits, 2) if sem_credits > 0 else 0.0

        # ── Compensation semestrielle LMD : si moy. générale ≥ 10, toutes les UE notées passent ──
        if semester_avg >= 10:
            for u in ue_results:
                if u['moyenne'] is not None and not u['validated']:
                    u['validated'] = True
                    u['validated_by_compensation'] = True
                    u['credits_acquis'] = u['credits']

        # ── Totaux ──
        semester_total_credits = sum(u['credits'] for u in ue_results)
        obtained_credits = sum(u['credits_acquis'] for u in ue_results)

        # ── Sauvegarder ──
        transcript = session.query(GradeTranscript).filter_by(
            student_id=student_id,
            semester_id=semester_id
        ).first()

        if not transcript:
            transcript = GradeTranscript(
                student_id=student_id,
                semester_id=semester_id,
                generated_by_id=user_id
            )
            session.add(transcript)

        transcript.gpa = semester_avg
        transcript.total_credits = semester_total_credits
        transcript.obtained_credits = obtained_credits
        transcript.ue_details = json.dumps(ue_results, ensure_ascii=False)
        transcript.generated_at = utcnow()
        transcript.generated_by_id = user_id

        session.commit()
        transcript_dict = transcript.to_dict()
        session.close()

        return jsonify({'success': True, 'transcript': transcript_dict})
    except Exception as e:
        print(f"Erreur generate_transcript: {e}")
        import traceback; traceback.print_exc()
        return jsonify({'error': str(e)}), 500

@app.route('/api/transcripts', methods=['GET'])
@paseto_required
def get_all_transcripts():
    """Liste des relevés générés.
    - Admin  : tous les relevés de la plateforme.
    - Professeur : uniquement les relevés des étudiants inscrits dans les UEs
                   dont ce professeur a au moins un EC affecté, OU les relevés
                   qu'il a lui-même générés.
    """
    try:
        user_id = get_current_user_id()
        session = get_session()

        user = session.query(User).filter_by(id=user_id).first()
        if user.role not in [UserRole.ADMIN, UserRole.PROFESSOR]:
            session.close()
            return jsonify({'error': 'Accès non autorisé'}), 403

        query = session.query(GradeTranscript).options(
            joinedload(GradeTranscript.student),
            joinedload(GradeTranscript.semester).joinedload(Semester.formation)
        )

        if user.role == UserRole.PROFESSOR:
            # Un professeur voit uniquement les relevés qu'il a lui-même générés.
            # Il n'a pas accès aux relevés produits par d'autres professeurs,
            # même si l'étudiant est inscrit dans l'un de ses ECs.
            # (Le relevé est un acte de délibération : seul son auteur et l'admin y ont accès.)
            query = query.filter(GradeTranscript.generated_by_id == user_id)

        transcripts = query.order_by(GradeTranscript.generated_at.desc()).all()
        
        transcripts_list = []
        for t in transcripts:
            # ✅ Récupérer manuellement le générateur si nécessaire
            generator_name = 'Système'
            if t.generated_by_id:
                generator = session.query(User).filter_by(id=t.generated_by_id).first()
                if generator:
                    generator_name = generator.full_name
            
            ue_data = None
            if t.ue_details:
                try:
                    ue_data = json.loads(t.ue_details)
                except Exception:
                    pass

            transcripts_list.append({
                'id': t.id,
                'student_id': t.student_id,
                'student_name': t.student.full_name if t.student else 'Inconnu',
                'student_email': t.student.email if t.student else 'N/A',
                'semester_id': t.semester_id,
                'semester_name': t.semester.name if t.semester else 'N/A',
                'formation_name': t.semester.formation.name if t.semester and t.semester.formation else 'N/A',
                'gpa': t.gpa,
                'total_credits': t.total_credits,
                'obtained_credits': t.obtained_credits,
                'validated': (t.gpa >= 10) if t.gpa is not None else False,
                'ue_details': ue_data,
                'generated_by': generator_name,
                'generated_by_id': t.generated_by_id,
                'generated_at': t.generated_at.isoformat() if t.generated_at else None,
                'is_published': bool(getattr(t, 'is_published', False)),
            })
        
        session.close()
        return jsonify(transcripts_list)
    except Exception as e:
        print(f"Erreur get_all_transcripts: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/student/transcripts', methods=['GET'])
@paseto_required
def get_student_transcripts():
    """Relevés de l'étudiant connecté"""
    try:
        user_id = get_current_user_id()
        session = get_session()
        
        user = session.query(User).filter_by(id=user_id).first()
        if user.role != UserRole.STUDENT:
            session.close()
            return jsonify({'error': 'Accès réservé aux étudiants'}), 403
        
        # ✅ CORRECTION : Ne pas utiliser joinedload sur generated_by
        transcripts = session.query(GradeTranscript).options(
            joinedload(GradeTranscript.semester).joinedload(Semester.formation)
            # RETIRÉ : joinedload(GradeTranscript.generated_by)
        ).filter_by(student_id=user_id).order_by(GradeTranscript.generated_at.desc()).all()
        
        transcripts_list = []
        for t in transcripts:
            # ✅ Récupérer manuellement le générateur
            generator_name = 'Système'
            if t.generated_by_id:
                generator = session.query(User).filter_by(id=t.generated_by_id).first()
                if generator:
                    generator_name = generator.full_name
            
            ue_data = None
            if t.ue_details:
                try:
                    ue_data = json.loads(t.ue_details)
                except Exception:
                    pass

            # #29 — n'exposer que les relevés publiés aux étudiants
            if not getattr(t, 'is_published', False):
                continue
            transcripts_list.append({
                'id': t.id,
                'semester_name': t.semester.name if t.semester else 'N/A',
                'semester_number': t.semester.number if t.semester else None,
                'formation_name': t.semester.formation.name if t.semester and t.semester.formation else 'N/A',
                'gpa': t.gpa,
                'total_credits': t.total_credits,
                'obtained_credits': t.obtained_credits,
                'validated': (t.gpa >= 10) if t.gpa is not None else False,
                'ue_details': ue_data,
                'generated_by': generator_name,
                'generated_at': t.generated_at.isoformat() if t.generated_at else None,
                'is_published': True,
            })

        session.close()
        return jsonify(transcripts_list)
    except Exception as e:
        print(f"❌ Erreur get_student_transcripts: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/transcripts/<int:transcript_id>/pdf', methods=['GET'])
@paseto_required
def export_transcript_pdf(transcript_id):
    """Exporter un relevé en PDF"""
    try:
        user_id = get_current_user_id()
        session = get_session()
        
        transcript = session.query(GradeTranscript).filter_by(id=transcript_id).first()
        if not transcript:
            session.close()
            return jsonify({'error': 'Relevé non trouvé'}), 404
        
        # Vérifier permissions
        user = session.query(User).filter_by(id=user_id).first()
        if user.role == UserRole.STUDENT and transcript.student_id != user_id:
            session.close()
            return jsonify({'error': 'Accès non autorisé'}), 403
        
        # Récupérer le détail UE stocké (logique LMD) — ou recalculer si ancien relevé
        from utils import generate_transcript_pdf

        ue_details = None
        if transcript.ue_details:
            try:
                ue_details = json.loads(transcript.ue_details)
            except Exception:
                ue_details = None

        if not ue_details:
            # Fallback pour anciens relevés : reconstruction à plat par EC
            papers = session.query(StudentPaper).join(Subject).join(EC).join(UE).filter(
                StudentPaper.student_id == transcript.student_id,
                UE.semester_id == transcript.semester_id,
                StudentPaper.score.isnot(None)
            ).all()
            online_attempts = session.query(ExamAttempt).join(
                OnlineExam, ExamAttempt.exam_id == OnlineExam.id
            ).join(Subject, OnlineExam.subject_id == Subject.id).join(EC).join(UE).filter(
                ExamAttempt.student_id == transcript.student_id,
                UE.semester_id == transcript.semester_id,
                ExamAttempt.score.isnot(None)
            ).all()
            # Regrouper par UE (structure minimale pour le PDF)
            ue_map = {}
            for p in papers:
                ec = p.subject.ec
                if not ec:
                    continue
                ue = ec.ue
                if ue.id not in ue_map:
                    ue_map[ue.id] = {'ue_code': ue.code, 'ue_name': ue.name,
                                     'credits': ue.credits, 'ecs': [],
                                     'moyenne': None, 'validated': None,
                                     'validated_by_compensation': False, 'credits_acquis': 0}
                ue_map[ue.id]['ecs'].append({'ec_code': ec.code, 'ec_name': ec.name,
                                             'coefficient': ec.coefficient, 'note': p.score})
            for att in online_attempts:
                ec = att.exam.subject.ec if att.exam and att.exam.subject else None
                if not ec:
                    continue
                ue = ec.ue
                if ue.id not in ue_map:
                    ue_map[ue.id] = {'ue_code': ue.code, 'ue_name': ue.name,
                                     'credits': ue.credits, 'ecs': [],
                                     'moyenne': None, 'validated': None,
                                     'validated_by_compensation': False, 'credits_acquis': 0}
                ue_map[ue.id]['ecs'].append({'ec_code': ec.code, 'ec_name': ec.name,
                                             'coefficient': ec.coefficient, 'note': att.score})
            ue_details = list(ue_map.values())

        transcript_data = {
            'student_name': transcript.student.full_name,
            'student_email': transcript.student.email,
            'semester_name': transcript.semester.name,
            'formation_name': transcript.semester.formation.name if transcript.semester.formation else 'N/A',
            'gpa': transcript.gpa,
            'total_credits': transcript.total_credits,
            'obtained_credits': transcript.obtained_credits,
            'ue_details': ue_details,
            'generated_at': transcript.generated_at.strftime('%d/%m/%Y')
        }
        
        pdf_path = f"exports/releve_{transcript.id}.pdf"
        generate_transcript_pdf(transcript_data, pdf_path)
        
        session.close()
        
        return send_file(pdf_path, as_attachment=True, download_name=f"releve_notes_{transcript.student.full_name}.pdf")
    except Exception as e:
        print(f"Erreur export_transcript_pdf: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/transcripts/bulk-pdf', methods=['GET'])
@paseto_required
def export_transcripts_bulk_pdf():
    """Exporter tous les relevés d'un semestre en ZIP (admin/professeur uniquement)"""
    import zipfile
    import io
    try:
        user_id = get_current_user_id()
        session = get_session()

        user = session.query(User).filter_by(id=user_id).first()
        if user.role == UserRole.STUDENT:
            session.close()
            return jsonify({'error': 'Accès non autorisé'}), 403

        semester_id = request.args.get('semester_id', type=int)
        if not semester_id:
            session.close()
            return jsonify({'error': 'semester_id requis'}), 400

        transcripts = session.query(GradeTranscript).filter_by(semester_id=semester_id).all()
        if not transcripts:
            session.close()
            return jsonify({'error': 'Aucun relevé trouvé pour ce semestre'}), 404

        from utils import generate_transcript_pdf

        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
            for transcript in transcripts:
                papers = session.query(StudentPaper).join(Subject).join(EC).join(UE).filter(
                    StudentPaper.student_id == transcript.student_id,
                    UE.semester_id == transcript.semester_id,
                    StudentPaper.score != None
                ).all()

                online_attempts = session.query(ExamAttempt).join(
                    OnlineExam, ExamAttempt.exam_id == OnlineExam.id
                ).join(
                    Subject, OnlineExam.subject_id == Subject.id
                ).join(EC).join(UE).filter(
                    ExamAttempt.student_id == transcript.student_id,
                    UE.semester_id == transcript.semester_id,
                    ExamAttempt.score != None
                ).all()

                notes_list = [{
                    'ec_code': p.subject.ec.code if p.subject.ec else 'N/A',
                    'ec_name': p.subject.ec.name if p.subject.ec else p.subject.title,
                    'score': p.score,
                    'coefficient': p.subject.ec.coefficient if p.subject.ec else 1
                } for p in papers]

                for attempt in online_attempts:
                    ec = attempt.exam.subject.ec if attempt.exam and attempt.exam.subject else None
                    notes_list.append({
                        'ec_code': ec.code if ec else 'N/A',
                        'ec_name': ec.name if ec else (attempt.exam.subject.title if attempt.exam and attempt.exam.subject else 'N/A'),
                        'score': attempt.score,
                        'coefficient': ec.coefficient if ec else 1
                    })

                transcript_data = {
                    'student_name': transcript.student.full_name,
                    'student_email': transcript.student.email,
                    'semester_name': transcript.semester.name,
                    'formation_name': transcript.semester.formation.name if transcript.semester.formation else 'N/A',
                    'gpa': transcript.gpa,
                    'total_credits': transcript.total_credits,
                    'obtained_credits': transcript.obtained_credits,
                    'papers': notes_list,
                    'generated_at': transcript.generated_at.strftime('%d/%m/%Y')
                }

                safe_name = transcript.student.full_name.replace(' ', '_').replace('/', '-')
                pdf_path = f"exports/releve_{transcript.id}.pdf"
                generate_transcript_pdf(transcript_data, pdf_path)
                zf.write(pdf_path, arcname=f"releve_{safe_name}.pdf")

        session.close()
        zip_buffer.seek(0)

        semester_label = transcripts[0].semester.name.replace(' ', '_').replace('/', '-') if transcripts else str(semester_id)
        return send_file(
            zip_buffer,
            mimetype='application/zip',
            as_attachment=True,
            download_name=f"releves_{semester_label}.zip"
        )
    except Exception as e:
        print(f"Erreur export_transcripts_bulk_pdf: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/transcripts/<int:transcript_id>', methods=['DELETE'])
@paseto_required
def delete_transcript(transcript_id):
    """
    Suppression d'un relevé de notes.
    - Admin  : peut supprimer n'importe quel relevé
    - Professeur : uniquement les relevés qu'il a générés
    - Étudiant : interdit (document officiel)
    """
    try:
        user_id = get_current_user_id()
        session = get_session()

        user = session.query(User).filter_by(id=user_id).first()
        if user.role == UserRole.STUDENT:
            session.close()
            return jsonify({'error': 'Les étudiants ne peuvent pas supprimer un relevé de notes.'}), 403

        transcript = session.query(GradeTranscript).filter_by(id=transcript_id).first()
        if not transcript:
            session.close()
            return jsonify({'error': 'Relevé introuvable.'}), 404

        if user.role == UserRole.PROFESSOR and transcript.generated_by_id != user_id:
            session.close()
            return jsonify({'error': 'Vous ne pouvez supprimer que les relevés que vous avez générés.'}), 403

        student_name = transcript.student.full_name if transcript.student else 'Inconnu'
        semester_name = transcript.semester.name if transcript.semester else 'Inconnu'

        session.delete(transcript)
        session.commit()
        session.close()

        return jsonify({
            'success': True,
            'message': f'Relevé de {student_name} ({semester_name}) supprimé avec succès.'
        })
    except Exception as e:
        print(f"Erreur delete_transcript: {e}")
        return jsonify({'error': str(e)}), 500

# ============================================================================
# #29 — PUBLICATION DES RELEVÉS DE NOTES
# ============================================================================

@app.route('/api/transcripts/<int:transcript_id>/publish', methods=['PUT'])
@paseto_required
def toggle_transcript_publish(transcript_id):
    """Publier ou masquer un relevé de notes (admin/prof uniquement)"""
    try:
        user_id = get_current_user_id()
        session = get_session()
        user = session.query(User).filter_by(id=user_id).first()
        if user.role not in [UserRole.ADMIN, UserRole.PROFESSOR]:
            session.close()
            return jsonify({'error': 'Accès non autorisé'}), 403
        t = session.query(GradeTranscript).filter_by(id=transcript_id).first()
        if not t:
            session.close()
            return jsonify({'error': 'Relevé introuvable'}), 404
        data = request.get_json() or {}
        t.is_published = bool(data.get('is_published', not t.is_published))
        session.commit()
        result = {'success': True, 'is_published': t.is_published}
        session.close()
        return jsonify(result)
    except Exception as e:
        print(f"❌ toggle_transcript_publish: {e}")
        return jsonify({'error': str(e)}), 500


# ============================================================================
# #4 — BANQUE DE QUESTIONS
# ============================================================================

@app.route('/api/question_bank', methods=['GET'])
@paseto_required
def list_question_bank():
    """Lister les questions sauvegardées (admin/prof)"""
    try:
        user_id = get_current_user_id()
        session = get_session()
        user = session.query(User).filter_by(id=user_id).first()
        if user.role not in [UserRole.ADMIN, UserRole.PROFESSOR]:
            session.close()
            return jsonify({'error': 'Accès non autorisé'}), 403
        questions = session.query(QuestionBank).order_by(QuestionBank.created_at.desc()).all()
        result = [q.to_dict() for q in questions]
        session.close()
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/question_bank', methods=['POST'])
@paseto_required
def save_question_bank():
    """Sauvegarder une question dans la banque"""
    try:
        user_id = get_current_user_id()
        session = get_session()
        user = session.query(User).filter_by(id=user_id).first()
        if user.role not in [UserRole.ADMIN, UserRole.PROFESSOR]:
            session.close()
            return jsonify({'error': 'Accès non autorisé'}), 403
        data = request.get_json() or {}
        if not data.get('content'):
            session.close()
            return jsonify({'error': 'Contenu requis'}), 400
        q = QuestionBank(
            title=(data.get('title') or data['content'][:80]).strip(),
            content=data['content'].strip(),
            rubric=data.get('rubric', ''),
            question_type=data.get('question_type', 'open'),
            bloom_level=data.get('bloom_level', ''),
            ec_id=data.get('ec_id') or None,
            created_by_id=user_id,
        )
        session.add(q)
        session.commit()
        result = q.to_dict()
        session.close()
        return jsonify({'success': True, 'question': result}), 201
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/question_bank/<int:q_id>', methods=['DELETE'])
@paseto_required
def delete_question_bank(q_id):
    """Supprimer une question de la banque"""
    try:
        user_id = get_current_user_id()
        session = get_session()
        user = session.query(User).filter_by(id=user_id).first()
        q = session.query(QuestionBank).filter_by(id=q_id).first()
        if not q:
            session.close()
            return jsonify({'error': 'Question introuvable'}), 404
        if user.role != UserRole.ADMIN and q.created_by_id != user_id:
            session.close()
            return jsonify({'error': 'Accès non autorisé'}), 403
        session.delete(q)
        session.commit()
        session.close()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/question_bank/assemble', methods=['POST'])
@paseto_required
def assemble_from_bank():
    """Crée un sujet d'examen à partir d'une sélection de questions de la banque."""
    try:
        user_id = get_current_user_id()
        session = get_session()
        user = session.query(User).filter_by(id=user_id).first()
        if not user or user.role not in [UserRole.ADMIN, UserRole.PROFESSOR]:
            session.close()
            return jsonify({'error': 'Accès non autorisé'}), 403

        data = request.get_json() or {}
        question_ids   = data.get('question_ids', [])
        title          = data.get('title', 'Examen').strip()
        duration       = int(data.get('duration', 60))
        student_level  = data.get('student_level', 'Licence')
        ec_id          = data.get('ec_id') or None
        exam_type_hint = data.get('exam_type', '')

        if not question_ids:
            session.close()
            return jsonify({'error': 'Aucune question sélectionnée'}), 400

        # Récupérer les questions dans l'ordre fourni
        questions = []
        for qid in question_ids:
            q = session.query(QuestionBank).filter_by(id=int(qid)).first()
            if q:
                questions.append(q)

        if not questions:
            session.close()
            return jsonify({'error': 'Questions introuvables'}), 404

        # Déterminer le type d'examen d'après les questions sélectionnées
        types_present = set(q.question_type for q in questions)
        has_qcm  = 'qcm' in types_present
        has_vf   = 'vf'  in types_present
        has_open = 'open' in types_present or 'subopen' in types_present
        if exam_type_hint:
            exam_type_label = exam_type_hint
        elif has_qcm and has_open:
            exam_type_label = 'Mixte (QCM + Questions ouvertes)'
        elif has_qcm and has_vf:
            exam_type_label = 'Mixte (QCM + Vrai/Faux)'
        elif has_qcm:
            exam_type_label = 'QCM'
        elif has_vf:
            exam_type_label = 'Vrai / Faux'
        else:
            exam_type_label = 'Questions ouvertes'

        # Mapper type → marqueur
        TYPE_MARKER = {'qcm': '[QCM]', 'vf': '[VF]', 'open': '[OUVERT]', 'subopen': '[SUBOPEN]'}

        # Calculer les points (total = 20, répartis proportionnellement)
        n = len(questions)
        base_pts = 20 // n
        remainder = 20 - base_pts * n

        # ── Assembler le contenu ──────────────────────────────────
        sep = '══════════════════════════════════════'
        content_lines = [
            sep,
            title.upper(),
            sep,
            f'Type d\'examen : {exam_type_label}',
            f'Niveau : {student_level} | Durée : {duration} minutes | Note totale : 20 points',
            sep,
            '',
            'INSTRUCTIONS AUX ÉTUDIANTS',
            '──────────────────────────',
            'Lisez attentivement chaque question avant de répondre. '
            'Respectez le barème indiqué pour chaque question.',
            '',
            sep,
            'QUESTIONS',
            sep,
            '',
        ]

        rubric_lines = [sep, 'BARÈME DE NOTATION', sep, '']

        for i, q in enumerate(questions):
            num = i + 1
            pts = base_pts + (1 if i < remainder else 0)
            marker = TYPE_MARKER.get(q.question_type, '[OUVERT]')

            # Titre de la question avec marqueur de type
            content_lines.append(f'Question {num} — {q.title} ............. ({pts} pt{"s" if pts > 1 else ""}) {marker}')
            # Corps de la question (contenu brut de la banque)
            content_lines.append(q.content.strip())
            content_lines.append('')

            # Barème
            rubric_lines.append(f'Question {num} — {q.title} ({pts} pt{"s" if pts > 1 else ""})')
            if q.rubric and q.rubric.strip():
                for line in q.rubric.strip().split('\n'):
                    rubric_lines.append(f'  {line}')
            else:
                rubric_lines.append(f'  • Réponse attendue : {pts} pt{"s" if pts > 1 else ""}')
            rubric_lines.append('')

        rubric_lines += ['──────────────────────────', f'TOTAL : 20 / 20 points', sep]

        content = '\n'.join(content_lines).strip()
        rubric  = '\n'.join(rubric_lines).strip()

        # ── Créer le sujet ────────────────────────────────────────
        from models import Subject
        subject = Subject(
            title=title,
            content=content,
            rubric=rubric,
            ec_id=int(ec_id) if ec_id else None,
            created_by_id=user_id,
        )
        session.add(subject)
        session.commit()
        result = subject.to_dict()
        session.close()
        return jsonify({'success': True, 'subject': result}), 201

    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({'error': str(e)}), 500


# ============================================================================
# #2 — INSCRIPTION MANUELLE D'UN ÉTUDIANT À UN EC (via UE parente)
# ============================================================================

@app.route('/api/admin/enroll_student_ec', methods=['POST'])
@paseto_required
def enroll_student_ec():
    """Inscrire un étudiant à la UE parente d'un EC donné (admin uniquement)"""
    try:
        user_id = get_current_user_id()
        session = get_session()
        user = session.query(User).filter_by(id=user_id).first()
        if user.role != UserRole.ADMIN:
            session.close()
            return jsonify({'error': 'Accès non autorisé'}), 403
        data = request.get_json() or {}
        student_id = data.get('student_id')
        ec_id = data.get('ec_id')
        if not student_id or not ec_id:
            session.close()
            return jsonify({'error': 'student_id et ec_id requis'}), 400
        ec = session.query(EC).filter_by(id=ec_id).first()
        if not ec:
            session.close()
            return jsonify({'error': 'EC introuvable'}), 404
        ue_id = ec.ue_id
        existing = session.query(StudentUEEnrollment).filter_by(student_id=student_id, ue_id=ue_id).first()
        if existing:
            session.close()
            return jsonify({'success': True, 'message': 'Déjà inscrit à cette UE'})
        enroll = StudentUEEnrollment(student_id=student_id, ue_id=ue_id)
        session.add(enroll)
        session.commit()
        session.close()
        return jsonify({'success': True, 'message': 'Étudiant inscrit avec succès'}), 201
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ============================================================================
# #7 — UPLOAD D'IMAGE POUR LES SUJETS D'EXAMEN
# ============================================================================

@app.route('/api/subjects/<int:subject_id>/upload_image', methods=['POST'])
@paseto_required
def upload_subject_image(subject_id):
    """Attacher une image à un sujet d'examen"""
    try:
        user_id = get_current_user_id()
        session = get_session()
        user = session.query(User).filter_by(id=user_id).first()
        subject = session.query(Subject).filter_by(id=subject_id).first()
        if not subject:
            session.close()
            return jsonify({'error': 'Sujet introuvable'}), 404
        if user.role not in [UserRole.ADMIN, UserRole.PROFESSOR] or \
           (user.role == UserRole.PROFESSOR and subject.creator_id != user_id):
            session.close()
            return jsonify({'error': 'Accès non autorisé'}), 403
        if 'image' not in request.files:
            session.close()
            return jsonify({'error': 'Aucune image fournie'}), 400
        img = request.files['image']
        if not img.filename:
            session.close()
            return jsonify({'error': 'Fichier invalide'}), 400
        import uuid
        ext = img.filename.rsplit('.', 1)[-1].lower()
        if ext not in ('png', 'jpg', 'jpeg', 'gif', 'webp'):
            session.close()
            return jsonify({'error': 'Format non supporté (PNG, JPG, GIF, WEBP)'}), 400
        filename = f"subject_{subject_id}_{uuid.uuid4().hex[:8]}.{ext}"
        save_path = Path(app.config['UPLOAD_FOLDER']) / filename
        img.save(str(save_path))
        subject.image_filename = filename
        session.commit()
        session.close()
        return jsonify({'success': True, 'image_url': f"/uploads/{filename}"})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ============================================================================
# LEVÉE DE BANNISSEMENT — PROF / ADMIN
# ============================================================================

@app.route('/api/exam_attempts/<int:attempt_id>/unban', methods=['POST'])
@paseto_required
def unban_exam_attempt(attempt_id):
    """
    Lever le bannissement d'un étudiant sur une tentative d'examen.
    - Admin : peut unban n'importe quelle tentative
    - Professeur : uniquement si l'examen lui appartient
    """
    try:
        user_id = get_current_user_id()
        session = get_session()
        user = session.query(User).filter_by(id=user_id).first()

        if user.role not in [UserRole.ADMIN, UserRole.PROFESSOR]:
            session.close()
            return jsonify({'error': 'Accès non autorisé'}), 403

        attempt = session.query(ExamAttempt).filter_by(id=attempt_id).first()
        if not attempt:
            session.close()
            return jsonify({'error': 'Tentative introuvable'}), 404

        if user.role == UserRole.PROFESSOR and attempt.exam.created_by_id != user_id:
            session.close()
            return jsonify({'error': 'Vous ne pouvez lever que les bannissements de vos propres examens'}), 403

        if attempt.status != AttemptStatus.BANNED:
            session.close()
            return jsonify({'error': "Cet étudiant n'est pas banni sur cette tentative"}), 400

        data = request.get_json() or {}
        reason = data.get('reason', '').strip()

        attempt.status = AttemptStatus.IN_PROGRESS
        attempt.banned_at = None
        # Log the unban action
        log = ExamActivityLog(
            attempt_id=attempt.id,
            event_type='unban',
            details=f'Bannissement levé par {user.full_name}' + (f' — Motif : {reason}' if reason else ''),
            risk_score=0
        )
        session.add(log)
        session.commit()

        student_name = attempt.student.full_name if attempt.student else 'Inconnu'
        session.close()
        return jsonify({'success': True, 'message': f'Bannissement de {student_name} levé avec succès.'})
    except Exception as e:
        print(f"❌ Erreur unban_exam_attempt: {e}")
        return jsonify({'error': str(e)}), 500


# ============================================================================
# EXPORT CSV DES RÉSULTATS D'UN EXAMEN EN LIGNE
# ============================================================================

@app.route('/api/online_exams/<int:exam_id>/results/csv', methods=['GET'])
@paseto_required
def export_exam_results_csv(exam_id):
    """Export CSV des résultats d'un examen en ligne (prof propriétaire ou admin)."""
    try:
        import csv, io
        from flask import Response

        user_id = get_current_user_id()
        session = get_session()
        user = session.query(User).filter_by(id=user_id).first()

        if user.role not in [UserRole.ADMIN, UserRole.PROFESSOR]:
            session.close()
            return jsonify({'error': 'Accès non autorisé'}), 403

        exam = session.query(OnlineExam).filter_by(id=exam_id).first()
        if not exam:
            session.close()
            return jsonify({'error': 'Examen introuvable'}), 404

        if user.role == UserRole.PROFESSOR and exam.created_by_id != user_id:
            session.close()
            return jsonify({'error': 'Accès réservé au professeur propriétaire de cet examen'}), 403

        attempts = session.query(ExamAttempt).options(
            joinedload(ExamAttempt.student)
        ).filter_by(exam_id=exam_id).order_by(ExamAttempt.started_at).all()

        output = io.StringIO()
        writer = csv.writer(output, delimiter=';')
        writer.writerow([
            'Nom complet', 'Email', 'Statut', 'Note /20',
            'Début', 'Soumission', 'Durée (min)', 'Incidents'
        ])

        for a in attempts:
            status_labels = {
                'submitted': 'Soumis', 'in_progress': 'En cours',
                'banned': 'Banni', 'timed_out': 'Temps écoulé'
            }
            status_val = (a.status.value if hasattr(a.status, 'value') else str(a.status)) if a.status else ''
            status_label = status_labels.get(status_val, str(a.status))

            duration = ''
            if a.started_at and a.submitted_at:
                delta = a.submitted_at - a.started_at
                duration = str(round(delta.total_seconds() / 60, 1))

            incidents = session.query(ExamActivityLog).filter_by(
                attempt_id=a.id
            ).count()

            writer.writerow([
                a.student.full_name if a.student else 'Inconnu',
                a.student.email if a.student else '',
                status_label,
                a.score if a.score is not None else '',
                a.started_at.strftime('%d/%m/%Y %H:%M') if a.started_at else '',
                a.submitted_at.strftime('%d/%m/%Y %H:%M') if a.submitted_at else '',
                duration,
                incidents
            ])

        csv_content = '﻿' + output.getvalue()  # BOM UTF-8 pour Excel
        session.close()

        safe_title = ''.join(c if c.isalnum() or c in ' -_' else '_' for c in (exam.title or 'examen'))
        filename = f"resultats_{safe_title[:40]}.csv"
        return Response(
            csv_content,
            mimetype='text/csv; charset=utf-8',
            headers={'Content-Disposition': f'attachment; filename="{filename}"'}
        )
    except Exception as e:
        print(f"❌ Erreur export_exam_results_csv: {e}")
        return jsonify({'error': str(e)}), 500


# ============================================================================
# CORRECTION AUTOMATIQUE DES EXAMENS EN LIGNE AVEC IA
# ============================================================================

@app.route('/api/exam_attempts/<int:attempt_id>/correct', methods=['POST'])
@paseto_required
def correct_exam_attempt(attempt_id):
    """Corriger automatiquement une tentative d'examen avec IA"""
    try:
        user_id = get_current_user_id()
        session = get_session()
        
        user = session.query(User).filter_by(id=user_id).first()
        if user.role not in [UserRole.PROFESSOR, UserRole.ADMIN]:
            session.close()
            return jsonify({'error': 'Accès non autorisé'}), 403
        
        # Récupérer la tentative
        attempt = session.query(ExamAttempt).options(
            joinedload(ExamAttempt.exam).joinedload(OnlineExam.subject),
            joinedload(ExamAttempt.student)
        ).filter_by(id=attempt_id).first()
        
        if not attempt:
            session.close()
            return jsonify({'error': 'Tentative non trouvée'}), 404
        
        # Vérifier que la tentative est soumise
        if attempt.status not in [AttemptStatus.SUBMITTED, AttemptStatus.AUTO_SUBMITTED]:
            session.close()
            return jsonify({'error': 'Cette tentative n\'est pas encore soumise'}), 400
        
        # Vérifier que le professeur est propriétaire de l'examen
        if user.role == UserRole.PROFESSOR and attempt.exam.created_by_id != user_id:
            session.close()
            return jsonify({'error': 'Vous ne pouvez corriger que vos propres examens'}), 403
        
        exam = attempt.exam
        subject = exam.subject
        
        # Extraire les réponses de l'étudiant (plusieurs formats possibles)
        try:
            answers_data = json.loads(attempt.answers) if attempt.answers else {}
        except Exception:
            answers_data = {}

        student_answers = ''

        if isinstance(answers_data, dict):
            # Format de l'examen en ligne : { qcm: {"1":"A","2":"Vrai"}, texte: {"3":"...","4":"..."} }
            qcm_answers  = answers_data.get('qcm', {})
            text_answers = answers_data.get('texte', answers_data.get('text', {}))

            if qcm_answers or text_answers:
                lines = []
                # Rassembler tous les numéros de questions dans l'ordre
                all_keys = sorted(
                    set(list(qcm_answers.keys()) + list(text_answers.keys())),
                    key=lambda x: int(x) if x.isdigit() else 0
                )
                for k in all_keys:
                    if k in qcm_answers:
                        lines.append(f"Question {k} : {qcm_answers[k]}")
                    if k in text_answers:
                        lines.append(f"Question {k} : {text_answers[k]}")
                student_answers = '\n'.join(lines)
            else:
                # Anciens formats : content / reponse / answer
                student_answers = (
                    answers_data.get('content') or
                    answers_data.get('reponse') or
                    answers_data.get('answer') or
                    ''
                )
        elif isinstance(answers_data, str):
            student_answers = answers_data

        if not student_answers or not student_answers.strip():
            # Tenter le brut si le JSON ne contient rien d'utile
            student_answers = attempt.answers or ''

        if not student_answers or not student_answers.strip():
            session.close()
            return jsonify({'error': 'Aucune réponse à corriger pour cet étudiant'}), 400
        
        system_prompt = _build_correction_system_prompt(
            exam.title + (" — " + subject.title if subject.title else ""),
            subject.content
        )

        user_message = f"""SUJET D'EXAMEN:
{subject.content}

BARÈME DE NOTATION:
{subject.rubric}

COPIE À CORRIGER (Examen en ligne):
Étudiant: {attempt.student.full_name}
Durée de l'examen: {exam.duration_minutes} minutes

RÉPONSES DE L'ÉTUDIANT:
{student_answers}

RAPPEL: Tu DOIS finir par "Note totale: XX.XX/20" """

        # Appeler Claude pour la correction
        result = call_claude(system_prompt, user_message, temperature=0.15)
        score = extract_score_from_correction(result)
        
        # Stocker les résultats
        attempt.score = score
        attempt.feedback = result
        attempt.corrected_at = utcnow()
        attempt.corrected_by_id = user_id
        
        session.commit()
        
        # Envoyer email à l'étudiant si adresse valide
        try:
            if attempt.student.email and '@temp.edu' not in attempt.student.email:
                email_sent = send_paper_corrected_email(
                    student_email=attempt.student.email,
                    student_name=attempt.student.full_name,
                    subject_title=f"{exam.title} (Examen en ligne)",
                    score=score,
                    paper_id=attempt.id
                )
                if email_sent:
                    print(f"✅ Email envoyé à {attempt.student.email}")
        except Exception as email_error:
            print(f"⚠️ Erreur envoi email: {email_error}")
        
        attempt_dict = attempt.to_dict()
        session.close()
        
        return jsonify({
            'success': True,
            'attempt': attempt_dict,
            'message': f'Correction terminée: {score}/20'
        })
        
    except Exception as e:
        print(f"❌ Erreur correct_exam_attempt: {e}")
        import traceback; traceback.print_exc()
        try: session.rollback(); session.close()
        except: pass
        return jsonify({'error': str(e)}), 500


@app.route('/api/online_exams/<int:exam_id>/attempts', methods=['GET'])
@paseto_required
def get_exam_attempts(exam_id):
    """Récupérer toutes les tentatives d'un examen (professeur/admin)"""
    try:
        user_id = get_current_user_id()
        session = get_session()
        
        user = session.query(User).filter_by(id=user_id).first()
        if user.role not in [UserRole.PROFESSOR, UserRole.ADMIN]:
            session.close()
            return jsonify({'error': 'Accès non autorisé'}), 403
        
        exam = session.query(OnlineExam).filter_by(id=exam_id).first()
        if not exam:
            session.close()
            return jsonify({'error': 'Examen non trouvé'}), 404
        
        # Vérifier propriété pour professeur
        if user.role == UserRole.PROFESSOR and exam.created_by_id != user_id:
            session.close()
            return jsonify({'error': 'Accès non autorisé'}), 403
        
        attempts = session.query(ExamAttempt).options(
            joinedload(ExamAttempt.student)
        ).filter_by(exam_id=exam_id).order_by(ExamAttempt.started_at.desc()).all()
        
        attempts_list = []
        for attempt in attempts:
            attempt_dict = attempt.to_dict()
            # Ajouter info incidents
            attempt_dict['has_incidents'] = attempt.warnings_count > 0 or attempt.tab_switches > 0
            attempt_dict['needs_correction'] = attempt.status in [AttemptStatus.SUBMITTED, AttemptStatus.AUTO_SUBMITTED] and attempt.score is None
            attempts_list.append(attempt_dict)
        
        session.close()
        return jsonify(attempts_list)
        
    except Exception as e:
        print(f"❌ Erreur get_exam_attempts: {e}")
        try: session.rollback(); session.close()
        except: pass
        return jsonify({'error': str(e)}), 500


# ============================================================================
# EXPORT CSV DES NOTES
# ============================================================================

@app.route('/api/online_exams/<int:exam_id>/export-csv', methods=['GET'])
@paseto_required
def export_exam_csv(exam_id):
    """Exporte les notes d'un examen en CSV (prof/admin)."""
    import csv
    try:
        user_id = get_current_user_id()
        session = get_session()
        user = session.query(User).filter_by(id=user_id).first()
        if user.role not in [UserRole.PROFESSOR, UserRole.ADMIN]:
            session.close()
            return jsonify({'error': 'Accès non autorisé'}), 403
        exam = session.query(OnlineExam).filter_by(id=exam_id).first()
        if not exam:
            session.close()
            return jsonify({'error': 'Examen non trouvé'}), 404
        if user.role == UserRole.PROFESSOR and exam.created_by_id != user_id:
            session.close()
            return jsonify({'error': 'Accès non autorisé'}), 403
        attempts = session.query(ExamAttempt).options(
            joinedload(ExamAttempt.student)
        ).filter_by(exam_id=exam_id).order_by(ExamAttempt.submitted_at).all()
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(['Étudiant', 'Email', 'Statut', 'Note /20', 'Risque %',
                         'Tab switches', 'Alertes', 'Durée (min)', 'Soumis à', 'Signature pré'])
        for a in attempts:
            name  = a.student.full_name if a.student else '?'
            email = a.student.email if a.student else ''
            dur   = int((a.submitted_at - a.started_at).total_seconds() / 60) if a.submitted_at and a.started_at else ''
            writer.writerow([
                name, email, a.status.value,
                a.score if a.score is not None else '',
                a.risk_score or 0,
                a.tab_switches or 0,
                a.warnings_count or 0,
                dur,
                a.submitted_at.strftime('%Y-%m-%d %H:%M') if a.submitted_at else '',
                'Oui' if a.pre_exam_signature_data else 'Non',
            ])
        session.close()
        filename = f"notes_{exam.title.replace(' ','_')}.csv"
        return send_file(
            io.BytesIO(output.getvalue().encode('utf-8-sig')),
            mimetype='text/csv',
            as_attachment=True,
            download_name=filename
        )
    except Exception as e:
        print(f"❌ export_exam_csv {exam_id}: {e}")
        try: session.close()
        except: pass
        return jsonify({'error': str(e)}), 500


# ============================================================================
# STATISTIQUES PAR EXAMEN
# ============================================================================

@app.route('/api/online_exams/<int:exam_id>/stats', methods=['GET'])
@paseto_required
def get_exam_stats(exam_id):
    """Statistiques détaillées d'un examen : distribution, médiane, taux réussite."""
    try:
        user_id = get_current_user_id()
        session = get_session()
        user = session.query(User).filter_by(id=user_id).first()
        if user.role not in [UserRole.PROFESSOR, UserRole.ADMIN]:
            session.close()
            return jsonify({'error': 'Accès non autorisé'}), 403
        exam = session.query(OnlineExam).filter_by(id=exam_id).first()
        if not exam:
            session.close()
            return jsonify({'error': 'Examen non trouvé'}), 404
        attempts = session.query(ExamAttempt).filter_by(exam_id=exam_id).all()
        done = [a for a in attempts if a.status.value in ('submitted', 'auto_submitted')]
        scores = [a.score for a in done if a.score is not None]
        distribution = [0] * 5  # [0-4, 5-9, 10-13, 14-16, 17-20]
        for s in scores:
            if   s < 5:  distribution[0] += 1
            elif s < 10: distribution[1] += 1
            elif s < 14: distribution[2] += 1
            elif s < 17: distribution[3] += 1
            else:        distribution[4] += 1
        durations = []
        for a in done:
            if a.submitted_at and a.started_at:
                durations.append(int((a.submitted_at - a.started_at).total_seconds() / 60))
        session.close()
        return jsonify({
            'exam_title':       exam.title,
            'total':            len(attempts),
            'submitted':        len(done),
            'in_progress':      sum(1 for a in attempts if a.status.value == 'in_progress'),
            'banned':           sum(1 for a in attempts if a.status.value == 'banned'),
            'corrected':        sum(1 for a in done if a.score is not None),
            'avg_score':        round(sum(scores)/len(scores), 2) if scores else None,
            'median_score':     round(statistics.median(scores), 2) if scores else None,
            'min_score':        min(scores) if scores else None,
            'max_score':        max(scores) if scores else None,
            'pass_rate':        round(sum(1 for s in scores if s >= 10) / len(scores) * 100, 1) if scores else None,
            'distribution':     distribution,
            'avg_duration_min': round(sum(durations)/len(durations), 1) if durations else None,
            'avg_risk':         round(sum(a.risk_score or 0 for a in attempts) / len(attempts), 1) if attempts else 0,
            'high_risk_count':  sum(1 for a in attempts if (a.risk_score or 0) >= 70),
            'pre_sig_rate':     round(sum(1 for a in done if a.pre_exam_signature_data) / len(done) * 100, 1) if done else 0,
        })
    except Exception as e:
        print(f"❌ get_exam_stats {exam_id}: {e}")
        try: session.close()
        except: pass
        return jsonify({'error': str(e)}), 500


# ============================================================================
# BILAN PAR ÉTUDIANT
# ============================================================================

@app.route('/api/online_exams/<int:exam_id>/bilan', methods=['GET'])
@paseto_required
def get_exam_bilan(exam_id):
    """Liste détaillée par étudiant : score, risque, durée, statut, extra-temps, notes."""
    try:
        user_id = get_current_user_id()
        session = get_session()
        user = session.query(User).filter_by(id=user_id).first()
        if not user or user.role not in [UserRole.PROFESSOR, UserRole.ADMIN]:
            session.close()
            return jsonify({'error': 'Accès non autorisé'}), 403
        exam = session.query(OnlineExam).filter_by(id=exam_id).first()
        if not exam:
            session.close()
            return jsonify({'error': 'Examen non trouvé'}), 404

        from sqlalchemy import func as sa_func
        attempts = session.query(ExamAttempt).filter_by(exam_id=exam_id).all()

        # Compter les notes de surveillance par tentative
        note_counts = {}
        if attempts:
            ids = [a.id for a in attempts]
            rows = session.query(
                ExamActivityLog.attempt_id,
                sa_func.count(ExamActivityLog.id)
            ).filter(
                ExamActivityLog.attempt_id.in_(ids),
                ExamActivityLog.event_type == 'proctor_note'
            ).group_by(ExamActivityLog.attempt_id).all()
            note_counts = {r[0]: r[1] for r in rows}

        rows_out = []
        for a in attempts:
            duration_min = None
            if a.submitted_at and a.started_at:
                duration_min = round((a.submitted_at - a.started_at).total_seconds() / 60, 1)
            status_val = (a.status.value if hasattr(a.status, 'value') else str(a.status)) if a.status else ''
            rows_out.append({
                'attempt_id':    a.id,
                'student_name':  a.student.full_name if a.student else '—',
                'student_email': a.student.email if a.student else '—',
                'status':        status_val,
                'score':         a.score,
                'feedback':      a.feedback or '',
                'risk_score':    a.risk_score or 0,
                'extra_minutes': a.extra_minutes or 0,
                'duration_min':  duration_min,
                'submitted_at':  a.submitted_at.isoformat() if a.submitted_at else None,
                'corrected_at':  a.corrected_at.isoformat() if a.corrected_at else None,
                'note_count':    note_counts.get(a.id, 0),
            })

        rows_out.sort(key=lambda r: (r['status'] != 'submitted', -(r['score'] or -1)))
        exam_title = exam.title
        session.close()
        return jsonify({'exam_title': exam_title, 'attempts': rows_out})
    except Exception as e:
        print(f"❌ get_exam_bilan {exam_id}: {e}")
        try: session.close()
        except: pass
        return jsonify({'error': str(e)}), 500


@app.route('/api/online_exams/<int:exam_id>/bilan/pdf', methods=['GET'])
@paseto_required
def get_exam_bilan_pdf(exam_id):
    """Génère un PDF du bilan par étudiant avec reportlab."""
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib import colors
        from reportlab.lib.units import cm
        from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.enums import TA_CENTER, TA_LEFT

        user_id = get_current_user_id()
        session = get_session()
        user = session.query(User).filter_by(id=user_id).first()
        if not user or user.role not in [UserRole.PROFESSOR, UserRole.ADMIN]:
            session.close()
            return jsonify({'error': 'Accès non autorisé'}), 403
        exam = session.query(OnlineExam).filter_by(id=exam_id).first()
        if not exam:
            session.close()
            return jsonify({'error': 'Examen non trouvé'}), 404

        attempts = session.query(ExamAttempt).filter_by(exam_id=exam_id).all()
        exam_title = exam.title
        generated_at = utcnow().strftime('%d/%m/%Y %H:%M')

        status_labels = {
            'submitted': 'Soumis', 'auto_submitted': 'Auto-soumis',
            'in_progress': 'En cours', 'banned': 'Exclu', 'not_started': 'Absent'
        }

        rows_data = []
        scores = []
        for a in sorted(attempts, key=lambda x: (x.student.full_name if x.student else '')):
            sv = (a.status.value if hasattr(a.status, 'value') else str(a.status)) if a.status else ''
            dur = None
            if a.submitted_at and a.started_at:
                dur = round((a.submitted_at - a.started_at).total_seconds() / 60, 0)
            rows_data.append({
                'name':    a.student.full_name if a.student else '—',
                'status':  status_labels.get(sv, sv),
                'score':   a.score,
                'risk':    a.risk_score or 0,
                'dur':     int(dur) if dur is not None else None,
                'extra':   a.extra_minutes or 0,
            })
            if a.score is not None:
                scores.append(a.score)

        session.close()

        buf = io.BytesIO()
        doc = SimpleDocTemplate(buf, pagesize=A4,
                                leftMargin=2*cm, rightMargin=2*cm,
                                topMargin=2*cm, bottomMargin=2*cm)
        styles = getSampleStyleSheet()
        title_style = ParagraphStyle('title', parent=styles['Title'], fontSize=16, spaceAfter=4)
        sub_style   = ParagraphStyle('sub',   parent=styles['Normal'], fontSize=10, textColor=colors.grey, spaceAfter=12)
        story = [
            Paragraph(f"Bilan — {exam_title}", title_style),
            Paragraph(f"Généré le {generated_at} • {len(rows_data)} participant(s) • Moyenne : {round(sum(scores)/len(scores),2) if scores else '—'}/20", sub_style),
            Spacer(1, 0.3*cm),
        ]

        header = ['Étudiant', 'Statut', 'Note /20', 'Risque', 'Durée', 'Extra']
        table_data = [header]
        for r in rows_data:
            table_data.append([
                r['name'],
                r['status'],
                f"{r['score']:.2f}" if r['score'] is not None else '—',
                f"{r['risk']}%",
                f"{r['dur']} min" if r['dur'] is not None else '—',
                f"+{r['extra']} min" if r['extra'] > 0 else '—',
            ])

        col_widths = [6*cm, 3*cm, 2.5*cm, 2*cm, 2*cm, 2*cm]
        tbl = Table(table_data, colWidths=col_widths, repeatRows=1)

        ts = TableStyle([
            ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#1e293b')),
            ('TEXTCOLOR',  (0,0), (-1,0), colors.white),
            ('FONTNAME',   (0,0), (-1,0), 'Helvetica-Bold'),
            ('FONTSIZE',   (0,0), (-1,-1), 9),
            ('ALIGN',      (1,0), (-1,-1), 'CENTER'),
            ('ALIGN',      (0,0), (0,-1), 'LEFT'),
            ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.white, colors.HexColor('#f8fafc')]),
            ('GRID',       (0,0), (-1,-1), 0.3, colors.HexColor('#e2e8f0')),
            ('TOPPADDING', (0,0), (-1,-1), 5),
            ('BOTTOMPADDING', (0,0), (-1,-1), 5),
        ])
        # Colorer les notes
        for i, r in enumerate(rows_data, start=1):
            if r['score'] is not None:
                c = colors.HexColor('#059669') if r['score'] >= 10 else colors.HexColor('#dc2626')
                ts.add('TEXTCOLOR', (2,i), (2,i), c)
                ts.add('FONTNAME',  (2,i), (2,i), 'Helvetica-Bold')
        tbl.setStyle(ts)
        story.append(tbl)

        doc.build(story)
        buf.seek(0)
        safe_title = ''.join(c for c in exam_title if c.isalnum() or c in '-_ ')[:40]
        response = make_response(buf.read())
        response.headers['Content-Type'] = 'application/pdf'
        response.headers['Content-Disposition'] = f'attachment; filename="bilan-{safe_title}.pdf"'
        return response
    except Exception as e:
        import traceback
        print(f"❌ get_exam_bilan_pdf {exam_id}: {e}\n{traceback.format_exc()}")
        try: session.close()
        except: pass
        return jsonify({'error': str(e)}), 500


# ============================================================================
# CORRECTION MANUELLE
# ============================================================================

@app.route('/api/exam_attempts/<int:attempt_id>/manual-grade', methods=['PUT'])
@paseto_required
def manual_grade_attempt(attempt_id):
    """Correction manuelle par le professeur : saisie note + commentaire."""
    try:
        user_id = get_current_user_id()
        session = get_session()
        user = session.query(User).filter_by(id=user_id).first()
        if user.role not in [UserRole.PROFESSOR, UserRole.ADMIN]:
            session.close()
            return jsonify({'error': 'Accès non autorisé'}), 403
        attempt = session.query(ExamAttempt).filter_by(id=attempt_id).first()
        if not attempt:
            session.close()
            return jsonify({'error': 'Tentative non trouvée'}), 404
        data   = request.get_json(silent=True) or {}
        score  = data.get('score')
        feedback = data.get('feedback', '').strip()
        if score is None:
            session.close()
            return jsonify({'error': 'Note obligatoire'}), 400
        try:
            score = float(score)
        except (ValueError, TypeError):
            session.close()
            return jsonify({'error': 'Note invalide'}), 400
        if not (0 <= score <= 20):
            session.close()
            return jsonify({'error': 'Note doit être entre 0 et 20'}), 400
        attempt.score          = score
        attempt.feedback       = feedback
        attempt.corrected_at   = utcnow()
        attempt.corrected_by_id = user_id
        session.commit()
        student_email = attempt.student.email if attempt.student else None
        attempt_id_copy = attempt.id
        session.close()
        return jsonify({'success': True, 'score': score, 'message': 'Note enregistrée'})
    except Exception as e:
        print(f"❌ manual_grade_attempt {attempt_id}: {e}")
        try: session.rollback(); session.close()
        except: pass
        return jsonify({'error': str(e)}), 500


# ============================================================================
# HISTORIQUE EXAMENS ÉTUDIANT
# ============================================================================

@app.route('/api/student/exam-history', methods=['GET'])
@paseto_required
def get_student_exam_history():
    """Historique complet des examens passés par l'étudiant connecté."""
    try:
        user_id = get_current_user_id()
        session = get_session()
        attempts = session.query(ExamAttempt).options(
            joinedload(ExamAttempt.exam)
        ).filter_by(student_id=user_id).order_by(ExamAttempt.started_at.desc()).all()
        history = []
        for a in attempts:
            exam = a.exam
            dur  = int((a.submitted_at - a.started_at).total_seconds() / 60) if a.submitted_at and a.started_at else None
            history.append({
                'attempt_id':   a.id,
                'exam_id':      a.exam_id,
                'exam_title':   exam.title if exam else '?',
                'status':       a.status.value,
                'score':        a.score,
                'feedback':     a.feedback,
                'risk_score':   a.risk_score or 0,
                'started_at':   a.started_at.isoformat() if a.started_at else None,
                'submitted_at': a.submitted_at.isoformat() if a.submitted_at else None,
                'duration_min': dur,
                'tab_switches': a.tab_switches or 0,
                'warnings':     a.warnings_count or 0,
                'has_pre_sig':  bool(a.pre_exam_signature_data),
                'corrected_at': a.corrected_at.isoformat() if a.corrected_at else None,
            })
        session.close()
        return jsonify({'history': history, 'total': len(history)})
    except Exception as e:
        print(f"❌ get_student_exam_history: {e}")
        try: session.close()
        except: pass
        return jsonify({'error': str(e)}), 500


# ============================================================================
# DÉTECTION DE PLAGIAT
# ============================================================================

@app.route('/api/online_exams/<int:exam_id>/plagiarism-check', methods=['GET'])
@paseto_required
def plagiarism_check(exam_id):
    """Détecte les copies suspectes en comparant les réponses soumises."""
    from difflib import SequenceMatcher
    import json as _json
    try:
        user_id = get_current_user_id()
        session = get_session()
        user = session.query(User).filter_by(id=user_id).first()
        if user.role not in [UserRole.PROFESSOR, UserRole.ADMIN]:
            session.close()
            return jsonify({'error': 'Accès non autorisé'}), 403
        exam = session.query(OnlineExam).filter_by(id=exam_id).first()
        if not exam:
            session.close()
            return jsonify({'error': 'Examen non trouvé'}), 404
        threshold = float(request.args.get('threshold', 0.75))
        attempts = session.query(ExamAttempt).options(
            joinedload(ExamAttempt.student)
        ).filter(
            ExamAttempt.exam_id == exam_id,
            ExamAttempt.status.in_([AttemptStatus.SUBMITTED, AttemptStatus.AUTO_SUBMITTED])
        ).all()
        def extract_text(answers_raw):
            if not answers_raw:
                return ''
            try:
                data = answers_raw if isinstance(answers_raw, dict) else _json.loads(answers_raw)
                parts = []
                if isinstance(data, dict):
                    for v in data.values():
                        parts.append(str(v))
                elif isinstance(data, list):
                    for item in data:
                        if isinstance(item, dict):
                            parts.append(str(item.get('answer', item.get('response', ''))))
                        else:
                            parts.append(str(item))
                return ' '.join(parts).lower().strip()
            except Exception:
                return str(answers_raw).lower().strip()
        suspicious = []
        for i in range(len(attempts)):
            for j in range(i+1, len(attempts)):
                a1, a2 = attempts[i], attempts[j]
                t1 = extract_text(a1.answers)
                t2 = extract_text(a2.answers)
                if not t1 or not t2 or len(t1) < 30 or len(t2) < 30:
                    continue
                ratio = SequenceMatcher(None, t1, t2).ratio()
                if ratio >= threshold:
                    suspicious.append({
                        'student1_id':   a1.student_id,
                        'student1_name': a1.student.full_name if a1.student else '?',
                        'attempt1_id':   a1.id,
                        'student2_id':   a2.student_id,
                        'student2_name': a2.student.full_name if a2.student else '?',
                        'attempt2_id':   a2.id,
                        'similarity':    round(ratio * 100, 1),
                        'level':         'CRITIQUE' if ratio >= 0.9 else 'SUSPECT',
                    })
        suspicious.sort(key=lambda x: x['similarity'], reverse=True)
        session.close()
        return jsonify({
            'exam_title':    exam.title,
            'total_checked': len(attempts),
            'threshold_pct': round(threshold * 100),
            'suspicious':    suspicious,
            'total_pairs':   len(suspicious),
        })
    except Exception as e:
        import traceback
        print(f"❌ Erreur plagiarism_check exam {exam_id}: {e}\n{traceback.format_exc()}")
        try: session.close()
        except: pass
        return jsonify({'error': str(e)}), 500


# ============================================================================
# RAPPORT D'INTÉGRITÉ PDF
# ============================================================================

@app.route('/api/security/face_references', methods=['GET'])
@paseto_required
def list_face_references():
    """Liste toutes les photos de référence pour les examens du prof/admin."""
    user_id = get_current_user_id()
    session = get_session()
    try:
        user = session.query(User).filter_by(id=user_id).first()
        if not user or user.role not in [UserRole.ADMIN, UserRole.PROFESSOR]:
            session.close()
            return jsonify({'error': 'Accès non autorisé'}), 403

        # Récupérer les tentatives concernées
        if user.role == UserRole.PROFESSOR:
            exam_ids = [e.id for e in session.query(OnlineExam).filter_by(created_by_id=user_id).all()]
            if not exam_ids:
                session.close()
                return jsonify({'references': []})
            attempts = session.query(ExamAttempt).filter(
                ExamAttempt.exam_id.in_(exam_ids)
            ).all()
        else:
            attempts = session.query(ExamAttempt).all()

        attempt_ids = [a.id for a in attempts]
        attempt_map = {a.id: a for a in attempts}

        import json as _json
        results = []

        # Chercher dans CameraLog d'abord
        cam_logs = session.query(CameraLog).filter(
            CameraLog.attempt_id.in_(attempt_ids),
            CameraLog.event_type == 'face_reference_captured'
        ).all() if attempt_ids else []

        cam_by_attempt = {}
        for c in cam_logs:
            if c.attempt_id not in cam_by_attempt:
                cam_by_attempt[c.attempt_id] = c

        # Chercher dans ExamActivityLog pour les events sans photo dans CameraLog
        act_logs = session.query(ExamActivityLog).filter(
            ExamActivityLog.attempt_id.in_(attempt_ids),
            ExamActivityLog.event_type == 'face_reference_captured'
        ).all() if attempt_ids else []

        processed = set()
        for log in act_logs:
            if log.attempt_id in processed:
                continue
            processed.add(log.attempt_id)
            att = attempt_map.get(log.attempt_id)
            if not att:
                continue
            student = session.query(User).filter_by(id=att.student_id).first()
            exam = session.query(OnlineExam).filter_by(id=att.exam_id).first()

            image_data = None
            cam = cam_by_attempt.get(log.attempt_id)
            if cam and cam.image_data:
                image_data = cam.image_data
            else:
                try:
                    parsed = _json.loads(log.event_data or '{}')
                    if isinstance(parsed, dict):
                        image_data = parsed.get('image_data') or parsed.get('photo')
                except Exception:
                    pass

            results.append({
                'attempt_id':   att.id,
                'student_name': student.full_name if student else '—',
                'exam_title':   exam.title if exam else '—',
                'captured_at':  log.timestamp.isoformat() if log.timestamp else None,
                'image_data':   image_data,
                'has_photo':    bool(image_data)
            })

        session.close()
        return jsonify({'references': results})
    except Exception as e:
        try: session.rollback(); session.close()
        except: pass
        return jsonify({'error': str(e)}), 500


@app.route('/api/exam_attempts/<int:attempt_id>/face_reference', methods=['GET'])
@paseto_required
def get_face_reference_photo(attempt_id):
    """Retourne la photo de référence de l'étudiant pour une tentative (prof/admin)."""
    user_id = get_current_user_id()
    session = get_session()
    try:
        user = session.query(User).filter_by(id=user_id).first()
        if not user or user.role not in [UserRole.ADMIN, UserRole.PROFESSOR]:
            session.close()
            return jsonify({'error': 'Accès non autorisé'}), 403

        attempt = session.query(ExamAttempt).filter_by(id=attempt_id).first()
        if not attempt:
            session.close()
            return jsonify({'error': 'Tentative non trouvée'}), 404

        # Vérification accès prof
        if user.role == UserRole.PROFESSOR:
            exam = session.query(OnlineExam).filter_by(id=attempt.exam_id).first()
            if not exam or exam.created_by_id != user_id:
                session.close()
                return jsonify({'error': 'Accès non autorisé à cet examen'}), 403

        # Chercher dans CameraLog
        cam = session.query(CameraLog).filter_by(
            attempt_id=attempt_id,
            event_type='face_reference_captured'
        ).order_by(CameraLog.timestamp.asc()).first()

        if cam and cam.image_data:
            session.close()
            return jsonify({'image_data': cam.image_data, 'source': 'camera_log'})

        # Fallback : chercher dans event_data de ExamActivityLog
        import json as _json
        log = session.query(ExamActivityLog).filter_by(
            attempt_id=attempt_id,
            event_type='face_reference_captured'
        ).first()
        if log and log.event_data:
            try:
                parsed = _json.loads(log.event_data)
                if isinstance(parsed, dict) and ('image_data' in parsed or 'photo' in parsed):
                    img = parsed.get('image_data') or parsed.get('photo')
                    session.close()
                    return jsonify({'image_data': img, 'source': 'activity_log'})
            except Exception:
                pass

        session.close()
        return jsonify({'image_data': None, 'message': 'Photo non disponible (capture non stockée)'})
    except Exception as e:
        try: session.rollback(); session.close()
        except: pass
        return jsonify({'error': str(e)}), 500


@app.route('/api/exam_attempts/<int:attempt_id>/integrity-report', methods=['GET'])
@paseto_required
def download_integrity_report(attempt_id):
    """Génère un rapport d'intégrité PDF pour une tentative (prof/admin)."""
    import base64, textwrap
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import cm
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image as RLImage
    from reportlab.lib import colors as rl_colors
    from reportlab.lib.enums import TA_CENTER, TA_LEFT
    try:
        user_id = get_current_user_id()
        session = get_session()
        user = session.query(User).filter_by(id=user_id).first()
        if user.role not in [UserRole.PROFESSOR, UserRole.ADMIN]:
            session.close()
            return jsonify({'error': 'Accès non autorisé'}), 403
        attempt = session.query(ExamAttempt).options(
            joinedload(ExamAttempt.student),
            joinedload(ExamAttempt.exam),
        ).filter_by(id=attempt_id).first()
        if not attempt:
            session.close()
            return jsonify({'error': 'Tentative non trouvée'}), 404
        student = attempt.student
        exam    = attempt.exam
        logs    = session.query(ExamActivityLog).filter_by(attempt_id=attempt_id).order_by(ExamActivityLog.timestamp).all()
        session.close()

        buf = io.BytesIO()
        doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=2*cm, rightMargin=2*cm, topMargin=2*cm, bottomMargin=2*cm)
        styles = getSampleStyleSheet()
        title_style = ParagraphStyle('t', parent=styles['Title'], fontSize=16, textColor=rl_colors.HexColor('#1e293b'), spaceAfter=6)
        h2_style    = ParagraphStyle('h2', parent=styles['Heading2'], fontSize=12, textColor=rl_colors.HexColor('#2563eb'), spaceBefore=14, spaceAfter=4)
        normal      = ParagraphStyle('n', parent=styles['Normal'], fontSize=9, leading=13)
        small       = ParagraphStyle('s', parent=styles['Normal'], fontSize=8, textColor=rl_colors.HexColor('#64748b'))
        story = []

        # Entête
        story.append(Paragraph('RAPPORT D\'INTÉGRITÉ — CEI', title_style))
        story.append(Paragraph(f'Examen : {exam.title if exam else "?"}', styles['Heading2']))
        story.append(Paragraph(f'Généré le {utcnow().strftime("%d/%m/%Y à %H:%M")} UTC', small))
        story.append(Spacer(1, 12))

        # Infos étudiant
        story.append(Paragraph('Informations étudiant', h2_style))
        info_data = [
            ['Nom complet', student.full_name if student else '?'],
            ['Email', student.email if student else ''],
            ['Statut tentative', attempt.status.value],
            ['Note obtenue', f'{attempt.score}/20' if attempt.score is not None else 'Non corrigé'],
            ['Démarré le', attempt.started_at.strftime('%d/%m/%Y %H:%M') if attempt.started_at else '—'],
            ['Soumis le', attempt.submitted_at.strftime('%d/%m/%Y %H:%M') if attempt.submitted_at else '—'],
        ]
        if attempt.submitted_at and attempt.started_at:
            dur = int((attempt.submitted_at - attempt.started_at).total_seconds() / 60)
            info_data.append(['Durée', f'{dur} minutes'])
        t = Table(info_data, colWidths=[5*cm, 12*cm])
        t.setStyle(TableStyle([
            ('BACKGROUND', (0,0), (0,-1), rl_colors.HexColor('#f1f5f9')),
            ('FONTSIZE', (0,0), (-1,-1), 9),
            ('GRID', (0,0), (-1,-1), 0.5, rl_colors.HexColor('#e2e8f0')),
            ('PADDING', (0,0), (-1,-1), 5),
        ]))
        story.append(t)
        story.append(Spacer(1, 10))

        # Score de risque
        story.append(Paragraph('Indicateurs de surveillance', h2_style))
        risk_color = rl_colors.red if (attempt.risk_score or 0) >= 70 else rl_colors.orange if (attempt.risk_score or 0) >= 40 else rl_colors.green
        risk_data = [
            ['Score de risque', f'{attempt.risk_score or 0}/100'],
            ['Tab switches', str(attempt.tab_switches or 0)],
            ['Alertes comportementales', str(attempt.warnings_count or 0)],
            ['Signature pré-examen', 'Présente ✓' if attempt.pre_exam_signature_data else 'Absente ✗'],
            ['Signature post-examen', 'Présente ✓' if attempt.signature_data else ('Auto-soumission' if attempt.status.value == 'auto_submitted' else 'Absente ✗')],
        ]
        rt = Table(risk_data, colWidths=[5*cm, 12*cm])
        rt.setStyle(TableStyle([
            ('BACKGROUND', (0,0), (0,-1), rl_colors.HexColor('#f1f5f9')),
            ('FONTSIZE', (0,0), (-1,-1), 9),
            ('GRID', (0,0), (-1,-1), 0.5, rl_colors.HexColor('#e2e8f0')),
            ('PADDING', (0,0), (-1,-1), 5),
            ('TEXTCOLOR', (1,0), (1,0), risk_color),
            ('FONTNAME', (1,0), (1,0), 'Helvetica-Bold'),
        ]))
        story.append(rt)
        story.append(Spacer(1, 10))

        # Signature pré-examen
        if attempt.pre_exam_signature_data:
            story.append(Paragraph('Signature pré-examen (attestation)', h2_style))
            try:
                sig_data = attempt.pre_exam_signature_data
                if ',' in sig_data:
                    sig_data = sig_data.split(',', 1)[1]
                sig_bytes = base64.b64decode(sig_data)
                sig_buf = io.BytesIO(sig_bytes)
                img = RLImage(sig_buf, width=8*cm, height=4*cm)
                story.append(img)
                if attempt.pre_exam_signature_meta:
                    try:
                        meta = json.loads(attempt.pre_exam_signature_meta)
                        story.append(Paragraph(
                            f"Traits: {meta.get('strokes','?')} · Durée: {round((meta.get('duration_ms',0))/1000,1)}s · Longueur: {round(meta.get('path_length',0))}px",
                            small
                        ))
                    except Exception: pass
            except Exception as e:
                story.append(Paragraph(f'[Signature non lisible: {e}]', small))
            story.append(Spacer(1, 8))

        # Timeline des événements
        if logs:
            story.append(Paragraph('Chronologie des événements', h2_style))
            log_data = [['Heure', 'Type', 'Détail']]
            for log in logs[:50]:
                ts = log.timestamp.strftime('%H:%M:%S') if log.timestamp else '—'
                detail = (log.event_data or '')[:80]
                log_data.append([ts, log.event_type or '?', detail])
            lt = Table(log_data, colWidths=[2*cm, 4*cm, 11*cm])
            lt.setStyle(TableStyle([
                ('BACKGROUND', (0,0), (-1,0), rl_colors.HexColor('#1e293b')),
                ('TEXTCOLOR', (0,0), (-1,0), rl_colors.white),
                ('FONTSIZE', (0,0), (-1,-1), 8),
                ('GRID', (0,0), (-1,-1), 0.3, rl_colors.HexColor('#e2e8f0')),
                ('PADDING', (0,0), (-1,-1), 4),
                ('ROWBACKGROUNDS', (0,1), (-1,-1), [rl_colors.white, rl_colors.HexColor('#f8fafc')]),
            ]))
            story.append(lt)

        doc.build(story)
        buf.seek(0)
        safe_name = (student.full_name if student else 'etudiant').replace(' ', '_')
        return send_file(buf, mimetype='application/pdf', as_attachment=True,
                         download_name=f'rapport_integrite_{safe_name}_{attempt_id}.pdf')
    except Exception as e:
        try: session.close()
        except: pass
        print(f'Erreur integrity_report: {e}')
        return jsonify({'error': str(e)}), 500


# ============================================================================
# LOGS ET INCIDENTS DES EXAMENS
# ============================================================================

@app.route('/api/online_exams/<int:exam_id>/incidents', methods=['GET'])
@paseto_required
def get_exam_incidents(exam_id):
    """Récupérer tous les incidents/logs d'un examen (professeur/admin)"""
    try:
        user_id = get_current_user_id()
        session = get_session()
        
        user = session.query(User).filter_by(id=user_id).first()
        if user.role not in [UserRole.PROFESSOR, UserRole.ADMIN, UserRole.SURVEILLANT]:
            session.close()
            return jsonify({'error': 'Accès non autorisé'}), 403

        exam = session.query(OnlineExam).filter_by(id=exam_id).first()
        if not exam:
            session.close()
            return jsonify({'error': 'Examen non trouvé'}), 404

        if user.role == UserRole.PROFESSOR and exam.created_by_id != user_id:
            session.close()
            return jsonify({'error': 'Accès non autorisé'}), 403

        # Récupérer les logs — pour un surveillant, uniquement ses étudiants assignés
        if user.role == UserRole.SURVEILLANT:
            assigned_attempt_ids = [
                pa.attempt_id for pa in session.query(ProctorAssignment).filter_by(proctor_id=user_id).all()
            ]
            logs = session.query(ExamActivityLog).join(ExamAttempt).filter(
                ExamAttempt.exam_id == exam_id,
                ExamActivityLog.attempt_id.in_(assigned_attempt_ids)
            ).order_by(ExamActivityLog.timestamp.desc()).all()
        else:
            logs = session.query(ExamActivityLog).join(ExamAttempt).filter(
                ExamAttempt.exam_id == exam_id
            ).order_by(ExamActivityLog.timestamp.desc()).all()
        
        incidents_list = []
        for log in logs:
            log_dict = log.to_dict()
            log_dict['student_name'] = log.attempt.student.full_name if log.attempt.student else 'Inconnu'
            log_dict['student_id'] = log.attempt.student_id
            log_dict['severity'] = 'high' if log.event_type in ['tab_switch', 'devtools_attempt'] else 'medium'
            incidents_list.append(log_dict)
        
        # Statistiques
        total_incidents = len(logs)
        tab_switches = len([l for l in logs if l.event_type == 'tab_switch'])
        banned_students = session.query(ExamAttempt).filter_by(
            exam_id=exam_id,
            status=AttemptStatus.BANNED
        ).count()
        
        session.close()
        
        return jsonify({
            'incidents': incidents_list,
            'statistics': {
                'total_incidents': total_incidents,
                'tab_switches': tab_switches,
                'banned_students': banned_students
            }
        })
        
    except Exception as e:
        print(f"❌ Erreur get_exam_incidents: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/professor/recent_incidents', methods=['GET'])
@paseto_required
def get_professor_recent_incidents():
    """Récupérer les incidents récents pour le professeur connecté"""
    try:
        user_id = get_current_user_id()
        session = get_session()
        
        user = session.query(User).filter_by(id=user_id).first()
        if user.role != UserRole.PROFESSOR:
            session.close()
            return jsonify({'error': 'Accès réservé aux professeurs'}), 403
        
        # Récupérer les examens actifs du professeur
        active_exams = session.query(OnlineExam).filter_by(
            created_by_id=user_id,
            status=ExamStatus.ACTIVE
        ).all()
        
        exam_ids = [e.id for e in active_exams]
        
        if not exam_ids:
            session.close()
            return jsonify({'incidents': [], 'unread_count': 0})
        
        # Incidents des dernières 24h
        since = utcnow() - timedelta(hours=24)
        
        incidents = session.query(ExamActivityLog).join(ExamAttempt).filter(
            ExamAttempt.exam_id.in_(exam_ids),
            ExamActivityLog.timestamp >= since
        ).order_by(ExamActivityLog.timestamp.desc()).limit(50).all()
        
        incidents_list = []
        for incident in incidents:
            incident_dict = incident.to_dict()
            incident_dict['student_name'] = incident.attempt.student.full_name
            incident_dict['exam_title'] = incident.attempt.exam.title
            incident_dict['severity'] = 'high' if incident.event_type in ['tab_switch', 'devtools_attempt'] else 'medium'
            incidents_list.append(incident_dict)
        
        # #12 — EC récemment affectés (7 derniers jours)
        ec_since = utcnow() - timedelta(days=7)
        new_assignments = session.query(ECAssignment).filter(
            ECAssignment.professor_id == user_id,
            ECAssignment.assigned_at >= ec_since
        ).all()
        ec_notifs = []
        for asgn in new_assignments:
            ec = asgn.ec
            ec_notifs.append({
                'id': f'ec_assign_{asgn.id}',
                'type': 'ec_assignment',
                'event_type': 'ec_assignment',
                'timestamp': asgn.assigned_at.isoformat() if asgn.assigned_at else None,
                'details': f"Vous avez été affecté(e) à l'EC : {ec.name if ec else asgn.ec_id}",
                'severity': 'info'
            })

        all_items = ec_notifs + incidents_list
        session.close()

        return jsonify({
            'incidents': all_items,
            'unread_count': len(all_items)
        })

    except Exception as e:
        print(f"❌ Erreur get_professor_recent_incidents: {e}")
        return jsonify({'error': str(e)}), 500


# ============================================================================
# HISTORIQUE DES EXAMENS (ADMIN)
# ============================================================================

@app.route('/api/admin/exams_history', methods=['GET'])
@paseto_required
def get_exams_history():
    """Historique des examens terminés avec statistiques (admin only)"""
    try:
        user_id = get_current_user_id()
        session = get_session()
        
        user = session.query(User).filter_by(id=user_id).first()
        if user.role != UserRole.ADMIN:
            session.close()
            return jsonify({'error': 'Accès réservé aux administrateurs'}), 403
        
        # Récupérer tous les examens terminés
        closed_exams = session.query(OnlineExam).filter_by(
            status=ExamStatus.CLOSED
        ).order_by(OnlineExam.end_time.desc()).all()
        
        history_list = []
        for exam in closed_exams:
            attempts = session.query(ExamAttempt).filter_by(exam_id=exam.id).all()
            
            submitted_count = len([a for a in attempts if a.status in [AttemptStatus.SUBMITTED, AttemptStatus.AUTO_SUBMITTED]])
            banned_count = len([a for a in attempts if a.status == AttemptStatus.BANNED])
            corrected_count = len([a for a in attempts if a.score is not None])
            
            # Moyenne des notes
            scores = [a.score for a in attempts if a.score is not None]
            average_score = round(sum(scores) / len(scores), 2) if scores else 0
            
            # Incidents totaux
            incidents_count = session.query(ExamActivityLog).join(ExamAttempt).filter(
                ExamAttempt.exam_id == exam.id
            ).count()
            
            history_list.append({
                'id': exam.id,
                'title': exam.title,
                'subject_title': exam.subject.title if exam.subject else None,
                'creator_name': exam.creator.full_name if exam.creator else None,
                'start_time': exam.start_time.isoformat(),
                'end_time': exam.end_time.isoformat(),
                'duration_minutes': exam.duration_minutes,
                'total_attempts': len(attempts),
                'submitted_count': submitted_count,
                'banned_count': banned_count,
                'corrected_count': corrected_count,
                'average_score': average_score,
                'incidents_count': incidents_count,
                'created_at': exam.created_at.isoformat()
            })
        
        session.close()
        return jsonify(history_list)
        
    except Exception as e:
        print(f" Erreur get_exams_history: {e}")
        return jsonify({'error': str(e)}), 500

# ============================================================================
# NOUVEAU : CRÉATION ÉTUDIANT SANS EMAIL
# ============================================================================

@app.route('/api/admin/users/student-no-email', methods=['POST'])
@paseto_required
def create_student_no_email():
    """Créer un étudiant sans adresse email"""
    try:
        admin_id = get_current_user_id()
        session = get_session()

        admin = session.query(User).filter_by(id=admin_id).first()
        if admin.role != UserRole.ADMIN:
            session.close()
            return jsonify({'error': 'Accès non autorisé'}), 403

        data = request.json
        full_name = data.get('full_name')
        
        if not full_name:
            session.close()
            return jsonify({'error': 'Nom complet requis'}), 400

        # Vérifier si un étudiant avec ce nom existe déjà
        existing = session.query(User).filter_by(
            full_name=full_name,
            role=UserRole.STUDENT
        ).first()
        
        if existing:
            session.close()
            return jsonify({'error': 'Un étudiant avec ce nom existe déjà'}), 400

        # Générer un email temporaire unique basé sur le nom
        safe_name = normalize_name(full_name).replace(' ', '.')
        temp_email = f"{safe_name}.{datetime.now().strftime('%Y%m%d%H%M%S')}@noemail.local"
        
        # Mot de passe par défaut
        temp_password = f"Student{datetime.now().year}"
        hashed_password = bcrypt.generate_password_hash(temp_password).decode('utf-8')

        new_user = User(
            email=temp_email,
            password_hash=hashed_password,
            full_name=full_name,
            role=UserRole.STUDENT,
            has_email=False  # ✅ Marquer comme sans email
        )

        session.add(new_user)
        session.commit()
        user_dict = new_user.to_dict()
        session.close()

        return jsonify({
            'success': True,
            'message': 'Étudiant créé sans email',
            'user': user_dict,
            'temp_password': temp_password
        }), 201
        
    except Exception as e:
        print(f"❌ Erreur create_student_no_email: {e}")
        return jsonify({'error': str(e)}), 500

# ============================================================================
# NOUVEAU : LISTE DES COPIES CORRIGÉES (PROFESSEUR)
# ============================================================================

@app.route('/api/professor/corrected_papers', methods=['GET'])
@paseto_required
def professor_corrected_papers():
    """Liste des copies corrigées : copies papier + examens en ligne"""
    try:
        user_id = get_current_user_id()
        session = get_session()

        user = session.query(User).filter_by(id=user_id).first()
        if user.role not in [UserRole.PROFESSOR, UserRole.ADMIN]:
            session.close()
            return jsonify({'error': 'Accès non autorisé'}), 403

        # ── Copies papier ────────────────────────────────────────────────────
        paper_query = session.query(StudentPaper).options(
            joinedload(StudentPaper.student),
            joinedload(StudentPaper.subject)
        ).filter(StudentPaper.corrected_at != None)

        if user.role == UserRole.PROFESSOR:
            paper_query = paper_query.filter(StudentPaper.corrected_by_id == user_id)

        papers = paper_query.order_by(StudentPaper.corrected_at.desc()).limit(100).all()

        papers_list = []
        for p in papers:
            papers_list.append({
                'id': p.id,
                'type': 'paper',
                'student_name':  p.student.full_name if p.student else 'Inconnu',
                'student_email': p.student.email if p.student and p.student.has_email else 'Pas d\'email',
                'subject_title': p.subject.title if p.subject else 'N/A',
                'score': p.score,
                'corrected_at': p.corrected_at.isoformat() if p.corrected_at else None,
                'email_sent': p.email_sent,
                'filename': p.filename
            })

        # ── Examens en ligne corrigés ────────────────────────────────────────
        attempt_query = session.query(ExamAttempt).options(
            joinedload(ExamAttempt.student),
            joinedload(ExamAttempt.exam).joinedload(OnlineExam.subject)
        ).join(OnlineExam, ExamAttempt.exam_id == OnlineExam.id).filter(
            ExamAttempt.score.isnot(None)
        )

        if user.role == UserRole.PROFESSOR:
            attempt_query = attempt_query.filter(OnlineExam.created_by_id == user_id)

        attempts = attempt_query.order_by(ExamAttempt.corrected_at.desc()).limit(100).all()

        for att in attempts:
            papers_list.append({
                'id': att.id,
                'type': 'online',
                'student_name':  att.student.full_name if att.student else 'Inconnu',
                'student_email': att.student.email if att.student else 'Pas d\'email',
                'subject_title': att.exam.title if att.exam else 'Examen en ligne',
                'score': att.score,
                'corrected_at': (att.corrected_at or att.submitted_at).isoformat() if (att.corrected_at or att.submitted_at) else None,
                'email_sent': False,
                'exam_id': att.exam_id
            })

        # Tri global par date décroissante
        papers_list.sort(key=lambda x: x['corrected_at'] or '', reverse=True)

        session.close()
        return jsonify({'papers': papers_list})

    except Exception as e:
        print(f"❌ Erreur professor_corrected_papers: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/ai/generate-exam-suggestions', methods=['POST'])
@paseto_required
def generate_exam_suggestions():
    """Génère des suggestions de sujets d'examen à partir d'un cours uploadé"""
    current_user_id = get_current_user_id()
    session = get_session()
    
    user = session.query(User).filter_by(id=int(current_user_id)).first()
    
    if not user or user.role not in [UserRole.PROFESSOR, UserRole.ADMIN]:
        session.close()
        return jsonify({'success': False, 'error': 'Accès non autorisé'}), 403
    
    try:
        # ✅ NOUVELLE VERSION : Upload du fichier cours
        if 'course_file' not in request.files:
            session.close()
            return jsonify({'success': False, 'error': 'Fichier cours requis'}), 400
        
        file = request.files['course_file']
        
        if file.filename == '':
            session.close()
            return jsonify({'success': False, 'error': 'Aucun fichier sélectionné'}), 400
        
        if not allowed_file(file.filename):
            session.close()
            return jsonify({'success': False, 'error': 'Type de fichier non autorisé (PDF, DOCX, TXT uniquement)'}), 400
        
        # Sauvegarder temporairement le fichier
        filename = secure_filename(file.filename)
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        temp_filename = f"course_{timestamp}_{filename}"
        temp_filepath = os.path.join(app.config['UPLOAD_FOLDER'], temp_filename)
        file.save(temp_filepath)
        
        # Extraire le contenu du cours
        course_content = extract_text_from_file(temp_filepath)
        
        if not course_content or len(course_content.strip()) < 100:
            os.remove(temp_filepath)
            session.close()
            return jsonify({
                'success': False, 
                'error': 'Le contenu du cours est trop court ou illisible (minimum 100 caractères)'
            }), 400
        
        # Récupérer les paramètres additionnels du formulaire
        difficulty = request.form.get('difficulty', 'Moyen')
        student_level = request.form.get('student_level', 'Licence')
        exam_type = request.form.get('exam_type', '')
        # Types de questions choisis par l'utilisateur (QCM, Vrai/Faux, Questions ouvertes)
        question_types = request.form.get('question_types', '')

        # Construire la contrainte de type selon les choix de l'utilisateur
        if question_types:
            types_list = [t.strip() for t in question_types.split(',') if t.strip()]
            if len(types_list) == 1:
                forced_type_line = f"- Type d'examen OBLIGATOIRE : {types_list[0]}"
                suggested_exam_type_hint = f"({types_list[0]})"
            else:
                combined = ' + '.join(types_list)
                forced_type_line = f"- Types de questions OBLIGATOIRES à inclure : {combined} (examen mixte)"
                suggested_exam_type_hint = f"(Mixte : {combined})"
        elif exam_type:
            forced_type_line = f"- Type d'examen souhaité : {exam_type}"
            suggested_exam_type_hint = f"({exam_type})"
        else:
            forced_type_line = ""
            suggested_exam_type_hint = ""

        prompt = f"""Tu es un expert en pédagogie universitaire francophone, spécialiste dans TOUS les domaines académiques (sciences exactes, droit, médecine, lettres, sciences humaines, ingénierie, arts, langues, économie, agronomie, architecture, etc.).

CONTENU DU COURS UPLOADÉ :
{course_content[:8000]}
{"[... contenu tronqué ...]" if len(course_content) > 8000 else ""}

PARAMÈTRES :
- Niveau de difficulté : {difficulty}
- Niveau des étudiants : {student_level}
{forced_type_line}

ÉTAPE 1 — IDENTIFICATION DU DOMAINE :
Identifie d'abord silencieusement la discipline enseignée (ex: droit des obligations, biochimie, algèbre linéaire, histoire médiévale, architecture urbaine, littérature africaine, etc.) en lisant le contenu du cours.

ÉTAPE 2 — GÉNÉRATION DES SUGGESTIONS :
Génère 3 suggestions de sujets d'examen directement basées sur les concepts, théories et exercices présents dans ce cours.
{"IMPORTANT : Le type d'examen de TOUTES les suggestions doit respecter les types demandés : " + question_types if question_types else ""}

Pour chaque suggestion :
1. Un titre précis et disciplinaire
2. Une description détaillée (2-3 phrases) de ce qui sera évalué
3. Le type d'examen adapté à la discipline (QCM, Dissertation, Exercices, Étude de cas, Problème, Commentaire de texte, Calcul, TP, Oral, etc.)
4. La durée recommandée en minutes
5. 4-6 points clés extraits du cours
6. 3-5 exemples de questions concrètes issues du cours
7. Critères d'évaluation avec barème sur 20 points

Réponds UNIQUEMENT avec un JSON valide dans ce format exact :
{{
    "course_summary": "Résumé de la discipline et du contenu en 2-3 phrases",
    "detected_domain": "Domaine détecté (ex: Droit civil, Biochimie, Mathématiques...)",
    "main_topics": ["Thème 1", "Thème 2", "Thème 3"],
    "suggestions": [
        {{
            "title": "...",
            "description": "...",
            "exam_type": "...",
            "duration": 120,
            "difficulty": "{difficulty}",
            "key_points": ["point1 du cours", "point2 du cours"],
            "questions_examples": ["question issue du cours", "exercice sur le concept Y"],
            "grading_criteria": "Barème : Q1 (5pts) — ..., Q2 (8pts) — ..., Q3 (7pts) — ..."
        }}
    ]
}}
"""
        
        response_text = call_ai_simple(prompt)
        
        # Parser la réponse JSON
        import json
        import re
        
        json_match = re.search(r'\{.*\}', response_text, re.DOTALL)
        if json_match:
            suggestions_data = json.loads(json_match.group())
            detected_domain = suggestions_data.get('detected_domain', '')

            # Injecter detected_domain, student_level et question_types dans chaque suggestion
            enriched_suggestions = []
            for s in suggestions_data.get('suggestions', []):
                s['detected_domain'] = detected_domain
                s['student_level'] = student_level
                # Si l'utilisateur a forcé des types, override le exam_type de l'IA
                if question_types:
                    s['exam_type'] = question_types
                    s['question_types'] = question_types
                enriched_suggestions.append(s)

            session.close()
            return jsonify({
                'success': True,
                'course_summary': suggestions_data.get('course_summary', ''),
                'detected_domain': detected_domain,
                'main_topics': suggestions_data.get('main_topics', []),
                'suggestions': enriched_suggestions,
                'course_filename': filename
            })
        else:
            os.remove(temp_filepath)
            session.close()
            return jsonify({'success': False, 'error': 'Format de réponse IA invalide'}), 500
            
    except Exception as e:
        print(f" Erreur génération suggestions: {e}")
        import traceback
        traceback.print_exc()
        if 'temp_filepath' in locals() and os.path.exists(temp_filepath):
            os.remove(temp_filepath)
        session.close()
        err_str = str(e)
        if 'credit balance' in err_str or 'too low' in err_str:
            user_msg = "Le service d'intelligence artificielle est temporairement indisponible. Veuillez contacter l'administrateur."
        elif 'rate_limit' in err_str or 'rate limit' in err_str.lower():
            user_msg = "Trop de requêtes simultanées. Veuillez patienter quelques secondes et réessayer."
        else:
            user_msg = "Une erreur est survenue lors de la génération. Veuillez réessayer."
        return jsonify({'success': False, 'error': user_msg}), 500

@app.route('/api/subjects/generate-full-exam', methods=['POST'])
@paseto_required
def generate_full_exam_from_suggestion():
    """Génère un sujet d'examen complet avec questions numérotées et barème (sans sauvegarder)"""
    user_id = get_current_user_id()
    session = get_session()
    user = session.query(User).filter_by(id=int(user_id)).first()
    session.close()

    if not user or user.role not in [UserRole.PROFESSOR, UserRole.ADMIN]:
        return jsonify({'error': 'Accès non autorisé'}), 403

    data = request.get_json()
    suggestion = data.get('suggestion', {})

    title = suggestion.get('title', 'Examen')
    exam_type = suggestion.get('exam_type', 'Examen écrit')
    difficulty = suggestion.get('difficulty', 'Moyen')
    duration = suggestion.get('duration', 120)
    description = suggestion.get('description', '')
    key_points = suggestion.get('key_points', [])
    student_level = suggestion.get('student_level', 'Licence 3')
    questions_examples = suggestion.get('questions_examples', [])

    key_points_str = '\n'.join(f'- {p}' for p in key_points)
    examples_str = '\n'.join(f'{i+1}. {q}' for i, q in enumerate(questions_examples)) if questions_examples else ''
    examples_section = f'- Exemples de questions de base:\n{examples_str}' if examples_str else ''

    detected_domain = suggestion.get('detected_domain', '')
    domain_line = f"- Domaine disciplinaire : {detected_domain}" if detected_domain else ""

    # Récupérer les types de questions choisis par l'utilisateur (prioritaire sur exam_type de l'IA)
    question_types = suggestion.get('question_types', '')
    if question_types:
        exam_type = question_types  # override avec le choix utilisateur

    # Détecter le type d'examen pour choisir le bon template de format
    exam_type_lower = exam_type.lower()
    has_qcm   = any(k in exam_type_lower for k in ['qcm', 'choix multiple', 'mcq'])
    has_vf    = any(k in exam_type_lower for k in ['vrai', 'faux', 'vrai/faux', 'v/f'])
    has_open  = any(k in exam_type_lower for k in ['ouvert', 'open', 'développ', 'court', 'dissertation', 'synthèse', 'problème', 'cas', 'exercice', 'commentaire', 'calcul'])
    is_qcm    = has_qcm and not has_open and not has_vf
    is_vf     = has_vf and not has_qcm and not has_open
    is_mixed  = (has_qcm or has_vf) and has_open or any(k in exam_type_lower for k in ['mixte', 'mix', 'combiné', 'partiel', ',', '+'])

    # Template de format des questions selon le type
    # ── Templates avec marqueurs de type explicites ──────────────
    # Le marqueur [QCM], [VF], [OUVERT] est écrit dans le titre de chaque question.
    # Le parser JavaScript le lit en priorité → classification garantie, sans heuristiques fragiles.

    if is_qcm:
        questions_format = """Question 1 — [Titre court] ............. (1 pt) [QCM]
[Énoncé de la question, clair et précis]
A) [Premier choix — description courte, max 15 mots]
B) [Deuxième choix — description courte, max 15 mots]
C) [Troisième choix — description courte, max 15 mots]
D) [Quatrième choix — description courte, max 15 mots]

Question 2 — [Titre court] ............. (1 pt) [QCM]
[Énoncé]
A) [Choix court]
B) [Choix court]
C) [Choix court]
D) [Choix court]

[Continuer ainsi. Chaque question vaut 1 pt, total = 20 pts]"""
        format_rules = """- OBLIGATOIRE : chaque titre se termine par [QCM]
- 4 choix A) B) C) D) courts (max 15 mots chacun) — jamais de verbes d'instruction (Définissez, Expliquez...)
- Une seule bonne réponse
- 20 questions × 1 pt = 20 pts"""

    elif is_vf:
        questions_format = """Question 1 — [Affirmation à évaluer] ............. (1 pt) [VF]
Vrai / Faux

Question 2 — [Affirmation à évaluer] ............. (1 pt) [VF]
Vrai / Faux

[Continuer ainsi. Total = 20 pts]"""
        format_rules = """- OBLIGATOIRE : chaque titre se termine par [VF]
- La ligne suivante est UNIQUEMENT "Vrai / Faux" (rien d'autre)
- 20 questions × 1 pt = 20 pts"""

    elif is_mixed:
        questions_format = """Partie I — Questions à Choix Multiples (10 pts)

Question 1 — [Titre] ............. (1 pt) [QCM]
[Énoncé]
A) [Choix court]
B) [Choix court]
C) [Choix court]
D) [Choix court]

Question 2 — [Titre] ............. (1 pt) [QCM]
[Énoncé]
A) [Choix court]
B) [Choix court]
C) [Choix court]
D) [Choix court]

[... continuer jusqu'à 10 questions QCM ...]

Partie II — Questions Ouvertes (10 pts)

Question 11 — [Titre] ............. (X pts) [OUVERT]
[Énoncé complet et détaillé]

Question 12 — [Titre] ............. (Y pts) [OUVERT]
[Énoncé complet et détaillé]

[Continuer. Total des deux parties = 20 pts]"""
        format_rules = """- OBLIGATOIRE : chaque titre QCM se termine par [QCM], chaque titre ouvert par [OUVERT]
- Partie I : 10 questions QCM, choix A) B) C) D) courts (max 15 mots)
- Partie II : questions ouvertes, numérotation continue
- Total = 20 pts"""

    else:
        # Ouvert, dissertation, étude de cas, problème, etc.
        questions_format = """Question 1 — [Titre court] ............. (X pts) [OUVERT]
[Énoncé complet, précis et détaillé]

Question 2 — [Titre court] ............. (Y pts) [OUVERT]
[Énoncé complet, précis et détaillé]

[Continuer selon durée et difficulté. Total = 20 pts]"""
        format_rules = """- OBLIGATOIRE : chaque titre se termine par [OUVERT]
- Énoncés complets et détaillés
- Points somment à 20"""

    prompt = f"""Tu es un expert en création d'examens universitaires francophones, compétent dans TOUS les domaines académiques (sciences, droit, médecine, lettres, arts, ingénierie, langues, économie, histoire, philosophie, agronomie, architecture, etc.).

Crée un sujet d'examen COMPLET et DÉTAILLÉ avec ces informations :
- Titre : {title}
- Type : {exam_type}
- Niveau : {student_level}
- Difficulté : {difficulty}
- Durée : {duration} minutes
- Description : {description}
{domain_line}
- Thèmes à couvrir :
{key_points_str}
{examples_section}

GÉNÈRE le sujet en respectant EXACTEMENT ce format (NE DÉVIE JAMAIS de ce format) :

══════════════════════════════════════
{title.upper()}
══════════════════════════════════════
Type d'examen : {exam_type}
Niveau : {student_level} | Difficulté : {difficulty}
Durée : {duration} minutes | Note totale : 20 points
══════════════════════════════════════

INSTRUCTIONS AUX ÉTUDIANTS
──────────────────────────
[2-3 phrases d'instructions claires et précises adaptées au type d'examen]

══════════════════════════════════════
QUESTIONS
══════════════════════════════════════

{questions_format}

══════════════════════════════════════
BARÈME DE NOTATION
══════════════════════════════════════

Question 1 — [Titre] (X pts)
  • Critère : Z pts — [Ce qui est attendu]

[Un critère par question]

──────────────────────────
TOTAL : 20 / 20 points
══════════════════════════════════════

Règles ABSOLUES à respecter :
{format_rules}
- Langage académique et rigoureux en français
- Questions adaptées au niveau {student_level} et à {duration} minutes de composition"""

    try:
        full_exam_text = call_ai_simple(prompt)

        # Séparer contenu et barème
        bareme_markers = ['BARÈME DE NOTATION', 'BAREME DE NOTATION', 'BARÈME', 'Barème']
        rubric_start = -1
        for marker in bareme_markers:
            idx = full_exam_text.find(marker)
            if idx != -1:
                # Remonter jusqu'à la ligne de séparation
                line_start = full_exam_text.rfind('\n', 0, idx)
                rubric_start = line_start if line_start != -1 else idx
                break

        if rubric_start != -1:
            content = full_exam_text[:rubric_start].strip()
            rubric = full_exam_text[rubric_start:].strip()
        else:
            content = full_exam_text
            rubric = full_exam_text

        return jsonify({
            'success': True,
            'title': title,
            'content': content,
            'rubric': rubric,
            'full_text': full_exam_text
        })

    except Exception as e:
        import traceback
        traceback.print_exc()
        err_str = str(e)
        if 'credit balance' in err_str or 'too low' in err_str:
            user_msg = "Le service d'intelligence artificielle est temporairement indisponible. Veuillez contacter l'administrateur."
        elif 'rate_limit' in err_str or 'rate limit' in err_str.lower():
            user_msg = "Trop de requêtes simultanées. Veuillez patienter quelques secondes et réessayer."
        else:
            user_msg = "Une erreur est survenue lors de la génération. Veuillez réessayer."
        return jsonify({'error': user_msg}), 500


@app.route('/api/subjects/create-from-suggestion', methods=['POST'])
@paseto_required
def create_subject_from_suggestion():
    """Crée un sujet à partir d'une suggestion IA"""
    current_user_id = get_current_user_id()
    session = get_session()  # ✅ Créer session
    
    user = session.query(User).filter_by(id=int(current_user_id)).first()  # ✅ CORRECT
    
    if not user or user.role not in [UserRole.PROFESSOR, UserRole.ADMIN]:
        session.close()
        return jsonify({'success': False, 'error': 'Accès non autorisé'}), 403
    
    data = request.get_json()

    try:
        # Utiliser le barème fourni si disponible, sinon le générer avec Claude
        rubric = data.get('rubric_override')
        if not rubric:
            rubric_prompt = f"""
Voici un sujet d'examen:

{data['content']}

Génère un barème de notation détaillé pour ce sujet.
Répartis les points sur 20 en fonction des différentes parties/questions.
Sois précis sur ce qui est attendu pour chaque point.
"""
            rubric = call_ai_simple(rubric_prompt)

        # Créer le sujet
        new_subject = Subject(
            title=data['title'],
            content=data['content'],
            rubric=rubric,
            creator_id=int(current_user_id),
            ec_id=data.get('ec_id'),
            is_active=True
        )
        
        session.add(new_subject) 
        session.commit()
        
        subject_dict = new_subject.to_dict()
        session.close()
        
        return jsonify({
            'success': True,
            'subject': subject_dict
        })
        
    except Exception as e:
        session.rollback()
        session.close()
        print(f"❌ Erreur création sujet: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/admin/security_report', methods=['GET'])
@paseto_required
def admin_security_report():
    """Rapport de sécurité — incidents d'examens en ligne (admin/prof)."""
    user_id = get_current_user_id()
    session = get_session()
    try:
        user = session.query(User).filter_by(id=user_id).first()
        if not user or user.role not in [UserRole.ADMIN, UserRole.PROFESSOR]:
            session.close()
            return jsonify({'error': 'Accès non autorisé'}), 403

        from sqlalchemy import func as sqlfunc

        # Restriction prof : seulement ses propres examens
        prof_exam_ids = None
        if user.role == UserRole.PROFESSOR:
            prof_exam_ids = [
                e.id for e in session.query(OnlineExam).filter_by(created_by_id=user_id).all()
            ]
            if not prof_exam_ids:
                session.close()
                return jsonify({'event_summary': [], 'high_risk': [], 'banned_count': 0})

        # Top événements (filtrés si prof)
        log_query = session.query(
            ExamActivityLog.event_type,
            sqlfunc.count(ExamActivityLog.id).label('cnt')
        )
        if prof_exam_ids is not None:
            prof_attempt_ids = [
                a.id for a in session.query(ExamAttempt).filter(
                    ExamAttempt.exam_id.in_(prof_exam_ids)
                ).all()
            ]
            log_query = log_query.filter(ExamActivityLog.attempt_id.in_(prof_attempt_ids or [0]))
        event_counts = log_query.group_by(ExamActivityLog.event_type).order_by(
            sqlfunc.count(ExamActivityLog.id).desc()
        ).all()

        # Tentatives à haut risque (filtrées si prof)
        risky_q = session.query(ExamAttempt).options(
            joinedload(ExamAttempt.student),
            joinedload(ExamAttempt.exam)
        ).filter(ExamAttempt.risk_score >= 70)
        if prof_exam_ids is not None:
            risky_q = risky_q.filter(ExamAttempt.exam_id.in_(prof_exam_ids))
        risky = risky_q.order_by(ExamAttempt.risk_score.desc()).limit(20).all()

        risky_list = [{
            'attempt_id':     a.id,
            'student_name':   a.student.full_name if a.student else '—',
            'exam_title':     a.exam.title if a.exam else '—',
            'risk_score':     a.risk_score,
            'warnings_count': a.warnings_count,
            'tab_switches':   a.tab_switches,
            'no_face_count':  a.no_face_count or 0,
            'status':         a.status.value,
            'banned_at':      a.banned_at.isoformat() if a.banned_at else None,
            'ban_reason':     a.ban_reason
        } for a in risky]

        # Tentatives bannies (filtrées si prof)
        banned_q = session.query(ExamAttempt).filter(ExamAttempt.status == AttemptStatus.BANNED)
        if prof_exam_ids is not None:
            banned_q = banned_q.filter(ExamAttempt.exam_id.in_(prof_exam_ids))
        banned_count = banned_q.count()

        session.close()
        return jsonify({
            'event_summary':  [{'event': e, 'count': c} for e, c in event_counts],
            'high_risk':      risky_list,
            'banned_count':   banned_count,
            'total_attempts': session.query(ExamAttempt).count() if False else None
        })
    except Exception as e:
        try: session.rollback(); session.close()
        except: pass
        return jsonify({'error': str(e)}), 500


@app.route('/api/notifications', methods=['GET'])
@paseto_required
def get_notifications():
    """Return unseen corrected exams and papers for the current student."""
    user_id = get_current_user_id()
    session = get_session()
    try:
        user = session.query(User).get(user_id)
        if not user or user.role != UserRole.STUDENT:
            return jsonify({'notifications': [], 'count': 0})

        notifications = []

        # Corrected online exam attempts
        attempts = session.query(ExamAttempt).filter(
            ExamAttempt.student_id == user_id,
            ExamAttempt.corrected_at != None,
            ExamAttempt.score != None
        ).order_by(ExamAttempt.corrected_at.desc()).limit(20).all()

        for att in attempts:
            exam = att.exam
            notifications.append({
                'id': f'attempt_{att.id}',
                'type': 'online_exam',
                'title': exam.title if exam else 'Examen en ligne',
                'message': f'Votre copie a été corrigée — note : {att.score:.2f}/20' if att.score is not None else 'Votre copie a été corrigée',
                'corrected_at': att.corrected_at.isoformat() if att.corrected_at else None,
                'attempt_id': att.id
            })

        # Corrected written papers
        papers = session.query(StudentPaper).filter(
            StudentPaper.student_id == user_id,
            StudentPaper.corrected_at != None
        ).order_by(StudentPaper.corrected_at.desc()).limit(20).all()

        for p in papers:
            subject = p.subject
            notifications.append({
                'id': f'paper_{p.id}',
                'type': 'paper',
                'title': subject.title if subject else 'Copie',
                'message': f'Votre copie a été corrigée — note : {p.score:.2f}/20' if p.score is not None else 'Votre copie a été corrigée',
                'corrected_at': p.corrected_at.isoformat() if p.corrected_at else None,
                'paper_id': p.id
            })

        # Seuil "lu" stocké en base
        last_read = user.notifications_last_read
        if last_read and last_read.tzinfo is None:
            last_read = last_read.replace(tzinfo=timezone.utc)

        def _is_read(iso_str):
            if not last_read or not iso_str:
                return False
            try:
                dt = datetime.fromisoformat(iso_str)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt <= last_read
            except Exception:
                return False

        for n in notifications:
            n['is_read'] = _is_read(n.get('corrected_at'))

        notifications.sort(key=lambda x: x['corrected_at'] or '', reverse=True)
        unread_count = sum(1 for n in notifications if not n['is_read'])

        return jsonify({'notifications': notifications, 'count': len(notifications), 'unread_count': unread_count})

    except Exception as e:
        session.rollback()
        return jsonify({'notifications': [], 'count': 0, 'unread_count': 0, 'error': str(e)}), 500
    finally:
        session.close()


@app.route('/api/notifications/mark-read', methods=['PUT'])
@paseto_required
def mark_notifications_read():
    """Marquer toutes les notifications comme lues (persiste en base)."""
    user_id = get_current_user_id()
    session = get_session()
    try:
        user = session.query(User).get(user_id)
        if not user:
            return jsonify({'error': 'Utilisateur introuvable'}), 404
        user.notifications_last_read = utcnow()
        session.commit()
        return jsonify({'success': True})
    except Exception as e:
        session.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        session.close()


# ============================================================================
# TEMPS SUPPLÉMENTAIRE INDIVIDUEL
# ============================================================================

@app.route('/api/exam_attempts/<int:attempt_id>/extra-time', methods=['PUT'])
@paseto_required
def grant_extra_time(attempt_id):
    """Accorded des minutes supplémentaires à un étudiant pendant l'examen."""
    try:
        user_id = get_current_user_id()
        session = get_session()
        role = get_current_user_role()
        if role not in ['professor', 'admin', 'surveillant']:
            session.close()
            return jsonify({'error': 'Accès non autorisé'}), 403
        attempt = session.query(ExamAttempt).filter_by(id=attempt_id).first()
        if not attempt:
            session.close()
            return jsonify({'error': 'Tentative non trouvée'}), 404
        # Refuser si l'étudiant a déjà terminé
        if attempt.status != AttemptStatus.IN_PROGRESS:
            session.close()
            return jsonify({'error': 'L\'étudiant a déjà terminé ou été exclu — impossible d\'accorder du temps'}), 400
        # Refuser si l'examen est clôturé
        exam = session.query(OnlineExam).filter_by(id=attempt.exam_id).first()
        if exam and exam.status != ExamStatus.ACTIVE:
            session.close()
            return jsonify({'error': 'L\'examen est clôturé — impossible d\'accorder du temps'}), 400
        data = request.get_json(silent=True) or {}
        minutes = int(data.get('minutes', 0))
        if not (1 <= minutes <= 60):
            session.close()
            return jsonify({'error': 'Valeur entre 1 et 60 minutes'}), 400
        prev = attempt.extra_minutes or 0
        attempt.extra_minutes = prev + minutes
        session.commit()
        total = attempt.extra_minutes  # rechargé automatiquement (session ouverte)
        session.close()
        print(f"⏱ Temps +{minutes}min accordé (tentative {attempt_id}), total extra: {total}min")
        return jsonify({'success': True, 'total_extra': total, 'added': minutes})
    except Exception as e:
        print(f"❌ grant_extra_time {attempt_id}: {e}")
        try: session.close()
        except: pass
        return jsonify({'error': str(e)}), 500


# ============================================================================
# NOTES DE SURVEILLANT SUR UN ÉTUDIANT
# ============================================================================

@app.route('/api/exam_attempts/<int:attempt_id>/proctor-note', methods=['POST'])
@paseto_required
def add_proctor_note(attempt_id):
    """Ajoute une note textuelle du surveillant/prof sur une tentative (stockée en activity_log)."""
    try:
        user_id = get_current_user_id()
        session = get_session()
        role = get_current_user_role()
        if role not in ['professor', 'admin', 'surveillant']:
            session.close()
            return jsonify({'error': 'Accès non autorisé'}), 403
        attempt = session.query(ExamAttempt).filter_by(id=attempt_id).first()
        if not attempt:
            session.close()
            return jsonify({'error': 'Tentative non trouvée'}), 404
        data = request.get_json(silent=True) or {}
        note = (data.get('note') or '').strip()
        if not note:
            session.close()
            return jsonify({'error': 'Note vide'}), 400
        author = session.query(User).filter_by(id=user_id).first()
        author_name = author.full_name if author else f'User#{user_id}'
        log = ExamActivityLog(
            attempt_id  = attempt_id,
            event_type  = 'proctor_note',
            event_data  = json.dumps({'note': note, 'author': author_name, 'author_id': user_id}, ensure_ascii=False),
            timestamp   = utcnow(),
        )
        session.add(log)
        session.commit()
        session.close()
        return jsonify({'success': True, 'note': note, 'author': author_name})
    except Exception as e:
        print(f"❌ add_proctor_note {attempt_id}: {e}")
        try: session.close()
        except: pass
        return jsonify({'error': str(e)}), 500


@app.route('/api/exam_attempts/<int:attempt_id>/proctor-notes', methods=['GET'])
@paseto_required
def get_proctor_notes(attempt_id):
    """Liste toutes les notes de surveillance d'une tentative."""
    try:
        user_id = get_current_user_id()
        session = get_session()
        role = get_current_user_role()
        if role not in ['professor', 'admin', 'surveillant']:
            session.close()
            return jsonify({'error': 'Accès non autorisé'}), 403
        logs = session.query(ExamActivityLog).filter_by(
            attempt_id=attempt_id, event_type='proctor_note'
        ).order_by(ExamActivityLog.timestamp).all()
        notes = []
        for l in logs:
            try:   d = json.loads(l.event_data or '{}')
            except: d = {}
            notes.append({
                'id':        l.id,
                'note':      d.get('note', ''),
                'author':    d.get('author', '?'),
                'author_id': d.get('author_id'),
                'timestamp': l.timestamp.isoformat() if l.timestamp else None,
            })
        session.close()
        return jsonify({'notes': notes, 'total': len(notes)})
    except Exception as e:
        print(f"❌ get_proctor_notes {attempt_id}: {e}")
        try: session.close()
        except: pass
        return jsonify({'error': str(e)}), 500


# ============================================================================
# QR CODE D'ACCÈS À L'EXAMEN
# ============================================================================

@app.route('/api/online_exams/<int:exam_id>/qrcode', methods=['GET'])
@paseto_required
def get_exam_qrcode(exam_id):
    """Génère et retourne un QR code (PNG base64) pointant vers la page de l'examen."""
    try:
        import qrcode as _qrcode, base64 as _b64
        user_id = get_current_user_id()
        session = get_session()
        role = get_current_user_role()
        if role not in ['professor', 'admin']:
            session.close()
            return jsonify({'error': 'Accès non autorisé'}), 403
        exam = session.query(OnlineExam).filter_by(id=exam_id).first()
        if not exam:
            session.close()
            return jsonify({'error': 'Examen non trouvé'}), 404
        # Récupérer les données AVANT de fermer la session
        exam_title = exam.title
        base_url   = request.host_url.rstrip('/')
        exam_url   = f"{base_url}/app"
        session.close()
        qr = _qrcode.QRCode(version=1, box_size=8, border=3,
                             error_correction=_qrcode.constants.ERROR_CORRECT_M)
        qr.add_data(exam_url)
        qr.make(fit=True)
        img = qr.make_image(fill_color='black', back_color='white')
        buf = io.BytesIO()
        img.save(buf, format='PNG')
        buf.seek(0)
        b64 = _b64.b64encode(buf.read()).decode()
        return jsonify({
            'exam_id':    exam_id,
            'exam_title': exam_title,
            'url':        exam_url,
            'qrcode_b64': f"data:image/png;base64,{b64}",
        })
    except Exception as e:
        import traceback
        print(f"❌ get_exam_qrcode {exam_id}: {e}\n{traceback.format_exc()}")
        try: session.close()
        except: pass
        return jsonify({'error': str(e)}), 500


# ============================================================================
# EMAIL RÉCAPITULATIF POST-CLÔTURE (déclenché par close_online_exam)
# ============================================================================

def _send_exam_closure_summary(exam_id: int, professor_email: str, professor_name: str):
    """Envoie un email récapitulatif au professeur après clôture de l'examen (thread)."""
    try:
        session = get_session()
        exam = session.query(OnlineExam).filter_by(id=exam_id).first()
        if not exam:
            session.close()
            return
        attempts  = session.query(ExamAttempt).filter_by(exam_id=exam_id).all()
        total     = len(attempts)
        submitted = sum(1 for a in attempts if a.status.value in ('submitted', 'auto_submitted'))
        banned    = sum(1 for a in attempts if a.status.value == 'banned')
        scores    = [a.score for a in attempts if a.score is not None]
        avg       = round(sum(scores)/len(scores), 2) if scores else None
        high_risk = sum(1 for a in attempts if (a.risk_score or 0) >= 70)
        exam_title = exam.title
        session.close()

        from utils import send_email as _send_email
        subject_line = f"[CEI] Clôture : {exam_title}"
        html_body = f"""<div style="font-family:sans-serif;max-width:520px;margin:auto;border:1px solid #e2e8f0;border-radius:10px;overflow:hidden;">
  <div style="background:#1e293b;padding:20px 24px;">
    <h2 style="color:white;margin:0;font-size:16px;">CEI — Clôture d'examen</h2>
  </div>
  <div style="padding:24px;">
    <p>Bonjour <strong>{professor_name}</strong>,</p>
    <p>L'examen <strong>« {exam_title} »</strong> vient d'être clôturé.</p>
    <table style="width:100%;border-collapse:collapse;margin:16px 0;">
      <tr style="background:#f8fafc;"><td style="padding:8px 12px;color:#64748b;font-size:13px;">Inscrits</td><td style="padding:8px 12px;font-weight:700;">{total}</td></tr>
      <tr><td style="padding:8px 12px;color:#64748b;font-size:13px;">Soumis</td><td style="padding:8px 12px;font-weight:700;color:#6366f1;">{submitted}</td></tr>
      <tr style="background:#f8fafc;"><td style="padding:8px 12px;color:#64748b;font-size:13px;">Exclus</td><td style="padding:8px 12px;font-weight:700;color:#ef4444;">{banned}</td></tr>
      <tr><td style="padding:8px 12px;color:#64748b;font-size:13px;">Note moyenne</td><td style="padding:8px 12px;font-weight:700;color:{'#10b981' if avg and avg>=10 else '#ef4444'};">{f'{avg}/20' if avg is not None else '—'}</td></tr>
      <tr style="background:#f8fafc;"><td style="padding:8px 12px;color:#64748b;font-size:13px;">Haut risque (≥70%)</td><td style="padding:8px 12px;font-weight:700;color:#f59e0b;">{high_risk}</td></tr>
    </table>
    <p style="font-size:13px;color:#64748b;">Connectez-vous à la plateforme CEI pour corriger les copies et consulter les rapports d'intégrité.</p>
  </div>
</div>"""
        _send_email(professor_email, subject_line, html_body)
        print(f"📧 Email clôture envoyé à {professor_email} pour exam#{exam_id}")
    except Exception as e:
        print(f"⚠️  Email clôture exam#{exam_id}: {e}")


# ============================================================================
# ZIP COPIES CORRIGÉES PAR EXAMEN
# ============================================================================

@app.route('/api/online_exams/<int:exam_id>/corrections/zip', methods=['GET'])
@paseto_required
def download_corrections_zip(exam_id):
    """ZIP de toutes les copies corrigées d'un examen (une copie texte par étudiant)."""
    try:
        import zipfile as zipfile_mod
        user_id = get_current_user_id()
        session = get_session()
        user = session.query(User).filter_by(id=user_id).first()
        if user.role not in [UserRole.PROFESSOR, UserRole.ADMIN]:
            session.close()
            return jsonify({'error': 'Accès non autorisé'}), 403
        exam = session.query(OnlineExam).filter_by(id=exam_id).first()
        if not exam:
            session.close()
            return jsonify({'error': 'Examen non trouvé'}), 404
        attempts = session.query(ExamAttempt).options(
            joinedload(ExamAttempt.student)
        ).filter(
            ExamAttempt.exam_id == exam_id,
            ExamAttempt.score.isnot(None)
        ).all()
        if not attempts:
            session.close()
            return jsonify({'error': 'Aucune copie corrigée pour cet examen'}), 404

        zip_buf = io.BytesIO()
        with zipfile_mod.ZipFile(zip_buf, 'w', zipfile_mod.ZIP_DEFLATED) as zf:
            for attempt in attempts:
                try:
                    answers_data = json.loads(attempt.answers) if attempt.answers else {}
                    student_text = (
                        answers_data.get('content') or answers_data.get('reponse') or
                        answers_data.get('answer')  or answers_data.get('text') or 'Non disponible'
                    )
                except Exception:
                    student_text = attempt.answers or 'Non disponible'

                duration_str = '—'
                if attempt.submitted_at and attempt.started_at:
                    mins = int((attempt.submitted_at - attempt.started_at).total_seconds() / 60)
                    duration_str = f"{mins} min"

                content = (
                    f"COPIE CORRIGÉE — {exam.title}\n"
                    f"{'='*60}\n"
                    f"Étudiant   : {attempt.student.full_name if attempt.student else '—'}\n"
                    f"Note       : {attempt.score}/20\n"
                    f"Risque     : {attempt.risk_score or 0}%\n"
                    f"Durée      : {duration_str}\n"
                    f"Corrigé le : {attempt.corrected_at.strftime('%d/%m/%Y %H:%M') if attempt.corrected_at else '—'}\n"
                    f"Infractions: {attempt.tab_switches or 0} changement(s) de fenêtre, "
                    f"{attempt.warnings_count or 0} avertissement(s), "
                    f"{attempt.no_face_count or 0} absence(s) de visage\n"
                    f"\n{'='*60}\n"
                    f"RÉPONSES DE L'ÉTUDIANT\n"
                    f"{'='*60}\n"
                    f"{student_text}\n"
                    f"\n{'='*60}\n"
                    f"CORRECTION IA\n"
                    f"{'='*60}\n"
                    f"{attempt.feedback or 'Pas de feedback disponible'}\n"
                )
                safe_name = re.sub(r'[^\w\s-]', '', attempt.student.full_name if attempt.student else 'etudiant')
                safe_name = safe_name.strip().replace(' ', '_')
                filename = f"{safe_name}_{attempt.score:.1f}_sur_20.txt"
                zf.writestr(filename, content.encode('utf-8'))

        exam_title = exam.title
        session.close()
        zip_buf.seek(0)
        safe_title = re.sub(r'[^\w\s-]', '', exam_title).strip().replace(' ', '_')
        return send_file(
            zip_buf,
            mimetype='application/zip',
            as_attachment=True,
            download_name=f"corrections_{safe_title}.zip"
        )
    except Exception as e:
        print(f"❌ download_corrections_zip {exam_id}: {e}")
        try: session.close()
        except: pass
        return jsonify({'error': str(e)}), 500


# ============================================================================
# RÉVISION DÉTAILLÉE D'UNE TENTATIVE
# ============================================================================

@app.route('/api/exam_attempts/<int:attempt_id>/review', methods=['GET'])
@paseto_required
def get_attempt_review(attempt_id):
    """Vue complète d'une tentative: réponses, correction, incidents, notes surveillant."""
    try:
        user_id = get_current_user_id()
        session = get_session()
        user = session.query(User).filter_by(id=user_id).first()

        attempt = session.query(ExamAttempt).options(
            joinedload(ExamAttempt.exam).joinedload(OnlineExam.subject),
            joinedload(ExamAttempt.student),
            joinedload(ExamAttempt.activity_logs)
        ).filter_by(id=attempt_id).first()

        if not attempt:
            session.close()
            return jsonify({'error': 'Tentative non trouvée'}), 404

        if user.role == UserRole.PROFESSOR and attempt.exam.created_by_id != user_id:
            session.close()
            return jsonify({'error': 'Accès non autorisé'}), 403

        try:
            answers_data = json.loads(attempt.answers) if attempt.answers else {}
        except Exception:
            answers_data = {}

        # Format examen en ligne : { qcm: {}, texte: {} }
        qcm_a  = answers_data.get('qcm',  {}) if isinstance(answers_data, dict) else {}
        text_a = answers_data.get('texte', answers_data.get('text', {})) if isinstance(answers_data, dict) else {}
        if qcm_a or text_a:
            lines = []
            all_keys = sorted(set(list(qcm_a.keys()) + list(text_a.keys())),
                               key=lambda x: int(x) if str(x).isdigit() else 0)
            for k in all_keys:
                if k in qcm_a:  lines.append(f"Question {k} : {qcm_a[k]}")
                if k in text_a: lines.append(f"Question {k} : {text_a[k]}")
            student_text = '\n'.join(lines)
        else:
            student_text = (
                answers_data.get('content') or answers_data.get('reponse') or
                answers_data.get('answer')  or answers_data.get('text') or
                attempt.answers or ''
            ) if isinstance(answers_data, dict) else (attempt.answers or '')

        incidents = []
        proctor_notes = []
        for log in sorted(attempt.activity_logs, key=lambda x: x.timestamp or datetime.min):
            try:
                ed = json.loads(log.event_data) if log.event_data else {}
            except Exception:
                ed = {}
            if log.event_type == 'proctor_note':
                proctor_notes.append({
                    'note':      ed.get('note', ''),
                    'author':    ed.get('author', ''),
                    'timestamp': log.timestamp.isoformat() if log.timestamp else None,
                })
            else:
                incidents.append({
                    'type':      log.event_type,
                    'data':      ed,
                    'timestamp': log.timestamp.isoformat() if log.timestamp else None,
                })

        duration_min = None
        if attempt.submitted_at and attempt.started_at:
            duration_min = round((attempt.submitted_at - attempt.started_at).total_seconds() / 60, 1)

        session.close()
        return jsonify({
            'attempt_id':    attempt.id,
            'student_name':  attempt.student.full_name if attempt.student else '—',
            'student_email': attempt.student.email if attempt.student else '—',
            'exam_title':    attempt.exam.title if attempt.exam else '—',
            'subject_title': attempt.exam.subject.title if attempt.exam and attempt.exam.subject else '—',
            'status':        attempt.status.value,
            'score':         attempt.score,
            'started_at':    attempt.started_at.isoformat()  if attempt.started_at  else None,
            'submitted_at':  attempt.submitted_at.isoformat() if attempt.submitted_at else None,
            'duration_min':  duration_min,
            'risk_score':    attempt.risk_score or 0,
            'tab_switches':  attempt.tab_switches or 0,
            'warnings_count':attempt.warnings_count or 0,
            'no_face_count': attempt.no_face_count or 0,
            'extra_minutes': attempt.extra_minutes or 0,
            'ban_reason':    attempt.ban_reason,
            'student_answer': student_text,
            'raw_answers':   attempt.answers or '',
            'feedback':      attempt.feedback,
            'incidents':     incidents,
            'proctor_notes': proctor_notes,
            'corrected_at':  attempt.corrected_at.isoformat() if attempt.corrected_at else None,
        })
    except Exception as e:
        print(f"❌ get_attempt_review {attempt_id}: {e}")
        try: session.close()
        except: pass
        return jsonify({'error': str(e)}), 500


# ============================================================================
# DASHBOARD ANALYTIQUE GLOBAL PROFESSEUR
# ============================================================================

@app.route('/api/professor/analytics', methods=['GET'])
@paseto_required
def get_professor_analytics():
    """Statistiques globales : tous les examens, moyennes, taux réussite, activité récente."""
    try:
        user_id = get_current_user_id()
        session = get_session()
        user = session.query(User).filter_by(id=user_id).first()
        if user.role not in [UserRole.PROFESSOR, UserRole.ADMIN]:
            session.close()
            return jsonify({'error': 'Accès non autorisé'}), 403

        if user.role == UserRole.ADMIN:
            exams = session.query(OnlineExam).all()
        else:
            exams = session.query(OnlineExam).filter_by(created_by_id=user_id).all()

        exam_ids = [e.id for e in exams]

        all_attempts = session.query(ExamAttempt).filter(
            ExamAttempt.exam_id.in_(exam_ids)
        ).options(joinedload(ExamAttempt.student)).all() if exam_ids else []

        submitted = [a for a in all_attempts if a.status.value in ('submitted', 'auto_submitted')]
        all_scores = [a.score for a in submitted if a.score is not None]

        exam_stats = []
        for exam in exams:
            e_attempts  = [a for a in all_attempts if a.exam_id == exam.id]
            e_submitted = [a for a in e_attempts if a.status.value in ('submitted', 'auto_submitted')]
            e_scores    = [a.score for a in e_submitted if a.score is not None]
            exam_stats.append({
                'id':        exam.id,
                'title':     exam.title,
                'status':    exam.status.value,
                'total':     len(e_attempts),
                'submitted': len(e_submitted),
                'corrected': len(e_scores),
                'avg_score': round(sum(e_scores)/len(e_scores), 2) if e_scores else None,
                'pass_rate': round(sum(1 for s in e_scores if s >= 10)/len(e_scores)*100, 1) if e_scores else None,
            })

        ranked = [e for e in exam_stats if e['avg_score'] is not None and e['corrected'] >= 2]
        ranked.sort(key=lambda x: x['avg_score'], reverse=True)

        recent = session.query(ExamAttempt).filter(
            ExamAttempt.exam_id.in_(exam_ids),
            ExamAttempt.corrected_at.isnot(None)
        ).order_by(ExamAttempt.corrected_at.desc()).limit(10).all() if exam_ids else []

        recent_list = [{
            'student_name': a.student.full_name if a.student else '—',
            'exam_title':   a.exam.title if a.exam else '—',
            'score':        a.score,
            'corrected_at': a.corrected_at.isoformat() if a.corrected_at else None,
        } for a in recent]

        status_counts = {}
        for exam in exams:
            s = exam.status.value
            status_counts[s] = status_counts.get(s, 0) + 1

        session.close()
        return jsonify({
            'total_exams':      len(exams),
            'status_counts':    status_counts,
            'total_attempts':   len(all_attempts),
            'total_submitted':  len(submitted),
            'total_corrected':  len(all_scores),
            'overall_avg':      round(sum(all_scores)/len(all_scores), 2) if all_scores else None,
            'overall_pass_rate':round(sum(1 for s in all_scores if s >= 10)/len(all_scores)*100, 1) if all_scores else None,
            'top_exams':        ranked[:3],
            'bottom_exams':     ranked[-3:][::-1] if len(ranked) >= 3 else [],
            'recent_corrections': recent_list,
            'exam_stats':       exam_stats,
        })
    except Exception as e:
        print(f"❌ get_professor_analytics: {e}")
        try: session.close()
        except: pass
        return jsonify({'error': str(e)}), 500


# ============================================================================
# PDF RAPPORT INDIVIDUEL PAR TENTATIVE
# ============================================================================

@app.route('/api/exam_attempts/<int:attempt_id>/report/pdf', methods=['GET'])
@paseto_required
def download_attempt_report_pdf(attempt_id):
    """PDF rapport individuel complet d'une tentative d'examen."""
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable
        from reportlab.lib import colors as rl_colors
        from reportlab.lib.units import cm

        user_id = get_current_user_id()
        session = get_session()
        user = session.query(User).filter_by(id=user_id).first()
        if user.role not in [UserRole.PROFESSOR, UserRole.ADMIN]:
            session.close()
            return jsonify({'error': 'Accès non autorisé'}), 403

        attempt = session.query(ExamAttempt).options(
            joinedload(ExamAttempt.exam).joinedload(OnlineExam.subject),
            joinedload(ExamAttempt.student),
            joinedload(ExamAttempt.activity_logs)
        ).filter_by(id=attempt_id).first()

        if not attempt:
            session.close()
            return jsonify({'error': 'Tentative non trouvée'}), 404

        if user.role == UserRole.PROFESSOR and attempt.exam.created_by_id != user_id:
            session.close()
            return jsonify({'error': 'Accès non autorisé'}), 403

        try:
            answers_data = json.loads(attempt.answers) if attempt.answers else {}
            student_text = (
                answers_data.get('content') or answers_data.get('reponse') or
                answers_data.get('answer')  or answers_data.get('text') or 'Non disponible'
            )
        except Exception:
            student_text = attempt.answers or 'Non disponible'

        duration_str = '—'
        if attempt.submitted_at and attempt.started_at:
            mins = int((attempt.submitted_at - attempt.started_at).total_seconds() / 60)
            duration_str = f"{mins} min"

        incident_count = sum(1 for log in attempt.activity_logs if log.event_type != 'proctor_note')
        note_count     = sum(1 for log in attempt.activity_logs if log.event_type == 'proctor_note')

        buffer = io.BytesIO()
        doc    = SimpleDocTemplate(buffer, pagesize=A4,
                                   leftMargin=1.5*cm, rightMargin=1.5*cm,
                                   topMargin=1.5*cm, bottomMargin=1.5*cm)
        styles = getSampleStyleSheet()
        story  = []

        # En-tête
        hdr_data = [['CEI — Rapport de Copie',
                      f"Généré le {datetime.utcnow().strftime('%d/%m/%Y %H:%M')} UTC"]]
        hdr_tbl = Table(hdr_data, colWidths=[13*cm, 5*cm])
        hdr_tbl.setStyle(TableStyle([
            ('BACKGROUND', (0,0), (-1,-1), rl_colors.HexColor('#1e293b')),
            ('TEXTCOLOR',  (0,0), (-1,-1), rl_colors.white),
            ('FONTNAME',   (0,0), (0,0),  'Helvetica-Bold'),
            ('FONTSIZE',   (0,0), (0,0),  13),
            ('FONTSIZE',   (1,0), (1,0),   9),
            ('ALIGN',      (1,0), (1,0),  'RIGHT'),
            ('VALIGN',     (0,0), (-1,-1),'MIDDLE'),
            ('PADDING',    (0,0), (-1,-1), 10),
        ]))
        story.append(hdr_tbl)
        story.append(Spacer(1, 0.4*cm))

        score_color = rl_colors.HexColor('#10b981') if (attempt.score or 0) >= 10 else rl_colors.HexColor('#ef4444')
        risk_val    = attempt.risk_score or 0
        risk_color  = (rl_colors.HexColor('#ef4444') if risk_val >= 70 else
                       rl_colors.HexColor('#f59e0b') if risk_val >= 40 else rl_colors.HexColor('#10b981'))

        info_data = [
            ['Étudiant', attempt.student.full_name if attempt.student else '—',
             'Note',        f"{attempt.score}/20" if attempt.score is not None else '—'],
            ['Examen',   attempt.exam.title if attempt.exam else '—',
             'Risque',      f"{risk_val}%"],
            ['Matière',  attempt.exam.subject.title if attempt.exam and attempt.exam.subject else '—',
             'Durée',       duration_str],
            ['Statut',   attempt.status.value,
             'Extra-temps', f"{attempt.extra_minutes or 0} min"],
        ]
        info_tbl = Table(info_data, colWidths=[3*cm, 9*cm, 2.5*cm, 3.5*cm])
        info_style = TableStyle([
            ('FONTNAME',       (0,0), (-1,-1),  'Helvetica'),
            ('FONTSIZE',       (0,0), (-1,-1),   9),
            ('FONTNAME',       (0,0), (0,-1),   'Helvetica-Bold'),
            ('FONTNAME',       (2,0), (2,-1),   'Helvetica-Bold'),
            ('TEXTCOLOR',      (0,0), (0,-1),    rl_colors.HexColor('#64748b')),
            ('TEXTCOLOR',      (2,0), (2,-1),    rl_colors.HexColor('#64748b')),
            ('ROWBACKGROUNDS', (0,0), (-1,-1),  [rl_colors.HexColor('#f8fafc'), rl_colors.white]),
            ('PADDING',        (0,0), (-1,-1),   7),
            ('BOX',            (0,0), (-1,-1),   0.5, rl_colors.HexColor('#e2e8f0')),
            ('INNERGRID',      (0,0), (-1,-1),   0.3, rl_colors.HexColor('#e2e8f0')),
        ])
        if attempt.score is not None:
            info_style.add('TEXTCOLOR', (3,0), (3,0), score_color)
            info_style.add('FONTNAME',  (3,0), (3,0), 'Helvetica-Bold')
        info_style.add('TEXTCOLOR', (3,1), (3,1), risk_color)
        info_tbl.setStyle(info_style)
        story.append(info_tbl)
        story.append(Spacer(1, 0.4*cm))

        story.append(Paragraph(
            f"<b>Incidents</b> : {attempt.tab_switches or 0} changement(s) de fenêtre · "
            f"{attempt.warnings_count or 0} avertissement(s) · "
            f"{attempt.no_face_count or 0} absence(s) de visage · "
            f"{incident_count} événement(s) total · {note_count} note(s) de surveillance",
            ParagraphStyle('inc', parent=styles['Normal'], fontSize=9)
        ))
        story.append(Spacer(1, 0.4*cm))
        story.append(HRFlowable(width="100%", thickness=0.5, color=rl_colors.HexColor('#e2e8f0')))
        story.append(Spacer(1, 0.3*cm))

        story.append(Paragraph("<b>Réponses de l'étudiant</b>",
            ParagraphStyle('h3', parent=styles['Normal'], fontSize=11,
                           textColor=rl_colors.HexColor('#1e293b'))))
        story.append(Spacer(1, 0.2*cm))
        ans_style = ParagraphStyle('ans', parent=styles['Normal'], fontSize=8.5, leading=13,
                                   textColor=rl_colors.HexColor('#334155'))
        truncated_ans = (student_text[:4000] + '…') if len(student_text) > 4000 else student_text
        for chunk in truncated_ans.split('\n'):
            if chunk.strip():
                story.append(Paragraph(chunk.replace('&','&amp;').replace('<','&lt;').replace('>','&gt;'), ans_style))
            else:
                story.append(Spacer(1, 0.1*cm))

        if attempt.feedback:
            story.append(Spacer(1, 0.4*cm))
            story.append(HRFlowable(width="100%", thickness=0.5, color=rl_colors.HexColor('#e2e8f0')))
            story.append(Spacer(1, 0.3*cm))
            story.append(Paragraph("<b>Correction IA</b>",
                ParagraphStyle('h3fb', parent=styles['Normal'], fontSize=11,
                               textColor=rl_colors.HexColor('#6366f1'))))
            story.append(Spacer(1, 0.2*cm))
            fb_style = ParagraphStyle('fb', parent=styles['Normal'], fontSize=8.5, leading=13,
                                       textColor=rl_colors.HexColor('#334155'))
            truncated_fb = (attempt.feedback[:5000] + '…') if len(attempt.feedback) > 5000 else attempt.feedback
            for chunk in truncated_fb.split('\n'):
                if chunk.strip():
                    story.append(Paragraph(chunk.replace('&','&amp;').replace('<','&lt;').replace('>','&gt;'), fb_style))
                else:
                    story.append(Spacer(1, 0.1*cm))

        doc.build(story)
        student_name = attempt.student.full_name if attempt.student else 'etudiant'
        session.close()

        buffer.seek(0)
        safe_sn = re.sub(r'[^\w\s-]', '', student_name).strip().replace(' ', '_')
        return send_file(
            buffer,
            mimetype='application/pdf',
            as_attachment=True,
            download_name=f"rapport_{safe_sn}_attempt{attempt_id}.pdf"
        )
    except Exception as e:
        print(f"❌ download_attempt_report_pdf {attempt_id}: {e}")
        import traceback; traceback.print_exc()
        try: session.close()
        except: pass
        return jsonify({'error': str(e)}), 500


# ─── Statut de l'agent proctor autonome ────────────────────────────────────

_HEARTBEAT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'agent_heartbeat.json')
_AGENT_STALE_SEC  = 90   # heartbeat trop ancien → "stale"
_AGENT_OFFLINE_SEC = 180  # heartbeat très ancien → "offline"

@app.route('/api/agent/status', methods=['GET'])
@paseto_required
def get_agent_status():
    """
    Retourne le statut de l'agent proctor autonome.
    Paramètre optionnel : ?exam_id=<int>  — pour inclure les stats de cet examen.

    Statuts possibles :
      - active  : heartbeat < 90s   → agent opérationnel
      - stale   : heartbeat 90-180s → agent potentiellement bloqué
      - offline : heartbeat > 180s ou fichier absent → service arrêté
    """
    from datetime import timezone as _tz

    exam_id = request.args.get('exam_id', type=int)

    # Lire le fichier heartbeat écrit par l'agent toutes les 30s
    try:
        with open(_HEARTBEAT_FILE, 'r', encoding='utf-8') as f:
            hb = json.load(f)
    except Exception:
        return jsonify({
            'alive': False, 'status': 'offline',
            'status_label': 'Agent hors ligne — service PM2 non démarré',
            'status_color': '#ef4444',
            'last_check': None, 'last_check_ago_sec': None,
            'interval_seconds': 30, 'risk_alert': 60, 'risk_urgent': 80,
            'exams_monitored': 0, 'total_alerts_session': 0, 'exam': {}
        }), 200

    # Calculer le délai depuis le dernier heartbeat
    last_check_ago = None
    last_check_iso = hb.get('last_check')
    try:
        last_dt = datetime.fromisoformat(last_check_iso)
        if last_dt.tzinfo is None:
            last_dt = last_dt.replace(tzinfo=_tz.utc)
        last_check_ago = int((datetime.now(_tz.utc) - last_dt).total_seconds())
    except Exception:
        pass

    # Déterminer le statut précis
    if last_check_ago is None or last_check_ago > _AGENT_OFFLINE_SEC:
        status       = 'offline'
        alive        = False
        status_label = 'Agent hors ligne — relancez via : pm2 restart cei-agent-proctor'
        status_color = '#ef4444'
    elif last_check_ago > _AGENT_STALE_SEC:
        status       = 'stale'
        alive        = False
        status_label = f'Agent inactif — dernier heartbeat il y a {last_check_ago}s'
        status_color = '#f59e0b'
    else:
        status       = 'active'
        alive        = True
        n = hb.get('exams_monitored', 0)
        status_label = f'Agent actif — Surveillance IA en cours ({n} examen{"s" if n != 1 else ""})'
        status_color = '#10b981'

    # Stats spécifiques à l'examen demandé
    exam_stats = {}
    if exam_id is not None:
        raw = hb.get('exam_stats', {})
        es  = raw.get(str(exam_id), raw.get(exam_id, {}))
        if es:
            exam_stats = {
                'exam_id':     exam_id,
                'students':    es.get('total', 0),
                'alerts_sent': es.get('alerts_sent', 0),
                'banned':      es.get('banned', 0),
            }

    return jsonify({
        'alive':                alive,
        'status':               status,
        'status_label':         status_label,
        'status_color':         status_color,
        'last_check':           last_check_iso,
        'last_check_ago_sec':   last_check_ago,
        'interval_seconds':     hb.get('interval_seconds', 30),
        'risk_alert':           hb.get('risk_alert', 60),
        'risk_urgent':          hb.get('risk_urgent', 80),
        'exams_monitored':      hb.get('exams_monitored', 0),
        'total_alerts_session': hb.get('total_alerts_session', 0),
        'exam':                 exam_stats,
    }), 200


if __name__ == '__main__':
    print("\n" + "="*60)
    print("SYSTÈME DE NOTATION AVANCÉ - VERSION COMPLÈTE")
    print("="*60)
    print("\n📱 URLs:")
    print(" Landing Page: http://localhost:7000")
    print(" Application: http://localhost:7000/app")
    print("\n Fonctionnalités:")
    print(" Authentification multi-rôles")
    print(" CRUD Maquette pédagogique COMPLET")
    print(" CRUD Utilisateurs (Admin)")
    print(" Upload et création de sujets")
    print(" Correction de copies unique et en lot")
    print(" Gestion des réclamations")
    print(" Statistiques détaillées")
    print("\n Créer un admin: python create_admin.py\n")

    debug_mode = os.getenv('FLASK_DEBUG', 'False').lower() in ('true', '1')
    app.run(debug=debug_mode, host='0.0.0.0', port=7000)

# Note: export_paper_pdf route is registered from export_route.py via register_export_route(app)