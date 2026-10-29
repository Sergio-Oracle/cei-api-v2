"""Synchronisation Surveillants <-> EC.

Source de vérité unique : les Groupes Surveillants rattachés à un EC.
Toute modification de cette source (ajout/retrait d'un membre, rattachement/
détachement d'un EC) est répercutée automatiquement sur les examens
DRAFT/SCHEDULED de cet EC : ExamProctor (qui surveille) + ProctorAssignment
(quel étudiant pour quel surveillant, pré-affecté via StudentUEEnrollment).
Remplace la gestion manuelle par examen (ex-modal « Gestion de la
Surveillance ») — un « renfort » s'ajoute désormais au groupe permanent, pas
à un examen isolé, et se propage à tous ses examens.
"""
from datetime import datetime, timedelta
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from cache import cache_get
from models import (
    EC, Subject, OnlineExam, ExamStatus, ExamProctor, ProctorAssignment, ExamAttempt,
    ProctorGroup, ProctorGroupEC, ProctorGroupExam, ProctorGroupMember,
    StudentUEEnrollment, User, UserRole,
)

# Statuts d'examen sur lesquels la resynchronisation automatique agit. Un examen
# ACTIVE n'accepte que des AJOUTS de surveillants (voir _extend_active_exam) :
# jamais de retrait ni de déplacement d'un étudiant déjà pris en charge.
_SYNCABLE_STATUSES = [ExamStatus.DRAFT, ExamStatus.SCHEDULED, ExamStatus.ACTIVE]


def exam_group_ids(session, exam) -> list:
    """Groupes qui surveillent cet examen : ceux de son EC (tous ses examens)
    et ceux rattachés à cet examen précis. Source unique, utilisée partout où
    l'on cherche « qui surveille / qui supervise cet examen »."""
    ids = {g for (g,) in session.query(ProctorGroupExam.group_id).filter_by(exam_id=exam.id)}
    ec_id = session.query(Subject.ec_id).filter_by(id=exam.subject_id).scalar()
    if ec_id:
        ids |= {g for (g,) in session.query(ProctorGroupEC.group_id).filter_by(ec_id=ec_id)}
    return sorted(ids)


def exam_groups(session, exam) -> list:
    ids = exam_group_ids(session, exam)
    return session.query(ProctorGroup).filter(ProctorGroup.id.in_(ids)).all() if ids else []


def _wave_positions(session, exam_id) -> dict:
    """{student_id: rang de sa vague} pour un examen par vagues, {} sinon. Sert à équilibrer
    la charge de chaque surveillant VAGUE PAR VAGUE : seuls les étudiants d'une même vague
    composent en même temps, c'est donc dans chaque vague que la répartition doit être juste."""
    from models import ExamWave, ExamWaveStudent
    rows = (session.query(ExamWaveStudent.student_id, ExamWave.position)
            .join(ExamWave, ExamWave.id == ExamWaveStudent.wave_id)
            .filter(ExamWaveStudent.exam_id == exam_id).all())
    return {sid: pos for sid, pos in rows}


# ── Présence des surveillants et rééquilibrage SANS perte ────────────────────
# Un surveillant est « présent » tant que son heartbeat a moins de PRESENCE_TTL
# secondes (4 min : une coupure brève ou un serveur chargé n'en fait pas un
# absent). Avant le début de l'examen + PRESENCE_GRACE_SECONDS, un surveillant
# qui ne s'est encore jamais connecté est compté présent (on lui laisse le temps
# d'arriver). Passé ce délai, il est absent : ses étudiants vont aux autres.
PRESENCE_TTL = 240
PRESENCE_GRACE_SECONDS = 300


def present_proctor_ids(exam, proctor_ids) -> list:
    """Parmi `proctor_ids`, ceux considérés présents sur cet examen."""
    now = datetime.utcnow()
    started_long_enough = bool(exam.start_time and (now - exam.start_time).total_seconds() > PRESENCE_GRACE_SECONDS)
    out = []
    for pid in proctor_ids:
        if cache_get(f'cei:proctor_present:{exam.id}:{pid}'):
            out.append(pid)
        elif not cache_get(f'cei:proctor_seen:{exam.id}:{pid}') and not started_long_enough:
            out.append(pid)
    return out


