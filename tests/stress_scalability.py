"""
TESTS DE STRESS ET SCALABILITÉ — CEI API v2
Tests de montée en charge progressive, détection de dégradation, proctoring concurrent.
Exécuter directement : python tests/stress_scalability.py
"""
import sys
import os
import threading
import time
import json
import statistics
import random
from datetime import datetime
from dataclasses import dataclass, field
from typing import List, Dict
import httpx
from colorama import Fore, Style, init

init(autoreset=True)

BASE_URL = os.getenv("TEST_BASE_URL", "https://dev-cei.ddns.net")
EXAM_ID  = 4
TIMEOUT  = 15

# ─── Structures de données ────────────────────────────────────────────────────

@dataclass
class RequestResult:
    endpoint:  str
    method:    str
    status:    int
    latency_ms: float
    error:     str = ""

@dataclass
class StressReport:
    test_name:   str
    users:       int
    duration_s:  float
    results:     List[RequestResult] = field(default_factory=list)

    @property
    def total(self): return len(self.results)
    @property
    def success(self): return sum(1 for r in self.results if 200 <= r.status < 300)
    # Erreurs serveur = 5xx + timeouts. Les 4xx sont des rejets métier normaux.
    @property
    def server_errors(self): return sum(1 for r in self.results if r.status >= 500 or r.error)
    @property
    def business_rejects(self): return sum(1 for r in self.results if 400 <= r.status < 500)
    @property
    def errors(self): return self.server_errors
    # Taux basé sur les erreurs serveur uniquement
    @property
    def success_rate(self):
        non_error = self.total - self.server_errors
        return (non_error / self.total * 100) if self.total else 0
    @property
    def latencies(self): return [r.latency_ms for r in self.results if not r.error]
    @property
    def avg_ms(self): return statistics.mean(self.latencies) if self.latencies else 0
    @property
    def p95_ms(self):
        if not self.latencies: return 0
        return sorted(self.latencies)[int(len(self.latencies) * 0.95)]
    @property
    def p99_ms(self):
        if not self.latencies: return 0
        return sorted(self.latencies)[int(len(self.latencies) * 0.99)]
    @property
    def rps(self): return self.total / self.duration_s if self.duration_s else 0


# ─── Client HTTP ──────────────────────────────────────────────────────────────

def get_token(email="serge@rtn.sn", password="passer"):
    with httpx.Client(base_url=BASE_URL, timeout=TIMEOUT) as c:
        r = c.post("/api/auth/login", json={"email": email, "password": password})
        if r.status_code == 200:
            return r.json()["access_token"]
    return None


def make_request(client, method, path, headers, json_data=None):
    t0 = time.monotonic()
    try:
        r = client.request(method, path, headers=headers, json=json_data)
        ms = (time.monotonic() - t0) * 1000
        return RequestResult(endpoint=path, method=method, status=r.status_code, latency_ms=ms)
    except Exception as e:
        ms = (time.monotonic() - t0) * 1000
        return RequestResult(endpoint=path, method=method, status=0, latency_ms=ms, error=str(e))


# ─── Scénarios de test ────────────────────────────────────────────────────────

def scenario_admin_monitoring(client, headers, results: list):
    """Scénario admin : dashboard + stats + incidents."""
    endpoints = [
        ("GET", "/api/admin/dashboard"),
        ("GET", "/api/online_exams"),
        ("GET", f"/api/online_exams/{EXAM_ID}/stats"),
        ("GET", f"/api/online_exams/{EXAM_ID}/bilan"),
        ("GET", f"/api/online_exams/{EXAM_ID}/incidents"),
    ]
    for method, path in endpoints:
        r = make_request(client, method, path, headers)
        results.append(r)
        time.sleep(random.uniform(0.1, 0.5))


def scenario_surveillant_actif(client, headers, attempt_ids: list, results: list):
    """Scénario surveillant : polling toutes les 5s."""
    endpoints = [
        ("GET", f"/api/online_exams/{EXAM_ID}/active_proctoring"),
        ("GET", f"/api/online_exams/{EXAM_ID}/student_messages"),
        ("GET", "/api/surveillant/exams"),
    ]
    # Ajouter risk_status pour chaque étudiant assigné
    for aid in attempt_ids[:3]:
        endpoints.append(("GET", f"/api/exam_attempts/{aid}/risk_status"))
        endpoints.append(("GET", f"/api/exam_attempts/{aid}/pending_messages"))

    for method, path in endpoints:
        r = make_request(client, method, path, headers)
        results.append(r)
        time.sleep(0.2)


def scenario_proctoring_camera(client, headers, attempt_id: int, results: list,
                                n_snapshots: int = 10):
    """Scénario proctoring : envoie N snapshots caméra consécutifs."""
    img = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
           "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")
    for i in range(n_snapshots):
        r = make_request(client, "POST",
            f"/api/exam_attempts/{attempt_id}/camera_snapshot",
            headers,
            json_data={
                "image_data": img,
                "face_detected": random.random() > 0.05,
                "face_count": 1,
                "confidence": round(random.uniform(0.85, 0.99), 2)
            }
        )
        results.append(r)
        time.sleep(10 / n_snapshots)  # Espace sur 10 secondes


