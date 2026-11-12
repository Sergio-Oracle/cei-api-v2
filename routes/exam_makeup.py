"""Blueprint Rattrapage : une session à part pour les étudiants qui n'ont pas pu composer (voir services/exam_makeup.py).
Distinct des vagues (routes/exam_waves.py) : les deux mécanismes ne se touchent pas."""
from flask import Blueprint, request, jsonify

from auth_paseto import paseto_required, get_current_user_id
from models import get_session, User, UserRole, OnlineExam
from services import exam_makeup as M
from services.ec_rights import can_manage_exam

exam_makeup_bp = Blueprint('exam_makeup', __name__)


def _parse_utc_naive(raw):
    from routes.exams import _parse_utc_naive as p
    return p(raw)


def _load(session, exam_id):
    """(examen, utilisateur, erreur) — réservé à l'admin, au responsable d'examen et au professeur qui gère l'examen."""
    user = session.query(User).filter_by(id=get_current_user_id()).first()
    exam = session.query(OnlineExam).filter_by(id=exam_id).first()
    if not exam:
        return None, user, (jsonify({'error': 'Examen non trouvé'}), 404)
    if not user or user.role not in (UserRole.PROFESSOR, UserRole.ADMIN, UserRole.RESPONSABLE_EXAMEN):
        return None, user, (jsonify({'error': 'Accès non autorisé'}), 403)
    if user.role == UserRole.PROFESSOR and not can_manage_exam(session, exam, user.id):
        return None, user, (jsonify({'error': 'Vous ne pouvez gérer que le rattrapage de vos propres examens'}), 403)
    return exam, user, None


@exam_makeup_bp.route('/api/online_exams/<int:exam_id>/makeup/candidates', methods=['GET'])
@paseto_required
def makeup_candidates(exam_id):
    """Étudiants pouvant être admis au rattrapage. Par défaut ceux qui n'ont pas composé ; `include_composed=1` : tous les inscrits.
    Paramètres : search, include_composed, limit (≤ 500), offset."""
    session = get_session()
    try:
        exam, _, err = _load(session, exam_id)
        if err:
            return err
        if exam.makeup_of_id:
            exam = session.get(OnlineExam, exam.makeup_of_id)        # un rattrapage se complète depuis les inscrits de l'examen d'origine
        return jsonify(M.candidates(
            session, exam, request.args.get('search') or '', request.args.get('include_composed') in ('1', 'true'),
            request.args.get('limit', 200, type=int), request.args.get('offset', 0, type=int)))
    finally:
        session.close()


@exam_makeup_bp.route('/api/online_exams/<int:exam_id>/makeups', methods=['GET'])
@paseto_required
def list_makeups(exam_id):
    """Sessions de rattrapage de cet examen : [{id, title, start_time, end_time, status, students, participants}]."""
    session = get_session()
    try:
        exam, _, err = _load(session, exam_id)
        if err:
            return err
        return jsonify({'makeups': M.list_makeups(session, exam), 'makeup_of_id': exam.makeup_of_id})
    finally:
        session.close()


@exam_makeup_bp.route('/api/online_exams/<int:exam_id>/makeups', methods=['POST'])
@paseto_required
def create_makeup(exam_id):
    """Organise un rattrapage (examen à part). Corps : {student_ids: [..], start_time (ISO), end_time (ISO)}.
    L'examen d'origine n'est pas modifié ; seuls les étudiants choisis accèdent au rattrapage."""
    session = get_session()
    try:
        exam, user, err = _load(session, exam_id)
        if err:
            return err
        data = request.get_json(silent=True) or {}
        try:
            start = _parse_utc_naive(data.get('start_time'))
            end = _parse_utc_naive(data.get('end_time'))
        except (TypeError, ValueError):
            return jsonify({'error': "Début et fin du rattrapage requis."}), 400
        try:
            m = M.create_makeup(session, exam, data.get('student_ids') or [], start, end, actor_id=user.id)
        except M.MakeupError as e:
            session.rollback()
            return jsonify({'error': str(e)}), 400
        session.commit()
        out = {'success': True, 'exam': m.to_dict(), 'students': len(data.get('student_ids') or [])}
        ec_id = m.subject.ec_id if m.subject else None
        to_notify = []
        if ec_id:                      # surveillants : comme à la création de n'importe quel examen de cet EC
            try:
                from services.proctor_service import sync_ec_proctors
                to_notify = sync_ec_proctors(session, ec_id) or []
            except Exception as e:
                session.rollback()
                print(f"[rattrapage] surveillants non mis à jour: {e}")
        ids = [int(i) for i in (data.get('student_ids') or [])]
        mid = m.id
        session.close()
        M.notify_students(mid, ids)
        for n in to_notify:
            try:
                from notif_bus import notify_user
                notify_user(n.get('user_id'), n.get('event'), n.get('title'), n.get('message'), priority=n.get('priority'), tags=n.get('tags'))
            except Exception:
                pass
        return jsonify(out), 201
    except Exception as e:
        session.rollback()
        print(f"Erreur create_makeup: {e}")
        return jsonify({'error': str(e)}), 500
    finally:
        try:
            session.close()
        except Exception:
            pass


@exam_makeup_bp.route('/api/online_exams/<int:exam_id>/makeup_students', methods=['POST'])
@paseto_required
def add_makeup_students(exam_id):
    """Ajoute des étudiants à un rattrapage existant (exam_id = le rattrapage). Corps : {student_ids: [..]}."""
    session = get_session()
    try:
        exam, user, err = _load(session, exam_id)
        if err:
            return err
        try:
            added = M.add_students(session, exam, (request.get_json(silent=True) or {}).get('student_ids') or [], actor_id=user.id)
        except M.MakeupError as e:
            session.rollback()
            return jsonify({'error': str(e)}), 400
        session.commit()
        M.notify_students(exam.id, added)
        return jsonify({'success': True, 'added': len(added)})
    except Exception as e:
        session.rollback()
        print(f"Erreur add_makeup_students: {e}")
        return jsonify({'error': str(e)}), 500
    finally:
        session.close()
