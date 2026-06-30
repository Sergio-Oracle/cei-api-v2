"""
Blueprint Formations — Contrôleur MVC.

Couvre :
  - Formations, Semestres, UEs, ECs (lecture + CRUD admin)
  - Affectations EC ↔ Professeur
  - Inscriptions Étudiant ↔ UE
  - Étudiants du professeur

Migré depuis app.py — zéro régression.
"""
from flask import Blueprint, request, jsonify
from sqlalchemy.orm import joinedload

from auth_paseto import paseto_required, get_current_user_id, get_current_user_role
from models import (
    get_session,
    User, UserRole,
    Formation, Semester, UE, EC, ECAssignment, StudentUEEnrollment,
)

formations_bp = Blueprint('formations', __name__)


def _is_admin(session):
    u = session.query(User).filter_by(id=get_current_user_id()).first()
    if not u or u.role != UserRole.ADMIN:
        session.close()
        return False, None
    return True, u


# ═══════════════════════════════════════════════════════════════════════════════
# LECTURE
# ═══════════════════════════════════════════════════════════════════════════════

@formations_bp.route('/api/formations', methods=['GET'])
@paseto_required
def get_formations():
    try:
        session = get_session()
        formations = session.query(Formation).filter_by(is_active=True).all()
        result = [f.to_dict() for f in formations]
        session.close()
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@formations_bp.route('/api/formations/<int:formation_id>/semesters', methods=['GET'])
@paseto_required
def get_formation_semesters(formation_id):
    try:
        session = get_session()
        semesters = session.query(Semester).filter_by(formation_id=formation_id, is_active=True).all()
        result = [s.to_dict() for s in semesters]
        session.close()
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@formations_bp.route('/api/semesters/<int:semester_id>/ues', methods=['GET'])
@paseto_required
def get_semester_ues(semester_id):
    try:
        session = get_session()
        ues = session.query(UE).filter_by(semester_id=semester_id, is_active=True).all()
        result = [ue.to_dict() for ue in ues]
        session.close()
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@formations_bp.route('/api/ues/<int:ue_id>/ecs', methods=['GET'])
@paseto_required
def get_ue_ecs(ue_id):
    try:
        user_id = get_current_user_id()
        session = get_session()
        user = session.query(User).filter_by(id=user_id).first()
        query = session.query(EC).filter_by(ue_id=ue_id, is_active=True)
        if user and user.role == UserRole.PROFESSOR:
            query = query.join(ECAssignment).filter(ECAssignment.professor_id == user_id)
        result = [ec.to_dict() for ec in query.all()]
        session.close()
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@formations_bp.route('/api/ecs', methods=['GET'])
@paseto_required
def get_all_ecs():
    try:
        user_id = get_current_user_id()
        session = get_session()
        user = session.query(User).filter_by(id=user_id).first()
        query = session.query(EC).filter_by(is_active=True).options(joinedload(EC.ue))
        if user and user.role == UserRole.PROFESSOR:
            query = query.join(ECAssignment).filter(ECAssignment.professor_id == user_id)
        niveau      = request.args.get('niveau')
        formation_id = request.args.get('formation_id', type=int)
        if niveau or formation_id:
            query = (query
                     .join(UE, EC.ue_id == UE.id)
                     .join(Semester, UE.semester_id == Semester.id)
                     .join(Formation, Semester.formation_id == Formation.id))
            if niveau:
                query = query.filter(Formation.level == niveau)
            if formation_id:
                query = query.filter(Formation.id == formation_id)
        result = [ec.to_dict() for ec in query.all()]
        session.close()
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@formations_bp.route('/api/ues', methods=['GET'])
@paseto_required
def list_all_ues():
    try:
        role = get_current_user_role()
        if role not in ['professor', 'admin']:
            return jsonify({'error': 'Accès non autorisé'}), 403
        session = get_session()
        ues = session.query(UE).order_by(UE.name).all()
        result = [u.to_dict() for u in ues]
        session.close()
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ═══════════════════════════════════════════════════════════════════════════════
# CRUD FORMATIONS
# ═══════════════════════════════════════════════════════════════════════════════

