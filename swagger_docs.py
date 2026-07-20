"""
CEI — Documentation API Swagger / OpenAPI 3.0
Accessible à /api/docs (Swagger UI) et /api/docs/openapi.json (spec brute)
Scan exhaustif v4 — app.py, proctoring_routes.py, csv_import_routes.py, export_route.py
164 endpoints documentés dans la spec OpenAPI 3.0 (ce nombre n'est PAS calculé
automatiquement — le mettre à jour ici et dans les deux badges HTML plus bas
à chaque route ajoutée/retirée dans OPENAPI_SPEC["paths"])
"""
import os
import base64
from functools import wraps
from flask import Blueprint, jsonify, request, Response

swagger_bp = Blueprint('swagger', __name__)

# ─────────────────────────────────────────────────────────────────────────────
# Basic Auth — credentials lus depuis .env (DOCS_USER / DOCS_PASS)
# Valeurs par défaut conservées pour compatibilité Serveur A
# ─────────────────────────────────────────────────────────────────────────────

_DOCS_USER = os.getenv('DOCS_USER', 'serge@rtn.sn')
_DOCS_PASS = os.getenv('DOCS_PASS', 'passer')

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
            "role":            {"type": "string", "enum": ["admin","professor","surveillant","student"]},
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
        "properties": {
            "id":               {"type": "integer"},
            "title":            {"type": "string"},
            "subject_id":       {"type": "integer"},
            "duration_minutes": {"type": "integer", "example": 90},
            "access_code":      {"type": "string", "example": "EXAM2026"},
            "status":           {"type": "string", "enum": ["draft","active","closed","archived"]},
            "max_attempts":     {"type": "integer"},
            "starts_at":        {"type": "string", "format": "date-time"},
            "ends_at":          {"type": "string", "format": "date-time"},
            "created_at":       {"type": "string", "format": "date-time"}
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
            "submitted_at":   {"type": "string", "format": "date-time"}
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
        {"name": "Proctoring",               "description": "Infrastructure de surveillance vidéo LiveKit — tokens, snapshots caméra, événements, signatures, enregistrements"},
        {"name": "Agent autonome",           "description": "API du service de surveillance IA autonome — statut, alertes, heartbeat"},
        {"name": "Intelligence Artificielle","description": "Génération de sujets et suggestions par IA"},
        {"name": "Réclamations",             "description": "Dépôt, traitement IA et décision sur les réclamations"},
        {"name": "Relevés de notes",         "description": "Génération et téléchargement des relevés PDF"},
        {"name": "Tableaux de bord",         "description": "Dashboards professeur et étudiant"},
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
            "description": "Retourne un **access token PASETO v4.public** (15 min, à stocker en mémoire) et pose un cookie httpOnly `cei_refresh` (7 jours) pour le rafraîchissement.",
            "security": [],
            "requestBody": {"required": True, "content": {"application/json": {"schema": {
                "type": "object", "required": ["email","password"],
                "properties": {
                    "email":    {"type": "string", "example": "serge@rtn.sn"},
                    "password": {"type": "string", "example": "passer"}
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
                "403": {"description": "Compte désactivé"}
            }
        }},
        "/api/auth/register": {"post": {
            "tags": ["Authentification"], "summary": "Créer un compte",
            "security": [],
            "requestBody": {"required": True, "content": {"application/json": {"schema": {
                "type": "object", "required": ["email","password","full_name","role"],
                "properties": {
                    "email":     {"type": "string"},
                    "password":  {"type": "string"},
                    "full_name": {"type": "string"},
                    "role":      {"type": "string", "enum": ["professor","surveillant","student"]}
                }
            }}}},
            "responses": {"201": {"description": "Compte créé"}, "409": {"description": "Email déjà utilisé"}}
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
                    {"name": "role",   "in": "query", "schema": {"type": "string", "enum": ["admin","professor","surveillant","student"]}},
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
                        "role":         {"type": "string", "enum": ["professor","surveillant","student","admin"]},
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
        "/api/admin/users/{target_user_id}": {
            "put": {
                "tags": ["Administration"], "summary": "Modifier un utilisateur (admin)",
                "parameters": [{"name": "target_user_id", "in": "path", "required": True, "schema": {"type": "integer"}, "description": "ID de l'utilisateur à modifier"}],
                "requestBody": {"content": {"application/json": {"schema": {
                    "type": "object",
                    "properties": {
                        "full_name":    {"type": "string"},
                        "email":        {"type": "string"},
                        "role":         {"type": "string", "enum": ["admin","professor","surveillant","student"]},
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
                "parameters": [{"name": "target_user_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
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
        "/api/admin/formations/{formation_id}": {
            "put": {
                "tags": ["Académique"], "summary": "Modifier une formation (admin)",
                "parameters": [{"name": "formation_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
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
                "parameters": [{"name": "formation_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
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
        "/api/admin/semesters/{semester_id}": {
            "put": {
                "tags": ["Académique"], "summary": "Modifier un semestre (admin)",
                "parameters": [{"name": "semester_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
                "requestBody": {"content": {"application/json": {"schema": {
                    "type": "object",
                    "properties": {"name": {"type": "string"}, "order": {"type": "integer"}}
                }}}},
                "responses": {"200": {"description": "Mis à jour"}}
            },
            "delete": {
                "tags": ["Académique"], "summary": "Supprimer un semestre (admin)",
                "parameters": [{"name": "semester_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
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
        "/api/admin/ues/{ue_id}": {
            "put": {
                "tags": ["Académique"], "summary": "Modifier une UE (admin)",
                "parameters": [{"name": "ue_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
                "requestBody": {"content": {"application/json": {"schema": {
                    "type": "object",
                    "properties": {"name": {"type": "string"}, "code": {"type": "string"},
                                   "credits": {"type": "number"}, "coefficient": {"type": "number"}}
                }}}},
                "responses": {"200": {"description": "UE mise à jour"}}
            },
            "delete": {
                "tags": ["Académique"], "summary": "Supprimer une UE (admin)",
                "parameters": [{"name": "ue_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
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
        "/api/admin/ecs/{ec_id}": {
            "put": {
                "tags": ["Académique"], "summary": "Modifier un EC (admin)",
                "parameters": [{"name": "ec_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
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
                "parameters": [{"name": "ec_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
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
        "/api/admin/ecs/{ec_id}/assign": {"post": {
            "tags": ["Académique"], "summary": "Affecter un professeur à un EC via l'ID EC (admin)",
            "parameters": [{"name": "ec_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
            "requestBody": {"required": True, "content": {"application/json": {"schema": {
                "type": "object", "required": ["professor_id"],
                "properties": {"professor_id": {"type": "integer"}}
            }}}},
            "responses": {"201": {"description": "Affectation créée"}}
        }},
        "/api/admin/ec_assignments/{assignment_id}": {"delete": {
            "tags": ["Académique"], "summary": "Retirer l'affectation d'un professeur (admin)",
            "parameters": [{"name": "assignment_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
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
        "/api/admin/student_enrollments/{enrollment_id}": {"delete": {
            "tags": ["Académique"], "summary": "Désinscrire un étudiant (admin)",
            "parameters": [{"name": "enrollment_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
            "responses": {"200": {"description": "Désinscrit"}}
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
                "tags": ["Groupes Surveillants"], "summary": "Renommer un groupe (admin)",
                "parameters": [{"name": "gid", "in": "path", "required": True, "schema": {"type": "integer"}}],
                "requestBody": {"content": {"application/json": {"schema": {
                    "type": "object", "properties": {"name": {"type": "string"}}
                }}}},
                "responses": {"200": {"description": "Groupe mis à jour"}}
            },
            "delete": {
                "tags": ["Groupes Surveillants"], "summary": "Supprimer un groupe (admin)",
                "parameters": [{"name": "gid", "in": "path", "required": True, "schema": {"type": "integer"}}],
                "responses": {"200": {"description": "Supprimé"}, "404": {"$ref": "#/components/responses/NotFound"}}
            }
        },
        "/api/admin/proctor_groups/{gid}/members": {"post": {
            "tags": ["Groupes Surveillants"], "summary": "Ajouter des surveillants à un groupe (admin)",
            "description": "Notifie automatiquement chaque surveillant ajouté.",
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
            "parameters": [
                {"name": "gid", "in": "path", "required": True, "schema": {"type": "integer"}},
                {"name": "mid", "in": "path", "required": True, "schema": {"type": "integer"}, "description": "id de la ligne d'appartenance (pas l'id du surveillant)"}
            ],
            "responses": {"200": {"description": "Retiré"}}
        }},
        "/api/admin/proctor_groups/{gid}/ecs": {"post": {
            "tags": ["Groupes Surveillants"], "summary": "Rattacher un EC à un groupe (admin)",
            "description": "Tout examen créé pour cet EC affectera automatiquement tous les membres du groupe.",
            "parameters": [{"name": "gid", "in": "path", "required": True, "schema": {"type": "integer"}}],
            "requestBody": {"required": True, "content": {"application/json": {"schema": {
                "type": "object", "required": ["ec_id"],
                "properties": {"ec_id": {"type": "integer"}}
            }}}},
            "responses": {"200": {"description": "EC rattaché", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ProctorGroup"}}}}}
        }},
        "/api/admin/proctor_groups/{gid}/ecs/{ec_id}": {"delete": {
            "tags": ["Groupes Surveillants"], "summary": "Détacher un EC d'un groupe (admin)",
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
            "description": "Crée les comptes utilisateurs en masse. Envoie un email de bienvenue à chaque utilisateur avec email valide.",
            "requestBody": {"required": True, "content": {"multipart/form-data": {"schema": {
                "type": "object", "required": ["file"],
                "properties": {"file": {"type": "string", "format": "binary", "description": "Fichier CSV (colonnes : full_name, email, role, password)"}}
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

        "/api/subjects": {"get": {
            "tags": ["Sujets"], "summary": "Liste des sujets (filtrés par rôle et EC)",
            "parameters": [
                {"name": "ec_id",  "in": "query", "schema": {"type": "integer"}},
                {"name": "page",   "in": "query", "schema": {"type": "integer", "default": 1}},
                {"name": "search", "in": "query", "schema": {"type": "string"}}
            ],
            "responses": {"200": {"description": "Sujets", "content": {"application/json": {"schema": {
                "type": "array", "items": {"$ref": "#/components/schemas/Subject"}
            }}}}}
        }},
        "/api/subjects/{subject_id}": {
            "get": {
                "tags": ["Sujets"], "summary": "Détail d'un sujet",
                "parameters": [{"name": "subject_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
                "responses": {
                    "200": {"description": "Sujet", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Subject"}}}},
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
            "summary": "Uploader un fichier pour créer un sujet",
            "description": "Envoie un PDF/DOCX/TXT. L'IA génère automatiquement le barème. Support OCR pour les PDF CIDFont illisibles.",
            "requestBody": {"required": True, "content": {"multipart/form-data": {"schema": {
                "type": "object", "required": ["file"],
                "properties": {
                    "file":  {"type": "string", "format": "binary"},
                    "ec_id": {"type": "integer"},
                    "title": {"type": "string"}
                }
            }}}},
            "responses": {
                "201": {"description": "Sujet créé avec barème IA"},
                "400": {"description": "Fichier invalide ou contenu illisible"}
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
                        "max_no_face_count":   {"type": "integer", "default": 10, "description": "Nb de détections sans visage avant alerte"},
                        "ban_on_devtools":     {"type": "boolean", "default": True, "description": "Exclure si outils développeur détectés"}
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
            "tags": ["Examens en ligne"], "summary": "Démarrer une tentative (étudiant)",
            "parameters": [{"name": "exam_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
            "requestBody": {"required": True, "content": {"application/json": {"schema": {
                "type": "object", "required": ["access_code"],
                "properties": {"access_code": {"type": "string", "example": "EXAM2026"}}
            }}}},
            "responses": {
                "200": {"description": "Tentative démarrée", "content": {"application/json": {"schema": {
                    "type": "object",
                    "properties": {
                        "success":    {"type": "boolean"},
                        "attempt":    {"$ref": "#/components/schemas/ExamAttempt"},
                        "continuing": {"type": "boolean", "description": "True si une tentative en cours a été reprise"}
                    }
                }}}},
                "400": {"description": "Code incorrect ou examen non actif"},
                "409": {"description": "Tentative déjà soumise"}
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
                        "enum": ["tab_switch","devtools_attempt","no_face_detected","multiple_faces","copy_paste","fullscreen_exit","window_blur"],
                        "description": "tab_switch +15pts | devtools_attempt +10pts | no_face_detected +10pts | multiple_faces +20pts"
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
                        "enum": ["no_face_detected","multiple_faces","tab_switch","camera_disabled","fullscreen_exit"],
                        "description": "no_face_detected +10pts | multiple_faces +20pts | tab_switch +15pts"
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
        "/api/exam_attempts/{attempt_id}/private_token": {"get": {
            "tags": ["Surveillant"],
            "summary": "Token LiveKit pour appel privé surveillant ↔ étudiant",
            "parameters": [{"name": "attempt_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
            "responses": {"200": {"description": "Token appel privé"}}
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
        "/api/surveillant/exams": {"get": {
            "tags": ["Surveillant"], "summary": "Examens assignés au surveillant connecté",
            "responses": {"200": {"description": "Examens du surveillant", "content": {"application/json": {"schema": {
                "type": "array", "items": {"$ref": "#/components/schemas/OnlineExam"}
            }}}}}
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
                                    "face_detected": {"type": "boolean"}
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

        "/api/agent/alerts": {
            "post": {
                "tags": ["Agent autonome"],
                "summary": "Pousser une alerte — SERVICE AGENT UNIQUEMENT",
                "description": (
                    "⚠️ **Endpoint interne** — réservé au service `cei-agent-proctor` (PM2).\n\n"
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
                "⚠️ **Endpoint interne** — réservé au service `cei-agent-proctor` (PM2).\n\n"
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
                "⚠️ **Endpoint interne** — réservé au service `cei-agent-proctor`.\n\n"
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
            "summary": "Générer des suggestions d'examens depuis un cours",
            "description": (
                "Upload d'un cours (PDF/DOCX/TXT). L'IA détecte la discipline, analyse le contenu "
                "et génère 3 suggestions adaptées. Le domaine détecté est transmis pour la génération complète."
            ),
            "requestBody": {"required": True, "content": {"multipart/form-data": {"schema": {
                "type": "object", "required": ["course_file"],
                "properties": {
                    "course_file":   {"type": "string", "format": "binary"},
                    "difficulty":    {"type": "string", "enum": ["Facile","Moyen","Difficile"], "default": "Moyen"},
                    "student_level": {"type": "string", "example": "Licence 3"},
                    "exam_type":     {"type": "string", "example": "QCM"}
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
            "description": "Prend un objet suggestion (issu de generate-exam-suggestions) et génère un sujet complet avec questions numérotées et barème.",
            "requestBody": {"required": True, "content": {"application/json": {"schema": {
                "type": "object", "required": ["suggestion"],
                "properties": {"suggestion": {"type": "object", "description": "Objet suggestion retourné par generate-exam-suggestions"}}
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
            "requestBody": {"required": True, "content": {"application/json": {"schema": {
                "type": "object", "required": ["title","content"],
                "properties": {
                    "title":   {"type": "string"},
                    "content": {"type": "string"},
                    "rubric":  {"type": "string"},
                    "ec_id":   {"type": "integer"}
                }
            }}}},
            "responses": {"201": {"description": "Sujet sauvegardé", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Subject"}}}}}
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
        "/api/reclamations/{reclamation_id}": {"put": {
            "tags": ["Réclamations"],
            "summary": "Répondre manuellement à une réclamation (prof/admin)",
            "description": "Le professeur peut accepter (avec ou sans modification de note) ou rejeter la réclamation.",
            "parameters": [{"name": "reclamation_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
            "requestBody": {"required": True, "content": {"application/json": {"schema": {
                "type": "object", "required": ["status"],
                "properties": {
                    "status":    {"type": "string", "enum": ["resolved","rejected"]},
                    "response":  {"type": "string", "description": "Explication de la décision"},
                    "new_score": {"type": "number", "description": "Nouvelle note si acceptée (optionnel)"}
                }
            }}}},
            "responses": {"200": {"description": "Réclamation traitée"}}
        }},
        "/api/reclamations/{reclamation_id}/process_ia": {"post": {
            "tags": ["Réclamations"],
            "summary": "Traiter une réclamation par IA",
            "description": "L'IA re-corrige la copie en tenant compte de la contestation et propose une note révisée. Le prof peut ensuite accepter ou rejeter.",
            "parameters": [{"name": "reclamation_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
            "responses": {"200": {"description": "Proposition IA", "content": {"application/json": {"schema": {
                "type": "object",
                "properties": {
                    "ia_proposed_score":  {"type": "number"},
                    "ia_proposed_status": {"type": "string", "enum": ["accepted","rejected","partial"]},
                    "ia_proposed_reason": {"type": "string"}
                }
            }}}}}
        }},
        "/api/reclamations/{reclamation_id}/apply_proposal": {"post": {
            "tags": ["Réclamations"],
            "summary": "Accepter et appliquer la proposition IA (prof/admin)",
            "description": "Applique la note proposée par l'IA à la copie et clôt la réclamation avec statut 'resolved'.",
            "parameters": [{"name": "reclamation_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
            "responses": {"200": {"description": "Proposition IA appliquée"}, "400": {"description": "Aucune proposition disponible"}}
        }},
        "/api/reclamations/{reclamation_id}/reject_proposal": {"post": {
            "tags": ["Réclamations"],
            "summary": "Rejeter la proposition IA (prof/admin)",
            "description": "Rejette la proposition IA sans modifier la note. La réclamation est clôturée avec statut 'rejected'.",
            "parameters": [{"name": "reclamation_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
            "requestBody": {"content": {"application/json": {"schema": {
                "type": "object",
                "properties": {"response": {"type": "string", "default": "Proposition IA rejetée par le professeur"}}
            }}}},
            "responses": {"200": {"description": "Proposition rejetée"}}
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
        "/api/transcripts/{transcript_id}/pdf": {"get": {
            "tags": ["Relevés de notes"], "summary": "Télécharger un relevé en PDF",
            "parameters": [{"name": "transcript_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
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
            "description": "Retourne le nombre de sujets créés et de copies corrigées par le professeur connecté.",
            "responses": {"200": {"description": "Stats prof", "content": {"application/json": {"schema": {
                "type": "object",
                "properties": {
                    "my_subjects":        {"type": "integer"},
                    "papers_corrected":   {"type": "integer"}
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
            "responses": {"200": {"description": "Incidents récents"}}
        }},
        "/api/student/papers": {"get": {
            "tags": ["Tableaux de bord"], "summary": "Copies de l'étudiant connecté avec notes",
            "responses": {"200": {"description": "Mes copies", "content": {"application/json": {"schema": {
                "type": "array", "items": {"$ref": "#/components/schemas/StudentPaper"}
            }}}}}
        }},
        "/api/student/online_results": {"get": {
            "tags": ["Tableaux de bord"], "summary": "Résultats des examens en ligne de l'étudiant connecté",
            "responses": {"200": {"description": "Résultats", "content": {"application/json": {"schema": {
                "type": "array", "items": {
                    "type": "object",
                    "properties": {
                        "attempt_id":   {"type": "integer"},
                        "exam_title":   {"type": "string"},
                        "score":        {"type": "number"},
                        "corrected_at": {"type": "string", "format": "date-time"},
                        "auto_correct": {"type": "boolean"},
                        "has_reclamation": {"type": "boolean"},
                        "reclamation_status": {"type": "string"}
                    }
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
                "200": {"description": "Email de réinitialisation envoyé (si le compte existe)"},
                "404": {"description": "Aucun compte avec cet email"}
            }
        }},
        "/api/auth/reset-password": {"post": {
            "tags": ["Authentification"], "summary": "Réinitialiser le mot de passe avec un token",
            "security": [],
            "requestBody": {"required": True, "content": {"application/json": {"schema": {
                "type": "object", "required": ["token","new_password"],
                "properties": {
                    "token":        {"type": "string", "description": "Token reçu par email"},
                    "new_password": {"type": "string", "minLength": 6}
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
        "/api/admin/students/{student_id}/set_formation": {"post": {
            "tags": ["Académique"], "summary": "Affecter une formation à un étudiant (admin)",
            "parameters": [{"name": "student_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
            "requestBody": {"required": True, "content": {"application/json": {"schema": {
                "type": "object", "required": ["formation_id"],
                "properties": {"formation_id": {"type": "integer"}}
            }}}},
            "responses": {"200": {"description": "Formation affectée"}}
        }},
        "/api/admin/students/{student_id}/enroll_formation": {"post": {
            "tags": ["Académique"], "summary": "Inscrire un étudiant à tous les EC d'une formation (admin)",
            "parameters": [{"name": "student_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
            "requestBody": {"required": True, "content": {"application/json": {"schema": {
                "type": "object", "required": ["formation_id"],
                "properties": {"formation_id": {"type": "integer"}}
            }}}},
            "responses": {"200": {"description": "Étudiant inscrit à toute la formation"}}
        }},
        "/api/admin/enroll_student_ec": {"post": {
            "tags": ["Académique"], "summary": "Inscrire un étudiant à un EC spécifique (admin)",
            "requestBody": {"required": True, "content": {"application/json": {"schema": {
                "type": "object", "required": ["student_id","ec_id"],
                "properties": {
                    "student_id": {"type": "integer"},
                    "ec_id":      {"type": "integer"}
                }
            }}}},
            "responses": {"200": {"description": "Inscrit"}, "409": {"description": "Déjà inscrit"}}
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
            "tags": ["Examens en ligne"], "summary": "Modifier les paramètres d'un examen (admin)",
            "parameters": [{"name": "exam_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
            "requestBody": {"content": {"application/json": {"schema": {
                "type": "object",
                "properties": {
                    "title":               {"type": "string"},
                    "start_time":          {"type": "string", "format": "date-time"},
                    "end_time":            {"type": "string", "format": "date-time"},
                    "max_tab_switches":    {"type": "integer"},
                    "enable_copy_paste":   {"type": "boolean"},
                    "randomize_questions": {"type": "boolean"}
                }
            }}}},
            "responses": {"200": {"description": "Examen mis à jour"}}
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
            "parameters": [{"name": "attempt_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
            "responses": {"200": {"description": "Score et feedback", "content": {"application/json": {"schema": {
                "type": "object",
                "properties": {
                    "score":        {"type": "number"},
                    "feedback":     {"type": "string"},
                    "submitted_at": {"type": "string", "format": "date-time"}
                }
            }}}}}
        }},
        "/api/exam_attempts/{attempt_id}/manual-grade": {"put": {
            "tags": ["Examens en ligne"], "summary": "Note manuelle d'une tentative (prof/admin)",
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
            "description": "Retourne la copie complète avec les questions, réponses, score par question et incidents.",
            "parameters": [{"name": "attempt_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
            "responses": {"200": {"description": "Révision complète"}}
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
        "/api/exam_attempts/{attempt_id}/signature/{sig_type}": {"get": {
            "tags": ["Proctoring"], "summary": "Signature électronique de l'étudiant",
            "parameters": [
                {"name": "attempt_id", "in": "path", "required": True, "schema": {"type": "integer"}},
                {"name": "sig_type", "in": "path", "required": True, "schema": {"type": "string", "enum": ["start","submit"]}, "description": "start = signature au démarrage ; submit = signature à la soumission"}
            ],
            "responses": {"200": {"description": "Image de la signature en base64"}}
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
        "/api/question_bank/{q_id}": {"delete": {
            "tags": ["Intelligence Artificielle"], "summary": "Supprimer une question de la banque",
            "parameters": [{"name": "q_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
            "responses": {"200": {"description": "Question supprimée"}}
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

        # ══════════════════════════════════════════════════════════════════════
        # RELEVÉS — Routes manquantes
        # ══════════════════════════════════════════════════════════════════════

        "/api/transcripts/bulk-pdf": {"get": {
            "tags": ["Relevés de notes"], "summary": "Télécharger tous les relevés en un seul ZIP/PDF",
            "responses": {
                "200": {"description": "Archive", "content": {"application/zip": {"schema": {"type": "string", "format": "binary"}}}}
            }
        }},
        "/api/transcripts/{transcript_id}": {"delete": {
            "tags": ["Relevés de notes"], "summary": "Supprimer un relevé de notes (admin)",
            "parameters": [{"name": "transcript_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
            "responses": {"200": {"description": "Relevé supprimé"}, "404": {"$ref": "#/components/responses/NotFound"}}
        }},
        "/api/transcripts/{transcript_id}/publish": {"put": {
            "tags": ["Relevés de notes"], "summary": "Publier un relevé (le rendre visible à l'étudiant)",
            "parameters": [{"name": "transcript_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
            "responses": {"200": {"description": "Relevé publié"}}
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
        "id": 5, "title": "Examen Final Réseaux L3", "subject_id": 12,
        "duration_minutes": 90, "access_code": "RESEAU2026", "status": "active",
        "max_attempts": 1,
        "starts_at": "2026-11-10T09:00:00Z", "ends_at": "2026-11-10T11:30:00Z",
        "created_at": "2026-11-01T00:00:00Z"
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
      <span class="cei-badge cei-badge-count">164 endpoints</span>
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
      <span class="cei-badge b-e">164 endpoints</span>
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
