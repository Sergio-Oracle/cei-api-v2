"""
Droits liés à l'affectation d'un professeur à un EC.

Responsable : crée sujets et examens, publie les résultats.
Tuteur (enseignant non éditeur dans Moodle) : voit les étudiants et les
examens de l'EC, corrige ; ne crée ni sujet ni examen, ne publie pas.
Une affectation sans type (saisie à la main, ou antérieure au 07/10) est
une affectation de responsable.
"""
from models import ECAssignment, UserRole

TUTOR_MESSAGE = ("Vous êtes tuteur de cet EC : la création de sujets et d'examens est réservée "
                 "à l'enseignant responsable. Demandez-la-lui, ou à l'administrateur.")


def assignment_kind(session, ec_id, user_id):
    """'responsable', 'tuteur', ou None si le professeur n'est pas affecté."""
    if not ec_id:
        return None
    a = session.query(ECAssignment).filter_by(ec_id=ec_id, professor_id=user_id).first()
    if not a:
        return None
    return a.kind or 'responsable'


def can_author(session, ec_id, user) -> bool:
    """Peut créer sujets et examens sur cet EC."""
    if user.role in (UserRole.ADMIN, UserRole.RESPONSABLE_EXAMEN):
        return True
    return user.role == UserRole.PROFESSOR and assignment_kind(session, ec_id, user.id) == 'responsable'


# ── Droits sur les EXAMENS d'un EC ───────────────────────────────────────────
# Un examen appartient à son créateur ET aux enseignants de l'EC : quand le responsable d'examen
# (ou l'administrateur) crée un examen à la place d'un enseignant, celui-ci doit le voir et pouvoir
# le reprendre. Responsable de l'EC : mêmes droits que le créateur. Tuteur : voit, surveille et
# corrige, mais ne modifie pas l'examen et ne publie pas les résultats.
from sqlalchemy import or_, select
from models import ADMIN_LIKE, OnlineExam, Subject


def exam_ec_id(exam):
    return exam.subject.ec_id if exam is not None and exam.subject else None


def exam_access(session, exam, user_id):
    """'owner' | 'responsable' | 'tuteur' | None pour un enseignant donné."""
    if exam is None:
        return None
    if exam.created_by_id == user_id:
        return 'owner'
    return assignment_kind(session, exam_ec_id(exam), user_id)


def can_manage_exam(session, exam, user_id) -> bool:
    """Modifier, activer, prolonger, clôturer, supprimer, organiser en vagues, publier."""
    return exam_access(session, exam, user_id) in ('owner', 'responsable')


def can_view_exam(session, exam, user_id) -> bool:
    """Consulter, surveiller, corriger, exporter (tuteurs compris)."""
    return exam_access(session, exam, user_id) is not None


def visible_exam_filter(user_id):
    """Critère SQL : examens créés par l'enseignant OU portant sur un EC auquel il est affecté."""
    ecs = select(ECAssignment.ec_id).where(ECAssignment.professor_id == user_id)
    in_my_ecs = select(Subject.id).where(Subject.ec_id.in_(ecs))
    return or_(OnlineExam.created_by_id == user_id, OnlineExam.subject_id.in_(in_my_ecs))


def require_author(session, ec_id, user) -> None:
    """Création d'un sujet ou d'un examen sur un EC : administrateur et responsable d'examen partout ;
    enseignant seulement sur un EC dont il est RESPONSABLE (jamais sans EC, jamais en tuteur, jamais
    sur un EC où il n'est pas affecté). Lève PermissionError avec un message prêt à afficher."""
    if user.role in ADMIN_LIKE:
        return
    if user.role != UserRole.PROFESSOR:
        raise PermissionError('Accès non autorisé')
    if not ec_id:
        raise PermissionError("Sélectionnez un EC dont vous êtes responsable : un sujet ou un examen "
                              "est toujours rattaché à un EC.")
    kind = assignment_kind(session, ec_id, user.id)
    if kind is None:
        raise PermissionError("Vous n'êtes pas affecté à cet EC : demandez à l'administrateur ou au "
                              "responsable d'examen.")
    if kind == 'tuteur':
        raise PermissionError(TUTOR_MESSAGE)


def require_exam_author(session, subject, user) -> None:
    """Créer un examen sur ce sujet : mêmes règles que pour créer un sujet, appliquées à l'EC du sujet.
    Seul cas toléré hors EC : un ancien sujet sans EC, utilisé par son propre créateur."""
    if user.role in ADMIN_LIKE:
        return
    if user.role == UserRole.PROFESSOR and subject.ec_id is None and subject.creator_id == user.id:
        return
    require_author(session, subject.ec_id, user)


def can_see_subject(session, subject, user_id) -> bool:
    """Un enseignant voit les sujets qu'il a créés et ceux des EC auxquels il est affecté."""
    return subject.creator_id == user_id or assignment_kind(session, subject.ec_id, user_id) is not None


def can_edit_subject(session, subject, user_id) -> bool:
    """Modifier un sujet : son créateur, ou le responsable de son EC (jamais un tuteur)."""
    return subject.creator_id == user_id or assignment_kind(session, subject.ec_id, user_id) == 'responsable'
