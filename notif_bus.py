"""
Bus de notifications CEI — Redis Pub/Sub.

Chaque appel à notify_user / notify_exam / notify_admins publie sur un ou
plusieurs canaux Redis Pub/Sub `cei:notif:user:{id}`, consommés par
/api/notifications/poll (long-poll navigateur, badge Header temps réel).

Historique : un second canal « ntfy » (push mobile / hors navigateur) a
existé ici mais n'a jamais été déployé (NTFY_URL toujours vide, aucun
serveur ntfy, topics devinables sans ACL) — retiré le 09/09. Les
paramètres `priority` / `tags` des fonctions publiques sont conservés
(acceptés, ignorés) pour ne pas casser les appelants et rester prêts pour
un futur canal de push (Web Push/VAPID).

Usage :
    from notif_bus import notify_user, notify_exam

    notify_user(student_id, 'correction_done', 'Copie corrigée', 'Note : 14.5/20', 'high')
    notify_exam(exam_id, 'student_banned', 'Étudiant exclu', 'Moussa Diallo — fraude', 'urgent')
"""
import os, json, logging
from concurrent.futures import ThreadPoolExecutor

import redis as _redis

_log       = logging.getLogger('cei.notif_bus')
_REDIS_URL = os.getenv('REDIS_URL', 'redis://127.0.0.1:6379/0')

# Correctif montée en charge (29/08, audit) : chaque appel notify_user/
# notify_exam créait des threads OS natifs (Thread(...).start()) sans aucune
# limite — publier les résultats d'un examen à 300 étudiants (une boucle
# notify_user par étudiant, voir publish_exam_results) créait des centaines
# de threads d'un coup. Un pool borné absorbe les rafales sans faire
# exploser le nombre de threads système ; ces publications sont courtes
# (Redis PUBLISH), une file d'attente sur quelques workers suffit largement.
_executor = ThreadPoolExecutor(max_workers=16, thread_name_prefix='notif_bus')

# Pool dédié aux publications (opérations courtes, max 5 connexions)
_pool = _redis.ConnectionPool.from_url(
    _REDIS_URL,
    decode_responses=True,
    max_connections=5,
    socket_connect_timeout=1,
)


def _get_redis() -> _redis.Redis:
    return _redis.Redis(connection_pool=_pool)


# ── Publication Redis ────────────────────────────────────────────────────────

def _redis_publish(channel: str, payload: dict) -> None:
    try:
        _get_redis().publish(channel, json.dumps(payload))
    except Exception as exc:
        _log.warning('Redis publish failed channel=%s: %s', channel, exc)


def _redis_publish_many(user_ids, payload: dict) -> None:
    """Publie le même payload sur le canal individuel de plusieurs utilisateurs."""
    try:
        r = _get_redis()
        data = json.dumps(payload)
        for uid in user_ids:
            try:
                r.publish(f'cei:notif:user:{uid}', data)
            except Exception as exc:
                _log.warning('Redis publish failed user=%s: %s', uid, exc)
    except Exception as exc:
        _log.warning('Redis publish_many failed: %s', exc)


# ── API publique ─────────────────────────────────────────────────────────────

def notify_user(
    user_id: int,
    event_type: str,
    title: str,
    message: str,
    priority: str = 'default',
    tags: list[str] | None = None,
    extra: dict | None = None,
) -> None:
    """
    Notifie un utilisateur précis (étudiant, professeur).
    Canal Redis : cei:notif:user:{user_id}

    `extra` : champs additionnels fusionnés dans le payload (ex: exam_id/
    attempt_id pour un lien profond côté frontend) — jamais utilisé pour du
    contenu affiché tel quel, seulement pour du routage/deep-linking.
    """
    payload = {'type': event_type, 'title': title, 'message': message}
    if extra:
        payload.update(extra)
    _executor.submit(_redis_publish, f'cei:notif:user:{user_id}', payload)


def _exam_staff_ids(exam_id: int) -> set:
    """Ensemble des identifiants du personnel couvrant un examen :
    surveillants assignés (ExamProctor) + superviseur(s) du/des groupe(s)
    rattaché(s) à l'EC + professeur créateur. Best-effort, session isolée."""
    ids: set = set()
    try:
        from models import (
            get_session, OnlineExam, ExamProctor,
            ProctorGroup, ProctorGroupEC,
        )
        session = get_session()
        try:
            exam = session.query(OnlineExam).get(exam_id)
            if not exam:
                return ids

            for ep in session.query(ExamProctor).filter_by(exam_id=exam_id).all():
                if ep.proctor_id:
                    ids.add(ep.proctor_id)

            ec_id = exam.subject.ec_id if exam.subject else None
            if ec_id:
                groups = (
                    session.query(ProctorGroup)
                    .join(ProctorGroupEC, ProctorGroupEC.group_id == ProctorGroup.id)
                    .filter(ProctorGroupEC.ec_id == ec_id)
                    .all()
                )
                for g in groups:
                    for s in g.supervisors:
                        if s.supervisor_id:
                            ids.add(s.supervisor_id)

            if exam.created_by_id:
                ids.add(exam.created_by_id)
        finally:
            session.close()
    except Exception as exc:
        _log.warning('_exam_staff_ids(%s) failed: %s', exam_id, exc)
    return ids


def notify_exam(
    exam_id: int,
    event_type: str,
    title: str,
    message: str,
    priority: str = 'default',
    tags: list[str] | None = None,
) -> None:
    """
    Notifie tout le personnel couvrant un examen (surveillants assignés +
    superviseur(s) du groupe + professeur créateur).

    Publie sur le canal individuel de chacun (cei:notif:user:{id}) — c'est
    ce que /api/notifications/poll écoute. Auparavant cette fonction publiait
    sur cei:notif:exam:{id}, un canal que *personne* n'abonnait : toutes les
    alertes surveillant (bannissement, risque élevé, surveillant déconnecté…)
    partaient dans le vide (corrigé le 09/09, en même temps que le retrait de
    ntfy qui était l'autre canal — désactivé — de cette fonction).
    """
    payload = {'type': event_type, 'title': title, 'message': message}

    def _fan_out() -> None:
        staff_ids = _exam_staff_ids(exam_id)
        if staff_ids:
            _redis_publish_many(staff_ids, payload)

    _executor.submit(_fan_out)


def _publish_to_admins(payload: dict) -> None:
    """Publie sur le canal Redis individuel de chaque administrateur (pour le long-poll)."""
    try:
        from models import get_session, User, UserRole
        session = get_session()
        try:
            admin_ids = [u.id for u in session.query(User).filter_by(role=UserRole.ADMIN).all()]
        finally:
            session.close()
        _redis_publish_many(admin_ids, payload)
    except Exception as exc:
        _log.warning('notify_admins redis publish failed: %s', exc)


def notify_admins(
    event_type: str,
    title: str,
    message: str,
    priority: str = 'default',
    tags: list[str] | None = None,
) -> None:
    """
    Notifie tous les administrateurs (alertes infra : panne MinIO, etc.).
    Canal Redis : cei:notif:user:{admin_id} (un par admin, pour le badge Header)
    """
    payload = {'type': event_type, 'title': title, 'message': message}
    _executor.submit(_publish_to_admins, payload)
