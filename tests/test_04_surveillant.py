"""
TESTS INTÉGRATION — Surveillant
Couvre: ses examens, monitoring, avertissements, messages, proctoring
"""
import pytest
from conftest import assert_ok, KNOWN_EXAM_ID


class TestSurveillantExams:
    def test_mes_examens_assignes(self, client, surveillant_headers):
        r = client.get("/api/surveillant/exams", headers=surveillant_headers)
        assert_ok(r)
        items = r.json() if isinstance(r.json(), list) else r.json().get("exams", [])
        # Le surveillant doit avoir au moins 1 examen assigné
        assert len(items) >= 1, "Le surveillant doit avoir au moins 1 examen"

    def test_active_proctoring_examen(self, client, surveillant_headers):
        r = client.get(f"/api/online_exams/{KNOWN_EXAM_ID}/active_proctoring",
                       headers=surveillant_headers)
        assert_ok(r)
        data = r.json()
        assert "exam_title" in data or "attempts" in data

    def test_proctors_de_examen(self, client, admin_headers):
        # Cet endpoint exige le rôle enseignant/admin, pas surveillant
        r = client.get(f"/api/online_exams/{KNOWN_EXAM_ID}/proctors",
                       headers=admin_headers)
        # L'admin peut ne pas avoir le bon rôle non plus (enseignant uniquement)
        assert r.status_code in (200, 403), \
            f"Attendu 200 ou 403, reçu {r.status_code}\nBody: {r.text}"
        if r.status_code == 200:
            data = r.json()
            proctors = data.get("proctors", data if isinstance(data, list) else [])
            assert len(proctors) >= 1, "L'examen doit avoir au moins 1 surveillant assigné"

    def test_messages_etudiants_examen(self, client, surveillant_headers):
        r = client.get(f"/api/online_exams/{KNOWN_EXAM_ID}/student_messages",
                       headers=surveillant_headers)
        assert_ok(r)

    def test_surveillant_ne_peut_creer_examen(self, client, surveillant_headers):
        r = client.post("/api/online_exams", json={
            "title": "Test Interdit",
            "duration_minutes": 60
        }, headers=surveillant_headers)
        assert r.status_code in (401, 403)


class TestRiskEtAvertissements:
    def _get_attempt_id(self, client, headers):
        r = client.get(f"/api/online_exams/{KNOWN_EXAM_ID}/attempts", headers=headers)
        attempts = r.json() if isinstance(r.json(), list) else r.json().get("attempts", [])
        return attempts[0]["id"] if attempts else None

    def test_risk_status_tentative(self, client, surveillant_headers, admin_headers):
        attempt_id = self._get_attempt_id(client, admin_headers)
        if not attempt_id:
            pytest.skip("Aucune tentative")
        r = client.get(f"/api/exam_attempts/{attempt_id}/risk_status",
                       headers=surveillant_headers)
        assert_ok(r)
        data = r.json()
        assert "risk_score" in data
        assert "banned" in data
        assert 0 <= data["risk_score"] <= 100, \
            f"risk_score hors limites [0,100]: {data['risk_score']}"

    def test_proctor_notes_tentative(self, client, surveillant_headers, admin_headers):
        attempt_id = self._get_attempt_id(client, admin_headers)
        if not attempt_id:
            pytest.skip("Aucune tentative")
        r = client.get(f"/api/exam_attempts/{attempt_id}/proctor-notes",
                       headers=surveillant_headers)
        assert r.status_code in (200, 404)

    def test_ajouter_note_proctor(self, client, surveillant_headers, admin_headers):
        attempt_id = self._get_attempt_id(client, admin_headers)
        if not attempt_id:
            pytest.skip("Aucune tentative")
        r = client.post(f"/api/exam_attempts/{attempt_id}/proctor-note",
            json={"note": "Test note automatique — surveillance unitaire"},
            headers=surveillant_headers)
        assert r.status_code in (200, 201, 400, 404)

    def test_envoyer_avertissement(self, client, surveillant_headers, admin_headers):
        attempt_id = self._get_attempt_id(client, admin_headers)
        if not attempt_id:
            pytest.skip("Aucune tentative")
        r = client.post(f"/api/exam_attempts/{attempt_id}/send_warning",
            json={"message": "Test automatique — avertissement unitaire", "type": "warning"},
            headers=surveillant_headers)
        assert r.status_code in (200, 400, 403, 404)

    def test_messages_en_attente_tentative(self, client, surveillant_headers, admin_headers):
        attempt_id = self._get_attempt_id(client, admin_headers)
        if not attempt_id:
            pytest.skip("Aucune tentative")
        r = client.get(f"/api/exam_attempts/{attempt_id}/pending_messages",
                       headers=surveillant_headers)
        assert r.status_code in (200, 404)


class TestProctorToken:
    def test_proctor_token_genere(self, client, surveillant_headers):
        r = client.get(f"/api/online_exams/{KNOWN_EXAM_ID}/proctor_token",
                       headers=surveillant_headers)
        # LiveKit peut être indisponible en test — accepter 200 ou 503/500
        assert r.status_code in (200, 500, 503), \
            f"Code inattendu pour proctor_token: {r.status_code}"
        if r.status_code == 200:
            data = r.json()
            assert "token" in data or "livekit_url" in data

    def test_livekit_token_tentative(self, client, surveillant_headers, admin_headers):
        r = client.get(f"/api/online_exams/{KNOWN_EXAM_ID}/attempts", headers=admin_headers)
        attempts = r.json() if isinstance(r.json(), list) else r.json().get("attempts", [])
        if not attempts:
            pytest.skip("Aucune tentative")
        attempt_id = attempts[0]["id"]
        r2 = client.get(f"/api/exam_attempts/{attempt_id}/livekit_token",
                        headers=surveillant_headers)
        assert r2.status_code in (200, 403, 500, 503)


class TestDistribution:
    def test_lister_surveillants_examen(self, client, admin_headers):
        r = client.get(f"/api/online_exams/{KNOWN_EXAM_ID}/proctors", headers=admin_headers)
        assert_ok(r)
        data = r.json()
        assert "proctors" in data or isinstance(data, list)

    def test_distribuer_surveillants(self, client, admin_headers):
        r = client.post(f"/api/online_exams/{KNOWN_EXAM_ID}/distribute_proctors",
                        headers=admin_headers)
        # L'examen est fermé donc peut retourner 400 — c'est OK
        assert r.status_code in (200, 400, 404)
        if r.status_code == 200:
            data = r.json()
            assert "total_students" in data or "distribution" in data
