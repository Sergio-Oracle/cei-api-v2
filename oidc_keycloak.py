"""
CEI en tant que client OIDC (Relying Party) du Keycloak de l'UNCHK
(realm "UNCHK", https://senid.unchk.sn/realms/UNCHK — le même serveur que
Moodle utilise déjà avec client_id=moodle). Sert uniquement à AUTHENTIFIER
l'utilisateur (prouver qui il est) — le rôle CEI reste entièrement géré par
l'admin CEI (voir routes/oidc.py : jamais d'auto-création de compte).

Config 100% .env, à l'image de auth_paseto.py — échec explicite si une
variable manque, pas de valeur par défaut silencieuse (une SSO mal
configurée doit casser bruyamment dès la première tentative, pas produire
un comportement bancal en silence).
"""
import os
import requests
import jwt
from jwt import PyJWKClient

_jwks_client_cache: PyJWKClient | None = None


def _require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"[oidc_keycloak] Variable d'environnement manquante : {name}")
    return value


def issuer() -> str:
    return _require_env('OIDC_ISSUER').rstrip('/')


def client_id() -> str:
    return _require_env('OIDC_CLIENT_ID')


def client_secret() -> str:
    return _require_env('OIDC_CLIENT_SECRET')


def redirect_uri() -> str:
    return _require_env('OIDC_REDIRECT_URI')


def scopes() -> str:
    return os.getenv('OIDC_SCOPES', 'openid profile email')


def _authorization_endpoint() -> str:
    return f"{issuer()}/protocol/openid-connect/auth"


def _token_endpoint() -> str:
    return f"{issuer()}/protocol/openid-connect/token"


def _jwks_uri() -> str:
    return f"{issuer()}/protocol/openid-connect/certs"


def build_authorization_url(state: str, nonce: str) -> str:
    from urllib.parse import urlencode
    params = {
        'response_type': 'code',
        'client_id': client_id(),
        'scope': scopes(),
        'nonce': nonce,
        'state': state,
        'redirect_uri': redirect_uri(),
    }
    return f"{_authorization_endpoint()}?{urlencode(params)}"


def exchange_code_for_tokens(code: str) -> dict:
    resp = requests.post(_token_endpoint(), data={
        'grant_type': 'authorization_code',
        'code': code,
        'redirect_uri': redirect_uri(),
        'client_id': client_id(),
        'client_secret': client_secret(),
    }, timeout=10)
    resp.raise_for_status()
    return resp.json()


def _jwks_client() -> PyJWKClient:
    global _jwks_client_cache
    if _jwks_client_cache is None:
        _jwks_client_cache = PyJWKClient(_jwks_uri(), cache_keys=True, lifespan=3600)
    return _jwks_client_cache


def validate_id_token(id_token: str, expected_nonce: str) -> dict:
    """Valide la signature (JWKS Keycloak), l'audience, l'émetteur et
    l'expiration — puis le nonce (spécifique OIDC, PyJWT ne le vérifie pas
    lui-même). Lève ValueError sur tout problème."""
    try:
        signing_key = _jwks_client().get_signing_key_from_jwt(id_token)
        claims = jwt.decode(
            id_token, signing_key.key, algorithms=['RS256'],
            audience=client_id(), issuer=issuer(),
            # Léger décalage d'horloge constaté entre ce serveur et Keycloak
            # UNCHK (~17s, deux infrastructures distinctes) — sans marge,
            # PyJWT rejette le token comme "not yet valid (iat)" alors qu'il
            # est parfaitement légitime. 60s absorbe une dérive raisonnable
            # sans affaiblir la vérification d'expiration de façon notable.
            leeway=60,
        )
    except Exception as e:
        raise ValueError(f"id_token invalide : {e}")

    if claims.get('nonce') != expected_nonce:
        raise ValueError("nonce invalide (rejeu possible)")
    return claims
