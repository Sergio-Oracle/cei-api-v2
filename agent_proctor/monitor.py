"""
Agent Proctor CEI — Service de surveillance autonome
Surveille tous les examens actifs, analyse les comportements suspects,
envoie des emails et des alertes dashboard aux surveillants et enseignants.
"""
import os
import sys
import time
import json
import requests
import threading
from datetime import datetime, timezone

# Chemin vers la racine du projet pour charger .env
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agent_proctor.config import (
    CEI_BASE_URL, AGENT_SECRET,
    OLLAMA_URL, OLLAMA_KEY, OLLAMA_MODEL,
    RISK_ALERT, RISK_URGENT,
    CHECK_INTERVAL, ALERT_COOLDOWN,
)
from agent_proctor.email_alerts import send_alert_email, send_summary_email

# ── État interne ──────────────────────────────────────────────────────────────
# cooldown par étudiant : attempt_id → timestamp dernière alerte
_alert_cooldown: dict[int, float] = {}

# stats par examen pour le rapport périodique : exam_id → dict
_exam_stats: dict[int, dict] = {}

# lock pour la sécurité thread
_lock = threading.Lock()

# Fichier heartbeat — lu par l'API pour connaître le statut de l'agent
_HEARTBEAT_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'agent_heartbeat.json')


def _write_heartbeat(exams_monitored: int, total_alerts_session: int):
    """Écrit un fichier heartbeat toutes les 30s pour que l'API sache que l'agent est actif."""
    try:
        import json as _j
        data = {
            "alive":               True,
            "last_check":          datetime.now(timezone.utc).isoformat(),
            "interval_seconds":    CHECK_INTERVAL,
            "risk_alert":          RISK_ALERT,
            "risk_urgent":         RISK_URGENT,
            "exams_monitored":     exams_monitored,
            "total_alerts_session":total_alerts_session,
            "exam_stats":          {},
        }
        with _lock:
            for exam_id, stats in _exam_stats.items():
                data["exam_stats"][str(exam_id)] = {
                    "title":       stats.get("title", ""),
                    "total":       stats.get("total", 0),
                    "alerts_sent": stats.get("alerts", 0),
                    "banned":      stats.get("banned", 0),
                }
        with open(_HEARTBEAT_FILE, 'w') as f:
            _j.dump(data, f)
    except Exception as e:
        print(f"⚠️  Heartbeat write error: {e}")


# ── Appel API CEI ─────────────────────────────────────────────────────────────

def _cei_headers() -> dict:
    return {
        "X-Agent-Secret": AGENT_SECRET,
        "Content-Type":   "application/json",
    }


def _get_active_exams() -> list[dict]:
    try:
        r = requests.get(
            f"{CEI_BASE_URL}/api/agent/active_exams",
            headers=_cei_headers(), timeout=10
        )
        if r.ok:
            return r.json().get("exams", [])
    except Exception as e:
        print(f"⚠️  Impossible de récupérer les examens actifs : {e}")
    return []


def _get_exam_proctoring(exam_id: int) -> dict:
    try:
        r = requests.get(
            f"{CEI_BASE_URL}/api/agent/exam_proctoring/{exam_id}",
            headers=_cei_headers(), timeout=10
        )
        if r.ok:
            return r.json()
    except Exception as e:
        print(f"⚠️  Proctoring examen {exam_id} : {e}")
    return {}


def _push_alert(alert: dict):
    """Envoie l'alerte au dashboard via l'API CEI."""
    try:
        requests.post(
            f"{CEI_BASE_URL}/api/agent/alerts",
            headers=_cei_headers(),
            json=alert,
            timeout=5
        )
    except Exception as e:
        print(f"⚠️  Push alerte dashboard : {e}")


# ── Analyse IA ────────────────────────────────────────────────────────────────

def _ai_analyze(student_name: str, risk_score: int, no_face: int,
                multi_face: int, tab_switches: int, warnings: int) -> str:
    """Analyse comportementale par Ollama. Retourne une évaluation courte."""
    if not OLLAMA_URL or not OLLAMA_KEY:
        # Analyse règle-basée si Ollama indisponible
        if risk_score >= RISK_URGENT:
            return "Comportement hautement suspect — intervention immédiate recommandée."
        return "Anomalies répétées détectées — surveillance renforcée conseillée."

    prompt = (
        f"Tu es un agent de surveillance d'examen universitaire. "
        f"Évalue brièvement en 1-2 phrases le comportement de cet étudiant :\n"
        f"- Score de risque : {risk_score}/100\n"
        f"- Visage absent : {no_face} fois\n"
        f"- Plusieurs visages : {multi_face} fois\n"
        f"- Changements d'onglet : {tab_switches} fois\n"
        f"- Avertissements reçus : {warnings}\n"
        f"Sois concis, factuel, sans drama. Indique si une intervention humaine est nécessaire."
    )
    try:
        import re as _re
        resp = requests.post(
            f"{OLLAMA_URL}/api/chat",
            headers={"Authorization": f"Bearer {OLLAMA_KEY}", "Content-Type": "application/json"},
            json={
                "model":   OLLAMA_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "stream":  False,
                "think":   False,
                "options": {"temperature": 0.1, "num_predict": 100},
            },
            timeout=30
        )
        if resp.ok:
            content = resp.json()["message"]["content"]
            content = _re.sub(r'<think>.*?</think>', '', content, flags=_re.DOTALL).strip()
            return content[:300]
    except Exception as e:
        print(f"⚠️  Ollama analyse : {e}")

    # Fallback règle-basée si Ollama échoue
    if risk_score >= RISK_URGENT:
        return "Comportement hautement suspect — intervention immédiate recommandée."
    return "Anomalies répétées détectées — surveillance renforcée conseillée."


