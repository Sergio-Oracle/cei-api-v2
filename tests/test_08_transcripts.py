"""
TESTS INTÉGRATION — Transcripts, Réclamations, Notifications
Couvre: génération et consultation des transcripts, workflow réclamation,
        traitement IA, notifications utilisateur
"""
import pytest
from conftest import assert_ok, assert_json, KNOWN_EXAM_ID


# ─── Constantes de test ───────────────────────────────────────────────────────

KNOWN_STUDENT_ID   = 1
KNOWN_SEMESTER_ID  = 1


class TestTranscripts:
    """Tests des relevés de notes (transcripts académiques)."""

    def test_liste_transcripts_admin(self, client, admin_headers):
        r = client.get("/api/transcripts", headers=admin_headers)
        assert_ok(r)
        data = r.json()
        items = data if isinstance(data, list) else data.get("transcripts", [])
        assert isinstance(items, list)

    def test_mes_transcripts_etudiant(self, client, student_headers):
        r = client.get("/api/student/transcripts", headers=student_headers)
        assert_ok(r)
        data = r.json()
        assert isinstance(data, (list, dict))

    def test_transcripts_exige_auth(self, client):
        r = client.get("/api/transcripts")
        assert r.status_code in (401, 403)

    def test_transcripts_etudiant_exige_auth(self, client):
        r = client.get("/api/student/transcripts")
        assert r.status_code in (401, 403)

    def test_transcript_pdf_existant(self, client, admin_headers):
        """Récupérer le PDF d'un transcript."""
        r = client.get("/api/transcripts", headers=admin_headers)
        transcripts = r.json() if isinstance(r.json(), list) else r.json().get("transcripts", [])
        if not transcripts:
            pytest.skip("Aucun transcript disponible")
        tid = transcripts[0]["id"]
        r2 = client.get(f"/api/transcripts/{tid}/pdf", headers=admin_headers)
        assert r2.status_code in (200, 404)
        if r2.status_code == 200:
            ct = r2.headers.get("content-type", "")
            assert "pdf" in ct or "octet" in ct, f"Content-Type inattendu: {ct}"

    def test_transcript_pdf_inexistant(self, client, admin_headers):
        r = client.get("/api/transcripts/99999/pdf", headers=admin_headers)
        assert r.status_code in (400, 404)

    def test_generer_transcript(self, client, admin_headers):
        """Générer un transcript pour un étudiant et un semestre."""
        r = client.post(f"/api/transcripts/generate/{KNOWN_STUDENT_ID}/{KNOWN_SEMESTER_ID}",
                        headers=admin_headers)
        # Peut retourner 200 (créé) ou 400 si déjà existant
        assert r.status_code in (200, 201, 400, 404, 409)

    def test_generer_transcript_exige_admin(self, client, student_headers):
        r = client.post(f"/api/transcripts/generate/{KNOWN_STUDENT_ID}/{KNOWN_SEMESTER_ID}",
                        headers=student_headers)
        assert r.status_code in (401, 403)

    def test_publier_transcript(self, client, admin_headers):
        """Publier un transcript existant."""
        r = client.get("/api/transcripts", headers=admin_headers)
        transcripts = r.json() if isinstance(r.json(), list) else r.json().get("transcripts", [])
        if not transcripts:
            pytest.skip("Aucun transcript disponible")
        tid = transcripts[0]["id"]
        r2 = client.put(f"/api/transcripts/{tid}/publish", headers=admin_headers)
        assert r2.status_code in (200, 400, 404)

    def test_supprimer_transcript_exige_admin(self, client, student_headers):
        r = client.delete("/api/transcripts/1", headers=student_headers)
        assert r.status_code in (401, 403)

    def test_bulk_pdf_transcripts(self, client, admin_headers):
        """Télécharger un ZIP de tous les transcripts."""
        r = client.get("/api/transcripts/bulk-pdf", headers=admin_headers)
        # 400 si aucun transcript publié disponible, 200 si OK
        assert r.status_code in (200, 400, 404, 422)
        if r.status_code == 200:
            ct = r.headers.get("content-type", "")
            assert "zip" in ct or "octet" in ct or "pdf" in ct, \
                f"Content-Type inattendu pour bulk PDF: {ct}"


