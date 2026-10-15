"""Blueprint Vagues d'examen : un même examen passé par groupes d'étudiants, à des
créneaux successifs, pour étaler la charge réseau (voir services/exam_waves.py)."""
from flask import Blueprint, request, jsonify

from auth_paseto import paseto_required, get_current_user_id
from models import get_session, User, UserRole, OnlineExam, ExamWave, ExamWaveStudent, ExamStatus
from services import exam_waves as W

exam_waves_bp = Blueprint('exam_waves', __name__)


def _parse_utc_naive(raw):
    from routes.exams import _parse_utc_naive as p
    return p(raw)


def _load(session, exam_id):
    """(examen, utilisateur, erreur) — réservé à l'admin et au professeur créateur."""
    user = session.query(User).filter_by(id=get_current_user_id()).first()
    exam = session.query(OnlineExam).filter_by(id=exam_id).first()
    if not exam:
        return None, user, (jsonify({'error': 'Examen non trouvé'}), 404)
    if not user or user.role not in (UserRole.PROFESSOR, UserRole.ADMIN):
        return None, user, (jsonify({'error': 'Accès non autorisé'}), 403)
    if user.role == UserRole.PROFESSOR and exam.created_by_id != user.id:
        return None, user, (jsonify({'error': 'Vous ne pouvez gérer que les vagues de vos propres examens'}), 403)
    return exam, user, None


def _rebalance_proctors(session, exam):
    """Les surveillants se répartissent les étudiants vague par vague (voir
    services/proctor_service.py) : à refaire dès que la composition des vagues change."""
    try:
        from services.proctor_service import rebalance_unstarted
        rebalance_unstarted(session, exam, force=True)
    except Exception as e:
        session.rollback()
        print(f"[vagues] répartition des surveillants non mise à jour: {e}")


def _editable(exam):
    if exam.status not in (ExamStatus.SCHEDULED, ExamStatus.ACTIVE, ExamStatus.DRAFT):
        return jsonify({'error': "Impossible de modifier les vagues d'un examen terminé"}), 400
    return None


@exam_waves_bp.route('/api/online_exams/<int:exam_id>/waves', methods=['GET'])
@paseto_required
def get_waves(exam_id):
    session = get_session()
    try:
        exam, _, err = _load(session, exam_id)
        if err:
            return err
        return jsonify(W.summary(session, exam))
    finally:
        session.close()


@exam_waves_bp.route('/api/online_exams/<int:exam_id>/waves/plan', methods=['POST'])
@paseto_required
def plan_exam_waves(exam_id):
    """Crée N vagues consécutives et répartit équitablement les étudiants inscrits.
    Corps : {count, first_start (ISO), window_minutes?, gap_minutes?, order?: alphabetique|aleatoire}"""
    session = get_session()
    try:
        exam, _, err = _load(session, exam_id)
        if err:
            return err
        bad = _editable(exam)
        if bad:
            return bad
        data = request.get_json(silent=True) or {}
        try:
            count = int(data.get('count'))
            first_start = _parse_utc_naive(data.get('first_start'))
        except (TypeError, ValueError):
            return jsonify({'error': "Nombre de vagues et heure de début de la première vague requis."}), 400
        try:
            W.plan_waves(session, exam, count, first_start,
                         window_minutes=data.get('window_minutes'),
                         gap_minutes=data.get('gap_minutes', 15),
                         order=data.get('order') or 'alphabetique')
        except W.WaveError as e:
            session.rollback()
            return jsonify({'error': str(e)}), 400
        session.commit()
        _rebalance_proctors(session, exam)
        try:
            from services.moodle_calendar import schedule_sync
            schedule_sync(exam.id)
        except Exception:
            pass
        return jsonify({'success': True, **W.summary(session, exam)}), 201
    except Exception as e:
        session.rollback()
        print(f"Erreur plan_exam_waves: {e}")
        return jsonify({'error': str(e)}), 500
    finally:
        session.close()


@exam_waves_bp.route('/api/online_exams/<int:exam_id>/waves/<int:wave_id>', methods=['PUT'])
@paseto_required
def update_exam_wave(exam_id, wave_id):
    """Renomme une vague ou change son créneau (start_time / end_time)."""
    session = get_session()
    try:
        exam, _, err = _load(session, exam_id)
        if err:
            return err
        bad = _editable(exam)
        if bad:
            return bad
        wave = session.query(ExamWave).filter_by(id=wave_id, exam_id=exam_id).first()
        if not wave:
            return jsonify({'error': 'Vague non trouvée'}), 404
        data = request.get_json(silent=True) or {}
        try:
            W.update_wave(session, exam, wave,
                          name=data.get('name'),
                          start_time=_parse_utc_naive(data['start_time']) if data.get('start_time') else None,
                          end_time=_parse_utc_naive(data['end_time']) if data.get('end_time') else None)
        except (W.WaveError, ValueError) as e:
            session.rollback()
            return jsonify({'error': str(e)}), 400
        session.commit()
        try:
            from services.moodle_calendar import schedule_sync
            schedule_sync(exam.id)
        except Exception:
            pass
        return jsonify({'success': True, **W.summary(session, exam)})
    finally:
        session.close()


@exam_waves_bp.route('/api/online_exams/<int:exam_id>/waves/<int:wave_id>/students', methods=['POST'])
@paseto_required
def move_wave_students(exam_id, wave_id):
    """Déplace des étudiants vers cette vague. Corps : {student_ids: [..]}"""
    session = get_session()
    try:
        exam, _, err = _load(session, exam_id)
        if err:
            return err
        bad = _editable(exam)
        if bad:
            return bad
        wave = session.query(ExamWave).filter_by(id=wave_id, exam_id=exam_id).first()
        if not wave:
            return jsonify({'error': 'Vague non trouvée'}), 404
        ids = (request.get_json(silent=True) or {}).get('student_ids') or []
        if not isinstance(ids, list) or not ids:
            return jsonify({'error': 'Liste d\'étudiants requise'}), 400
        eligible = set(W.eligible_student_ids(session, exam))
        unknown = [i for i in ids if int(i) not in eligible]
        if unknown:
            return jsonify({'error': f"{len(unknown)} étudiant(s) ne sont pas inscrits à cet examen."}), 400
        try:
            moved = W.move_students(session, exam, wave, ids)
        except W.WaveError as e:
            session.rollback()
            return jsonify({'error': str(e)}), 400
        session.commit()
        _rebalance_proctors(session, exam)
        return jsonify({'success': True, 'moved': moved, **W.summary(session, exam)})
    finally:
        session.close()


@exam_waves_bp.route('/api/online_exams/<int:exam_id>/waves', methods=['DELETE'])
@paseto_required
def delete_exam_waves(exam_id):
    """Supprime toutes les vagues (retour à une fenêtre unique)."""
    session = get_session()
    try:
        exam, _, err = _load(session, exam_id)
        if err:
            return err
        try:
            W.remove_waves(session, exam)
        except W.WaveError as e:
            session.rollback()
            return jsonify({'error': str(e)}), 400
        session.commit()
        _rebalance_proctors(session, exam)
        return jsonify({'success': True, **W.summary(session, exam)})
    finally:
        session.close()
