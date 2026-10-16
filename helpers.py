"""
Helpers partagés — accessibles par app.py ET les blueprints.
"""
import re
from datetime import datetime, timezone


def utcnow() -> datetime:
    """datetime UTC compatible Python 3.12+"""
    return datetime.now(timezone.utc)


def require_admin(session):
    """Vérifie que l'utilisateur courant est ADMINISTRATEUR (gestion des utilisateurs : le
    responsable d'examen n'y a pas accès), ferme la session et
    renvoie None sinon (à l'appelant de retourner 403). Factorisé depuis
    admin_users.py — utilisé aussi par routes/api_clients.py."""
    from auth_paseto import get_current_user_id
    from models import User, UserRole
    user = session.query(User).filter_by(id=get_current_user_id()).first()
    if not user or user.role != UserRole.ADMIN:
        session.close()
        return None
    return user


def require_admin_like(session):
    """Administrateur OU responsable d'examen : tout ce que fait l'administrateur
    SAUF la gestion des utilisateurs (qui reste sur require_admin)."""
    from auth_paseto import get_current_user_id
    from models import User, UserRole
    user = session.query(User).filter_by(id=get_current_user_id()).first()
    if not user or user.role not in (UserRole.ADMIN, UserRole.RESPONSABLE_EXAMEN):
        session.close()
        return None
    return user


def strip_bareme_from_content(content: str) -> str:
    """Retirer la section barème du contenu du sujet (pour les étudiants)."""
    if not content:
        return content
    m = re.search(r'\n[═=─]{5,}[^\n]*\n[^\n]*[Bb]ar[eè]me', content)
    if m:
        return content[:m.start()].rstrip()
    m = re.search(r'\n\s*[Bb]ar[eè]me\s+de\s+[Nn]otation', content, re.IGNORECASE)
    if m:
        return content[:m.start()].rstrip()
    m = re.search(r'\nBAR[ÈE]ME\s*\n', content)
    if m:
        return content[:m.start()].rstrip()
    return content