def _open_population(session, exam_id):
    """Affectations à répartir : étudiants en cours d'examen (clé 'run') et pas
    encore entrés (clé = rang de vague). Les copies rendues/exclues ne bougent jamais."""
    from models import AttemptStatus
    attempts = {}
    for a in session.query(ExamAttempt).filter_by(exam_id=exam_id).all():
        attempts[a.student_id] = a
    wave_pos = _wave_positions(session, exam_id)
    groups = {}
    for pa in session.query(ProctorAssignment).filter_by(exam_id=exam_id).order_by(ProctorAssignment.id).all():
        a = attempts.get(pa.student_id)
        if a is None:
            groups.setdefault(('wait', wave_pos.get(pa.student_id, 0)), []).append((pa, None))
        elif a.status == AttemptStatus.IN_PROGRESS:
            groups.setdefault(('run', 0), []).append((pa, a))
        # sinon : copie rendue, exclue... on n'y touche pas
    return groups


def rebalance_exam_proctors(session, exam, present_ids) -> dict:
    """Répartit les étudiants de l'examen entre les surveillants PRÉSENTS, sans
    jamais supprimer d'affectation : on ne fait que changer le surveillant d'une
    ligne existante (et on en crée une pour un étudiant en cours qui n'en a pas).

    1. les étudiants d'un surveillant absent passent aux présents (le moins chargé d'abord) ;
    2. si l'écart de charge entre présents dépasse 1 (surveillant revenu ou ajouté
       en cours d'examen), on déplace des étudiants du plus chargé vers le moins chargé.
    Les messages restent attachés à la tentative : le nouveau surveillant les voit.
    Renvoie {'moved': n, 'orphans': n, 'loads': {surveillant: n}} ; {} si rien à faire."""
    exam_proctors = [ep.proctor_id for ep in session.query(ExamProctor).filter_by(exam_id=exam.id).all()]
    present = [pid for pid in exam_proctors if pid in set(present_ids)]
    if not present:
        return {}
    now = datetime.utcnow()
    groups = _open_population(session, exam.id)

    # étudiants en cours sans aucune ligne d'affectation : on leur en donne une
    from models import AttemptStatus
    have = {pa.student_id for items in groups.values() for pa, _ in items}
    have |= {pa.student_id for pa in session.query(ProctorAssignment).filter_by(exam_id=exam.id).all()}
    for a in session.query(ExamAttempt).filter_by(exam_id=exam.id, status=AttemptStatus.IN_PROGRESS).all():
        if a.student_id not in have:
            pa = ProctorAssignment(exam_id=exam.id, proctor_id=present[0], student_id=a.student_id, attempt_id=a.id)
            session.add(pa)
            groups.setdefault(('run', 0), []).append((pa, a))

    moved = orphans = 0
    for items in groups.values():
        load = {pid: 0 for pid in present}
        for pa, _a in items:
            if pa.proctor_id in load:
                load[pa.proctor_id] += 1

        def move(pa, a, target):
            pa.proctor_id = target
            pa.assigned_at = now
            if a is not None and not pa.attempt_id:
                pa.attempt_id = a.id

        for pa, a in items:                        # 1. orphelins
            if pa.proctor_id not in load:
                target = min(load, key=load.get)
                move(pa, a, target)
                load[target] += 1
                orphans += 1
                moved += 1
        for _ in range(len(items)):                # 2. équilibrage
            hi = max(load, key=load.get)
            lo = min(load, key=load.get)
            if load[hi] - load[lo] < 2:
                break
            pa, a = next(((p, x) for p, x in reversed(items) if p.proctor_id == hi), (None, None))
            if pa is None:
                break
            move(pa, a, lo)
            load[hi] -= 1
            load[lo] += 1
            moved += 1
    if moved:
        session.commit()
    loads = {}
    for items in groups.values():
        for pa, _a in items:
            loads[pa.proctor_id] = loads.get(pa.proctor_id, 0) + 1
    return {'moved': moved, 'orphans': orphans, 'loads': loads}


