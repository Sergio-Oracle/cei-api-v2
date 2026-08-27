"""Configuration du service Agent Proctor CEI."""
import os
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), '..', '.env'), override=True)

# CEI Platform — chaque serveur qui héberge une instance CEI doit surveiller
# SA PROPRE instance, jamais celle d'un autre serveur (pas de surveillance
# croisée). AGENT_APP_URL est une clé dédiée à l'agent, distincte de APP_URL
# (utilisée ailleurs par l'app elle-même pour générer des liens publics dans
# les emails — ne doit jamais pointer vers un localhost/URL interne). Si
# AGENT_APP_URL est absent, l'agent retombe sur APP_URL (comportement
# historique, correct quand APP_URL EST déjà l'URL publique de CETTE instance).
CEI_BASE_URL   = os.getenv("AGENT_APP_URL") or os.getenv("APP_URL", "http://127.0.0.1:5000")
AGENT_SECRET   = os.getenv("AGENT_SECRET_KEY", "changeme-agent-secret-key")

# SMTP — mêmes credentials que la plateforme
SMTP_SERVER    = os.getenv("SMTP_SERVER",   "smx7.unchk.sn")
SMTP_PORT      = int(os.getenv("SMTP_PORT", "587"))
SMTP_USERNAME  = os.getenv("SMTP_USERNAME", "jokkomeet")
SMTP_PASSWORD  = os.getenv("SMTP_PASSWORD", "")
FROM_EMAIL     = os.getenv("SMTP_FROM_EMAIL", "noreply.jokkomeet@unchk.sn")
FROM_NAME      = "CEI — Agent de Surveillance"

# IA — Ollama local (même config que la plateforme)
OLLAMA_URL     = os.getenv("OLLAMA_API_URL", "").rstrip("/")
OLLAMA_KEY     = os.getenv("OLLAMA_API_KEY", "")
OLLAMA_MODEL   = os.getenv("OLLAMA_MODEL", "qwen3.6:latest")

# Seuils d'alerte — volontairement distincts des seuils d'affichage du badge
# UI surveillant (40/70, riskCls dans proctor/monitor/[id]/page.tsx) :
# l'email part un peu avant que le badge ne passe au rouge (70), pour donner
# une longueur d'avance au staff. Pas une incohérence à corriger.
RISK_ALERT       = int(os.getenv("AGENT_RISK_ALERT",       "60"))   # alerte email
RISK_URGENT       = int(os.getenv("AGENT_RISK_URGENT",      "80"))   # alerte urgente
CHECK_INTERVAL    = int(os.getenv("AGENT_CHECK_INTERVAL",   "30"))   # secondes entre analyses
ALERT_COOLDOWN    = int(os.getenv("AGENT_ALERT_COOLDOWN",   "600"))  # 10 min entre 2 alertes/étudiant
SUMMARY_INTERVAL  = int(os.getenv("AGENT_SUMMARY_INTERVAL", "3600")) # 1h entre 2 rapports enseignant (évite de saturer la boîte mail)
