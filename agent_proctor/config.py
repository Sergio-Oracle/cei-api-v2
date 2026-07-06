"""Configuration du service Agent Proctor CEI."""
import os
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), '..', '.env'), override=True)

# CEI Platform
CEI_BASE_URL   = os.getenv("APP_URL", "http://127.0.0.1:5000")
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

# Seuils d'alerte
RISK_ALERT     = int(os.getenv("AGENT_RISK_ALERT",   "60"))  # alerte email
RISK_URGENT    = int(os.getenv("AGENT_RISK_URGENT",  "80"))  # alerte urgente
CHECK_INTERVAL = int(os.getenv("AGENT_CHECK_INTERVAL","30"))  # secondes entre analyses
ALERT_COOLDOWN = int(os.getenv("AGENT_ALERT_COOLDOWN","600")) # 10 min entre 2 alertes/étudiant