def _extend_active_exam(session, exam, target_ids) -> list:
    """Examen DÉJÀ COMMENCÉ : on peut y AJOUTER des surveillants à tout moment
    (nouveau membre du groupe, groupe rattaché en retard...), jamais en
    retirer ni déplacer un étudiant déjà pris en charge. Seuls les étudiants
    pas encore entrés en examen sont répartis à nouveau, pour que les
    nouveaux surveillants aient leur part."""
    current = {ep.proctor_id for ep in session.query(ExamProctor).filter_by(exam_id=exam.id).all()}
    new_ids = [pid for pid in target_ids if pid not in current]
    to_notify = []
    for pid in new_ids:
        session.add(ExamProctor(exam_id=exam.id, proctor_id=pid, assigned_by_id=exam.created_by_id))
        to_notify.append({
            'user_id': pid, 'event': 'proctor_assigned',
            'title': 'Nouvel examen à surveiller',
            'message': f'Vous surveillez « {exam.title} » (groupe) — l\'examen est déjà en cours.',
            'priority': 'high', 'tags': ['eyes'],
        })
    session.commit()
    rebalance_unstarted(session, exam, force=bool(new_ids))
    if new_ids:
        # Le nouveau surveillant reçoit sa part dès sa première connexion (voir _check_disconnected_proctors).
        from cache import cache_delete
        cache_delete(f'cei:proctor_presenthash:{exam.id}')
    return to_notify


def rebalance_unstarted(session, exam, force=True):
    """Répartit à nouveau, entre TOUS les surveillants de l'examen, les seuls
    étudiants qui ne sont pas encore entrés en examen (équilibré selon la
    charge déjà portée). Les étudiants déjà en cours ou rendus ne bougent pas."""
    all_ids = [ep.proctor_id for ep in session.query(ExamProctor).filter_by(exam_id=exam.id).all()]
    if not all_ids:
        return
    assignments = session.query(ProctorAssignment).filter_by(exam_id=exam.id).order_by(ProctorAssignment.id).all()
    if not assignments:
        _redistribute_students(session, exam, all_ids)
        return
    started = {sid for (sid,) in session.query(ExamAttempt.student_id).filter_by(exam_id=exam.id)}
    wave_pos = _wave_positions(session, exam.id)           # {} si l'examen n'est pas par vagues
    counts_by_wave = {}                                     # rang de vague -> {surveillant: charge}
    free = []
    for pa in assignments:
        if pa.attempt_id or pa.student_id in started:
            c = counts_by_wave.setdefault(wave_pos.get(pa.student_id, 0), {pid: 0 for pid in all_ids})
            if pa.proctor_id in c:
                c[pa.proctor_id] += 1
        else:
            free.append(pa)
    known = {pid for pid in all_ids}
    if force or any(pa.proctor_id not in known for pa in free):
        for pa in free:
            c = counts_by_wave.setdefault(wave_pos.get(pa.student_id, 0), {pid: 0 for pid in all_ids})
            pid = min(c, key=c.get)
            pa.proctor_id = pid
            c[pid] += 1
        session.commit()


def sync_exam_proctors(session, exam) -> list:
    """Surveillants + pré-répartition des étudiants d'UN examen à venir, à
    partir de ses groupes. Renvoie les notifications à envoyer."""
    group_ids = exam_group_ids(session, exam)
    target_ids, seen = [], set()
    if group_ids:
        for m in session.query(ProctorGroupMember).filter(
                ProctorGroupMember.group_id.in_(group_ids)).order_by(ProctorGroupMember.id).all():
            if m.proctor_id not in seen:
                seen.add(m.proctor_id)
                target_ids.append(m.proctor_id)
    target_set = set(target_ids)

    if exam.status == ExamStatus.ACTIVE:
        return _extend_active_exam(session, exam, target_ids)

    to_notify = []
    current = {ep.proctor_id: ep for ep in session.query(ExamProctor).filter_by(exam_id=exam.id).all()}
    for pid in target_set - current.keys():
        session.add(ExamProctor(exam_id=exam.id, proctor_id=pid, assigned_by_id=exam.created_by_id))
        to_notify.append({
            'user_id': pid,
            'event': 'proctor_assigned',
            'title': 'Nouvel examen à surveiller',
            'message': f'Vous surveillez « {exam.title} » (groupe).',
            'priority': 'default',
            'tags': ['eyes'],
        })
    for pid in current.keys() - target_set:
        session.query(ProctorAssignment).filter_by(exam_id=exam.id, proctor_id=pid).delete()
        session.delete(current[pid])
    session.commit()
    _redistribute_students(session, exam, target_ids)
    return to_notify


