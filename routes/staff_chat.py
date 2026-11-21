"""Messagerie du personnel d'un examen : voir services/staff_chat.py.

  GET  /api/online_exams/<id>/staff_chat?since_id=N   messages visibles par moi + membres de l'équipe
  POST /api/online_exams/<id>/staff_chat              {body, to?}  envoie un message à toute l'équipe ou à une personne
"""
from flask import Blueprint, jsonify, request

from auth_paseto import paseto_required, get_current_user_id
from extensions import limiter
from models import get_session, User, OnlineExam
from services import staff_chat as SC

staff_chat_bp = Blueprint('staff_chat', __name__)


def _ctx(session, exam_id):
    user = session.get(User, get_current_user_id())
    exam = session.get(OnlineExam, exam_id)
    if not exam:
        return None, None, (jsonify({'error': 'Examen introuvable'}), 404)
    if not SC.may_chat(session, user, exam_id):
        return None, None, (jsonify({'error': "Réservé au personnel de cet examen"}), 403)
    return user, exam, None


@staff_chat_bp.route('/api/online_exams/<int:exam_id>/staff_chat', methods=['GET'])
@paseto_required
@limiter.exempt   # 21/11/2026 : relève périodique d'un panneau ouvert en permanence ; derrière une même adresse (salle d'examen), la limite par IP renvoyait des 429 (42 le 17/11) → erreurs et « discrédit ». L'envoi (POST) reste limité.
def staff_chat_list(exam_id):
    session = get_session()
    try:
        user, exam, err = _ctx(session, exam_id)
        if err:
            return err
        try:
            since = max(0, int(request.args.get('since_id') or 0))
        except ValueError:
            since = 0
        out = {'messages': SC.fetch(session, exam_id, user.id, since)}
        if not since:
            out['participants'] = SC.participants(session, exam_id)
            out['me'] = {'id': user.id, 'name': user.full_name, 'role': user.role.value}
        return jsonify(out)
    finally:
        session.close()


@staff_chat_bp.route('/api/online_exams/<int:exam_id>/staff_chat', methods=['POST'])
@paseto_required
@limiter.limit('30 per minute')
def staff_chat_send(exam_id):
    session = get_session()
    try:
        user, exam, err = _ctx(session, exam_id)
        if err:
            return err
        data = request.get_json(silent=True) or {}
        to = data.get('to')
        try:
            to_id = int(to) if to not in (None, '', 0) else None
            msg = SC.send(session, exam, user, data.get('body'), to_id)
        except (TypeError, ValueError) as e:
            return jsonify({'error': str(e)}), 400
        return jsonify({'success': True, 'message': msg})
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
