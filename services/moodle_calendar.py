"""
Dates des examens CEI dans le calendrier Moodle (phase 4 de la feuille de
route CEI–UNCHK).

Chaque examen CEI dont le sujet est rattaché à un EC ayant un cours Moodle
(même code) y devient un événement de cours, visible par tous les inscrits.
Créé à la création de l'examen, remplacé quand son titre ou son horaire
change, retiré à sa suppression. Tout se fait en arrière-plan et sans
jamais bloquer CEI : un Moodle injoignable laisse une erreur sur la ligne
MoodleExamEvent, rattrapée par le bouton « Publier les examens à venir » de
la page Moodle.

Moodle ne sait déplacer un événement que d'un jour à l'autre
(core_calendar_update_event_start_day) : un changement d'heure ou de durée
supprime donc l'ancien événement et en crée un nouveau.
"""
import calendar
import hashlib
import os
import threading
from datetime import datetime, timezone

from services import moodle_sync
from services.moodle_sync import MoodleError


def _app_url() -> str:
    return os.getenv('APP_URL', 'https://dev-cei.ddns.net').rstrip('/')


def _event_payload(exam, course_id: int) -> dict:
    # start_time/end_time sont stockés en UTC naïf (voir create_online_exam).
    start = calendar.timegm(exam.start_time.timetuple())
    duration = max(0, int((exam.end_time - exam.start_time).total_seconds()))
    link = f"{_app_url()}/exam/{exam.id}"
    return {
        'name': f"Examen CEI : {exam.title}"[:255],
        'description': (f"<p>Examen en ligne sur CEI (Centre d'Examen Intelligent) — durée {exam.duration_minutes} min.</p>"
                        f"<p>Accès le jour de l'examen : <a href=\"{link}\">{link}</a></p>"),
        'format': 1,
        'courseid': course_id,
        'eventtype': 'course',
        'timestart': start,
        'timeduration': duration,
        'visible': 1,
    }


def _signature(payload: dict) -> str:
    raw = f"{payload['courseid']}|{payload['name']}|{payload['timestart']}|{payload['timeduration']}|{payload['description']}"
    return hashlib.sha256(raw.encode()).hexdigest()


def _delete_event(session, row) -> None:
    """Retire l'événement Moodle d'une ligne (sans erreur s'il a déjà disparu)."""
    from models import MoodleInstance
    if not row.moodle_event_id or not row.instance_id:
        return
    inst = session.get(MoodleInstance, row.instance_id)
    if not inst:
        return
    try:
        moodle_sync.client_for(inst).call('core_calendar_delete_calendar_events',
                                          {'events': [{'eventid': row.moodle_event_id, 'repeat': 0}]})
    except MoodleError as e:
        # Événement supprimé à la main dans Moodle : rien à retirer.
        if 'nopermissions' not in str(e) and 'invalidrecord' not in str(e) and 'dmlmissingrecord' not in str(e):
            raise


def _target(session, exam):
    """(plateforme, client, payload, signature) de l'événement attendu pour
    cet examen, ou une raison de ne rien publier."""
    from models import ExamStatus, Subject, EC
    if not exam or exam.status == ExamStatus.DRAFT:
        return None, 'examen absent ou brouillon'
    subject = session.get(Subject, exam.subject_id)
    ec = session.get(EC, subject.ec_id) if subject and subject.ec_id else None
    if not ec:
        return None, 'sujet sans EC'
    found = moodle_sync.find_course_for_ec(session, ec.code)
    if not found:
        return None, f'aucun cours Moodle {ec.code}'
    inst, client, course = found
    payload = _event_payload(exam, course['id'])
    return (inst, client, payload, _signature(payload)), None