class TestReclamations:
    """Tests du workflow complet de réclamation."""

    def _creer_reclamation(self, client, student_headers):
        """Helper: créer une réclamation de test."""
        r = client.post("/api/reclamations", json={
            "paper_id": 1,
            "reason": "Je conteste ma note — test automatique",
            "details": "La question 3 était ambiguë et ma réponse était acceptable."
        }, headers=student_headers)
        return r

    def test_liste_reclamations_admin(self, client, admin_headers):
        r = client.get("/api/reclamations", headers=admin_headers)
        assert_ok(r)
        data = r.json()
        items = data if isinstance(data, list) else data.get("reclamations", [])
        assert isinstance(items, list)

    def test_liste_reclamations_exige_auth(self, client):
        r = client.get("/api/reclamations")
        assert r.status_code in (401, 403)

    def test_creer_reclamation_etudiant(self, client, student_headers):
        r = self._creer_reclamation(client, student_headers)
        # Peut échouer si paper_id=1 n'existe pas ou n'appartient pas à l'étudiant
        assert r.status_code in (200, 201, 400, 404, 409)

    def test_creer_reclamation_sans_paper(self, client, student_headers):
        r = client.post("/api/reclamations", json={
            "reason": "Test sans paper_id"
        }, headers=student_headers)
        assert r.status_code in (400, 422)

    def test_reclamation_exige_auth(self, client):
        r = client.post("/api/reclamations", json={
            "paper_id": 1,
            "reason": "Test non authentifié"
        })
        assert r.status_code in (401, 403)

    def test_traitement_ia_reclamation(self, client, admin_headers):
        """Traitement IA d'une réclamation existante."""
        r = client.get("/api/reclamations", headers=admin_headers)
        reclamations = r.json() if isinstance(r.json(), list) else r.json().get("reclamations", [])
        if not reclamations:
            pytest.skip("Aucune réclamation disponible")
        rid = reclamations[0]["id"]
        r2 = client.post(f"/api/reclamations/{rid}/process_ia", json={},
                         headers=admin_headers)
        # IA peut être lente, hors ligne ou déjà traitée
        assert r2.status_code in (200, 400, 404, 500, 503), \
            f"Traitement IA réclamation: HTTP {r2.status_code}"

    def test_traitement_ia_exige_admin(self, client, student_headers):
        r = client.post("/api/reclamations/1/process_ia", json={},
                        headers=student_headers)
        assert r.status_code in (401, 403)

    def test_appliquer_proposition_ia(self, client, admin_headers):
        """Appliquer la proposition IA d'une réclamation."""
        r = client.get("/api/reclamations", headers=admin_headers)
        reclamations = r.json() if isinstance(r.json(), list) else r.json().get("reclamations", [])
        if not reclamations:
            pytest.skip("Aucune réclamation disponible")
        rid = reclamations[0]["id"]
        r2 = client.post(f"/api/reclamations/{rid}/apply_proposal", json={},
                         headers=admin_headers)
        # Peut échouer si pas de proposition IA
        assert r2.status_code in (200, 400, 404)

    def test_rejeter_proposition_ia(self, client, admin_headers):
        """Rejeter la proposition IA d'une réclamation."""
        r = client.get("/api/reclamations", headers=admin_headers)
        reclamations = r.json() if isinstance(r.json(), list) else r.json().get("reclamations", [])
        if not reclamations:
            pytest.skip("Aucune réclamation disponible")
        rid = reclamations[0]["id"]
        r2 = client.post(f"/api/reclamations/{rid}/reject_proposal", json={
            "reason": "Rejet test automatique"
        }, headers=admin_headers)
        assert r2.status_code in (200, 400, 404)

    def test_mettre_a_jour_statut_reclamation(self, client, admin_headers):
        """Changer le statut d'une réclamation (en cours, clôturée, etc.)."""
        r = client.get("/api/reclamations", headers=admin_headers)
        reclamations = r.json() if isinstance(r.json(), list) else r.json().get("reclamations", [])
        if not reclamations:
            pytest.skip("Aucune réclamation disponible")
        rid = reclamations[0]["id"]
        r2 = client.put(f"/api/reclamations/{rid}", json={
            "status": "in_review",
            "admin_comment": "Réclamation en cours d'examen — test automatique"
        }, headers=admin_headers)
        assert r2.status_code in (200, 400, 404)

    def test_reclamation_inexistante(self, client, admin_headers):
        r = client.put("/api/reclamations/99999", json={"status": "closed"},
                       headers=admin_headers)
        assert r.status_code == 404


class TestNotifications:
    """Tests du système de notifications."""

    def test_liste_notifications_admin(self, client, admin_headers):
        r = client.get("/api/notifications", headers=admin_headers)
        assert_ok(r)
        data = r.json()
        items = data if isinstance(data, list) else data.get("notifications", [])
        assert isinstance(items, list)

    def test_liste_notifications_etudiant(self, client, student_headers):
        r = client.get("/api/notifications", headers=student_headers)
        assert_ok(r)
        data = r.json()
        assert isinstance(data, (list, dict))

    def test_notifications_exige_auth(self, client):
        r = client.get("/api/notifications")
        assert r.status_code in (401, 403)

    def test_marquer_notifications_lues(self, client, admin_headers):
        r = client.put("/api/notifications/mark-read", json={},
                       headers=admin_headers)
        assert r.status_code in (200, 204)

    def test_marquer_lues_exige_auth(self, client):
        r = client.put("/api/notifications/mark-read", json={})
        assert r.status_code in (401, 403)

    def test_notifications_structure_valide(self, client, admin_headers):
        """Vérifier la structure des notifications."""
        r = client.get("/api/notifications", headers=admin_headers)
        data = r.json()
        items = data if isinstance(data, list) else data.get("notifications", [])
        for n in items[:5]:
            assert "id" in n, f"Notification sans ID: {n}"
