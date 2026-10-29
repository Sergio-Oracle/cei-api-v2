"""Surveillance de l'expiration des certificats TLS (cei.unchk.sn, sfu-cei.unchk.sn) : les administrateurs sont
prévenus chaque jour dès qu'il reste moins de 45 jours, jusqu'au renouvellement. Appelée par la tâche de fond
(services/exam_closer.py), au plus une fois par jour."""
import socket
import ssl
from datetime import datetime, timezone

from cache import cache_set_nx

HOSTS = ['cei.unchk.sn', 'sfu-cei.unchk.sn']
WARN_DAYS = 45


def days_left(host: str, port: int = 443, timeout: float = 8.0) -> int:
    ctx = ssl.create_default_context()
    with socket.create_connection((host, port), timeout=timeout) as raw:
        with ctx.wrap_socket(raw, server_hostname=host) as tls:
            cert = tls.getpeercert()
    end = datetime.fromtimestamp(ssl.cert_time_to_seconds(cert['notAfter']), tz=timezone.utc)
    return (end - datetime.now(timezone.utc)).days


def check(warn_days: int = WARN_DAYS) -> list:
    """Renvoie les hôtes dont le certificat expire bientôt ; prévient les administrateurs (une fois par jour et par hôte)."""
    soon = []
    for host in HOSTS:
        try:
            left = days_left(host)
        except Exception as e:
            print(f"[certificats] {host} : lecture impossible ({e})")
            continue
        if left > warn_days:
            continue
        soon.append((host, left))
        if not cache_set_nx(f'cei:certwatch:{host}', 24 * 3600):
            continue
        try:
            from notif_bus import notify_admins
            msg = (f"Le certificat de {host} expire dans {left} jour(s). Demandez son renouvellement à la DITSI "
                   f"(certificat *.unchk.sn) puis remplacez-le sur la production et sur srv-sfu-cei.")
            notify_admins('cert_expiry', 'Certificat à renouveler', msg)
            print(f"[certificats] {msg}")
        except Exception as e:
            print(f"[certificats] notification : {e}")
    return soon
