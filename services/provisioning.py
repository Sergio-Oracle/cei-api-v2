"""
Création automatique d'un compte CEI pour une personne connue de Moodle —
phase 1 de la feuille de route CEI–UNCHK.

Principe : Keycloak dit QUI est la personne (email vérifié), Moodle dit CE
QU'ELLE FAIT (cours suivis, cours enseignés), CEI en déduit le compte, le
rôle, les inscriptions et les affectations. Un seul service, appelé par
chaque point d'entrée (connexion SSO, échange de jeton ENT, plus tard
synchronisation admin et LTI).

Règles décidées avec l'utilisateur (25/09) :
- absent de CEI ET de tout Moodle → refusé (création manuelle par l'admin) ;
- enseignant dans au moins un cours Moodle → PROFESSOR, affecté aux EC de
  ses cours ; sinon → STUDENT, inscrit aux UE de ses cours ;
- jamais de rôle admin / surveillant / superviseur automatique ;
- pas de mot de passe CEI : la personne se connecte avec son mot de passe
  UNCHK/Moodle via le SSO (tous les comptes Moodle réels sont en auth oidc).
  password_hash étant NOT NULL, il reçoit une valeur aléatoire jamais
  communiquée ; « Mot de passe oublié » reste possible en secours ;
- compte existant : jamais recréé. Seule exception, décidée par
  l'utilisateur le 25/09 : un compte ÉTUDIANT qui enseigne dans Moodle
  devient PROFESSEUR (le rôle Moodle prime). Jamais l'inverse : un
  professeur, admin, surveillant ou superviseur inscrit comme étudiant dans
  un cours Moodle garde son rôle — une rétrogradation automatique lui ferait
  perdre ses sujets, examens et droits ;
- formation d'un nouvel étudiant : champ « Département » de son profil
  Moodle (AES → formation …-AES), pas une déduction à partir de ses UE.
"""
import secrets

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from extensions import bcrypt
from models import (User, UserRole, EC, UE, Semester, Formation, ECAssignment,
                    StudentUEEnrollment)
from services import moodle_sync

# Raisons de refus, reprises telles quelles dans ?sso_error= (routes/oidc.py)
UNKNOWN_ACCOUNT = 'unknown_account'        # synchro Moodle désactivée : comportement d'origine
NOT_IN_MOODLE = 'not_in_moodle'
MOODLE_SUSPENDED = 'moodle_suspended'
NO_MOODLE_COURSE = 'no_moodle_course'
MOODLE_UNAVAILABLE = 'moodle_unavailable'


def _find_in_moodle(session, email):
    """Fusionne ce que les plateformes actives savent de la personne.
    Renvoie (personne, None) ou (None, raison)."""
    instances = moodle_sync.active_instances(session)
    if not instances:
        return None, NOT_IN_MOODLE
    found, errors, suspended = [], 0, 0
    for inst in instances:
        try:
            person = moodle_sync.client_for(inst).find_person(email)
        except moodle_sync.MoodleError as e:
            errors += 1
            print(f'[provisioning] {inst.name} injoignable pour {email} : {e}')
            continue
        if not person:
            continue
        if person['suspended']:
            suspended += 1
            continue
        found.append(person)
    if not found:
        if errors == len(instances):
            return None, MOODLE_UNAVAILABLE
        return None, MOODLE_SUSPENDED if suspended else NOT_IN_MOODLE
    merged = {
        'fullname': next((p['fullname'] for p in found if p['fullname']), email),
        'department': next((p['department'] for p in found if p['department']), ''),
        'course_codes': set().union(*(p['course_codes'] for p in found)),
        'teaching_codes': set().union(*(p['teaching_codes'] for p in found)),
    }
    if not merged['course_codes']:
        return None, NO_MOODLE_COURSE
    return merged, None


def formation_for_student(session, department, ue_ids):
    """Formation CEI d'un étudiant d'après son département Moodle : formation
    dont le code se termine par ce département (AES → L1-AES). Si le même
    département existe à plusieurs niveaux (L1-AES, L2-AES), celle où
    l'étudiant a ses UE. Département sans formation CEI (SPO, SEG…) ou
    ambiguïté non résolue → None, jamais deviné."""
    department = (department or '').strip().upper()
    if not department:
        return None
    candidates = [f for f in session.query(Formation).all()
                  if f.code.upper().split('-', 1)[-1] == department]
    if len(candidates) > 1 and ue_ids:
        counts = dict(session.query(Semester.formation_id, func.count(UE.id))
                      .join(UE, UE.semester_id == Semester.id)
                      .filter(UE.id.in_(ue_ids), Semester.formation_id.in_([c.id for c in candidates]))
                      .group_by(Semester.formation_id).all())
        if counts:
            best = max(counts, key=counts.get)
            candidates = [c for c in candidates if c.id == best]
    return candidates[0] if len(candidates) == 1 else None


