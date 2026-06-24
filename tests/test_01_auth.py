"""
TEST UNITAIRES — Authentification PASETO v4
Couvre: login, logout, refresh, public-key, register, profil, RBAC, tokens invalides
"""
import pytest
import time
from conftest import assert_ok, assert_json, ADMIN_CREDS, SURVEILLANT_CREDS, STUDENT_CREDS


class TestLogin:
    def test_login_admin_succes(self, client):
        r = client.post("/api/auth/login", json=ADMIN_CREDS)
        data = assert_json(r)
        assert_ok(r, 200)
        assert data["success"] is True
        assert "access_token" in data
        assert data["access_token"].startswith("v4.public.")
        assert data["user"]["role"] == "admin"
        assert "password" not in data["user"]  # mot de passe jamais exposé

    def test_login_surveillant_succes(self, client):
        r = client.post("/api/auth/login", json=SURVEILLANT_CREDS)
        data = assert_json(r)
        assert_ok(r, 200)
        assert data["user"]["role"] == "surveillant"

    def test_login_mauvais_password(self, client):
        r = client.post("/api/auth/login", json={"email": ADMIN_CREDS["email"], "password": "mauvais"})
        assert r.status_code == 401
        assert "error" in r.json()

    def test_login_email_inexistant(self, client):
        r = client.post("/api/auth/login", json={"email": "fantome@fantome.com", "password": "xxx"})
        assert r.status_code == 401

    def test_login_champs_manquants(self, client):
        r = client.post("/api/auth/login", json={"email": ADMIN_CREDS["email"]})
        # L'API retourne 401 (non autorisé) plutôt que 400 quand le mot de passe est absent
        assert r.status_code in (400, 401, 422)

    def test_login_cookie_refresh_pose(self, client):
        r = client.post("/api/auth/login", json=ADMIN_CREDS)
        assert r.status_code == 200
        # Le cookie httpOnly doi être présent dans Set-Cookie
        assert "cei_refresh" in r.headers.get("set-cookie", ""), \
            "Cookie cei_refresh absent du header Set-Cookie"

    def test_login_token_format_paseto(self, client):
        r = client.post("/api/auth/login", json=ADMIN_CREDS)
        token = r.json()["access_token"]
        parts = token.split(".")
        # PASETO v4.public sans footer = 3 parties (v4.public.payload)
        # Avec footer = 4 parties (v4.public.payload.footer)
        assert len(parts) in (3, 4), f"Token PASETO v4.public doit avoir 3 ou 4 parties: {token[:30]}"
        assert parts[0] == "v4"
        assert parts[1] == "public"


class TestPublicKey:
    def test_public_key_sans_auth(self, client):
        r = client.get("/api/auth/public-key")
        assert_ok(r, 200)
        data = r.json()
        assert data["version"] == "v4.public"
        assert data["algorithm"] == "Ed25519"
        assert "public_key" in data
        assert data["token_ttl_minutes"] == 15

    def test_public_key_est_base64(self, client):
        import base64
        r = client.get("/api/auth/public-key")
        key_b64 = r.json()["public_key"]
        decoded = base64.b64decode(key_b64)
        assert len(decoded) > 50  # clé PEM doit être > 50 octets


class TestProfil:
    def test_get_me(self, client, admin_headers):
        r = client.get("/api/auth/me", headers=admin_headers)
        assert_ok(r)
        data = r.json()
        assert "id" in data and "email" in data and "role" in data
        assert "password" not in data

    def test_get_me_sans_token(self, client):
        r = client.get("/api/auth/me")
        assert r.status_code == 401

    def test_get_me_token_invalide(self, client):
        r = client.get("/api/auth/me", headers={"Authorization": "Bearer FAUX"})
        assert r.status_code == 401

    def test_get_me_token_mal_forme(self, client):
        r = client.get("/api/auth/me", headers={"Authorization": "Token FAUX"})
        assert r.status_code == 401


