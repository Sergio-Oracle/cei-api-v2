#!/usr/bin/env python3
"""
Test de bout en bout de l'API externe (/api/external/<rôle>/*) — remplace le
test manuel via Swagger UI, qui échoue de façon récurrente à cause de jetons
copiés-collés périmés (1h de validité) ou du mauvais rôle. Ce script se
connecte lui-même à chaque exécution : jamais de jeton à copier-coller.

Utilise 4 comptes QA dédiés, créés spécifiquement pour ce test (isolés des
comptes réels UNCHK) :
  qa-professor@cei-test.local / qa-student@cei-test.local /
  qa-surveillant@cei-test.local / qa-superviseur@cei-test.local
  mot de passe : CeiQaTest2026!

Usage :
  python3 scripts/test_external_api_e2e.py --base-url https://preprod-cei.unchk.sn --api-key cei_...
  python3 scripts/test_external_api_e2e.py --base-url https://preprod-cei.unchk.sn --api-key cei_... --seed

Sans --seed : teste les 18 routes de lecture qui ne nécessitent aucune donnée
préalable (fonctionne en pur HTTPS, comme un vrai dev ENT — aucun accès
serveur requis), plus les cas d'erreur (mauvais rôle, mauvaise clé, sans
jeton).

Avec --seed (à exécuter SUR le serveur, ou avec un accès direct à la base) :
crée des données de test temporaires (sujet, examen, copie, réclamation,
relevé), teste les 31 routes (y compris détail/écriture), puis nettoie tout.
"""
import argparse
import json
import sys
import urllib.request
import urllib.error

QA_PASSWORD = "CeiQaTest2026!"
QA_ACCOUNTS = {
    "professor": "qa-professor@cei-test.local",
    "student": "qa-student@cei-test.local",
    "surveillant": "qa-surveillant@cei-test.local",
    "superviseur": "qa-superviseur@cei-test.local",
}

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"


class Client:
    def __init__(self, base_url, api_key):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key

    def _request(self, method, path, token=None, body=None, api_key=True):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if api_key:
            headers["X-CEI-API-Key"] = self.api_key
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base_url + path, method=method, data=data, headers=headers)
        try:
            r = urllib.request.urlopen(req, timeout=15)
            return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read())
            except Exception:
                return e.code, {}

    def login(self, email, password):
        status, body = self._request("POST", "/api/auth/login", body={"email": email, "password": password, "force": True}, api_key=False)
        if status != 200:
            raise RuntimeError(f"Login échoué pour {email} : {status} {body}")
        return body["access_token"]

    def call(self, method, path, token, body=None, api_key=True):
        return self._request(method, path, token=token, body=body, api_key=api_key)


def check(results, label, condition, detail=""):
    status = PASS if condition else FAIL
    results.append((condition, label))
    print(f"  [{status}] {label}" + (f" — {detail}" if detail and not condition else ""))


def run_readonly_suite(client, results):
    print("\n== Connexion des 4 comptes QA (jeton frais, jamais périmé) ==")
    tokens = {}
    for role, email in QA_ACCOUNTS.items():
        tokens[role] = client.login(email, QA_PASSWORD)
        print(f"  {role:12} -> jeton obtenu ({email})")

    print("\n== Professeur (9 routes sans paramètre) ==")
    for path in ["/api/external/professor/exams", "/api/external/professor/corrections",
                 "/api/external/professor/subjects", "/api/external/professor/questions",
                 "/api/external/professor/transcripts", "/api/external/professor/reclamations",
                 "/api/external/professor/students", "/api/external/professor/ecs",
                 "/api/external/professor/analytics"]:
        status, _ = client.call("GET", path, tokens["professor"])
        check(results, f"GET {path}", status == 200, f"status={status}")

    print("\n== Étudiant (5 routes sans paramètre) ==")
    for path in ["/api/external/student/exams", "/api/external/student/results",
                 "/api/external/student/papers", "/api/external/student/transcripts",
                 "/api/external/student/reclamations"]:
        status, _ = client.call("GET", path, tokens["student"])
        check(results, f"GET {path}", status == 200, f"status={status}")

    print("\n== Surveillant (1 route sans paramètre) ==")
    status, _ = client.call("GET", "/api/external/surveillant/assignments", tokens["surveillant"])
    check(results, "GET /api/external/surveillant/assignments", status == 200, f"status={status}")

    print("\n== Superviseur (3 routes sans paramètre) ==")
    for path in ["/api/external/superviseur/groups", "/api/external/superviseur/dashboard",
                 "/api/external/superviseur/call-requests"]:
        status, _ = client.call("GET", path, tokens["superviseur"])
        check(results, f"GET {path}", status == 200, f"status={status}")

    print("\n== Cas d'erreur (protection croisée) ==")
    status, body = client.call("GET", "/api/external/professor/exams", tokens["student"])
    check(results, "Jeton étudiant sur route professeur -> 403", status == 403, f"status={status} body={body}")
    status, _ = client.call("GET", "/api/external/student/exams", None)
    check(results, "Sans jeton -> 401", status == 401, f"status={status}")
    status, _ = client._request("GET", "/api/external/student/exams", token=tokens["student"], api_key=False)
    check(results, "Sans clé API -> 401", status == 401, f"status={status}")

    return tokens