def upgrade_if_moodle_teacher(session, user):
    """Compte ÉTUDIANT qui enseigne dans Moodle → PROFESSEUR, affecté aux EC
    de ses cours. Jamais dans l'autre sens. Utilise la liste des enseignants
    en cache (aucun appel Moodle pendant la connexion) et ne bloque jamais
    une connexion : en cas de problème, le compte reste tel quel."""
    if not user or user.role != UserRole.STUDENT or not user.email:
        return False
    try:
        codes = moodle_sync.teacher_map().get(user.email.strip().lower())
        if not codes:
            return False
        user.role = UserRole.PROFESSOR
        already = {ec_id for (ec_id,) in session.query(ECAssignment.ec_id).filter_by(professor_id=user.id)}
        ecs = session.query(EC).filter(EC.code.in_(codes)).all()
        for ec in ecs:
            if ec.id not in already:
                session.add(ECAssignment(ec_id=ec.id, professor_id=user.id))
        session.commit()
        print(f"[provisioning] {user.email} : étudiant → professeur (enseigne dans Moodle : "
              f"{', '.join(codes)} ; {len(ecs)} EC CEI affecté(s))")
        return True
    except Exception as e:
        session.rollback()
        print(f"[provisioning] vérification du rôle Moodle échouée pour {user.email} : {e}")
        return False


def provision_from_moodle(session, email):
    """Crée le compte CEI d'une personne connue de Moodle.
    Renvoie (user, None) si le compte existe ou vient d'être créé, sinon
    (None, raison). Un compte existant n'est modifié que dans un cas :
    étudiant qui enseigne dans Moodle (voir upgrade_if_moodle_teacher)."""
    email = (email or '').strip().lower()
    existing = session.query(User).filter_by(email=email).first()
    if existing:
        upgrade_if_moodle_teacher(session, existing)
        return existing, None
    if not moodle_sync.is_enabled():
        return None, UNKNOWN_ACCOUNT

    person, reason = _find_in_moodle(session, email)
    if reason:
        return None, reason

    is_teacher = bool(person['teaching_codes'])
    user = User(
        email=email,
        full_name=person['fullname'][:100],
        role=UserRole.PROFESSOR if is_teacher else UserRole.STUDENT,
        password_hash=bcrypt.generate_password_hash(secrets.token_urlsafe(32)).decode('utf-8'),
        is_active=True,
        email_verified=True,   # identité vérifiée par Keycloak
        has_email=True,
        created_via='moodle_sso',
    )
    session.add(user)
    try:
        session.flush()
    except IntegrityError:
        # Deux premières connexions simultanées de la même personne :
        # l'autre requête a créé le compte, on le réutilise.
        session.rollback()
        return session.query(User).filter_by(email=email).first(), None

    if is_teacher:
        ecs = session.query(EC).filter(EC.code.in_(person['teaching_codes'])).all()
        for ec in ecs:
            session.add(ECAssignment(ec_id=ec.id, professor_id=user.id))
        detail = f"{len(ecs)} EC affecté(s)"
    else:
        ecs = session.query(EC).filter(EC.code.in_(person['course_codes'])).all()
        ue_ids = sorted({ec.ue_id for ec in ecs})
        for ue_id in ue_ids:
            session.add(StudentUEEnrollment(student_id=user.id, ue_id=ue_id))
        formation = formation_for_student(session, person['department'], ue_ids)
        if formation:
            user.formation_id = formation.id
            if formation.niveau:
                user.niveau = formation.niveau.code[:5]
        detail = (f"{len(ue_ids)} UE inscrite(s), formation "
                  f"{formation.code if formation else 'non déterminée (département ' + (person['department'] or 'vide') + ')'}")

    session.commit()
    print(f"[provisioning] compte créé depuis Moodle : {email} ({user.role.value}, {detail})")
    return user, None
