"""
Blueprint Admin — Utilisateurs, Dashboard, Étudiants.

Routes migrées depuis app.py :
  GET  /api/admin/dashboard
  GET  /api/admin/corrected_papers
  GET  /api/users/proctors
  GET  /api/admin/users
  POST /api/admin/users
  PUT  /api/admin/users/<id>
  DELETE /api/admin/users/<id>
  GET  /api/students/list
  POST /api/admin/users/student-no-email   (depuis route ~6638)
"""
from flask import Blueprint, request, jsonify
from sqlalchemy import or_

from extensions import bcrypt
from auth_paseto import paseto_required, get_current_user_id, get_current_user_role
from models import (
    get_session,
    User, UserRole,
    Subject, StudentPaper, Reclamation, CorrectionHistory,
    Formation, ECAssignment, StudentUEEnrollment,
    GradeTranscript, ExamAttempt, ExamActivityLog, CameraLog,
    ProctorAssignment, ReclamationStatus, TokenBlocklist,
    OnlineExam, ExamProctor, QuestionBank,
)
from utils import send_account_created_email

admin_users_bp = Blueprint('admin_users', __name__)


def _require_admin(session):
    """Vérifie que l'utilisateur courant est admin, ferme la session et lève 403 sinon."""
    user = session.query(User).filter_by(id=get_current_user_id()).first()
    if not user or user.role != UserRole.ADMIN:
        session.close()
        return None
    return user


# ── Dashboard ─────────────────────────────────────────────────────────────────
@admin_users_bp.route('/api/admin/dashboard', methods=['GET'])
@paseto_required
def admin_dashboard():
    try:
        session = get_session()
        if not _require_admin(session):
            return jsonify({'error': 'Accès non autorisé'}), 403

        data = {
            'total_users':            session.query(User).count(),
            'total_students':         session.query(User).filter_by(role=UserRole.STUDENT).count(),
            'total_professors':       session.query(User).filter_by(role=UserRole.PROFESSOR).count(),
            'total_surveillants':     session.query(User).filter_by(role=UserRole.SURVEILLANT).count(),
            'total_subjects':         session.query(Subject).count(),
            'total_papers':           session.query(StudentPaper).count(),
            'pending_reclamations':   session.query(Reclamation).filter_by(status=ReclamationStatus.PENDING).count(),
            'total_corrected_papers': session.query(StudentPaper).filter(StudentPaper.corrected_at != None).count(),
        }
        session.close()
        return jsonify(data)
    except Exception as e:
        print(f"ERROR admin_dashboard: {e}")
        return jsonify({'error': str(e)}), 500


# ── Copies corrigées ──────────────────────────────────────────────────────────
@admin_users_bp.route('/api/admin/corrected_papers', methods=['GET'])
@paseto_required
def admin_corrected_papers():
    try:
        from sqlalchemy.orm import joinedload
        session = get_session()
        if not _require_admin(session):
            return jsonify({'error': 'Accès non autorisé'}), 403

        papers = (
            session.query(StudentPaper)
            .options(joinedload(StudentPaper.student), joinedload(StudentPaper.subject))
            .filter(StudentPaper.corrected_at != None)
            .order_by(StudentPaper.corrected_at.desc())
            .limit(50)
            .all()
        )
        result = [{
            'id':             p.id,
            'student_name':   p.student.full_name if p.student else 'Inconnu',
            'student_email':  p.student.email     if p.student else 'N/A',
            'subject_title':  p.subject.title     if p.subject else 'N/A',
            'score':          p.score,
            'corrected_at':   p.corrected_at.isoformat() if p.corrected_at else None,
            'filename':       p.filename,
        } for p in papers]
        session.close()
        return jsonify({'papers': result})
    except Exception as e:
        print(f"ERROR admin_corrected_papers: {e}")
        return jsonify({'error': str(e)}), 500


# ── Surveillants disponibles ──────────────────────────────────────────────────
@admin_users_bp.route('/api/users/proctors', methods=['GET'])
@paseto_required
def get_proctor_users():
    try:
        role = get_current_user_role()
        if role not in ['professor', 'admin']:
            return jsonify({'error': 'Accès non autorisé'}), 403
        session = get_session()
        users = (
            session.query(User)
            .filter(User.role == UserRole.SURVEILLANT, User.is_active == True)
            .order_by(User.full_name)
            .all()
        )
        result = [u.to_dict() for u in users]
        session.close()
        return jsonify(result)
    except Exception as e:
        print(f"ERROR get_proctor_users: {e}")
        return jsonify({'error': str(e)}), 500


