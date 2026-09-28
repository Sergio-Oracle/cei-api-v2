"""
Synchronisation automatique Moodle → CEI (phase 6).

Deux déclencheurs, mêmes traitements que le bouton « Appliquer » :
  1. Webhook : Moodle poste chaque événement (cours créé ou modifié,
     inscription, rôle, catégorie…) sur /api/moodle/webhook/<plateforme>.
     L'événement est mis en file (Redis) et regroupé par cours : une
     inscription en masse de 3 000 étudiants ne donne qu'UNE synchronisation
     du cours, lancée 60 s après le dernier événement reçu pour lui.
  2. Filet de sécurité programmé (si activé pour la plateforme) : chaque heure
     maquette + activité CEI + nouveaux cours ; chaque nuit (01 h UTC, heure
     de Dakar) synchronisation complète de tous les cours.

Le planificateur tourne dans chaque processus gunicorn (démarré à la
première requête, gunicorn chargeant l'application avant de dupliquer ses
processus), mais un verrou Redis fait qu'un seul passe à la fois.
"""
import json
import threading
import time
from datetime import datetime, timezone, timedelta

from cache import _get_client, cache_set_nx, cache_delete
from services import moodle_sync
from services.moodle_sync import MoodleError

TICK_SECONDS = 30
DEBOUNCE_SECONDS = 60
MAX_PER_TICK = 5
LIGHT_EVERY = timedelta(minutes=55)
FULL_HOUR_UTC = 1

# Événements Moodle → travail à faire. Les autres sont ignorés (acceptés, sans effet).
COURSE_EVENTS = {
    'course_created', 'course_updated', 'course_restored', 'course_content_deleted',
    'user_enrolment_created', 'user_enrolment_updated', 'user_enrolment_deleted',
    'role_assigned', 'role_unassigned', 'enrol_instance_created', 'enrol_instance_updated',
    'group_member_added', 'course_module_created',
}
STRUCTURE_EVENTS = {'course_category_created', 'course_category_updated', 'course_category_deleted'}

_started = False
_start_lock = threading.Lock()


def _queue_key(instance_id: int) -> str:
    return f"cei:moodle:queue:{instance_id}"


def _short(event_name: str) -> str:
    return (event_name or '').rstrip('\\').split('\\')[-1]


def enqueue_events(instance_id: int, events: list) -> dict:
    """Met en file les événements reconnus. Chaque nouvel événement d'un cours
    repousse son traitement de DEBOUNCE_SECONDS (regroupement)."""
    r = _get_client()
    queued, ignored = [], 0
    for ev in events:
        name = _short(ev.get('eventname') or ev.get('event') or '')
        course_id = ev.get('courseid')
        if name in STRUCTURE_EVENTS:
            item = 'structure'
        elif name in COURSE_EVENTS and course_id and int(course_id) > 1:   # 1 = page d'accueil du site
            item = f"course:{int(course_id)}"
        else:
            ignored += 1
            continue
        queued.append(item)
    if queued and r is not None:
        due = time.time() + DEBOUNCE_SECONDS
        r.zadd(_queue_key(instance_id), {item: due for item in set(queued)})
    return {'queued': len(set(queued)), 'ignored': ignored}


def _run_course(session, inst, client, course_id: int) -> dict:
    from models import EC
    from services.provisioning import sync_course
    from services.moodle_structure import build_structure
    from services.moodle_activity import ensure_activity
    found = client.call('core_course_get_courses_by_field', {'field': 'id', 'value': course_id})
    courses = found.get('courses', []) if isinstance(found, dict) else []
    if not courses:
        return {'course_id': course_id, 'skipped': 'cours introuvable (supprimé ?)'}
    course = courses[0]
    ec = session.query(EC).filter_by(code=course['shortname']).first()
    if not ec:
        # Nouveau cours : sa place dans la maquette vient des catégories Moodle.
        build_structure(session, inst, client, dry_run=False)
        ec = session.query(EC).filter_by(code=course['shortname']).first()
    out = {'course': course['shortname']}
    if ec:
        rep = sync_course(session, ec, client, course, dry_run=False)
        out['students_created'] = rep['students']['created']
        out['enrollments_added'] = rep['students']['enrollments_added']
        out['teachers_assigned'] = rep['teachers']['assignments_added']
    else:
        out['skipped'] = 'hors maquette'
    out['activity'] = ensure_activity(inst, client, course, dry_run=False)['status']
    return out


