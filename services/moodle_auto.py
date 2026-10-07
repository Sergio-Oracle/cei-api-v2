"""
Synchronisation automatique Moodle → CEI, sans rien installer sur Moodle.

Moodle ne sait pas prévenir un service extérieur sans extension : c'est donc
CEI qui surveille Moodle, par des lectures légères (mesurées sur la préprod
le 29/09) et synchronise tout de suite ce qui a changé :
  - toutes les minutes : liste des cours (0,8 s) et des catégories (0,5 s) —
    nouveau cours, cours modifié, nouvelle catégorie ;
  - toutes les 5 minutes : enseignants de tous les cours (un appel, ~4 s) —
    tuteur ajouté ou retiré ;
  - en continu, par rotation : « empreinte » des inscrits de chaque cours
    (identifiants seuls, 3,3 s pour 3 357 inscrits), quelques cours par
    passage, chaque cours revu toutes les ~10 minutes — inscription ou
    désinscription ;
  - chaque nuit (1 h UTC, heure de Dakar) : synchronisation complète.
Un changement détecté met le cours en file (Redis) ; une salve de changements
sur un même cours ne donne qu'une synchronisation (regroupement de 60 s).
La synchronisation elle-même (services/provisioning.sync_course) ajoute, met
à jour et retire, avec ses garde-fous.

Si l'UNCHK installe un jour une extension de webhooks sur Moodle, les
événements reçus sur /api/moodle/webhook/<plateforme> alimentent la même
file (enqueue_events) : la réaction devient quasi immédiate.

Tout tourne dans le service dédié cei-moodle-sync (moodle_worker/run.py),
un seul processus, jamais dans l'API web. Rythme choisi pour ménager Moodle
(mesuré le 29/09 : lire les inscrits des 114 cours prend 153 s au total, un
appel à la fois) : les inscrits sont relus sur un cycle de 15 minutes, soit
4 cours par passage de 30 s — Moodle est occupé par CEI ~15 % du temps, par
une seule requête légère à la fois. Seules exceptions, dans l'API web car
ponctuelles et liées à une action : mise à jour d'une personne à sa
connexion, d'un cours à la création/activation d'un examen.
"""
import hashlib
import json
import threading
import time
from datetime import datetime, timezone

from cache import _get_client, cache_get, cache_set, cache_set_nx, cache_delete
from services import moodle_sync
from services.moodle_sync import MoodleError

TICK_SECONDS = 30
DEBOUNCE_SECONDS = 60
MAX_PER_TICK = 5              # cours synchronisés par passage (file)
COURSES_EVERY = 60            # s — liste des cours et catégories
TEACHERS_EVERY = 300          # s — enseignants
ENROL_CYCLE_SECONDS = 900     # chaque cours relu toutes les 15 min
ENROL_BUDGET_SECONDS = 15     # plafond de temps de lecture par passage
FULL_HOUR_UTC = 1

COURSE_EVENTS = {
    'course_created', 'course_updated', 'course_restored', 'course_content_deleted',
    'user_enrolment_created', 'user_enrolment_updated', 'user_enrolment_deleted',
    'role_assigned', 'role_unassigned', 'enrol_instance_created', 'enrol_instance_updated',
}
STRUCTURE_EVENTS = {'course_category_created', 'course_category_updated', 'course_category_deleted'}



def _k(inst_id: int, what: str) -> str:
    return f"cei:moodle:{what}:{inst_id}"


def _enqueue(inst_id: int, items) -> int:
    items = set(items)
    r = _get_client()
    if items and r is not None:
        due = time.time() + DEBOUNCE_SECONDS
        r.zadd(_k(inst_id, 'queue'), {item: due for item in items})
    return len(items)


