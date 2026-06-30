"""
CEI — Documentation API Swagger / OpenAPI 3.0
Accessible à /api/docs (Swagger UI) et /api/docs/openapi.json (spec brute)
Scan exhaustif v4 — app.py, proctoring_routes.py, csv_import_routes.py, export_route.py
111 opérations HTTP — couverture 100% vérifiée programmatiquement
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
            "id":         {"type": "integer"},
            "email":      {"type": "string", "example": "user@ec2lt.sn"},
            "full_name":  {"type": "string", "example": "Moussa Diallo"},
            "role":       {"type": "string", "enum": ["admin","professor","surveillant","student"]},
            "is_active":  {"type": "boolean"},
            "has_email":  {"type": "boolean"},
            "created_at": {"type": "string", "format": "date-time"}
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
    "Formation": {
        "type": "object",
        "properties": {
            "id":             {"type": "integer"},
            "name":           {"type": "string", "example": "Licence Informatique"},
            "code":           {"type": "string", "example": "LI"},
            "description":    {"type": "string"},
            "duration_years": {"type": "integer", "example": 3}
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
            "API REST complète de la plateforme CEI de l'**RTN – Réseaux et Techniques Numériques (EC2LT)**.\n\n"
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
            "name": "EC2LT — VisioPLUS",
            "email": "entreprisertn221@gmail.com",
            "url": "http://62.171.190.6:8100"
        },
        "license": {"name": "MIT", "url": "https://opensource.org/licenses/MIT"}
    },
    "servers": [
        {"url": "http://62.171.190.6:8100", "description": "Production EC2LT"},
        {"url": "http://localhost:5000",  "description": "Développement local"}
    ],
    "tags": [
        {"name": "Authentification",         "description": "Connexion PASETO v4, rafraîchissement token, déconnexion, profil, mot de passe"},
        {"name": "Administration",           "description": "Tableau de bord admin, utilisateurs, historique"},
        {"name": "Académique",               "description": "Formations, semestres, UE, EC, inscriptions, affectations"},
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
                "requestBody": {"required": True, "content": {"application/json": {"schema": {
                    "type": "object", "required": ["email","full_name","role","password"],
                    "properties": {
                        "email":      {"type": "string"},
                        "full_name":  {"type": "string"},
                        "role":       {"type": "string", "enum": ["professor","surveillant","student"]},
                        "password":   {"type": "string"},
                        "send_email": {"type": "boolean", "default": True}
                    }
                }}}},
                "responses": {"201": {"description": "Utilisateur créé"}, "409": {"description": "Email déjà utilisé"}}
            }
        },
        "/api/admin/users/{target_user_id}": {
            "put": {
                "tags": ["Administration"], "summary": "Modifier un utilisateur (admin)",
                "parameters": [{"name": "target_user_id", "in": "path", "required": True, "schema": {"type": "integer"}, "description": "ID de l'utilisateur à modifier"}],
                "requestBody": {"content": {"application/json": {"schema": {
                    "type": "object",
                    "properties": {
                        "full_name": {"type": "string"},
                        "email":     {"type": "string"},
                        "role":      {"type": "string", "enum": ["admin","professor","surveillant","student"]},
                        "password":  {"type": "string"},
                        "is_active": {"type": "boolean"}
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
            "description": "Crée un compte étudiant avec une adresse @noemail.local générée automatiquement. Utile pour les étudiants sans email personnel.",
            "requestBody": {"required": True, "content": {"application/json": {"schema": {
                "type": "object", "required": ["full_name"],
                "properties": {"full_name": {"type": "string", "example": "Amadou Ba"}}
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
        # ACADÉMIQUE — Formations / Semestres / UE / EC / Inscriptions
        # ══════════════════════════════════════════════════════════════════════

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
            "requestBody": {"required": True, "content": {"application/json": {"schema": {
                "type": "object", "required": ["name"],
                "properties": {
                    "name":           {"type": "string", "example": "Licence Informatique"},
                    "code":           {"type": "string", "example": "LI3"},
                    "description":    {"type": "string"},
                    "duration_years": {"type": "integer", "example": 3}
                }
            }}}},
            "responses": {"201": {"description": "Formation créée"}}
        }},
        "/api/admin/formations/{formation_id}": {
            "put": {
                "tags": ["Académique"], "summary": "Modifier une formation (admin)",
                "parameters": [{"name": "formation_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
                "requestBody": {"content": {"application/json": {"schema": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"}, "code": {"type": "string"},
                        "description": {"type": "string"}, "duration_years": {"type": "integer"}
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
            "description": "Retourne un CSV avec les colonnes : formation, semestre, UE, EC, coefficient, crédits.",
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
            "description": "Crée la hiérarchie Formation → Semestres → UE → EC depuis un fichier CSV.",
            "requestBody": {"required": True, "content": {"multipart/form-data": {"schema": {
                "type": "object", "required": ["file"],
                "properties": {"file": {"type": "string", "format": "binary"}}
            }}}},
            "responses": {
                "200": {"description": "Maquette importée", "content": {"application/json": {"schema": {
                    "type": "object",
                    "properties": {
                        "formations_created": {"type": "integer"},
                        "ues_created":        {"type": "integer"},
                        "ecs_created":        {"type": "integer"},
                        "errors":             {"type": "array", "items": {"type": "string"}}
                    }
                }}}}
            }
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
            "description": "Enregistre une photo horodatée de la caméra étudiant avec le résultat de la détection de visage.",
            "parameters": [{"name": "attempt_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
            "requestBody": {"required": True, "content": {"application/json": {"schema": {
                "type": "object",
                "properties": {
                    "image_data":    {"type": "string", "description": "Image base64 (optionnel)"},
                    "face_detected": {"type": "boolean"},
                    "face_count":    {"type": "integer"},
                    "confidence":    {"type": "number"}
                }
            }}}},
            "responses": {"200": {"description": "Snapshot enregistré"}}
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
                "description": "Retourne les 50 dernières alertes non lues. Appelé par le dashboard toutes les 15 secondes. Requiert un JWT (rôle admin/prof/surveillant).",
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
# HTML Swagger UI & ReDoc
# ─────────────────────────────────────────────────────────────────────────────

_SWAGGER_HTML = """<!DOCTYPE html>
<html lang="fr">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>CEI API — Documentation</title>
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui.css">
  <style>
    /* ── Fond clair, lisible ── */
    html, body {
      margin: 0;
      background: #f0f4f8;
      font-size: 16px;
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Arial, sans-serif;
      color: #1a202c;
    }

    /* ── Barre de titre ── */
    .topbar {
      background: #1e40af !important;
      padding: 10px 0 !important;
      box-shadow: 0 2px 8px rgba(0,0,0,0.18);
    }
    .topbar-wrapper img { display: none; }
    .topbar-wrapper::after {
      content: "CEI — Centre d'Examen Intelligent · API v2.1 · 157 endpoints";
      color: #ffffff;
      font-weight: 700;
      font-size: 17px;
      letter-spacing: .3px;
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    }
    .topbar .download-url-wrapper { display: none !important; }

    /* ── Zone principale ── */
    .swagger-ui {
      background: #f0f4f8;
      font-size: 15px;
    }

    /* ── Bloc d'information ── */
    .swagger-ui .info {
      background: #ffffff;
      border-radius: 10px;
      padding: 24px 28px;
      margin: 20px 0 16px;
      box-shadow: 0 1px 4px rgba(0,0,0,0.08);
    }
    .swagger-ui .info .title {
      font-size: 26px !important;
      color: #1e3a8a !important;
      font-weight: 800;
    }
    .swagger-ui .info p,
    .swagger-ui .info li,
    .swagger-ui .renderedMarkdown p {
      font-size: 15px !important;
      line-height: 1.7 !important;
      color: #374151 !important;
    }
    .swagger-ui .info .base-url {
      font-size: 14px !important;
      color: #4b5563 !important;
    }

    /* ── Filtres et barre de recherche ── */
    .swagger-ui .filter input {
      font-size: 15px !important;
      padding: 8px 12px !important;
      border-radius: 6px !important;
      border: 1.5px solid #9ca3af !important;
      background: #ffffff !important;
      color: #111827 !important;
    }
    .swagger-ui .filter input:focus {
      border-color: #1e40af !important;
      outline: 2px solid #bfdbfe !important;
    }

    /* ── Tags (groupes de routes) ── */
    .swagger-ui .opblock-tag {
      font-size: 18px !important;
      font-weight: 700 !important;
      color: #1e3a8a !important;
      border-bottom: 2px solid #dbeafe !important;
      padding: 10px 0 6px !important;
    }
    .swagger-ui .opblock-tag:hover { background: #eff6ff !important; }

    /* ── Blocs de routes ── */
    .swagger-ui .opblock {
      border-radius: 8px !important;
      margin-bottom: 6px !important;
      box-shadow: 0 1px 3px rgba(0,0,0,0.07) !important;
      border-width: 1px !important;
    }
    .swagger-ui .opblock-summary {
      padding: 10px 14px !important;
      align-items: center !important;
    }
    .swagger-ui .opblock-summary-method {
      font-size: 13px !important;
      font-weight: 700 !important;
      min-width: 72px !important;
      text-align: center !important;
      border-radius: 4px !important;
      padding: 5px 10px !important;
    }
    .swagger-ui .opblock-summary-path {
      font-size: 15px !important;
      font-weight: 600 !important;
      color: #1e293b !important;
      letter-spacing: .2px;
    }
    .swagger-ui .opblock-summary-description {
      font-size: 14px !important;
      color: #4b5563 !important;
    }

    /* ── Intérieur déplié ── */
    .swagger-ui .opblock-body {
      background: #ffffff !important;
      border-radius: 0 0 8px 8px !important;
      padding: 16px !important;
    }
    .swagger-ui .opblock-section-header h4 {
      font-size: 14px !important;
      font-weight: 700 !important;
      color: #374151 !important;
    }

    /* ── Paramètres ── */
    .swagger-ui table thead tr th,
    .swagger-ui .parameters-col_name,
    .swagger-ui .parameter__name {
      font-size: 14px !important;
      color: #1e293b !important;
      font-weight: 600 !important;
    }
    .swagger-ui table tbody tr td,
    .swagger-ui .parameter__type,
    .swagger-ui .parameter__in {
      font-size: 14px !important;
      color: #374151 !important;
    }
    .swagger-ui .parameter__name.required::after {
      color: #dc2626 !important;
      font-size: 14px !important;
    }

    /* ── Champs de saisie Try It Out ── */
    .swagger-ui input[type=text],
    .swagger-ui textarea,
    .swagger-ui select {
      font-size: 14px !important;
      border: 1.5px solid #9ca3af !important;
      border-radius: 5px !important;
      padding: 7px 10px !important;
      background: #ffffff !important;
      color: #111827 !important;
    }
    .swagger-ui input[type=text]:focus,
    .swagger-ui textarea:focus {
      border-color: #1e40af !important;
      outline: 2px solid #bfdbfe !important;
    }

    /* ── Bouton Execute ── */
    .swagger-ui .btn.execute {
      background: #1d4ed8 !important;
      color: #ffffff !important;
      font-size: 14px !important;
      font-weight: 700 !important;
      border-radius: 6px !important;
      padding: 9px 22px !important;
      border: none !important;
    }
    .swagger-ui .btn.execute:hover { background: #1e40af !important; }

    /* ── Boutons Cancel/Try it out ── */
    .swagger-ui .btn {
      font-size: 13px !important;
      border-radius: 5px !important;
    }
    .swagger-ui .btn.cancel {
      color: #dc2626 !important;
      border-color: #dc2626 !important;
    }
    .swagger-ui .try-out__btn {
      font-size: 13px !important;
      font-weight: 600 !important;
    }

    /* ── Réponses ── */
    .swagger-ui .responses-inner h4,
    .swagger-ui .response-col_status {
      font-size: 14px !important;
      font-weight: 700 !important;
    }
    .swagger-ui .response-col_description {
      font-size: 14px !important;
      color: #374151 !important;
    }
    .swagger-ui .highlight-code pre,
    .swagger-ui .microlight {
      font-size: 13px !important;
      line-height: 1.6 !important;
      background: #f8fafc !important;
      border-radius: 6px !important;
      padding: 12px !important;
      color: #1e293b !important;
    }

    /* ── Schémas / Models ── */
    .swagger-ui section.models h4,
    .swagger-ui .model-title {
      font-size: 15px !important;
      font-weight: 700 !important;
      color: #1e3a8a !important;
    }
    .swagger-ui .model {
      font-size: 14px !important;
      color: #374151 !important;
    }
    .swagger-ui .prop-type { color: #0369a1 !important; }
    .swagger-ui .prop-format { color: #6b7280 !important; font-size: 13px !important; }

    /* ── Badges de méthode HTTP (couleurs accessibles) ── */
    .swagger-ui .opblock-get    .opblock-summary-method { background: #0369a1 !important; }
    .swagger-ui .opblock-post   .opblock-summary-method { background: #15803d !important; }
    .swagger-ui .opblock-put    .opblock-summary-method { background: #92400e !important; }
    .swagger-ui .opblock-patch  .opblock-summary-method { background: #6d28d9 !important; }
    .swagger-ui .opblock-delete .opblock-summary-method { background: #b91c1c !important; }

    /* ── Bandeau d'autorisation ── */
    .swagger-ui .auth-wrapper .authorize {
      font-size: 14px !important;
      border-color: #1d4ed8 !important;
      color: #1d4ed8 !important;
    }
    .swagger-ui .btn.authorize svg { fill: #1d4ed8 !important; }

    /* ── Lien ReDoc ── */
    #redoc-link {
      display: inline-block;
      margin: 0 20px 16px;
      padding: 7px 16px;
      background: #f1f5f9;
      border: 1.5px solid #cbd5e1;
      border-radius: 6px;
      font-size: 14px;
      font-weight: 600;
      color: #1e3a8a;
      text-decoration: none;
    }
    #redoc-link:hover { background: #dbeafe; border-color: #1e40af; }
  </style>
</head>
<body>
<div id="swagger-ui"></div>
<a id="redoc-link" href="/api/docs/redoc" target="_blank">📄 Voir aussi ReDoc (lecture facile)</a>
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
  <title>CEI API — ReDoc</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>
    body { margin: 0; padding: 0; background: #f0f4f8; }
    #redoc-header {
      background: #1e40af;
      color: #fff;
      padding: 14px 24px;
      font-size: 17px;
      font-weight: 700;
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
      box-shadow: 0 2px 8px rgba(0,0,0,0.18);
    }
    #redoc-header a {
      color: #bfdbfe;
      font-size: 14px;
      font-weight: 500;
      text-decoration: none;
      margin-left: 20px;
    }
    #redoc-header a:hover { color: #fff; text-decoration: underline; }
  </style>
</head>
<body>
  <div id="redoc-header">
    CEI — Centre d'Examen Intelligent · API v2.1
    <a href="/api/docs">← Swagger UI</a>
  </div>
  <redoc
    spec-url='/api/docs/openapi.json'
    expand-responses="200,201"
    hide-download-button
    theme='{
      "colors": {
        "primary": { "main": "#1e40af" },
        "text":    { "primary": "#1a202c", "secondary": "#4b5563" },
        "http": {
          "get":    "#0369a1",
          "post":   "#15803d",
          "put":    "#92400e",
          "delete": "#b91c1c",
          "patch":  "#6d28d9"
        }
      },
      "typography": {
        "fontSize":       "16px",
        "lineHeight":     "1.7",
        "fontFamily":     "-apple-system, BlinkMacSystemFont, Segoe UI, Roboto, Arial, sans-serif",
        "headings": {
          "fontFamily":   "-apple-system, BlinkMacSystemFont, Segoe UI, sans-serif",
          "fontWeight":   "700"
        },
        "code": {
          "fontSize":     "14px",
          "fontFamily":   "SFMono-Regular, Menlo, Consolas, monospace",
          "lineHeight":   "1.6"
        }
      },
      "sidebar": {
        "backgroundColor": "#f8fafc",
        "textColor":       "#1e293b",
        "width":           "280px"
      },
      "rightPanel": {
        "backgroundColor": "#1e293b"
      },
      "spacing": {
        "sectionVertical":   20,
        "unit":               6
      }
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
    return jsonify(OPENAPI_SPEC)