@formations_bp.route('/api/admin/formations', methods=['POST'])
@paseto_required
def create_formation():
    try:
        session = get_session()
        ok, _ = _is_admin(session)
        if not ok: return jsonify({'error': 'Accès non autorisé'}), 403
        data = request.json or {}
        if session.query(Formation).filter_by(code=data.get('code', '')).first():
            session.close()
            return jsonify({'error': 'Code formation déjà utilisé'}), 400
        f = Formation(
            code=data['code'], name=data['name'],
            level=data.get('level', ''), department=data.get('department', ''),
            description=data.get('description', ''),
        )
        session.add(f); session.commit()
        result = f.to_dict(); session.close()
        return jsonify({'success': True, 'formation': result}), 201
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@formations_bp.route('/api/admin/formations/<int:fid>', methods=['PUT'])
@paseto_required
def update_formation(fid):
    try:
        session = get_session()
        ok, _ = _is_admin(session)
        if not ok: return jsonify({'error': 'Accès non autorisé'}), 403
        f = session.query(Formation).filter_by(id=fid).first()
        if not f: session.close(); return jsonify({'error': 'Formation non trouvée'}), 404
        data = request.json or {}
        if 'code' in data and data['code'] != f.code:
            if session.query(Formation).filter_by(code=data['code']).first():
                session.close(); return jsonify({'error': 'Code déjà utilisé'}), 400
            f.code = data['code']
        for field in ('name', 'level', 'department', 'description', 'is_active'):
            if field in data: setattr(f, field, data[field])
        session.commit(); result = f.to_dict(); session.close()
        return jsonify({'success': True, 'formation': result})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@formations_bp.route('/api/admin/formations/<int:fid>', methods=['DELETE'])