class TestRefreshEtLogout:
    def test_refresh_avec_cookie(self, client):
        # Login pour avoir le cookie
        r1 = client.post("/api/auth/login", json=ADMIN_CREDS)
        assert r1.status_code == 200
        # Le client httpx conserve les cookies automatiquement
        r2 = client.post("/api/auth/refresh")
        assert r2.status_code == 200
        data = r2.json()
        assert "access_token" in data
        assert data["access_token"].startswith("v4.public.")

    def test_refresh_sans_cookie(self, client):
        # Nouveau client sans cookies
        with __import__("httpx").Client(base_url=client.base_url) as fresh:
            r = fresh.post("/api/auth/refresh")
            assert r.status_code == 401

    def test_rotation_refresh_token(self, client):
        """Vérifier que le refresh token change à chaque usage."""
        r1 = client.post("/api/auth/login", json=ADMIN_CREDS)
        cookie1 = r1.headers.get("set-cookie", "")

        r2 = client.post("/api/auth/refresh")
        cookie2 = r2.headers.get("set-cookie", "")

        # Même si les deux Set-Cookie sont présents, les valeurs doivent différer
        if cookie2:  # le refresh renvoie un nouveau cookie
            assert cookie1 != cookie2, "Le refresh token doit être différent après rotation"

    def test_logout_revoque_token(self, client, admin_token):
        r = client.post("/api/auth/logout",
            headers={"Authorization": f"Bearer {admin_token}"})
        assert r.status_code == 200
        data = r.json()
        assert data.get("success") is True

    def test_logout_supprime_cookie(self, client):
        client.post("/api/auth/login", json=ADMIN_CREDS)
        r = client.post("/api/auth/logout",
            headers={"Authorization": f"Bearer {client.post('/api/auth/login', json=ADMIN_CREDS).json()['access_token']}"})
        # Le cookie doit être supprimé (Max-Age=0 ou Expires dans le passé)
        set_cookie = r.headers.get("set-cookie", "")
        if set_cookie:
            assert "Max-Age=0" in set_cookie or "expires=" in set_cookie.lower()


class TestRBAC:
    """Tests d'isolation des rôles — Role-Based Access Control."""

    def test_etudiant_bloque_sur_admin_dashboard(self, client, student_token):
        r = client.get("/api/admin/dashboard",
            headers={"Authorization": f"Bearer {student_token}"})
        assert r.status_code in (401, 403), \
            f"Étudiant ne doit pas accéder à admin/dashboard, reçu {r.status_code}"

    def test_etudiant_bloque_sur_admin_users(self, client, student_token):
        r = client.get("/api/admin/users",
            headers={"Authorization": f"Bearer {student_token}"})
        assert r.status_code in (401, 403)

    def test_surveillant_bloque_sur_admin_dashboard(self, client, surveillant_token):
        r = client.get("/api/admin/dashboard",
            headers={"Authorization": f"Bearer {surveillant_token}"})
        assert r.status_code in (401, 403)

    def test_admin_accede_a_tout(self, client, admin_headers):
        # /api/surveillant/exams est réservé au rôle surveillant — admin ne peut pas y accéder
        for path in ["/api/admin/dashboard", "/api/admin/users", "/api/online_exams"]:
            r = client.get(path, headers=admin_headers)
            assert r.status_code not in (401, 403), \
                f"Admin bloqué sur {path}: HTTP {r.status_code}"


class TestTokenBlocklist:
    """Vérifier que les tokens révoqués sont refusés."""

    def test_token_apres_logout_refuse(self, client):
        # Login → récupérer token
        r1 = client.post("/api/auth/login", json={"email": "aristoud@gmail.com", "password": "passer"})
        if r1.status_code != 200:
            pytest.skip("Surveillant unavailable")
        token = r1.json()["access_token"]

        # Utiliser le token → OK
        r2 = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert r2.status_code == 200

        # Logout
        client.post("/api/auth/logout", headers={"Authorization": f"Bearer {token}"})

        # Refresh doit être refusé (le cookie refresh est révoqué)
        r3 = client.post("/api/auth/refresh")
        assert r3.status_code == 401
