"""
CEI API v2 — Authentification PASETO v4.public (Ed25519)
Remplace flask-jwt-extended.

Stratégie tokens :
  - Access token  : v4.public Ed25519, TTL 15 min, header Authorization: Bearer
  - Refresh token : v4.public Ed25519, TTL 7 jours, httpOnly cookie path=/api/auth

Sécurité :
  - Rotation refresh token à chaque usage (TokenBlocklist)
  - Clé privée uniquement côté serveur
  - Clé publique exposée sur /api/auth/public-key (vérification client possible)
  - Résistant aux attaques algorithm confusion (PASETO = algorithme fixe)
"""

import os
import json
import base64
import hashlib
from datetime import datetime, timedelta, timezone
from functools import wraps

import pyseto
from pyseto import Key
from flask import request, jsonify, g, make_response

# ─── Chargement des clés depuis .env ────────────────────────────────────────

def _load_key(env_var: str, purpose: str) -> Key:
    raw = os.environ.get(env_var, "").strip()
    if not raw:
        raise RuntimeError(
            f"[auth_paseto] Variable d'environnement manquante : {env_var}\n"
            "Lancez : python scripts/generate_paseto_keys.py >> .env"
        )
    try:
        # Les clés sont stockées en PEM encodé en base64 dans .env
        pem_bytes = base64.b64decode(raw)
        return Key.new(version=4, purpose=purpose, key=pem_bytes)
    except Exception as e:
        raise RuntimeError(f"[auth_paseto] Clé invalide ({env_var}) : {e}")

_PRIVATE_KEY: Key = None
_PUBLIC_KEY:  Key = None

def init_paseto():
    """Appelé une fois au démarrage de l'app Flask."""
    global _PRIVATE_KEY, _PUBLIC_KEY
    _PRIVATE_KEY = _load_key("PASETO_PRIVATE_KEY", "public")
    _PUBLIC_KEY  = _load_key("PASETO_PUBLIC_KEY",  "public")

# ─── Durées ──────────────────────────────────────────────────────────────────

ACCESS_TTL  = timedelta(minutes=int(os.getenv("PASETO_ACCESS_TTL_MIN",  "15")))
REFRESH_TTL = timedelta(days   =int(os.getenv("PASETO_REFRESH_TTL_DAYS", "7")))
COOKIE_NAME = "cei_refresh"

# ─── Session unique par compte (étudiants) ───────────────────────────────────
# Défini ici (pas dans routes/auth.py, qui l'utilisait auparavant) pour que
# paseto_required puisse la revérifier à CHAQUE requête, pas seulement au
# login. Correctif fiabilité (29/08, retour utilisateur) : avant ce
# correctif, le "force login" (bouton "Se déconnecter de l'autre appareil et
# continuer ici") blocklistait bien le REFRESH token de l'ancien appareil,
# mais son ACCESS token déjà émis (stateless, jamais revérifié par requête)
# restait pleinement valide jusqu'à 15-60 min selon la config — l'ancien
# appareil restait visiblement connecté et fonctionnel tout ce temps.
_SESSION_KEY_PREFIX = 'cei:session:active:'


def session_key(user_id: int) -> str:
    return f'{_SESSION_KEY_PREFIX}{user_id}'

# ─── Création de tokens ──────────────────────────────────────────────────────

def _now() -> datetime:
    return datetime.now(timezone.utc)

def create_access_token(user_id: int, role: str, email: str = "", sid: str | None = None) -> str:
    """sid (session id) : pour les étudiants uniquement, réutilise le hash du
    refresh token émis dans la MÊME connexion (même valeur que token_hash
    stocké dans cei:session:active:{user_id}) — permet à paseto_required de
    vérifier, à chaque requête, que cet access token appartient toujours à
    la session active la plus récente. Omis (None) pour le personnel, qui
    n'est pas soumis à la session unique."""
    now = _now()
    payload = json.dumps({
        "sub":   str(user_id),
        "role":  role,
        "email": email,
        "sid":   sid,
        "iat":   now.isoformat(),
        "exp":   (now + ACCESS_TTL).isoformat(),
        "type":  "access",
    })
    return pyseto.encode(_PRIVATE_KEY, payload).decode()