def enqueue_events(instance_id: int, events: list) -> dict:
    """Événements poussés par un webhook Moodle (facultatif)."""
    items, ignored = [], 0
    for ev in events:
        name = (ev.get('eventname') or ev.get('event') or '').rstrip('\\').split('\\')[-1]
        course_id = ev.get('courseid')
        if name in STRUCTURE_EVENTS:
            items.append('structure')
        elif name in COURSE_EVENTS and course_id and int(course_id) > 1:   # 1 = page d'accueil du site
            items.append(f"course:{int(course_id)}")
        else:
            ignored += 1
    return {'queued': _enqueue(instance_id, items), 'ignored': ignored}


# ── Détection des changements ──────────────────────────────────────────────

def _digest(values) -> str:
    return hashlib.sha1(json.dumps(values, sort_keys=True, default=str).encode()).hexdigest()


def watch_courses(inst, client) -> list:
    """Cours et catégories : ce qui est nouveau ou modifié depuis le dernier regard."""
    courses = client.list_courses()
    current = {str(c['id']): _digest([c['shortname'], c.get('fullname'), c.get('categoryid'), c.get('timemodified')])
               for c in courses}
    cats = client.call('core_course_get_categories')
    cat_digest = _digest(sorted((c['id'], c['name'], c.get('parent'), c.get('timemodified')) for c in cats))
    before = cache_get(_k(inst.id, 'watch:courses'))
    before_cats = cache_get(_k(inst.id, 'watch:cats'))
    cache_set(_k(inst.id, 'watch:courses'), current, ttl=7 * 86400)
    cache_set(_k(inst.id, 'watch:cats'), cat_digest, ttl=7 * 86400)
    if before is None:
        return []          # premier regard : référence, pas de changement
    items = [f"course:{cid}" for cid, d in current.items() if before.get(cid) != d]
    if before_cats is not None and before_cats != cat_digest:
        items.append('structure')
    return items


def watch_teachers(inst, session) -> list:
    """Enseignants : cours dont la liste des enseignants a changé."""
    moodle_sync.refresh_teacher_map(session)
    by_course = {}
    for email, codes in (moodle_sync.teacher_map() or {}).items():
        items = codes.items() if isinstance(codes, dict) else ((c, '') for c in codes)
        for code, kind in items:   # le rôle compte : un tuteur devenu éditeur relance le cours
            by_course.setdefault(code, []).append(f'{email}:{kind}')
    current = {code: _digest(sorted(emails)) for code, emails in by_course.items()}
    before = cache_get(_k(inst.id, 'watch:teachers'))
    cache_set(_k(inst.id, 'watch:teachers'), current, ttl=7 * 86400)
    if before is None:
        return []
    changed = {code for code in set(current) | set(before) if current.get(code) != before.get(code)}
    if not changed:
        return []
    ids = {c['shortname']: c['id'] for c in moodle_sync.client_for(inst).list_courses()}
    return [f"course:{ids[code]}" for code in changed if code in ids]


