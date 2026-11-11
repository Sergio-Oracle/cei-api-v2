"""
Bus de notifications CEI — file Redis par utilisateur.

Chaque appel à notify_user / notify_exam / notify_admins ajoute un événement
à la liste Redis `cei:notif:user:{id}` de chaque destinataire. Le client
draine cette file à intervalle court via /api/notifications/poll (voir
hooks/useNotificationPoll.ts) — plus aucune connexion tenue côté serveur.

Historique :
  - un canal « ntfy » (push mobile) a existé ici, jamais déployé — retiré le 09/09.
  - la livraison se faisait ensuite par Redis Pub/Sub + long-polling 25 s
    (une connexion Gunicorn tenue par client) — remplacé le 09/09 par cette
    file Redis + poll court. Avantages : zéro connexion tenue, et un
    événement émis alors que le client n'a aucun onglet ouvert est conservé
    (TTL 1 h, 50 max) au lieu d'être perdu comme avec Pub/Sub.

Les paramètres `priority` / `tags` des fonctions publiques sont conservés
(acceptés, ignorés) pour ne pas casser les ~15 appelants et rester prêts
pour un futur canal de push (Web Push/VAPID).

Usage :
    from notif_bus import notify_user, notify_exam

    notify_user(student_id, 'correction_done', 'Copie corrigée', 'Note : 14.5/20', 'high')
    notify_exam(exam_id, 'student_banned', 'Étudiant exclu', 'Moussa Diallo — fraude', 'urgent')
"""
import os, json, time, logging
from concurrent.futures import ThreadPoolExecutor

import redis as _redis

_log       = logging.getLogger('cei.notif_bus')
_REDIS_URL = os.getenv('REDIS_URL', 'redis://127.0.0.1:6379/0')

# File par utilisateur : on garde les N derniers événements, avec un TTL —
# une file jamais drainée (utilisateur parti) disparaît d'elle-même.
_QUEUE_MAX = 50
_QUEUE_TTL = 3600  # 1 h

# Historique consultable (page « Notifications » du personnel) : copie des mêmes
# événements, NON drainée par le poll — sinon la cloche annonce « 1 » mais la
# page reste vide, l'événement ayant déjà été consommé par le poll.
_HIST_MAX = 100
_HIST_TTL = 30 * 24 * 3600  # 30 jours

# Correctif montée en charge (29/08, audit) : chaque appel notify_user/
# notify_exam créait des threads OS natifs sans limite — publier les
# résultats d'un examen à 300 étudiants créait des centaines de threads d'un
# coup. Un pool borné absorbe les rafales ; ces écritures sont courtes
# (RPUSH/LTRIM/EXPIRE en pipeline), quelques workers suffisent.
_executor = ThreadPoolExecutor(max_workers=16, thread_name_prefix='notif_bus')

# Pool dédié aux écritures (opérations courtes, max 5 connexions)
# Le pool est volontairement petit : chaque processus Gunicorn possède le
# sien et une multiplication du plafond par tous les workers épuiserait Redis.
# En revanche, un ConnectionPool ordinaire lève immédiatement « Too many
# connections » quand les 16 tâches du bus se réveillent ensemble (notamment
# à la publication de centaines de résultats) et la notification est perdue.
# Le pool bloquant met les tâches excédentaires en attente quelques secondes :
# elles sont courtes et l'événement reste livré, sans augmenter le nombre de
# connexions simultanées à Redis.
_pool = _redis.BlockingConnectionPool.from_url(
    _REDIS_URL,
    decode_responses=True,
    max_connections=5,
    timeout=5,
    socket_connect_timeout=1,
)


def _get_redis() -> _redis.Redis:
    return _redis.Redis(connection_pool=_pool)


# ── Écriture dans les files Redis ───────────────────────────────────────────