def process_queue(session, inst) -> list:
    r = _get_client()
    if r is None:
        return []
    due = r.zrangebyscore(_queue_key(inst.id), 0, time.time(), start=0, num=MAX_PER_TICK)
    if not due:
        return []
    client = moodle_sync.client_for(inst)
    results = []
    for raw in due:
        item = raw.decode() if isinstance(raw, bytes) else raw
        r.zrem(_queue_key(inst.id), item)
        try:
            if item == 'structure':
                from services.moodle_structure import build_structure
                rep = build_structure(session, inst, client, dry_run=False)
                results.append({'structure': {k: len(v) for k, v in rep.items() if isinstance(v, list)}})
            else:
                results.append(_run_course(session, inst, client, int(item.split(':', 1)[1])))
        except Exception as e:  # une erreur n'arrête pas la file
            session.rollback()
            results.append({'item': item, 'error': str(e)[:300]})
    _record(session, inst, 'webhook', results)
    return results


def run_pass(session, inst, full: bool) -> dict:
    """Passe complète (nuit) ou légère (heure) pour une plateforme."""
    from models import EC
    from services.moodle_structure import build_structure
    from services.moodle_activity import ensure_activity
    from services.provisioning import sync_course
    client = moodle_sync.client_for(inst)
    report = {'kind': 'full' if full else 'light', 'errors': []}
    structure = build_structure(session, inst, client, dry_run=False)
    report['structure'] = {k: len(v) for k, v in structure.items() if isinstance(v, list)}
    ecs = {e.code: e for e in session.query(EC).all()}
    courses = [c for c in client.list_courses() if c['shortname'] in ecs]
    installed = synced = 0
    for course in courses:
        try:
            if ensure_activity(inst, client, course, dry_run=False)['status'] == 'installed':
                installed += 1
            if full:
                sync_course(session, ecs[course['shortname']], client, course, dry_run=False)
                synced += 1
        except Exception as e:
            session.rollback()
            report['errors'].append(f"{course['shortname']} : {str(e)[:200]}")
    if not full:
        # Enseignants : la carte (cache 7 j) est rafraîchie à chaque passe légère.
        moodle_sync.refresh_teacher_map(session)
    report.update({'courses': len(courses), 'activities_installed': installed, 'courses_synced': synced})
    _record(session, inst, report['kind'], report)
    now = datetime.now(timezone.utc)
    inst.auto_sync_last_at = now
    if full:
        inst.auto_sync_last_full_at = now
    session.commit()
    return report


def _record(session, inst, kind: str, detail) -> None:
    inst.auto_sync_last_report = json.dumps({'at': datetime.now(timezone.utc).isoformat(), 'kind': kind,
                                             'detail': detail}, ensure_ascii=False, default=str)[:20000]
    session.commit()


def _due_pass(inst, now: datetime):
    if not inst.auto_sync_enabled:
        return None
    last_full = inst.auto_sync_last_full_at
    if now.hour == FULL_HOUR_UTC and (not last_full or last_full.date() < now.date()):
        return 'full'
    if not inst.auto_sync_last_at or now - inst.auto_sync_last_at >= LIGHT_EVERY:
        return 'light'
    return None


def run_in_background(instance_id: int, full: bool) -> bool:
    """Lance une passe tout de suite (bouton de la page Moodle). False si une
    passe tourne déjà pour cette plateforme."""
    if not cache_set_nx(f"cei:moodle:job:{instance_id}", 3600):
        return False

    def job():
        from models import get_session, MoodleInstance
        session = get_session()
        try:
            run_pass(session, session.get(MoodleInstance, instance_id), full)
        except Exception as e:
            print(f"[moodle_auto] passe {instance_id} : {e}")
        finally:
            session.close()
            cache_delete(f"cei:moodle:job:{instance_id}")

    threading.Thread(target=job, daemon=True, name='moodle-auto-pass').start()
    return True


def _tick() -> None:
    from models import get_session, MoodleInstance
    if not cache_set_nx('cei:moodle:tick', TICK_SECONDS - 2):
        return  # un autre processus s'en charge
    session = get_session()
    try:
        now = datetime.now(timezone.utc)
        for inst in session.query(MoodleInstance).filter_by(is_active=True).all():
            try:
                process_queue(session, inst)
            except MoodleError as e:
                print(f"[moodle_auto] file {inst.name} : {e}")
            kind = _due_pass(inst, now)
            if kind:
                run_in_background(inst.id, full=(kind == 'full'))
    finally:
        session.close()


def start_scheduler() -> None:
    """Idempotent, un fil par processus."""
    global _started
    if not moodle_sync.is_enabled():
        return
    with _start_lock:
        if _started:
            return
        _started = True

    def loop():
        time.sleep(10)
        while True:
            try:
                _tick()
            except Exception as e:
                print(f"[moodle_auto] tick : {e}")
            time.sleep(TICK_SECONDS)

    threading.Thread(target=loop, daemon=True, name='moodle-auto').start()
