"""
Blueprint Notifications.

GET  /api/notifications          — corrections récentes (étudiant)
PUT  /api/notifications/mark-read — marquer toutes comme lues
GET  /api/notifications/poll     — draine la file Redis de l'utilisateur (réponse immédiate)
"""
import os, json, logging
from datetime import datetime, timezone
from flask import Blueprint, jsonify
from auth_paseto import paseto_required, get_current_user_id
from extensions import limiter
from helpers     import utcnow
from models      import (
    get_session, User, UserRole, utc_iso,
    StudentPaper, ExamAttempt,
)
import redis as _redis_lib

_log       = logging.getLogger('cei.notifications')
_REDIS_URL = os.getenv('REDIS_URL', 'redis://127.0.0.1:6379/0')

notifications_bp = Blueprint('notifications', __name__)


def _staff_notifications(user):
    """Personnel (professeur, surveillant, superviseur, admin) : historique des
    événements du bus (Redis, 30 jours) — le poll les consomme pour la cloche,
    cette copie reste pour la page « Notifications »."""
    items = []
    try:
        r = _redis_lib.from_url(_REDIS_URL, decode_responses=True, socket_connect_timeout=1)
        raw = r.lrange(f'cei:notif:history:{user.id}', 0, -1)
    except Exception as e:
        _log.warning('historique notifications indisponible: %s', e)
        raw = []
    last_read = user.notifications_last_read
    last_ms = None
    if last_read:
        if last_read.tzinfo is None:
            last_read = last_read.replace(tzinfo=timezone.utc)
        last_ms = last_read.timestamp() * 1000
    for line in raw:
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        ts = ev.get('ts') or 0
        items.append({
            'id': f"{ev.get('type', 'evt')}_{ts}",
            'type': ev.get('type'),
            'title': ev.get('title'),
            'message': ev.get('message'),
            'created_at': datetime.fromtimestamp(ts / 1000, tz=timezone.utc).isoformat().replace('+00:00', 'Z') if ts else None,
            'exam_id': ev.get('exam_id'),
            'is_read': bool(last_ms and ts <= last_ms),
        })
    items.sort(key=lambda x: x['created_at'] or '', reverse=True)
    return jsonify({'notifications': items, 'count': len(items),
                    'unread_count': sum(1 for i in items if not i['is_read'])})


@notifications_bp.route('/api/notifications', methods=['GET'])
@paseto_required
def get_notifications():
    user_id = get_current_user_id()
    session = get_session()
    try:
        user = session.query(User).get(user_id)
        if not user:
            return jsonify({'notifications': [], 'count': 0, 'unread_count': 0})
        if user.role != UserRole.STUDENT:
            return _staff_notifications(user)

        notifications = []

        for att in session.query(ExamAttempt).filter(
            ExamAttempt.student_id == user_id,
            ExamAttempt.corrected_at != None,
            ExamAttempt.score != None,
        ).order_by(ExamAttempt.corrected_at.desc()).limit(60).all():
            exam = att.exam
            # La note n'est connue de l'étudiant qu'une fois les résultats publiés
            # par l'enseignant (même règle que /api/student/online_results)
            if not exam or not exam.results_published:
                continue
            notifications.append({
                'id':           f'attempt_{att.id}',
                'type':         'online_exam',
                'title':        exam.title if exam else 'Examen en ligne',
                'message':      f'Votre copie a été corrigée — note : {att.score:.2f}/20' if att.score is not None else 'Votre copie a été corrigée',
                # la notification date de la publication (sinon elle paraîtrait déjà lue)
                'created_at':   utc_iso(exam.results_published_at or att.corrected_at),
                'attempt_id':   att.id,
            })

        for p in session.query(StudentPaper).filter(
            StudentPaper.student_id == user_id,
            StudentPaper.corrected_at != None,
        ).order_by(StudentPaper.corrected_at.desc()).limit(20).all():
            subject = p.subject
            notifications.append({
                'id':           f'paper_{p.id}',
                'type':         'paper',
                'title':        subject.title if subject else 'Copie',
                'message':      f'Votre copie a été corrigée — note : {p.score:.2f}/20' if p.score is not None else 'Votre copie a été corrigée',
                'created_at':   p.corrected_at.isoformat() if p.corrected_at else None,
                'paper_id':     p.id,
            })

        last_read = user.notifications_last_read
        if last_read and last_read.tzinfo is None:
            last_read = last_read.replace(tzinfo=timezone.utc)

        def _is_read(iso_str):
            if not last_read or not iso_str:
                return False
            try:
                dt = datetime.fromisoformat(iso_str.replace('Z', '+00:00'))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt <= last_read
            except Exception:
                return False

        for n in notifications:
            n['is_read'] = _is_read(n.get('created_at'))

        notifications.sort(key=lambda x: x['created_at'] or '', reverse=True)
        unread_count = sum(1 for n in notifications if not n['is_read'])
        return jsonify({
            'notifications': notifications,
            'count':         len(notifications),
            'unread_count':  unread_count,
        })
    except Exception as e:
        session.rollback()
        return jsonify({'notifications': [], 'count': 0, 'unread_count': 0, 'error': str(e)}), 500
    finally:
        session.close()


@notifications_bp.route('/api/notifications/mark-read', methods=['PUT'])
@paseto_required
def mark_notifications_read():
    user_id = get_current_user_id()
    session = get_session()
    try:
        user = session.query(User).get(user_id)
        if not user:
            return jsonify({'error': 'Utilisateur introuvable'}), 404
        user.notifications_last_read = utcnow()
        session.commit()
        return jsonify({'success': True})
    except Exception as e:
        session.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        session.close()


_POLL_BATCH = 50


@notifications_bp.route('/api/notifications/poll', methods=['GET'])
@paseto_required
@limiter.exempt
def notification_poll():
    """
    Draine la file Redis de l'utilisateur (`cei:notif:user:{id}`, liste
    alimentée par notif_bus) et renvoie immédiatement les événements en
    attente. Aucune connexion tenue : le client rappelle cet endpoint à
    intervalle court (cf. hooks/useNotificationPoll.ts).

    LRANGE + LTRIM dans une transaction Redis (MULTI/EXEC) = lecture-puis-
    purge atomique, ordre FIFO (le plus ancien d'abord).

    Retour :
      200 { has_events: bool, events: [ { type, title, message, ts, ... } ] }
      (`has_event` / `event` = 1er élément, pour un client long-poll d'une
       version précédente encore chargé pendant une bascule.)
    """
    user_id = get_current_user_id()
    key = f'cei:notif:user:{user_id}'
    events = []
    r = None
    try:
        r = _redis_lib.from_url(
            _REDIS_URL,
            decode_responses=True,
            socket_connect_timeout=2,
            socket_timeout=2,
        )
        pipe = r.pipeline(transaction=True)
        pipe.lrange(key, 0, _POLL_BATCH - 1)
        pipe.ltrim(key, _POLL_BATCH, -1)
        raw = pipe.execute()[0]
        for item in raw or []:
            try:
                events.append(json.loads(item))
            except Exception:
                pass
    except Exception as exc:
        _log.warning('notification_poll error user=%s: %s', user_id, exc)
    finally:
        try:
            if r:
                r.close()
        except Exception:
            pass

    return jsonify({
        'has_events': bool(events),
        'events':     events,
        'has_event':  bool(events),
        'event':      events[0] if events else None,
    })