def _enqueue(user_id, payload: dict) -> None:
    """Ajoute un événement à la file d'un utilisateur (RPUSH = plus récent en
    queue), tronque aux N derniers, rafraîchit le TTL — le tout en pipeline."""
    try:
        key = f'cei:notif:user:{user_id}'
        p = _get_redis().pipeline(transaction=True)
        hkey = f'cei:notif:history:{user_id}'
        data = json.dumps(payload)
        p.rpush(key, data)
        p.ltrim(key, -_QUEUE_MAX, -1)
        p.expire(key, _QUEUE_TTL)
        p.rpush(hkey, data)
        p.ltrim(hkey, -_HIST_MAX, -1)
        p.expire(hkey, _HIST_TTL)
        p.execute()
    except Exception as exc:
        _log.warning('Redis enqueue failed user=%s: %s', user_id, exc)


def _enqueue_many(user_ids, payload: dict) -> None:
    """Même chose pour plusieurs utilisateurs, en un seul aller-retour Redis
    (clés indépendantes → pas besoin de transaction globale)."""
    try:
        r = _get_redis()
        data = json.dumps(payload)
        p = r.pipeline(transaction=False)
        for uid in user_ids:
            key = f'cei:notif:user:{uid}'
            p.rpush(key, data)
            p.ltrim(key, -_QUEUE_MAX, -1)
            p.expire(key, _QUEUE_TTL)
            hkey = f'cei:notif:history:{uid}'
            p.rpush(hkey, data)
            p.ltrim(hkey, -_HIST_MAX, -1)
            p.expire(hkey, _HIST_TTL)
        p.execute()
    except Exception as exc:
        _log.warning('Redis enqueue_many failed: %s', exc)


# Une publication de résultats peut concerner 1 500 étudiants, avec un message
# différent par note. Il ne faut alors ni créer 1 500 tâches, ni tenter de les
# faire passer simultanément dans les cinq connexions Redis du processus.
# Les écritures sont regroupées en petites transactions non atomiques : chaque
# lot correspond à des clés d'utilisateurs distinctes et peut être rejoué sans
# altérer une note ou une copie.
_PERSONAL_BATCH_SIZE = 200
_PERSONAL_BATCH_RETRIES = 3


def _enqueue_personal_batch(events: list[tuple[int, dict]]) -> None:
    """Ajoute des notifications personnalisées en lots bornés.

    `events` contient ``(user_id, payload)``. Cette primitive est utilisée
    pour les publications de résultats, où le message varie d'un étudiant à
    l'autre. Elle reste interne afin que les appelants ordinaires continuent
    d'employer :func:`notify_user`.
    """
    if not events:
        return
    for start in range(0, len(events), _PERSONAL_BATCH_SIZE):
        batch = events[start:start + _PERSONAL_BATCH_SIZE]
        for attempt in range(_PERSONAL_BATCH_RETRIES):
            try:
                pipe = _get_redis().pipeline(transaction=False)
                for user_id, payload in batch:
                    key = f'cei:notif:user:{user_id}'
                    hkey = f'cei:notif:history:{user_id}'
                    data = json.dumps(payload)
                    pipe.rpush(key, data)
                    pipe.ltrim(key, -_QUEUE_MAX, -1)
                    pipe.expire(key, _QUEUE_TTL)
                    pipe.rpush(hkey, data)
                    pipe.ltrim(hkey, -_HIST_MAX, -1)
                    pipe.expire(hkey, _HIST_TTL)
                pipe.execute()
                break
            except Exception as exc:
                if attempt + 1 == _PERSONAL_BATCH_RETRIES:
                    _log.warning('Redis personal batch failed (%s event(s)): %s', len(batch), exc)
                else:
                    time.sleep(0.1 * (attempt + 1))


def notify_users(events) -> None:
    """Programme des notifications personnalisées sans saturer Redis.

    Chaque élément est ``(user_id, event_type, title, message)``. Les
    priorités et tags ne changent pas la file Redis actuelle, donc ils ne sont
    volontairement pas exposés ici. L'appel est non bloquant, comme
    :func:`notify_user`.
    """
    payloads = []
    for event in events or ():
        try:
            user_id, event_type, title, message = event
            payloads.append((int(user_id), _payload(event_type, title, message)))
        except (TypeError, ValueError):
            _log.warning('notification personnalisée ignorée : format invalide')
    if payloads:
        _executor.submit(_enqueue_personal_batch, payloads)