def sync_exam(session, exam_id: int, dry_run: bool = False) -> str:
    """Aligne l'événement Moodle d'un examen sur son état CEI. Renvoie
    'created', 'updated', 'unchanged', 'removed' ou 'skipped:<raison>'.
    Lève MoodleError si Moodle refuse (l'erreur est aussi notée en base)."""
    from models import OnlineExam, MoodleExamEvent
    row = session.query(MoodleExamEvent).filter_by(exam_id=exam_id).first()
    target, reason = _target(session, session.get(OnlineExam, exam_id))
    if not target:
        if row:
            return 'removed' if dry_run else remove_exam(session, exam_id)
        return f'skipped:{reason}'
    inst, client, payload, sig = target
    if row and row.moodle_event_id and row.signature == sig and row.instance_id == inst.id and not row.last_error:
        return 'unchanged'
    existed = bool(row and row.moodle_event_id)
    if dry_run:
        return 'updated' if existed else 'created'

    if not row:
        row = MoodleExamEvent(exam_id=exam_id)
        session.add(row)
    try:
        if existed:
            _delete_event(session, row)
            row.moodle_event_id = None
        res = client.call('core_calendar_create_calendar_events', {'events': [payload]})
        event = (res.get('events') or [{}])[0] if isinstance(res, dict) else {}
        if not event.get('id'):
            warnings = res.get('warnings') if isinstance(res, dict) else None
            raise MoodleError(f"Événement non créé : {warnings or res}")
        row.instance_id, row.moodle_course_id, row.moodle_event_id = inst.id, payload['courseid'], event['id']
        row.signature, row.last_error = sig, None
        row.synced_at = datetime.now(timezone.utc)
        session.commit()
        return 'updated' if existed else 'created'
    except MoodleError as e:
        row.last_error = str(e)[:1000]
        session.commit()
        raise


def remove_exam(session, exam_id: int) -> str:
    from models import MoodleExamEvent
    row = session.query(MoodleExamEvent).filter_by(exam_id=exam_id).first()
    if not row:
        return 'skipped:aucun événement'
    try:
        _delete_event(session, row)
    except MoodleError as e:
        row.last_error = f"Suppression impossible : {e}"[:1000]
        session.commit()
        raise
    session.delete(row)
    session.commit()
    return 'removed'


def _in_background(fn, exam_ids) -> None:
    if not moodle_sync.is_enabled() or not exam_ids:
        return

    def run():
        from models import get_session
        session = get_session()
        try:
            for exam_id in exam_ids:
                try:
                    fn(session, exam_id)
                except Exception as e:  # jamais bloquant pour CEI
                    session.rollback()
                    print(f"[moodle_calendar] examen {exam_id} : {e}")
        finally:
            session.close()

    threading.Thread(target=run, daemon=True, name='moodle-calendar').start()


def schedule_sync(*exam_ids: int) -> None:
    """À appeler APRÈS le commit qui crée ou modifie un examen."""
    _in_background(sync_exam, [i for i in exam_ids if i])


def schedule_remove(*exam_ids: int) -> None:
    """À appeler après la suppression d'un examen (la ligne MoodleExamEvent,
    elle, survit jusqu'au retrait de l'événement)."""
    _in_background(remove_exam, [i for i in exam_ids if i])


def sync_upcoming(session, dry_run: bool = True) -> dict:
    """Rattrapage depuis la page Moodle : publie ou met à jour les examens
    planifiés ou en cours qui ne sont pas encore terminés."""
    from models import OnlineExam, ExamStatus
    now = datetime.utcnow()
    exams = (session.query(OnlineExam)
             .filter(OnlineExam.status.in_([ExamStatus.SCHEDULED, ExamStatus.ACTIVE]), OnlineExam.end_time > now)
             .order_by(OnlineExam.start_time).all())
    report = {'created': [], 'updated': [], 'unchanged': [], 'removed': [], 'skipped': [], 'errors': []}
    for exam in exams:
        label = f"{exam.title} ({exam.start_time.strftime('%d/%m/%Y %H:%M')} UTC)"
        try:
            outcome = sync_exam(session, exam.id, dry_run=dry_run)
        except MoodleError as e:
            report['errors'].append({'exam': label, 'error': str(e)})
            continue
        if outcome.startswith('skipped:'):
            report['skipped'].append({'exam': label, 'reason': outcome.split(':', 1)[1]})
        else:
            report[outcome].append(label)
    return report