def sync_ec_proctors(session, ec_id):
    """Recalcule les surveillants + la pré-répartition des étudiants pour
    tous les examens à venir liés à cet EC. À appeler après toute
    modification de groupe/EC, et à la création d'un examen."""
    exams = session.query(OnlineExam).join(Subject, OnlineExam.subject_id == Subject.id).filter(
        Subject.ec_id == ec_id,
        OnlineExam.status.in_(_SYNCABLE_STATUSES),
    ).all()
    to_notify = []
    for exam in exams:
        to_notify.extend(sync_exam_proctors(session, exam))
    return to_notify


def sync_group(session, group_id) -> list:
    """Après un changement de membres : tous les examens à venir du groupe
    (ceux de ses EC et ceux rattachés directement)."""
    to_notify = []
    for exam, _source in group_exams(session, group_id, statuses=_SYNCABLE_STATUSES):
        to_notify.extend(sync_exam_proctors(session, exam))
    return to_notify


# ── Planning d'un groupe et chevauchements ──────────────────────────────────
# Un groupe (ou un surveillant) ne peut pas être sur deux examens à la fois :
# deux examens doivent être séparés d'au moins le repos minimum du groupe
# (ProctorGroup.min_gap_minutes, 30 min par défaut, réglable par groupe).

DEFAULT_GAP_MINUTES = 30


def group_gap_minutes(session, group_id) -> int:
    v = session.query(ProctorGroup.min_gap_minutes).filter_by(id=group_id).scalar()
    return DEFAULT_GAP_MINUTES if v is None else int(v)
_PLANNED_STATUSES = [ExamStatus.DRAFT, ExamStatus.SCHEDULED, ExamStatus.ACTIVE]


def group_exams(session, group_id, statuses=None, upcoming=True) -> list:
    """[(examen, 'ec' | 'exam')] couverts par le groupe, triés par début."""
    statuses = statuses or _PLANNED_STATUSES
    q = session.query(OnlineExam).filter(OnlineExam.status.in_(statuses))
    if upcoming:
        q = q.filter(OnlineExam.end_time >= datetime.utcnow())
    ec_ids = [e for (e,) in session.query(ProctorGroupEC.ec_id).filter_by(group_id=group_id)]
    direct = {e for (e,) in session.query(ProctorGroupExam.exam_id).filter_by(group_id=group_id)}
    found = {}
    if ec_ids:
        for exam in q.join(Subject, OnlineExam.subject_id == Subject.id).filter(Subject.ec_id.in_(ec_ids)).all():
            found[exam.id] = (exam, 'ec')
    if direct:
        for exam in q.filter(OnlineExam.id.in_(direct)).all():
            found.setdefault(exam.id, (exam, 'exam'))
    return sorted(found.values(), key=lambda t: t[0].start_time)


def exam_window(session, exam):
    """Début et fin réelle : la fin tient compte du temps supplémentaire le
    plus long accordé à une tentative de cet examen."""
    extra = session.query(func.max(ExamAttempt.extra_minutes)).filter_by(exam_id=exam.id).scalar() or 0
    return exam.start_time, exam.end_time + timedelta(minutes=extra)


def _overlap(session, a, b, gap_minutes=DEFAULT_GAP_MINUTES) -> bool:
    a0, a1 = exam_window(session, a)
    b0, b1 = exam_window(session, b)
    gap = timedelta(minutes=gap_minutes)
    return a0 < b1 + gap and b0 < a1 + gap


def _exam_label(exam) -> str:
    return f"« {exam.title} » ({exam.start_time:%d/%m %H:%M}–{exam.end_time:%H:%M} UTC)"


