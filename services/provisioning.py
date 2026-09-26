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


def ensure_formation_for_department(session, department, ec, dry_run=False):
    """Formation d'un étudiant d'après son département Moodle, CRÉÉE si elle
    n'existe pas encore (décision utilisateur du 26/09 : pas de passage
    manuel par la maquette puis nouvelle synchronisation). Rien n'est
    inventé : niveau et pôle sont ceux de la formation à laquelle appartient
    le cours réellement suivi (ec), le code suit la convention existante
    (<niveau>-<département>, ex. L1-SPO). Seul le nom complet est provisoire,
    à compléter par l'admin dans la maquette.
    Renvoie (formation ou None, code créé ou à créer ou None)."""
    department = (department or '').strip().upper()
    formation = formation_for_student(session, department, [ec.ue_id] if ec else [])
    if formation or not department or not ec:
        return formation, None
    source = (session.query(Formation)
              .join(Semester, Semester.formation_id == Formation.id)
              .join(UE, UE.semester_id == Semester.id)
              .filter(UE.id == ec.ue_id).first())
    if not source or not source.niveau:
        return None, None
    code = f'{source.niveau.code}-{department}'
    existing = session.query(Formation).filter_by(code=code).first()
    if existing:
        return existing, None
    if dry_run:
        return None, code
    created = Formation(
        code=code, name=f'{department} — créée depuis Moodle', level=source.level,
        niveau_id=source.niveau_id, pole_id=source.pole_id, department=department, is_active=True,
        description=(f'Créée automatiquement depuis le département Moodle « {department} » ; niveau et pôle '
                     f'repris du cours {ec.code}. À compléter dans la maquette : nom complet, semestres.'),
    )
    try:
        with session.begin_nested():  # une création simultanée du même code n'annule pas le reste
            session.add(created)
    except IntegrityError:
        return session.query(Formation).filter_by(code=code).first(), None
    print(f'[provisioning] formation créée automatiquement : {code} (pôle/niveau du cours {ec.code})')
    return created, code


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


