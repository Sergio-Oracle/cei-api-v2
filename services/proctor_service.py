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
from models import (
    EC, Subject, OnlineExam, ExamStatus, ExamProctor, ProctorAssignment,
    ProctorGroupEC, ProctorGroupMember, StudentUEEnrollment, User, UserRole,
)

# Statuts d'examen sur lesquels la resynchronisation automatique agit — un
# examen déjà ACTIVE n'est pas touché ici pour ne pas perturber une
# surveillance en cours (filet de sécurité séparé : heartbeat/déconnexion).
_SYNCABLE_STATUSES = [ExamStatus.DRAFT, ExamStatus.SCHEDULED]


def sync_ec_proctors(session, ec_id):
    """Recalcule les surveillants + la pré-répartition des étudiants pour
    tous les examens à venir liés à cet EC, à partir des groupes qui lui
    sont rattachés. À appeler après toute modification de groupe/EC, et à la
    création d'un examen."""
    ec = session.query(EC).filter_by(id=ec_id).first()
    if not ec:
        return

    group_ids = [ge.group_id for ge in session.query(ProctorGroupEC).filter_by(ec_id=ec_id).all()]
    target_ids, seen = [], set()
    if group_ids:
        members = session.query(ProctorGroupMember).filter(
            ProctorGroupMember.group_id.in_(group_ids)
        ).all()
        for m in members:
            if m.proctor_id not in seen:
                seen.add(m.proctor_id)
                target_ids.append(m.proctor_id)
    target_set = set(target_ids)

    exams = session.query(OnlineExam).join(Subject, OnlineExam.subject_id == Subject.id).filter(
        Subject.ec_id == ec_id,
        OnlineExam.status.in_(_SYNCABLE_STATUSES),
    ).all()

    to_notify = []

    for exam in exams:
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

    # Retourner la liste des notifications préparées pour envoi hors transaction
    return to_notify


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
    session.add(ProctorAssignment(
        exam_id=exam_id, proctor_id=min_pid, student_id=student_id, attempt_id=attempt_id,
    ))


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
