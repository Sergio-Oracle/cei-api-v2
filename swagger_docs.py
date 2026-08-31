"""
CEI — Documentation API Swagger / OpenAPI 3.0
Accessible à /api/docs (Swagger UI) et /api/docs/openapi.json (spec brute)
Scan exhaustif — couvre tous les blueprints : app.py, proctoring_routes.py,
csv_import_routes.py, export_route.py, routes/{auth,exams,professor,admin_users,
formations,superviseur,subjects,question_bank,papers,reclamations,transcripts,
statistics,notifications}.py
Le nombre d'endpoints documentés (badges Swagger UI / ReDoc) est calculé
automatiquement depuis OPENAPI_SPEC["paths"] — voir _ENDPOINT_COUNT plus bas.
Rien à mettre à jour à la main quand une route est ajoutée/retirée.
"""
import os
import base64
import secrets as _secrets
from functools import wraps
from flask import Blueprint, jsonify, request, Response

swagger_bp = Blueprint('swagger', __name__)

# ─────────────────────────────────────────────────────────────────────────────
# Basic Auth — credentials lus depuis .env (DOCS_USER / DOCS_PASS). Si absents,
# on génère un mot de passe aléatoire au démarrage plutôt que de retomber sur
# un identifiant connu/faible codé en dur : /api/docs reste alors verrouillé
# (fail closed) au lieu d'être protégé par un secret déjà présent en clair
# dans l'historique git.
# ─────────────────────────────────────────────────────────────────────────────

_DOCS_USER = os.getenv('DOCS_USER') or 'admin'
_DOCS_PASS = os.getenv('DOCS_PASS') or _secrets.token_urlsafe(24)

def _require_docs_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.headers.get('Authorization', '')
        if auth.startswith('Basic '):
            try:
                decoded = base64.b64decode(auth[6:]).decode('utf-8')
                user, pwd = decoded.split(':', 1)
                if user == _DOCS_USER and pwd == _DOCS_PASS:
                    return f(*args, **kwargs)
            except Exception:
                pass
        return Response(
            'Accès réservé aux développeurs autorisés.',
            401,
            {'WWW-Authenticate': 'Basic realm="CEI API Docs"'}
        )
    return decorated

# ─────────────────────────────────────────────────────────────────────────────
# Composants réutilisables
# ─────────────────────────────────────────────────────────────────────────────

_SCHEMAS = {
    "Error": {
        "type": "object",
        "properties": {"error": {"type": "string", "example": "Message d'erreur"}}
    },
    "Success": {
        "type": "object",
        "properties": {
            "success": {"type": "boolean"},
            "message": {"type": "string"}
        }
    },
    "User": {
        "type": "object",
        "properties": {
            "id":              {"type": "integer"},
            "email":           {"type": "string", "example": "user@ec2lt.sn"},
            "full_name":       {"type": "string", "example": "Moussa Diallo"},
            "role":            {"type": "string", "enum": ["admin","professor","surveillant","superviseur","student"]},
            "niveau":          {"type": "string", "description": "Étudiant seulement. Code court (ex: 'L3') — dérivé automatiquement de formation.niveau.code quand formation_id est renseigné ; sinon texte libre parmi L1/L2/L3/M1/M2.", "example": "L3"},
            "niveau_name":     {"type": "string", "description": "Nom complet du niveau dérivé de la formation (ex: 'Licence 3'), absent si aucune formation n'est rattachée."},
            "formation_id":    {"type": "integer", "description": "Étudiant seulement. Rattacher une formation inscrit automatiquement l'étudiant à toutes les UE de cette formation."},
            "formation_code":  {"type": "string", "example": "L3-TR-DEV"},
            "formation_name":  {"type": "string"},
            "pole_code":       {"type": "string", "description": "Dérivé de la formation rattachée", "example": "STN"},
            "pole_name":       {"type": "string"},
            "is_active":       {"type": "boolean"},
            "email_verified":  {"type": "boolean"},
            "has_email":       {"type": "boolean"},
            "created_at":      {"type": "string", "format": "date-time"},
            "last_login":      {"type": "string", "format": "date-time"}
        }
    },
    "Subject": {
        "type": "object",
        "properties": {
            "id":           {"type": "integer"},
            "title":        {"type": "string", "example": "Examen de Réseaux L3"},
            "content":      {"type": "string"},
            "rubric":       {"type": "string"},
            "ec_id":        {"type": "integer"},
            "creator_id":   {"type": "integer"},
            "created_at":   {"type": "string", "format": "date-time"},
            "papers_count": {"type": "integer"}
        }
    },
    "StudentPaper": {
        "type": "object",
        "properties": {
            "id":           {"type": "integer"},
            "subject_id":   {"type": "integer"},
            "student_id":   {"type": "integer"},
            "student_name": {"type": "string"},
            "score":        {"type": "number", "format": "float", "example": 14.5},
            "grade":        {"type": "string", "description": "Feedback IA complet"},
            "filename":     {"type": "string"},
            "corrected_at": {"type": "string", "format": "date-time"},
            "email_sent":   {"type": "boolean"}
        }
    },
    "OnlineExam": {
        "type": "object",
        "description": "Champs réels renvoyés par OnlineExam.to_dict() (models.py) — pas de champ access_code/max_attempts : la reprise après déconnexion utilise un code persistant et self-service par tentative (voir /api/online_exams/{exam_id}/start).",
        "properties": {
            "id":                  {"type": "integer"},
            "subject_id":          {"type": "integer"},
            "subject_title":       {"type": "string"},
            "title":               {"type": "string"},
            "instructions":        {"type": "string"},
            "duration_minutes":    {"type": "integer", "example": 90, "description": "Calculé automatiquement à partir de start_time/end_time"},
            "start_time":          {"type": "string", "format": "date-time"},
            "end_time":            {"type": "string", "format": "date-time"},
            "status":              {"type": "string", "enum": ["draft","scheduled","active","closed"]},
            "max_tab_switches":    {"type": "integer", "default": 2},
            "enable_copy_paste":   {"type": "boolean", "default": False},
            "enable_right_click":  {"type": "boolean", "default": False},
            "enable_file_download": {"type": "boolean", "default": False, "description": "Autoriser le téléchargement des fichiers du sujet"},
            "randomize_questions": {"type": "boolean", "default": False},
            "questions_per_page":  {"type": "integer", "default": 5},
            "time_per_question_seconds": {"type": "integer", "nullable": True, "description": "Minuteur par page pour les questions fermées (QCM/Vrai-Faux uniquement, jamais les questions ouvertes). NULL/absent = désactivé. À expiration, avance automatique à la page suivante."},
            "max_no_face_count":   {"type": "integer", "default": 10},
            "ban_on_devtools":     {"type": "boolean", "default": True},
            "auto_ban_enabled":    {"type": "boolean", "default": False},
            "auto_correct":        {"type": "boolean", "default": False},
            "results_published":   {"type": "boolean", "default": False},
            "enable_calculator":   {"type": "boolean", "default": False, "description": "Calculatrice scientifique intégrée à la page de composition"},
            "allow_secondary_camera": {"type": "boolean", "default": False, "description": "Autorise l'étudiant à coupler une caméra secondaire via smartphone (angle latéral)"},
            "require_biometric": {"type": "boolean", "default": False, "description": "Exige une vérification d'identité par reconnaissance faciale avant l'accès à cet examen (opt-in par examen, décoché par défaut pour tout nouvel examen — les examens créés avant ce champ ne sont pas rétroactivement modifiés)"},
            "auto_correct":        {"type": "boolean", "default": False, "description": "Correction IA automatique dès qu'un étudiant soumet sa copie"},
            "scheduled_correction_at": {"type": "string", "format": "date-time", "nullable": True, "description": "Heure précise programmée pour corriger en bloc toutes les copies soumises non corrigées — voir /api/agent/run_scheduled_correction/{exam_id}"},
            "correction_triggered_at": {"type": "string", "format": "date-time", "nullable": True, "description": "Renseigné automatiquement une fois la correction planifiée effectivement déclenchée — empêche tout second déclenchement"},
            "creator_name":        {"type": "string"},
            "created_at":          {"type": "string", "format": "date-time"},
            "is_active":           {"type": "boolean"},
            "attempts_count":      {"type": "integer"}
        }
    },
    "ExamAttempt": {
        "type": "object",
        "properties": {
            "id":             {"type": "integer"},
            "exam_id":        {"type": "integer"},
            "student_id":     {"type": "integer"},
            "student_name":   {"type": "string"},
            "status":         {"type": "string", "enum": ["in_progress","submitted","auto_submitted","graded","banned"]},
            "score":          {"type": "number", "format": "float"},
            "risk_score":     {"type": "integer", "minimum": 0, "maximum": 100},
            "tab_switches":   {"type": "integer"},
            "warnings_count": {"type": "integer"},
            "started_at":     {"type": "string", "format": "date-time"},
            "submitted_at":   {"type": "string", "format": "date-time"},
            "last_seen_at":   {"type": "string", "format": "date-time", "nullable": True, "description": "Horodatage du dernier heartbeat reçu (voir /api/exam_attempts/{attempt_id}/heartbeat). Purement informatif — ne déclenche jamais de violation/risk_score, sert uniquement au badge 'hors ligne' côté surveillant au-delà du seuil (60s)."}
        }
    },
    "Pole": {
        "type": "object",
        "description": "Pôle académique UNCHK — racine de la hiérarchie Pôle → Niveau → Formation.",
        "properties": {
            "id":               {"type": "integer"},
            "code":             {"type": "string", "example": "STN"},
            "name":             {"type": "string", "example": "Sciences et Technologies du Numérique"},
            "description":      {"type": "string"},
            "is_active":        {"type": "boolean"},
            "formations_count": {"type": "integer", "description": "Nombre de formations rattachées (via un Niveau de ce pôle)"},
            "created_at":       {"type": "string", "format": "date-time"}
        }
    },
    "Niveau": {
        "type": "object",
        "description": "Niveau académique (Licence 1..Master 2), rattaché à un Pôle. Le code n'est pas unique globalement — seulement par pôle (ex: 'L1' peut exister sous STN ET sous LSHE).",
        "properties": {
            "id":               {"type": "integer"},
            "code":             {"type": "string", "example": "L3"},
            "name":             {"type": "string", "example": "Licence 3"},
            "description":      {"type": "string"},
            "pole_id":          {"type": "integer"},
            "pole_code":        {"type": "string", "example": "STN"},
            "pole_name":        {"type": "string"},
            "is_active":        {"type": "boolean"},
            "formations_count": {"type": "integer"},
            "created_at":       {"type": "string", "format": "date-time"}
        }
    },
    "Formation": {
        "type": "object",
        "properties": {
            "id":              {"type": "integer"},
            "code":            {"type": "string", "example": "L3-TR-DEV"},
            "name":            {"type": "string", "example": "Licence 3 Telecoms-DevOps"},
            "level":           {"type": "string", "description": "Texte synchronisé automatiquement depuis niveau.name — ne pas définir directement, dérivé de niveau_id", "example": "Licence 3"},
            "department":      {"type": "string", "example": "Trunc Commun"},
            "description":     {"type": "string"},
            "niveau_id":       {"type": "integer", "description": "Niveau de rattachement — détermine aussi pole_id (dérivé, non saisi directement)"},
            "niveau_code":     {"type": "string", "example": "L3"},
            "niveau_name":     {"type": "string", "example": "Licence 3"},
            "pole_id":         {"type": "integer", "description": "Dérivé de niveau.pole_id — ne pas définir directement"},
            "pole_code":       {"type": "string", "example": "STN"},
            "pole_name":       {"type": "string"},
            "is_active":       {"type": "boolean"},
            "semesters_count": {"type": "integer"},
            "created_at":      {"type": "string", "format": "date-time"}
        }
    },
    "Semester": {
        "type": "object",
        "properties": {
            "id":           {"type": "integer"},
            "name":         {"type": "string", "example": "Semestre 1"},
            "formation_id": {"type": "integer"},
            "order":        {"type": "integer"}
        }
    },
    "UE": {
        "type": "object",
        "properties": {
            "id":          {"type": "integer"},
            "name":        {"type": "string", "example": "Réseaux et Télécommunications"},
            "code":        {"type": "string", "example": "RT301"},
            "semester_id": {"type": "integer"},
            "credits":     {"type": "number"},
            "coefficient": {"type": "number"}
        }
    },
    "EC": {
        "type": "object",
        "properties": {
            "id":          {"type": "integer"},
            "name":        {"type": "string", "example": "Protocoles TCP/IP"},
            "code":        {"type": "string", "example": "RT301-01"},
            "ue_id":       {"type": "integer"},
            "coefficient": {"type": "number"},
            "cm":          {"type": "integer", "description": "Heures Cours Magistral"},
            "td":          {"type": "integer", "description": "Heures Travaux Dirigés"},
            "tp":          {"type": "integer", "description": "Heures Travaux Pratiques"},
            "tpe":         {"type": "integer", "description": "Travail Personnel Étudiant"},
            "vht":         {"type": "integer", "description": "Volume Horaire Total"},
            "is_active":   {"type": "boolean"}
        }
    },
    "ProctorGroup": {
        "type": "object",
        "description": "Groupe de surveillants rattaché à un ou plusieurs EC — chaque membre est automatiquement affecté à tout nouvel examen créé pour ces EC.",
        "properties": {
            "id":         {"type": "integer"},
            "name":       {"type": "string", "example": "Surveillants Informatique L1"},
            "created_by": {"type": "string", "description": "Nom de l'admin ayant créé le groupe"},
            "created_at": {"type": "string", "format": "date-time"},
            "ec_ids":     {"type": "array", "items": {"type": "integer"}},
            "vigilance_level": {"type": "string", "enum": ["A", "B", "C"], "default": "A", "description": "Niveau de vigilance exigé des membres pour être comptés 'actifs et engagés' côté superviseur"},
            "members": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id":                     {"type": "integer", "description": "id de la ligne d'appartenance au groupe"},
                        "proctor_id":             {"type": "integer"},
                        "proctor_name":           {"type": "string"},
                        "proctor_email":          {"type": "string"},
                        "proctor_last_login":     {"type": "string", "format": "date-time", "nullable": True}
                    }
                }
            },
            "supervisors": {
                "type": "array",
                "description": "Un ou plusieurs superviseurs peuvent être rattachés au même groupe.",
                "items": {
                    "type": "object",
                    "properties": {
                        "id":                {"type": "integer", "description": "id de la ligne de rattachement (à utiliser pour DELETE .../supervisors/{id})"},
                        "supervisor_id":     {"type": "integer"},
                        "supervisor_name":   {"type": "string"},
                        "supervisor_email":  {"type": "string"}
                    }
                }
            }
        }
    },
    "Reclamation": {
        "type": "object",
        "properties": {
            "id":       {"type": "integer"},
            "paper_id": {"type": "integer"},
            "reason":   {"type": "string"},
            "status":   {"type": "string", "enum": ["pending","resolved","rejected"]},
            "response": {"type": "string"},
            "ia_proposed_status": {"type": "string"},
            "ia_proposed_score":  {"type": "number"},
            "created_at": {"type": "string", "format": "date-time"}
        }
    },
    "RestitutionExample": {
        "type": "object",
        "properties": {
            "id":                  {"type": "integer"},
            "paper_id":            {"type": "integer", "nullable": True},
            "attempt_id":          {"type": "integer", "nullable": True},
            "subject_id":          {"type": "integer", "nullable": True},
            "subject_title":       {"type": "string", "nullable": True},
            "label":               {"type": "string", "enum": ["best", "improve"]},
            "anonymized_content":  {"type": "string"},
            "anonymized_feedback": {"type": "string", "nullable": True},
            "score":               {"type": "number", "nullable": True},
            "max_score":           {"type": "number"},
            "is_published":        {"type": "boolean"},
            "created_by_id":       {"type": "integer"},
            "creator_name":        {"type": "string", "nullable": True},
            "created_at":          {"type": "string", "format": "date-time", "nullable": True},
            "published_at":        {"type": "string", "format": "date-time", "nullable": True}
        }
    },
    "GradeTranscript": {
        "type": "object",
        "properties": {
            "id":              {"type": "integer"},
            "student_id":      {"type": "integer"},
            "student_name":    {"type": "string"},
            "semester_id":     {"type": "integer"},
            "semester_name":   {"type": "string"},
            "formation_name":  {"type": "string"},
            "gpa":             {"type": "number"},
            "total_credits":   {"type": "integer"},
            "obtained_credits":{"type": "integer"},
            "validated":       {"type": "boolean"},
            "generated_at":    {"type": "string", "format": "date-time"}
        }
    },
    "AgentAlert": {
        "type": "object",
        "properties": {
            "exam_id":      {"type": "integer"},
            "exam_title":   {"type": "string"},
            "attempt_id":   {"type": "integer"},
            "student_name": {"type": "string"},
            "risk_score":   {"type": "integer", "minimum": 0, "maximum": 100},
            "level":        {"type": "string", "enum": ["ALERTE","URGENT"]},
            "no_face":      {"type": "integer"},
            "multi_face":   {"type": "integer"},
            "tab_switches": {"type": "integer"},
            "ai_note":      {"type": "string"},
            "timestamp":    {"type": "string", "format": "date-time"},
            "read":         {"type": "boolean"}
        }
    },
    "ExamIncident": {
        "type": "object",
        "properties": {
            "id":           {"type": "integer"},
            "attempt_id":   {"type": "integer"},
            "student_name": {"type": "string"},
            "event_type":   {"type": "string"},
            "severity":     {"type": "string", "enum": ["high","medium","low"]},
            "timestamp":    {"type": "string", "format": "date-time"}
        }
    }
}

_RESPONSES = {
    "Unauthorized": {
        "description": "Token JWT manquant ou invalide",
        "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Error"}}}
    },
    "Forbidden": {
        "description": "Droits insuffisants",
        "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Error"}}}
    },
    "NotFound": {
        "description": "Ressource introuvable",
        "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Error"}}}
    }
}

# ─────────────────────────────────────────────────────────────────────────────
# Spec OpenAPI 3.0 complète
# ─────────────────────────────────────────────────────────────────────────────

