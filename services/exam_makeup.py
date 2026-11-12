"""Rattrapage : une SESSION À PART pour les étudiants qui n'ont pas pu composer l'examen.

Cas réel : des étudiants absents, sans moyens (pas de connexion, pas de matériel) ou empêchés avant d'avoir vu les questions n'ont pas
composé. Le personnel les choisit et fixe une nouvelle date ; ils composent les MÊMES questions, dans une session distincte.

Ce n'est PAS une vague. Les vagues étalent la charge d'UNE session entre tous ses étudiants ; le rattrapage est une seconde session,
pour des étudiants choisis, à une autre date. Les deux mécanismes ne se touchent pas :
  - le rattrapage est un examen à part (`OnlineExam.makeup_of_id` = examen d'origine) : mêmes questions et mêmes réglages, ses propres
    horaires, ses surveillants, ses copies, ses statistiques, sa correction et sa publication de résultats ;
  - l'examen d'origine n'est PAS modifié : ni son statut, ni ses horaires, ni ses vagues, ni ses copies (aucune copie n'est effacée) ;
  - seuls les étudiants de la liste du rattrapage y accèdent (voir services/exam_access.py) : être inscrit à l'UE ne suffit pas ;
  - le rattrapage peut lui-même être passé par vagues, comme n'importe quel examen.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone

from models import (
    ExamAttempt, ExamMakeupStudent, OnlineExam, User, ExamStatus, AttemptStatus,
)
from services import exam_waves as W

# Colonnes de l'examen d'origine qui ne sont PAS recopiées dans le rattrapage
_NOT_COPIED = {
    'id', 'title', 'start_time', 'end_time', 'status', 'created_at', 'makeup_of_id',
    'results_published', 'results_published_at',          # le rattrapage a sa propre publication
    'scheduled_correction_at', 'correction_triggered_at',  # la correction planifiée de l'examen d'origine ne le concerne pas
}
MAX_STUDENTS = 3000


class MakeupError(ValueError):
    """Erreur de saisie, message prêt à afficher."""


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _composed(attempt) -> bool:
    """Vrai si l'étudiant a une vraie copie dans l'examen d'origine (vue ou avec réponses)."""
    from routes.exams import _never_seen
    return attempt is not None and not _never_seen(attempt)


def _makeups(session, base_exam):
    return session.query(OnlineExam).filter(OnlineExam.makeup_of_id == base_exam.id).order_by(OnlineExam.id).all()


def _in_makeups(session, base_exam) -> dict:
    """{student_id: titre du rattrapage} pour les étudiants déjà admis à un rattrapage de cet examen."""
    ids = [m.id for m in _makeups(session, base_exam)]
    if not ids:
        return {}
    titles = {m.id: m.title for m in _makeups(session, base_exam)}
    return {sid: titles[eid] for (sid, eid) in session.query(ExamMakeupStudent.student_id, ExamMakeupStudent.exam_id)
            .filter(ExamMakeupStudent.exam_id.in_(ids)).all()}


PASS_MARK = 10.0      # sous la moyenne : note inférieure à 10/20


