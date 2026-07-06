"""Envoi des emails d'alerte de surveillance aux surveillants et enseignants."""
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime
from config import SMTP_SERVER, SMTP_PORT, SMTP_USERNAME, SMTP_PASSWORD, FROM_EMAIL, FROM_NAME, CEI_BASE_URL


def _send(to_emails: list[str], subject: str, html: str) -> bool:
    """Envoi SMTP mutualisé."""
    if not to_emails:
        return False
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = f"{FROM_NAME} <{FROM_EMAIL}>"
        msg["To"]      = ", ".join(to_emails)
        msg.attach(MIMEText(html, "html", "utf-8"))
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=15) as s:
            s.ehlo()
            s.starttls()
            s.login(SMTP_USERNAME, SMTP_PASSWORD)
            s.sendmail(FROM_EMAIL, to_emails, msg.as_string())
        print(f"✅ Email envoyé → {to_emails}")
        return True
    except Exception as e:
        print(f"❌ Erreur email : {e}")
        return False


def _footer() -> str:
    return """
    <p style="color:#94a3b8;font-size:11px;margin-top:24px;text-align:center;line-height:1.7;">
        © 2026 CEI — RTN – Réseaux et Techniques Numériques<br>
        Liberté 2, derrière immeuble BICIS, Jet d'eau – Dakar – Sénégal<br>
        (+221) 77 662 76 94 &nbsp;·&nbsp;
        <a href="mailto:entreprisertn221@gmail.com" style="color:#94a3b8;">entreprisertn221@gmail.com</a>
    </p>"""


def send_alert_email(to_emails: list[str], alerts: list[dict], exam_title: str) -> bool:
    """
    Email d'alerte envoyé aux surveillants et enseignants.
    alerts = liste de dicts avec : student_name, risk_score, no_face, multi_face,
                                    tab_switches, warnings_count, attempt_id, level
    """
    now = datetime.now().strftime("%d/%m/%Y à %H:%M")
    urgent_count  = sum(1 for a in alerts if a.get("level") == "URGENT")
    alert_count   = len(alerts)

    subject = (
        f"🔴 URGENT — {urgent_count} étudiant(s) suspect(s) | {exam_title}"
        if urgent_count
        else f"⚠️ ALERTE SURVEILLANCE — {alert_count} étudiant(s) | {exam_title}"
    )

    rows = ""
    for a in sorted(alerts, key=lambda x: -x.get("risk_score", 0)):
        level   = a.get("level", "ALERTE")
        color   = "#ef4444" if level == "URGENT" else "#f59e0b"
        badge   = f'<span style="background:{color};color:#fff;padding:2px 8px;border-radius:12px;font-size:12px;font-weight:700;">{level}</span>'
        rows += f"""
        <tr style="border-bottom:1px solid #e2e8f0;">
            <td style="padding:10px 8px;">{badge}</td>
            <td style="padding:10px 8px;font-weight:600;">{a.get('student_name','?')}</td>
            <td style="padding:10px 8px;text-align:center;">
                <span style="font-size:18px;font-weight:700;color:{color};">{a.get('risk_score',0)}</span>
                <span style="color:#94a3b8;">/100</span>
            </td>
            <td style="padding:10px 8px;color:#64748b;font-size:13px;">
                Sans visage : {a.get('no_face',0)}×&nbsp;&nbsp;
                Multi-visage : {a.get('multi_face',0)}×&nbsp;&nbsp;
                Tab switch : {a.get('tab_switches',0)}×&nbsp;&nbsp;
                Avert. : {a.get('warnings_count',0)}
            </td>
            <td style="padding:10px 8px;">
                <a href="{CEI_BASE_URL}/app" style="background:#2563eb;color:#fff;padding:5px 12px;border-radius:6px;text-decoration:none;font-size:12px;">
                    Intervenir →
                </a>
            </td>
        </tr>"""

    html = f"""
<!DOCTYPE html>
<html lang="fr">
<head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#f8fafc;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
<div style="max-width:700px;margin:32px auto;background:#fff;border-radius:12px;overflow:hidden;box-shadow:0 4px 24px rgba(0,0,0,.08);">

    <!-- En-tête -->
    <div style="background:{'#dc2626' if urgent_count else '#d97706'};padding:24px 32px;">
        <div style="color:#fff;font-size:22px;font-weight:700;">
            {'🔴 Intervention urgente requise' if urgent_count else '⚠️ Alerte de surveillance'}
        </div>
        <div style="color:rgba(255,255,255,.85);margin-top:4px;font-size:14px;">
            Examen : <strong>{exam_title}</strong> · Détecté le {now}
        </div>
    </div>

    <!-- Corps -->
    <div style="padding:28px 32px;">
        <p style="color:#334155;margin-bottom:20px;">
            L'agent de surveillance CEI a détecté <strong>{alert_count} étudiant(s)</strong>
            présentant des comportements anormaux
            {'dont <strong>' + str(urgent_count) + ' cas urgents</strong>' if urgent_count else ''}.
        </p>

        <table style="width:100%;border-collapse:collapse;font-size:14px;">
            <thead>
                <tr style="background:#f1f5f9;color:#64748b;font-size:12px;text-transform:uppercase;">
                    <th style="padding:8px;text-align:left;">Niveau</th>
                    <th style="padding:8px;text-align:left;">Étudiant</th>
                    <th style="padding:8px;text-align:center;">Risque</th>
                    <th style="padding:8px;text-align:left;">Anomalies détectées</th>
                    <th style="padding:8px;"></th>
                </tr>
            </thead>
            <tbody>{rows}</tbody>
        </table>

        <div style="margin-top:24px;padding:16px;background:#eff6ff;border-radius:8px;border-left:4px solid #2563eb;">
            <p style="color:#1e40af;font-size:13px;margin:0;">
                <strong>Action recommandée :</strong> Connectez-vous au tableau de bord CEI
                pour visualiser les flux vidéo, envoyer un avertissement ou exclure un étudiant.
            </p>
            <a href="{CEI_BASE_URL}/app" style="display:inline-block;margin-top:12px;background:#2563eb;color:#fff;
               padding:10px 20px;border-radius:8px;text-decoration:none;font-weight:600;font-size:14px;">
                Ouvrir le tableau de bord →
            </a>
        </div>
    </div>

    <div style="padding:0 32px 24px;">{_footer()}</div>
</div>
</body>
</html>"""

    return _send(to_emails, subject, html)