def sync_course(session, ec, client, course, dry_run=True):
    """Synchronise UN cours Moodle (= un EC CEI) avec les mêmes règles que la
    connexion : comptes manquants créés, étudiant qui enseigne → professeur,
    affectations EC, inscriptions à l'UE, formation depuis le département
    Moodle. Uniquement additif : aucun compte supprimé, aucune inscription
    retirée, aucun rôle autre qu'étudiant modifié. dry_run → compte sans
    rien écrire. Renvoie un bilan détaillé."""
    students = client.enrolled_students(course['id'])
    teachers = client.course_teachers(course['id'])
    teacher_emails = {t['email'] for t in teachers}

    emails = sorted({x['email'] for x in students + teachers if x['email']})
    existing = {}
    for i in range(0, len(emails), 1000):
        for u in session.query(User).filter(User.email.in_(emails[i:i + 1000])).all():
            existing[u.email.lower()] = u

    # Une empreinte aléatoire commune à tous les comptes créés par cet appel :
    # personne ne connaît le mot de passe sous-jacent (connexion par SSO) et
    # on évite un calcul bcrypt par compte.
    pw_hash = None if dry_run else bcrypt.generate_password_hash(secrets.token_urlsafe(32)).decode('utf-8')

    def new_user(email, fullname, role, formation=None):
        u = User(email=email, full_name=(fullname or email)[:100], role=role, password_hash=pw_hash,
                 is_active=True, email_verified=True, has_email=True, created_via='moodle_sync')
        if formation:
            u.formation_id = formation.id
            if formation.niveau:
                u.niveau = formation.niveau.code[:5]
        session.add(u)
        return u

    # Listes d'emails (et pas seulement des totaux) : en simulation, rien
    # n'est créé entre deux cours, donc un même étudiant absent de CEI
    # apparaît dans chacun de ses cours — l'interface dédoublonne le bilan
    # global à partir de ces listes.
    t_rep = {'moodle': len(teachers), 'created': 0, 'upgraded': 0, 'assignments_added': 0,
             'other_role': [], 'created_emails': [], 'upgraded_emails': []}
    s_rep = {'moodle': len(students), 'created': 0, 'enrollments_added': 0, 'already_enrolled': 0,
             'formation_filled': 0, 'other_role': 0, 'without_formation': {}, 'formations_created': [],
             'created_emails': [], 'enrolled_emails': [], 'formation_filled_emails': []}

    # ── Enseignants ──
    assigned = {pid for (pid,) in session.query(ECAssignment.professor_id).filter_by(ec_id=ec.id)}
    for t in teachers:
        u = existing.get(t['email'])
        if u is None:
            t_rep['created'] += 1
            t_rep['created_emails'].append(t['email'])
            if not dry_run:
                u = new_user(t['email'], t['fullname'], UserRole.PROFESSOR)
                session.flush()
                existing[t['email']] = u
        elif u.role == UserRole.STUDENT:
            t_rep['upgraded'] += 1
            t_rep['upgraded_emails'].append(t['email'])
            if not dry_run:
                u.role = UserRole.PROFESSOR
        elif u.role != UserRole.PROFESSOR:
            t_rep['other_role'].append({'email': t['email'], 'role': u.role.value})
            continue
        if u is None or u.id not in assigned:
            t_rep['assignments_added'] += 1
            if not dry_run:
                session.add(ECAssignment(ec_id=ec.id, professor_id=u.id))
                assigned.add(u.id)

    # ── Étudiants ──
    enrolled = {sid for (sid,) in session.query(StudentUEEnrollment.student_id).filter_by(ue_id=ec.ue_id)}
    formations = {}   # département → (formation ou None, code créé / à créer ou None)

    def formation_of(dept):
        if dept not in formations:
            formation, new_code = ensure_formation_for_department(session, dept, ec, dry_run=dry_run)
            formations[dept] = (formation, new_code)
            if new_code:
                s_rep['formations_created'].append(new_code)
        return formations[dept]

    to_enroll = []
    for s in students:
        email = s['email']
        if not email or email in teacher_emails:
            continue
        u = existing.get(email)
        formation, new_code = formation_of(s['department'])
        if u is None:
            s_rep['created'] += 1
            s_rep['enrollments_added'] += 1
            s_rep['created_emails'].append(email)
            s_rep['enrolled_emails'].append(email)
            # Sans formation seulement si le département est vide ou si le
            # cours n'a lui-même ni niveau ni pôle (création impossible).
            if not formation and not new_code:
                key = s['department'] or '(vide)'
                s_rep['without_formation'].setdefault(key, []).append(email)
            if not dry_run:
                u = new_user(email, s['fullname'], UserRole.STUDENT, formation)
                existing[email] = u
                to_enroll.append(u)
            continue
        if u.role != UserRole.STUDENT:
            s_rep['other_role'] += 1  # ex. professeur inscrit comme étudiant dans Moodle : jamais modifié
            continue
        if u.formation_id is None and (formation or new_code):
            s_rep['formation_filled'] += 1
            s_rep['formation_filled_emails'].append(email)
            if not dry_run and formation:
                u.formation_id = formation.id
                if formation.niveau:
                    u.niveau = formation.niveau.code[:5]
        if u.id in enrolled:
            s_rep['already_enrolled'] += 1
        else:
            s_rep['enrollments_added'] += 1
            s_rep['enrolled_emails'].append(email)
            if not dry_run:
                to_enroll.append(u)

    if not dry_run:
        session.flush()  # identifiants des comptes créés
        for u in to_enroll:
            if u.id not in enrolled:
                session.add(StudentUEEnrollment(student_id=u.id, ue_id=ec.ue_id))
                enrolled.add(u.id)
        session.commit()

    return {'teachers': t_rep, 'students': s_rep}


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
        if not formation and person['department'] and ecs:
            # Département sans formation CEI : créée depuis le niveau/pôle du
            # cours suivi (même règle que la synchronisation admin).
            formation, _ = ensure_formation_for_department(session, person['department'], ecs[0])
        if formation:
            user.formation_id = formation.id
            if formation.niveau:
                user.niveau = formation.niveau.code[:5]
        detail = (f"{len(ue_ids)} UE inscrite(s), formation "
                  f"{formation.code if formation else 'non déterminée (département ' + (person['department'] or 'vide') + ')'}")

    session.commit()
    print(f"[provisioning] compte créé depuis Moodle : {email} ({user.role.value}, {detail})")
    return user, None
