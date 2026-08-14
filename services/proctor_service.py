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