def scenario_etudiant_lecture(client, headers, results: list):
    """Scénario étudiant après examen : lecture résultats."""
    endpoints = [
        ("GET", "/api/student/online_results"),
        ("GET", "/api/student/papers"),
        ("GET", "/api/student/exam-history"),
        ("GET", "/api/notifications"),
        ("GET", "/api/online_exams"),
    ]
    for method, path in endpoints:
        r = make_request(client, method, path, headers)
        results.append(r)
        time.sleep(random.uniform(0.5, 2))


# ─── Tests de montée en charge ────────────────────────────────────────────────

def run_concurrent_test(test_name: str, n_users: int, scenario_fn,
                         scenario_args: tuple = ()) -> StressReport:
    """Lancer n_users threads exécutant le même scénario simultanément."""
    results = []
    lock = threading.Lock()
    token = get_token()
    if not token:
        print(f"{Fore.RED}✗ Impossible d'obtenir un token pour le test '{test_name}'")
        return StressReport(test_name, n_users, 0)

    def worker():
        with httpx.Client(base_url=BASE_URL, timeout=TIMEOUT) as c:
            headers = {"Authorization": f"Bearer {token}"}
            local_results = []
            scenario_fn(c, headers, *scenario_args, local_results)
            with lock:
                results.extend(local_results)

    t0 = time.monotonic()
    threads = [threading.Thread(target=worker) for _ in range(n_users)]
    for t in threads: t.start()
    for t in threads: t.join()
    duration = time.monotonic() - t0

    return StressReport(test_name, n_users, duration, results)


def print_report(report: StressReport, sla_p95_ms: float = 2000, sla_success: float = 99.0):
    """Afficher un rapport coloré avec analyse SLA.
    SLA basé sur les erreurs serveur (5xx + timeouts) uniquement.
    Les 4xx sont des rejets métier normaux (rôle, exam fermé, etc.).
    """
    ok  = report.success_rate >= sla_success
    p95_ok = report.p95_ms <= sla_p95_ms

    status = f"{Fore.GREEN}✓ PASS" if (ok and p95_ok) else f"{Fore.RED}✗ FAIL"
    print(f"\n{status}  {Style.BRIGHT}{report.test_name}")
    print(f"  Utilisateurs : {report.users}")
    print(f"  Requêtes     : {report.total} en {report.duration_s:.1f}s → {report.rps:.1f} RPS")
    print(f"  2xx succès   : {report.success}/{report.total} ({report.success * 100 / report.total:.0f}%)")
    print(f"  4xx métier   : {report.business_rejects} (rôle/exam fermé — non fatal)")
    print(f"  Err. serveur : {report.server_errors}/{report.total} "
          f"({report.success_rate:.1f}% OK) "
          f"{'✓' if ok else f'[SLA≥{sla_success}%]'}")
    print(f"  Latence moy  : {report.avg_ms:.0f}ms")
    print(f"  P95          : {report.p95_ms:.0f}ms "
          f"{'✓' if p95_ok else f'[SLA≤{sla_p95_ms}ms]'}")
    print(f"  P99          : {report.p99_ms:.0f}ms")

    if report.server_errors > 0:
        err_sample = [r for r in report.results if r.error or r.status >= 500][:5]
        for e in err_sample:
            print(f"  {Fore.RED}  → {e.method} {e.endpoint} : HTTP {e.status} {e.error[:50]}")

    return ok and p95_ok


# ─── Suite de tests ───────────────────────────────────────────────────────────

