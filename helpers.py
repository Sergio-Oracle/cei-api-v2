"""
Helpers partagés — accessibles par app.py ET les blueprints.
"""
import re
from datetime import datetime, timezone


def utcnow() -> datetime:
    """datetime UTC compatible Python 3.12+"""
    return datetime.now(timezone.utc)


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