def group_conflicts(session, group_id, new_exams) -> list:
    """Chevauchements que créerait l'ajout de `new_exams` au planning du
    groupe : entre eux, et avec les examens qu'il couvre déjà."""
    new_ids = {e.id for e in new_exams}
    existing = [e for e, _ in group_exams(session, group_id) if e.id not in new_ids]
    gap = group_gap_minutes(session, group_id)
    conflicts, pool = [], list(existing)
    for exam in sorted(new_exams, key=lambda e: e.start_time):
        for other in pool:
            if _overlap(session, exam, other, gap):
                conflicts.append({'exam_id': exam.id, 'exam': _exam_label(exam),
                                  'with_exam_id': other.id, 'with_exam': _exam_label(other)})
        pool.append(exam)
    return conflicts


def member_conflicts(session, group_id, new_exams) -> list:
    """Avertissement : membres du groupe déjà pris sur le même créneau par un
    AUTRE groupe dont ils font partie."""
    members = {m.proctor_id: m for m in session.query(ProctorGroupMember).filter_by(group_id=group_id)}
    if not members or not new_exams:
        return []
    others = {}
    for m in session.query(ProctorGroupMember).filter(
            ProctorGroupMember.proctor_id.in_(members), ProctorGroupMember.group_id != group_id):
        others.setdefault(m.group_id, []).append(m.proctor_id)
    warnings = []
    gap_here = group_gap_minutes(session, group_id)
    for gid, pids in others.items():
        group = session.get(ProctorGroup, gid)
        gap = max(gap_here, group_gap_minutes(session, gid))
        for other, _ in group_exams(session, gid):
            for exam in new_exams:
                if other.id != exam.id and _overlap(session, exam, other, gap):
                    for pid in pids:
                        warnings.append({'proctor_id': pid,
                                         'proctor': members[pid].proctor.full_name if members[pid].proctor else '?',
                                         'exam': _exam_label(exam), 'with_exam': _exam_label(other),
                                         'other_group': group.name if group else '?'})
    return warnings


def _redistribute_students(session, exam, proctor_ids):
    """Répartit (round-robin, ordre alphabétique) les étudiants inscrits à
    l'UE de l'EC du sujet entre les surveillants donnés — même logique que
    l'ex-répartition manuelle, désormais automatique."""
    session.query(ProctorAssignment).filter_by(exam_id=exam.id).delete()
    if not proctor_ids:
        session.commit()
        return

    subject = session.query(Subject).filter_by(id=exam.subject_id).first()
    if not (subject and subject.ec_id):
        session.commit()
        return
    ec = session.query(EC).filter_by(id=subject.ec_id).first()
    if not (ec and ec.ue_id):
        session.commit()
        return

    students = session.query(User).join(
        StudentUEEnrollment, User.id == StudentUEEnrollment.student_id
    ).filter(
        StudentUEEnrollment.ue_id == ec.ue_id,
        User.role == UserRole.STUDENT,
    ).order_by(User.full_name).all()

    # Examen par vagues : round-robin vague après vague (chaque vague reçoit une part
    # égale de chaque surveillant, puisque ce sont ses étudiants qui composent ensemble).
    wave_pos = _wave_positions(session, exam.id)
    if wave_pos:
        students.sort(key=lambda u: (wave_pos.get(u.id, 10_000), (u.full_name or '').lower(), u.id))

    nb = len(proctor_ids)
    for i, student in enumerate(students):
        pid = proctor_ids[i % nb]
        session.add(ProctorAssignment(exam_id=exam.id, proctor_id=pid, student_id=student.id, attempt_id=None))
    session.commit()