# ── Liste tous les utilisateurs ───────────────────────────────────────────────
@admin_users_bp.route('/api/admin/users', methods=['GET'])
@paseto_required
def get_all_users():
    try:
        session = get_session()
        if not _require_admin(session):
            return jsonify({'error': 'Accès non autorisé'}), 403

        search      = request.args.get('search', '').strip()
        niveau      = request.args.get('niveau', '').strip()
        role_filter = request.args.get('role', '').strip()

        query = session.query(User)
        if search:
            query = query.filter(or_(
                User.full_name.ilike(f'%{search}%'),
                User.email.ilike(f'%{search}%'),
            ))
        if niveau:
            query = query.filter(User.niveau == niveau)
        if role_filter:
            try:
                query = query.filter(User.role == UserRole[role_filter.upper()])
            except KeyError:
                pass

        users = query.order_by(User.created_at.desc()).all()
        result = [u.to_dict() for u in users]
        session.close()
        return jsonify(result)
    except Exception as e:
        print(f"ERROR get_all_users: {e}")
        return jsonify({'error': str(e)}), 500


# ── Créer un utilisateur ──────────────────────────────────────────────────────
@admin_users_bp.route('/api/admin/users', methods=['POST'])
@paseto_required
def create_user():
    session = get_session()
    try:
        if not _require_admin(session):
            return jsonify({'error': 'Accès non autorisé'}), 403

        data     = request.get_json(silent=True) or {}
        existing = session.query(User).filter_by(email=data.get('email', '')).first()
        if existing:
            labels = {'professor': 'un enseignant', 'student': 'un étudiant',
                      'admin': 'un administrateur', 'surveillant': 'un surveillant'}
            label = labels.get(existing.role.value, 'un utilisateur')
            return jsonify({'error': f"Cet email est déjà utilisé par {label} ({existing.full_name})."}), 400

        role_str = data.get('role', 'student').upper()
        if role_str not in ['STUDENT', 'PROFESSOR', 'ADMIN', 'SURVEILLANT']:
            return jsonify({'error': 'Rôle invalide'}), 400

        niveau_val = (data.get('niveau') or '').strip().upper() or None
        if niveau_val and niveau_val not in ['L1', 'L2', 'L3', 'M1', 'M2']:
            niveau_val = None
        if role_str != 'STUDENT':
            niveau_val = None  # le niveau (L1..M2) n'a de sens que pour un étudiant

        new_user = User(
            email=data['email'],
            password_hash=bcrypt.generate_password_hash(data['password']).decode('utf-8'),
            full_name=data['full_name'],
            role=UserRole[role_str],
            niveau=niveau_val,
        )
        session.add(new_user); session.commit()
        user_dict = new_user.to_dict()

        try:
            send_account_created_email(data['email'], data['full_name'],
                                       data.get('role', 'student'), data['password'])
        except Exception as e:
            print(f"WARNING email: {e}")

        return jsonify({'success': True, 'message': 'Utilisateur créé avec succès', 'user': user_dict}), 201
    except Exception as e:
        print(f"ERROR create_user: {e}")
        import traceback; traceback.print_exc()
        session.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        session.close()


# ── Modifier un utilisateur ───────────────────────────────────────────────────
@admin_users_bp.route('/api/admin/users/<int:target_id>', methods=['PUT'])
@paseto_required
def update_user(target_id):
    session = get_session()
    try:
        if not _require_admin(session):
            return jsonify({'error': 'Accès non autorisé'}), 403

        user = session.query(User).filter_by(id=target_id).first()
        if not user:
            return jsonify({'error': 'Utilisateur non trouvé'}), 404

        data = request.get_json(silent=True) or {}
        if 'full_name' in data:
            user.full_name = data['full_name']
        if 'email' in data and data['email'] != user.email:
            if session.query(User).filter_by(email=data['email']).first():
                return jsonify({'error': 'Cet email est déjà utilisé'}), 400
            user.email = data['email']
        if data.get('password'):
            user.password_hash = bcrypt.generate_password_hash(data['password']).decode('utf-8')
        if 'role' in data:
            rs = data['role'].upper()
            if rs in ['STUDENT', 'PROFESSOR', 'ADMIN', 'SURVEILLANT']:
                user.role = UserRole[rs]
        if 'is_active' in data:
            user.is_active = bool(data['is_active'])
        if 'niveau' in data:
            nv = (data['niveau'] or '').strip().upper() or None
            nv = nv if nv in ['L1', 'L2', 'L3', 'M1', 'M2'] else None
            user.niveau = nv if user.role == UserRole.STUDENT else None
        elif user.role != UserRole.STUDENT and user.niveau is not None:
            user.niveau = None  # rôle changé vers non-étudiant : nettoyer un niveau résiduel

        session.commit()
        user_dict = user.to_dict()
        return jsonify({'success': True, 'message': 'Utilisateur modifié avec succès', 'user': user_dict})
    except Exception as e:
        print(f"ERROR update_user: {e}")
        import traceback; traceback.print_exc()
        session.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        session.close()


