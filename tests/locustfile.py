"""
TESTS DE CHARGE — Locust — CEI API v2
Simule des scénarios réels : admin, professeur, surveillant, étudiant en parallèle.

Lancement :
    locust -f tests/locustfile.py --host=https://dev-cei.ddns.net \
           --headless -u 100 -r 10 --run-time 2m --html tests/reports/load_report.html
"""
import json
import os
import random
from locust import HttpUser, task, between, events
from locust.env import Environment

BASE = os.getenv("TEST_BASE_URL", "https://dev-cei.ddns.net")
EXAM_ID = 4


# ─── Utilisateurs simulés ──────────────────────────────────────────────────────

class AdminUser(HttpUser):
    """Simule un administrateur qui surveille et gère la plateforme."""
    wait_time = between(1, 3)
    weight = 5   # 5% du trafic

    def on_start(self):
        r = self.client.post("/api/auth/login", json={
            "email": "serge@rtn.sn", "password": "passer"
        })
        if r.status_code == 200:
            token = r.json()["access_token"]
            self.headers = {"Authorization": f"Bearer {token}"}
        else:
            self.headers = {}
            self.stop(force=True)

    @task(3)
    def dashboard(self):
        self.client.get("/api/admin/dashboard", headers=self.headers,
                        name="/api/admin/dashboard")

    @task(2)
    def liste_utilisateurs(self):
        self.client.get("/api/admin/users", headers=self.headers,
                        name="/api/admin/users")

    @task(2)
    def security_report(self):
        self.client.get("/api/admin/security_report", headers=self.headers,
                        name="/api/admin/security_report")

    @task(2)
    def exams_actifs(self):
        self.client.get("/api/online_exams", headers=self.headers,
                        name="/api/online_exams")

    @task(1)
    def bilan_examen(self):
        self.client.get(f"/api/online_exams/{EXAM_ID}/bilan", headers=self.headers,
                        name="/api/online_exams/{id}/bilan")

    @task(1)
    def historique_exams(self):
        self.client.get("/api/admin/exams_history", headers=self.headers,
                        name="/api/admin/exams_history")


class ProfesseurUser(HttpUser):
    """Simule un professeur qui consulte ses copies et examens."""
    wait_time = between(2, 5)
    weight = 10  # 10% du trafic

    def on_start(self):
        r = self.client.post("/api/auth/login", json={
            "email": "serge@rtn.sn", "password": "passer"
        })
        if r.status_code == 200:
            token = r.json()["access_token"]
            self.headers = {"Authorization": f"Bearer {token}"}
        else:
            self.headers = {}
            self.stop(force=True)

    @task(3)
    def dashboard_prof(self):
        self.client.get("/api/professor/dashboard", headers=self.headers,
                        name="/api/professor/dashboard")

    @task(2)
    def mes_etudiants(self):
        self.client.get("/api/professor/my_students", headers=self.headers,
                        name="/api/professor/my_students")

    @task(2)
    def liste_examens(self):
        self.client.get("/api/online_exams", headers=self.headers,
                        name="/api/online_exams")

    @task(2)
    def stats_examen(self):
        self.client.get(f"/api/online_exams/{EXAM_ID}/stats", headers=self.headers,
                        name="/api/online_exams/{id}/stats")

    @task(2)
    def tentatives_examen(self):
        self.client.get(f"/api/online_exams/{EXAM_ID}/attempts", headers=self.headers,
                        name="/api/online_exams/{id}/attempts")

    @task(1)
    def incidents(self):
        self.client.get(f"/api/online_exams/{EXAM_ID}/incidents", headers=self.headers,
                        name="/api/online_exams/{id}/incidents")

    @task(1)
    def analytics(self):
        self.client.get("/api/professor/analytics", headers=self.headers,
                        name="/api/professor/analytics")

    @task(1)
    def plagiarism_check(self):
        self.client.get(f"/api/online_exams/{EXAM_ID}/plagiarism-check",
                        headers=self.headers, name="/api/online_exams/{id}/plagiarism-check")


class SurveillantUser(HttpUser):
    """Simule un surveillant en train de monitorer un examen en direct."""
    wait_time = between(3, 8)   # polling toutes les 3-8s
    weight = 15  # 15% du trafic

    def on_start(self):
        r = self.client.post("/api/auth/login", json={
            "email": "aristoud@gmail.com", "password": "passer"
        })
        if r.status_code == 200:
            token = r.json()["access_token"]
            self.headers = {"Authorization": f"Bearer {token}"}
            # Récupérer une tentative à monitorer
            r2 = self.client.get(f"/api/online_exams/{EXAM_ID}/attempts",
                                 headers=self.headers)
            attempts = r2.json() if isinstance(r2.json(), list) else r2.json().get("attempts", [])
            self.attempt_ids = [a["id"] for a in attempts[:5]] if attempts else [1]
        else:
            self.headers = {}
            self.attempt_ids = [1]
            self.stop(force=True)

    @task(5)
    def monitoring_actif(self):
        """Polling principal du dashboard surveillance."""
        self.client.get(f"/api/online_exams/{EXAM_ID}/active_proctoring",
                        headers=self.headers, name="/api/online_exams/{id}/active_proctoring")

    @task(4)
    def risk_status_etudiant(self):
        """Consulter le risque d'un étudiant aléatoire."""
        attempt_id = random.choice(self.attempt_ids)
        self.client.get(f"/api/exam_attempts/{attempt_id}/risk_status",
                        headers=self.headers, name="/api/exam_attempts/{id}/risk_status")

    @task(3)
    def messages_etudiants(self):
        self.client.get(f"/api/online_exams/{EXAM_ID}/student_messages",
                        headers=self.headers, name="/api/online_exams/{id}/student_messages")

    @task(2)
    def mes_examens(self):
        self.client.get("/api/surveillant/exams", headers=self.headers,
                        name="/api/surveillant/exams")

    @task(1)
    def pending_messages(self):
        attempt_id = random.choice(self.attempt_ids)
        self.client.get(f"/api/exam_attempts/{attempt_id}/pending_messages",
                        headers=self.headers, name="/api/exam_attempts/{id}/pending_messages")