def main():
    print(f"\n{Style.BRIGHT}{'═'*60}")
    print(f"  CEI API v2 — Tests Stress & Scalabilité")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | {BASE_URL}")
    print(f"{'═'*60}{Style.RESET_ALL}")

    # Récupérer les IDs de tentatives une fois
    token = get_token()
    if not token:
        print(f"{Fore.RED}✗ Impossible de se connecter. Vérifiez le serveur.")
        sys.exit(1)

    with httpx.Client(base_url=BASE_URL, timeout=TIMEOUT) as c:
        headers = {"Authorization": f"Bearer {token}"}
        r = c.get(f"/api/online_exams/{EXAM_ID}/attempts", headers=headers)
        attempts = r.json() if isinstance(r.json(), list) else r.json().get("attempts", [])
        attempt_ids = [a["id"] for a in attempts[:10]]
        attempt_id  = attempt_ids[0] if attempt_ids else 1

    all_pass = True
    reports = []

    # ──────────────────────────────────────────────────────────────────────────
    print(f"\n{Style.BRIGHT}[ PHASE 1 — Charge légère : 10 utilisateurs ]")

    r1 = run_concurrent_test(
        "10 admins — Monitoring dashboard simultané",
        n_users=10, scenario_fn=scenario_admin_monitoring
    )
    reports.append(r1)
    ok = print_report(r1, sla_p95_ms=1500, sla_success=99)
    all_pass = all_pass and ok

    r2 = run_concurrent_test(
        "10 surveillants — Polling monitoring simultané",
        n_users=10, scenario_fn=scenario_surveillant_actif,
        scenario_args=(attempt_ids,)
    )
    reports.append(r2)
    ok = print_report(r2, sla_p95_ms=2000, sla_success=98)
    all_pass = all_pass and ok

    # ──────────────────────────────────────────────────────────────────────────
    print(f"\n{Style.BRIGHT}[ PHASE 2 — Charge moyenne : 50 utilisateurs ]")

    r3 = run_concurrent_test(
        "50 étudiants — Lecture résultats simultanée",
        n_users=50, scenario_fn=scenario_etudiant_lecture
    )
    reports.append(r3)
    ok = print_report(r3, sla_p95_ms=3000, sla_success=97)
    all_pass = all_pass and ok

    # ──────────────────────────────────────────────────────────────────────────
    print(f"\n{Style.BRIGHT}[ PHASE 3 — Stress Proctoring : snapshots caméra simultanés ]")

    def proctoring_wrapper(client, headers, results):
        scenario_proctoring_camera(client, headers, attempt_id, results, n_snapshots=5)

    for n_cameras in [10, 25, 50, 100]:
        r = run_concurrent_test(
            f"{n_cameras} caméras — Snapshots simultanés",
            n_users=n_cameras,
            scenario_fn=proctoring_wrapper
        )
        reports.append(r)
        ok = print_report(r, sla_p95_ms=3000, sla_success=95)
        all_pass = all_pass and ok
        time.sleep(2)  # Laisser le serveur respirer entre les paliers

    # ──────────────────────────────────────────────────────────────────────────
    print(f"\n{Style.BRIGHT}[ PHASE 4 — Scalabilité : montée en charge progressive ]")

    for n in [20, 50, 100, 200, 300]:
        def mixed_scenario(client, headers, results):
            choice = random.random()
            if choice < 0.6:
                scenario_etudiant_lecture(client, headers, results)
            elif choice < 0.85:
                scenario_surveillant_actif(client, headers, attempt_ids[:2], results)
            else:
                scenario_admin_monitoring(client, headers, results)

        r = run_concurrent_test(
            f"Montée en charge — {n} utilisateurs mixtes",
            n_users=n,
            scenario_fn=mixed_scenario
        )
        reports.append(r)
        sla_p95 = 2000 + (n // 100) * 1000  # SLA plus souple à grande échelle
        sla_succ = max(90, 99 - n // 100)
        ok = print_report(r, sla_p95_ms=sla_p95, sla_success=sla_succ)
        all_pass = all_pass and ok
        time.sleep(3)

    # ──────────────────────────────────────────────────────────────────────────
    print(f"\n{Style.BRIGHT}[ PHASE 5 — Soak test : 30 utilisateurs pendant 60 secondes ]")

    def soak_worker_results_collector():
        results = []
        token_local = get_token()
        if not token_local:
            return results
        with httpx.Client(base_url=BASE_URL, timeout=TIMEOUT) as c:
            headers = {"Authorization": f"Bearer {token_local}"}
            end_time = time.monotonic() + 60
            while time.monotonic() < end_time:
                r = make_request(c, "GET", "/api/online_exams", headers)
                results.append(r)
                r2 = make_request(c, "GET", f"/api/online_exams/{EXAM_ID}/stats", headers)
                results.append(r2)
                time.sleep(2)
        return results

    print("  Lancement du soak test (60s)... ", end="", flush=True)
    all_soak_results = []
    lock = threading.Lock()
    t0 = time.monotonic()

    def soak_thread():
        res = soak_worker_results_collector()
        with lock:
            all_soak_results.extend(res)

    threads = [threading.Thread(target=soak_thread) for _ in range(30)]
    for t in threads: t.start()
    for t in threads: t.join()
    soak_duration = time.monotonic() - t0
    print("done")

    soak_report = StressReport("Soak test — 30 users × 60s", 30, soak_duration, all_soak_results)
    ok = print_report(soak_report, sla_p95_ms=3000, sla_success=98)
    all_pass = all_pass and ok

    # ──────────────────────────────────────────────────────────────────────────
    print(f"\n{'═'*60}")
    total_reqs = sum(r.total for r in reports)
    total_errs = sum(r.errors for r in reports)
    all_latencies = []
    for r in reports: all_latencies.extend(r.latencies)

    if all_pass:
        print(f"{Fore.GREEN}{Style.BRIGHT}  ✓ TOUS LES TESTS PASSÉS")
    else:
        print(f"{Fore.RED}{Style.BRIGHT}  ✗ CERTAINS TESTS ÉCHOUÉS — voir détails ci-dessus")

    print(f"\n  Requêtes totales     : {total_reqs}")
    print(f"  Erreurs serveur (5xx): {total_errs}")
    if all_latencies:
        global_p95 = sorted(all_latencies)[int(len(all_latencies) * 0.95)]
        global_p99 = sorted(all_latencies)[int(len(all_latencies) * 0.99)]
        print(f"  Latence P95 gbl  : {global_p95:.0f}ms")
        print(f"  Latence P99 gbl  : {global_p99:.0f}ms")
    print(f"{'═'*60}\n")

    sys.exit(0 if all_pass else 1)


if __name__ == "__main__":
    main()