# ── Supprimer un utilisateur ──────────────────────────────────────────────────
@admin_users_bp.route('/api/admin/users/<int:target_id>', methods=['DELETE'])
@paseto_required
def delete_user(target_id):
    try:
        me      = get_current_user_id()
        session = get_session()
        if not _require_admin(session):
            return jsonify({'error': 'Accès non autorisé'}), 403
        if me == target_id:
            session.close()
            return jsonify({'error': 'Impossible de supprimer votre propre compte'}), 400

        user = session.query(User).filter_by(id=target_id).first()
        if not user:
            session.close()
            return jsonify({'error': 'Utilisateur non trouvé'}), 404

        # ── Dépendances bloquantes ───────────────────────────────────────────
        # Ces relations ne peuvent être ni supprimées (perte d'audit/de contenu),
        # ni mises à NULL (colonne NOT NULL) sans casser l'intégrité des données.
        # On bloque la suppression avec un message clair plutôt que de laisser
        # PostgreSQL renvoyer une ForeignKeyViolation brute (500 opaque).
        blocking = []
        n = session.query(OnlineExam).filter_by(created_by_id=target_id).count()
        if n:
            blocking.append(f"{n} examen(s) créé(s)")
        n = session.query(QuestionBank).filter_by(created_by_id=target_id).count()
        if n:
            blocking.append(f"{n} question(s) de banque créée(s)")
        n = session.query(ExamProctor).filter(or_(
            ExamProctor.proctor_id == target_id, ExamProctor.assigned_by_id == target_id
        )).count()
        if n:
            blocking.append(f"{n} affectation(s) de surveillance")
        n = session.query(CorrectionHistory).filter_by(corrector_id=target_id).count()
        if n:
            blocking.append(f"{n} correction(s) effectuée(s) (historique)")
        if blocking:
            session.close()
            return jsonify({'error': 'Suppression impossible : ' + ', '.join(blocking) +
                             ". Réaffectez ou archivez ces éléments avant de supprimer ce compte."}), 409

        # ── Bookkeeping sans valeur d'audit — supprimable/annulable sans risque ─
        session.query(TokenBlocklist).filter_by(user_id=target_id).delete(synchronize_session=False)
        session.query(ExamAttempt).filter_by(corrected_by_id=target_id).update(
            {'corrected_by_id': None}, synchronize_session=False)
        session.query(GradeTranscript).filter_by(generated_by_id=target_id).update(
            {'generated_by_id': None}, synchronize_session=False)
        session.query(ProctorAssignment).filter(or_(
            ProctorAssignment.proctor_id == target_id, ProctorAssignment.student_id == target_id
        )).delete(synchronize_session=False)

        # Copies de l'étudiant → dépendances en cascade
        paper_ids = [p.id for p in session.query(StudentPaper).filter_by(student_id=target_id).all()]
        if paper_ids:
            session.query(Reclamation).filter(or_(
                Reclamation.paper_id.in_(paper_ids),
                Reclamation.student_id == target_id,
            )).delete(synchronize_session=False)
            session.query(CorrectionHistory).filter(
                CorrectionHistory.paper_id.in_(paper_ids)
            ).delete(synchronize_session=False)
        else:
            session.query(Reclamation).filter_by(student_id=target_id).delete()

        for r in session.query(Reclamation).filter_by(responded_by_id=target_id).all():
            r.responded_by_id = None

        session.query(StudentPaper).filter_by(student_id=target_id).delete()
        for p in session.query(StudentPaper).filter_by(corrected_by_id=target_id).all():
            p.corrected_by_id = None

        session.query(Subject).filter_by(creator_id=target_id).delete()
        session.query(ECAssignment).filter_by(professor_id=target_id).delete()
        session.query(StudentUEEnrollment).filter_by(student_id=target_id).delete()
        session.query(GradeTranscript).filter_by(student_id=target_id).delete(synchronize_session=False)

        attempt_ids = [a.id for a in session.query(ExamAttempt).filter_by(student_id=target_id).all()]
        if attempt_ids:
            session.query(ProctorAssignment).filter(
                ProctorAssignment.attempt_id.in_(attempt_ids)
            ).delete(synchronize_session=False)
            session.query(CameraLog).filter(
                CameraLog.attempt_id.in_(attempt_ids)
            ).delete(synchronize_session=False)
            session.query(ExamActivityLog).filter(
                ExamActivityLog.attempt_id.in_(attempt_ids)
            ).delete(synchronize_session=False)
            session.query(Reclamation).filter(
                Reclamation.attempt_id.in_(attempt_ids)
            ).delete(synchronize_session=False)
        session.query(ExamAttempt).filter_by(student_id=target_id).delete(synchronize_session=False)

        session.delete(user); session.commit(); session.close()
        return jsonify({'success': True, 'message': 'Utilisateur supprimé avec succès'})
    except Exception as e:
        print(f"ERROR delete_user: {e}")
        import traceback; traceback.print_exc()
        try:
            session.rollback(); session.close()
        except Exception:
            pass
        return jsonify({'error': str(e)}), 500


