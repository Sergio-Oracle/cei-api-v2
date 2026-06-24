"""
TESTS INTÉGRATION — Examens en ligne, sujets, copies, question bank
"""
import pytest
from conftest import assert_ok, assert_json, KNOWN_EXAM_ID, KNOWN_SUBJECT_ID


class TestSujets:
    def test_lister_sujets(self, client, admin_headers):
        r = client.get("/api/subjects", headers=admin_headers)
        assert_ok(r)
        items = r.json() if isinstance(r.json(), list) else []
        assert len(items) >= 1

    def test_detail_sujet(self, client, admin_headers):
        r = client.get(f"/api/subjects/{KNOWN_SUBJECT_ID}", headers=admin_headers)
        assert_ok(r)
        data = r.json()
        assert "id" in data or "subject" in data

    def test_sujet_inexistant(self, client, admin_headers):
        r = client.get("/api/subjects/99999", headers=admin_headers)
        assert r.status_code == 404


class TestExamensEnLigne:
    def test_lister_examens(self, client, admin_headers):
        r = client.get("/api/online_exams", headers=admin_headers)
        assert_ok(r)
        items = r.json() if isinstance(r.json(), list) else r.json().get("exams", [])
        assert len(items) >= 1

    def test_details_examen(self, client, admin_headers):
        r = client.get(f"/api/online_exams/{KNOWN_EXAM_ID}/details", headers=admin_headers)
        assert_ok(r)
        data = r.json()
        exam = data.get("exam", data)
        assert exam.get("title")
        assert exam.get("status") in ("draft","active","closed","paused","scheduled")

    def test_examen_inexistant(self, client, admin_headers):
        r = client.get("/api/online_exams/99999/details", headers=admin_headers)
        assert r.status_code == 404

    def test_tentatives_examen(self, client, admin_headers):
        r = client.get(f"/api/online_exams/{KNOWN_EXAM_ID}/attempts", headers=admin_headers)
        assert_ok(r)
        data = r.json()
        attempts = data if isinstance(data, list) else data.get("attempts", [])
        assert len(attempts) > 0, "L'examen devrait avoir des tentatives"

    def test_stats_examen(self, client, admin_headers):
        r = client.get(f"/api/online_exams/{KNOWN_EXAM_ID}/stats", headers=admin_headers)
        assert_ok(r)
        data = r.json()
        champs = ["avg_score","avg_risk","banned"]
        for c in champs:
            assert c in data, f"Champ stats manquant: {c}"

    def test_bilan_examen(self, client, admin_headers):
        r = client.get(f"/api/online_exams/{KNOWN_EXAM_ID}/bilan", headers=admin_headers)
        assert_ok(r)
        data = r.json()
        assert "exam_title" in data or "attempts" in data

    def test_incidents_examen(self, client, admin_headers):
        r = client.get(f"/api/online_exams/{KNOWN_EXAM_ID}/incidents", headers=admin_headers)
        assert_ok(r)
        items = r.json() if isinstance(r.json(), list) else r.json().get("incidents", [])
        assert len(items) > 0, "L'examen doit avoir des incidents enregistrés"

    def test_qrcode_examen(self, client, admin_headers):
        r = client.get(f"/api/online_exams/{KNOWN_EXAM_ID}/qrcode", headers=admin_headers)
        assert_ok(r)
        ct = r.headers.get("content-type", "")
        assert "image" in ct or "json" in ct, f"QR code inattendu: {ct}"

    def test_export_csv_resultats(self, client, admin_headers):
        r = client.get(f"/api/online_exams/{KNOWN_EXAM_ID}/export-csv", headers=admin_headers)
        assert_ok(r)
        ct = r.headers.get("content-type", "")
        assert "csv" in ct or "text" in ct or "octet" in ct, f"CT: {ct}"

    def test_plagiarism_check(self, client, admin_headers):
        r = client.get(f"/api/online_exams/{KNOWN_EXAM_ID}/plagiarism-check", headers=admin_headers)
        assert r.status_code in (200, 202)

    def test_creer_examen_complet(self, client, admin_headers):
        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone.utc)
        start = (now + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S")
        end   = (now + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S")
        payload = {
            "title": "Examen Test Unitaire — À supprimer",
            "subject_id": KNOWN_SUBJECT_ID,
            "duration_minutes": 60,
            "ec_id": 1,
            "start_time": start,
            "end_time": end,
            "shuffle_questions": True,
            "max_attempts": 1,
            "face_detection_required": False,
            "instructions": "Cet examen est créé par les tests automatiques."
        }
        r = client.post("/api/online_exams", json=payload, headers=admin_headers)
        assert r.status_code in (200, 201), f"{r.status_code}: {r.text}"
        exam_id = r.json().get("exam", {}).get("id") or r.json().get("id")
        assert exam_id, "ID examen manquant"

        # Nettoyage
        r_del = client.delete(f"/api/online_exams/{exam_id}", headers=admin_headers)
        assert r_del.status_code in (200, 204)

    def test_liste_recordings(self, client, admin_headers):
        r = client.get(f"/api/online_exams/{KNOWN_EXAM_ID}/recordings", headers=admin_headers)
        # 500 possible si LiveKit n'est pas configuré dans cet environnement
        assert r.status_code in (200, 404, 500)


class TestTentatives:
    def test_resultat_tentative(self, client, admin_headers):
        # Récupérer une tentative existante
        r = client.get(f"/api/online_exams/{KNOWN_EXAM_ID}/attempts", headers=admin_headers)
        attempts = r.json() if isinstance(r.json(), list) else r.json().get("attempts", [])
        if not attempts:
            pytest.skip("Aucune tentative disponible")
        attempt_id = attempts[0]["id"]

        r2 = client.get(f"/api/exam_attempts/{attempt_id}/result", headers=admin_headers)
        assert r2.status_code in (200, 404)

    def test_rapport_integrite(self, client, admin_headers):
        r = client.get(f"/api/online_exams/{KNOWN_EXAM_ID}/attempts", headers=admin_headers)
        attempts = r.json() if isinstance(r.json(), list) else r.json().get("attempts", [])
        if not attempts:
            pytest.skip("Aucune tentative disponible")
        attempt_id = attempts[0]["id"]

        r2 = client.get(f"/api/exam_attempts/{attempt_id}/integrity-report", headers=admin_headers)
        assert r2.status_code in (200, 404)


class TestBanqueQuestions:
    def test_lister_questions(self, client, admin_headers):
        r = client.get("/api/question_bank", headers=admin_headers)
        assert_ok(r)

    def test_creer_supprimer_question(self, client, admin_headers):
        r = client.post("/api/question_bank", json={
            "content": "Question test unitaire — à supprimer",
            "question_text": "Question test unitaire — à supprimer",
            "question_type": "qcm",
            "options": ["A", "B", "C", "D"],
            "correct_answer": "A",
            "points": 2,
            "ec_id": 1
        }, headers=admin_headers)
        assert r.status_code in (200, 201), f"{r.status_code}: {r.text}"
        q_id = r.json().get("question", {}).get("id") or r.json().get("id")
        if q_id:
            r_del = client.delete(f"/api/question_bank/{q_id}", headers=admin_headers)
            assert r_del.status_code in (200, 204)
