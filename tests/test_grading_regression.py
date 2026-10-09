"""Régression : sujet importé (points seulement dans le barème) doit être noté."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from routes.exams import _question_points_map

def test_points_lus_dans_le_bareme():
    content = "Question 1 — Quelle est la capitale ?\nA) X\nB) Y\n\nQuestion 2 — Autre ?\nA) X"
    rubric = "Question 1 (1.5 pts) : B\nQuestion 2 (2 pts) : A\n──────\nTOTAL"
    m = _question_points_map(content, rubric)
    assert m.get("1") == 1.5 and m.get("2") == 2.0

def test_points_dans_le_sujet():
    m = _question_points_map("Question 1 — Titre (3 pts)\nA) x", "")
    assert m.get("1") == 3.0


def test_dates_api_en_utc_explicite():
    """Régression 09/10 : une date sans « Z » était lue à l'heure locale de l'appareil
    (examens « Terminé », « déjà soumis »)."""
    from datetime import datetime
    from models import utc_iso
    assert utc_iso(datetime(2026, 10, 9, 12, 0, 0)).endswith('Z')
    assert utc_iso(None) is None