# ── Liste des étudiants (professeur/admin) ────────────────────────────────────
@admin_users_bp.route('/api/students/list', methods=['GET'])
@paseto_required
def get_students_list():
    try:
        role = get_current_user_role()
        if role not in ['professor', 'admin']:
            return jsonify({'error': 'Accès non autorisé'}), 403
        session = get_session()
        students = session.query(User).filter_by(role=UserRole.STUDENT).order_by(User.full_name).all()
        result = []
        for s in students:
            f = session.query(Formation).filter_by(id=s.formation_id).first() if getattr(s, 'formation_id', None) else None
            result.append({
                'id':             s.id,
                'full_name':      s.full_name,
                'email':          s.email,
                'formation_id':   getattr(s, 'formation_id', None),
                'formation_name': f.name if f else None,
                'formation_code': f.code if f else None,
            })
        session.close()
        return jsonify(result)
    except Exception as e:
        print(f"ERROR get_students_list: {e}")
        return jsonify({'error': str(e)}), 500


# ── Créer un étudiant sans email institutionnel ───────────────────────────────
@admin_users_bp.route('/api/admin/users/student-no-email', methods=['POST'])
@paseto_required
def create_student_no_email():
    """Crée un compte étudiant sans email institutionnel (email temporaire généré)."""
    try:
        session = get_session()
        if not _require_admin(session):
            return jsonify({'error': 'Accès non autorisé'}), 403

        data      = request.json or {}
        full_name = (data.get('full_name') or '').strip()
        if not full_name:
            session.close()
            return jsonify({'error': 'Nom complet requis'}), 400

        import unicodedata, re, secrets as _sec
        normalized = unicodedata.normalize('NFKD', full_name).encode('ASCII', 'ignore').decode()
        normalized = re.sub(r'[^\w\s]', '', normalized).lower().strip()
        slug       = re.sub(r'\s+', '.', normalized)
        email      = f"{slug}.{_sec.token_hex(3)}@no-email.cei.local"
        password   = _sec.token_urlsafe(8)

        if session.query(User).filter_by(email=email).first():
            email = f"{slug}.{_sec.token_hex(5)}@no-email.cei.local"

        niveau_val = (data.get('niveau') or '').strip().upper() or None
        if niveau_val and niveau_val not in ['L1', 'L2', 'L3', 'M1', 'M2']:
            niveau_val = None

        new_user = User(
            email=email,
            password_hash=bcrypt.generate_password_hash(password).decode('utf-8'),
            full_name=full_name,
            role=UserRole.STUDENT,
            niveau=niveau_val,
            has_email=False,
        )
        session.add(new_user); session.commit()
        user_dict = new_user.to_dict(); session.close()
        return jsonify({
            'success': True,
            'message': 'Étudiant créé (sans email institutionnel)',
            'user':    user_dict,
            'temp_password': password,
        }), 201
    except Exception as e:
        print(f"ERROR create_student_no_email: {e}")
        return jsonify({'error': str(e)}), 500
