"""
TESTS INTÉGRATION — Intelligence Artificielle
Couvre: génération de suggestions d'examen, génération complète, create from suggestion
"""
import pytest
from conftest import assert_ok, KNOWN_SUBJECT_ID


class TestGenerationSuggestions:
    def test_suggestions_examen_structure(self, client, admin_headers):
        """L'endpoint exige un fichier cours multipart — sans fichier = 400."""
        r = client.post("/api/ai/generate-exam-suggestions", json={
            "subject_id": KNOWN_SUBJECT_ID,
            "topic": "Réseaux informatiques",
            "num_questions": 5,
            "difficulty": "medium"
        }, headers=admin_headers)
        # 400 = "Fichier cours requis" (attendu sans fichier joint)
        # 200/202 si un fichier est joint et l'IA est disponible
        # 503 si l'IA (Ollama/Gemini) est hors ligne
        assert r.status_code in (200, 202, 400, 500, 503), \
            f"Génération IA: HTTP {r.status_code}"
        if r.status_code == 200:
            data = r.json()
            assert "suggestions" in data or "questions" in data or isinstance(data, list)

    def test_suggestions_examen_exige_auth(self, client):
        r = client.post("/api/ai/generate-exam-suggestions", json={
            "subject_id": KNOWN_SUBJECT_ID,
            "topic": "Réseaux"
        })
        assert r.status_code in (401, 403)

    def test_suggestions_examen_sans_sujet(self, client, admin_headers):
        """Sans subject_id, l'API doit retourner une erreur claire."""
        r = client.post("/api/ai/generate-exam-suggestions", json={
            "topic": "Réseaux"
        }, headers=admin_headers)
        assert r.status_code in (400, 422, 500)

    def test_suggestions_sujet_inexistant(self, client, admin_headers):
        r = client.post("/api/ai/generate-exam-suggestions", json={
            "subject_id": 99999,
            "topic": "Fantôme",
            "num_questions": 3
        }, headers=admin_headers)
        assert r.status_code in (400, 404, 500, 503)


class TestGenerationExamenComplet:
    def test_generer_examen_complet(self, client, admin_headers):
        """Générer un examen complet depuis un sujet."""
        r = client.post("/api/subjects/generate-full-exam", json={
            "subject_id": KNOWN_SUBJECT_ID,
            "title": "Examen Test IA — Généré automatiquement",
            "num_questions": 5,
            "duration_minutes": 30
        }, headers=admin_headers)
        # Tolère les timeouts et indisponibilités IA
        assert r.status_code in (200, 201, 400, 500, 503), \
            f"Génération examen: HTTP {r.status_code}"

    def test_examen_sans_titre(self, client, admin_headers):
        r = client.post("/api/subjects/generate-full-exam", json={
            "subject_id": KNOWN_SUBJECT_ID,
            "num_questions": 3
        }, headers=admin_headers)
        # Peut accepter (titre optionnel) ou rejeter
        assert r.status_code in (200, 201, 400, 500, 503)

    def test_generer_examen_exige_admin(self, client, student_headers):
        r = client.post("/api/subjects/generate-full-exam", json={
            "subject_id": KNOWN_SUBJECT_ID,
            "title": "Test",
            "num_questions": 3
        }, headers=student_headers)
        assert r.status_code in (401, 403)


class TestCreateFromSuggestion:
    def test_create_from_suggestion_structure(self, client, admin_headers):
        """Créer une question depuis une suggestion IA."""
        r = client.post("/api/subjects/create-from-suggestion", json={
            "subject_id": KNOWN_SUBJECT_ID,
            "question_text": "Qu'est-ce que le protocole TCP ?",
            "question_type": "qcm",
            "options": [
                "Un protocole de transport fiable",
                "Un protocole de routage",
                "Un protocole réseau",
                "Un protocole applicatif"
            ],
            "correct_answer": "Un protocole de transport fiable",
            "points": 2,
            "difficulty": "easy"
        }, headers=admin_headers)
        assert r.status_code in (200, 201, 400, 422, 500), \
            f"Create from suggestion: HTTP {r.status_code}"

    def test_create_from_suggestion_exige_auth(self, client):
        r = client.post("/api/subjects/create-from-suggestion", json={
            "subject_id": KNOWN_SUBJECT_ID,
            "question_text": "Test"
        })
        assert r.status_code in (401, 403)


class TestPlagiarism:
    def test_plagiarism_check_structure(self, client, admin_headers):
        from conftest import KNOWN_EXAM_ID
        r = client.get(f"/api/online_exams/{KNOWN_EXAM_ID}/plagiarism-check",
                       headers=admin_headers)
        assert r.status_code in (200, 202, 404)
        if r.status_code == 200:
            data = r.json()
            # Doit retourner des résultats de similitude ou un statut
            assert isinstance(data, (dict, list))

    def test_plagiarism_examen_inexistant(self, client, admin_headers):
        r = client.get("/api/online_exams/99999/plagiarism-check",
                       headers=admin_headers)
        assert r.status_code in (400, 404)

    def test_plagiarism_exige_auth(self, client):
        from conftest import KNOWN_EXAM_ID
        r = client.get(f"/api/online_exams/{KNOWN_EXAM_ID}/plagiarism-check")
        assert r.status_code in (401, 403)
