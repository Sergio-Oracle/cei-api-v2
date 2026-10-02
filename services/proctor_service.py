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
from models import (
    EC, Subject, OnlineExam, ExamStatus, ExamProctor, ProctorAssignment, ExamAttempt,
    ProctorGroup, ProctorGroupEC, ProctorGroupExam, ProctorGroupMember,
    StudentUEEnrollment, User, UserRole,
)

# Statuts d'examen sur lesquels la resynchronisation automatique agit — un
# examen déjà ACTIVE n'est pas touché ici pour ne pas perturber une
# surveillance en cours (filet de sécurité séparé : heartbeat/déconnexion).
_SYNCABLE_STATUSES = [ExamStatus.DRAFT, ExamStatus.SCHEDULED]


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
# deux examens doivent être séparés d'au moins CONFLICT_MARGIN (passage d'un
# examen à l'autre, retardataires du premier).

CONFLICT_MARGIN = timedelta(minutes=15)
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


def _overlap(session, a, b) -> bool:
    a0, a1 = exam_window(session, a)
    b0, b1 = exam_window(session, b)
    return a0 < b1 + CONFLICT_MARGIN and b0 < a1 + CONFLICT_MARGIN


def _exam_label(exam) -> str:
    return f"« {exam.title} » ({exam.start_time:%d/%m %H:%M}–{exam.end_time:%H:%M} UTC)"


def group_conflicts(session, group_id, new_exams) -> list:
    """Chevauchements que créerait l'ajout de `new_exams` au planning du
    groupe : entre eux, et avec les examens qu'il couvre déjà."""
    new_ids = {e.id for e in new_exams}
    existing = [e for e, _ in group_exams(session, group_id) if e.id not in new_ids]
    conflicts, pool = [], list(existing)
    for exam in sorted(new_exams, key=lambda e: e.start_time):
        for other in pool:
            if _overlap(session, exam, other):
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
    for gid, pids in others.items():
        group = session.get(ProctorGroup, gid)
        for other, _ in group_exams(session, gid):
            for exam in new_exams:
                if other.id != exam.id and _overlap(session, exam, other):
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
        return

    proctor_ids = [ep.proctor_id for ep in session.query(ExamProctor).filter_by(exam_id=exam_id).all()]
    if not proctor_ids:
        return

    counts = {pid: 0 for pid in proctor_ids}
    for pa in session.query(ProctorAssignment).filter_by(exam_id=exam_id).all():
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
