"""
TESTS INTÉGRATION — Administration
Couvre: dashboard, CRUD utilisateurs, maquette pédagogique, imports CSV, sécurité
"""
import pytest
from conftest import assert_ok, assert_json, KNOWN_EXAM_ID, KNOWN_FORMATION_ID


class TestDashboard:
    def test_dashboard_retourne_stats(self, client, admin_headers):
        r = client.get("/api/admin/dashboard", headers=admin_headers)
        data = assert_json(r); assert_ok(r)
        champs = ["total_users","total_students","total_professors","total_surveillants",
                  "total_subjects","pending_reclamations"]
        for c in champs:
            assert c in data, f"Champ manquant dans dashboard: {c}"

    def test_dashboard_valeurs_coherentes(self, client, admin_headers):
        r = client.get("/api/admin/dashboard", headers=admin_headers)
        d = r.json()
        assert d["total_users"] >= d["total_students"] + d["total_professors"]
        assert d["total_students"] >= 0
        assert d["pending_reclamations"] >= 0

    def test_dashboard_exige_admin(self, client, surveillant_headers):
        r = client.get("/api/admin/dashboard", headers=surveillant_headers)
        assert r.status_code in (401, 403)


class TestGestionUtilisateurs:
    def test_liste_utilisateurs(self, client, admin_headers):
        r = client.get("/api/admin/users", headers=admin_headers)
        assert_ok(r)
        users = r.json()
        assert isinstance(users, list)
        assert len(users) > 0
        u = users[0]
        assert "id" in u and "email" in u and "role" in u

    def test_liste_utilisateurs_pas_de_password(self, client, admin_headers):
        r = client.get("/api/admin/users", headers=admin_headers)
        for u in r.json():
            assert "password" not in u, "Les mots de passe ne doivent jamais être exposés"

    def test_creer_et_supprimer_utilisateur(self, client, admin_headers):
        # Créer
        r_create = client.post("/api/admin/users", json={
            "email": "test_unit_delete@test-cei.sn",
            "full_name": "Test Suppression",
            "role": "student",
            "password": "Test@12345"
        }, headers=admin_headers)
        assert r_create.status_code in (200, 201), f"Création échouée: {r_create.text}"
        user_id = r_create.json().get("user", {}).get("id") or r_create.json().get("id")
        assert user_id, "ID utilisateur absent de la réponse"

        # Vérifier présence dans la liste
        r_list = client.get("/api/admin/users", headers=admin_headers)
        ids = [u["id"] for u in r_list.json()]
        assert user_id in ids

        # Supprimer
        r_del = client.delete(f"/api/admin/users/{user_id}", headers=admin_headers)
        assert r_del.status_code in (200, 204)

        # Vérifier suppression
        r_list2 = client.get("/api/admin/users", headers=admin_headers)
        ids2 = [u["id"] for u in r_list2.json()]
        assert user_id not in ids2

    def test_creer_utilisateur_email_duplique(self, client, admin_headers):
        r = client.post("/api/admin/users", json={
            "email": "serge@rtn.sn",  # déjà existant
            "full_name": "Doublon",
            "role": "student",
            "password": "Test@12345"
        }, headers=admin_headers)
        assert r.status_code in (400, 409)

    def test_creer_etudiant_sans_email(self, client, admin_headers):
        r = client.post("/api/admin/users/student-no-email", json={
            "full_name": "Étudiant Sans Email Test",
            "role": "student"
        }, headers=admin_headers)
        assert r.status_code in (200, 201), f"Attendu 200/201, reçu {r.status_code}: {r.text}"
        data = r.json()
        user_id = data.get("user", {}).get("id") or data.get("id")
        if user_id:
            client.delete(f"/api/admin/users/{user_id}", headers=admin_headers)

    def test_liste_etudiants(self, client, admin_headers):
        r = client.get("/api/students/list", headers=admin_headers)
        assert_ok(r)
        data = r.json()
        students = data if isinstance(data, list) else data.get("students", [])
        assert len(students) > 0

    def test_liste_surveillants(self, client, admin_headers):
        r = client.get("/api/users/proctors", headers=admin_headers)
        assert_ok(r)
        data = r.json()
        proctors = data if isinstance(data, list) else data.get("proctors", [])
        assert len(proctors) >= 1
        for p in proctors:
            assert p.get("role") in ("surveillant", "professor", "admin"), \
                f"Un proctor a un rôle inattendu: {p.get('role')}"


class TestMaquettePedagogique:
    def test_lister_formations(self, client, admin_headers):
        r = client.get("/api/formations", headers=admin_headers)
        assert_ok(r)
        items = r.json() if isinstance(r.json(), list) else r.json().get("formations", [])
        assert len(items) >= 1

    def test_lister_semestres_formation(self, client, admin_headers):
        r = client.get(f"/api/formations/{KNOWN_FORMATION_ID}/semesters", headers=admin_headers)
        assert_ok(r)

    def test_lister_ues(self, client, admin_headers):
        r = client.get("/api/ues", headers=admin_headers)
        assert_ok(r)

    def test_lister_ecs(self, client, admin_headers):
        r = client.get("/api/ecs", headers=admin_headers)
        assert_ok(r)
        ecs = r.json() if isinstance(r.json(), list) else r.json().get("ecs", [])
        assert len(ecs) >= 1

    def test_creer_supprimer_formation(self, client, admin_headers):
        r = client.post("/api/admin/formations", json={
            "name": "Test Formation Temporaire",
            "code": "TST-AUTO-001",
            "description": "Formation créée par les tests unitaires"
        }, headers=admin_headers)
        assert r.status_code in (200, 201), f"{r.status_code}: {r.text}"
        formation_id = r.json().get("formation", {}).get("id") or r.json().get("id")
        assert formation_id

        r_del = client.delete(f"/api/admin/formations/{formation_id}", headers=admin_headers)
        assert r_del.status_code in (200, 204)

    def test_exams_history(self, client, admin_headers):
        r = client.get("/api/admin/exams_history", headers=admin_headers)
        assert_ok(r)

    def test_security_report(self, client, admin_headers):
        r = client.get("/api/admin/security_report", headers=admin_headers)
        assert_ok(r)


class TestImportCSV:
    def test_template_users_telecharge(self, client, admin_headers):
        r = client.get("/api/admin/users/csv-template", headers=admin_headers)
        assert_ok(r)
        ct = r.headers.get("content-type", "")
        assert "csv" in ct or "text" in ct or "excel" in ct or "octet" in ct, \
            f"Content-Type inattendu: {ct}"

    def test_template_maquette_telecharge(self, client, admin_headers):
        r = client.get("/api/admin/maquette/csv-template", headers=admin_headers)
        assert_ok(r)

    def test_import_csv_users_invalide(self, client, admin_headers):
        import io
        bad_csv = b"not,a,valid,format\n1,2,3,4\n"
        r = client.post("/api/admin/users/import-csv",
            files={"file": ("users.csv", io.BytesIO(bad_csv), "text/csv")},
            headers=admin_headers)
        # Doit soit réussir (données ignorées) soit retourner une erreur claire
        assert r.status_code in (200, 400, 422), f"Code inattendu: {r.status_code}"