def _payload(event_type: str, title: str, message: str, extra: dict | None = None) -> dict:
    d = {'type': event_type, 'title': title, 'message': message, 'ts': int(time.time() * 1000)}
    if extra:
        d.update(extra)
    return d


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
    File Redis : cei:notif:user:{user_id}

    `extra` : champs additionnels fusionnés dans le payload (ex: exam_id/
    attempt_id pour un lien profond côté frontend) — jamais utilisé pour du
    contenu affiché tel quel, seulement pour du routage/deep-linking.
    """
    _executor.submit(_enqueue, user_id, _payload(event_type, title, message, extra))


def _exam_staff_ids(exam_id: int) -> set:
    """Ensemble des identifiants du personnel couvrant un examen :
    surveillants assignés (ExamProctor) + superviseur(s) du/des groupe(s)
    rattaché(s) à l'EC + professeur créateur + enseignants affectés à l'EC +
    responsables d'examen. Best-effort, session isolée."""
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

            from services.proctor_service import exam_groups
            for g in exam_groups(session, exam):
                for s in g.supervisors:
                    if s.supervisor_id:
                        ids.add(s.supervisor_id)

            if exam.created_by_id:
                ids.add(exam.created_by_id)

            # Enseignants de l'EC (responsable ou tuteur) : l'examen peut avoir été créé à leur place
            # par le responsable d'examen ou l'administrateur — ils doivent en suivre les alertes.
            from models import ECAssignment, User, UserRole
            ec_id = exam.subject.ec_id if exam.subject else None
            if ec_id:
                for (pid,) in session.query(ECAssignment.professor_id).filter_by(ec_id=ec_id):
                    if pid:
                        ids.add(pid)
            # Responsables d'examen : présents d'office dans tous les EC, donc sur tous les examens.
            for (uid,) in session.query(User.id).filter(User.role == UserRole.RESPONSABLE_EXAMEN,
                                                         User.is_active == True):
                ids.add(uid)
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
    superviseur(s) du groupe + professeur créateur), sur la file individuelle
    de chacun.

    Auparavant cette fonction publiait sur cei:notif:exam:{id}, un canal
    Pub/Sub que *personne* n'abonnait : toutes les alertes surveillant
    (bannissement, risque élevé, surveillant déconnecté…) partaient dans le
    vide (corrigé le 09/09, en même temps que le retrait de ntfy).
    """
    payload = _payload(event_type, title, message, {'exam_id': exam_id})

    def _fan_out() -> None:
        staff_ids = _exam_staff_ids(exam_id)
        if staff_ids:
            _enqueue_many(staff_ids, payload)

    _executor.submit(_fan_out)


def _enqueue_admins(payload: dict) -> None:
    """Ajoute l'événement à la file de chaque administrateur."""
    try:
        from models import get_session, User, UserRole
        session = get_session()
        try:
            admin_ids = [u.id for u in session.query(User).filter(User.role.in_([UserRole.ADMIN, UserRole.RESPONSABLE_EXAMEN])).all()]
        finally:
            session.close()
        if admin_ids:
            _enqueue_many(admin_ids, payload)
    except Exception as exc:
        _log.warning('notify_admins enqueue failed: %s', exc)


def notify_admins(
    event_type: str,
    title: str,
    message: str,
    priority: str = 'default',
    tags: list[str] | None = None,
) -> None:
    """
    Notifie tous les administrateurs (alertes infra : panne MinIO, etc.).
    File Redis : cei:notif:user:{admin_id} (une par admin, pour le badge Header)
    """
    _executor.submit(_enqueue_admins, _payload(event_type, title, message))
