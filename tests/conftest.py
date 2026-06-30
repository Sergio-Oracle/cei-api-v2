"""
conftest.py — Fixtures pytest partagées pour CEI API v2
"""
import os
import sys
import pytest
import httpx

# S'assurer que le répertoire parent est dans le path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ─── Configuration ────────────────────────────────────────────────────────────

BASE_URL = os.getenv("TEST_BASE_URL", "http://62.171.190.6:8100")
TIMEOUT  = float(os.getenv("TEST_TIMEOUT", "30"))

# Credentials de test (existent déjà en base)
ADMIN_CREDS     = {"email": "serge@rtn.sn",                  "password": "passer"}
SURVEILLANT_CREDS = {"email": "aristoud@gmail.com",           "password": "passer"}
STUDENT_CREDS   = {"email": "laprincesseawa99@gmail.com",     "password": "passer"}

# IDs connus en base (examen "Réseau de campus")
KNOWN_EXAM_ID      = 4
KNOWN_SUBJECT_ID   = 1
KNOWN_EC_ID        = 1
KNOWN_FORMATION_ID = 1

# ─── Client HTTP partagé ──────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def client():
    """Client HTTP réutilisable sur toute la session de tests."""
    with httpx.Client(base_url=BASE_URL, timeout=TIMEOUT) as c:
        yield c

@pytest.fixture(scope="session")
def admin_token(client):
    """Token PASETO admin valide pour toute la session."""
    r = client.post("/api/auth/login", json=ADMIN_CREDS)
    assert r.status_code == 200, f"Login admin échoué: {r.text}"
    return r.json()["access_token"]

@pytest.fixture(scope="session")
def surveillant_token(client):
    """Token PASETO surveillant."""
    r = client.post("/api/auth/login", json=SURVEILLANT_CREDS)
    if r.status_code != 200:
        pytest.skip("Surveillant indisponible")
    return r.json()["access_token"]

@pytest.fixture(scope="session")
def student_token(client):
    """Token PASETO étudiant."""
    r = client.post("/api/auth/login", json=STUDENT_CREDS)
    if r.status_code != 200:
        pytest.skip("Étudiant indisponible")
    return r.json()["access_token"]

@pytest.fixture
def admin_headers(admin_token):
    return {"Authorization": f"Bearer {admin_token}"}

@pytest.fixture
def surveillant_headers(surveillant_token):
    return {"Authorization": f"Bearer {surveillant_token}"}

@pytest.fixture
def student_headers(student_token):
    return {"Authorization": f"Bearer {student_token}"}

# ─── Helpers ─────────────────────────────────────────────────────────────────

def assert_ok(response, expected=200):
    assert response.status_code == expected, (
        f"Attendu HTTP {expected}, reçu {response.status_code}\n"
        f"Body: {response.text[:300]}"
    )

def assert_json(response):
    assert "application/json" in response.headers.get("content-type", ""), \
        f"Réponse non-JSON: {response.headers.get('content-type')}"
    return response.json()
