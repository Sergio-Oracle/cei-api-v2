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
    if user.role == UserRole.ADMIN:
        return True
    return user.role == UserRole.PROFESSOR and assignment_kind(session, ec_id, user.id) == 'responsable'