OPENAPI_SPEC = {
    "openapi": "3.0.3",
    "info": {
        "title": "CEI — Centre d'Examen Intelligent API",
        "version": "2.1.0",
        "description": (
            "API REST complète de la plateforme CEI de l'**UNCHK — VisioPLUS**.\n\n"
            "## Authentification\n"
            "1. `POST /api/auth/login` → récupérer `access_token`\n"
            "2. Bouton **Authorize** → saisir `Bearer <access_token>`\n\n"
            "## Rôles\n"
            "| Rôle | Accès |\n|---|---|\n"
            "| `admin` | Complet |\n"
            "| `professor` | Sujets, examens, corrections |\n"
            "| `superviseur` | Supervision des groupes de surveillants (dashboard, demandes d'appel) |\n"
            "| `surveillant` | Dashboard surveillance |\n"
            "| `student` | Examens, notes, réclamations |\n\n"
            "## Chaîne IA\n"
            "Anthropic Claude → Google Gemini → DeepSeek → Ollama local\n\n"
            "## Score de risque (proctoring)\n"
            "| Événement | Points |\n|---|---|\n"
            "| Visage absent | +10 |\n| Plusieurs visages | +20 |\n"
            "| Changement onglet | +15 (max 60) |\n| Avertissement | +5 (max 40) |"
        ),
        "contact": {
            "name": "UNCHK — VisioPLUS",
            "email": "visioplus@unchk.edu.sn",
            "url": "https://dev-cei.ddns.net"
        },
        "license": {"name": "MIT", "url": "https://opensource.org/licenses/MIT"}
    },
    "servers": [
        {"url": "https://dev-cei.ddns.net", "description": "Production UNCHK"},
        {"url": "http://localhost:5000",    "description": "Développement local"}
    ],
    "tags": [
        {"name": "Authentification",         "description": "Connexion PASETO v4, rafraîchissement token, déconnexion, profil, mot de passe"},
        {"name": "Administration",           "description": "Tableau de bord admin, utilisateurs, historique"},
        {"name": "Académique",               "description": "Pôles, Niveaux, Formations, semestres, UE, EC, inscriptions, affectations — hiérarchie Pôle → Niveau → Formation → Semestre → UE → EC"},
        {"name": "Groupes Surveillants",      "description": "Groupes de surveillants rattachés à un ou plusieurs EC — affectation automatique à chaque nouvel examen créé pour ces EC"},
        {"name": "Import CSV",               "description": "Import en masse d'utilisateurs et de maquette pédagogique"},
        {"name": "Sujets",                   "description": "Upload et gestion des sujets d'examen"},
        {"name": "Copies",                   "description": "Upload, correction IA et export des copies étudiants"},
        {"name": "Examens en ligne",         "description": "Création, gestion du cycle de vie et tentatives étudiants"},
        {"name": "Surveillant",              "description": "Routes dédiées aux surveillants : examens assignés, monitoring en direct, avertissements, bannissements, messages, enregistrements"},
        {"name": "Superviseur",              "description": "Rôle positionné au-dessus des surveillants : suivi de leur engagement réel (niveaux de vigilance A/B/C) et réponse aux demandes de reprise après déconnexion quand aucun surveillant n'est assigné"},
        {"name": "Proctoring",               "description": "Infrastructure de surveillance vidéo LiveKit — tokens, snapshots caméra, événements, enregistrements"},
        {"name": "Agent autonome",           "description": "API du service de surveillance IA autonome — statut, alertes, heartbeat"},
        {"name": "Intelligence Artificielle","description": "Génération de sujets et suggestions par IA"},
        {"name": "Réclamations",             "description": "Dépôt, traitement IA et décision sur les réclamations"},
        {"name": "Relevés de notes",         "description": "Génération et téléchargement des relevés PDF"},
        {"name": "Tableaux de bord",         "description": "Dashboards professeur et étudiant"},
        {"name": "Système",                  "description": "Endpoints d'infrastructure (health check pour load balancer / monitoring)"},
        {"name": "Biométrie",                "description": "Inscription et vérification d'un facteur biométrique (reconnaissance faciale ou WebAuthn) exigé à chaque accès à un examen"},
    ],
    "components": {
        "securitySchemes": {
            "BearerAuth": {
                "type": "http", "scheme": "bearer", "bearerFormat": "PASETO-v4-public",
                "description": "PASETO v4.public token obtenu via POST /api/auth/login"
            },
            "AgentSecret": {
                "type": "apiKey", "in": "header", "name": "X-Agent-Secret",
                "description": "Clé AGENT_SECRET_KEY du service agent proctor"
            }
        },
        "schemas": _SCHEMAS,
        "responses": _RESPONSES
    },
    "security": [{"BearerAuth": []}],
    "paths": {

        # ══════════════════════════════════════════════════════════════════════
        # AUTHENTIFICATION
        # ══════════════════════════════════════════════════════════════════════

        "/api/auth/login": {"post": {
            "tags": ["Authentification"], "summary": "Connexion — obtenir un token PASETO v4",
            "description": "Retourne un **access token PASETO v4.public** (15 min, à stocker en mémoire) et pose un cookie httpOnly `cei_refresh` (7 jours) pour le rafraîchissement. **Session unique (étudiants uniquement, 24/08 puis 29/08)** : si le compte a déjà une session active sur un autre appareil, la connexion est refusée avec `409` — renvoyer `force: true` pour déconnecter l'autre appareil et se connecter quand même. Depuis le 29/08, `force: true` révoque réellement l'ancien appareil dès sa PROCHAINE requête (pas seulement à sa prochaine reconnexion) : l'access token porte un `sid` interne revérifié à chaque appel `@paseto_required`, tout endpoint appelé par l'ancien appareil renvoie alors `401 {session_superseded: true}`.",
            "security": [],
            "requestBody": {"required": True, "content": {"application/json": {"schema": {
                "type": "object", "required": ["email","password"],
                "properties": {
                    "email":    {"type": "string", "example": "serge@rtn.sn"},
                    "password": {"type": "string", "example": "passer"},
                    "force":    {"type": "boolean", "description": "Déconnecte l'autre appareil déjà connecté (étudiants) et procède à la connexion malgré le conflit de session."}
                }
            }}}},
            "responses": {
                "200": {"description": "Token PASETO retourné + cookie refresh posé", "content": {"application/json": {"schema": {
                    "type": "object",
                    "properties": {
                        "success":      {"type": "boolean"},
                        "access_token": {"type": "string", "example": "v4.public.eyJzdWIiOi..."},
                        "user":         {"$ref": "#/components/schemas/User"}
                    }
                }}}},
                "401": {"description": "Identifiants incorrects"},
                "403": {"description": "Compte désactivé"},
                "409": {"description": "Session déjà active sur un autre appareil (étudiants) — renvoyer avec force: true pour la déconnecter", "content": {"application/json": {"schema": {
                    "type": "object",
                    "properties": {
                        "error": {"type": "string"},
                        "session_conflict": {"type": "boolean"},
                        "device_label": {"type": "string", "example": "Chrome sur Windows"},
                        "since": {"type": "string", "format": "date-time"}
                    }
                }}}}
            }
        }},
        "/api/auth/register": {"post": {
            "tags": ["Authentification"], "summary": "Créer un compte",
            "description": "Auto-inscription publique — crée toujours un compte **student**, quoi qu'il arrive. Il n'y a pas de champ `role` : cette route ne permet de créer que des étudiants. Pour créer un compte professeur, surveillant, superviseur ou admin, un administrateur doit utiliser `POST /api/admin/users`.",
            "security": [],
            "requestBody": {"required": True, "content": {"application/json": {"schema": {
                "type": "object", "required": ["email","password","full_name"],
                "properties": {
                    "email":         {"type": "string"},
                    "password":      {"type": "string"},
                    "full_name":     {"type": "string"},
                    "formation_id":  {"type": "integer", "description": "Optionnel — rattache l'étudiant à sa Formation et l'inscrit automatiquement à toutes les UE de cette formation."}
                }
            }}}},
            "responses": {"201": {"description": "Compte créé (toujours role=student)"}, "400": {"description": "Email déjà utilisé"}}
        }},
        "/api/auth/me": {"get": {
            "tags": ["Authentification"], "summary": "Profil de l'utilisateur connecté",
            "responses": {
                "200": {"description": "Profil", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/User"}}}},
                "401": {"$ref": "#/components/responses/Unauthorized"}
            }
        }},
        "/api/profile": {"put": {
            "tags": ["Authentification"], "summary": "Modifier son profil",
            "requestBody": {"content": {"application/json": {"schema": {
                "type": "object",
                "properties": {"full_name": {"type": "string"}, "email": {"type": "string"}}
            }}}},
            "responses": {"200": {"description": "Profil mis à jour"}}
        }},
        "/api/profile/password": {"put": {
            "tags": ["Authentification"], "summary": "Changer son mot de passe",
            "requestBody": {"required": True, "content": {"application/json": {"schema": {
                "type": "object", "required": ["current_password","new_password"],
                "properties": {
                    "current_password":  {"type": "string"},
                    "new_password":      {"type": "string", "minLength": 6},
                    "confirm_password":  {"type": "string", "description": "Confirmation du nouveau mot de passe"}
                }
            }}}},
            "responses": {"200": {"description": "Mot de passe modifié"}, "400": {"description": "Mot de passe actuel incorrect ou confirmation non concordante"}}
        }},
        "/api/auth/refresh": {"post": {
            "tags": ["Authentification"], "summary": "Rafraîchir l'access token (cookie refresh requis)",
            "description": "Utilise le cookie httpOnly `cei_refresh` pour émettre un nouvel access token. L'ancien refresh token est révoqué (rotation). Envoyer la requête avec `credentials: 'include'` depuis le frontend.",
            "security": [],
            "responses": {
                "200": {"description": "Nouvel access token + nouveau cookie refresh", "content": {"application/json": {"schema": {
                    "type": "object",
                    "properties": {
                        "success":      {"type": "boolean"},
                        "access_token": {"type": "string", "example": "v4.public.eyJzdWIiOi..."}
                    }
                }}}},
                "401": {"description": "Cookie absent, token révoqué ou expiré"}
            }
        }},
        "/api/auth/logout": {"post": {
            "tags": ["Authentification"], "summary": "Déconnexion — révoquer le refresh token",
            "description": "Révoque le refresh token courant (inscrit en base dans `token_blocklist`) et supprime le cookie `cei_refresh`.",
            "responses": {
                "200": {"description": "Déconnecté avec succès"},
                "401": {"description": "Token access manquant"}
            }
        }},
        "/api/auth/public-key": {"get": {
            "tags": ["Authentification"], "summary": "Clé publique Ed25519 du serveur",
            "description": "Expose la clé publique PASETO v4 (Ed25519) encodée en base64. Utilisable par le frontend pour vérifier localement les tokens.",
            "security": [],
            "responses": {
                "200": {"description": "Clé publique", "content": {"application/json": {"schema": {
                    "type": "object",
                    "properties": {
                        "version":           {"type": "string", "example": "v4.public"},
                        "algorithm":         {"type": "string", "example": "Ed25519"},
                        "public_key":        {"type": "string", "description": "PEM encodé en base64"},
                        "token_ttl_minutes": {"type": "integer", "example": 15}
                    }
                }}}}
            }
        }},

        # ══════════════════════════════════════════════════════════════════════
        # ADMINISTRATION
        # ══════════════════════════════════════════════════════════════════════

        "/api/admin/dashboard": {"get": {
            "tags": ["Administration"], "summary": "Statistiques globales (admin)",
            "responses": {
                "200": {"description": "Statistiques", "content": {"application/json": {"schema": {
                    "type": "object",
                    "properties": {
                        "total_users":           {"type": "integer"},
                        "total_students":        {"type": "integer"},
                        "total_professors":      {"type": "integer"},
                        "total_surveillants":    {"type": "integer"},
                        "total_subjects":        {"type": "integer"},
                        "total_papers":          {"type": "integer"},
                        "total_corrected_papers":{"type": "integer"},
                        "active_exams":          {"type": "integer"},
                        "pending_reclamations":  {"type": "integer"}
                    }
                }}}},
                "403": {"$ref": "#/components/responses/Forbidden"}
            }
        }},
        "/api/admin/users": {
            "get": {
                "tags": ["Administration"], "summary": "Liste de tous les utilisateurs (admin)",
                "parameters": [
                    {"name": "role",   "in": "query", "schema": {"type": "string", "enum": ["admin","professor","surveillant","superviseur","student"]}},
                    {"name": "page",   "in": "query", "schema": {"type": "integer", "default": 1}},
                    {"name": "search", "in": "query", "schema": {"type": "string"}}
                ],
                "responses": {"200": {"description": "Liste paginée"}}
            },
            "post": {
                "tags": ["Administration"], "summary": "Créer un utilisateur (admin)",
                "description": "Envoie automatiquement un email 'compte créé' avec les identifiants en tâche de fond. Pour un étudiant, formation_id rattache l'étudiant à sa Formation (hiérarchie Pôle → Niveau → Formation) et l'inscrit automatiquement à toutes les UE de cette formation.",
                "requestBody": {"required": True, "content": {"application/json": {"schema": {
                    "type": "object", "required": ["email","full_name","role","password"],
                    "properties": {
                        "email":        {"type": "string"},
                        "full_name":    {"type": "string"},
                        "role":         {"type": "string", "enum": ["professor","surveillant","superviseur","student","admin"]},
                        "password":     {"type": "string"},
                        "niveau":       {"type": "string", "enum": ["L1","L2","L3","M1","M2"], "description": "Étudiant seulement. Fallback texte libre — ignoré/écrasé si formation_id est fourni (le niveau est alors dérivé de la formation)."},
                        "formation_id": {"type": "integer", "description": "Étudiant seulement. Rattache à une Formation et inscrit automatiquement à toutes ses UE."}
                    }
                }}}},
                "responses": {"201": {"description": "Utilisateur créé", "content": {"application/json": {"schema": {
                    "type": "object", "properties": {"success": {"type": "boolean"}, "message": {"type": "string"}, "user": {"$ref": "#/components/schemas/User"}}
                }}}}, "400": {"description": "Email déjà utilisé ou rôle invalide"}}
            }
        },
        "/api/admin/users/{target_id}": {
            "put": {
                "tags": ["Administration"], "summary": "Modifier un utilisateur (admin)",
                "parameters": [{"name": "target_id", "in": "path", "required": True, "schema": {"type": "integer"}, "description": "ID de l'utilisateur à modifier"}],
                "requestBody": {"content": {"application/json": {"schema": {
                    "type": "object",
                    "properties": {
                        "full_name":    {"type": "string"},
                        "email":        {"type": "string"},
                        "role":         {"type": "string", "enum": ["admin","professor","surveillant","superviseur","student"]},
                        "password":     {"type": "string"},
                        "is_active":    {"type": "boolean"},
                        "niveau":       {"type": "string", "enum": ["L1","L2","L3","M1","M2"]},
                        "formation_id": {"type": "integer", "description": "Change/ajoute la formation — réinscrit automatiquement aux UE manquantes (n'enlève jamais une inscription existante). Envoyer null pour détacher la formation."}
                    }
                }}}},
                "responses": {"200": {"description": "Mis à jour"}, "404": {"$ref": "#/components/responses/NotFound"}}
            },
            "delete": {
                "tags": ["Administration"], "summary": "Supprimer un utilisateur (admin)",
                "description": "Impossible de supprimer son propre compte.",
                "parameters": [{"name": "target_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
                "responses": {"200": {"description": "Supprimé"}, "400": {"description": "Impossible de se supprimer soi-même"}, "404": {"$ref": "#/components/responses/NotFound"}}
            }
        },
        "/api/admin/users/student-no-email": {"post": {
            "tags": ["Administration"],
            "summary": "Créer un étudiant sans adresse email (admin)",
            "description": "Crée un compte étudiant avec une adresse @no-email.cei.local générée automatiquement. Utile pour les étudiants sans email personnel.",
            "requestBody": {"required": True, "content": {"application/json": {"schema": {
                "type": "object", "required": ["full_name"],
                "properties": {
                    "full_name":    {"type": "string", "example": "Amadou Ba"},
                    "niveau":       {"type": "string", "enum": ["L1","L2","L3","M1","M2"], "description": "Fallback texte libre — ignoré si formation_id est fourni"},
                    "formation_id": {"type": "integer", "description": "Rattache à une Formation et inscrit automatiquement à toutes ses UE"}
                }
            }}}},
            "responses": {
                "201": {"description": "Étudiant créé", "content": {"application/json": {"schema": {
                    "type": "object",
                    "properties": {
                        "success":       {"type": "boolean"},
                        "user":          {"$ref": "#/components/schemas/User"},
                        "temp_password": {"type": "string", "description": "Mot de passe temporaire à communiquer à l'étudiant"}
                    }
                }}}},
                "400": {"description": "Nom déjà existant"}
            }
        }},
        "/api/admin/corrected_papers": {"get": {
            "tags": ["Administration"], "summary": "50 dernières copies corrigées (admin)",
            "responses": {
                "200": {"description": "Copies récentes", "content": {"application/json": {"schema": {
                    "type": "object",
                    "properties": {"papers": {"type": "array", "items": {"$ref": "#/components/schemas/StudentPaper"}}}
                }}}}
            }
        }},
        "/api/admin/exams_history": {"get": {
            "tags": ["Administration"], "summary": "Historique des examens terminés (admin)",
            "description": "Liste tous les examens clôturés avec statistiques : nombre de tentatives, moyenne, incidents, exclusions.",
            "responses": {
                "200": {"description": "Historique", "content": {"application/json": {"schema": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "integer"}, "title": {"type": "string"},
                            "total_attempts":   {"type": "integer"},
                            "submitted_count":  {"type": "integer"},
                            "banned_count":     {"type": "integer"},
                            "corrected_count":  {"type": "integer"},
                            "average_score":    {"type": "number"},
                            "incidents_count":  {"type": "integer"},
                            "start_time":       {"type": "string", "format": "date-time"},
                            "end_time":         {"type": "string", "format": "date-time"}
                        }
                    }
                }}}}
            }
        }},
        "/api/users/proctors": {"get": {
            "tags": ["Administration"], "summary": "Liste des surveillants disponibles",
            "description": "Retourne les utilisateurs avec le rôle `surveillant` actifs. Utilisé pour affecter des surveillants à un examen.",
            "responses": {
                "200": {"description": "Surveillants", "content": {"application/json": {"schema": {
                    "type": "array", "items": {"$ref": "#/components/schemas/User"}
                }}}}
            }
        }},
        "/api/students/list": {"get": {
            "tags": ["Administration"], "summary": "Liste complète des étudiants (prof/admin)",
            "responses": {
                "200": {"description": "Étudiants", "content": {"application/json": {"schema": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "integer"},
                            "full_name": {"type": "string"},
                            "email": {"type": "string"}
                        }
                    }
                }}}}
            }
        }},

        # ══════════════════════════════════════════════════════════════════════
        # ACADÉMIQUE — Pôles / Niveaux / Formations / Semestres / UE / EC
        # Hiérarchie : Pôle → Niveau → Formation → Semestre → UE → EC
        # ══════════════════════════════════════════════════════════════════════

        "/api/poles": {"get": {
            "tags": ["Académique"], "summary": "Liste des pôles actifs",
            "responses": {"200": {"description": "Pôles", "content": {"application/json": {"schema": {
                "type": "array", "items": {"$ref": "#/components/schemas/Pole"}
            }}}}}
        }},
        "/api/poles/{pole_id}/formations": {"get": {
            "tags": ["Académique"], "summary": "Formations d'un pôle (via leurs niveaux)",
            "parameters": [{"name": "pole_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
            "responses": {"200": {"description": "Formations", "content": {"application/json": {"schema": {
                "type": "array", "items": {"$ref": "#/components/schemas/Formation"}
            }}}}}
        }},
        "/api/poles/{pole_id}/niveaux": {"get": {
            "tags": ["Académique"], "summary": "Niveaux d'un pôle",
            "parameters": [{"name": "pole_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
            "responses": {"200": {"description": "Niveaux", "content": {"application/json": {"schema": {
                "type": "array", "items": {"$ref": "#/components/schemas/Niveau"}
            }}}}}
        }},
        "/api/admin/poles": {"post": {
            "tags": ["Académique"], "summary": "Créer un pôle (admin)",
            "requestBody": {"required": True, "content": {"application/json": {"schema": {
                "type": "object", "required": ["code", "name"],
                "properties": {
                    "code":        {"type": "string", "example": "STN"},
                    "name":        {"type": "string", "example": "Sciences et Technologies du Numérique"},
                    "description": {"type": "string"}
                }
            }}}},
            "responses": {
                "201": {"description": "Pôle créé", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Pole"}}}},
                "200": {"description": "Pôle réactivé (un pôle désactivé avec ce code existait déjà)"}
            }
        }},
        "/api/admin/poles/{pid}": {
            "put": {
                "tags": ["Académique"], "summary": "Modifier un pôle (admin)",
                "parameters": [{"name": "pid", "in": "path", "required": True, "schema": {"type": "integer"}}],
                "requestBody": {"content": {"application/json": {"schema": {
                    "type": "object",
                    "properties": {"name": {"type": "string"}, "description": {"type": "string"}, "is_active": {"type": "boolean"}}
                }}}},
                "responses": {"200": {"description": "Pôle mis à jour"}}
            },
            "delete": {
                "tags": ["Académique"], "summary": "Supprimer un pôle et ses niveaux (admin)",
                "description": "Suppression définitive du pôle et de ses niveaux (cascade). Les formations qui en dépendaient sont détachées (niveau_id/pole_id → NULL), jamais supprimées.",
                "parameters": [{"name": "pid", "in": "path", "required": True, "schema": {"type": "integer"}}],
                "responses": {"200": {"description": "Supprimé"}, "404": {"$ref": "#/components/responses/NotFound"}}
            }
        },
        "/api/niveaux": {"get": {
            "tags": ["Académique"], "summary": "Liste de tous les niveaux actifs (tous pôles confondus)",
            "responses": {"200": {"description": "Niveaux", "content": {"application/json": {"schema": {
                "type": "array", "items": {"$ref": "#/components/schemas/Niveau"}
            }}}}}
        }},
        "/api/admin/niveaux": {"post": {
            "tags": ["Académique"], "summary": "Créer un niveau, rattaché à un pôle (admin)",
            "requestBody": {"required": True, "content": {"application/json": {"schema": {
                "type": "object", "required": ["code", "name", "pole_id"],
                "properties": {
                    "code":        {"type": "string", "example": "L3", "description": "Unique par pôle, pas globalement (ex: 'L1' peut exister sous 2 pôles différents)"},
                    "name":        {"type": "string", "example": "Licence 3"},
                    "description": {"type": "string"},
                    "pole_id":     {"type": "integer"}
                }
            }}}},
            "responses": {
                "201": {"description": "Niveau créé", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Niveau"}}}},
                "200": {"description": "Niveau réactivé (un niveau désactivé avec ce code existait déjà sous ce pôle)"},
                "400": {"description": "pole_id manquant, ou code déjà utilisé (actif) sous ce pôle"}
            }
        }},
        "/api/admin/niveaux/{nid}": {
            "put": {
                "tags": ["Académique"], "summary": "Modifier un niveau (admin)",
                "description": "Changer le nom ou le pôle synchronise automatiquement level/pole_id sur toutes les formations rattachées.",
                "parameters": [{"name": "nid", "in": "path", "required": True, "schema": {"type": "integer"}}],
                "requestBody": {"content": {"application/json": {"schema": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"}, "description": {"type": "string"},
                        "pole_id": {"type": "integer"}, "is_active": {"type": "boolean"}
                    }
                }}}},
                "responses": {"200": {"description": "Niveau mis à jour"}}
            },
            "delete": {
                "tags": ["Académique"], "summary": "Supprimer un niveau (admin)",
                "description": "Suppression définitive. Les formations qui en dépendaient sont détachées (niveau_id/pole_id → NULL), jamais supprimées.",
                "parameters": [{"name": "nid", "in": "path", "required": True, "schema": {"type": "integer"}}],
                "responses": {"200": {"description": "Supprimé"}, "404": {"$ref": "#/components/responses/NotFound"}}
            }
        },

        "/api/formations": {"get": {
            "tags": ["Académique"], "summary": "Liste des formations",
            "responses": {"200": {"description": "Formations", "content": {"application/json": {"schema": {
                "type": "array", "items": {"$ref": "#/components/schemas/Formation"}
            }}}}}
        }},
        "/api/formations/{formation_id}/semesters": {"get": {
            "tags": ["Académique"], "summary": "Semestres d'une formation",
            "parameters": [{"name": "formation_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
            "responses": {"200": {"description": "Semestres", "content": {"application/json": {"schema": {
                "type": "array", "items": {"$ref": "#/components/schemas/Semester"}
            }}}}}
        }},
        "/api/semesters/{semester_id}/ues": {"get": {
            "tags": ["Académique"], "summary": "UE d'un semestre",
            "parameters": [{"name": "semester_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
            "responses": {"200": {"description": "UE", "content": {"application/json": {"schema": {
                "type": "array", "items": {"$ref": "#/components/schemas/UE"}
            }}}}}
        }},
        "/api/ues/{ue_id}/ecs": {"get": {
            "tags": ["Académique"], "summary": "Éléments constitutifs d'une UE",
            "parameters": [{"name": "ue_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
            "responses": {"200": {"description": "EC", "content": {"application/json": {"schema": {
                "type": "array", "items": {"$ref": "#/components/schemas/EC"}
            }}}}}
        }},
        "/api/ecs": {"get": {
            "tags": ["Académique"], "summary": "Liste de tous les EC (filtrés par rôle)",
            "description": "Admin voit tous les EC. Professeur voit uniquement ses EC affectés.",
            "responses": {"200": {"description": "EC", "content": {"application/json": {"schema": {
                "type": "array", "items": {"$ref": "#/components/schemas/EC"}
            }}}}}
        }},
        "/api/admin/formations": {"post": {
            "tags": ["Académique"], "summary": "Créer une formation (admin)",
            "description": "pole_id/level ne se saisissent pas directement : ils sont dérivés de niveau_id (niveau.pole_id / niveau.name).",
            "requestBody": {"required": True, "content": {"application/json": {"schema": {
                "type": "object", "required": ["code", "name"],
                "properties": {
                    "code":        {"type": "string", "example": "L3-TR-DEV"},
                    "name":        {"type": "string", "example": "Licence 3 Telecoms-DevOps"},
                    "niveau_id":   {"type": "integer", "description": "Détermine aussi le pôle (dérivé de niveau.pole_id) et level (dérivé de niveau.name)"},
                    "department":  {"type": "string", "example": "Trunc Commun"},
                    "description": {"type": "string"}
                }
            }}}},
            "responses": {"201": {"description": "Formation créée", "content": {"application/json": {"schema": {
                "type": "object", "properties": {"success": {"type": "boolean"}, "formation": {"$ref": "#/components/schemas/Formation"}}
            }}}}}
        }},
        "/api/admin/formations/{fid}": {
            "put": {
                "tags": ["Académique"], "summary": "Modifier une formation (admin)",
                "parameters": [{"name": "fid", "in": "path", "required": True, "schema": {"type": "integer"}}],
                "requestBody": {"content": {"application/json": {"schema": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"}, "code": {"type": "string"},
                        "niveau_id": {"type": "integer", "description": "Change de niveau → pole_id et level resynchronisés automatiquement"},
                        "department": {"type": "string"}, "description": {"type": "string"}, "is_active": {"type": "boolean"}
                    }
                }}}},
                "responses": {"200": {"description": "Formation mise à jour"}}
            },
            "delete": {
                "tags": ["Académique"], "summary": "Supprimer une formation (admin)",
                "parameters": [{"name": "fid", "in": "path", "required": True, "schema": {"type": "integer"}}],
                "responses": {"200": {"description": "Supprimée"}, "404": {"$ref": "#/components/responses/NotFound"}}
            }
        },
        "/api/admin/semesters": {"post": {
            "tags": ["Académique"], "summary": "Créer un semestre (admin)",
            "requestBody": {"required": True, "content": {"application/json": {"schema": {
                "type": "object", "required": ["name","formation_id"],
                "properties": {
                    "name":         {"type": "string", "example": "Semestre 1"},
                    "formation_id": {"type": "integer"},
                    "order":        {"type": "integer", "example": 1}
                }
            }}}},
            "responses": {"201": {"description": "Semestre créé"}}
        }},
        "/api/admin/semesters/{sid}": {
            "put": {
                "tags": ["Académique"], "summary": "Modifier un semestre (admin)",
                "parameters": [{"name": "sid", "in": "path", "required": True, "schema": {"type": "integer"}}],
                "requestBody": {"content": {"application/json": {"schema": {
                    "type": "object",
                    "properties": {"name": {"type": "string"}, "order": {"type": "integer"}}
                }}}},
                "responses": {"200": {"description": "Mis à jour"}}
            },
            "delete": {
                "tags": ["Académique"], "summary": "Supprimer un semestre (admin)",
                "parameters": [{"name": "sid", "in": "path", "required": True, "schema": {"type": "integer"}}],
                "responses": {"200": {"description": "Supprimé"}}
            }
        },
        "/api/admin/ues": {"post": {
            "tags": ["Académique"], "summary": "Créer une UE (admin)",
            "requestBody": {"required": True, "content": {"application/json": {"schema": {
                "type": "object", "required": ["name","semester_id"],
                "properties": {
                    "name":        {"type": "string", "example": "Réseaux"},
                    "code":        {"type": "string"},
                    "semester_id": {"type": "integer"},
                    "credits":     {"type": "number", "example": 6},
                    "coefficient": {"type": "number", "example": 2}
                }
            }}}},
            "responses": {"201": {"description": "UE créée"}}
        }},
        "/api/admin/ues/{uid}": {
            "put": {
                "tags": ["Académique"], "summary": "Modifier une UE (admin)",
                "parameters": [{"name": "uid", "in": "path", "required": True, "schema": {"type": "integer"}}],
                "requestBody": {"content": {"application/json": {"schema": {
                    "type": "object",
                    "properties": {"name": {"type": "string"}, "code": {"type": "string"},
                                   "credits": {"type": "number"}, "coefficient": {"type": "number"}}
                }}}},
                "responses": {"200": {"description": "UE mise à jour"}}
            },
            "delete": {
                "tags": ["Académique"], "summary": "Supprimer une UE (admin)",
                "parameters": [{"name": "uid", "in": "path", "required": True, "schema": {"type": "integer"}}],
                "responses": {"200": {"description": "UE supprimée"}}
            }
        },
        "/api/admin/ecs": {"post": {
            "tags": ["Académique"], "summary": "Créer un EC (admin)",
            "requestBody": {"required": True, "content": {"application/json": {"schema": {
                "type": "object", "required": ["name","ue_id"],
                "properties": {
                    "name":        {"type": "string"},
                    "code":        {"type": "string"},
                    "ue_id":       {"type": "integer"},
                    "coefficient": {"type": "number", "example": 1},
                    "cm":          {"type": "integer", "default": 0, "description": "Heures Cours Magistral"},
                    "td":          {"type": "integer", "default": 0, "description": "Heures Travaux Dirigés"},
                    "tp":          {"type": "integer", "default": 0, "description": "Heures Travaux Pratiques"},
                    "tpe":         {"type": "integer", "default": 0, "description": "Travail Personnel Étudiant"},
                    "vht":         {"type": "integer", "default": 0, "description": "Volume Horaire Total"}
                }
            }}}},
            "responses": {"201": {"description": "EC créé"}}
        }},
        "/api/admin/ecs/{eid}": {
            "put": {
                "tags": ["Académique"], "summary": "Modifier un EC (admin)",
                "parameters": [{"name": "eid", "in": "path", "required": True, "schema": {"type": "integer"}}],
                "requestBody": {"content": {"application/json": {"schema": {
                    "type": "object",
                    "properties": {
                        "name":        {"type": "string"},
                        "code":        {"type": "string"},
                        "coefficient": {"type": "number"},
                        "cm":          {"type": "integer"},
                        "td":          {"type": "integer"},
                        "tp":          {"type": "integer"},
                        "tpe":         {"type": "integer"},
                        "vht":         {"type": "integer"},
                        "is_active":   {"type": "boolean"}
                    }
                }}}},
                "responses": {"200": {"description": "EC mis à jour"}}
            },
            "delete": {
                "tags": ["Académique"], "summary": "Supprimer un EC (admin)",
                "parameters": [{"name": "eid", "in": "path", "required": True, "schema": {"type": "integer"}}],
                "responses": {"200": {"description": "EC supprimé"}}
            }
        },
        "/api/admin/ec_assignments": {"post": {
            "tags": ["Académique"], "summary": "Affecter un professeur à un EC (admin)",
            "requestBody": {"required": True, "content": {"application/json": {"schema": {
                "type": "object", "required": ["ec_id","professor_id"],
                "properties": {
                    "ec_id":         {"type": "integer"},
                    "professor_id":  {"type": "integer"}
                }
            }}}},
            "responses": {"201": {"description": "Affectation créée"}, "409": {"description": "Déjà affecté"}}
        }},
        "/api/admin/ecs/{eid}/assign": {"post": {
            "tags": ["Académique"], "summary": "Affecter un professeur à un EC via l'ID EC (admin)",
            "parameters": [{"name": "eid", "in": "path", "required": True, "schema": {"type": "integer"}}],
            "requestBody": {"required": True, "content": {"application/json": {"schema": {
                "type": "object", "required": ["professor_id"],
                "properties": {"professor_id": {"type": "integer"}}
            }}}},
            "responses": {"201": {"description": "Affectation créée"}}
        }},
        "/api/admin/ec_assignments/{aid}": {"delete": {
            "tags": ["Académique"], "summary": "Retirer l'affectation d'un professeur (admin)",
            "parameters": [{"name": "aid", "in": "path", "required": True, "schema": {"type": "integer"}}],
            "responses": {"200": {"description": "Affectation supprimée"}}
        }},
        "/api/admin/student_enrollments": {"post": {
            "tags": ["Académique"], "summary": "Inscrire un étudiant à une UE ou un EC (admin)",
            "requestBody": {"required": True, "content": {"application/json": {"schema": {
                "type": "object", "required": ["student_id"],
                "properties": {
                    "student_id": {"type": "integer"},
                    "ue_id":      {"type": "integer"},
                    "ec_id":      {"type": "integer"}
                }
            }}}},
            "responses": {"201": {"description": "Inscrit"}, "409": {"description": "Déjà inscrit"}}
        }},
        "/api/admin/students/{student_id}/enroll": {"post": {
            "tags": ["Académique"], "summary": "Inscrire un étudiant à plusieurs UE/EC (admin)",
            "parameters": [{"name": "student_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
            "requestBody": {"required": True, "content": {"application/json": {"schema": {
                "type": "object",
                "properties": {
                    "ue_ids": {"type": "array", "items": {"type": "integer"}},
                    "ec_ids": {"type": "array", "items": {"type": "integer"}}
                }
            }}}},
            "responses": {"200": {"description": "Inscriptions effectuées"}}
        }},
        "/api/admin/student_enrollments/{eid}": {"delete": {
            "tags": ["Académique"], "summary": "Désinscrire un étudiant (admin)",
            "parameters": [{"name": "eid", "in": "path", "required": True, "schema": {"type": "integer"}}],
            "responses": {"200": {"description": "Désinscrit"}}
        }},
        "/api/admin/student_enrollments/bulk": {"post": {
            "tags": ["Académique"],
            "summary": "Inscrire plusieurs étudiants à une même UE en une fois (admin)",
            "description": "Même logique unitaire que POST /api/admin/student_enrollments, bouclée sur une liste de student_ids — rend les inscriptions de classes entières rapides. Notifie chaque étudiant nouvellement inscrit.",
            "requestBody": {"required": True, "content": {"application/json": {"schema": {
                "type": "object", "required": ["student_ids", "ue_id"],
                "properties": {
                    "student_ids": {"type": "array", "items": {"type": "integer"}},
                    "ue_id":       {"type": "integer"}
                }
            }}}},
            "responses": {
                "201": {"description": "Inscriptions effectuées", "content": {"application/json": {"schema": {
                    "type": "object",
                    "properties": {
                        "success":          {"type": "boolean"},
                        "enrolled":         {"type": "integer"},
                        "already_enrolled": {"type": "integer"},
                        "errors":           {"type": "array", "items": {"type": "string"}}
                    }
                }}}},
                "400": {"description": "Étudiants ou UE manquants"},
                "403": {"$ref": "#/components/responses/Forbidden"},
                "404": {"$ref": "#/components/responses/NotFound"}
            }
        }},
        "/api/admin/student_enrollments/bulk_remove": {"post": {
            "tags": ["Académique"],
            "summary": "Désinscrire plusieurs étudiants d'une même UE en une fois (admin)",
            "description": "Symétrique de bulk — retrait en masse (ex: erreur d'affectation, changement de maquette).",
            "requestBody": {"required": True, "content": {"application/json": {"schema": {
                "type": "object", "required": ["student_ids", "ue_id"],
                "properties": {
                    "student_ids": {"type": "array", "items": {"type": "integer"}},
                    "ue_id":       {"type": "integer"}
                }
            }}}},
            "responses": {
                "200": {"description": "Désinscriptions effectuées", "content": {"application/json": {"schema": {
                    "type": "object", "properties": {"success": {"type": "boolean"}, "removed": {"type": "integer"}}
                }}}},
                "400": {"description": "Étudiants ou UE manquants"},
                "403": {"$ref": "#/components/responses/Forbidden"}
            }
        }},

        # ══════════════════════════════════════════════════════════════════════
        # GROUPES SURVEILLANTS
        # ══════════════════════════════════════════════════════════════════════

        "/api/admin/proctor_groups": {
            "get": {
                "tags": ["Groupes Surveillants"], "summary": "Liste des groupes de surveillants",
                "responses": {"200": {"description": "Groupes", "content": {"application/json": {"schema": {
                    "type": "array", "items": {"$ref": "#/components/schemas/ProctorGroup"}
                }}}}}
            },
            "post": {
                "tags": ["Groupes Surveillants"], "summary": "Créer un groupe de surveillants (admin)",
                "requestBody": {"required": True, "content": {"application/json": {"schema": {
                    "type": "object", "required": ["name"],
                    "properties": {"name": {"type": "string", "example": "Surveillants Informatique L1"}}
                }}}},
                "responses": {"201": {"description": "Groupe créé", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ProctorGroup"}}}}, "400": {"description": "Nom requis"}}
            }
        },
        "/api/admin/proctor_groups/{gid}": {
            "put": {
                "tags": ["Groupes Surveillants"], "summary": "Renommer un groupe ou régler son niveau de vigilance",
                "description": "Accessible à l'admin ET au professeur propriétaire du groupe (ProctorGroup.created_by_id). Le rattachement des superviseurs se fait via les routes dédiées POST/DELETE .../supervisors, pas ici.",
                "parameters": [{"name": "gid", "in": "path", "required": True, "schema": {"type": "integer"}}],
                "requestBody": {"content": {"application/json": {"schema": {
                    "type": "object", "properties": {
                        "name": {"type": "string"},
                        "vigilance_level": {"type": "string", "enum": ["A", "B", "C"], "description": "A=interaction réelle · B=A+consultation d'un étudiant récente · C=B+vérification caméra périodique du surveillant (booléen seulement, aucune image transmise/stockée)"}
                    }
                }}}},
                "responses": {"200": {"description": "Groupe mis à jour", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ProctorGroup"}}}}, "403": {"description": "Non géré par ce professeur"}}
            },
            "delete": {
                "tags": ["Groupes Surveillants"], "summary": "Supprimer un groupe (admin)",
                "parameters": [{"name": "gid", "in": "path", "required": True, "schema": {"type": "integer"}}],
                "responses": {"200": {"description": "Supprimé"}, "404": {"$ref": "#/components/responses/NotFound"}}
            }
        },
        "/api/admin/proctor_groups/{gid}/members": {"post": {
            "tags": ["Groupes Surveillants"], "summary": "Ajouter des surveillants à un groupe (admin)",
            "description": "Notifie automatiquement chaque surveillant ajouté. Se propage immédiatement à tous les examens DRAFT/SCHEDULED des EC rattachés à ce groupe : le nouveau membre est ajouté comme surveillant et les étudiants inscrits sont ré-répartis (round-robin) entre tous les surveillants du groupe (services/proctor_service.sync_ec_proctors). Un examen déjà ACTIVE n'est pas resynchronisé.",
            "parameters": [{"name": "gid", "in": "path", "required": True, "schema": {"type": "integer"}}],
            "requestBody": {"required": True, "content": {"application/json": {"schema": {
                "type": "object", "required": ["proctor_ids"],
                "properties": {"proctor_ids": {"type": "array", "items": {"type": "integer"}}}
            }}}},
            "responses": {"200": {"description": "Membres ajoutés", "content": {"application/json": {"schema": {
                "type": "object", "properties": {"group": {"$ref": "#/components/schemas/ProctorGroup"}}
            }}}}}
        }},
        "/api/admin/proctor_groups/{gid}/members/{mid}": {"delete": {
            "tags": ["Groupes Surveillants"], "summary": "Retirer un membre d'un groupe (admin)",
            "description": "Se propage immédiatement aux examens DRAFT/SCHEDULED des EC rattachés : le surveillant retiré perd son affectation et ses étudiants sont ré-répartis entre les membres restants.",
            "parameters": [
                {"name": "gid", "in": "path", "required": True, "schema": {"type": "integer"}},
                {"name": "mid", "in": "path", "required": True, "schema": {"type": "integer"}, "description": "id de la ligne d'appartenance (pas l'id du surveillant)"}
            ],
            "responses": {"200": {"description": "Retiré"}}
        }},
        "/api/admin/proctor_groups/{gid}/supervisors": {"post": {
            "tags": ["Groupes Surveillants"], "summary": "Rattacher un ou plusieurs superviseurs à un groupe",
            "description": "Accessible à l'admin ET au professeur propriétaire du groupe (ProctorGroup.created_by_id) — même règle que le reste de la gestion du groupe. Un groupe peut avoir plusieurs superviseurs.",
            "parameters": [{"name": "gid", "in": "path", "required": True, "schema": {"type": "integer"}}],
            "requestBody": {"required": True, "content": {"application/json": {"schema": {
                "type": "object", "required": ["supervisor_ids"],
                "properties": {"supervisor_ids": {"type": "array", "items": {"type": "integer"}, "description": "ids d'utilisateurs de rôle superviseur"}}
            }}}},
            "responses": {"201": {"description": "Superviseurs ajoutés", "content": {"application/json": {"schema": {
                "type": "object", "properties": {"added": {"type": "integer"}, "already": {"type": "integer"}, "group": {"$ref": "#/components/schemas/ProctorGroup"}}
            }}}}}
        }},
        "/api/admin/proctor_groups/{gid}/supervisors/{sid}": {"delete": {
            "tags": ["Groupes Surveillants"], "summary": "Retirer un superviseur d'un groupe",
            "parameters": [
                {"name": "gid", "in": "path", "required": True, "schema": {"type": "integer"}},
                {"name": "sid", "in": "path", "required": True, "schema": {"type": "integer"}, "description": "id de la ligne de rattachement (pas l'id du superviseur)"}
            ],
            "responses": {"200": {"description": "Retiré"}}
        }},
        "/api/admin/proctor_groups/{gid}/ecs": {"post": {
            "tags": ["Groupes Surveillants"], "summary": "Rattacher un EC à un groupe (admin)",
            "description": "Tout examen créé pour cet EC affectera automatiquement tous les membres du groupe, avec pré-répartition des étudiants inscrits. Se propage aussi immédiatement aux examens DRAFT/SCHEDULED déjà existants pour cet EC. Un professeur ne peut rattacher que ses propres EC (403 sinon).",
            "parameters": [{"name": "gid", "in": "path", "required": True, "schema": {"type": "integer"}}],
            "requestBody": {"required": True, "content": {"application/json": {"schema": {
                "type": "object", "required": ["ec_id"],
                "properties": {"ec_id": {"type": "integer"}}
            }}}},
            "responses": {"200": {"description": "EC rattaché", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ProctorGroup"}}}}}
        }},
        "/api/admin/proctor_groups/{gid}/ecs/{ec_id}": {"delete": {
            "tags": ["Groupes Surveillants"], "summary": "Détacher un EC d'un groupe (admin)",
            "description": "Se propage immédiatement aux examens DRAFT/SCHEDULED de cet EC : les surveillants qui ne viennent plus d'aucun groupe rattaché sont retirés.",
            "parameters": [
                {"name": "gid", "in": "path", "required": True, "schema": {"type": "integer"}},
                {"name": "ec_id", "in": "path", "required": True, "schema": {"type": "integer"}}
            ],
            "responses": {"200": {"description": "Détaché"}}
        }},

        # ══════════════════════════════════════════════════════════════════════
        # IMPORT CSV
        # ══════════════════════════════════════════════════════════════════════

        "/api/admin/users/csv-template": {"get": {
            "tags": ["Import CSV"],
            "summary": "Télécharger le template CSV pour l'import d'utilisateurs",
            "description": "Retourne un fichier CSV avec les colonnes : full_name, email, role, password.",
            "responses": {
                "200": {
                    "description": "Fichier CSV template",
                    "content": {"text/csv": {"schema": {"type": "string", "format": "binary"}}}
                }
            }
        }},
        "/api/admin/maquette/csv-template": {"get": {
            "tags": ["Import CSV"],
            "summary": "Télécharger le template CSV pour la maquette pédagogique",
            "description": "Colonnes : type, pole_code, pole_name, pole_description, niveau_code, niveau_name, niveau_description, formation_code, formation_name, formation_department, semester_number, semester_name, semester_credits, ue_code, ue_name, ue_credits, ec_code, ec_name, ec_cm, ec_td, ec_tp, ec_tpe, ec_vht, ec_coefficient. Un Pôle ou Niveau qui n'existe pas encore (code + nom renseignés) est créé automatiquement à l'import.",
            "responses": {
                "200": {
                    "description": "Fichier CSV template",
                    "content": {"text/csv": {"schema": {"type": "string", "format": "binary"}}}
                }
            }
        }},
        "/api/admin/users/import-csv": {"post": {
            "tags": ["Import CSV"],
            "summary": "Importer des utilisateurs en masse depuis un fichier CSV",
            "description": "Crée les comptes utilisateurs en masse. Envoie un email de bienvenue à chaque utilisateur avec email valide. Colonne optionnelle formation_code (étudiants uniquement) : rattache immédiatement l'étudiant à sa Formation (Pôle/Niveau dérivés) et l'inscrit à toutes les UE de cette formation — sans elle, l'étudiant est créé sans rattachement (badge \"Sans pôle\").",
            "requestBody": {"required": True, "content": {"multipart/form-data": {"schema": {
                "type": "object", "required": ["file"],
                "properties": {"file": {"type": "string", "format": "binary", "description": "Fichier CSV (colonnes : full_name, email, role, password, formation_code [optionnel, étudiants])"}}
            }}}},
            "responses": {
                "200": {"description": "Import terminé", "content": {"application/json": {"schema": {
                    "type": "object",
                    "properties": {
                        "created":  {"type": "integer"},
                        "skipped":  {"type": "integer"},
                        "errors":   {"type": "array", "items": {"type": "string"}}
                    }
                }}}}
            }
        }},
        "/api/admin/maquette/import-csv": {"post": {
            "tags": ["Import CSV"],
            "summary": "Importer la maquette pédagogique depuis un fichier CSV",
            "description": "Crée la hiérarchie Pôle → Niveau → Formation → Semestre → UE → EC depuis un fichier CSV. Pôle et Niveau sont créés automatiquement s'ils n'existent pas encore.",
            "requestBody": {"required": True, "content": {"multipart/form-data": {"schema": {
                "type": "object", "required": ["file"],
                "properties": {"file": {"type": "string", "format": "binary"}}
            }}}},
            "responses": {
                "200": {"description": "Maquette importée", "content": {"application/json": {"schema": {
                    "type": "object",
                    "properties": {
                        "success": {"type": "boolean"}, "message": {"type": "string"},
                        "created": {"type": "object", "properties": {
                            "formations": {"type": "integer"}, "semesters": {"type": "integer"},
                            "ues": {"type": "integer"}, "ecs": {"type": "integer"}
                        }},
                        "errors": {"type": "array", "items": {"type": "string"}}
                    }
                }}}}
            }
        }},
        "/api/admin/maquette/excel-template": {"get": {
            "tags": ["Import CSV"],
            "summary": "Télécharger le template Excel au format officiel de l'établissement",
            "description": "Colonnes UE (Code/Nom/Crédit/Type) fusionnées puis EC (Code/Nom/Coef.), pourcentages CC/EX entre crochets dans le nom de l'EC — ex: 'Introduction à la sociologie [CC:40%, EX:60%]'.",
            "responses": {"200": {"description": "Fichier Excel template", "content": {
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": {"schema": {"type": "string", "format": "binary"}}
            }}}
        }},
        "/api/admin/maquette/import-excel-preview": {"post": {
            "tags": ["Import CSV"],
            "summary": "Prévisualiser un import Excel UE/EC pour un semestre existant",
            "description": "Analyse le fichier sans rien écrire en base — signale les UE/EC déjà existants (already_exists) qui seront ignorés à la confirmation.",
            "requestBody": {"required": True, "content": {"multipart/form-data": {"schema": {
                "type": "object", "required": ["semester_id", "file"],
                "properties": {
                    "semester_id": {"type": "integer", "description": "Semestre cible — doit déjà exister (créé via Pôle → Niveau → Formation → Semestre)"},
                    "file":        {"type": "string", "format": "binary"}
                }
            }}}},
            "responses": {"200": {"description": "Aperçu", "content": {"application/json": {"schema": {
                "type": "object",
                "properties": {
                    "success": {"type": "boolean"}, "semester_id": {"type": "integer"}, "semester_name": {"type": "string"},
                    "ue_count": {"type": "integer"}, "ec_count": {"type": "integer"},
                    "ues": {"type": "array", "items": {"type": "object", "properties": {
                        "code": {"type": "string"}, "name": {"type": "string"}, "credits": {"type": "integer"},
                        "ue_type": {"type": "string"}, "already_exists": {"type": "boolean"},
                        "ecs": {"type": "array", "items": {"type": "object", "properties": {
                            "code": {"type": "string"}, "name": {"type": "string"}, "coefficient": {"type": "integer"},
                            "cc_percentage": {"type": "integer"}, "ex_percentage": {"type": "integer"}, "already_exists": {"type": "boolean"}
                        }}}
                    }}}
                }
            }}}}}
        }},
        "/api/admin/maquette/import-excel-confirm": {"post": {
            "tags": ["Import CSV"],
            "summary": "Confirmer un import Excel prévisualisé (crée réellement les UE/EC)",
            "description": "Prend en entrée exactement le tableau 'ues' renvoyé par import-excel-preview (éventuellement édité) ; les entrées already_exists=true sont ignorées.",
            "requestBody": {"required": True, "content": {"application/json": {"schema": {
                "type": "object", "required": ["semester_id", "ues"],
                "properties": {
                    "semester_id": {"type": "integer"},
                    "ues": {"type": "array", "items": {"type": "object"}, "description": "Format identique à la réponse de import-excel-preview"}
                }
            }}}},
            "responses": {"200": {"description": "Import confirmé", "content": {"application/json": {"schema": {
                "type": "object",
                "properties": {
                    "success": {"type": "boolean"}, "created_ues": {"type": "integer"},
                    "created_ecs": {"type": "integer"}, "skipped_existing": {"type": "integer"}
                }
            }}}}}
        }},

        # ══════════════════════════════════════════════════════════════════════
        # SUJETS
        # ══════════════════════════════════════════════════════════════════════

        "/api/subjects": {
            "get": {
                "tags": ["Sujets"], "summary": "Liste des sujets (filtrés par rôle et EC)",
                "parameters": [
                    {"name": "ec_id",  "in": "query", "schema": {"type": "integer"}},
                    {"name": "page",   "in": "query", "schema": {"type": "integer", "default": 1}},
                    {"name": "search", "in": "query", "schema": {"type": "string"}}
                ],
                "responses": {"200": {"description": "Sujets", "content": {"application/json": {"schema": {
                    "type": "array", "items": {"$ref": "#/components/schemas/Subject"}
                }}}}}
            },
            "post": {
                "tags": ["Sujets"], "summary": "Créer un sujet manuellement (titre + contenu + barème saisis)",
                "requestBody": {"required": True, "content": {"application/json": {"schema": {
                    "type": "object", "required": ["title", "content"],
                    "properties": {
                        "title":   {"type": "string"},
                        "content": {"type": "string"},
                        "rubric":  {"type": "string"},
                        "ec_id":   {"type": "integer"}
                    }
                }}}},
                "responses": {
                    "201": {"description": "Sujet créé", "content": {"application/json": {"schema": {
                        "type": "object", "properties": {"success": {"type": "boolean"}, "subject": {"$ref": "#/components/schemas/Subject"}}
                    }}}},
                    "403": {"$ref": "#/components/responses/Forbidden"},
                    "422": {"description": "Validation échouée (titre/contenu manquant)"}
                }
            }
        },
        "/api/subjects/{subject_id}": {
            "get": {
                "tags": ["Sujets"], "summary": "Détail d'un sujet",
                "parameters": [{"name": "subject_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
                "responses": {
                    "200": {"description": "Sujet", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Subject"}}}},
                    "404": {"$ref": "#/components/responses/NotFound"}
                }
            },
            "put": {
                "tags": ["Sujets"], "summary": "Éditer un sujet déjà validé (titre/contenu/barème)",
                "description": "Bloqué si un examen lié est déjà actif/clôturé ou a reçu des tentatives.",
                "parameters": [{"name": "subject_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
                "requestBody": {"content": {"application/json": {"schema": {
                    "type": "object",
                    "properties": {
                        "title":   {"type": "string"},
                        "content": {"type": "string"},
                        "rubric":  {"type": "string"}
                    }
                }}}},
                "responses": {
                    "200": {"description": "Sujet mis à jour", "content": {"application/json": {"schema": {
                        "type": "object", "properties": {"success": {"type": "boolean"}, "subject": {"$ref": "#/components/schemas/Subject"}}
                    }}}},
                    "403": {"$ref": "#/components/responses/Forbidden"},
                    "404": {"$ref": "#/components/responses/NotFound"}
                }
            },
            "delete": {
                "tags": ["Sujets"], "summary": "Supprimer un sujet (admin/prof propriétaire)",
                "parameters": [{"name": "subject_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
                "responses": {"200": {"description": "Supprimé"}, "403": {"$ref": "#/components/responses/Forbidden"}}
            }
        },
        "/api/subjects/upload": {"post": {
            "tags": ["Sujets"],
            "summary": "Uploader un ou plusieurs fichiers pour créer un sujet",
            "description": "Envoie un ou plusieurs PDF/DOCX/TXT via le champ répété `files`. Chaque fichier est extrait séparément puis concaténé avec un séparateur `--- Fichier: <nom> ---` avant d'être soumis à l'IA (utile pour un cours réparti en plusieurs documents : poly + TD + annales). L'IA génère automatiquement le barème. Support OCR pour les PDF CIDFont illisibles. Fichiers Word : le format réel (.doc binaire legacy pré-2007 vs .docx OOXML moderne) est détecté par signature d'octets, pas seulement par l'extension déclarée — un .doc legacy est extrait via `catdoc` (python-docx ne le lit pas).",
            "requestBody": {"required": True, "content": {"multipart/form-data": {"schema": {
                "type": "object", "required": ["files"],
                "properties": {
                    "files": {"type": "array", "items": {"type": "string", "format": "binary"}, "description": "Un ou plusieurs fichiers PDF/DOCX/TXT (champ répété)"},
                    "ec_id": {"type": "integer"},
                    "title": {"type": "string"},
                    "rubric_mode": {"type": "string", "enum": ["ai", "manual"], "description": "'ai' = barème généré par l'IA (défaut), 'manual' = squelette vierge à compléter"},
                    "total_points": {"type": "integer", "description": "Barème total souhaité (1-200, défaut 20)"}
                }
            }}}},
            "responses": {
                "201": {"description": "Sujet créé avec barème IA"},
                "400": {"description": "Aucun fichier fourni, type non autorisé, ou contenu illisible"}
            }
        }},

        # ══════════════════════════════════════════════════════════════════════
        # COPIES
        # ══════════════════════════════════════════════════════════════════════

        "/api/papers/correct": {"post": {
            "tags": ["Copies"],
            "summary": "Corriger une copie par IA (alias de /api/papers/upload)",
            "description": "Alias identique à `POST /api/papers/upload` — même fonction, même comportement. L'IA détecte le domaine et corrige selon le barème du sujet.",
            "requestBody": {"required": True, "content": {"multipart/form-data": {"schema": {
                "type": "object", "required": ["file","subject_id"],
                "properties": {
                    "file":         {"type": "string", "format": "binary"},
                    "subject_id":   {"type": "integer"},
                    "student_id":   {"type": "integer"},
                    "student_name": {"type": "string"}
                }
            }}}},
            "responses": {
                "200": {"description": "Copie corrigée", "content": {"application/json": {"schema": {
                    "type": "object",
                    "properties": {
                        "score":    {"type": "number", "example": 14.5},
                        "feedback": {"type": "string"},
                        "paper_id": {"type": "integer"}
                    }
                }}}},
                "400": {"description": "Fichier ou subject_id manquant"},
                "403": {"description": "Le professeur ne peut corriger que ses propres sujets"},
                "404": {"$ref": "#/components/responses/NotFound"}
            }
        }},
        "/api/papers/upload": {"post": {
            "tags": ["Copies"],
            "summary": "Uploader et corriger une copie par IA",
            "description": "L'IA détecte le domaine (droit, médecine, maths...) et corrige selon le barème du sujet.",
            "requestBody": {"required": True, "content": {"multipart/form-data": {"schema": {
                "type": "object", "required": ["file","subject_id"],
                "properties": {
                    "file":         {"type": "string", "format": "binary"},
                    "subject_id":   {"type": "integer"},
                    "student_id":   {"type": "integer"},
                    "student_name": {"type": "string"}
                }
            }}}},
            "responses": {
                "200": {"description": "Copie corrigée", "content": {"application/json": {"schema": {
                    "type": "object",
                    "properties": {
                        "score":    {"type": "number", "example": 14.5},
                        "feedback": {"type": "string"},
                        "paper_id": {"type": "integer"}
                    }
                }}}}
            }
        }},
        "/api/papers/upload-batch": {"post": {
            "tags": ["Copies"],
            "summary": "Correction en masse de plusieurs copies",
            "description": "Corrige plusieurs fichiers en une requête. Le nom de l'étudiant est extrait du contenu du fichier automatiquement.",
            "requestBody": {"required": True, "content": {"multipart/form-data": {"schema": {
                "type": "object", "required": ["files","subject_id"],
                "properties": {
                    "files":      {"type": "array", "items": {"type": "string", "format": "binary"}},
                    "subject_id": {"type": "integer"}
                }
            }}}},
            "responses": {
                "200": {"description": "Résultats par fichier", "content": {"application/json": {"schema": {
                    "type": "object",
                    "properties": {
                        "results":       {"type": "array", "items": {"type": "object"}},
                        "errors":        {"type": "array", "items": {"type": "string"}},
                        "success_count": {"type": "integer"},
                        "error_count":   {"type": "integer"}
                    }
                }}}}
            }
        }},
        "/api/papers/subject/{subject_id}": {"get": {
            "tags": ["Copies"], "summary": "Copies corrigées pour un sujet",
            "parameters": [{"name": "subject_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
            "responses": {"200": {"description": "Copies", "content": {"application/json": {"schema": {
                "type": "array", "items": {"$ref": "#/components/schemas/StudentPaper"}
            }}}}}
        }},
        "/api/papers/detail/{paper_id}": {"get": {
            "tags": ["Copies"], "summary": "Détail d'une copie corrigée",
            "parameters": [{"name": "paper_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
            "responses": {
                "200": {"description": "Copie avec feedback complet"},
                "404": {"$ref": "#/components/responses/NotFound"}
            }
        }},
        "/api/papers/{paper_id}/export": {"get": {
            "tags": ["Copies"],
            "summary": "Exporter une copie corrigée en PDF",
            "description": "Génère un PDF contenant le feedback complet, la note et les informations de l'étudiant. L'étudiant ne peut exporter que sa propre copie.",
            "parameters": [{"name": "paper_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
            "responses": {
                "200": {
                    "description": "Fichier PDF",
                    "content": {"application/pdf": {"schema": {"type": "string", "format": "binary"}}}
                },
                "403": {"$ref": "#/components/responses/Forbidden"},
                "404": {"$ref": "#/components/responses/NotFound"}
            }
        }},
        "/api/papers/{paper_id}/publish": {"put": {
            "tags": ["Copies"],
            "summary": "Publier / dépublier la note d'une copie à l'étudiant",
            "description": "Tant que non publiée, le prof/admin voit toujours la note (correction/gestion) mais l'étudiant reçoit score=null (symétrie avec OnlineExam.results_published).",
            "parameters": [{"name": "paper_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
            "requestBody": {"content": {"application/json": {"schema": {
                "type": "object", "properties": {"published": {"type": "boolean", "default": True}}
            }}}},
            "responses": {"200": {"description": "Statut de publication mis à jour"}, "404": {"$ref": "#/components/responses/NotFound"}}
        }},
        "/api/papers/publish-bulk": {"put": {
            "tags": ["Copies"],
            "summary": "Publier plusieurs copies d'un coup (après correction en lot)",
            "requestBody": {"required": True, "content": {"application/json": {"schema": {
                "type": "object", "required": ["paper_ids"],
                "properties": {"paper_ids": {"type": "array", "items": {"type": "integer"}}}
            }}}},
            "responses": {"200": {"description": "Copies publiées", "content": {"application/json": {"schema": {
                "type": "object", "properties": {"success": {"type": "boolean"}, "published_count": {"type": "integer"}}
            }}}}}
        }},
        "/api/statistics/{subject_id}": {"get": {
            "tags": ["Copies"], "summary": "Statistiques d'un sujet (moyenne, médiane, distribution)",
            "parameters": [{"name": "subject_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
            "responses": {
                "200": {"description": "Statistiques", "content": {"application/json": {"schema": {
                    "type": "object",
                    "properties": {
                        "subject_id":    {"type": "integer"},
                        "subject_title": {"type": "string"},
                        "totalStudents": {"type": "integer"},
                        "averageScore":  {"type": "number"},
                        "medianScore":   {"type": "number"},
                        "minScore":      {"type": "number"},
                        "maxScore":      {"type": "number"},
                        "stdDeviation":  {"type": "number"},
                        "passRate":      {"type": "number", "description": "Taux de réussite (note ≥ 10)"},
                        "scoreDistribution": {
                            "type": "object",
                            "description": "Distribution des notes par tranche",
                            "properties": {
                                "0-5":   {"type": "integer"},
                                "5-10":  {"type": "integer"},
                                "10-15": {"type": "integer"},
                                "15-20": {"type": "integer"}
                            }
                        },
                        "papers": {"type": "array", "items": {"$ref": "#/components/schemas/StudentPaper"}}
                    }
                }}}}
            }
        }},

        # ══════════════════════════════════════════════════════════════════════
        # EXAMENS EN LIGNE
        # ══════════════════════════════════════════════════════════════════════

        "/api/online_exams": {
            "get": {
                "tags": ["Examens en ligne"], "summary": "Liste des examens en ligne",
                "parameters": [
                    {"name": "status", "in": "query", "schema": {"type": "string", "enum": ["draft","active","closed","archived"]}},
                    {"name": "page",   "in": "query", "schema": {"type": "integer"}}
                ],
                "responses": {"200": {"description": "Examens", "content": {"application/json": {"schema": {
                    "type": "array", "items": {"$ref": "#/components/schemas/OnlineExam"}
                }}}}}
            },
            "post": {
                "tags": ["Examens en ligne"], "summary": "Créer un examen en ligne",
                "requestBody": {"required": True, "content": {"application/json": {"schema": {
                    "type": "object", "required": ["title","subject_id"],
                    "properties": {
                        "title":               {"type": "string", "example": "Examen Final L3"},
                        "subject_id":          {"type": "integer"},
                        "start_time":          {"type": "string", "format": "date-time"},
                        "end_time":            {"type": "string", "format": "date-time"},
                        "instructions":        {"type": "string"},
                        "max_tab_switches":    {"type": "integer", "default": 2, "description": "Nb de changements d'onglet avant exclusion"},
                        "enable_copy_paste":   {"type": "boolean", "default": False, "description": "Autoriser copier-coller"},
                        "enable_right_click":  {"type": "boolean", "default": False, "description": "Autoriser clic droit"},
                        "randomize_questions": {"type": "boolean", "default": False, "description": "Mélanger les questions"},
                        "max_no_face_count":   {"type": "integer", "default": 10, "description": "Nb de détections sans visage avant seuil"},
                        "ban_on_devtools":     {"type": "boolean", "default": True, "description": "Détecter l'ouverture des outils développeur"},
                        "auto_ban_enabled":    {"type": "boolean", "default": False, "description": "Si false (défaut), un seuil atteint (onglets/visage/devtools) envoie une alerte (agent autonome + notification) au lieu d'exclure automatiquement l'étudiant — décision manuelle requise."},
                        "enable_file_download": {"type": "boolean", "default": False, "description": "Autoriser le téléchargement des fichiers du sujet (images/vidéos/audios) — si false, bloque notamment le bouton de téléchargement natif des lecteurs vidéo/audio, indépendamment du clic droit"},
                        "enable_calculator":   {"type": "boolean", "default": False, "description": "Active une calculatrice scientifique intégrée à la page de composition (aucun appel réseau) — évite le recours à une calculatrice physique ou un téléphone, non vérifiables par le surveillant"},
                        "allow_secondary_camera": {"type": "boolean", "default": False, "description": "Autorise l'étudiant à coupler son smartphone comme caméra secondaire (angle latéral, QR code depuis la page d'examen) — voir POST /api/exam_attempts/{id}/phone_camera/pair"},
                        "require_biometric":   {"type": "boolean", "default": False, "description": "Exige une vérification d'identité par reconnaissance faciale avant l'accès à cet examen — opt-in par examen, décoché par défaut"},
                        "auto_correct":        {"type": "boolean", "default": False, "description": "Correction IA automatique dès qu'un étudiant soumet sa copie"},
                        "scheduled_correction_at": {"type": "string", "format": "date-time", "nullable": True, "description": "Heure précise (optionnelle) à laquelle corriger EN BLOC toutes les copies soumises et pas encore corrigées de cet examen — indépendant de `auto_correct`. Déclenché par l'agent autonome (voir /api/agent/due_corrections), jamais deux fois pour le même examen."}
                    }
                }}}},
                "responses": {"201": {"description": "Examen créé", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/OnlineExam"}}}}}
            }
        },
        "/api/online_exams/{exam_id}/details": {"get": {
            "tags": ["Examens en ligne"], "summary": "Détail complet d'un examen",
            "parameters": [{"name": "exam_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
            "responses": {"200": {"description": "Examen + stats + tentatives"}, "404": {"$ref": "#/components/responses/NotFound"}}
        }},
        "/api/online_exams/{exam_id}": {"delete": {
            "tags": ["Examens en ligne"], "summary": "Supprimer un examen (admin/prof propriétaire)",
            "description": "Impossible de supprimer un examen actif avec des tentatives en cours.",
            "parameters": [{"name": "exam_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
            "responses": {"200": {"description": "Supprimé"}, "400": {"description": "Examen actif avec tentatives"}}
        }},
        "/api/online_exams/{exam_id}/activate": {"post": {
            "tags": ["Examens en ligne"], "summary": "Activer un examen (le rendre accessible)",
            "parameters": [{"name": "exam_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
            "responses": {"200": {"description": "Examen activé"}, "400": {"description": "Déjà actif ou clôturé"}}
        }},
        "/api/online_exams/{exam_id}/close": {"post": {
            "tags": ["Examens en ligne"], "summary": "Clôturer un examen",
            "description": "Soumet automatiquement toutes les copies en cours.",
            "parameters": [{"name": "exam_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
            "responses": {"200": {"description": "Clôturé"}}
        }},
        "/api/online_exams/{exam_id}/start": {"post": {
            "tags": ["Examens en ligne"], "summary": "Démarrer ou reprendre une tentative (étudiant)",
            "description": (
                "Premier démarrage : `access_code` non requis. Reprise d'une tentative déjà IN_PROGRESS (l'étudiant a quitté "
                "puis revient) : `access_code` devient obligatoire. Le code est désormais **persistant et self-service** — "
                "généré automatiquement au premier besoin (valable jusqu'à `end_time` de l'examen, réutilisable à chaque "
                "reprise, plus de code à usage unique ni d'appel obligatoire à un surveillant). Il est renvoyé directement "
                "dans le champ `code` des réponses 403 `code_required`/`code_invalid` — le frontend l'affiche à l'étudiant "
                "avec un bouton copier. Chaque reprise réussie déclenche une notification (bip + toast) au surveillant/"
                "superviseur assigné via /api/exam_attempts/{attempt_id}/heartbeat et notify_user, qui peut ensuite "
                "librement décider d'appeler l'étudiant (voir /api/exam_attempts/{attempt_id}/call_request), sans que "
                "cela bloque la reprise."
            ),
            "parameters": [{"name": "exam_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
            "requestBody": {"content": {"application/json": {"schema": {
                "type": "object",
                "properties": {
                    "access_code": {"type": "string", "example": "483920", "description": "Requis uniquement pour reprendre une tentative déjà en cours ; réutilisable (non consommé)"}
                }
            }}}},
            "responses": {
                "200": {"description": "Tentative démarrée ou reprise", "content": {"application/json": {"schema": {
                    "type": "object",
                    "properties": {
                        "success":    {"type": "boolean"},
                        "attempt":    {"$ref": "#/components/schemas/ExamAttempt"},
                        "continuing": {"type": "boolean", "description": "True si une tentative en cours a été reprise"}
                    }
                }}}},
                "201": {"description": "Nouvelle tentative créée (premier démarrage)"},
                "400": {"description": "Examen non disponible (hors plage horaire, non activé) ou déjà soumis"},
                "403": {"description": "code_required=true (code manquant/invalide) — la réponse inclut `code` (le code self-service valide) pour que le frontend l'affiche directement"}
            }
        }},
        "/api/online_exams/{exam_id}/attempts": {"get": {
            "tags": ["Examens en ligne"], "summary": "Toutes les tentatives d'un examen (prof/admin)",
            "parameters": [{"name": "exam_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
            "responses": {"200": {"description": "Tentatives", "content": {"application/json": {"schema": {
                "type": "array", "items": {"$ref": "#/components/schemas/ExamAttempt"}
            }}}}}
        }},
        "/api/online_exams/{exam_id}/incidents": {"get": {
            "tags": ["Examens en ligne"],
            "summary": "Incidents et logs de surveillance d'un examen",
            "description": "Retourne tous les événements suspects (tab switch, visage absent...) avec statistiques.",
            "parameters": [{"name": "exam_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
            "responses": {
                "200": {"description": "Incidents + statistiques", "content": {"application/json": {"schema": {
                    "type": "object",
                    "properties": {
                        "incidents": {"type": "array", "items": {"$ref": "#/components/schemas/ExamIncident"}},
                        "statistics": {
                            "type": "object",
                            "properties": {
                                "total_incidents": {"type": "integer"},
                                "tab_switches":    {"type": "integer"},
                                "banned_students": {"type": "integer"}
                            }
                        }
                    }
                }}}}
            }
        }},
        "/api/exam_attempts/{attempt_id}/save": {"post": {
            "tags": ["Examens en ligne"], "summary": "Sauvegarder une réponse en cours",
            "parameters": [{"name": "attempt_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
            "requestBody": {"content": {"application/json": {"schema": {
                "type": "object",
                "properties": {"content": {"type": "string"}}
            }}}},
            "responses": {"200": {"description": "Sauvegardé"}}
        }},
        "/api/exam_attempts/{attempt_id}/submit": {"post": {
            "tags": ["Examens en ligne"], "summary": "Soumettre définitivement la copie",
            "parameters": [{"name": "attempt_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
            "requestBody": {"content": {"application/json": {"schema": {
                "type": "object",
                "properties": {"content": {"type": "string"}}
            }}}},
            "responses": {"200": {"description": "Soumis"}, "400": {"description": "Déjà soumis"}}
        }},
        "/api/exam_attempts/{attempt_id}/subject": {"get": {
            "tags": ["Examens en ligne"],
            "summary": "Récupérer le sujet d'une tentative en cours (étudiant)",
            "description": "Retourne le contenu du sujet pour l'étudiant pendant l'examen. Accessible uniquement par l'étudiant propriétaire de la tentative.",
            "parameters": [{"name": "attempt_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
            "responses": {
                "200": {"description": "Contenu du sujet", "content": {"application/json": {"schema": {
                    "type": "object",
                    "properties": {
                        "subject_title":   {"type": "string"},
                        "subject_content": {"type": "string"},
                        "duration_minutes":{"type": "integer"},
                        "saved_content":   {"type": "string", "description": "Réponse sauvegardée précédemment"}
                    }
                }}}},
                "403": {"$ref": "#/components/responses/Forbidden"}
            }
        }},
        "/api/exam_attempts/{attempt_id}/log_activity": {"post": {
            "tags": ["Examens en ligne"],
            "summary": "Logger une activité suspecte (client étudiant)",
            "description": "Appelé automatiquement par le frontend lors d'un événement suspect. Incrémente le score de risque et peut déclencher un bannissement automatique.",
            "parameters": [{"name": "attempt_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
            "requestBody": {"required": True, "content": {"application/json": {"schema": {
                "type": "object", "required": ["event_type"],
                "properties": {
                    "event_type": {
                        "type": "string",
                        "enum": ["tab_switch","devtools_attempt","no_face_detected","multiple_faces","copy_paste","fullscreen_exit","window_blur","tab_closed","gaze_away","head_turned","talking_detected","face_covered","suspect_object_detected","suspect_object_confirmed","sustained_audio_detected","multi_screen_detected","env_scan_completed","env_scan_person_detected","env_scan_unavailable"],
                        "description": "tab_switch/fullscreen_exit/window_blur/tab_closed comptent tous comme changement de contexte (tab_switches+1) | devtools_attempt +10pts | no_face_detected +10pts | multiple_faces +20pts. tab_closed est envoyé via fetch keepalive (survit à la fermeture brutale de l'onglet/navigateur, contrairement à un fetch normal qui serait annulé). Les événements gaze_away/head_turned/talking_detected/face_covered/suspect_object_detected/suspect_object_confirmed/sustained_audio_detected/multi_screen_detected et env_scan_* (scan environnement 360° avant le début de l'examen) sont envoyés en parallèle vers ce endpoint (journal d'audit, event_data en texte libre) ET vers /proctoring_event (incrémente le risk_score) — voir ce dernier pour le détail des pondérations, sauf env_scan_completed/env_scan_unavailable qui sont purement informatifs (jamais envoyés à /proctoring_event, jamais de pénalité)."
                    },
                    "event_data": {"type": "string", "description": "Données supplémentaires (JSON stringifié, optionnel)"}
                }
            }}}},
            "responses": {"200": {"description": "Activité loguée", "content": {"application/json": {"schema": {
                "type": "object",
                "properties": {
                    "success":        {"type": "boolean"},
                    "warnings_count": {"type": "integer"},
                    "tab_switches":   {"type": "integer"},
                    "no_face_count":  {"type": "integer"},
                    "banned":         {"type": "boolean"},
                    "ban_reason":     {"type": "string"}
                }
            }}}}}
        }},
        "/api/exam_attempts/{attempt_id}/heartbeat": {"post": {
            "tags": ["Examens en ligne"],
            "summary": "Signaler la présence de l'étudiant pendant l'examen (heartbeat)",
            "description": (
                "Appelé automatiquement par le frontend toutes les 20 secondes pendant que l'étudiant compose "
                "(voir enterExam() dans exam/[id]/page.tsx). Met à jour `ExamAttempt.last_seen_at`, utilisé par le "
                "dashboard surveillant pour afficher un badge « hors ligne depuis Xs » quand ce signal s'arrête "
                "(seuil 60s, voir /api/online_exams/{exam_id}/active_proctoring). "
                "Purement informatif : l'absence de heartbeat ne déclenche jamais de violation ni de pénalité sur "
                "le risk_score — une coupure réseau reste explicitement exclue du comptage de fraude."
            ),
            "parameters": [{"name": "attempt_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
            "responses": {
                "200": {"description": "Heartbeat enregistré", "content": {"application/json": {"schema": {
                    "type": "object", "properties": {"success": {"type": "boolean"}}
                }}}},
                "403": {"description": "La tentative n'appartient pas à l'utilisateur connecté"},
                "404": {"$ref": "#/components/responses/NotFound"}
            }
        }},
        "/api/exam_attempts/{attempt_id}/correct": {"post": {
            "tags": ["Examens en ligne"],
            "summary": "Corriger une copie par IA (prof/admin)",
            "description": "L'IA détecte le domaine disciplinaire et corrige selon le barème. Retourne note sur 20 et feedback détaillé.",
            "parameters": [{"name": "attempt_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
            "responses": {
                "200": {"description": "Copie corrigée", "content": {"application/json": {"schema": {
                    "type": "object",
                    "properties": {
                        "score":    {"type": "number", "example": 16.5},
                        "feedback": {"type": "string"}
                    }
                }}}}
            }
        }},
        "/api/exam_attempts/{attempt_id}/paginated": {"get": {
            "tags": ["Examens en ligne"],
            "summary": "Questions paginées et mélangées d'une tentative (étudiant)",
            "description": "Découpage en pages et ordre de mélange des questions (façon Moodle) calculés UNE FOIS côté serveur avec un mélange déterministe (seed = attempt_id) — stable pour un même étudiant à travers les rechargements de page, au lieu d'être recalculés à chaque montage du composant.",
            "parameters": [{"name": "attempt_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
            "responses": {
                "200": {"description": "Questions paginées", "content": {"application/json": {"schema": {
                    "type": "object",
                    "properties": {
                        "questions_per_page": {"type": "integer", "description": "0 = pagination désactivée (toutes les questions sur une page)"},
                        "p1_blocks": {"type": "array", "items": {"type": "object"}, "description": "Questions QCM/QCM_MULTI/VF/appariement (partie 1), mélangées si randomize_questions"},
                        "p2_items":  {"type": "array", "items": {"type": "object"}, "description": "Questions ouvertes/section/code (partie 2), jamais mélangées"},
                        "p1_pages":  {"type": "array", "items": {"type": "array", "items": {"type": "object"}}},
                        "p2_pages":  {"type": "array", "items": {"type": "array", "items": {"type": "object"}}}
                    }
                }}}},
                "404": {"$ref": "#/components/responses/NotFound"}
            }
        }},

        # ══════════════════════════════════════════════════════════════════════
        # PROCTORING
        # ══════════════════════════════════════════════════════════════════════

        "/api/online_exams/{exam_id}/active_proctoring": {"get": {
            "tags": ["Surveillant"],
            "summary": "Vue temps réel de tous les étudiants actifs (surveillant\/prof)",
            "description": "Retourne les tentatives en cours avec score de risque, incidents et statut. Les surveillants voient uniquement les étudiants qui leur sont assignés. Les professeurs voient tous les étudiants.",
            "parameters": [{"name": "exam_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
            "responses": {"200": {"description": "Étudiants actifs", "content": {"application/json": {"schema": {
                "type": "object",
                "properties": {
                    "attempts":     {"type": "array", "items": {"$ref": "#/components/schemas/ExamAttempt"}},
                    "exam_title":   {"type": "string"},
                    "active_count": {"type": "integer"}
                }
            }}}}}
        }},
        "/api/exam_attempts/{attempt_id}/proctoring_event": {"post": {
            "tags": ["Proctoring"],
            "summary": "Enregistrer un événement de surveillance (face_detector.js)",
            "description": "Appelé automatiquement par face_detector.js toutes les 2 secondes. Incrémente le score de risque.",
            "parameters": [{"name": "attempt_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
            "requestBody": {"required": True, "content": {"application/json": {"schema": {
                "type": "object", "required": ["event_type"],
                "properties": {
                    "event_type": {
                        "type": "string",
                        "enum": ["no_face_detected","no_face_low_light","multiple_faces","tab_switch","camera_disabled","fullscreen_exit","tab_closed","whisper_detected","copy_attempt","paste_attempt","cut_attempt","mouse_left_window","pattern_gaze_talk_mouth","pattern_multi_face_audio","pattern_object_gaze_away","pattern_mouth_covered_audio","pattern_head_turned_talking","pattern_whisper_gaze","face_covered","identity_mismatch_sustained","gaze_away","head_turned","talking_detected","suspect_object_detected","suspect_object_confirmed","sustained_audio_detected","multi_screen_detected","env_scan_person_detected"],
                        "description": "no_face_detected +10pts | no_face_low_light +0pts (luminosité insuffisante/excessive détectée côté client — détection faciale jugée non fiable, signal informatif seulement, ne pénalise jamais l'étudiant) | multiple_faces +20pts | tab_switch +15pts | tab_closed +10pts (fermeture d'onglet/navigateur pendant l'examen — envoyé via fetch keepalive pour survivre à la fermeture brutale de la page) | whisper_detected +6pts (seuil audio bas, distinct de sustained_audio_detected) | copy_attempt/cut_attempt +8pts, paste_attempt +15pts (journalisés qu'ils soient bloqués ou autorisés) | mouse_left_window +0pts (signal faible, informatif) | face_covered +15pts (heuristique : bas du visage non exploitable par la détection) | gaze_away/head_turned +5pts, talking_detected +8pts (MediaPipe, 4 vérifications consécutives requises) | sustained_audio_detected +10pts | multi_screen_detected +20pts | env_scan_person_detected +30pts (scan environnement 360° avant le début de l'examen) | suspect_object_detected +12pts (EfficientDet-Lite2 seul, MediaPipe — signal significatif mais sujet à confusion main/objet) | suspect_object_confirmed +28pts (EfficientDet-Lite2 ET YOLOv8n indépendamment d'accord sur la même catégorie — corroboration inter-modèles déclenchée une seule fois, sur l'image courante, quand EfficientDet atteint déjà son propre seuil ; voir cei-next/lib/yolo-detector.ts) | pattern_* : événements composites du moteur de corrélation comportementale (plusieurs signaux indépendants réunis dans une même fenêtre glissante de 10s, ex. regard détourné+parole+bouche en mouvement) — pondération +20 à +30pts, nettement plus fiable qu'un signal isolé | identity_mismatch_sustained +38pts (5 vérifications de reconnaissance faciale consécutives en échec, ~25s — correctif sécurité 27/08 : gèle la référence côté client et notifie immédiatement le surveillant, ne recapture plus jamais silencieusement ; voir POST .../identity_manual_verify pour la levée du signalement)"
                    }
                }
            }}}},
            "responses": {"200": {"description": "Événement enregistré", "content": {"application/json": {"schema": {
                "type": "object",
                "properties": {
                    "risk_score": {"type": "integer"},
                    "banned":     {"type": "boolean"}
                }
            }}}}}
        }},
        "/api/exam_attempts/{attempt_id}/camera_snapshot": {"post": {
            "tags": ["Proctoring"],
            "summary": "Envoyer un snapshot caméra (face_detector.js)",
            "description": (
                "Enregistre une photo horodatée de la caméra étudiant avec le résultat de la détection de visage.\n\n"
                "**Stockage** : l'image est uploadée vers MinIO (`S3_SNAPSHOTS_BUCKET=cei-snapshots`) "
                "sous la clé `snapshots/{exam_id}/{attempt_id}/{timestamp}.jpg`. "
                "La colonne `image_data` (base64 PostgreSQL) n'est plus utilisée pour les nouvelles entrées.\n\n"
                "**Réponse** : `stored: 's3'` si l'upload a réussi, `'none'` si `image_data` était absent."
            ),
            "parameters": [{"name": "attempt_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
            "requestBody": {"required": True, "content": {"application/json": {"schema": {
                "type": "object",
                "properties": {
                    "image_data":    {"type": "string", "description": "Image base64 JPEG (data:image/jpeg;base64,... ou brut). Uploadée vers MinIO."},
                    "event_type":    {"type": "string", "enum": ["periodic", "face_missing", "multiple_faces"], "description": "Type d'événement"},
                    "face_detected": {"type": "boolean"},
                    "faces_count":   {"type": "integer"},
                    "confidence_score": {"type": "number"}
                }
            }}}},
            "responses": {"200": {"description": "Snapshot enregistré", "content": {"application/json": {"schema": {
                "type": "object",
                "properties": {
                    "success":     {"type": "boolean"},
                    "snapshot_id": {"type": "integer"},
                    "stored":      {"type": "string", "enum": ["s3", "none"], "description": "Destination du stockage"}
                }
            }}}}}
        }},
        "/api/exam_attempts/{attempt_id}/risk_status": {"get": {
            "tags": ["Surveillant"], "summary": "Score de risque et statut de bannissement (surveillant/prof)",
            "parameters": [{"name": "attempt_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
            "responses": {"200": {"description": "Statut", "content": {"application/json": {"schema": {
                "type": "object",
                "properties": {
                    "risk_score":     {"type": "integer", "minimum": 0, "maximum": 100},
                    "warnings_count": {"type": "integer"},
                    "tab_switches":   {"type": "integer"},
                    "banned":         {"type": "boolean"},
                    "ban_reason":     {"type": "string"}
                }
            }}}}}
        }},
        "/api/exam_attempts/{attempt_id}/send_warning": {"post": {
            "tags": ["Surveillant"], "summary": "Envoyer un avertissement à un étudiant (surveillant/prof)",
            "parameters": [{"name": "attempt_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
            "requestBody": {"required": True, "content": {"application/json": {"schema": {
                "type": "object",
                "properties": {
                    "message": {"type": "string", "example": "Votre visage n'est plus visible."},
                    "type":    {"type": "string", "enum": ["warning","message","private_call","end_call"], "default": "warning"}
                }
            }}}},
            "responses": {"200": {"description": "Avertissement envoyé"}}
        }},
        "/api/exam_attempts/{attempt_id}/proctor_ban": {"post": {
            "tags": ["Surveillant"], "summary": "Exclure définitivement un étudiant (surveillant/prof)",
            "parameters": [{"name": "attempt_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
            "requestBody": {"required": True, "content": {"application/json": {"schema": {
                "type": "object", "required": ["reason"],
                "properties": {"reason": {"type": "string", "example": "Fraude avérée"}}
            }}}},
            "responses": {"200": {"description": "Étudiant exclu"}}
        }},
        "/api/exam_attempts/{attempt_id}/identity_manual_verify": {"post": {
            "tags": ["Surveillant"], "summary": "Trancher un signalement identity_mismatch_sustained (surveillant/prof)",
            "description": "Correctif sécurité 27/08 — remplace l'ancienne recapture automatique silencieuse après 5 échecs de reconnaissance faciale consécutifs. 'confirmed' débloque la reconnaissance côté étudiant (nouvelle référence recapturée explicitement) ; 'rejected' exclut directement la tentative (AttemptStatus.BANNED).",
            "parameters": [{"name": "attempt_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
            "requestBody": {"required": True, "content": {"application/json": {"schema": {
                "type": "object", "required": ["verdict"],
                "properties": {"verdict": {"type": "string", "enum": ["confirmed", "rejected"]}}
            }}}},
            "responses": {
                "200": {"description": "Verdict enregistré", "content": {"application/json": {"schema": {
                    "type": "object", "properties": {"success": {"type": "boolean"}, "banned": {"type": "boolean"}}
                }}}},
                "400": {"description": "verdict manquant ou invalide"},
                "403": {"description": "Rôle non autorisé ou étudiant non affecté à ce surveillant"}
            }
        }},
        "/api/exam_attempts/{attempt_id}/pending_messages": {"get": {
            "tags": ["Proctoring"],
            "summary": "Messages en attente pour l'étudiant (polling côté étudiant)",
            "description": "L'interface étudiant appelle cet endpoint toutes les 5 secondes pour recevoir les avertissements du surveillant.",
            "parameters": [
                {"name": "attempt_id", "in": "path", "required": True, "schema": {"type": "integer"}},
                {"name": "since", "in": "query", "schema": {"type": "string", "format": "date-time"}, "description": "ISO datetime — retourne uniquement les messages après cette date"}
            ],
            "responses": {"200": {"description": "Messages non lus", "content": {"application/json": {"schema": {
                "type": "object",
                "properties": {
                    "messages":   {"type": "array", "items": {"type": "object"}},
                    "risk_score": {"type": "integer"},
                    "banned":     {"type": "boolean"}
                }
            }}}}}
        }},
        "/api/exam_attempts/{attempt_id}/student_message": {"post": {
            "tags": ["Surveillant"], "summary": "Envoyer un message (étudiant ↔ surveillant)",
            "parameters": [{"name": "attempt_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
            "requestBody": {"required": True, "content": {"application/json": {"schema": {
                "type": "object", "required": ["content"],
                "properties": {"content": {"type": "string", "example": "J'ai une question sur l'énoncé."}}
            }}}},
            "responses": {"200": {"description": "Message envoyé"}}
        }},
        "/api/online_exams/{exam_id}/student_messages": {"get": {
            "tags": ["Surveillant"], "summary": "Messages des étudiants — vue surveillant/prof",
            "parameters": [
                {"name": "exam_id", "in": "path", "required": True, "schema": {"type": "integer"}},
                {"name": "since", "in": "query", "schema": {"type": "string", "format": "date-time"}, "description": "Retourne uniquement les messages après cette date"}
            ],
            "responses": {"200": {"description": "Messages", "content": {"application/json": {"schema": {
                "type": "object",
                "properties": {
                    "success":  {"type": "boolean"},
                    "messages": {"type": "array", "items": {
                        "type": "object",
                        "properties": {
                            "attempt_id":   {"type": "integer"},
                            "student_name": {"type": "string"},
                            "message":      {"type": "string"},
                            "timestamp":    {"type": "string", "format": "date-time"},
                            "log_id":       {"type": "integer"}
                        }
                    }}
                }
            }}}}}
        }},
        "/api/exam_attempts/{attempt_id}/livekit_token": {"get": {
            "tags": ["Proctoring"], "summary": "Token LiveKit étudiant (publier flux vidéo)",
            "parameters": [{"name": "attempt_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
            "responses": {"200": {"description": "Token LiveKit", "content": {"application/json": {"schema": {
                "type": "object",
                "properties": {
                    "token":       {"type": "string"},
                    "room_name":   {"type": "string"},
                    "livekit_url": {"type": "string"}
                }
            }}}}}
        }},
        "/api/exam_attempts/{attempt_id}/phone_camera/pair": {"post": {
            "tags": ["Proctoring"], "summary": "Génère un code de couplage pour la caméra secondaire (smartphone)",
            "description": "Étudiant, tentative en cours, examen avec `allow_secondary_camera` activé. Retourne un code à 6 chiffres (5 min) et une URL `/phone-camera?code=...` à afficher en QR — le téléphone échange ce code contre un token LiveKit via `POST /api/phone_camera/token` (endpoint public, le téléphone n'a pas de session CEI).",
            "parameters": [{"name": "attempt_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
            "responses": {
                "200": {"description": "Code généré", "content": {"application/json": {"schema": {
                    "type": "object", "properties": {"code": {"type": "string", "example": "350064"}, "url": {"type": "string"}, "expires_in": {"type": "integer", "example": 300}}
                }}}},
                "403": {"description": "Caméra secondaire non activée pour cet examen"}
            }
        }},
        "/api/exam_attempts/{attempt_id}/phone_camera/status": {"get": {
            "tags": ["Proctoring"], "summary": "Le téléphone s'est-il couplé avec succès ?",
            "description": "Pollé par la page d'examen pendant l'affichage du QR code, pour afficher une confirmation dès que le téléphone a rejoint la salle.",
            "parameters": [{"name": "attempt_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
            "responses": {"200": {"description": "Statut", "content": {"application/json": {"schema": {"type": "object", "properties": {"linked": {"type": "boolean"}}}}}}}
        }},
        "/api/phone_camera/token": {"post": {
            "tags": ["Proctoring"], "summary": "Échange un code de couplage contre un token LiveKit (téléphone, PUBLIC)",
            "description": "Aucune authentification CEI — appelé depuis la page mobile ouverte en scannant le QR code. Code à usage unique (consommé dès l'appel), identité LiveKit `student-{user_id}-phone` dans la même salle que la caméra principale (`exam-{exam_id}`), publish-only (`canSubscribe: false`).",
            "security": [],
            "requestBody": {"required": True, "content": {"application/json": {"schema": {
                "type": "object", "required": ["code"], "properties": {"code": {"type": "string", "example": "350064"}}
            }}}},
            "responses": {
                "200": {"description": "Token LiveKit publish-only", "content": {"application/json": {"schema": {
                    "type": "object", "properties": {"token": {"type": "string"}, "ws_url": {"type": "string"}, "room": {"type": "string"}, "exam_title": {"type": "string"}}
                }}}},
                "404": {"description": "Code invalide, expiré ou déjà utilisé"}
            }
        }},
        "/api/exam_attempts/{attempt_id}/private_token": {"get": {
            "tags": ["Surveillant"],
            "summary": "Token LiveKit pour appel privé (étudiant ↔ surveillant/professeur/superviseur/admin)",
            "description": "Room LiveKit `private-{attempt_id}`, partagée par les deux sens d'appel : surveillant→étudiant pendant l'examen, ET étudiant→surveillant/superviseur/professeur depuis le tableau de bord (bouton optionnel « Besoin d'aide ? » — le code de reprise lui-même n'exige plus cet appel, voir /online_exams/{exam_id}/start). Rôles autorisés : student (uniquement le sien), professor, admin, surveillant, superviseur.",
            "parameters": [{"name": "attempt_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
            "responses": {"200": {"description": "Token appel privé"}}
        }},
        "/api/exam_attempts/{attempt_id}/call_request": {"post": {
            "tags": ["Surveillant"],
            "summary": "Étudiant demande un appel d'assistance (optionnel, hors page d'examen)",
            "description": "Notifie, par ordre de priorité, le(s) surveillant(s) assigné(s) à cet étudiant, sinon le(s) superviseur(s) du groupe couvrant l'EC de l'examen, sinon le professeur créateur de l'examen — jamais plusieurs niveaux à la fois. Bouton « Besoin d'aide ? » optionnel côté étudiant : depuis le retour du code de reprise self-service (voir /online_exams/{exam_id}/start), cet appel n'est plus une étape obligatoire pour reprendre l'examen.",
            "parameters": [{"name": "attempt_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
            "responses": {"200": {"description": "Notification envoyée", "content": {"application/json": {"schema": {
                "type": "object", "properties": {"success": {"type": "boolean"}, "notified": {"type": "integer", "description": "Nombre de destinataires notifiés"}}
            }}}}, "400": {"description": "Tentative non IN_PROGRESS"}}
        }},
        "/api/exam_attempts/{attempt_id}/access_code": {"post": {
            "tags": ["Surveillant"],
            "summary": "[Legacy/optionnel] Régénère manuellement le code de reprise d'une tentative",
            "description": (
                "Depuis le passage au code self-service (voir /online_exams/{exam_id}/start), ce code est normalement "
                "émis automatiquement au premier besoin et reste valable jusqu'à la fin de l'examen — l'étudiant n'a "
                "plus besoin qu'un surveillant/superviseur/professeur passe par cet endpoint. Il reste disponible pour "
                "un usage manuel ponctuel (ex. régénérer un code perdu/compromis) : un seul rôle habilité à la fois par "
                "tentative, par ordre de priorité : surveillant assigné → superviseur du groupe couvrant l'EC (si aucun "
                "surveillant assigné) → professeur créateur (en dernier repli) → admin toujours. Invalide tout code "
                "existant pour cette tentative. Code à 6 chiffres, valable 10 minutes (plus court que le code "
                "self-service normal, par précaution puisqu'il est communiqué manuellement)."
            ),
            "parameters": [{"name": "attempt_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
            "responses": {"200": {"description": "Code généré", "content": {"application/json": {"schema": {
                "type": "object", "properties": {
                    "success": {"type": "boolean"}, "code": {"type": "string", "example": "483920"},
                    "expires_at": {"type": "string", "format": "date-time"}, "generated_by_name": {"type": "string"}
                }
            }}}}, "403": {"description": "Rôle non habilité pour cette tentative (voir ordre de priorité ci-dessus)"}}
        }},
        "/api/online_exams/{exam_id}/proctor_token": {"get": {
            "tags": ["Surveillant"], "summary": "Token LiveKit surveillant — accès à tous les flux vidéo",
            "parameters": [{"name": "exam_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
            "responses": {"200": {"description": "Token surveillant"}}
        }},
        "/api/online_exams/{exam_id}/proctors": {
            "get": {
                "tags": ["Surveillant"], "summary": "Surveillants affectés à un examen",
                "parameters": [{"name": "exam_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
                "responses": {"200": {"description": "Surveillants", "content": {"application/json": {"schema": {
                    "type": "object",
                    "properties": {
                        "success":             {"type": "boolean"},
                        "proctors":            {"type": "array", "items": {
                            "type": "object",
                            "properties": {
                                "id":            {"type": "integer"},
                                "proctor_id":    {"type": "integer"},
                                "proctor_name":  {"type": "string"},
                                "student_count": {"type": "integer"}
                            }
                        }},
                        "total_students":      {"type": "integer"},
                        "unassigned_students": {"type": "integer"}
                    }
                }}}}}
            },
            "post": {
                "tags": ["Surveillant"], "summary": "Affecter un surveillant à un examen (prof\/admin)",
                "parameters": [{"name": "exam_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
                "requestBody": {"required": True, "content": {"application/json": {"schema": {
                    "type": "object", "required": ["proctor_id"],
                    "properties": {"proctor_id": {"type": "integer"}}
                }}}},
                "responses": {"201": {"description": "Affecté"}}
            }
        },
        "/api/online_exams/{exam_id}/proctors/{proctor_id}": {"delete": {
            "tags": ["Surveillant"], "summary": "Retirer un surveillant d'un examen (prof\/admin)",
            "parameters": [
                {"name": "exam_id",    "in": "path", "required": True, "schema": {"type": "integer"}},
                {"name": "proctor_id", "in": "path", "required": True, "schema": {"type": "integer"}}
            ],
            "responses": {"200": {"description": "Surveillant retiré"}}
        }},
        "/api/online_exams/{exam_id}/distribute_proctors": {"post": {
            "tags": ["Surveillant"],
            "summary": "Distribuer automatiquement les étudiants entre les surveillants",
            "description": "Répartit équitablement les étudiants actifs entre les surveillants affectés. Peut être relancé pour redistribuer.",
            "parameters": [{"name": "exam_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
            "responses": {"200": {"description": "Distribution effectuée", "content": {"application/json": {"schema": {
                "type": "object",
                "properties": {
                    "success":        {"type": "boolean"},
                    "total_students": {"type": "integer"},
                    "total_proctors": {"type": "integer"},
                    "mode":           {"type": "string", "enum": ["auto","manual"], "description": "Mode de distribution"},
                    "message":        {"type": "string"},
                    "distribution":   {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "proctor_id":    {"type": "integer"},
                                "proctor_name":  {"type": "string"},
                                "student_count": {"type": "integer"}
                            }
                        }
                    }
                }
            }}}}}
        }},
        "/api/online_exams/{exam_id}/proctor_heartbeat": {"post": {
            "tags": ["Surveillant"],
            "summary": "Signaler la présence en direct d'un surveillant (heartbeat + signaux de vigilance)",
            "description": (
                "Appelé périodiquement (ex. toutes les 30s) par la page de monitoring tant qu'elle reste ouverte. Sert aussi de "
                "déclencheur : si un AUTRE surveillant de cet examen n'a plus émis de heartbeat depuis 90s, ses étudiants sont "
                "automatiquement redistribués aux surveillants encore en ligne (Notes point 11) — ce comportement se base "
                "UNIQUEMENT sur l'appel lui-même, jamais sur les signaux `interacting`/`viewed_student`/`face_present` ci-dessous, "
                "qui n'alimentent que l'affichage du superviseur (voir /api/superviseur/dashboard) et ne doivent jamais faire "
                "perdre ses étudiants à un surveillant réellement présent."
            ),
            "parameters": [{"name": "exam_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
            "requestBody": {"content": {"application/json": {"schema": {
                "type": "object",
                "properties": {
                    "interacting":     {"type": "boolean", "description": "Interaction souris/clavier récente + onglet visible au premier plan (Tier A)"},
                    "viewed_student":  {"type": "boolean", "description": "A consulté un élément lié à un étudiant précis récemment (Tier B)"},
                    "face_present":    {"type": "boolean", "description": "Vérification caméra du surveillant positive (Tier C uniquement — jamais d'image transmise)"}
                }
            }}}},
            "responses": {"200": {"description": "Heartbeat enregistré", "content": {"application/json": {"schema": {
                "type": "object", "properties": {
                    "success": {"type": "boolean"},
                    "vigilance_level": {"type": "string", "enum": ["A", "B", "C"], "description": "Niveau exigé par le groupe couvrant cet examen — indique au client quels signaux envoyer"}
                }
            }}}}}
        }},
        "/api/surveillant/exams": {"get": {
            "tags": ["Surveillant"], "summary": "Examens assignés au surveillant connecté",
            "responses": {"200": {"description": "Examens du surveillant", "content": {"application/json": {"schema": {
                "type": "array", "items": {"$ref": "#/components/schemas/OnlineExam"}
            }}}}}
        }},
        "/api/superviseur/dashboard": {"get": {
            "tags": ["Superviseur"], "summary": "Tableau de bord — groupes supervisés et statut de chaque surveillant",
            "description": "Pour chaque groupe où l'utilisateur connecté figure parmi les superviseurs rattachés, liste ses membres avec un statut à 3 états calculé à partir du heartbeat ET des signaux de vigilance (voir /api/online_exams/{exam_id}/proctor_heartbeat) : engaged (actif et engagé selon le niveau A/B/C du groupe), idle (onglet ouvert mais signal(x) requis manquant(s)), disconnected (heartbeat expiré).",
            "responses": {"200": {"description": "Groupes + statuts", "content": {"application/json": {"schema": {
                "type": "object", "properties": {
                    "groups": {"type": "array", "items": {"type": "object", "properties": {
                        "id": {"type": "integer"}, "name": {"type": "string"}, "vigilance_level": {"type": "string", "enum": ["A", "B", "C"]},
                        "members": {"type": "array", "items": {"type": "object", "properties": {
                            "id": {"type": "integer"}, "full_name": {"type": "string"}, "email": {"type": "string"},
                            "status": {"type": "string", "enum": ["engaged", "idle", "disconnected"]},
                            "is_active_now": {"type": "boolean", "description": "Rétrocompatibilité — true pour engaged ET idle"},
                            "monitoring_exam_id": {"type": "integer", "nullable": True}
                        }}}
                    }}},
                    "total_groups": {"type": "integer"}, "total_surveillants": {"type": "integer"}, "active_surveillants": {"type": "integer", "description": "Compte uniquement les 'engaged', pas les 'idle'"}
                }
            }}}}, "403": {"description": "Réservé aux superviseurs"}}
        }},
        "/api/superviseur/call_requests": {"get": {
            "tags": ["Superviseur"], "summary": "Demandes d'appel étudiant en attente (reprise après déconnexion)",
            "description": "Ne retourne que les demandes pour lesquelles AUCUN surveillant n'est assigné à l'étudiant (sinon c'est au surveillant assigné de répondre — voir la règle d'autorité unique dans /api/exam_attempts/{attempt_id}/access_code), et uniquement pour les groupes dont l'EC couvert correspond à un groupe supervisé par l'utilisateur connecté.",
            "responses": {"200": {"description": "Demandes en attente", "content": {"application/json": {"schema": {
                "type": "object", "properties": {"requests": {"type": "array", "items": {"type": "object", "properties": {
                    "attempt_id": {"type": "integer"}, "exam_id": {"type": "integer"}, "exam_title": {"type": "string"},
                    "student_name": {"type": "string"}, "timestamp": {"type": "string", "format": "date-time"}
                }}}}
            }}}}, "403": {"description": "Réservé aux superviseurs"}}
        }},
        "/api/superviseur/proctor_call/{proctor_id}/token": {"get": {
            "tags": ["Superviseur"],
            "summary": "Token LiveKit pour l'appel privé superviseur ↔ surveillant",
            "description": "Autorisé pour le superviseur qui supervise réellement ce surveillant, et pour le surveillant lui-même (pour répondre).",
            "parameters": [{"name": "proctor_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
            "responses": {
                "200": {"description": "Token LiveKit", "content": {"application/json": {"schema": {
                    "type": "object", "properties": {
                        "token": {"type": "string"}, "ws_url": {"type": "string"},
                        "room": {"type": "string"}, "identity": {"type": "string"}
                    }
                }}}},
                "403": {"description": "Ne supervise pas ce surveillant"},
                "503": {"description": "LiveKit non configuré"}
            }
        }},
        "/api/superviseur/proctor_call/{proctor_id}/request": {"post": {
            "tags": ["Superviseur"],
            "summary": "Demander un appel à un surveillant supervisé",
            "description": "Notifie le surveillant en temps réel où qu'il se trouve dans l'application, même mécanisme que la demande d'appel étudiant → surveillant.",
            "parameters": [{"name": "proctor_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
            "responses": {"200": {"description": "Demande envoyée"}, "403": {"description": "Ne supervise pas ce surveillant"}}
        }},
        "/api/exam_attempts/{attempt_id}/recording": {"post": {
            "tags": ["Proctoring"],
            "summary": "Démarrer ou arrêter l'enregistrement vidéo individuel (LiveKit → MinIO)",
            "parameters": [{"name": "attempt_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
            "requestBody": {"required": True, "content": {"application/json": {"schema": {
                "type": "object", "required": ["action"],
                "properties": {
                    "action":    {"type": "string", "enum": ["start","stop"], "description": "Démarrer ou arrêter l'enregistrement"},
                    "egress_id": {"type": "string", "description": "Requis si action=stop — ID LiveKit Egress retourné au démarrage"}
                }
            }}}},
            "responses": {"200": {"description": "OK", "content": {"application/json": {"schema": {
                "type": "object",
                "properties": {
                    "success":   {"type": "boolean"},
                    "egress_id": {"type": "string", "description": "ID de l'Egress (action=start)"},
                    "filepath":  {"type": "string", "description": "Chemin MinIO (action=stop)"}
                }
            }}}}}
        }},
        "/api/online_exams/{exam_id}/room_recording": {"post": {
            "tags": ["Proctoring"],
            "summary": "Démarrer ou arrêter l'enregistrement de la salle entière",
            "parameters": [{"name": "exam_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
            "requestBody": {"required": True, "content": {"application/json": {"schema": {
                "type": "object", "required": ["action"],
                "properties": {
                    "action":    {"type": "string", "enum": ["start","stop"]},
                    "egress_id": {"type": "string", "description": "Requis si action=stop"}
                }
            }}}},
            "responses": {"200": {"description": "OK", "content": {"application/json": {"schema": {
                "type": "object",
                "properties": {
                    "success":   {"type": "boolean"},
                    "egress_id": {"type": "string"},
                    "filepath":  {"type": "string"}
                }
            }}}}}
        }},
        "/api/online_exams/{exam_id}/group_recording": {"post": {
            "tags": ["Proctoring"],
            "summary": "Démarrer ou arrêter l'enregistrement du groupe du surveillant",
            "description": "Enregistre uniquement le groupe d'étudiants assigné au surveillant connecté.",
            "parameters": [{"name": "exam_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
            "requestBody": {"required": True, "content": {"application/json": {"schema": {
                "type": "object", "required": ["action"],
                "properties": {
                    "action":     {"type": "string", "enum": ["start","stop"]},
                    "egress_ids": {"type": "array", "items": {"type": "string"}, "description": "IDs Egress à arrêter (action=stop)"}
                }
            }}}},
            "responses": {"200": {"description": "OK", "content": {"application/json": {"schema": {
                "type": "object",
                "properties": {
                    "success":  {"type": "boolean"},
                    "started":  {"type": "integer", "description": "Nb d'enregistrements démarrés"},
                    "stopped":  {"type": "integer", "description": "Nb d'enregistrements arrêtés"},
                    "errors":   {"type": "array", "items": {"type": "string"}}
                }
            }}}}}
        }},
        "/api/online_exams/{exam_id}/recordings": {"get": {
            "tags": ["Proctoring"],
            "summary": "Snapshots caméra et enregistrements par étudiant",
            "description": "Retourne pour chaque étudiant ses snapshots caméra avec métadonnées de détection visage.",
            "parameters": [{"name": "exam_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
            "responses": {"200": {"description": "Données d'enregistrement", "content": {"application/json": {"schema": {
                "type": "object",
                "properties": {
                    "exam_id": {"type": "integer"},
                    "students": {"type": "array", "items": {
                        "type": "object",
                        "properties": {
                            "attempt_id":      {"type": "integer"},
                            "student_name":    {"type": "string"},
                            "student_email":   {"type": "string"},
                            "status":          {"type": "string"},
                            "snapshots_count": {"type": "integer"},
                            "snapshots": {"type": "array", "items": {
                                "type": "object",
                                "properties": {
                                    "id":            {"type": "integer"},
                                    "timestamp":     {"type": "string", "format": "date-time"},
                                    "event_type":    {"type": "string"},
                                    "image_data":    {"type": "string", "description": "Base64 (peut être null)"},
                                    "face_detected": {"type": "boolean"},
                                    "frame_analysis": {"type": "string", "nullable": True, "description": "JSON string {brightness, width, height} — luminosité 0-255 échantillonnée sur la frame ; <40 ou >235 indique un éclairage rendant la détection faciale peu fiable"}
                                }
                            }}
                        }
                    }}
                }
            }}}}}
        }},
        "/api/online_exams/{exam_id}/video_recordings": {"get": {
            "tags": ["Proctoring"], "summary": "Enregistrements vidéo stockés dans MinIO",
            "description": "Retourne les URLs pré-signées des vidéos stockées dans S3/MinIO.",
            "parameters": [{"name": "exam_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
            "responses": {"200": {"description": "Vidéos avec URLs pré-signées"}}
        }},
        "/api/proctoring/snapshot_local/{key}": {"get": {
            "tags": ["Proctoring"],
            "summary": "Servir un snapshot caméra depuis le fallback disque local",
            "description": "Utilisé quand MinIO était indisponible au moment de la capture (le snapshot a été écrit sur disque local à la place). Réservé aux professeur/admin/surveillant affectés à l'examen concerné.",
            "parameters": [{
                "name": "key", "in": "path", "required": True, "schema": {"type": "string"},
                "description": "Clé au format `snapshots_fallback/{exam_id}/{attempt_id}/{timestamp}.jpg` — contient des `/`, capturée via le convertisseur Flask `<path:key>`."
            }],
            "responses": {
                "200": {"description": "Image JPEG", "content": {"image/jpeg": {"schema": {"type": "string", "format": "binary"}}}},
                "400": {"description": "Clé de snapshot invalide"},
                "403": {"$ref": "#/components/responses/Forbidden"},
                "404": {"$ref": "#/components/responses/NotFound"}
            }
        }},

        # ══════════════════════════════════════════════════════════════════════
        # ══════════════════════════════════════════════════════════════════════
        # AGENT AUTONOME
        # ══════════════════════════════════════════════════════════════════════

        "/api/agent/status": {"get": {
            "tags": ["Agent autonome"],
            "summary": "Statut de l'agent autonome de surveillance",
            "description": (
                "Retourne l'état en temps réel de l'agent `cei-agent-proctor` basé sur le fichier heartbeat "
                "qu'il écrit toutes les 30 secondes.\n\n"
                "**Logique de détection :**\n"
                "- `alive=true` si le dernier heartbeat date de moins de 3× l'intervalle (90s par défaut)\n"
                "- `status=active` → agent opérationnel\n"
                "- `status=stale` → heartbeat trop ancien (agent bloqué ?)\n"
                "- `status=offline` → fichier heartbeat absent (service PM2 non démarré)\n\n"
                "Passer `?exam_id=N` pour obtenir les statistiques de cet examen spécifique "
                "(nb d'étudiants surveillés, alertes envoyées, exclusions)."
            ),
            "parameters": [
                {
                    "name": "exam_id", "in": "query",
                    "schema": {"type": "integer"},
                    "description": "Optionnel — ID de l'examen pour les stats spécifiques"
                }
            ],
            "responses": {
                "200": {
                    "description": "Statut de l'agent",
                    "content": {"application/json": {"schema": {
                        "type": "object",
                        "properties": {
                            "alive":                {"type": "boolean", "description": "True si l'agent répond dans les délais"},
                            "status":               {"type": "string", "enum": ["active","stale","offline"]},
                            "status_label":         {"type": "string", "example": "Agent actif — Surveillance IA en cours"},
                            "status_color":         {"type": "string", "example": "#10b981", "description": "Couleur CSS pour l'indicateur visuel"},
                            "last_check":           {"type": "string", "format": "date-time"},
                            "last_check_ago_sec":   {"type": "integer", "description": "Secondes depuis le dernier heartbeat"},
                            "interval_seconds":     {"type": "integer", "example": 30},
                            "risk_alert":           {"type": "integer", "example": 60, "description": "Seuil score de risque pour alerte email"},
                            "risk_urgent":          {"type": "integer", "example": 80, "description": "Seuil score de risque pour alerte urgente"},
                            "exams_monitored":      {"type": "integer", "description": "Nombre d'examens actifs lors du dernier cycle"},
                            "total_alerts_session": {"type": "integer", "description": "Total d'alertes envoyées depuis le démarrage"},
                            "exam": {
                                "type": "object",
                                "description": "Stats pour l'exam_id demandé (si fourni)",
                                "properties": {
                                    "exam_id":     {"type": "integer"},
                                    "students":    {"type": "integer", "description": "Nb d'étudiants surveillés"},
                                    "alerts_sent": {"type": "integer", "description": "Alertes envoyées pour cet examen"},
                                    "banned":      {"type": "integer", "description": "Étudiants exclus"}
                                }
                            }
                        }
                    }}}
                },
                "403": {"$ref": "#/components/responses/Forbidden"}
            }
        }},
        "/api/agent/due_corrections": {"get": {
            "tags": ["Agent autonome"],
            "summary": "Liste les examens dont la correction planifiée est due",
            "description": (
                "Retourne les examens dont `scheduled_correction_at` est passé et dont "
                "`correction_triggered_at` est encore NULL (jamais traités). Interrogé par l'agent "
                "autonome toutes les 30s (voir agent_proctor/monitor.py) pour déclencher la correction en "
                "bloc — voir /api/agent/run_scheduled_correction/{exam_id}. Requiert `X-Agent-Secret`. "
                "**Inaccessible via JWT.**"
            ),
            "responses": {
                "200": {"description": "Examens dus", "content": {"application/json": {"schema": {
                    "type": "object",
                    "properties": {"exams": {"type": "array", "items": {"type": "object", "properties": {
                        "id": {"type": "integer"}, "title": {"type": "string"}
                    }}}}
                }}}},
                "403": {"description": "Header X-Agent-Secret absent ou incorrect — inaccessible via JWT"}
            }
        }},
        "/api/agent/run_scheduled_correction/{exam_id}": {"post": {
            "tags": ["Agent autonome"],
            "summary": "Déclenche la correction planifiée en bloc d'un examen",
            "description": (
                "Corrige par IA toutes les tentatives soumises et pas encore notées de l'examen. Marque "
                "`correction_triggered_at` AVANT de corriger (pas après) pour qu'un appel en double "
                "(redémarrage de l'agent, chevauchement de cycles) ne déclenche jamais deux fois la même "
                "correction — un second appel renvoie `already_triggered: true` sans rien refaire. Une "
                "tentative en échec (ex. réponses vides) n'interrompt pas la correction des autres. Notifie "
                "le créateur de l'examen une fois terminé. Requiert `X-Agent-Secret`. **Inaccessible via JWT.**"
            ),
            "parameters": [{"name": "exam_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
            "responses": {
                "200": {"description": "Correction en bloc terminée (ou déjà déclenchée)", "content": {"application/json": {"schema": {
                    "type": "object",
                    "properties": {
                        "success":           {"type": "boolean"},
                        "corrected":         {"type": "integer", "description": "Nombre de copies corrigées avec succès"},
                        "failed":            {"type": "integer", "description": "Nombre de copies en échec (ex. aucune réponse)"},
                        "already_triggered": {"type": "boolean", "description": "True si cet examen avait déjà été traité — aucune action effectuée"}
                    }
                }}}},
                "403": {"description": "Header X-Agent-Secret absent ou incorrect — inaccessible via JWT"},
                "404": {"$ref": "#/components/responses/NotFound"}
            }
        }},
        "/api/agent/claim_lock": {"post": {
            "tags": ["Agent autonome"],
            "summary": "Réclame un verrou partagé (coordination entre plusieurs instances de l'agent)",
            "description": (
                "Plusieurs instances de l'agent autonome peuvent tourner en parallèle sur des serveurs "
                "différents (résilience — si l'une tombe, l'autre continue de surveiller la plateforme). "
                "Cet endpoint réclame atomiquement (Redis SETNX) une clé arbitraire pendant `ttl_seconds` : "
                "`claimed: true` pour la PREMIÈRE instance à la réclamer, `claimed: false` pour toute autre "
                "instance qui tente la même clé avant expiration. Utilisé en interne par l'agent pour éviter "
                "les alertes/emails en double (clé `alert:{attempt_id}`, ttl = cooldown d'alerte) et les "
                "rapports enseignant en double (clé `summary:{exam_id}`, ttl = intervalle de rapport). En cas "
                "de panne Redis, retourne `claimed: true` par défaut (un doublon occasionnel est préférable à "
                "un silence total). Requiert `X-Agent-Secret`. **Inaccessible via JWT.**"
            ),
            "requestBody": {"required": True, "content": {"application/json": {"schema": {
                "type": "object", "required": ["key"],
                "properties": {
                    "key":         {"type": "string", "example": "alert:1234", "description": "Identifiant arbitraire du verrou"},
                    "ttl_seconds": {"type": "integer", "default": 600, "minimum": 1, "maximum": 86400, "description": "Durée du verrou"}
                }
            }}}},
            "responses": {
                "200": {"description": "Résultat de la tentative de réclamation", "content": {"application/json": {"schema": {
                    "type": "object", "properties": {"claimed": {"type": "boolean"}}
                }}}},
                "400": {"description": "key manquant"},
                "403": {"description": "Header X-Agent-Secret absent ou incorrect — inaccessible via JWT"}
            }
        }},

        "/api/agent/alerts": {
            "post": {
                "tags": ["Agent autonome"],
                "summary": "Pousser une alerte — SERVICE AGENT UNIQUEMENT",
                "description": (
                    "**Endpoint interne** — réservé au service `cei-agent-proctor` (PM2).\n\n"
                    "Requiert le header `X-Agent-Secret` avec la valeur de `AGENT_SECRET_KEY` "
                    "(configurée dans le `.env` du serveur). **Inaccessible via JWT.**\n\n"
                    "Ne pas appeler depuis le frontend."
                ),
                "security": [{"AgentSecret": []}],
                "requestBody": {"required": True, "content": {"application/json": {"schema": {"$ref": "#/components/schemas/AgentAlert"}}}},
                "responses": {
                    "200": {"description": "Alerte enregistrée"},
                    "403": {"description": "Header X-Agent-Secret absent ou incorrect"}
                }
            },
            "get": {
                "tags": ["Agent autonome"], "summary": "Alertes non lues (dashboard surveillant/prof)",
                "description": (
                    "Retourne les 50 dernières alertes non lues. Requiert un JWT (rôle admin/prof/surveillant).\n\n"
                    "**Stockage** : Les alertes sont persistées dans une **Redis List** (`cei:agent:alerts`) "
                    "avec un maximum de 200 entrées. Les attempt_ids lus sont conservés dans un **Redis Set** "
                    "(`cei:agent:alerts:read`). Plus de fichier `agent_alerts.json` — stockage multi-serveur prêt.\n\n"
                    "**Push temps réel** : à chaque nouvelle alerte, le bus `notif_bus.py` publie sur "
                    "`cei:notif:exam:{id}` (long-polling navigateur) ET sur ntfy topic `exam-{id}` (push mobile)."
                ),
                "responses": {"200": {"description": "Alertes", "content": {"application/json": {"schema": {
                    "type": "object",
                    "properties": {
                        "alerts":       {"type": "array", "items": {"$ref": "#/components/schemas/AgentAlert"}},
                        "total_unread": {"type": "integer"}
                    }
                }}}}}
            }
        },
        "/api/agent/alerts/read": {"post": {
            "tags": ["Agent autonome"], "summary": "Marquer des alertes comme lues",
            "requestBody": {"required": True, "content": {"application/json": {"schema": {
                "type": "object",
                "properties": {"attempt_ids": {"type": "array", "items": {"type": "integer"}}}
            }}}},
            "responses": {"200": {"description": "Alertes marquées lues"}}
        }},
        "/api/agent/active_exams": {"get": {
            "tags": ["Agent autonome"],
            "summary": "Examens actifs — SERVICE AGENT UNIQUEMENT",
            "description": (
                "**Endpoint interne** — réservé au service `cei-agent-proctor` (PM2).\n\n"
                "Requiert le header `X-Agent-Secret` avec la valeur de `AGENT_SECRET_KEY`. "
                "**Inaccessible via JWT.** Ne pas appeler depuis le frontend.\n\n"
                "Pour tester dans Swagger : cliquer **Authorize** → onglet **AgentSecret** → "
                "saisir la valeur de `AGENT_SECRET_KEY` du `.env` serveur."
            ),
            "security": [{"AgentSecret": []}],
            "responses": {
                "200": {"description": "Examens actifs", "content": {"application/json": {"schema": {
                    "type": "object",
                    "properties": {"exams": {"type": "array", "items": {
                        "type": "object",
                        "properties": {"id": {"type": "integer"}, "title": {"type": "string"}}
                    }}}
                }}}},
                "403": {"description": "Header X-Agent-Secret absent ou incorrect — inaccessible via JWT"}
            }
        }},
        "/api/agent/exam_proctoring/{exam_id}": {"get": {
            "tags": ["Agent autonome"],
            "summary": "Données de surveillance complètes — SERVICE AGENT UNIQUEMENT",
            "description": (
                "**Endpoint interne** — réservé au service `cei-agent-proctor`.\n\n"
                "Retourne tentatives + emails des surveillants + email de l'enseignant.\n\n"
                "Requiert `X-Agent-Secret`. **Inaccessible via JWT.**"
            ),
            "security": [{"AgentSecret": []}],
            "parameters": [{"name": "exam_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
            "responses": {
                "200": {"description": "Données de surveillance", "content": {"application/json": {"schema": {
                    "type": "object",
                    "properties": {
                        "exam_id":        {"type": "integer"},
                        "title":          {"type": "string"},
                        "teacher_email":  {"type": "string"},
                        "proctor_emails": {"type": "array", "items": {"type": "string"}},
                        "attempts":       {"type": "array", "items": {"$ref": "#/components/schemas/ExamAttempt"}}
                    }
                }}}},
                "403": {"description": "Header X-Agent-Secret absent ou incorrect — inaccessible via JWT"}
            }
        }},

        # ══════════════════════════════════════════════════════════════════════
        # INTELLIGENCE ARTIFICIELLE
        # ══════════════════════════════════════════════════════════════════════

        "/api/ai/generate-exam-suggestions": {"post": {
            "tags": ["Intelligence Artificielle"],
            "summary": "Générer des suggestions d'examens depuis un ou plusieurs cours",
            "description": (
                "Upload d'un ou plusieurs cours (PDF/DOCX/TXT) via le champ répété `course_files`. Chaque fichier est "
                "extrait séparément puis concaténé avec un séparateur `--- Fichier: <nom> ---` avant analyse — utile "
                "pour un support réparti en plusieurs documents (poly + TD + annales). Taille cumulée max 50 Mo. "
                "L'IA détecte la discipline, analyse le contenu et génère 3 suggestions adaptées. Le domaine détecté "
                "est transmis pour la génération complète. "
                "`difficulty` et `duration` sont des contraintes de l'enseignant, jamais laissées à la discrétion de "
                "l'IA — les 3 suggestions renvoyées ont TOUJOURS exactement ces valeurs (écrasées côté serveur même "
                "si le modèle en propose d'autres)."
            ),
            "requestBody": {"required": True, "content": {"multipart/form-data": {"schema": {
                "type": "object", "required": ["course_files"],
                "properties": {
                    "course_files":  {"type": "array", "items": {"type": "string", "format": "binary"}, "description": "Un ou plusieurs fichiers de cours PDF/DOCX/TXT (champ répété), 50 Mo cumulés max"},
                    "difficulty":    {"type": "string", "enum": ["Facile","Moyen","Difficile"], "default": "Moyen"},
                    "student_level": {"type": "string", "example": "Licence 3"},
                    "exam_type":     {"type": "string", "example": "QCM"},
                    "duration":      {"type": "integer", "default": 90, "minimum": 15, "maximum": 480, "description": "Durée d'examen souhaitée (minutes), fixée par l'enseignant — pas générée par l'IA"}
                }
            }}}},
            "responses": {"200": {"description": "Suggestions générées", "content": {"application/json": {"schema": {
                "type": "object",
                "properties": {
                    "course_summary":  {"type": "string"},
                    "detected_domain": {"type": "string", "example": "Réseaux informatiques"},
                    "main_topics":     {"type": "array", "items": {"type": "string"}},
                    "suggestions": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "title":              {"type": "string"},
                                "description":        {"type": "string"},
                                "exam_type":          {"type": "string"},
                                "duration":           {"type": "integer"},
                                "difficulty":         {"type": "string"},
                                "key_points":         {"type": "array", "items": {"type": "string"}},
                                "questions_examples": {"type": "array", "items": {"type": "string"}},
                                "grading_criteria":   {"type": "string"},
                                "detected_domain":    {"type": "string"},
                                "student_level":      {"type": "string"}
                            }
                        }
                    }
                }
            }}}}}
        }},
        "/api/subjects/generate-full-exam": {"post": {
            "tags": ["Intelligence Artificielle"],
            "summary": "Générer un sujet complet depuis une suggestion",
            "description": (
                "Prend un objet suggestion (issu de generate-exam-suggestions) et génère un sujet complet avec questions numérotées et barème. "
                "`suggestion.question_types` peut inclure \"Questions dépendantes (sous-questions liées)\" (marqueur [SUBOPEN]) — exercices en "
                "plusieurs parties a/b/c où chaque sous-question s'appuie explicitement sur le résultat de la précédente ; le barème généré "
                "applique systématiquement la règle de l'erreur reportée. `suggestion.grading_criteria`, s'il est fourni (éventuellement "
                "adapté par l'enseignant avant génération), guide la répartition des points du barème détaillé. "
                "`suggestion.difficulty` reste la tendance dominante de l'examen, mais les questions individuelles couvrent TOUJOURS un mélange "
                "des trois niveaux (Facile/Moyen/Difficile) — chaque titre de question porte un second marqueur juste après son marqueur de "
                "type, ex. `[QCM] [Difficile]`, visible dans `content` pour l'enseignant mais retiré automatiquement de l'affichage étudiant "
                "côté frontend (comme les marqueurs [QCM]/[VF]/[OUVERT]). "
                "`suggestion.total_points` (défaut 20) et `suggestion.points_by_type` (Retour Atelier CEI 7/08 — barème choisi par "
                "l'enseignant, plus jamais réparti également par l'IA) remplacent le total fixe à 20 points d'origine : si plusieurs types de "
                "questions sont sélectionnés, `points_by_type` (ex. `{\"qcm\": 8, \"open\": 12}`) impose le nombre de points de CHAQUE partie ; "
                "un type absent de `points_by_type` (ou toute la clé si omise) retombe sur une répartition égale entre les types sélectionnés. "
                "Retour Atelier CEI 7/08 (correctif) — `question_count` (1 à 100, défaut 20) est désormais généré PAR LOTS, quelques questions à "
                "la fois par type plutôt qu'en un seul appel IA géant : élimine les trous de numérotation, titres corrompus et écarts de barème "
                "observés au-delà d'une trentaine de questions demandées en un seul bloc. Le nombre exact de questions et la somme exacte des "
                "points sont désormais garantis par le serveur (jamais recalculés à partir de ce que l'IA a écrit). Au-delà de 50 questions, le "
                "temps de génération augmente sensiblement (plusieurs appels IA séquentiels) — le frontend affiche un avertissement au-delà de "
                "ce seuil sans bloquer la génération."
            ),
            "requestBody": {"required": True, "content": {"application/json": {"schema": {
                "type": "object", "required": ["suggestion"],
                "properties": {"suggestion": {"type": "object", "description": "Objet suggestion retourné par generate-exam-suggestions, avec grading_criteria/total_points/points_by_type optionnellement adaptés par l'enseignant", "properties": {
                    "question_count":  {"type": "integer", "default": 20, "minimum": 1, "maximum": 100, "description": "Nombre total de questions à générer (réparti entre les types sélectionnés), généré par lots côté serveur"},
                    "total_points":    {"type": "integer", "default": 20, "minimum": 1, "maximum": 200, "description": "Barème total de l'examen généré, choisi par l'enseignant"},
                    "points_by_type":  {"type": "object", "description": "Points par type de question sélectionné (clés : qcm, qcm_multi, vf, appariement, code, open, subopen) — doit sommer à total_points si plusieurs types sont sélectionnés", "additionalProperties": {"type": "integer"}}
                }}}
            }}}},
            "responses": {"200": {"description": "Sujet généré", "content": {"application/json": {"schema": {
                "type": "object",
                "properties": {
                    "title":     {"type": "string"},
                    "content":   {"type": "string"},
                    "rubric":    {"type": "string"},
                    "full_text": {"type": "string"}
                }
            }}}}}
        }},
        "/api/subjects/create-from-suggestion": {"post": {
            "tags": ["Intelligence Artificielle"],
            "summary": "Sauvegarder un sujet généré par IA en base",
            "description": "`ec_id` est obligatoire (Retour DFIP #9) : un sujet sans EC rattaché empêcherait l'affectation automatique des groupes de surveillants à tout examen qui l'utiliserait, sans jamais le signaler. Un professeur ne peut lier le sujet qu'à un EC dont il est responsable (403 sinon).",
            "requestBody": {"required": True, "content": {"application/json": {"schema": {
                "type": "object", "required": ["title","content","ec_id"],
                "properties": {
                    "title":           {"type": "string"},
                    "content":         {"type": "string"},
                    "rubric_override": {"type": "string"},
                    "ec_id":           {"type": "integer", "description": "Obligatoire — EC auquel rattacher le sujet"},
                    "metadata":        {"type": "object"},
                    "media_link_key":  {"type": "string", "description": "Associe les médias uploadés pendant la composition (avant que le sujet n'existe) via leur link_key"}
                }
            }}}},
            "responses": {
                "200": {"description": "Sujet sauvegardé", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Subject"}}}},
                "400": {"description": "title/content manquants, ou ec_id manquant"},
                "403": {"description": "Rôle non autorisé, ou professeur non responsable de l'EC indiqué"}
            }
        }},
        "/api/subjects/generate-more-questions": {"post": {
            "tags": ["Intelligence Artificielle"],
            "summary": "Générer des questions supplémentaires à ajouter à un sujet (prof/admin)",
            "description": "Ajoute N nouvelles questions d'un type donné à un sujet déjà généré (sans le remplacer), en évitant les thèmes déjà couverts. Redistribue automatiquement les points sur le total du sujet (anciennes + nouvelles questions) et étend le barème d'une entrée par nouvelle question. `total_points` (optionnel) fixe explicitement ce total ; à défaut, il est détecté depuis la somme des points déjà présents dans `existing_content` (plus jamais imposé à 20). `difficulty` reste le niveau dominant demandé ; si `count` > 1, les nouvelles questions varient légèrement de niveau (marqueur [Facile]/[Moyen]/[Difficile] après le marqueur de type) plutôt que d'être toutes identiques.",
            "requestBody": {"required": True, "content": {"application/json": {"schema": {
                "type": "object", "required": ["existing_content"],
                "properties": {
                    "existing_content": {"type": "string", "description": "Contenu actuel du sujet"},
                    "existing_rubric":  {"type": "string", "description": "Barème actuel du sujet"},
                    "total_points":     {"type": "integer", "description": "Total à respecter après ajout (défaut : détecté depuis existing_content, sinon 20)"},
                    "count":            {"type": "integer", "default": 3, "description": "Nombre de questions à ajouter, 1 à 10"},
                    "question_type":    {"type": "string", "example": "QCM", "description": "Type des nouvelles questions"},
                    "title":            {"type": "string"},
                    "student_level":    {"type": "string", "example": "Licence 3"},
                    "difficulty":       {"type": "string", "example": "Moyen"}
                }
            }}}},
            "responses": {
                "200": {"description": "Questions générées", "content": {"application/json": {"schema": {
                    "type": "object",
                    "properties": {
                        "success":          {"type": "boolean"},
                        "new_content":      {"type": "string", "description": "Uniquement les nouvelles questions"},
                        "full_content":     {"type": "string", "description": "Sujet complet (anciennes + nouvelles questions, points redistribués)"},
                        "full_rubric":      {"type": "string", "description": "Barème complet étendu"},
                        "count_generated":  {"type": "integer"},
                        "duplicates":       {"type": "array", "items": {"type": "object", "properties": {"similarity": {"type": "number"}}}, "description": "Nouvelles questions détectées comme quasi-identiques à une question existante"}
                    }
                }}}},
                "400": {"description": "Contenu existant requis"},
                "403": {"$ref": "#/components/responses/Forbidden"}
            }
        }},
        "/api/subjects/suggest-question-count": {"post": {
            "tags": ["Intelligence Artificielle"],
            "summary": "Suggérer par IA un nombre de questions adapté (prof/admin)",
            "description": "L'IA suggère un nombre de questions adapté à la durée/difficulté/niveau, au lieu de laisser le professeur deviner. Repli heuristique (duration // 5) si l'IA est indisponible.",
            "requestBody": {"content": {"application/json": {"schema": {
                "type": "object",
                "properties": {
                    "duration":       {"type": "integer", "default": 60, "description": "Durée de l'examen en minutes"},
                    "difficulty":     {"type": "string", "default": "Moyen"},
                    "student_level":  {"type": "string", "default": "Licence 3"},
                    "question_types": {"type": "string", "default": "mixte"}
                }
            }}}},
            "responses": {
                "200": {"description": "Nombre suggéré", "content": {"application/json": {"schema": {
                    "type": "object",
                    "properties": {
                        "success":         {"type": "boolean"},
                        "suggested_count": {"type": "integer"},
                        "fallback":        {"type": "boolean", "description": "true si l'IA était indisponible et que le repli heuristique a été utilisé"}
                    }
                }}}},
                "403": {"$ref": "#/components/responses/Forbidden"}
            }
        }},

        # ══════════════════════════════════════════════════════════════════════
        # RÉCLAMATIONS
        # ══════════════════════════════════════════════════════════════════════

        "/api/reclamations": {
            "get": {
                "tags": ["Réclamations"], "summary": "Liste des réclamations (admin/prof : toutes ; étudiant : les siennes)",
                "responses": {"200": {"description": "Réclamations", "content": {"application/json": {"schema": {
                    "type": "array", "items": {"$ref": "#/components/schemas/Reclamation"}
                }}}}}
            },
            "post": {
                "tags": ["Réclamations"], "summary": "Déposer une réclamation (étudiant)",
                "description": "L'étudiant dispose de 7 jours après la correction pour déposer une réclamation.",
                "requestBody": {"required": True, "content": {"application/json": {"schema": {
                    "type": "object", "required": ["paper_id","reason"],
                    "properties": {
                        "paper_id": {"type": "integer"},
                        "reason":   {"type": "string", "example": "La question 3 a été mal évaluée."}
                    }
                }}}},
                "responses": {
                    "201": {"description": "Réclamation enregistrée"},
                    "400": {"description": "Fenêtre de 7 jours expirée"}
                }
            }
        },
        "/api/reclamations/{rid}": {
            "put": {
                "tags": ["Réclamations"],
                "summary": "Répondre manuellement à une réclamation (prof/admin)",
                "description": "Le professeur peut accepter (avec ou sans modification de note) ou rejeter la réclamation.",
                "parameters": [{"name": "rid", "in": "path", "required": True, "schema": {"type": "integer"}}],
                "requestBody": {"required": True, "content": {"application/json": {"schema": {
                    "type": "object", "required": ["status"],
                    "properties": {
                        "status":    {"type": "string", "enum": ["resolved","rejected"]},
                        "response":  {"type": "string", "description": "Explication de la décision"},
                        "new_score": {"type": "number", "description": "Nouvelle note si acceptée (optionnel)"}
                    }
                }}}},
                "responses": {"200": {"description": "Réclamation traitée"}}
            },
            "delete": {
                "tags": ["Réclamations"],
                "summary": "Supprimer une réclamation (prof/admin)",
                "parameters": [{"name": "rid", "in": "path", "required": True, "schema": {"type": "integer"}}],
                "responses": {"200": {"description": "Réclamation supprimée"}, "404": {"description": "Réclamation non trouvée"}}
            }
        },
        "/api/reclamations/{rid}/process_ia": {"post": {
            "tags": ["Réclamations"],
            "summary": "Traiter une réclamation par IA",
            "description": "L'IA re-corrige la copie en tenant compte de la contestation et propose une note révisée. Le prof peut ensuite accepter ou rejeter.",
            "parameters": [{"name": "rid", "in": "path", "required": True, "schema": {"type": "integer"}}],
            "responses": {"200": {"description": "Proposition IA", "content": {"application/json": {"schema": {
                "type": "object",
                "properties": {
                    "ia_proposed_score":  {"type": "number"},
                    "ia_proposed_status": {"type": "string", "enum": ["accepted","rejected","partial"]},
                    "ia_proposed_reason": {"type": "string"}
                }
            }}}}}
        }},
        "/api/reclamations/{rid}/apply_proposal": {"post": {
            "tags": ["Réclamations"],
            "summary": "Accepter et appliquer la proposition IA (prof/admin)",
            "description": "Applique la note proposée par l'IA à la copie et clôt la réclamation avec statut 'resolved'.",
            "parameters": [{"name": "rid", "in": "path", "required": True, "schema": {"type": "integer"}}],
            "responses": {"200": {"description": "Proposition IA appliquée"}, "400": {"description": "Aucune proposition disponible"}}
        }},
        "/api/reclamations/{rid}/reject_proposal": {"post": {
            "tags": ["Réclamations"],
            "summary": "Rejeter la proposition IA (prof/admin)",
            "description": "Rejette la proposition IA sans modifier la note. La réclamation est clôturée avec statut 'rejected'.",
            "parameters": [{"name": "rid", "in": "path", "required": True, "schema": {"type": "integer"}}],
            "requestBody": {"content": {"application/json": {"schema": {
                "type": "object",
                "properties": {"response": {"type": "string", "default": "Proposition IA rejetée par le professeur"}}
            }}}},
            "responses": {"200": {"description": "Proposition rejetée"}}
        }},
        "/api/reclamations/bulk_delete": {"post": {
            "tags": ["Réclamations"],
            "summary": "Supprimer plusieurs réclamations en un appel (prof/admin)",
            "description": "Utilisé notamment pour nettoyer les réclamations orphelines (copie/tentative d'origine supprimée).",
            "requestBody": {"required": True, "content": {"application/json": {"schema": {
                "type": "object", "required": ["reclamation_ids"],
                "properties": {"reclamation_ids": {"type": "array", "items": {"type": "integer"}}}
            }}}},
            "responses": {"200": {"description": "Réclamations supprimées"}, "400": {"description": "Aucune réclamation sélectionnée"}}
        }},

        # ══════════════════════════════════════════════════════════════════════
        # RESTITUTION — copies-exemples anonymisées (séances de restitution)
        # ══════════════════════════════════════════════════════════════════════

        "/api/restitution_examples": {
            "get": {
                "tags": ["Restitution"], "summary": "Liste des copies-exemples (filtrée par rôle)",
                "description": "Étudiant : uniquement les exemples publiés. Professeur : les siens. Admin : tous.",
                "parameters": [{"name": "subject_id", "in": "query", "required": False, "schema": {"type": "integer"}}],
                "responses": {"200": {"description": "Exemples", "content": {"application/json": {"schema": {
                    "type": "array", "items": {"$ref": "#/components/schemas/RestitutionExample"}
                }}}}}
            },
            "post": {
                "tags": ["Restitution"], "summary": "Créer un exemple (anonymisation IA d'une copie déjà corrigée)",
                "description": "Anonymise via IA le contenu d'une StudentPaper ou d'une ExamAttempt déjà notée (paper_id XOR attempt_id), en brouillon non publié.",
                "requestBody": {"required": True, "content": {"application/json": {"schema": {
                    "type": "object", "required": ["label"],
                    "properties": {
                        "paper_id":   {"type": "integer", "description": "Copie corrigée d'où extraire le contenu (paper_id ou attempt_id requis)"},
                        "attempt_id": {"type": "integer", "description": "Tentative d'examen en ligne notée (alternative à paper_id)"},
                        "label":      {"type": "string", "enum": ["best", "improve"]}
                    }
                }}}},
                "responses": {
                    "201": {"description": "Exemple créé (brouillon)"},
                    "400": {"description": "Copie non encore corrigée, ou paper_id/attempt_id manquant"}
                }
            }
        },
        "/api/restitution_examples/{eid}": {
            "put": {
                "tags": ["Restitution"], "summary": "Éditer le texte anonymisé (avant publication)",
                "parameters": [{"name": "eid", "in": "path", "required": True, "schema": {"type": "integer"}}],
                "requestBody": {"content": {"application/json": {"schema": {
                    "type": "object",
                    "properties": {
                        "anonymized_content":  {"type": "string"},
                        "anonymized_feedback": {"type": "string"},
                        "label":               {"type": "string", "enum": ["best", "improve"]}
                    }
                }}}},
                "responses": {"200": {"description": "Exemple mis à jour"}, "400": {"description": "Contenu anonymisé vide"}}
            },
            "delete": {
                "tags": ["Restitution"], "summary": "Supprimer un exemple",
                "parameters": [{"name": "eid", "in": "path", "required": True, "schema": {"type": "integer"}}],
                "responses": {"200": {"description": "Exemple supprimé"}, "404": {"description": "Exemple non trouvé"}}
            }
        },
        "/api/restitution_examples/{eid}/publish": {"put": {
            "tags": ["Restitution"],
            "summary": "Publier / dépublier un exemple au groupe",
            "description": "Rien n'est visible aux étudiants tant que l'enseignant n'a pas explicitement validé le texte anonymisé (même logique que StudentPaper.is_published).",
            "parameters": [{"name": "eid", "in": "path", "required": True, "schema": {"type": "integer"}}],
            "requestBody": {"content": {"application/json": {"schema": {
                "type": "object", "properties": {"published": {"type": "boolean", "default": True}}
            }}}},
            "responses": {"200": {"description": "Statut de publication mis à jour"}}
        }},

        # ══════════════════════════════════════════════════════════════════════
        # RELEVÉS DE NOTES
        # ══════════════════════════════════════════════════════════════════════

        "/api/transcripts/generate/{student_id}/{semester_id}": {"post": {
            "tags": ["Relevés de notes"], "summary": "Générer un relevé de notes",
            "parameters": [
                {"name": "student_id",  "in": "path", "required": True, "schema": {"type": "integer"}},
                {"name": "semester_id", "in": "path", "required": True, "schema": {"type": "integer"}}
            ],
            "responses": {"200": {"description": "Relevé généré", "content": {"application/json": {"schema": {
                "type": "object",
                "properties": {
                    "transcript_id":    {"type": "integer"},
                    "gpa":              {"type": "number"},
                    "mention":          {"type": "string", "example": "Bien"},
                    "total_credits":    {"type": "integer"},
                    "obtained_credits": {"type": "integer"}
                }
            }}}}}
        }},
        "/api/transcripts": {"get": {
            "tags": ["Relevés de notes"], "summary": "Tous les relevés générés (admin/prof)",
            "responses": {"200": {"description": "Relevés", "content": {"application/json": {"schema": {
                "type": "array", "items": {"$ref": "#/components/schemas/GradeTranscript"}
            }}}}}
        }},
        "/api/student/transcripts": {"get": {
            "tags": ["Relevés de notes"], "summary": "Relevés de l'étudiant connecté",
            "responses": {"200": {"description": "Mes relevés", "content": {"application/json": {"schema": {
                "type": "array", "items": {"$ref": "#/components/schemas/GradeTranscript"}
            }}}}}
        }},
        "/api/transcripts/{tid}/pdf": {"get": {
            "tags": ["Relevés de notes"], "summary": "Télécharger un relevé en PDF",
            "parameters": [{"name": "tid", "in": "path", "required": True, "schema": {"type": "integer"}}],
            "responses": {
                "200": {"description": "PDF", "content": {"application/pdf": {"schema": {"type": "string", "format": "binary"}}}},
                "404": {"$ref": "#/components/responses/NotFound"}
            }
        }},

        # ══════════════════════════════════════════════════════════════════════
        # TABLEAUX DE BORD
        # ══════════════════════════════════════════════════════════════════════

        "/api/professor/dashboard": {"get": {
            "tags": ["Tableaux de bord"], "summary": "Tableau de bord professeur",
            "description": "Retourne le nombre de sujets créés, de copies corrigées, et de surveillants affectés (via les Groupes Surveillants rattachés aux ECs du professeur) par le professeur connecté.",
            "responses": {"200": {"description": "Stats prof", "content": {"application/json": {"schema": {
                "type": "object",
                "properties": {
                    "my_subjects":        {"type": "integer"},
                    "papers_corrected":   {"type": "integer"},
                    "total_surveillants": {"type": "integer", "description": "Surveillants distincts affectés via les Groupes Surveillants rattachés aux ECs du professeur"},
                    "active_surveillants": {"type": "integer", "description": "Parmi eux, ceux actuellement en train de surveiller un examen ('engaged')"}
                }
            }}}}}
        }},
        "/api/professor/corrected_papers": {"get": {
            "tags": ["Tableaux de bord"], "summary": "100 dernières copies corrigées par le prof connecté",
            "responses": {"200": {"description": "Copies", "content": {"application/json": {"schema": {
                "type": "object",
                "properties": {"papers": {"type": "array", "items": {"$ref": "#/components/schemas/StudentPaper"}}}
            }}}}}
        }},
        "/api/professor/recent_incidents": {"get": {
            "tags": ["Tableaux de bord"], "summary": "Incidents récents des examens du professeur",
            "description": "Flux recalculé à chaque appel (incidents de proctoring des dernières 24h + affectations d'EC des 7 derniers jours) — jamais stocké tel quel. Les items supprimés/marqués comme lus via /dismiss sont exclus.",
            "responses": {"200": {"description": "Incidents récents"}}
        }},
        "/api/professor/recent_incidents/dismiss": {"post": {
            "tags": ["Tableaux de bord"], "summary": "Supprimer/marquer comme lu un ou plusieurs items du flux d'incidents",
            "description": "Individuel (item_id) ou en masse (item_ids) — utilisé par le bouton « Tout marquer comme lu ». Persiste par utilisateur ; le flux source n'étant jamais stocké, seule cette suppression l'est.",
            "requestBody": {"required": True, "content": {"application/json": {"schema": {
                "type": "object",
                "properties": {
                    "item_id":  {"type": "string", "description": "Id d'un seul item (accepte aussi un entier)."},
                    "item_ids": {"type": "array", "items": {"type": "string"}, "description": "Ids de plusieurs items."}
                }
            }}}},
            "responses": {
                "200": {"description": "Items supprimés", "content": {"application/json": {"schema": {
                    "type": "object", "properties": {"success": {"type": "boolean"}, "dismissed": {"type": "integer"}}
                }}}},
                "400": {"description": "item_id(s) manquant(s)"},
                "403": {"$ref": "#/components/responses/Forbidden"}
            }
        }},
        "/api/student/papers": {"get": {
            "tags": ["Tableaux de bord"], "summary": "Copies de l'étudiant connecté avec notes",
            "responses": {"200": {"description": "Mes copies", "content": {"application/json": {"schema": {
                "type": "array", "items": {"$ref": "#/components/schemas/StudentPaper"}
            }}}}}
        }},
        "/api/student/online_results": {"get": {
            "tags": ["Tableaux de bord"], "summary": "Résultats des examens en ligne de l'étudiant connecté",
            "description": "Point 19/Retour #29 — `score`/`feedback`/`corrected_at` restent `null` tant que l'enseignant n'a pas publié les résultats de l'examen (`PUT /api/online_exams/{exam_id}/publish-results`) ; `pending_publication` vaut alors `true`.",
            "responses": {"200": {"description": "Résultats", "content": {"application/json": {"schema": {
                "type": "array", "items": {
                    "type": "object",
                    "properties": {
                        "attempt_id":   {"type": "integer"},
                        "exam_title":   {"type": "string"},
                        "score":        {"type": "number", "nullable": True, "description": "null tant que non publié"},
                        "feedback":     {"type": "string", "nullable": True, "description": "null tant que non publié"},
                        "corrected_at": {"type": "string", "format": "date-time", "nullable": True},
                        "auto_correct": {"type": "boolean"},
                        "has_reclamation": {"type": "boolean"},
                        "reclamation_status": {"type": "string"},
                        "results_published": {"type": "boolean"},
                        "pending_publication": {"type": "boolean", "description": "true si corrigé mais pas encore publié"}
                    }
                }
            }}}}}
        }},
        "/api/student/post_submit_lock": {"get": {
            "tags": ["Tableaux de bord"],
            "summary": "Verrou anti-retour rapide après soumission d'un examen",
            "description": "Empêche de rouvrir l'examen quelques minutes après l'avoir soumis (ex. pour retenter avec un autre appareil) — le verrou expire au plus tard à la fin réelle de l'examen, s'il survient avant les 15 minutes.",
            "responses": {"200": {"description": "État du verrou", "content": {"application/json": {"schema": {
                "type": "object",
                "properties": {
                    "locked":     {"type": "boolean"},
                    "unlock_at":  {"type": "string", "format": "date-time", "nullable": True},
                    "exam_title": {"type": "string", "nullable": True}
                }
            }}}}}
        }},
        "/api/student/exam-history": {"get": {
            "tags": ["Tableaux de bord"], "summary": "Historique complet des examens de l'étudiant",
            "responses": {"200": {"description": "Historique", "content": {"application/json": {"schema": {
                "type": "array", "items": {"$ref": "#/components/schemas/ExamAttempt"}
            }}}}}
        }},
        "/api/professor/my_students": {"get": {
            "tags": ["Tableaux de bord"], "summary": "Étudiants inscrits aux EC du professeur connecté",
            "responses": {"200": {"description": "Étudiants", "content": {"application/json": {"schema": {
                "type": "array", "items": {"$ref": "#/components/schemas/User"}
            }}}}}
        }},
        "/api/professor/analytics": {"get": {
            "tags": ["Tableaux de bord"], "summary": "Analytique du professeur — notes, taux de réussite, évolution",
            "responses": {"200": {"description": "Données analytiques"}}
        }},

        # ══════════════════════════════════════════════════════════════════════
        # AUTHENTIFICATION — Mot de passe oublié
        # ══════════════════════════════════════════════════════════════════════

        "/api/auth/forgot-password": {"post": {
            "tags": ["Authentification"], "summary": "Demander la réinitialisation du mot de passe",
            "security": [],
            "requestBody": {"required": True, "content": {"application/json": {"schema": {
                "type": "object", "required": ["email"],
                "properties": {"email": {"type": "string", "example": "user@ec2lt.sn"}}
            }}}},
            "responses": {
                "200": {"description": "Toujours 200 avec success:true, que le compte existe ou non — ne révèle jamais si un email est enregistré (email_sent:false si absent/pas d'email)"}
            }
        }},
        "/api/auth/reset-password": {"post": {
            "tags": ["Authentification"], "summary": "Réinitialiser le mot de passe avec un token",
            "description": "Le lien envoyé par email pointe vers {APP_URL}/reset-password?token=... côté frontend, page dédiée qui appelle cet endpoint.",
            "security": [],
            "requestBody": {"required": True, "content": {"application/json": {"schema": {
                "type": "object", "required": ["token","new_password"],
                "properties": {
                    "token":        {"type": "string", "description": "Token reçu par email, valable 1h, usage unique"},
                    "new_password": {"type": "string", "minLength": 8}
                }
            }}}},
            "responses": {
                "200": {"description": "Mot de passe réinitialisé"},
                "400": {"description": "Token invalide ou expiré"}
            }
        }},

        # ══════════════════════════════════════════════════════════════════════
        # ADMINISTRATION — Routes manquantes
        # ══════════════════════════════════════════════════════════════════════

        "/api/admin/security_report": {"get": {
            "tags": ["Administration"], "summary": "Rapport de sécurité global (admin/prof)",
            "description": "Retourne les tentatives à risque élevé, les exclusions et les incidents sur tous les examens.",
            "responses": {"200": {"description": "Rapport sécurité"}}
        }},
        "/api/admin/students/{student_id}/enrollments": {"get": {
            "tags": ["Académique"], "summary": "Inscriptions d'un étudiant (admin)",
            "parameters": [{"name": "student_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
            "responses": {"200": {"description": "UEs et ECs de l'étudiant"}}
        }},
        "/api/admin/students/enrollments/bulk": {"get": {
            "tags": ["Académique"],
            "summary": "Inscriptions UE de TOUS les étudiants en un seul appel (admin)",
            "description": "Remplace N appels individuels à GET /api/admin/students/{student_id}/enrollments (un par étudiant) qui saturaient le rate-limit (60/min) sur les pages listant beaucoup d'étudiants (ex: 48 requêtes simultanées → 429). Résultat groupé par student_id.",
            "responses": {"200": {"description": "Inscriptions groupées par étudiant", "content": {"application/json": {"schema": {
                "type": "object",
                "description": "Clé = student_id (string), valeur = liste des inscriptions UE de cet étudiant",
                "additionalProperties": {
                    "type": "array", "items": {
                        "type": "object",
                        "properties": {
                            "enrollment_id":  {"type": "integer"},
                            "ue_id":          {"type": "integer"},
                            "ue_code":        {"type": "string"},
                            "ue_name":        {"type": "string"},
                            "semester_name":  {"type": "string"},
                            "formation_name": {"type": "string"},
                            "formation_code": {"type": "string"}
                        }
                    }
                }
            }}}}}
        }},
        "/api/admin/students/{student_id}/set_formation": {"post": {
            "tags": ["Académique"], "summary": "Affecter une formation à un étudiant (admin)",
            "parameters": [{"name": "student_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
            "requestBody": {"required": True, "content": {"application/json": {"schema": {
                "type": "object", "required": ["formation_id"],
                "properties": {"formation_id": {"type": "integer"}}
            }}}},
            "responses": {"200": {"description": "Formation affectée"}}
        }},
        "/api/ues": {"get": {
            "tags": ["Académique"], "summary": "Toutes les UEs (admin/prof)",
            "responses": {"200": {"description": "UEs", "content": {"application/json": {"schema": {
                "type": "array", "items": {"$ref": "#/components/schemas/UE"}
            }}}}}
        }},

        # ══════════════════════════════════════════════════════════════════════
        # SUJETS — Routes manquantes
        # ══════════════════════════════════════════════════════════════════════

        "/api/subjects/{subject_id}/upload_image": {"post": {
            "tags": ["Sujets"], "summary": "Uploader une image d'illustration pour un sujet",
            "parameters": [{"name": "subject_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
            "requestBody": {"required": True, "content": {"multipart/form-data": {"schema": {
                "type": "object", "required": ["file"],
                "properties": {"file": {"type": "string", "format": "binary"}}
            }}}},
            "responses": {"200": {"description": "Image uploadée"}, "404": {"$ref": "#/components/responses/NotFound"}}
        }},
        "/api/subjects/upload_media": {"post": {
            "tags": ["Sujets"],
            "summary": "Uploader un média (image/audio/vidéo) analysé par l'IA pour un sujet",
            "description": "Utilisable AVANT la sauvegarde finale du sujet via `link_key` (ex: uuid généré côté client) — associé au sujet définitif via `media_link_key` lors de l'appel à `create-from-suggestion`. Si `subject_id` est fourni (sujet déjà sauvegardé), l'association est immédiate. L'IA analyse le média selon `instructions` pour savoir comment l'exploiter dans les questions générées. Limite 25 Mo (image/audio) ou 80 Mo (vidéo).",
            "requestBody": {"required": True, "content": {"multipart/form-data": {"schema": {
                "type": "object", "required": ["file", "media_type"],
                "properties": {
                    "file":         {"type": "string", "format": "binary"},
                    "media_type":   {"type": "string", "enum": ["image", "audio", "video"]},
                    "link_key":     {"type": "string", "description": "Clé temporaire côté client — requise si subject_id absent"},
                    "subject_id":   {"type": "integer", "description": "Requis si link_key absent — associe le média immédiatement à un sujet existant"},
                    "instructions": {"type": "string", "description": "Consigne pour l'IA sur l'exploitation du média"}
                }
            }}}},
            "responses": {
                "201": {"description": "Média uploadé et analysé", "content": {"application/json": {"schema": {
                    "type": "object",
                    "properties": {
                        "success": {"type": "boolean"},
                        "media": {
                            "type": "object",
                            "properties": {
                                "id":           {"type": "integer"},
                                "subject_id":   {"type": "integer", "nullable": True},
                                "media_type":   {"type": "string", "enum": ["image", "audio", "video"]},
                                "filename":     {"type": "string"},
                                "s3_key":       {"type": "string"},
                                "instructions": {"type": "string"},
                                "ai_analysis":  {"type": "string"},
                                "created_at":   {"type": "string", "format": "date-time"},
                                "marker":       {"type": "string", "description": "Marqueur à insérer dans le sujet, ex: [IMAGE:photo.jpg]"}
                            }
                        }
                    }
                }}}},
                "400": {"description": "media_type invalide, fichier manquant/trop volumineux ou type non autorisé"},
                "403": {"$ref": "#/components/responses/Forbidden"}
            }
        }},
        "/api/subjects/{subject_id}/media": {"get": {
            "tags": ["Sujets"],
            "summary": "Lister les médias d'un sujet",
            "description": "Utilisé par la page d'examen pour afficher/lire les [IMAGE:...]/[AUDIO:...] du sujet — chaque média inclut une URL d'accès résolue.",
            "parameters": [{"name": "subject_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
            "responses": {"200": {"description": "Médias du sujet", "content": {"application/json": {"schema": {
                "type": "array", "items": {
                    "type": "object",
                    "properties": {
                        "id":         {"type": "integer"},
                        "media_type": {"type": "string", "enum": ["image", "audio", "video"]},
                        "filename":   {"type": "string"},
                        "url":        {"type": "string", "description": "URL d'accès résolue (MinIO)"},
                        "created_at": {"type": "string", "format": "date-time"}
                    }
                }
            }}}}}
        }},

        # ══════════════════════════════════════════════════════════════════════
        # EXAMENS EN LIGNE — Routes manquantes
        # ══════════════════════════════════════════════════════════════════════

        "/api/online_exams/{exam_id}/extend": {"post": {
            "tags": ["Examens en ligne"], "summary": "Prolonger la durée d'un examen actif",
            "parameters": [{"name": "exam_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
            "requestBody": {"required": True, "content": {"application/json": {"schema": {
                "type": "object", "required": ["minutes"],
                "properties": {"minutes": {"type": "integer", "example": 15, "description": "Minutes supplémentaires"}}
            }}}},
            "responses": {"200": {"description": "Durée prolongée"}, "400": {"description": "Examen non actif"}}
        }},
        "/api/admin/online_exams/{exam_id}": {"put": {
            "tags": ["Examens en ligne"], "summary": "Reprogrammer un examen DRAFT/SCHEDULED (titre, horaires, durée)",
            "description": "end_time est prioritaire sur duration_minutes si les deux sont fournis (la durée est recalculée à partir de lui) ; sinon end_time est dérivé de start_time + duration_minutes. Rejette si end_time <= start_time. Réservé aux examens DRAFT ou SCHEDULED.",
            "parameters": [{"name": "exam_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
            "requestBody": {"content": {"application/json": {"schema": {
                "type": "object",
                "properties": {
                    "title":            {"type": "string"},
                    "start_time":       {"type": "string", "format": "date-time"},
                    "end_time":         {"type": "string", "format": "date-time", "description": "Prioritaire sur duration_minutes si fourni."},
                    "duration_minutes": {"type": "integer", "description": "Ignoré si end_time est fourni — recalculé automatiquement."},
                    "enable_file_download": {"type": "boolean", "description": "Autoriser le téléchargement des fichiers du sujet"},
                    "enable_calculator":   {"type": "boolean", "description": "Calculatrice scientifique intégrée à la page de composition"},
                    "require_biometric":   {"type": "boolean", "description": "Exige une vérification d'identité par reconnaissance faciale avant l'accès à cet examen"}
                }
            }}}},
            "responses": {"200": {"description": "Examen mis à jour", "content": {"application/json": {"schema": {
                "type": "object", "properties": {"success": {"type": "boolean"}, "exam": {"$ref": "#/components/schemas/OnlineExam"}}
            }}}}, "400": {"description": "Statut non modifiable, ou end_time <= start_time"}}
        }},
        "/api/online_exams/{exam_id}/results/csv": {"get": {
            "tags": ["Examens en ligne"], "summary": "Exporter les résultats d'un examen en CSV",
            "parameters": [{"name": "exam_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
            "responses": {
                "200": {"description": "Fichier CSV", "content": {"text/csv": {"schema": {"type": "string", "format": "binary"}}}}
            }
        }},
        "/api/online_exams/{exam_id}/export-csv": {"get": {
            "tags": ["Examens en ligne"], "summary": "Export CSV complet (tentatives + scores + incidents)",
            "parameters": [{"name": "exam_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
            "responses": {
                "200": {"description": "CSV complet", "content": {"text/csv": {"schema": {"type": "string", "format": "binary"}}}}
            }
        }},
        "/api/online_exams/{exam_id}/import-grades": {"post": {
            "tags": ["Examens en ligne"],
            "summary": "Importer des notes calculées hors plateforme (prof/admin)",
            "description": "Fichier Excel (.xlsx/.xls) ou CSV avec colonnes 'email' et 'note' (0-20), pour des étudiants n'ayant pas composé sur la plateforme (épreuve papier, autre système). Crée une ExamAttempt marquée imported_grade=True, ou met à jour la note si une tentative existe déjà pour cet étudiant sur cet examen.",
            "parameters": [{"name": "exam_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
            "requestBody": {"required": True, "content": {"multipart/form-data": {"schema": {
                "type": "object", "required": ["file"],
                "properties": {"file": {"type": "string", "format": "binary", "description": "Fichier .xlsx, .xls ou .csv"}}
            }}}},
            "responses": {
                "200": {"description": "Import terminé", "content": {"application/json": {"schema": {
                    "type": "object",
                    "properties": {
                        "success": {"type": "boolean"},
                        "created": {"type": "integer", "description": "Nouvelles tentatives créées"},
                        "updated": {"type": "integer", "description": "Tentatives existantes mises à jour"},
                        "errors":  {"type": "array", "items": {"type": "string"}, "description": "Lignes en erreur (email/note invalide, étudiant introuvable...)"}
                    }
                }}}},
                "400": {"description": "Fichier manquant, format invalide ou colonnes 'email'/'note' absentes"},
                "403": {"$ref": "#/components/responses/Forbidden"},
                "404": {"$ref": "#/components/responses/NotFound"}
            }
        }},
        "/api/online_exams/{exam_id}/publish-results": {"put": {
            "tags": ["Examens en ligne"],
            "summary": "Publier ou dépublier les notes d'un examen aux étudiants (prof/admin)",
            "description": "Tant que non publié, le prof/admin voit toujours les notes (correction/gestion) mais l'étudiant reçoit score=null.",
            "parameters": [{"name": "exam_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
            "requestBody": {"content": {"application/json": {"schema": {
                "type": "object",
                "properties": {"published": {"type": "boolean", "default": True}}
            }}}},
            "responses": {
                "200": {"description": "Statut de publication mis à jour", "content": {"application/json": {"schema": {
                    "type": "object",
                    "properties": {"success": {"type": "boolean"}, "results_published": {"type": "boolean"}}
                }}}},
                "403": {"$ref": "#/components/responses/Forbidden"},
                "404": {"$ref": "#/components/responses/NotFound"}
            }
        }},
        "/api/online_exams/{exam_id}/stats": {"get": {
            "tags": ["Examens en ligne"], "summary": "Statistiques détaillées d'un examen (moyenne, distribution, incidents)",
            "parameters": [{"name": "exam_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
            "responses": {"200": {"description": "Statistiques", "content": {"application/json": {"schema": {
                "type": "object",
                "properties": {
                    "total_attempts":   {"type": "integer"},
                    "submitted_count":  {"type": "integer"},
                    "corrected_count":  {"type": "integer"},
                    "average_score":    {"type": "number"},
                    "pass_rate":        {"type": "number"},
                    "score_distribution": {"type": "object"}
                }
            }}}}}
        }},
        "/api/online_exams/{exam_id}/bilan": {"get": {
            "tags": ["Examens en ligne"], "summary": "Bilan complet d'un examen clôturé",
            "description": "Retourne tentatives, notes, incidents, ranking et rapport de surveillance.",
            "parameters": [{"name": "exam_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
            "responses": {"200": {"description": "Bilan complet"}}
        }},
        "/api/online_exams/{exam_id}/bilan/pdf": {"get": {
            "tags": ["Examens en ligne"], "summary": "Télécharger le bilan en PDF",
            "parameters": [{"name": "exam_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
            "responses": {
                "200": {"description": "PDF bilan", "content": {"application/pdf": {"schema": {"type": "string", "format": "binary"}}}}
            }
        }},
        "/api/online_exams/{exam_id}/security-report/pdf": {"get": {
            "tags": ["Examens en ligne"],
            "summary": "Rapport de sécurité PDF agrégé d'un examen (prof/admin)",
            "description": "Synthétise les incidents de surveillance de toutes les tentatives d'un même examen, triées par risque décroissant. À la différence de /api/admin/security_report (JSON, toutes évaluations confondues) et de /api/exam_attempts/{attempt_id}/integrity-report (une seule tentative), ce rapport est PDF et porte sur UN examen.",
            "parameters": [{"name": "exam_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
            "responses": {
                "200": {"description": "PDF rapport de sécurité", "content": {"application/pdf": {"schema": {"type": "string", "format": "binary"}}}},
                "403": {"$ref": "#/components/responses/Forbidden"},
                "404": {"$ref": "#/components/responses/NotFound"}
            }
        }},
        "/api/online_exams/{exam_id}/security_report/pdf": {"get": {
            "tags": ["Examens en ligne"],
            "summary": "Rapport de sécurité PDF agrégé d'un examen — variante (prof/admin)",
            "description": "Implémentation historique distincte de /api/online_exams/{exam_id}/security-report/pdf (tiret) : même objet (rapport PDF agrégé des incidents de surveillance d'un examen), logique de génération dupliquée indépendamment plutôt que factorisée. Toujours utilisée en production par components/shared/SecurityReportPanel.tsx (pages détail examen et sécurité, prof/admin) — ce n'est pas du code mort malgré le doublon.",
            "parameters": [{"name": "exam_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
            "responses": {
                "200": {"description": "PDF rapport de sécurité", "content": {"application/pdf": {"schema": {"type": "string", "format": "binary"}}}},
                "403": {"$ref": "#/components/responses/Forbidden"},
                "404": {"$ref": "#/components/responses/NotFound"}
            }
        }},
        "/api/online_exams/{exam_id}/plagiarism-check": {"get": {
            "tags": ["Examens en ligne"], "summary": "Vérification de plagiat entre les copies",
            "description": "Analyse les similarités textuelles entre toutes les réponses soumises. Retourne les paires suspectes.",
            "parameters": [{"name": "exam_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
            "responses": {"200": {"description": "Résultats de l'analyse de plagiat"}}
        }},
        "/api/online_exams/{exam_id}/qrcode": {"get": {
            "tags": ["Examens en ligne"], "summary": "Générer un QR code pointant vers l'application",
            "parameters": [{"name": "exam_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
            "responses": {"200": {"description": "QR code base64", "content": {"application/json": {"schema": {
                "type": "object",
                "properties": {
                    "exam_id":    {"type": "integer"},
                    "exam_title": {"type": "string"},
                    "url":        {"type": "string"},
                    "qrcode_b64": {"type": "string", "description": "data:image/png;base64,..."}
                }
            }}}}}
        }},
        "/api/online_exams/{exam_id}/corrections/zip": {"get": {
            "tags": ["Examens en ligne"], "summary": "Télécharger toutes les corrections en ZIP",
            "parameters": [{"name": "exam_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
            "responses": {
                "200": {"description": "Archive ZIP", "content": {"application/zip": {"schema": {"type": "string", "format": "binary"}}}}
            }
        }},

        # ══════════════════════════════════════════════════════════════════════
        # TENTATIVES — Routes manquantes
        # ══════════════════════════════════════════════════════════════════════

        "/api/exam_attempts/{attempt_id}/result": {"get": {
            "tags": ["Examens en ligne"], "summary": "Résultat d'une tentative (étudiant après soumission)",
            "description": "Point 19/Retour #29 — `score`/`feedback`/`corrected_at` restent `null` tant que l'enseignant n'a pas publié les résultats de l'examen (`PUT /api/online_exams/{exam_id}/publish-results`) ; `pending_publication` vaut alors `true`.",
            "parameters": [{"name": "attempt_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
            "responses": {"200": {"description": "Score et feedback", "content": {"application/json": {"schema": {
                "type": "object",
                "properties": {
                    "score":        {"type": "number", "nullable": True, "description": "null tant que non publié"},
                    "feedback":     {"type": "string", "nullable": True, "description": "null tant que non publié"},
                    "corrected_at": {"type": "string", "format": "date-time", "nullable": True},
                    "submitted_at": {"type": "string", "format": "date-time"},
                    "status":       {"type": "string"},
                    "results_published": {"type": "boolean"},
                    "pending_publication": {"type": "boolean", "description": "true si corrigé mais pas encore publié"}
                }
            }}}}}
        }},
        "/api/exam_attempts/{attempt_id}/manual-grade": {"put": {
            "tags": ["Examens en ligne"], "summary": "Note manuelle GLOBALE d'une tentative (prof/admin) — écrase la correction IA",
            "description": "Remplace intégralement la note et le feedback, que la copie ait été corrigée par l'IA ou non. Vide `question_scores` (voir /question-grades) : une note globale rend le détail par question obsolète, il est donc effacé plutôt que laissé silencieusement incohérent.",
            "parameters": [{"name": "attempt_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
            "requestBody": {"required": True, "content": {"application/json": {"schema": {
                "type": "object", "required": ["score"],
                "properties": {
                    "score":    {"type": "number", "minimum": 0, "maximum": 20},
                    "feedback": {"type": "string"}
                }
            }}}},
            "responses": {"200": {"description": "Note enregistrée"}}
        }},
        "/api/exam_attempts/{attempt_id}/question-grades": {"put": {
            "tags": ["Examens en ligne"], "summary": "Correction manuelle QUESTION PAR QUESTION (prof/admin)",
            "description": (
                "Ajuste le score/feedback d'une ou plusieurs questions individuelles de la correction IA (déterministe "
                "QCM/VF/appariement ou générée pour les questions ouvertes) — la note globale est recalculée automatiquement "
                "comme la somme des scores par question, ramenée sur 20. Nécessite qu'un détail par question existe déjà "
                "(`ExamAttempt.question_scores` non vide, alimenté automatiquement à la correction IA — voir /correct) ; sinon "
                "utilisez /manual-grade pour une note globale. CEI aide à corriger plus vite grâce à l'IA, mais l'enseignant "
                "garde toujours la main, y compris au niveau de chaque question."
            ),
            "parameters": [{"name": "attempt_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
            "requestBody": {"required": True, "content": {"application/json": {"schema": {
                "type": "object", "required": ["scores"],
                "properties": {"scores": {"type": "array", "items": {"type": "object", "properties": {
                    "num": {"type": "string", "example": "5"}, "score": {"type": "number"}, "feedback": {"type": "string"}
                }}}}
            }}}},
            "responses": {
                "200": {"description": "Note recalculée", "content": {"application/json": {"schema": {
                    "type": "object", "properties": {
                        "success": {"type": "boolean"}, "score": {"type": "number", "description": "Nouvelle note globale /20"},
                        "question_scores": {"type": "array", "items": {"type": "object"}}, "feedback": {"type": "string"}
                    }
                }}}},
                "400": {"description": "Aucun détail par question disponible pour cette copie"}
            }
        }},
        "/api/exam_attempts/{attempt_id}/unban": {"post": {
            "tags": ["Examens en ligne"], "summary": "Lever l'exclusion d'un étudiant (prof/admin)",
            "parameters": [{"name": "attempt_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
            "responses": {"200": {"description": "Étudiant réintégré"}}
        }},
        "/api/exam_attempts/{attempt_id}/extra-time": {"put": {
            "tags": ["Proctoring"], "summary": "Accorder du temps supplémentaire à un étudiant en cours",
            "description": "Réservé aux tentatives IN_PROGRESS sur un examen ACTIVE. Impossible si l'étudiant a déjà soumis ou été exclu.",
            "parameters": [{"name": "attempt_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
            "requestBody": {"required": True, "content": {"application/json": {"schema": {
                "type": "object", "required": ["minutes"],
                "properties": {"minutes": {"type": "integer", "minimum": 1, "maximum": 120, "example": 10}}
            }}}},
            "responses": {
                "200": {"description": "Temps accordé", "content": {"application/json": {"schema": {
                    "type": "object",
                    "properties": {
                        "success":    {"type": "boolean"},
                        "total_extra":{"type": "integer", "description": "Total de minutes supplémentaires accordées"},
                        "added":      {"type": "integer"}
                    }
                }}}},
                "400": {"description": "Étudiant déjà terminé ou examen clôturé"}
            }
        }},
        "/api/exam_attempts/{attempt_id}/pause/start": {"post": {
            "tags": ["Examens en ligne"], "summary": "Démarrer une pause self-service de 3 minutes (étudiant)",
            "description": "Une seule pause autorisée par tentative. Crédite immédiatement +3 min à extra_minutes (échéance protégée dès le départ, y compris contre l'auto-clôture par end_time) et notifie le surveillant/superviseur/professeur (informatif, non bloquant).",
            "parameters": [{"name": "attempt_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
            "responses": {
                "200": {"description": "Pause démarrée", "content": {"application/json": {"schema": {
                    "type": "object",
                    "properties": {
                        "success": {"type": "boolean"},
                        "resume_at": {"type": "string", "format": "date-time", "description": "Horodatage UTC de fin de pause (3 min après le démarrage)"},
                        "total_extra": {"type": "integer", "description": "Total de minutes supplémentaires après crédit de la pause"}
                    }
                }}}},
                "400": {"description": "Pause déjà utilisée, tentative terminée, ou examen clôturé"},
                "404": {"description": "Tentative introuvable"}
            }
        }},
        "/api/exam_attempts/{attempt_id}/proctor-note": {"post": {
            "tags": ["Surveillant"], "summary": "Ajouter une note de surveillance sur une tentative (surveillant\/prof)",
            "parameters": [{"name": "attempt_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
            "requestBody": {"required": True, "content": {"application/json": {"schema": {
                "type": "object", "required": ["note"],
                "properties": {"note": {"type": "string", "example": "Étudiant a regardé hors caméra à plusieurs reprises."}}
            }}}},
            "responses": {"200": {"description": "Note enregistrée"}}
        }},
        "/api/exam_attempts/{attempt_id}/proctor-notes": {"get": {
            "tags": ["Surveillant"], "summary": "Lire les notes de surveillance d'une tentative (surveillant\/prof)",
            "parameters": [{"name": "attempt_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
            "responses": {"200": {"description": "Notes de surveillance"}}
        }},
        "/api/exam_attempts/{attempt_id}/review": {"get": {
            "tags": ["Examens en ligne"], "summary": "Révision détaillée d'une tentative (prof/admin)",
            "description": "Retourne la copie complète (réponses, feedback, incidents, notes de surveillance) ainsi que `corrector_name` (null si corrigé par l'IA, sinon nom de l'enseignant) et `question_scores` (détail par question — null si la copie n'a pas encore ce niveau de détail, voir /question-grades pour l'éditer).",
            "parameters": [{"name": "attempt_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
            "responses": {"200": {"description": "Révision complète", "content": {"application/json": {"schema": {
                "type": "object", "properties": {
                    "corrector_name": {"type": "string", "nullable": True, "description": "null = correction IA, sinon nom de l'enseignant ayant corrigé/révisé"},
                    "question_scores": {"type": "array", "nullable": True, "items": {"type": "object", "properties": {
                        "num": {"type": "string"}, "type": {"type": "string", "enum": ["qcm", "qcm_multi", "vf", "appariement", "open"]},
                        "max": {"type": "number"}, "score": {"type": "number"}, "correct": {"type": "boolean"}, "given": {"type": "string"}, "feedback": {"type": "string"}
                    }}}
                }
            }}}}}
        }},
        "/api/exam_attempts/{attempt_id}/report/pdf": {"get": {
            "tags": ["Examens en ligne"], "summary": "Rapport PDF individuel d'une tentative",
            "parameters": [{"name": "attempt_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
            "responses": {
                "200": {"description": "PDF rapport", "content": {"application/pdf": {"schema": {"type": "string", "format": "binary"}}}}
            }
        }},
        "/api/exam_attempts/{attempt_id}/integrity-report": {"get": {
            "tags": ["Proctoring"], "summary": "Rapport d'intégrité complet d'une tentative",
            "description": "Score de risque, incidents détaillés, snapshots caméra, log d'activité.",
            "parameters": [{"name": "attempt_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
            "responses": {"200": {"description": "Rapport d'intégrité"}}
        }},
        "/api/exam_attempts/{attempt_id}/face_reference": {"get": {
            "tags": ["Proctoring"], "summary": "Récupérer la photo de référence du visage de l'étudiant",
            "parameters": [{"name": "attempt_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
            "responses": {"200": {"description": "Photo base64"}}
        }},
        # ══════════════════════════════════════════════════════════════════════
        # SÉCURITÉ / BIOMÉTRIE
        # ══════════════════════════════════════════════════════════════════════

        "/api/security/face_references": {"get": {
            "tags": ["Proctoring"], "summary": "Photos de référence enregistrées (admin)",
            "description": "Liste toutes les photos de visage de référence enregistrées par les étudiants.",
            "responses": {"200": {"description": "Références photo"}}
        }},

        # ══════════════════════════════════════════════════════════════════════
        # BANQUE DE QUESTIONS
        # ══════════════════════════════════════════════════════════════════════

        "/api/question_bank": {
            "get": {
                "tags": ["Intelligence Artificielle"], "summary": "Liste des questions sauvegardées",
                "parameters": [
                    {"name": "ec_id", "in": "query", "schema": {"type": "integer"}, "description": "Filtrer par EC"},
                    {"name": "type",  "in": "query", "schema": {"type": "string", "enum": ["qcm","open","short"]}}
                ],
                "responses": {"200": {"description": "Questions"}}
            },
            "post": {
                "tags": ["Intelligence Artificielle"], "summary": "Ajouter une question à la banque",
                "requestBody": {"required": True, "content": {"application/json": {"schema": {
                    "type": "object", "required": ["question","type"],
                    "properties": {
                        "question":  {"type": "string"},
                        "type":      {"type": "string", "enum": ["qcm","open","short"]},
                        "options":   {"type": "array", "items": {"type": "string"}, "description": "Choix pour QCM"},
                        "answer":    {"type": "string"},
                        "ec_id":     {"type": "integer"},
                        "points":    {"type": "number"}
                    }
                }}}},
                "responses": {"201": {"description": "Question ajoutée"}}
            }
        },
        "/api/question_bank/{q_id}": {
            "put": {
                "tags": ["Intelligence Artificielle"], "summary": "Éditer une question de la banque en place",
                "description": "Parité Moodle — les questions de la banque ne sont plus figées (titre, énoncé, barème, type, Bloom, EC, tags, statut).",
                "parameters": [{"name": "q_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
                "requestBody": {"content": {"application/json": {"schema": {
                    "type": "object",
                    "properties": {
                        "title":         {"type": "string"},
                        "content":       {"type": "string"},
                        "rubric":        {"type": "string"},
                        "question_type": {"type": "string"},
                        "bloom_level":   {"type": "string"},
                        "ec_id":         {"type": "integer", "nullable": True},
                        "tags":          {"type": "array", "items": {"type": "string"}},
                        "status":        {"type": "string", "enum": ["active", "hidden"]}
                    }
                }}}},
                "responses": {
                    "200": {"description": "Question mise à jour", "content": {"application/json": {"schema": {
                        "type": "object", "properties": {"success": {"type": "boolean"}, "question": {"type": "object"}}
                    }}}},
                    "400": {"description": "Titre ou contenu vide"},
                    "403": {"$ref": "#/components/responses/Forbidden"},
                    "404": {"description": "Question introuvable"}
                }
            },
            "delete": {
                "tags": ["Intelligence Artificielle"], "summary": "Supprimer une question de la banque",
                "parameters": [{"name": "q_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
                "responses": {"200": {"description": "Question supprimée"}}
            }
        },
        "/api/question_bank/{q_id}/duplicate": {"post": {
            "tags": ["Intelligence Artificielle"],
            "summary": "Dupliquer une question de la banque",
            "description": "Crée une copie (\"{titre} (copie)\") de la question — parité Moodle : créer une variante à partir d'une question existante sans repartir de zéro.",
            "parameters": [{"name": "q_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
            "responses": {
                "201": {"description": "Question dupliquée", "content": {"application/json": {"schema": {
                    "type": "object", "properties": {"success": {"type": "boolean"}, "question": {"type": "object"}}
                }}}},
                "403": {"$ref": "#/components/responses/Forbidden"},
                "404": {"$ref": "#/components/responses/NotFound"}
            }
        }},
        "/api/question_bank/bulk_move": {"post": {
            "tags": ["Intelligence Artificielle"],
            "summary": "Déplacer plusieurs questions vers un autre EC (bulk move)",
            "description": "Parité Moodle (bulk move entre catégories). Un professeur ne peut déplacer que ses propres questions ; les questions sautées (skipped) appartiennent à un autre professeur.",
            "requestBody": {"required": True, "content": {"application/json": {"schema": {
                "type": "object", "required": ["question_ids"],
                "properties": {
                    "question_ids": {"type": "array", "items": {"type": "integer"}},
                    "ec_id":        {"type": "integer", "nullable": True, "description": "null pour retirer l'EC des questions"}
                }
            }}}},
            "responses": {
                "200": {"description": "Questions déplacées", "content": {"application/json": {"schema": {
                    "type": "object",
                    "properties": {"success": {"type": "boolean"}, "moved": {"type": "integer"}, "skipped": {"type": "integer"}}
                }}}},
                "400": {"description": "Aucune question sélectionnée"},
                "403": {"$ref": "#/components/responses/Forbidden"}
            }
        }},
        "/api/question_bank/bulk_delete": {"post": {
            "tags": ["Intelligence Artificielle"],
            "summary": "Supprimer plusieurs questions de la banque (bulk delete)",
            "description": "Parité Moodle (bulk delete), au lieu de supprimer une par une. Un professeur ne peut supprimer que ses propres questions ; les questions sautées (skipped) appartiennent à un autre professeur.",
            "requestBody": {"required": True, "content": {"application/json": {"schema": {
                "type": "object", "required": ["question_ids"],
                "properties": {"question_ids": {"type": "array", "items": {"type": "integer"}}}
            }}}},
            "responses": {
                "200": {"description": "Questions supprimées", "content": {"application/json": {"schema": {
                    "type": "object",
                    "properties": {"success": {"type": "boolean"}, "deleted": {"type": "integer"}, "skipped": {"type": "integer"}}
                }}}},
                "400": {"description": "Aucune question sélectionnée"}
            }
        }},
        "/api/question_bank/duplicates": {"get": {
            "tags": ["Intelligence Artificielle"],
            "summary": "Lister les paires de questions quasi-identiques dans la banque",
            "description": "Retourne toutes les paires de questions avec une similarité ≥ 95%.",
            "responses": {"200": {"description": "Paires en doublon", "content": {"application/json": {"schema": {
                "type": "object",
                "properties": {
                    "duplicates": {"type": "array", "items": {
                        "type": "object",
                        "properties": {
                            "q1":         {"type": "object", "properties": {"id": {"type": "integer"}, "title": {"type": "string"}}},
                            "q2":         {"type": "object", "properties": {"id": {"type": "integer"}, "title": {"type": "string"}}},
                            "similarity": {"type": "number", "description": "Pourcentage, ex: 96.5"}
                        }
                    }},
                    "count": {"type": "integer"}
                }
            }}}}}
        }},
        "/api/question_bank/duplicates/auto-clean": {"post": {
            "tags": ["Intelligence Artificielle"],
            "summary": "Nettoyer automatiquement les doublons de la banque",
            "description": "Supprime automatiquement les doublons détectés (≥95% de similarité) — conserve la question la plus ancienne de chaque paire, supprime la plus récente. Répété tant que de nouvelles paires apparaissent (cas A≈B≈C), avec une limite de sécurité de 10 passes.",
            "responses": {"200": {"description": "Doublons supprimés", "content": {"application/json": {"schema": {
                "type": "object",
                "properties": {
                    "success":       {"type": "boolean"},
                    "deleted_count": {"type": "integer"},
                    "deleted":       {"type": "array", "items": {"type": "object", "properties": {"id": {"type": "integer"}, "title": {"type": "string"}}}}
                }
            }}}}}
        }},
        "/api/question_bank/check_duplicate": {"post": {
            "tags": ["Intelligence Artificielle"],
            "summary": "Vérifier si un contenu est un doublon dans la banque",
            "description": "Vérifie si un contenu est similaire à ≥95% d'une question déjà en banque — utilisé avant sauvegarde/édition.",
            "requestBody": {"required": True, "content": {"application/json": {"schema": {
                "type": "object", "required": ["content"],
                "properties": {
                    "content": {"type": "string"},
                    "id":      {"type": "integer", "description": "ID de la question en cours d'édition, à exclure de la comparaison"}
                }
            }}}},
            "responses": {"200": {"description": "Résultat de la vérification", "content": {"application/json": {"schema": {
                "type": "object",
                "properties": {
                    "duplicates":   {"type": "array", "items": {"type": "object", "properties": {"id": {"type": "integer"}, "title": {"type": "string"}, "similarity": {"type": "number"}}}},
                    "is_duplicate": {"type": "boolean"}
                }
            }}}}}
        }},
        "/api/question_bank/assemble": {"post": {
            "tags": ["Intelligence Artificielle"], "summary": "Assembler un sujet depuis la banque de questions",
            "requestBody": {"required": True, "content": {"application/json": {"schema": {
                "type": "object", "required": ["question_ids"],
                "properties": {
                    "question_ids": {"type": "array", "items": {"type": "integer"}},
                    "title":        {"type": "string"},
                    "ec_id":        {"type": "integer"}
                }
            }}}},
            "responses": {"201": {"description": "Sujet assemblé"}}
        }},

        # ══════════════════════════════════════════════════════════════════════
        # NOTIFICATIONS
        # ══════════════════════════════════════════════════════════════════════

        "/api/notifications": {"get": {
            "tags": ["Tableaux de bord"], "summary": "Notifications non lues du professeur connecté",
            "responses": {"200": {"description": "Notifications", "content": {"application/json": {"schema": {
                "type": "array", "items": {
                    "type": "object",
                    "properties": {
                        "id":         {"type": "integer"},
                        "type":       {"type": "string"},
                        "message":    {"type": "string"},
                        "read":       {"type": "boolean"},
                        "created_at": {"type": "string", "format": "date-time"}
                    }
                }
            }}}}}
        }},
        "/api/notifications/mark-read": {"put": {
            "tags": ["Tableaux de bord"], "summary": "Marquer les notifications comme lues",
            "requestBody": {"content": {"application/json": {"schema": {
                "type": "object",
                "properties": {"ids": {"type": "array", "items": {"type": "integer"}, "description": "IDs à marquer (vide = toutes)"}}
            }}}},
            "responses": {"200": {"description": "Notifications mises à jour"}}
        }},
        "/api/notifications/poll": {"get": {
            "tags": ["Tableaux de bord"],
            "summary": "Long-polling des notifications en temps réel (Redis Pub/Sub)",
            "description": "Attend au plus 25s un événement sur le canal Redis de l'utilisateur connecté. Le client doit se reconnecter immédiatement après chaque réponse (événement reçu ou timeout 204) — chaque connexion occupe un thread Gunicorn gthread pendant max 25s puis le libère.",
            "responses": {
                "200": {"description": "Événement reçu", "content": {"application/json": {"schema": {
                    "type": "object",
                    "properties": {
                        "has_event": {"type": "boolean", "example": True},
                        "event": {"type": "object", "properties": {"type": {"type": "string"}, "title": {"type": "string"}, "message": {"type": "string"}}}
                    }
                }}}},
                "204": {"description": "Timeout sans événement — le client doit se reconnecter"}
            }
        }},

        # ══════════════════════════════════════════════════════════════════════
        # RELEVÉS — Routes manquantes
        # ══════════════════════════════════════════════════════════════════════

        "/api/transcripts/bulk-pdf": {"get": {
            "tags": ["Relevés de notes"], "summary": "Télécharger tous les relevés en un seul ZIP/PDF",
            "responses": {
                "200": {"description": "Archive", "content": {"application/zip": {"schema": {"type": "string", "format": "binary"}}}}
            }
        }},
        "/api/transcripts/{tid}": {"delete": {
            "tags": ["Relevés de notes"], "summary": "Supprimer un relevé de notes (admin)",
            "parameters": [{"name": "tid", "in": "path", "required": True, "schema": {"type": "integer"}}],
            "responses": {"200": {"description": "Relevé supprimé"}, "404": {"$ref": "#/components/responses/NotFound"}}
        }},
        "/api/transcripts/{tid}/publish": {"put": {
            "tags": ["Relevés de notes"], "summary": "Publier un relevé (le rendre visible à l'étudiant)",
            "parameters": [{"name": "tid", "in": "path", "required": True, "schema": {"type": "integer"}}],
            "responses": {"200": {"description": "Relevé publié"}}
        }},

        # ══════════════════════════════════════════════════════════════════════
        # BIOMÉTRIE
        # ══════════════════════════════════════════════════════════════════════

        "/api/biometric/status": {"get": {
            "tags": ["Biométrie"], "summary": "Statut d'inscription biométrique de l'utilisateur connecté",
            "responses": {"200": {"description": "Statut", "content": {"application/json": {"schema": {
                "type": "object",
                "properties": {
                    "enrolled": {"type": "boolean"},
                    "method": {"type": "string", "enum": ["face"], "nullable": True},
                    "photo_url": {"type": "string", "nullable": True}
                }
            }}}}}
        }},
        "/api/biometric/enroll/face": {"post": {
            "tags": ["Biométrie"], "summary": "Inscrire (ou remplacer) sa reconnaissance faciale",
            "description": "Le descripteur (128 floats face-api.js) est calculé côté client et envoyé au serveur, qui stocke aussi la photo de référence en S3 pour audit.",
            "requestBody": {"required": True, "content": {"application/json": {"schema": {
                "type": "object", "required": ["descriptor"],
                "properties": {
                    "descriptor": {"type": "array", "items": {"type": "number"}, "description": "128 floats face-api.js"},
                    "image_data": {"type": "string", "description": "dataURL JPEG — photo de référence"}
                }
            }}}},
            "responses": {"200": {"description": "Inscrit"}, "400": {"description": "Descripteur invalide"}}
        }},
        "/api/biometric/verify/face": {"post": {
            "tags": ["Biométrie"], "summary": "Vérifier son identité par reconnaissance faciale",
            "description": "Compare le descripteur envoyé à celui enregistré (distance euclidienne, seuil 0.55). En cas de correspondance, pose un flag Redis à usage unique (180s) consommé par POST /api/online_exams/{id}/start.",
            "requestBody": {"required": True, "content": {"application/json": {"schema": {
                "type": "object", "required": ["descriptor"],
                "properties": {"descriptor": {"type": "array", "items": {"type": "number"}}}
            }}}},
            "responses": {
                "200": {"description": "Résultat de la comparaison", "content": {"application/json": {"schema": {
                    "type": "object", "properties": {"match": {"type": "boolean"}, "distance": {"type": "number"}}
                }}}},
                "404": {"description": "Aucune inscription faciale trouvée"}
            }
        }},
        "/api/biometric/fallback/call_request": {"post": {
            "tags": ["Biométrie"], "summary": "Demander un appel de vérification manuelle (repli après échecs répétés)",
            "requestBody": {"required": True, "content": {"application/json": {"schema": {
                "type": "object", "required": ["exam_id"], "properties": {"exam_id": {"type": "integer"}}
            }}}},
            "responses": {"200": {"description": "Surveillant(s)/superviseur/professeur notifiés"}}
        }},
        "/api/biometric/fallback/private_token": {"get": {
            "tags": ["Biométrie"], "summary": "Token LiveKit pour la room d'appel de vérification biométrique",
            "description": "Room dédiée biometric-{student_id}-{exam_id} (aucune tentative d'examen n'existe encore à ce stade).",
            "parameters": [
                {"name": "exam_id", "in": "query", "required": True, "schema": {"type": "integer"}},
                {"name": "student_id", "in": "query", "required": False, "schema": {"type": "integer"}, "description": "Requis pour le staff ; déduit automatiquement pour l'étudiant"}
            ],
            "responses": {"200": {"description": "Token LiveKit"}, "503": {"description": "LiveKit non configuré"}}
        }},
        "/api/biometric/fallback/manual_verify": {"post": {
            "tags": ["Biométrie"], "summary": "Valider manuellement l'identité d'un étudiant (staff, pendant l'appel)",
            "requestBody": {"required": True, "content": {"application/json": {"schema": {
                "type": "object", "required": ["exam_id", "student_id"],
                "properties": {"exam_id": {"type": "integer"}, "student_id": {"type": "integer"}}
            }}}},
            "responses": {"200": {"description": "Identité validée — l'étudiant peut réessayer d'accéder à l'examen"}, "403": {"description": "Non habilité pour cet examen"}}
        }},

        # ══════════════════════════════════════════════════════════════════════
        # SYSTÈME
        # ══════════════════════════════════════════════════════════════════════

        "/api/health": {"get": {
            "tags": ["Système"], "summary": "Health check (sans authentification)",
            "description": "Pour load balancer / monitoring — vérifie la connectivité base de données et Redis. Exempté du rate-limiting.",
            "security": [],
            "responses": {
                "200": {"description": "Tous les checks OK", "content": {"application/json": {"schema": {
                    "type": "object",
                    "properties": {
                        "status": {"type": "string", "enum": ["ok", "degraded"]},
                        "checks": {"type": "object", "properties": {
                            "database": {"type": "string", "enum": ["ok", "error"]},
                            "redis":    {"type": "string", "enum": ["ok", "unavailable"]}
                        }}
                    }
                }}}},
                "503": {"description": "Au moins un check en échec (status=degraded)"}
            }
        }},
    }
}

# ─────────────────────────────────────────────────────────────────────────────
# Enrichissement automatique — exemples JSON pour TOUTES les réponses
# ─────────────────────────────────────────────────────────────────────────────

_SCHEMA_EXAMPLES = {
    "User": {
        "id": 7, "email": "amadou.diallo@unchk.edu.sn", "full_name": "Amadou Diallo",
        "role": "student", "is_active": True, "has_email": True,
        "created_at": "2026-09-01T08:00:00Z"
    },
    "Subject": {
        "id": 12, "title": "Examen de Réseaux L3 — Session 2026",
        "content": "Partie 1 : Protocoles TCP/IP\nQ1. Expliquez le mécanisme de TCP Three-Way Handshake.",
        "rubric": "Q1 : 4 pts | Q2 : 6 pts | Q3 : 10 pts",
        "ec_id": 3, "creator_id": 5, "created_at": "2026-10-15T10:30:00Z", "papers_count": 28
    },
    "StudentPaper": {
        "id": 34, "subject_id": 12, "student_id": 7, "student_name": "Amadou Diallo",
        "score": 14.5,
        "grade": "Bonne maîtrise des protocoles. Q3 partiellement réussie — revoir la segmentation.",
        "filename": "copie_amadou_diallo.pdf",
        "corrected_at": "2026-10-20T14:22:00Z", "email_sent": True
    },
    "OnlineExam": {
        "id": 5, "title": "Examen Final Réseaux L3", "subject_id": 12, "subject_title": "Réseaux et Télécommunications",
        "duration_minutes": 150, "status": "active",
        "start_time": "2026-11-10T09:00:00Z", "end_time": "2026-11-10T11:30:00Z",
        "max_tab_switches": 2, "enable_copy_paste": False, "enable_right_click": False,
        "enable_file_download": False, "results_published": False,
        "creator_name": "Pr. Fatou Ndiaye", "created_at": "2026-11-01T00:00:00Z",
        "is_active": True, "attempts_count": 24
    },
    "ExamAttempt": {
        "id": 88, "exam_id": 5, "student_id": 7, "student_name": "Amadou Diallo",
        "status": "in_progress", "score": None, "risk_score": 15,
        "tab_switches": 1, "warnings_count": 0,
        "started_at": "2026-11-10T09:02:00Z", "submitted_at": None
    },
    "Formation": {
        "id": 1, "name": "Licence Informatique", "code": "LI",
        "description": "Formation Licence 3 en Informatique", "duration_years": 3
    },
    "Semester": {"id": 2, "name": "Semestre 1", "formation_id": 1, "order": 1},
    "UE": {
        "id": 4, "name": "Réseaux et Télécommunications", "code": "RT301",
        "semester_id": 2, "credits": 6, "coefficient": 2
    },
    "EC": {
        "id": 8, "name": "Protocoles TCP/IP", "code": "RT301-01", "ue_id": 4,
        "coefficient": 1, "cm": 24, "td": 12, "tp": 12, "tpe": 0, "vht": 48, "is_active": True
    },
    "Reclamation": {
        "id": 3, "paper_id": 34,
        "reason": "La question 2 a été mal évaluée — ma réponse sur le routage OSPF est correcte.",
        "status": "pending", "response": None,
        "ia_proposed_status": None, "ia_proposed_score": None,
        "created_at": "2026-10-22T10:00:00Z"
    },
    "GradeTranscript": {
        "id": 1, "student_id": 7, "student_name": "Amadou Diallo",
        "semester_id": 2, "semester_name": "Semestre 1",
        "formation_name": "Licence Informatique", "gpa": 13.4,
        "total_credits": 30, "obtained_credits": 28, "validated": True,
        "generated_at": "2026-12-15T09:00:00Z"
    },
    "AgentAlert": {
        "exam_id": 5, "exam_title": "Examen Final Réseaux L3", "attempt_id": 88,
        "student_name": "Amadou Diallo", "risk_score": 75, "level": "ALERTE",
        "no_face": 3, "multi_face": 1, "tab_switches": 2,
        "ai_note": "Comportement suspect — visage absent 3 fois consécutives.",
        "timestamp": "2026-11-10T09:45:00Z", "read": False
    },
    "ExamIncident": {
        "id": 10, "attempt_id": 88, "student_name": "Amadou Diallo",
        "event_type": "tab_switch", "severity": "medium",
        "timestamp": "2026-11-10T09:30:00Z"
    },
    "Error": {"error": "Message d'erreur détaillé"},
    "Success": {"success": True, "message": "Opération effectuée avec succès"},
}

_STATUS_EXAMPLES = {
    "200": {"success": True, "message": "Opération effectuée avec succès"},
    "201": {"success": True, "id": 42, "message": "Ressource créée avec succès"},
    "400": {"error": "Requête invalide — paramètre manquant ou valeur incorrecte"},
    "401": {"error": "Token manquant, invalide ou expiré"},
    "403": {"error": "Droits insuffisants pour cette action"},
    "404": {"error": "Ressource introuvable"},
    "409": {"error": "Conflit — cette ressource existe déjà"},
}


def _type_default(t):
    """Valeur par défaut selon le type JSON."""
    return {"integer": 1, "number": 1.5, "boolean": True, "array": [], "object": {}}.get(t, "valeur")


def _example_from_props(props):
    """Construit un dict exemple depuis les properties d'un schéma inline."""
    out = {}
    for k, v in props.items():
        if "example" in v:
            out[k] = v["example"]
        elif "default" in v:
            out[k] = v["default"]
        elif v.get("type") == "array":
            inner = v.get("items", {})
            inner_ref = inner.get("$ref", "").split("/")[-1]
            out[k] = [_SCHEMA_EXAMPLES[inner_ref]] if inner_ref in _SCHEMA_EXAMPLES else []
        elif v.get("type") == "object" and "properties" in v:
            out[k] = _example_from_props(v["properties"])
        elif "$ref" in v:
            name = v["$ref"].split("/")[-1]
            out[k] = _SCHEMA_EXAMPLES.get(name, {})
        else:
            out[k] = _type_default(v.get("type", "string"))
    return out


def _enrich_spec(spec):
    """Injecte automatiquement des exemples JSON dans toutes les réponses API."""
    for _path, methods in spec["paths"].items():
        for _method, operation in methods.items():
            if not isinstance(operation, dict):
                continue
            for code, resp in operation.get("responses", {}).items():
                if not isinstance(resp, dict) or "$ref" in resp:
                    continue

                content = resp.get("content", {})

                # Réponse sans content du tout → exemple générique par code HTTP
                if not content:
                    resp["content"] = {
                        "application/json": {
                            "example": _STATUS_EXAMPLES.get(str(code), {"success": True})
                        }
                    }
                    continue

                # Réponse JSON sans example → injecter exemple concret
                aj = content.get("application/json", {})
                if not aj or "example" in aj:
                    continue

                schema = aj.get("schema", {})
                ref    = schema.get("$ref", "")
                name   = ref.split("/")[-1] if ref else ""

                if name in _SCHEMA_EXAMPLES:
                    # Schéma par référence connu
                    aj["example"] = _SCHEMA_EXAMPLES[name]
                elif schema.get("type") == "array":
                    # Tableau : exemple = liste avec un élément
                    items    = schema.get("items", {})
                    item_ref = items.get("$ref", "").split("/")[-1]
                    if item_ref in _SCHEMA_EXAMPLES:
                        aj["example"] = [_SCHEMA_EXAMPLES[item_ref]]
                    elif items.get("type") == "object" and "properties" in items:
                        aj["example"] = [_example_from_props(items["properties"])]
                    else:
                        aj["example"] = []
                elif schema.get("type") == "object" and "properties" in schema:
                    # Schéma inline avec properties
                    aj["example"] = _example_from_props(schema["properties"])
                else:
                    # Fallback générique
                    aj["example"] = _STATUS_EXAMPLES.get(str(code), {"success": True})

    return spec


OPENAPI_SPEC = _enrich_spec(OPENAPI_SPEC)

# Calculé depuis OPENAPI_SPEC lui-même (jamais à mettre à jour à la main) --
# affiché dans les badges Swagger UI / ReDoc ci-dessous via {ENDPOINT_COUNT}.
_ENDPOINT_COUNT = sum(
    len([k for k in methods if k in ('get', 'post', 'put', 'delete', 'patch')])
    for methods in OPENAPI_SPEC['paths'].values()
)

# ─────────────────────────────────────────────────────────────────────────────
# HTML Swagger UI & ReDoc
# ─────────────────────────────────────────────────────────────────────────────

_CEI_SVG_LOGO = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 44 44" fill="none" aria-hidden="true">
  <rect width="44" height="44" rx="9" fill="rgba(255,255,255,0.12)" stroke="rgba(255,255,255,0.22)" stroke-width="1"/>
  <!-- Toque académique — mortarboard -->
  <path d="M22 9 L36 16.5 L22 24 L8 16.5 Z" fill="white"/>
  <path d="M13 20 L13 29.5 C17.5 34 26.5 34 31 29.5 L31 20" fill="rgba(255,255,255,0.18)" stroke="white" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>
  <line x1="36" y1="16.5" x2="36" y2="26" stroke="white" stroke-width="2" stroke-linecap="round"/>
  <circle cx="36" cy="28.5" r="3" fill="#10b981"/>
</svg>"""

_SWAGGER_HTML = """<!DOCTYPE html>
<html lang="fr">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>CEI — Documentation API</title>
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui.css">
  <style>
    *, *::before, *::after { box-sizing: border-box; }

    html, body {
      margin: 0; padding: 0;
      background: #f1f5f9;
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Arial, sans-serif;
      color: #1e293b;
    }

    /* ═══════════════════════════════════
       HEADER PERSONNALISÉ
    ═══════════════════════════════════ */
    .cei-header {
      background: #1e3a8a;
      border-bottom: 3px solid #1d4ed8;
      padding: 0;
      position: sticky;
      top: 0;
      z-index: 1000;
      box-shadow: 0 2px 12px rgba(0,0,0,0.22);
    }
    .cei-header-inner {
      max-width: 1400px;
      margin: 0 auto;
      padding: 0 24px;
      height: 60px;
      display: flex;
      align-items: center;
      gap: 16px;
    }
    .cei-logo-wrap {
      display: flex;
      align-items: center;
      gap: 12px;
      text-decoration: none;
      flex-shrink: 0;
    }
    .cei-logo-wrap svg { width: 36px; height: 36px; }
    .cei-brand-name {
      font-size: 16px;
      font-weight: 800;
      color: #ffffff;
      letter-spacing: .2px;
      line-height: 1.1;
    }
    .cei-brand-sub {
      font-size: 11px;
      color: rgba(255,255,255,.6);
      font-weight: 500;
      letter-spacing: .4px;
      text-transform: uppercase;
    }
    .cei-header-divider {
      width: 1px;
      height: 32px;
      background: rgba(255,255,255,.18);
      flex-shrink: 0;
    }
    .cei-header-meta {
      display: flex;
      align-items: center;
      gap: 8px;
      flex: 1;
    }
    .cei-badge {
      display: inline-flex;
      align-items: center;
      padding: 3px 10px;
      border-radius: 99px;
      font-size: 11.5px;
      font-weight: 700;
      letter-spacing: .3px;
    }
    .cei-badge-version { background: #1d4ed8; color: #fff; }
    .cei-badge-oas     { background: rgba(255,255,255,.12); color: rgba(255,255,255,.85); border: 1px solid rgba(255,255,255,.2); }
    .cei-badge-count   { background: rgba(16,185,129,.18); color: #6ee7b7; border: 1px solid rgba(16,185,129,.3); }
    .cei-header-nav {
      display: flex;
      align-items: center;
      gap: 4px;
      margin-left: auto;
    }
    .cei-nav-link {
      padding: 6px 14px;
      border-radius: 6px;
      font-size: 13px;
      font-weight: 600;
      color: rgba(255,255,255,.75);
      text-decoration: none;
      border: 1px solid transparent;
      transition: all .15s;
    }
    .cei-nav-link:hover { background: rgba(255,255,255,.1); color: #fff; }
    .cei-nav-link.active { background: rgba(255,255,255,.15); color: #fff; border-color: rgba(255,255,255,.25); }

    /* ═══════════════════════════════════
       MASQUER TOPBAR SWAGGER PAR DÉFAUT
    ═══════════════════════════════════ */
    .swagger-ui .topbar { display: none !important; }

    /* ═══════════════════════════════════
       ZONE PRINCIPALE
    ═══════════════════════════════════ */
    .swagger-ui { background: #f1f5f9; font-size: 15px; }
    .swagger-ui .wrapper { padding: 0 20px !important; }

    /* ═══════════════════════════════════
       BLOC INFO — REDESIGN COMPLET
    ═══════════════════════════════════ */
    .swagger-ui .info {
      background: #ffffff;
      border-radius: 0 0 12px 12px;
      border-top: none;
      border: 1px solid #e2e8f0;
      border-top: none;
      padding: 28px 32px 24px;
      margin: 0 0 20px;
      box-shadow: 0 1px 6px rgba(0,0,0,0.06);
    }
    /* Accent bleu sur le bord gauche */
    .swagger-ui .info::before {
      content: '';
      display: block;
      position: absolute;
      left: 0; top: 0; bottom: 0;
      width: 4px;
      background: #1d4ed8;
      border-radius: 4px 0 0 4px;
    }
    .swagger-ui .info { position: relative; }

    .swagger-ui .info hgroup.main { margin-bottom: 16px; }
    .swagger-ui .info .title {
      font-size: 22px !important;
      font-weight: 800 !important;
      color: #1e3a8a !important;
      letter-spacing: -.3px;
      line-height: 1.3 !important;
    }
    /* Masquer les badges version/OAS générés par Swagger UI (on a les nôtres) */
    .swagger-ui .info .title small { display: none !important; }
    .swagger-ui .info .title small.version-stamp { display: none !important; }

    .swagger-ui .info p,
    .swagger-ui .info li,
    .swagger-ui .renderedMarkdown p {
      font-size: 14.5px !important;
      line-height: 1.75 !important;
      color: #475569 !important;
    }
    .swagger-ui .info h2,
    .swagger-ui .renderedMarkdown h2 {
      font-size: 15px !important;
      font-weight: 700 !important;
      color: #1e293b !important;
      margin: 20px 0 8px !important;
      padding-bottom: 4px !important;
      border-bottom: 1.5px solid #e2e8f0 !important;
    }
    .swagger-ui .renderedMarkdown table {
      border-collapse: collapse !important;
      font-size: 13.5px !important;
      width: auto !important;
      border-radius: 6px !important;
      overflow: hidden !important;
      border: 1px solid #e2e8f0 !important;
      margin: 8px 0 16px !important;
    }
    .swagger-ui .renderedMarkdown th {
      background: #f1f5f9 !important;
      color: #1e293b !important;
      font-weight: 700 !important;
      padding: 8px 14px !important;
      text-align: left !important;
      border-bottom: 1.5px solid #e2e8f0 !important;
    }
    .swagger-ui .renderedMarkdown td {
      padding: 7px 14px !important;
      color: #475569 !important;
      border-bottom: 1px solid #f1f5f9 !important;
    }
    .swagger-ui .renderedMarkdown code {
      background: #eff6ff !important;
      color: #1d4ed8 !important;
      padding: 2px 6px !important;
      border-radius: 4px !important;
      font-size: 13px !important;
      font-family: 'SFMono-Regular', Menlo, Consolas, monospace !important;
    }
    .swagger-ui .info .base-url {
      font-size: 13px !important;
      color: #64748b !important;
      background: #f8fafc !important;
      border: 1px solid #e2e8f0 !important;
      border-radius: 6px !important;
      padding: 5px 12px !important;
      display: inline-block !important;
      margin-top: 8px !important;
    }

    /* Contact links in info */
    .swagger-ui .info a { color: #2563eb !important; }

    /* ═══════════════════════════════════
       FILTRE / RECHERCHE
    ═══════════════════════════════════ */
    .swagger-ui .filter-container { padding: 0 0 12px !important; }
    .swagger-ui .filter input {
      font-size: 14px !important;
      padding: 9px 14px !important;
      border-radius: 8px !important;
      border: 1.5px solid #cbd5e1 !important;
      background: #ffffff !important;
      color: #1e293b !important;
      width: 100% !important;
      box-shadow: 0 1px 3px rgba(0,0,0,0.05) !important;
    }
    .swagger-ui .filter input:focus {
      border-color: #2563eb !important;
      outline: none !important;
      box-shadow: 0 0 0 3px rgba(37,99,235,.15) !important;
    }

    /* ═══════════════════════════════════
       TAGS / GROUPES
    ═══════════════════════════════════ */
    .swagger-ui .opblock-tag {
      font-size: 17px !important;
      font-weight: 700 !important;
      color: #1e3a8a !important;
      border-bottom: 2px solid #dbeafe !important;
      padding: 12px 4px 8px !important;
      margin-top: 8px !important;
    }
    .swagger-ui .opblock-tag:hover { background: #f0f9ff !important; border-radius: 6px !important; }
    .swagger-ui .opblock-tag-section h3 { font-size: 17px !important; }
    .swagger-ui .opblock-tag small {
      font-size: 13px !important;
      color: #64748b !important;
      font-weight: 400 !important;
    }

    /* ═══════════════════════════════════
       BLOCS DE ROUTES
    ═══════════════════════════════════ */
    .swagger-ui .opblock {
      border-radius: 8px !important;
      margin-bottom: 5px !important;
      box-shadow: 0 1px 3px rgba(0,0,0,0.06) !important;
      border-width: 1px !important;
    }
    .swagger-ui .opblock.is-open { box-shadow: 0 2px 8px rgba(0,0,0,0.1) !important; }
    .swagger-ui .opblock-summary { padding: 10px 16px !important; align-items: center !important; }
    .swagger-ui .opblock-summary-method {
      font-size: 12px !important;
      font-weight: 800 !important;
      min-width: 70px !important;
      text-align: center !important;
      border-radius: 5px !important;
      padding: 5px 0 !important;
      letter-spacing: .5px;
    }
    .swagger-ui .opblock-summary-path {
      font-size: 14.5px !important;
      font-weight: 600 !important;
      color: #1e293b !important;
      font-family: 'SFMono-Regular', Menlo, Consolas, monospace !important;
    }
    .swagger-ui .opblock-summary-description {
      font-size: 13.5px !important;
      color: #64748b !important;
    }

    /* ── Couleurs méthodes HTTP — sans violet ── */
    .swagger-ui .opblock-get    .opblock-summary-method { background: #0369a1 !important; }
    .swagger-ui .opblock-get    { border-color: #bae6fd !important; }
    .swagger-ui .opblock-post   .opblock-summary-method { background: #15803d !important; }
    .swagger-ui .opblock-post   { border-color: #bbf7d0 !important; }
    .swagger-ui .opblock-put    .opblock-summary-method { background: #b45309 !important; }
    .swagger-ui .opblock-put    { border-color: #fde68a !important; }
    .swagger-ui .opblock-patch  .opblock-summary-method { background: #0f766e !important; }
    .swagger-ui .opblock-patch  { border-color: #99f6e4 !important; }
    .swagger-ui .opblock-delete .opblock-summary-method { background: #b91c1c !important; }
    .swagger-ui .opblock-delete { border-color: #fecaca !important; }

    /* ═══════════════════════════════════
       INTÉRIEUR DÉPLIÉ
    ═══════════════════════════════════ */
    .swagger-ui .opblock-body {
      background: #ffffff !important;
      border-radius: 0 0 8px 8px !important;
      padding: 18px 20px !important;
    }
    .swagger-ui .opblock-section-header {
      background: #f8fafc !important;
      border-radius: 6px !important;
      padding: 8px 12px !important;
      margin-bottom: 12px !important;
    }
    .swagger-ui .opblock-section-header h4 {
      font-size: 13px !important;
      font-weight: 700 !important;
      color: #374151 !important;
      text-transform: uppercase !important;
      letter-spacing: .6px !important;
    }

    /* ═══════════════════════════════════
       PARAMÈTRES
    ═══════════════════════════════════ */
    .swagger-ui table thead tr th,
    .swagger-ui .parameters-col_name,
    .swagger-ui .parameter__name {
      font-size: 13.5px !important;
      color: #1e293b !important;
      font-weight: 700 !important;
    }
    .swagger-ui table tbody tr td,
    .swagger-ui .parameter__type,
    .swagger-ui .parameter__in {
      font-size: 13.5px !important;
      color: #475569 !important;
    }
    .swagger-ui .parameter__name.required::after { color: #dc2626 !important; }
    .swagger-ui .parameter__in {
      background: #f1f5f9 !important;
      border-radius: 4px !important;
      padding: 1px 6px !important;
      font-size: 12px !important;
    }

    /* ═══════════════════════════════════
       CHAMPS TRY IT OUT
    ═══════════════════════════════════ */
    .swagger-ui input[type=text],
    .swagger-ui textarea,
    .swagger-ui select {
      font-size: 14px !important;
      border: 1.5px solid #cbd5e1 !important;
      border-radius: 6px !important;
      padding: 8px 12px !important;
      background: #ffffff !important;
      color: #1e293b !important;
    }
    .swagger-ui input[type=text]:focus,
    .swagger-ui textarea:focus {
      border-color: #2563eb !important;
      box-shadow: 0 0 0 3px rgba(37,99,235,.12) !important;
      outline: none !important;
    }

    /* ═══════════════════════════════════
       BOUTONS
    ═══════════════════════════════════ */
    .swagger-ui .btn {
      font-size: 13px !important;
      font-weight: 600 !important;
      border-radius: 6px !important;
    }
    .swagger-ui .btn.execute {
      background: #1d4ed8 !important;
      color: #ffffff !important;
      font-size: 14px !important;
      padding: 9px 24px !important;
      border: none !important;
    }
    .swagger-ui .btn.execute:hover { background: #1e3a8a !important; }
    .swagger-ui .btn.cancel { color: #dc2626 !important; border-color: #fca5a5 !important; }
    .swagger-ui .try-out__btn { font-weight: 700 !important; }
    .swagger-ui .auth-wrapper .authorize {
      border-color: #1d4ed8 !important;
      color: #1d4ed8 !important;
    }
    .swagger-ui .btn.authorize svg { fill: #1d4ed8 !important; }

    /* ═══════════════════════════════════
       RÉPONSES
    ═══════════════════════════════════ */
    .swagger-ui .responses-inner h4,
    .swagger-ui .response-col_status { font-size: 14px !important; font-weight: 700 !important; }
    .swagger-ui .response-col_description { font-size: 14px !important; color: #475569 !important; }
    .swagger-ui .highlight-code pre,
    .swagger-ui .microlight {
      font-size: 13px !important;
      line-height: 1.65 !important;
      background: #f8fafc !important;
      border: 1px solid #e2e8f0 !important;
      border-radius: 6px !important;
      padding: 14px !important;
      color: #1e293b !important;
    }
    .swagger-ui .response-col_status .response-undocumented { color: #94a3b8 !important; }

    /* ═══════════════════════════════════
       SCHÉMAS / MODELS
    ═══════════════════════════════════ */
    .swagger-ui section.models {
      background: #ffffff !important;
      border: 1px solid #e2e8f0 !important;
      border-radius: 10px !important;
      padding: 4px 0 !important;
      margin-top: 20px !important;
    }
    .swagger-ui section.models h4 { font-size: 15px !important; font-weight: 700 !important; color: #1e3a8a !important; }
    .swagger-ui .model-title { font-size: 14px !important; font-weight: 700 !important; color: #1e3a8a !important; }
    .swagger-ui .model { font-size: 14px !important; color: #475569 !important; }
    .swagger-ui .prop-type { color: #0369a1 !important; font-weight: 600 !important; }
    .swagger-ui .prop-format { color: #64748b !important; font-size: 12px !important; }
  </style>
</head>
<body>

<!-- ═══ HEADER PERSONNALISÉ ═══ -->
<header class="cei-header">
  <div class="cei-header-inner">
    <div class="cei-logo-wrap">
      """ + _CEI_SVG_LOGO + """
      <div>
        <div class="cei-brand-name">Centre d'Examen Intelligent</div>
        <div class="cei-brand-sub">UNCHK &mdash; VisioPLUS</div>
      </div>
    </div>
    <div class="cei-header-divider"></div>
    <div class="cei-header-meta">
      <span class="cei-badge cei-badge-version">v2.1</span>
      <span class="cei-badge cei-badge-oas">OpenAPI 3.0</span>
      <span class="cei-badge cei-badge-count">{ENDPOINT_COUNT} endpoints</span>
    </div>
    <nav class="cei-header-nav">
      <a class="cei-nav-link active" href="/api/docs">Swagger UI</a>
      <a class="cei-nav-link" href="/api/docs/redoc">ReDoc</a>
      <a class="cei-nav-link" href="/api/docs/openapi.json">JSON</a>
    </nav>
  </div>
</header>

<div id="swagger-ui"></div>

<script src="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-bundle.js"></script>
<script>
  SwaggerUIBundle({
    url: '/api/docs/openapi.json',
    dom_id: '#swagger-ui',
    presets: [SwaggerUIBundle.presets.apis, SwaggerUIBundle.SwaggerUIStandalonePreset],
    layout: 'BaseLayout',
    deepLinking: true,
    filter: true,
    tryItOutEnabled: true,
    persistAuthorization: true,
    displayRequestDuration: true,
    docExpansion: 'none',
    defaultModelsExpandDepth: 2,
    syntaxHighlight: { activated: true, theme: 'agate' },
  });
</script>
</body>
</html>"""

_REDOC_HTML = """<!DOCTYPE html>
<html lang="fr">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>CEI — Documentation API</title>
  <style>
    *, *::before, *::after { box-sizing: border-box; }
    html, body { margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Arial, sans-serif; }

    /* ════════════ HEADER ════════════ */
    .cei-header {
      background: #1e3a8a;
      border-bottom: 3px solid #1d4ed8;
      position: fixed;
      top: 0; left: 0; right: 0;
      height: 60px;
      z-index: 9999;
      box-shadow: 0 2px 16px rgba(0,0,0,0.28);
    }
    .cei-header-inner {
      padding: 0 24px;
      height: 60px;
      display: flex;
      align-items: center;
      gap: 16px;
    }
    .cei-logo-wrap {
      display: flex; align-items: center; gap: 12px;
      text-decoration: none; flex-shrink: 0;
    }
    .cei-logo-wrap svg { width: 36px; height: 36px; }
    .cei-brand-name {
      font-size: 15.5px; font-weight: 800; color: #fff;
      letter-spacing: .1px; line-height: 1.15;
    }
    .cei-brand-sub {
      font-size: 10.5px; color: rgba(255,255,255,.55);
      font-weight: 500; letter-spacing: .5px; text-transform: uppercase;
    }
    .cei-divider { width: 1px; height: 30px; background: rgba(255,255,255,.18); flex-shrink: 0; }
    .cei-meta { display: flex; align-items: center; gap: 7px; flex: 1; }
    .cei-badge {
      display: inline-flex; align-items: center;
      padding: 3px 10px; border-radius: 99px;
      font-size: 11px; font-weight: 700; letter-spacing: .4px;
    }
    .b-v  { background: #1d4ed8; color: #fff; }
    .b-o  { background: rgba(255,255,255,.1); color: rgba(255,255,255,.8); border: 1px solid rgba(255,255,255,.2); }
    .b-e  { background: rgba(16,185,129,.15); color: #6ee7b7; border: 1px solid rgba(16,185,129,.3); }
    .cei-nav { display: flex; align-items: center; gap: 4px; margin-left: auto; }
    .n-link {
      padding: 6px 14px; border-radius: 6px; font-size: 13px; font-weight: 600;
      color: rgba(255,255,255,.7); text-decoration: none;
      border: 1px solid transparent; transition: background .15s, color .15s;
    }
    .n-link:hover { background: rgba(255,255,255,.1); color: #fff; }
    .n-link.on { background: rgba(255,255,255,.14); color: #fff; border-color: rgba(255,255,255,.22); }

    /* Décalage pour le header fixe */
    body > redoc { display: block; margin-top: 60px; }

    /* ════════════ REDOC OVERRIDES ════════════ */
    /* Sidebar */
    [data-role="search-input"] { border-radius: 6px !important; }

    /* Filet séparateur entre sections */
    .redoc-wrap { padding-top: 0 !important; }
  </style>
</head>
<body>

<header class="cei-header">
  <div class="cei-header-inner">
    <div class="cei-logo-wrap">
      <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 44 44" fill="none" aria-hidden="true">
        <rect width="44" height="44" rx="9" fill="rgba(255,255,255,0.12)" stroke="rgba(255,255,255,0.22)" stroke-width="1"/>
        <path d="M22 9 L36 16.5 L22 24 L8 16.5 Z" fill="white"/>
        <path d="M13 20 L13 29.5 C17.5 34 26.5 34 31 29.5 L31 20" fill="rgba(255,255,255,0.18)" stroke="white" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>
        <line x1="36" y1="16.5" x2="36" y2="26" stroke="white" stroke-width="2" stroke-linecap="round"/>
        <circle cx="36" cy="28.5" r="3" fill="#10b981"/>
      </svg>
      <div>
        <div class="cei-brand-name">Centre d'Examen Intelligent</div>
        <div class="cei-brand-sub">UNCHK &mdash; VisioPLUS</div>
      </div>
    </div>
    <div class="cei-divider"></div>
    <div class="cei-meta">
      <span class="cei-badge b-v">v2.1</span>
      <span class="cei-badge b-o">OpenAPI 3.0</span>
      <span class="cei-badge b-e">{ENDPOINT_COUNT} endpoints</span>
    </div>
    <nav class="cei-nav">
      <a class="n-link" href="/api/docs">Swagger UI</a>
      <a class="n-link on" href="/api/docs/redoc">ReDoc</a>
      <a class="n-link" href="/api/docs/openapi.json">JSON</a>
    </nav>
  </div>
</header>

<redoc
  spec-url='/api/docs/openapi.json'
  expand-responses="200,201"
  hide-download-button
  required-props-first
  sort-props-alphabetically="false"
  theme='{
    "colors": {
      "primary":    { "main": "#1d4ed8" },
      "success":    { "main": "#15803d" },
      "warning":    { "main": "#b45309" },
      "error":      { "main": "#b91c1c" },
      "text":       { "primary": "#1e293b", "secondary": "#475569" },
      "border":     { "dark": "#cbd5e1", "light": "#e2e8f0" },
      "responses": {
        "success":  { "color": "#15803d", "backgroundColor": "#f0fdf4", "tabTextColor": "#15803d" },
        "error":    { "color": "#b91c1c", "backgroundColor": "#fff1f2", "tabTextColor": "#b91c1c" },
        "redirect": { "color": "#b45309", "backgroundColor": "#fffbeb", "tabTextColor": "#b45309" },
        "info":     { "color": "#0369a1", "backgroundColor": "#f0f9ff", "tabTextColor": "#0369a1" }
      },
      "http": {
        "get":    "#0369a1",
        "post":   "#15803d",
        "put":    "#b45309",
        "delete": "#b91c1c",
        "patch":  "#0f766e",
        "head":   "#475569",
        "options":"#475569"
      }
    },
    "schema": {
      "linesColor":       "#e2e8f0",
      "defaultDetailsWidth": "75%",
      "typeNameColor":    "#0369a1",
      "typeTitleColor":   "#1e3a8a",
      "requireLabelColor":"#b91c1c",
      "labelsTextSize":   "0.85em",
      "nestingSpacing":   "1em"
    },
    "typography": {
      "fontSize":      "15px",
      "lineHeight":    "1.75",
      "fontWeightRegular": "400",
      "fontWeightBold":    "700",
      "fontWeightLight":   "300",
      "fontFamily":    "-apple-system, BlinkMacSystemFont, Segoe UI, Roboto, Arial, sans-serif",
      "smoothing":     "antialiased",
      "optimizeSpeed": true,
      "headings": {
        "fontFamily": "-apple-system, BlinkMacSystemFont, Segoe UI, sans-serif",
        "fontWeight": "700",
        "lineHeight": "1.35"
      },
      "code": {
        "fontSize":   "13.5px",
        "fontFamily": "SFMono-Regular, Menlo, Consolas, Liberation Mono, monospace",
        "lineHeight": "1.65",
        "color":      "#1d4ed8",
        "backgroundColor": "#eff6ff",
        "wrap":       true
      },
      "links": {
        "color":     "#1d4ed8",
        "visited":   "#1d4ed8",
        "hover":     "#1e3a8a"
      }
    },
    "sidebar": {
      "backgroundColor": "#f8fafc",
      "textColor":       "#1e293b",
      "activeTextColor": "#1d4ed8",
      "sectionTitleColor":"#64748b",
      "lineHeight":      "1.6",
      "arrow": {
        "size": "1.5em",
        "color":"#94a3b8"
      },
      "width": "290px",
      "groupItems": { "subItemsColor": "#475569" },
      "level1Items": { "textTransform": "none" }
    },
    "rightPanel": {
      "backgroundColor": "#0f172a",
      "textColor":       "#e2e8f0",
      "width":           "40%"
    },
    "codeBlock": {
      "backgroundColor": "#1e293b"
    },
    "fab": { "backgroundColor": "#1d4ed8", "color": "#fff" },
    "spacing": {
      "unit":              6,
      "sectionHorizontal": 40,
      "sectionVertical":   24
    },
    "breakpoints": { "small": "50rem", "medium": "85rem", "large": "105rem" }
  }'
></redoc>

<script src="https://cdn.jsdelivr.net/npm/redoc@latest/bundles/redoc.standalone.js"></script>
</body>
</html>"""

# Injecte le compte reel (voir _ENDPOINT_COUNT) dans les deux pages -- seul
# point a toucher si un jour le format des badges change, jamais le chiffre.
_SWAGGER_HTML = _SWAGGER_HTML.replace('{ENDPOINT_COUNT}', str(_ENDPOINT_COUNT))
_REDOC_HTML = _REDOC_HTML.replace('{ENDPOINT_COUNT}', str(_ENDPOINT_COUNT))

# ─────────────────────────────────────────────────────────────────────────────
# Routes Flask
# ─────────────────────────────────────────────────────────────────────────────

@swagger_bp.route('/api/docs')
@_require_docs_auth
def swagger_ui():
    return _SWAGGER_HTML, 200, {'Content-Type': 'text/html; charset=utf-8'}

@swagger_bp.route('/api/docs/redoc')
@_require_docs_auth
def redoc_ui():
    return _REDOC_HTML, 200, {'Content-Type': 'text/html; charset=utf-8'}

@swagger_bp.route('/api/docs/openapi.json')
@_require_docs_auth
def openapi_spec():
    spec = dict(OPENAPI_SPEC)
    scheme = 'https' if (request.is_secure or request.headers.get('X-Forwarded-Proto') == 'https') else 'http'
    current_url = f"{scheme}://{request.host}"
    spec['servers'] = [{"url": current_url, "description": "Serveur actuel"}] + [
        s for s in OPENAPI_SPEC['servers'] if s['url'] != current_url
    ]
    return jsonify(spec)