class EtudiantUser(HttpUser):
    """Simule un étudiant pendant et après un examen."""
    wait_time = between(5, 15)  # les étudiants réfléchissent !
    weight = 70  # 70% du trafic — majorité des utilisateurs

    def on_start(self):
        r = self.client.post("/api/auth/login", json={
            "email": "laprincesseawa99@gmail.com", "password": "passer"
        })
        if r.status_code == 200:
            token = r.json()["access_token"]
            self.headers = {"Authorization": f"Bearer {token}"}
        else:
            self.headers = {}
            self.stop(force=True)

    @task(5)
    def voir_mes_resultats(self):
        self.client.get("/api/student/online_results", headers=self.headers,
                        name="/api/student/online_results")

    @task(4)
    def mes_copies(self):
        self.client.get("/api/student/papers", headers=self.headers,
                        name="/api/student/papers")

    @task(3)
    def historique_examens(self):
        self.client.get("/api/student/exam-history", headers=self.headers,
                        name="/api/student/exam-history")

    @task(3)
    def mes_transcripts(self):
        self.client.get("/api/student/transcripts", headers=self.headers,
                        name="/api/student/transcripts")

    @task(2)
    def liste_examens_disponibles(self):
        self.client.get("/api/online_exams", headers=self.headers,
                        name="/api/online_exams")

    @task(2)
    def mes_notifications(self):
        self.client.get("/api/notifications", headers=self.headers,
                        name="/api/notifications")

    @task(1)
    def profil(self):
        self.client.get("/api/auth/me", headers=self.headers,
                        name="/api/auth/me")


class ProctoringHeavyUser(HttpUser):
    """
    Simule un étudiant EN TRAIN de passer un examen avec proctoring actif.
    Envoie des snapshots caméra + events toutes les quelques secondes.
    C'est le scénario le plus lourd pour le serveur.
    """
    wait_time = between(8, 12)  # snapshot toutes les 8-12s (face_detector.js)
    weight = 30  # 30% — étudiants en examen simultané

    # Image base64 minimale (1x1 PNG)
    MINI_IMAGE = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
                  "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")

    def on_start(self):
        r = self.client.post("/api/auth/login", json={
            "email": "laprincesseawa99@gmail.com", "password": "passer"
        })
        if r.status_code == 200:
            token = r.json()["access_token"]
            self.headers = {"Authorization": f"Bearer {token}"}
            # Récupérer une tentative existante pour simuler le proctoring
            r2 = self.client.get(f"/api/online_exams/{EXAM_ID}/attempts",
                                 headers=self.headers)
            items = r2.json() if isinstance(r2.json(), list) else r2.json().get("attempts", [])
            submitted = [a["id"] for a in items if a.get("status") == "submitted"]
            self.attempt_id = submitted[0] if submitted else (items[0]["id"] if items else 1)
        else:
            self.headers = {}
            self.attempt_id = 1
            self.stop(force=True)

    @task(5)
    def camera_snapshot(self):
        """Snapshot caméra — action la plus fréquente du proctoring."""
        face_detected = random.random() > 0.1  # 90% du temps le visage est détecté
        self.client.post(f"/api/exam_attempts/{self.attempt_id}/camera_snapshot",
            json={
                "image_data": self.MINI_IMAGE,
                "face_detected": face_detected,
                "face_count": 1 if face_detected else 0,
                "confidence": random.uniform(0.85, 0.99) if face_detected else 0.0
            },
            headers=self.headers,
            name="/api/exam_attempts/{id}/camera_snapshot"
        )

    @task(3)
    def log_activite(self):
        """Logger l'activité de la page (tab switches, etc.)."""
        events_possibles = ["focus", "blur", "fullscreen_enter"]
        self.client.post(f"/api/exam_attempts/{self.attempt_id}/log_activity",
            json={"event_type": random.choice(events_possibles), "details": ""},
            headers=self.headers,
            name="/api/exam_attempts/{id}/log_activity"
        )

    @task(2)
    def sauvegarder_reponses(self):
        """Sauvegarde automatique des réponses."""
        self.client.post(f"/api/exam_attempts/{self.attempt_id}/save",
            json={"answers": {"q1": "Réponse test", "q2": "Option A"}},
            headers=self.headers,
            name="/api/exam_attempts/{id}/save"
        )

    @task(1)
    def pending_messages(self):
        """Polling messages du surveillant."""
        self.client.get(f"/api/exam_attempts/{self.attempt_id}/pending_messages",
            headers=self.headers,
            name="/api/exam_attempts/{id}/pending_messages"
        )