def assign_single_attempt(session, exam_id, student_id, attempt_id):
    """Affecte une tentative unique au surveillant le moins chargé de l'examen.

    Appelée au DÉMARRAGE de la tentative (start_exam_attempt), pas à chaque
    lecture du tableau de surveillance — auparavant get_active_proctoring
    (une route GET) recalculait et écrivait cette affectation à chaque appel,
    ce qui la rendait coûteuse et la répétait à chaque rafraîchissement de
    tous les surveillants connectés. Ne fait rien si l'étudiant est déjà
    affecté (pré-affectation) ou si l'examen n'a pas de surveillant. Ne
    commit pas — laisse l'appelant gérer la transaction.
    """
    already = session.query(ProctorAssignment).filter_by(exam_id=exam_id).filter(
        (ProctorAssignment.attempt_id == attempt_id) | (ProctorAssignment.student_id == student_id)
    ).first()
    if already:
        if not already.attempt_id:
            already.attempt_id = attempt_id
        # Pré-affecté à un surveillant qui n'est pas là : l'étudiant va à un présent.
        exam = session.get(OnlineExam, exam_id)
        all_ids = [ep.proctor_id for ep in session.query(ExamProctor).filter_by(exam_id=exam_id).all()]
        present = present_proctor_ids(exam, all_ids) if exam else []
        if present and already.proctor_id not in present:
            load = {pid: 0 for pid in present}
            for pa in session.query(ProctorAssignment).filter_by(exam_id=exam_id).all():
                if pa.proctor_id in load:
                    load[pa.proctor_id] += 1
            already.proctor_id = min(load, key=load.get)
            already.assigned_at = datetime.utcnow()
        return

    proctor_ids = [ep.proctor_id for ep in session.query(ExamProctor).filter_by(exam_id=exam_id).all()]
    if not proctor_ids:
        return
    exam = session.get(OnlineExam, exam_id)
    proctor_ids = (present_proctor_ids(exam, proctor_ids) if exam else []) or proctor_ids

    counts = {pid: 0 for pid in proctor_ids}
    wave_pos = _wave_positions(session, exam_id)
    mine = wave_pos.get(student_id, 0)
    for pa in session.query(ProctorAssignment).filter_by(exam_id=exam_id).all():
        # par vagues : on ne compte que la charge de la MÊME vague (celle qui compose en ce moment)
        if wave_pos and wave_pos.get(pa.student_id, 0) != mine:
            continue
        if pa.proctor_id in counts:
            counts[pa.proctor_id] += 1

    min_pid = min(counts, key=counts.get)
    # Le "already" verifie plus haut n'est pas atomique avec cette insertion :
    # sous forte concurrence (deux requetes /start quasi simultanees pour le
    # meme etudiant — double-clic reel, ou double-tentative apres coupure
    # reseau), les deux peuvent passer la verification avant que l'une des
    # deux ne committe. Sans filet, la seconde fait echouer TOUTE la
    # transaction (y compris la creation de la tentative elle-meme) avec un
    # 500 — vecu en test de charge (contrainte unique_exam_student_proctor).
    # Un SAVEPOINT isole cette insertion : si elle echoue, seule elle est
    # annulee, la tentative deja creee par l'appelant reste valide.
    try:
        with session.begin_nested():
            session.add(ProctorAssignment(
                exam_id=exam_id, proctor_id=min_pid, student_id=student_id, attempt_id=attempt_id,
            ))
    except IntegrityError:
        pass


def backfill_unassigned_attempts(session, exam_id=None):
    """Filet de sécurité à exécuter UNE FOIS au déploiement de ce correctif :
    affecte les tentatives IN_PROGRESS déjà démarrées avant que
    assign_single_attempt() n'existe et qui n'ont donc jamais reçu
    d'affectation (l'ancien filet — l'auto-affectation dans
    get_active_proctoring — vient d'être retiré). Idempotent, sans effet sur
    les tentatives déjà affectées."""
    from models import ExamAttempt, AttemptStatus
    q = session.query(ExamAttempt).filter_by(status=AttemptStatus.IN_PROGRESS)
    if exam_id:
        q = q.filter_by(exam_id=exam_id)
    n = 0
    for attempt in q.all():
        before = session.query(ProctorAssignment).filter_by(exam_id=attempt.exam_id).filter(
            (ProctorAssignment.attempt_id == attempt.id) | (ProctorAssignment.student_id == attempt.student_id)
        ).first()
        if not before:
            assign_single_attempt(session, attempt.exam_id, attempt.student_id, attempt.id)
            n += 1
    session.commit()
    return n


def exam_proctor_conflicts(session, exam) -> list:
    """À la création ou au déplacement d'un examen : ses groupes sont-ils déjà
    pris sur ce créneau ? (avertissement renvoyé à l'enseignant ; le
    rattachement lui-même, décidé dans la page des groupes, est refusé)."""
    out = []
    for group in exam_groups(session, exam):
        for c in group_conflicts(session, group.id, [exam]):
            out.append({'group_id': group.id, 'group': group.name, 'with_exam': c['with_exam']})
    return out