def candidates(session, base_exam, search: str = '', include_composed: bool = False, limit: int = 200, offset: int = 0) -> dict:
    """Étudiants inscrits à l'examen pouvant être convoqués au rattrapage.

    PRIORITAIRES (liste par défaut, remontés en premier) :
      - les ABSENTS : n'ont pas pu se connecter pour composer (aucune copie réelle) ;
      - les étudiants SOUS LA MOYENNE : note inférieure à 10/20.
    Avec `include_composed` : tous les inscrits (les autres — 10/20 et plus, ou copie pas encore notée — suivent) ; la copie d'origine
    reste intacte dans tous les cas.
    Chaque ligne : id, name, email, absent, below_average, composed, score, makeup (titre du rattrapage où il est déjà convoqué)."""
    eligible = W.eligible_student_ids(session, base_exam)
    empty = {'candidates': [], 'total': 0, 'priority_total': 0, 'absent_total': 0, 'below_total': 0, 'composed_total': 0}
    if not eligible:
        return empty
    attempts = {a.student_id: a for a in session.query(ExamAttempt).filter(
        ExamAttempt.exam_id == base_exam.id, ExamAttempt.student_id.in_(eligible)).all()}
    taken = _in_makeups(session, base_exam)
    users = {r[0]: (r[1], r[2]) for r in session.query(User.id, User.full_name, User.email).filter(User.id.in_(eligible)).all()}
    rows = []
    absent_total = below_total = composed_total = 0
    for sid in eligible:
        a = attempts.get(sid)
        comp = _composed(a)
        score = a.score if (comp and a is not None) else None
        absent = not comp
        below = comp and score is not None and float(score) < PASS_MARK
        composed_total += 1 if comp else 0
        absent_total += 1 if absent else 0
        below_total += 1 if below else 0
        priority = absent or below
        if not priority and not include_composed:
            continue
        name, email = users.get(sid, ('', ''))
        rows.append({'id': sid, 'name': name, 'email': email, 'absent': absent, 'below_average': bool(below), 'composed': comp,
                     'score': score, 'makeup': taken.get(sid), '_priority': priority})
    s = (search or '').strip().lower()
    if s:
        rows = [r for r in rows if s in (r['name'] or '').lower() or s in (r['email'] or '').lower()]
    # absents et sous la moyenne d'abord, puis les autres ; par ordre alphabétique dans chaque groupe
    rows.sort(key=lambda r: (0 if r['_priority'] else 1, (r['name'] or '').lower(), r['id']))
    total = len(rows)
    off = max(0, offset)
    page = rows[off:off + max(1, min(limit, 500))]
    for r in page:
        r.pop('_priority', None)
    return {'candidates': page, 'total': total, 'priority_total': absent_total + below_total,
            'absent_total': absent_total, 'below_total': below_total, 'composed_total': composed_total}


def list_makeups(session, base_exam) -> list:
    out = []
    for m in _makeups(session, base_exam):
        n = session.query(ExamMakeupStudent.id).filter_by(exam_id=m.id).count()
        started = session.query(ExamAttempt.id).filter(ExamAttempt.exam_id == m.id, ExamAttempt.exam_seen_at.isnot(None)).count()
        d = m.to_dict()
        out.append({'id': m.id, 'title': m.title, 'start_time': d['start_time'], 'end_time': d['end_time'],
                    'status': m.status.value, 'students': n, 'participants': started})
    return out


def _check_window(base_exam, start, end):
    if start is None or end is None or end <= start:
        raise MakeupError("La fin du rattrapage doit être postérieure à son début.")
    if start < _now() - timedelta(minutes=1):
        raise MakeupError("Le rattrapage doit commencer dans le futur.")
    if (end - start) < timedelta(minutes=(base_exam.duration_minutes or 0)):
        raise MakeupError(f"La fenêtre de rattrapage ne peut pas être plus courte que la durée de l'examen ({base_exam.duration_minutes} min).")


def _check_students(session, base_exam, student_ids) -> list:
    try:
        ids = sorted({int(i) for i in (student_ids or [])})
    except (TypeError, ValueError):
        raise MakeupError("Liste d'étudiants invalide.")
    if not ids:
        raise MakeupError("Choisissez au moins un étudiant.")
    if len(ids) > MAX_STUDENTS:
        raise MakeupError(f"Trop d'étudiants d'un coup ({MAX_STUDENTS} au maximum).")
    eligible = set(W.eligible_student_ids(session, base_exam))
    if any(i not in eligible for i in ids):
        raise MakeupError("Certains étudiants ne sont pas inscrits à cet examen.")
    return ids


def _next_title(session, base_exam) -> str:
    n = len(_makeups(session, base_exam))
    base = f"{base_exam.title} — Rattrapage"
    return base if n == 0 else f"{base} {n + 1}"