def watch_enrolments(inst, client, session) -> list:
    """Empreinte des inscrits, cours par cours, à tour de rôle : juste assez
    de cours par passage pour que chacun soit relu en ENROL_CYCLE_SECONDS
    (114 cours → 4 par passage), sans jamais dépasser ENROL_BUDGET_SECONDS."""
    from models import EC
    codes = {c for (c,) in session.query(EC.code).all()}
    courses = sorted((c for c in client.list_courses() if c['shortname'] in codes), key=lambda c: c['id'])
    if not courses:
        return []
    r = _get_client()
    pos = int(cache_get(_k(inst.id, 'watch:enrol_pos')) or 0) % len(courses)
    prints = cache_get(_k(inst.id, 'watch:enrol')) or {}
    items, start = [], time.monotonic()
    checked = 0
    quota = max(1, -(-len(courses) * TICK_SECONDS // ENROL_CYCLE_SECONDS))   # arrondi supérieur
    while checked < min(quota, len(courses)) and time.monotonic() - start < ENROL_BUDGET_SECONDS:
        course = courses[(pos + checked) % len(courses)]
        checked += 1
        users = client.call('core_enrol_get_enrolled_users', {'courseid': course['id'], 'options': [
            {'name': 'userfields', 'value': 'id'}, {'name': 'onlyactive', 'value': 1}]}, timeout=60)
        digest = _digest(sorted(u['id'] for u in users))
        key = str(course['id'])
        if key in prints and prints[key] != digest:
            items.append(f"course:{course['id']}")
        prints[key] = digest
    cache_set(_k(inst.id, 'watch:enrol'), prints, ttl=7 * 86400)
    cache_set(_k(inst.id, 'watch:enrol_pos'), (pos + checked) % len(courses), ttl=7 * 86400)
    return items


# ── Traitement ─────────────────────────────────────────────────────────────

def _run_course(session, inst, client, course_id: int) -> dict:
    from models import EC
    from services.provisioning import sync_course
    from services.moodle_structure import build_structure
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
    if not ec:
        return {'course': course['shortname'], 'skipped': 'hors maquette'}
    rep = sync_course(session, ec, client, course, dry_run=False)
    return {'course': course['shortname'],
            'students_created': rep['students']['created'], 'enrollments_added': rep['students']['enrollments_added'],
            'enrollments_removed': rep['students']['enrollments_removed'],
            'teachers_assigned': rep['teachers']['assignments_added'],
            'teachers_removed': rep['teachers']['assignments_removed'],
            'names_updated': rep['students']['names_updated'], 'formation_changed': rep['students']['formation_changed'],
            'removal_suspended': rep['students']['removal_suspended']}


def process_queue(session, inst, client) -> list:
    r = _get_client()
    if r is None:
        return []
    due = r.zrangebyscore(_k(inst.id, 'queue'), 0, time.time(), start=0, num=MAX_PER_TICK)
    results = []
    for item in due:
        r.zrem(_k(inst.id, 'queue'), item)
        try:
            if item == 'structure':
                from services.moodle_structure import build_structure
                rep = build_structure(session, inst, client, dry_run=False)
                results.append({'structure': {k: len(v) for k, v in rep.items() if isinstance(v, list)}})
            else:
                results.append(_run_course(session, inst, client, int(item.split(':', 1)[1])))
        except Exception as e:   # une erreur n'arrête pas la file
            session.rollback()
            results.append({'item': item, 'error': str(e)[:300]})
    if results:
        _record(session, inst, 'changements', results)
    return results


def sync_course_now(session, ec_code: str) -> dict | None:
    """Synchronisation immédiate d'un cours (création ou activation d'un
    examen : la liste des inscrits doit refléter Moodle à cet instant)."""
    found = moodle_sync.find_course_for_ec(session, ec_code)
    if not found:
        return None
    inst, client, course = found
    return _run_course(session, inst, client, course['id'])


def schedule_course_sync(ec_code: str | None) -> None:
    """Version arrière-plan de sync_course_now — jamais bloquante."""
    if not ec_code or not moodle_sync.is_enabled():
        return

    def run():
        from models import get_session
        session = get_session()
        try:
            sync_course_now(session, ec_code)
        except Exception as e:
            print(f"[moodle_auto] synchro immédiate {ec_code} : {e}")
        finally:
            session.close()

    threading.Thread(target=run, daemon=True, name='moodle-course-now').start()


def run_full(session, inst) -> dict:
    """Passe complète (nuit, ou bouton) : maquette puis tous les cours."""
    from models import EC
    from services.moodle_structure import build_structure
    client = moodle_sync.client_for(inst)
    report = {'errors': []}
    structure = build_structure(session, inst, client, dry_run=False)
    report['structure'] = {k: len(v) for k, v in structure.items() if isinstance(v, list)}
    codes = {c for (c,) in session.query(EC.code).all()}
    synced = 0
    for course in client.list_courses():
        if course['shortname'] not in codes:
            continue
        try:
            _run_course(session, inst, client, course['id'])
            synced += 1
        except Exception as e:
            session.rollback()
            report['errors'].append(f"{course['shortname']} : {str(e)[:200]}")
    report['courses_synced'] = synced
    _record(session, inst, 'complète', report)
    inst.auto_sync_last_full_at = datetime.now(timezone.utc)
    session.commit()
    return report


def _record(session, inst, kind: str, detail) -> None:
    inst.auto_sync_last_report = json.dumps({'at': datetime.now(timezone.utc).isoformat(), 'kind': kind,
                                             'detail': detail}, ensure_ascii=False, default=str)[:20000]
    inst.auto_sync_last_at = datetime.now(timezone.utc)
    session.commit()


def request_full(instance_id: int) -> bool:
    """Demande (bouton de la page Moodle) une synchronisation complète, faite
    par le service cei-moodle-sync au passage suivant. False si une passe
    complète est déjà demandée ou en cours."""
    if cache_get(_k(instance_id, 'job')):
        return False
    return cache_set_nx(_k(instance_id, 'full_request'), 3600)


def _run_full_guarded(session, inst) -> None:
    if not cache_set_nx(_k(inst.id, 'job'), 3 * 3600):
        return
    try:
        run_full(session, inst)
    except Exception as e:
        session.rollback()
        print(f"[moodle_auto] synchronisation complète {inst.name} : {e}")
    finally:
        cache_delete(_k(inst.id, 'job'))
        cache_delete(_k(inst.id, 'full_request'))


def _every(inst_id: int, what: str, seconds: int) -> bool:
    """Vrai au plus une fois toutes les `seconds` (tous processus confondus)."""
    return cache_set_nx(_k(inst_id, f'every:{what}'), seconds - 5)


def _tick() -> None:
    from models import get_session, MoodleInstance
    # Verrou tenu pendant tout le passage (qui peut durer plus de 30 s quand
    # la file contient plusieurs cours), libéré à la fin ; 15 min au plus si
    # le processus meurt en route.
    if not cache_set_nx('cei:moodle:tick', 900):
        return   # un autre processus s'en charge
    session = get_session()
    try:
        now = datetime.now(timezone.utc)
        for inst in session.query(MoodleInstance).filter_by(is_active=True).all():
            try:
                client = moodle_sync.client_for(inst)
                if inst.auto_sync_enabled:
                    found = []
                    if _every(inst.id, 'courses', COURSES_EVERY):
                        found += watch_courses(inst, client)
                    if _every(inst.id, 'teachers', TEACHERS_EVERY):
                        found += watch_teachers(inst, session)
                    found += watch_enrolments(inst, client, session)
                    _enqueue(inst.id, found)
                process_queue(session, inst, client)   # file : webhook éventuel + changements détectés
                last_full = inst.auto_sync_last_full_at
                nightly = (inst.auto_sync_enabled and now.hour == FULL_HOUR_UTC
                           and (not last_full or last_full.date() < now.date()))
                if nightly or cache_get(_k(inst.id, 'full_request')):
                    _run_full_guarded(session, inst)   # dans ce service, jamais dans l'API web
            except MoodleError as e:
                session.rollback()
                print(f"[moodle_auto] {inst.name} : {e}")
        # Documents de cours → moteur RAG (si en service et automatique activé)
        try:
            from services import rag_ingest
            rag_ingest.tick(session)
        except Exception as e:
            session.rollback()
            print(f"[moodle_auto] indexation RAG : {e}")
    finally:
        session.close()
        cache_delete('cei:moodle:tick')


def run_forever() -> None:
    """Boucle du service cei-moodle-sync."""
    if not moodle_sync.is_enabled():
        print("[moodle_auto] MOODLE_SYNC_ENABLED n'est pas à true : rien à faire, arrêt.")
        return
    # Seul processus à surveiller Moodle : un verrou laissé par un arrêt en
    # plein passage (redémarrage, déploiement) est forcément orphelin.
    cache_delete('cei:moodle:tick')

    import signal

    def stop(signum, frame):
        cache_delete('cei:moodle:tick')
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop)
    print(f"[moodle_auto] démarré — passage toutes les {TICK_SECONDS} s, inscrits relus en {ENROL_CYCLE_SECONDS // 60} min")
    while True:
        try:
            _tick()
        except Exception as e:
            print(f"[moodle_auto] passage : {e}")
        time.sleep(TICK_SECONDS)
