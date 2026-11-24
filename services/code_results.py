"""Résultats des tests de code d'une copie (24/11/2026).

Le code des étudiants s'exécute DANS UN NAVIGATEUR (WebAssembly, enceinte isolée : cei-next/lib/codeRunner.ts), jamais sur nos serveurs.
Pour la note, c'est le navigateur de l'ENSEIGNANT (ou du responsable d'examen) qui exécute tous les tests — visibles ET cachés — sur la réponse
de l'étudiant, puis envoie les sorties obtenues ici ; le serveur recalcule lui-même le verdict de chaque test (services/question_types.py : judge_code)
et enregistre le résultat pour (copie, question). Les tests cachés ne sont donc jamais transmis à un étudiant.

Table créée à la demande (CREATE TABLE IF NOT EXISTS) : aucune migration à jouer, la même procédure sur tous les serveurs.
"""
import json
import threading

from sqlalchemy import text

from models import get_session

_READY = False
_LOCK = threading.Lock()


def ensure_table(session=None) -> None:
    """Crée la table au premier usage, dans SA PROPRE session (jamais de commit sur celle de l'appelant)."""
    global _READY
    if _READY:
        return
    with _LOCK:
        if _READY:
            return
        own = get_session()
        try:
            own.execute(text(
                "CREATE TABLE IF NOT EXISTS code_test_results ("
                " attempt_id INTEGER NOT NULL REFERENCES exam_attempts(id) ON DELETE CASCADE,"
                " num VARCHAR(16) NOT NULL,"
                " code_sha CHAR(64) NOT NULL,"
                " fraction DOUBLE PRECISION NOT NULL,"
                " summary TEXT,"
                " detail TEXT,"
                " run_by INTEGER,"
                " run_at TIMESTAMP DEFAULT (now() AT TIME ZONE 'utc'),"
                " PRIMARY KEY (attempt_id, num))"))
            own.commit()
            _READY = True
        finally:
            own.close()


def load(session, attempt_id: int) -> dict:
    """{numéro: {'sha', 'fraction', 'summary'}} des questions déjà testées de cette copie."""
    ensure_table(session)
    rows = session.execute(text("SELECT num, code_sha, fraction, summary FROM code_test_results WHERE attempt_id = :a"), {'a': attempt_id}).fetchall()
    return {r[0]: {'sha': r[1], 'fraction': r[2], 'summary': r[3]} for r in rows}


def load_detail(session, attempt_id: int) -> dict:
    ensure_table(session)
    rows = session.execute(text("SELECT num, code_sha, fraction, summary, detail, run_at FROM code_test_results WHERE attempt_id = :a"), {'a': attempt_id}).fetchall()
    out = {}
    for r in rows:
        try:
            det = json.loads(r[4]) if r[4] else []
        except Exception:
            det = []
        out[r[0]] = {'sha': r[1], 'fraction': r[2], 'summary': r[3], 'detail': det, 'run_at': r[5].isoformat() + 'Z' if r[5] else None}
    return out


def save(session, attempt_id: int, num: str, sha: str, fraction: float, summary: str, detail: list, user_id) -> None:
    ensure_table(session)
    session.execute(text(
        "INSERT INTO code_test_results (attempt_id, num, code_sha, fraction, summary, detail, run_by, run_at)"
        " VALUES (:a, :n, :s, :f, :m, :d, :u, (now() AT TIME ZONE 'utc'))"
        " ON CONFLICT (attempt_id, num) DO UPDATE SET code_sha = :s, fraction = :f, summary = :m, detail = :d, run_by = :u, run_at = (now() AT TIME ZONE 'utc')"),
        {'a': attempt_id, 'n': str(num), 's': sha, 'f': float(fraction), 'm': summary, 'd': json.dumps(detail, ensure_ascii=False), 'u': user_id})
    session.commit()