def create_refresh_token(user_id: int) -> str:
    now = _now()
    payload = json.dumps({
        "sub":  str(user_id),
        "iat":  now.isoformat(),
        "exp":  (now + REFRESH_TTL).isoformat(),
        "type": "refresh",
    })
    return pyseto.encode(_PRIVATE_KEY, payload).decode()

# ─── Décodage et validation ──────────────────────────────────────────────────

def decode_token(token: str) -> dict:
    """Décode + valide la signature + vérifie l'expiration."""
    decoded = pyseto.decode(_PUBLIC_KEY, token)
    payload = json.loads(decoded.payload)
    exp_str = payload.get("exp")
    if not exp_str:
        raise ValueError("Champ 'exp' manquant dans le token")
    exp = datetime.fromisoformat(exp_str)
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    if _now() > exp:
        raise ValueError("Token expiré")
    return payload

# ─── Décorateur principal ─────────────────────────────────────────────────────

def paseto_required(f):
    """
    Remplace @jwt_required().
    Lit : Authorization: Bearer <token>
    Injecte : flask.g.token_data  (dict avec sub, role, email, iat, exp, type)
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return jsonify({"error": "Token manquant ou format invalide"}), 401
        token = auth[7:].strip()
        try:
            payload = decode_token(token)
            if payload.get("type") != "access":
                raise ValueError("Ce n'est pas un access token")
            g.token_data = payload
        except ValueError as e:
            return jsonify({"error": str(e)}), 401
        except Exception:
            return jsonify({"error": "Token invalide ou corrompu"}), 401

        # Session unique (étudiants) — revérifiée à CHAQUE requête, pas
        # seulement au login. Ne rejette QUE sur une non-correspondance
        # explicite (quelqu'un d'autre a fait un "force login" depuis, ce
        # token appartient à une session désormais remplacée) — ne rejette
        # jamais sur une absence de résultat (clé expirée normalement,
        # déconnexion explicite ailleurs, ou Redis momentanément
        # indisponible) : un access token reste une preuve cryptographique
        # valide de l'identité, et faire échouer TOUS les étudiants sur un
        # simple accroc Redis serait bien pire que la fenêtre de session
        # partagée que ce correctif referme. Même logique fail-open que
        # cache_should_score (cache.py) pour la même raison.
        if payload.get("role") == "student" and payload.get("sid"):
            try:
                from cache import cache_get
                existing = cache_get(session_key(int(payload["sub"])))
                if existing and existing.get("token_hash") != payload["sid"]:
                    return jsonify({
                        "error": "Session remplacée — vous vous êtes reconnecté depuis un autre appareil.",
                        "session_superseded": True,
                    }), 401
            except Exception:
                pass  # Redis indisponible ou erreur inattendue — fail-open, voir commentaire ci-dessus

        return f(*args, **kwargs)
    return decorated

# ─── Accesseurs — remplacent get_jwt_identity() / get_jwt() ──────────────────

def get_current_user_id() -> int:
    """Remplace get_jwt_identity()"""
    return int(g.token_data["sub"])

def get_current_user_role() -> str:
    """Remplace get_jwt().get('role')"""
    return g.token_data.get("role", "")

def get_current_user_email() -> str:
    return g.token_data.get("email", "")

# ─── Helpers cookies httpOnly (refresh token) ─────────────────────────────────

def set_refresh_cookie(response, refresh_token: str):
    """Pose le refresh token en cookie httpOnly (invisible au JS → XSS-safe)."""
    response.set_cookie(
        COOKIE_NAME,
        refresh_token,
        httponly=True,
        secure=os.getenv("APP_URL", "http://").startswith("https://"),
        samesite="Lax",
        max_age=int(REFRESH_TTL.total_seconds()),
        path="/api/auth",
    )

def clear_refresh_cookie(response):
    response.delete_cookie(COOKIE_NAME, path="/api/auth")

def get_refresh_token_from_cookie() -> str | None:
    return request.cookies.get(COOKIE_NAME)

# ─── Hash pour TokenBlocklist ─────────────────────────────────────────────────

def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()