# ── Cycle de surveillance ─────────────────────────────────────────────────────

def _process_exam(exam: dict):
    exam_id    = exam["id"]
    exam_title = exam.get("title", f"Examen #{exam_id}")
    data       = _get_exam_proctoring(exam_id)
    if not data:
        return

    attempts       = data.get("attempts", [])
    proctors_emails= data.get("proctor_emails", [])
    teacher_email  = data.get("teacher_email")
    now            = time.time()

    # Initialiser les stats de cet examen
    with _lock:
        if exam_id not in _exam_stats:
            _exam_stats[exam_id] = {
                "title":   exam_title,
                "teacher": teacher_email,
                "total":   0,
                "alerts":  0,
                "banned":  0,
            }
        _exam_stats[exam_id]["total"]  = len(attempts)
        _exam_stats[exam_id]["banned"] = sum(1 for a in attempts if a.get("status") == "banned")

    # Filtrer les étudiants à risque non encore en cooldown
    to_alert = []
    for a in attempts:
        risk    = a.get("risk_score", 0)
        att_id  = a.get("id")
        status  = a.get("status", "")

        if risk < RISK_ALERT or status in ("banned", "graded", "submitted", "auto_submitted"):
            continue

        last_alert = _alert_cooldown.get(att_id, 0)
        if now - last_alert < ALERT_COOLDOWN:
            continue  # encore en cooldown

        level = "URGENT" if risk >= RISK_URGENT else "ALERTE"
        no_face    = a.get("no_face_detected_count", 0)
        multi_face = a.get("multiple_faces_count", 0)
        tab_sw     = a.get("tab_switches", 0)
        warnings   = a.get("warnings_count", 0)

        ai_note = _ai_analyze(
            a.get("student_name", "?"), risk,
            no_face, multi_face, tab_sw, warnings
        )

        alert_obj = {
            "exam_id":      exam_id,
            "exam_title":   exam_title,
            "attempt_id":   att_id,
            "student_name": a.get("student_name", "?"),
            "risk_score":   risk,
            "level":        level,
            "no_face":      no_face,
            "multi_face":   multi_face,
            "tab_switches": tab_sw,
            "warnings_count": warnings,
            "ai_note":      ai_note,
            "timestamp":    datetime.now(timezone.utc).isoformat(),
        }
        to_alert.append(alert_obj)

        # Marquer le cooldown
        with _lock:
            _alert_cooldown[att_id] = now
            _exam_stats[exam_id]["alerts"] += 1

    if not to_alert:
        return

    print(f"🚨 {len(to_alert)} alerte(s) — {exam_title}")

    # Pousser vers le dashboard
    for alert_obj in to_alert:
        _push_alert(alert_obj)
        print(f"   → {alert_obj['level']} {alert_obj['student_name']} "
              f"(risque {alert_obj['risk_score']}/100)")

    # Envoyer email aux surveillants
    recipients = list(proctors_emails)
    if teacher_email:
        recipients.append(teacher_email)
    recipients = list(set(r for r in recipients if r and "@" in r))

    if recipients:
        send_alert_email(recipients, to_alert, exam_title)


def _send_periodic_summaries():
    """Envoie un rapport toutes les 15 minutes aux enseignants."""
    with _lock:
        snapshot = dict(_exam_stats)
    for exam_id, stats in snapshot.items():
        teacher = stats.get("teacher")
        if teacher and stats.get("alerts", 0) > 0:
            send_summary_email([teacher], stats["title"], stats)
            # Remettre les compteurs à zéro
            with _lock:
                if exam_id in _exam_stats:
                    _exam_stats[exam_id]["alerts"] = 0


# ── Boucle principale ─────────────────────────────────────────────────────────

def run():
    print("=" * 60)
    print("  CEI — Agent de Surveillance Autonome")
    print(f"  Plateforme : {CEI_BASE_URL}")
    print(f"  Seuil alerte : {RISK_ALERT}/100 | Urgence : {RISK_URGENT}/100")
    print(f"  Intervalle  : {CHECK_INTERVAL}s | Cooldown : {ALERT_COOLDOWN}s")
    print("=" * 60)

    summary_interval    = 900  # 15 minutes
    last_summary        = time.time()
    total_alerts_session = 0

    while True:
        try:
            exams = _get_active_exams()
            if exams:
                print(f"[{datetime.now().strftime('%H:%M:%S')}] "
                      f"Analyse de {len(exams)} examen(s) actif(s)…")
                for exam in exams:
                    _process_exam(exam)
                with _lock:
                    total_alerts_session = sum(s.get("alerts", 0) for s in _exam_stats.values())
            else:
                print(f"[{datetime.now().strftime('%H:%M:%S')}] "
                      "Aucun examen actif.")

            # Écrire le heartbeat après chaque cycle
            _write_heartbeat(len(exams) if exams else 0, total_alerts_session)

            # Rapport périodique
            if time.time() - last_summary >= summary_interval:
                _send_periodic_summaries()
                last_summary = time.time()

        except KeyboardInterrupt:
            print("\n⛔ Agent arrêté.")
            break
        except Exception as e:
            print(f"❌ Erreur cycle principal : {e}")

        time.sleep(CHECK_INTERVAL)
