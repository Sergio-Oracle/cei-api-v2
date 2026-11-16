"""Messagerie du personnel d'un examen (14/11/2026) : enseignant, responsable d'examen, administrateur, superviseur ↔ surveillants.

Demande : « il faut qu'il y ait possibilité d'échanger par message » entre l'enseignant / le responsable et les surveillants (jusque-là seul
l'appel existait, et dans un seul sens). Un fil par examen : message à TOUTE l'équipe (destinataire vide) ou à une personne précise.
Qui en fait partie : le personnel qui couvre l'examen (créateur, enseignants de l'EC, responsables d'examen, surveillants affectés, superviseurs
de leurs groupes — `notif_bus._exam_staff_ids`) + les administrateurs. Jamais un étudiant.
"""
from __future__ import annotations

from datetime import datetime

MAX_BODY = 1000
STAFF_ROLES = ('professor', 'admin', 'responsable_examen', 'surveillant', 'superviseur')


def team_ids(session, exam_id: int) -> set:
    """Identifiants des membres de l'équipe de l'examen (administrateurs actifs compris)."""
    from notif_bus import _exam_staff_ids
    from models import User, UserRole
    ids = set(_exam_staff_ids(exam_id))
    for (uid,) in session.query(User.id).filter(User.role == UserRole.ADMIN, User.is_active == True):
        ids.add(uid)
    return ids


def visible_team(session, exam_id: int) -> set:
    """Membres affichés et notifiés : le personnel de l'examen + les administrateurs QUI ONT PARTICIPÉ à ce fil. Les autres administrateurs (comptes de gestion,
    de test…) peuvent lire et écrire s'ils ouvrent la messagerie, mais ne sont ni listés ni notifiés à chaque message."""
    from notif_bus import _exam_staff_ids
    from models import ExamStaffMessage
    ids = set(_exam_staff_ids(exam_id))
    for sid, rid in session.query(ExamStaffMessage.sender_id, ExamStaffMessage.recipient_id).filter(ExamStaffMessage.exam_id == exam_id).distinct():
        ids.add(sid)
        if rid:
            ids.add(rid)
    return ids & team_ids(session, exam_id)


def may_chat(session, user, exam_id: int) -> bool:
    if user is None or getattr(user.role, 'value', str(user.role)) not in STAFF_ROLES or not user.is_active:
        return False
    return user.id in team_ids(session, exam_id)


def participants(session, exam_id: int) -> list:
    from models import User
    ids = visible_team(session, exam_id)
    rows = session.query(User).filter(User.id.in_(list(ids)), User.is_active == True).all() if ids else []
    return sorted(({'id': u.id, 'name': u.full_name, 'role': u.role.value} for u in rows if u.role.value in STAFF_ROLES), key=lambda x: (x['role'], x['name'] or ''))


def _row(m, names: dict) -> dict:
    return {'id': m.id, 'sender_id': m.sender_id, 'sender_name': names.get(m.sender_id, ''), 'recipient_id': m.recipient_id,
            'recipient_name': names.get(m.recipient_id, '') if m.recipient_id else None, 'body': m.body,
            'created_at': (m.created_at or datetime.utcnow()).isoformat() + 'Z'}


def fetch(session, exam_id: int, me_id: int, since_id: int = 0, limit: int = 200) -> list:
    """Messages visibles par `me_id` : ceux pour toute l'équipe, ceux qui lui sont adressés, ceux qu'il a écrits. Ordre chronologique."""
    from models import ExamStaffMessage, User
    q = session.query(ExamStaffMessage).filter(
        ExamStaffMessage.exam_id == exam_id,
        (ExamStaffMessage.recipient_id.is_(None)) | (ExamStaffMessage.recipient_id == me_id) | (ExamStaffMessage.sender_id == me_id))
    if since_id:
        rows = q.filter(ExamStaffMessage.id > since_id).order_by(ExamStaffMessage.id.asc()).limit(limit).all()
    else:
        rows = list(reversed(q.order_by(ExamStaffMessage.id.desc()).limit(limit).all()))      # les plus récents d'abord à l'ouverture
    ids = {m.sender_id for m in rows} | {m.recipient_id for m in rows if m.recipient_id}
    names = {u.id: u.full_name for u in session.query(User).filter(User.id.in_(list(ids))).all()} if ids else {}
    return [_row(m, names) for m in rows]


def send(session, exam, sender, body: str, to_id: int | None = None) -> dict:
    """Enregistre le message et prévient les destinataires (notification en temps réel). Lève ValueError pour une demande invalide."""
    from models import ExamStaffMessage
    import notif_bus
    text = (body or '').strip()
    if not text:
        raise ValueError('Message vide')
    if len(text) > MAX_BODY:
        raise ValueError(f'Message trop long ({MAX_BODY} caractères au plus)')
    team = team_ids(session, exam.id)
    if to_id is not None and (to_id not in team or to_id == sender.id):
        raise ValueError("Destinataire inconnu pour cet examen")
    m = ExamStaffMessage(exam_id=exam.id, sender_id=sender.id, recipient_id=to_id, body=text)
    session.add(m); session.commit()
    snippet = text if len(text) <= 120 else text[:117] + '…'
    for uid in ([to_id] if to_id else sorted(visible_team(session, exam.id) - {sender.id})):
        try:
            notif_bus.notify_user(uid, 'staff_message', f"Message de {sender.full_name} — {exam.title}", snippet, priority='default',
                                  extra={'exam_id': exam.id, 'sender_id': sender.id, 'sender_name': sender.full_name, 'direct': bool(to_id)})
        except Exception:
            pass
    return _row(m, {sender.id: sender.full_name})