def create_makeup(session, base_exam, student_ids, start: datetime, end: datetime, actor_id=None) -> OnlineExam:
    """Crée la session de rattrapage : un examen à part, ouvert, réservé aux étudiants choisis. Sans commit (l'appelant valide).
    L'examen d'origine n'est pas touché."""
    if base_exam.makeup_of_id:
        raise MakeupError("Ouvrez l'examen d'origine pour organiser un rattrapage.")
    if base_exam.status in (ExamStatus.DRAFT, ExamStatus.SCHEDULED) or base_exam.end_time > _now():
        raise MakeupError("L'examen n'est pas encore terminé : le rattrapage s'organise une fois l'examen passé.")
    ids = _check_students(session, base_exam, student_ids)
    _check_window(base_exam, start, end)

    values = {c.name: getattr(base_exam, c.name) for c in OnlineExam.__table__.columns if c.name not in _NOT_COPIED}
    m = OnlineExam(**values, title=_next_title(session, base_exam), start_time=start, end_time=end,
                   status=ExamStatus.ACTIVE, makeup_of_id=base_exam.id, results_published=False)
    session.add(m)
    session.flush()
    for sid in ids:
        session.add(ExamMakeupStudent(exam_id=m.id, student_id=sid, added_by_id=actor_id))
    session.flush()
    return m


def add_students(session, makeup_exam, student_ids, actor_id=None) -> list:
    """Ajoute des étudiants à un rattrapage existant (jamais terminé). Renvoie les identifiants réellement ajoutés."""
    if not makeup_exam.makeup_of_id:
        raise MakeupError("Cet examen n'est pas un rattrapage.")
    if makeup_exam.end_time <= _now():
        raise MakeupError("Ce rattrapage est terminé.")
    base = session.get(OnlineExam, makeup_exam.makeup_of_id)
    ids = _check_students(session, base, student_ids)
    have = {sid for (sid,) in session.query(ExamMakeupStudent.student_id).filter_by(exam_id=makeup_exam.id)}
    added = [i for i in ids if i not in have]
    for sid in added:
        session.add(ExamMakeupStudent(exam_id=makeup_exam.id, student_id=sid, added_by_id=actor_id))
    session.flush()
    return added


# ── Avis aux étudiants (notification + e-mail), en arrière-plan ─────────────────

def notify_students(exam_id: int, student_ids) -> None:
    """Prévient chaque étudiant admis : notification + e-mail avec date et heure. Jamais bloquant, une personne après l'autre."""
    ids = list(student_ids)
    if not ids:
        return

    def run():
        import os
        from models import get_session
        s = get_session()
        try:
            exam = s.get(OnlineExam, exam_id)
            if exam is None:
                return
            slot = W.slot_label(exam.start_time, exam.end_time)
            title, duration = exam.title, exam.duration_minutes
            users = [(u.id, u.email, u.full_name, bool(u.has_email), bool(u.is_active))
                     for u in s.query(User).filter(User.id.in_(ids)).all()]
        finally:
            s.close()
        app_url = os.getenv('APP_URL', 'https://dev-cei.ddns.net').rstrip('/')
        from notif_bus import notify_user
        from utils import send_exam_makeup_email
        for uid, email, name, has_email, active in users:
            if not active:
                continue
            try:
                notify_user(uid, 'exam_makeup', 'Rattrapage',
                            f"{title} : {slot}. Connectez-vous à l'heure indiquée.",
                            priority='high', tags=['calendar'], extra={'exam_id': exam_id})
            except Exception as e:
                print(f"[rattrapage] notification {uid}: {e}")
            if has_email and email and '@temp.edu' not in email:
                try:
                    send_exam_makeup_email(email, name, title, slot, duration, f"{app_url}/exam/{exam_id}")
                except Exception as e:
                    print(f"[rattrapage] e-mail {uid}: {e}")
                time.sleep(0.15)                      # cadence douce vers le serveur SMTP

    threading.Thread(target=run, daemon=True, name='makeup-notices').start()
