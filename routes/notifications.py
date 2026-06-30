"""
Blueprint Notifications.

GET  /api/notifications          — corrections récentes (étudiant)
PUT  /api/notifications/mark-read — marquer toutes comme lues
"""
from datetime import datetime, timezone
from flask import Blueprint, jsonify
from auth_paseto import paseto_required, get_current_user_id
from helpers     import utcnow
from models      import (
    get_session, User, UserRole,
    StudentPaper, ExamAttempt,
)

notifications_bp = Blueprint('notifications', __name__)


@notifications_bp.route('/api/notifications', methods=['GET'])
@paseto_required
def get_notifications():
    user_id = get_current_user_id()
    session = get_session()
    try:
        user = session.query(User).get(user_id)
        if not user or user.role != UserRole.STUDENT:
            return jsonify({'notifications': [], 'count': 0, 'unread_count': 0})

        notifications = []

        for att in session.query(ExamAttempt).filter(
            ExamAttempt.student_id == user_id,
            ExamAttempt.corrected_at != None,
            ExamAttempt.score != None,
        ).order_by(ExamAttempt.corrected_at.desc()).limit(20).all():
            exam = att.exam
            notifications.append({
                'id':           f'attempt_{att.id}',
                'type':         'online_exam',
                'title':        exam.title if exam else 'Examen en ligne',
                'message':      f'Votre copie a été corrigée — note : {att.score:.2f}/20' if att.score is not None else 'Votre copie a été corrigée',
                'corrected_at': att.corrected_at.isoformat() if att.corrected_at else None,
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
                'corrected_at': p.corrected_at.isoformat() if p.corrected_at else None,
                'paper_id':     p.id,
            })

        last_read = user.notifications_last_read
        if last_read and last_read.tzinfo is None:
            last_read = last_read.replace(tzinfo=timezone.utc)

        def _is_read(iso_str):
            if not last_read or not iso_str:
                return False
            try:
                dt = datetime.fromisoformat(iso_str)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt <= last_read
            except Exception:
                return False

        for n in notifications:
            n['is_read'] = _is_read(n.get('corrected_at'))

        notifications.sort(key=lambda x: x['corrected_at'] or '', reverse=True)
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