def send_summary_email(to_emails: list[str], exam_title: str, stats: dict) -> bool:
    """Email de rapport périodique (toutes les 15 min) envoyé à l'enseignant."""
    now = datetime.now().strftime("%d/%m/%Y à %H:%M")
    subject = f"📊 Rapport surveillance — {exam_title} ({now})"

    html = f"""
<!DOCTYPE html>
<html lang="fr">
<head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#f8fafc;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
<div style="max-width:600px;margin:32px auto;background:#fff;border-radius:12px;overflow:hidden;box-shadow:0 4px 24px rgba(0,0,0,.08);">

    <div style="background:#1e293b;padding:24px 32px;">
        <div style="color:#fff;font-size:20px;font-weight:700;">📊 Rapport de surveillance</div>
        <div style="color:#94a3b8;margin-top:4px;font-size:14px;">{exam_title} · {now}</div>
    </div>

    <div style="padding:28px 32px;">
        <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:16px;margin-bottom:24px;">
            <div style="background:#f0fdf4;border-radius:8px;padding:16px;text-align:center;">
                <div style="font-size:28px;font-weight:700;color:#16a34a;">{stats.get('total',0)}</div>
                <div style="font-size:12px;color:#64748b;margin-top:4px;">Étudiants actifs</div>
            </div>
            <div style="background:#fff7ed;border-radius:8px;padding:16px;text-align:center;">
                <div style="font-size:28px;font-weight:700;color:#d97706;">{stats.get('alerts',0)}</div>
                <div style="font-size:12px;color:#64748b;margin-top:4px;">Alertes envoyées</div>
            </div>
            <div style="background:#fef2f2;border-radius:8px;padding:16px;text-align:center;">
                <div style="font-size:28px;font-weight:700;color:#dc2626;">{stats.get('banned',0)}</div>
                <div style="font-size:12px;color:#64748b;margin-top:4px;">Exclus</div>
            </div>
        </div>

        <a href="{CEI_BASE_URL}/app" style="display:block;text-align:center;background:#2563eb;color:#fff;
           padding:12px;border-radius:8px;text-decoration:none;font-weight:600;">
            Voir le tableau de bord complet →
        </a>
    </div>

    <div style="padding:0 32px 24px;">{_footer()}</div>
</div>
</body>
</html>"""

    return _send(to_emails, subject, html)