def run_seeded_suite(client, tokens, results):
    """Nécessite un accès direct à la base (exécuter sur le serveur) pour
    créer/nettoyer les données de test des routes paramétrées/écriture."""
    import os
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import app  # noqa: F401  (initialise la config .env comme le reste de l'app)
    from models import (
        get_session, Subject, OnlineExam, ExamStatus, StudentPaper, Reclamation,
        User, UserRole, ExamProctor, ProctorAssignment, GradeTranscript, Semester,
    )

    session = get_session()
    try:
        prof_id = session.query(User).filter_by(email=QA_ACCOUNTS["professor"]).first().id
        student_id = session.query(User).filter_by(email=QA_ACCOUNTS["student"]).first().id
        surveillant_id = session.query(User).filter_by(email=QA_ACCOUNTS["surveillant"]).first().id
        semester_id = session.query(Semester.id).first()
        semester_id = semester_id[0] if semester_id else None

        subj = Subject(title="QA e2e sujet", content="c", rubric="r", filename="", creator_id=prof_id, is_active=True)
        session.add(subj)
        session.commit()

        paper = StudentPaper(student_id=student_id, subject_id=subj.id, content="c", corrected_by_id=prof_id,
                              score=15.0, grade="Bien", is_published=False)
        session.add(paper)
        session.commit()
        paper_id = paper.id
        subject_id = subj.id

        transcript_id = None
        if semester_id:
            t = GradeTranscript(student_id=student_id, semester_id=semester_id, generated_by_id=prof_id,
                                 gpa=13.0, total_credits=30, obtained_credits=30, is_published=False)
            session.add(t)
            session.commit()
            transcript_id = t.id
    finally:
        session.close()

    print("\n== Professeur — routes paramétrées / écriture ==")
    status, body = client.call("POST", "/api/external/professor/exams", tokens["professor"], {
        "subject_id": subject_id, "title": "QA e2e examen",
        "start_time": "2027-01-01T09:00:00Z", "end_time": "2027-01-01T11:00:00Z",
    })
    check(results, "POST /api/external/professor/exams", status == 201, f"status={status} body={body}")
    exam_id = body.get("exam", {}).get("id") if status == 201 else None

    if exam_id:
        status, _ = client.call("GET", f"/api/external/professor/exams/{exam_id}", tokens["professor"])
        check(results, "GET /api/external/professor/exams/{id}", status == 200, f"status={status}")
        status, _ = client.call("PUT", f"/api/external/professor/exams/{exam_id}", tokens["professor"], {"title": "QA e2e renommé"})
        check(results, "PUT /api/external/professor/exams/{id}", status == 200, f"status={status}")
        status, _ = client.call("GET", f"/api/external/professor/exams/{exam_id}/attempts", tokens["professor"])
        check(results, "GET /api/external/professor/exams/{id}/attempts", status == 200, f"status={status}")
        status, _ = client.call("GET", f"/api/external/professor/exams/{exam_id}/stats", tokens["professor"])
        check(results, "GET /api/external/professor/exams/{id}/stats", status == 200, f"status={status}")
        status, _ = client.call("PUT", f"/api/external/professor/exams/{exam_id}/publish-results", tokens["professor"], {"published": True})
        check(results, "PUT /api/external/professor/exams/{id}/publish-results", status == 200, f"status={status}")

    status, _ = client.call("GET", f"/api/external/professor/subjects/{subject_id}", tokens["professor"])
    check(results, "GET /api/external/professor/subjects/{id}", status == 200, f"status={status}")

    if transcript_id:
        status, _ = client.call("PUT", f"/api/external/professor/transcripts/{transcript_id}/publish", tokens["professor"], {"is_published": True})
        check(results, "PUT /api/external/professor/transcripts/{id}/publish", status == 200, f"status={status}")
    else:
        print("  [SKIP] PUT /api/external/professor/transcripts/{id}/publish — aucun semestre en base pour créer le relevé de test")

    print("\n== Étudiant — détail d'examen ==")
    if exam_id:
        status, _ = client.call("GET", f"/api/external/student/exams/{exam_id}", tokens["student"])
        check(results, "GET /api/external/student/exams/{id}", status == 200, f"status={status}")

    print("\n== Surveillant — statut / incidents (nécessite une affectation) ==")
    if exam_id:
        session = get_session()
        try:
            session.add(ExamProctor(exam_id=exam_id, proctor_id=surveillant_id, assigned_by_id=prof_id))
            session.add(ProctorAssignment(exam_id=exam_id, proctor_id=surveillant_id, student_id=student_id))
            session.commit()
        finally:
            session.close()

        status, _ = client.call("GET", f"/api/external/surveillant/exams/{exam_id}/status", tokens["surveillant"])
        check(results, "GET /api/external/surveillant/exams/{id}/status", status == 200, f"status={status}")
        status, _ = client.call("GET", f"/api/external/surveillant/exams/{exam_id}/incidents", tokens["surveillant"])
        check(results, "GET /api/external/surveillant/exams/{id}/incidents", status == 200, f"status={status}")

        session = get_session()
        try:
            session.query(ProctorAssignment).filter_by(exam_id=exam_id).delete(synchronize_session=False)
            session.query(ExamProctor).filter_by(exam_id=exam_id).delete(synchronize_session=False)
            session.commit()
        finally:
            session.close()

    print("\n== Étudiant — réclamation ==")
    status, body = client.call("POST", "/api/external/student/reclamations", tokens["student"], {
        "reason": "test e2e", "paper_id": paper_id,
    })
    check(results, "POST /api/external/student/reclamations", status == 201, f"status={status} body={body}")
    rec_id = body.get("reclamation", {}).get("id") if status == 201 else None

    if rec_id:
        status, _ = client.call("PUT", f"/api/external/professor/reclamations/{rec_id}/respond", tokens["professor"], {"status": "approved", "response": "ok"})
        check(results, "PUT /api/external/professor/reclamations/{id}/respond", status == 200, f"status={status}")

    # Nettoyage
    session = get_session()
    try:
        if rec_id:
            session.query(Reclamation).filter_by(id=rec_id).delete(synchronize_session=False)
        if transcript_id:
            session.query(GradeTranscript).filter_by(id=transcript_id).delete(synchronize_session=False)
        session.query(StudentPaper).filter_by(id=paper_id).delete(synchronize_session=False)
        if exam_id:
            session.query(ProctorAssignment).filter_by(exam_id=exam_id).delete(synchronize_session=False)
            session.query(ExamProctor).filter_by(exam_id=exam_id).delete(synchronize_session=False)
            session.query(OnlineExam).filter_by(id=exam_id).delete(synchronize_session=False)
        session.query(Subject).filter_by(id=subject_id).delete(synchronize_session=False)
        session.commit()
    finally:
        session.close()
    print("\n  (données de test nettoyées)")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--api-key", required=True)
    parser.add_argument("--seed", action="store_true", help="teste aussi les routes paramétrées/écriture (nécessite un accès DB, exécuter sur le serveur)")
    args = parser.parse_args()

    client = Client(args.base_url, args.api_key)
    results = []
    tokens = run_readonly_suite(client, results)

    if args.seed:
        run_seeded_suite(client, tokens, results)

    total = len(results)
    passed = sum(1 for ok, _ in results if ok)
    print(f"\n{'='*50}\n{passed}/{total} vérifications réussies\n{'='*50}")
    if passed != total:
        print("\nÉchecs :")
        for ok, label in results:
            if not ok:
                print(f"  - {label}")
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