@paseto_required
def delete_formation(fid):
    try:
        session = get_session()
        ok, _ = _is_admin(session)
        if not ok: return jsonify({'error': 'Accès non autorisé'}), 403
        f = session.query(Formation).filter_by(id=fid).first()
        if not f: session.close(); return jsonify({'error': 'Formation non trouvée'}), 404
        session.delete(f); session.commit(); session.close()
        return jsonify({'success': True, 'message': 'Formation supprimée'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ═══════════════════════════════════════════════════════════════════════════════
# CRUD SEMESTRES
# ═══════════════════════════════════════════════════════════════════════════════

@formations_bp.route('/api/admin/semesters', methods=['POST'])
@paseto_required
def create_semester():
    try:
        session = get_session()
        ok, _ = _is_admin(session)
        if not ok: return jsonify({'error': 'Accès non autorisé'}), 403
        data = request.json or {}
        if not session.query(Formation).filter_by(id=data.get('formation_id')).first():
            session.close(); return jsonify({'error': 'Formation non trouvée'}), 404
        s = Semester(
            formation_id=data['formation_id'], number=data['number'],
            name=data['name'], total_credits=data.get('total_credits', 30),
        )
        session.add(s); session.commit(); result = s.to_dict(); session.close()
        return jsonify({'success': True, 'semester': result}), 201
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@formations_bp.route('/api/admin/semesters/<int:sid>', methods=['PUT'])
@paseto_required
def update_semester(sid):
    try:
        session = get_session()
        ok, _ = _is_admin(session)
        if not ok: return jsonify({'error': 'Accès non autorisé'}), 403
        s = session.query(Semester).filter_by(id=sid).first()
        if not s: session.close(); return jsonify({'error': 'Semestre non trouvé'}), 404
        data = request.json or {}
        for field in ('number', 'name', 'total_credits', 'is_active'):
            if field in data: setattr(s, field, data[field])
        session.commit(); result = s.to_dict(); session.close()
        return jsonify({'success': True, 'semester': result})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@formations_bp.route('/api/admin/semesters/<int:sid>', methods=['DELETE'])
@paseto_required
def delete_semester(sid):
    try:
        session = get_session()
        ok, _ = _is_admin(session)
        if not ok: return jsonify({'error': 'Accès non autorisé'}), 403
        s = session.query(Semester).filter_by(id=sid).first()
        if not s: session.close(); return jsonify({'error': 'Semestre non trouvé'}), 404
        session.delete(s); session.commit(); session.close()
        return jsonify({'success': True, 'message': 'Semestre supprimé'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ═══════════════════════════════════════════════════════════════════════════════
# CRUD UEs
# ═══════════════════════════════════════════════════════════════════════════════

@formations_bp.route('/api/admin/ues', methods=['POST'])
@paseto_required
def create_ue():
    try:
        session = get_session()
        ok, _ = _is_admin(session)
        if not ok: return jsonify({'error': 'Accès non autorisé'}), 403
        data = request.json or {}
        if session.query(UE).filter_by(code=data.get('code', '')).first():
            session.close(); return jsonify({'error': 'Code UE déjà utilisé'}), 400
        if not session.query(Semester).filter_by(id=data.get('semester_id')).first():
            session.close(); return jsonify({'error': 'Semestre non trouvé'}), 404
        ue = UE(semester_id=data['semester_id'], code=data['code'],
                name=data['name'], credits=data.get('credits', 6))
        session.add(ue); session.commit(); result = ue.to_dict(); session.close()
        return jsonify({'success': True, 'ue': result}), 201
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@formations_bp.route('/api/admin/ues/<int:uid>', methods=['PUT'])
@paseto_required
def update_ue(uid):
    try:
        session = get_session()
        ok, _ = _is_admin(session)
        if not ok: return jsonify({'error': 'Accès non autorisé'}), 403
        ue = session.query(UE).filter_by(id=uid).first()
        if not ue: session.close(); return jsonify({'error': 'UE non trouvée'}), 404
        data = request.json or {}
        if 'code' in data and data['code'] != ue.code:
            if session.query(UE).filter_by(code=data['code']).first():
                session.close(); return jsonify({'error': 'Code déjà utilisé'}), 400
            ue.code = data['code']
        for field in ('name', 'credits', 'is_active'):
            if field in data: setattr(ue, field, data[field])
        session.commit(); result = ue.to_dict(); session.close()
        return jsonify({'success': True, 'ue': result})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@formations_bp.route('/api/admin/ues/<int:uid>', methods=['DELETE'])
@paseto_required
def delete_ue(uid):
    try:
        session = get_session()
        ok, _ = _is_admin(session)
        if not ok: return jsonify({'error': 'Accès non autorisé'}), 403
        ue = session.query(UE).filter_by(id=uid).first()
        if not ue: session.close(); return jsonify({'error': 'UE non trouvée'}), 404
        session.delete(ue); session.commit(); session.close()
        return jsonify({'success': True, 'message': 'UE supprimée'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ═══════════════════════════════════════════════════════════════════════════════
# CRUD ECs
# ═══════════════════════════════════════════════════════════════════════════════

@formations_bp.route('/api/admin/ecs', methods=['POST'])
@paseto_required
def create_ec():
    try:
        session = get_session()
        ok, _ = _is_admin(session)
        if not ok: return jsonify({'error': 'Accès non autorisé'}), 403
        data = request.json or {}
        if session.query(EC).filter_by(code=data.get('code', '')).first():
            session.close(); return jsonify({'error': 'Code EC déjà utilisé'}), 400
        if not session.query(UE).filter_by(id=data.get('ue_id')).first():
            session.close(); return jsonify({'error': 'UE non trouvée'}), 404
        ec = EC(ue_id=data['ue_id'], code=data['code'], name=data['name'],
                cm=data.get('cm', 0), td=data.get('td', 0), tp=data.get('tp', 0),
                tpe=data.get('tpe', 0), vht=data.get('vht', 0),
                coefficient=data.get('coefficient', 1))
        session.add(ec); session.commit(); result = ec.to_dict(); session.close()
        return jsonify({'success': True, 'ec': result}), 201
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@formations_bp.route('/api/admin/ecs/<int:eid>', methods=['PUT'])
@paseto_required
def update_ec(eid):
    try:
        session = get_session()
        ok, _ = _is_admin(session)
        if not ok: return jsonify({'error': 'Accès non autorisé'}), 403
        ec = session.query(EC).filter_by(id=eid).first()
        if not ec: session.close(); return jsonify({'error': 'EC non trouvé'}), 404
        data = request.json or {}
        if 'code' in data and data['code'] != ec.code:
            if session.query(EC).filter_by(code=data['code']).first():
                session.close(); return jsonify({'error': 'Code déjà utilisé'}), 400
            ec.code = data['code']
        for field in ('name', 'cm', 'td', 'tp', 'tpe', 'vht', 'coefficient', 'is_active'):
            if field in data: setattr(ec, field, data[field])
        session.commit(); result = ec.to_dict(); session.close()
        return jsonify({'success': True, 'ec': result})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@formations_bp.route('/api/admin/ecs/<int:eid>', methods=['DELETE'])
@paseto_required
def delete_ec(eid):
    try:
        session = get_session()
        ok, _ = _is_admin(session)
        if not ok: return jsonify({'error': 'Accès non autorisé'}), 403
        ec = session.query(EC).filter_by(id=eid).first()
        if not ec: session.close(); return jsonify({'error': 'EC non trouvé'}), 404
        session.delete(ec); session.commit(); session.close()
        return jsonify({'success': True, 'message': 'EC supprimé'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ═══════════════════════════════════════════════════════════════════════════════
# AFFECTATIONS EC ↔ PROFESSEUR
# ═══════════════════════════════════════════════════════════════════════════════

@formations_bp.route('/api/admin/ec_assignments', methods=['POST'])
@paseto_required
def assign_ec_to_professor():
    try:
        session = get_session()
        ok, _ = _is_admin(session)
        if not ok: return jsonify({'error': 'Accès non autorisé'}), 403
        data = request.json or {}
        ec_id, prof_id = data.get('ec_id'), data.get('professor_id')
        if not ec_id or not prof_id:
            session.close(); return jsonify({'error': 'EC et professeur requis'}), 400
        if not session.query(EC).filter_by(id=ec_id).first():
            session.close(); return jsonify({'error': 'EC non trouvé'}), 404
        if not session.query(User).filter_by(id=prof_id, role=UserRole.PROFESSOR).first():
            session.close(); return jsonify({'error': 'Professeur non trouvé'}), 404
        if session.query(ECAssignment).filter_by(ec_id=ec_id, professor_id=prof_id).first():
            session.close(); return jsonify({'error': 'Ce professeur est déjà affecté à cet EC'}), 400
        session.add(ECAssignment(ec_id=ec_id, professor_id=prof_id))
        session.commit(); session.close()
        return jsonify({'success': True, 'message': 'EC affecté avec succès'}), 201
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@formations_bp.route('/api/admin/ecs/<int:eid>/assign', methods=['POST'])
@paseto_required
def assign_ec_by_id(eid):
    try:
        session = get_session()
        ok, _ = _is_admin(session)
        if not ok: return jsonify({'error': 'Accès non autorisé'}), 403
        data = request.json or {}
        prof_id = data.get('professor_id')
        if not prof_id: session.close(); return jsonify({'error': 'Professeur requis'}), 400
        if not session.query(EC).filter_by(id=eid).first():
            session.close(); return jsonify({'error': 'EC non trouvé'}), 404
        if not session.query(User).filter_by(id=prof_id, role=UserRole.PROFESSOR).first():
            session.close(); return jsonify({'error': 'Professeur non trouvé'}), 404
        if session.query(ECAssignment).filter_by(ec_id=eid, professor_id=prof_id).first():
            session.close(); return jsonify({'error': 'Ce professeur est déjà affecté à cet EC'}), 400
        session.add(ECAssignment(ec_id=eid, professor_id=prof_id))
        session.commit(); session.close()
        return jsonify({'success': True, 'message': 'EC affecté avec succès'}), 201
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@formations_bp.route('/api/admin/ec_assignments/<int:aid>', methods=['DELETE'])
@paseto_required
def remove_ec_assignment(aid):
    try:
        session = get_session()
        ok, _ = _is_admin(session)
        if not ok: return jsonify({'error': 'Accès non autorisé'}), 403
        a = session.query(ECAssignment).filter_by(id=aid).first()
        if not a: session.close(); return jsonify({'error': 'Affectation non trouvée'}), 404
        session.delete(a); session.commit(); session.close()
        return jsonify({'success': True, 'message': 'Affectation supprimée'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ═══════════════════════════════════════════════════════════════════════════════
# INSCRIPTIONS ÉTUDIANT ↔ UE
# ═══════════════════════════════════════════════════════════════════════════════

@formations_bp.route('/api/admin/student_enrollments', methods=['POST'])
@paseto_required
def enroll_student_to_ue():
    try:
        session = get_session()
        ok, _ = _is_admin(session)
        if not ok: return jsonify({'error': 'Accès non autorisé'}), 403
        data = request.json or {}
        sid, uid = data.get('student_id'), data.get('ue_id')
        if not sid or not uid: session.close(); return jsonify({'error': 'Étudiant et UE requis'}), 400
        if not session.query(User).filter_by(id=sid, role=UserRole.STUDENT).first():
            session.close(); return jsonify({'error': 'Étudiant non trouvé'}), 404
        if not session.query(UE).filter_by(id=uid).first():
            session.close(); return jsonify({'error': 'UE non trouvée'}), 404
        if session.query(StudentUEEnrollment).filter_by(student_id=sid, ue_id=uid).first():
            session.close(); return jsonify({'error': 'Étudiant déjà inscrit à cette UE'}), 400
        session.add(StudentUEEnrollment(student_id=sid, ue_id=uid))
        session.commit(); session.close()
        return jsonify({'success': True, 'message': 'Étudiant inscrit avec succès'}), 201
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@formations_bp.route('/api/admin/students/<int:student_id>/enroll', methods=['POST'])
@paseto_required
def enroll_student_by_id(student_id):
    try:
        session = get_session()
        ok, _ = _is_admin(session)
        if not ok: return jsonify({'error': 'Accès non autorisé'}), 403
        data = request.json or {}
        ue_id = data.get('ue_id')
        if not ue_id: session.close(); return jsonify({'error': 'UE requis (ue_id manquant)'}), 400
        if not session.query(User).filter_by(id=student_id, role=UserRole.STUDENT).first():
            session.close(); return jsonify({'error': 'Étudiant non trouvé'}), 404
        if not session.query(UE).filter_by(id=ue_id).first():
            session.close(); return jsonify({'error': 'UE non trouvée'}), 404
        if session.query(StudentUEEnrollment).filter_by(student_id=student_id, ue_id=ue_id).first():
            session.close(); return jsonify({'error': 'Étudiant déjà inscrit à cette UE'}), 400
        session.add(StudentUEEnrollment(student_id=student_id, ue_id=ue_id))
        session.commit(); session.close()
        return jsonify({'success': True, 'message': 'Étudiant inscrit avec succès'}), 201
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@formations_bp.route('/api/professor/my_students', methods=['GET'])
@paseto_required
def get_professor_students():
    try:
        user_id = get_current_user_id()
        session = get_session()
        user = session.query(User).filter_by(id=user_id).first()
        if not user or user.role not in [UserRole.PROFESSOR, UserRole.ADMIN]:
            session.close(); return jsonify({'error': 'Accès non autorisé'}), 403

        assignments = session.query(ECAssignment).filter_by(professor_id=user_id).all()
        ec_ids = [a.ec_id for a in assignments]
        ecs = session.query(EC).filter(EC.id.in_(ec_ids)).all() if ec_ids else []
        ue_ids = list({ec.ue_id for ec in ecs if ec.ue_id})

        if not ue_ids:
            session.close()
            return jsonify({'ecs': [], 'students': [], 'total': 0})

        enrollments = (
            session.query(StudentUEEnrollment)
            .join(User, StudentUEEnrollment.student_id == User.id)
            .filter(StudentUEEnrollment.ue_id.in_(ue_ids), User.role == UserRole.STUDENT)
            .all()
        )
        student_ues = {}
        for e in enrollments:
            student_ues.setdefault(e.student_id, set()).add(e.ue_id)

        students_out = []
        for sid, enrolled_ue_ids in student_ues.items():
            student = session.query(User).filter_by(id=sid, role=UserRole.STUDENT).first()
            if not student: continue
            formation = (session.query(Formation).filter_by(id=student.formation_id).first()
                         if getattr(student, 'formation_id', None) else None)
            student_ecs = []
            for ec in ecs:
                if ec.ue_id in enrolled_ue_ids:
                    ue = session.query(UE).filter_by(id=ec.ue_id).first()
                    student_ecs.append({'ec_code': ec.code, 'ec_name': ec.name,
                                        'ue_code': ue.code if ue else '—'})
            students_out.append({
                'id':             student.id,
                'full_name':      student.full_name,
                'email':          student.email,
                'niveau':         student.niveau,
                'formation_code': formation.code if formation else None,
                'formation_name': formation.name if formation else None,
                'ecs':            student_ecs,
            })
        students_out.sort(key=lambda x: x['full_name'])

        ecs_out = []
        for ec in ecs:
            ue = session.query(UE).filter_by(id=ec.ue_id).first()
            count = session.query(StudentUEEnrollment).filter_by(ue_id=ec.ue_id).count()
            ecs_out.append({'ec_code': ec.code, 'ec_name': ec.name,
                            'ue_code': ue.code if ue else '—', 'student_count': count})

        session.close()
        return jsonify({'ecs': ecs_out, 'students': students_out, 'total': len(students_out)})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@formations_bp.route('/api/admin/students/<int:student_id>/enrollments', methods=['GET'])
@paseto_required
def get_student_enrollments(student_id):
    try:
        session = get_session()
        ok, _ = _is_admin(session)
        if not ok: return jsonify({'error': 'Accès non autorisé'}), 403
        result = []
        for e in session.query(StudentUEEnrollment).filter_by(student_id=student_id).all():
            ue = session.query(UE).filter_by(id=e.ue_id).first()
            if not ue: continue
            sem  = session.query(Semester).filter_by(id=ue.semester_id).first() if ue.semester_id else None
            form = session.query(Formation).filter_by(id=sem.formation_id).first() if sem and sem.formation_id else None
            result.append({
                'enrollment_id':  e.id,
                'ue_id':          ue.id,
                'ue_code':        ue.code,
                'ue_name':        ue.name,
                'semester_name':  sem.name  if sem  else '—',
                'formation_name': form.name if form else '—',
                'formation_code': form.code if form else '—',
            })
        session.close()
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@formations_bp.route('/api/admin/students/<int:student_id>/set_formation', methods=['POST'])
@paseto_required
def set_student_formation(student_id):
    try:
        session = get_session()
        ok, _ = _is_admin(session)
        if not ok: return jsonify({'error': 'Accès non autorisé'}), 403
        data = request.get_json() or {}
        formation_id = data.get('formation_id')
        if not formation_id: session.close(); return jsonify({'error': 'formation_id requis'}), 400
        student = session.query(User).filter_by(id=student_id, role=UserRole.STUDENT).first()
        if not student: session.close(); return jsonify({'error': 'Étudiant non trouvé'}), 404
        formation = session.query(Formation).filter_by(id=formation_id).first()
        if not formation: session.close(); return jsonify({'error': 'Formation non trouvée'}), 404
        if data.get('replace_all'):
            session.query(StudentUEEnrollment).filter_by(student_id=student_id).delete()
        added = 0
        for sem in session.query(Semester).filter_by(formation_id=formation_id).all():
            for ue in session.query(UE).filter_by(semester_id=sem.id).all():
                if not session.query(StudentUEEnrollment).filter_by(student_id=student_id, ue_id=ue.id).first():
                    session.add(StudentUEEnrollment(student_id=student_id, ue_id=ue.id))
                    added += 1
        student.formation_id = formation_id
        formation_name = formation.name
        session.commit(); session.close()
        return jsonify({'success': True, 'added': added, 'formation_name': formation_name,
                        'message': f'Formation : {formation_name} — {added} UE(s) ajoutée(s).'}), 200
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@formations_bp.route('/api/admin/students/<int:student_id>/enroll_formation', methods=['POST'])
@paseto_required
def enroll_student_formation(student_id):
    try:
        session = get_session()
        ok, _ = _is_admin(session)
        if not ok: return jsonify({'error': 'Accès non autorisé'}), 403
        data = request.get_json() or {}
        formation_id = data.get('formation_id')
        if not formation_id: session.close(); return jsonify({'error': 'formation_id requis'}), 400
        if not session.query(User).filter_by(id=student_id, role=UserRole.STUDENT).first():
            session.close(); return jsonify({'error': 'Étudiant non trouvé'}), 404
        formation = session.query(Formation).filter_by(id=formation_id).first()
        if not formation: session.close(); return jsonify({'error': 'Formation non trouvée'}), 404
        added, already = 0, 0
        for sem in session.query(Semester).filter_by(formation_id=formation_id).all():
            for ue in session.query(UE).filter_by(semester_id=sem.id).all():
                if session.query(StudentUEEnrollment).filter_by(student_id=student_id, ue_id=ue.id).first():
                    already += 1
                else:
                    session.add(StudentUEEnrollment(student_id=student_id, ue_id=ue.id)); added += 1
        session.commit(); session.close()
        return jsonify({'success': True, 'added': added, 'already': already,
                        'message': f'{added} UE(s) ajoutée(s), {already} déjà inscrite(s).'}), 201
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@formations_bp.route('/api/admin/student_enrollments/<int:eid>', methods=['DELETE'])
@paseto_required
def remove_student_enrollment(eid):
    try:
        session = get_session()
        ok, _ = _is_admin(session)
        if not ok: return jsonify({'error': 'Accès non autorisé'}), 403
        e = session.query(StudentUEEnrollment).filter_by(id=eid).first()
        if not e: session.close(); return jsonify({'error': 'Inscription non trouvée'}), 404
        session.delete(e); session.commit(); session.close()
        return jsonify({'success': True, 'message': 'Inscription supprimée'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
