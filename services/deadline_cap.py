"""Échéance d'une copie : la PAUSE de 3 minutes ne repousse JAMAIS la fin au-delà de l'heure de clôture (17/11/2026).

Avant : « Pause (3 min) » créditait 3 minutes dans `extra_minutes`, comme le temps accordé par un surveillant ou l'enseignant, donc l'échéance serveur devenait
fin de l'examen + 3 min : sur l'examen 182 (clos à 9 h 45), deux étudiantes ont composé jusqu'à 9 h 48 après avoir pris leur pause.
Maintenant : le crédit de la pause (`pause_used`) est retiré du calcul de l'échéance serveur ; le temps accordé par un surveillant / l'enseignant reste, lui,
pleinement pris en compte (accommodations). La pause reste utile : elle repousse le minuteur PERSONNEL de l'étudiant, mais jamais au-delà de la clôture.
"""
from datetime import datetime

PAUSE_CREDIT_MINUTES = 3            # doit rester égal au crédit de routes/exams.py (start_break)


def deadline_extra_minutes(attempt) -> int:
    """Minutes supplémentaires qui comptent pour l'échéance SERVEUR : tout le temps accordé, sauf le crédit de pause."""
    extra = int(getattr(attempt, 'extra_minutes', 0) or 0)
    if getattr(attempt, 'pause_used', False):
        extra = max(0, extra - PAUSE_CREDIT_MINUTES)
    return extra


def closes_in_seconds(session, exam, attempt):
    """Secondes avant la clôture réelle de CETTE copie (fin de sa vague ou de l'examen + temps accordé) ; None si inconnu. Donné à la page de l'étudiant
    pour que son minuteur ne promette jamais plus de temps que le serveur n'en accorde."""
    try:
        from services import exam_waves as W
        due = W.attempt_deadline(session, exam, attempt)
        return max(0, int((due - datetime.utcnow()).total_seconds()))
    except Exception:
        return None
