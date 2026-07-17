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
from cache import cache_get, cache_set, cache_delete_pattern, make_key
from models import (
    get_session,
    User, UserRole,
    Pole, Niveau, Formation, Semester, UE, EC, ECAssignment, StudentUEEnrollment,
    ProctorGroup, ProctorGroupMember, ProctorGroupEC,
)

_CACHE_TTL = 300  # 5 minutes — structure académique change rarement


def _invalidate_academic_cache():
    """Invalider le cache après toute modification de la structure académique."""
    cache_delete_pattern('cei:*formations*')
    cache_delete_pattern('cei:*semesters*')
    cache_delete_pattern('cei:*ues*')
    cache_delete_pattern('cei:*poles*')
    cache_delete_pattern('cei:*niveaux*')
    cache_delete_pattern('cei:*ecs*')

formations_bp = Blueprint('formations', __name__)


def _is_admin(session):
    u = session.query(User).filter_by(id=get_current_user_id()).first()
    if not u or u.role != UserRole.ADMIN:
        session.close()
        return False, None
    return True, u


# ═══════════════════════════════════════════════════════════════════════════════
# PÔLES
# ═══════════════════════════════════════════════════════════════════════════════

@formations_bp.route('/api/poles', methods=['GET'])
@paseto_required
def get_poles():
    try:
        key = make_key('poles', 'all')
        cached = cache_get(key)
        if cached is not None:
            return jsonify(cached)
        session = get_session()
        poles = session.query(Pole).filter_by(is_active=True).order_by(Pole.code).all()
        result = [p.to_dict() for p in poles]
        session.close()
        cache_set(key, result, ttl=_CACHE_TTL)
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@formations_bp.route('/api/poles/<int:pole_id>/formations', methods=['GET'])
@paseto_required
def get_pole_formations(pole_id):
    try:
        key = make_key('poles', str(pole_id), 'formations')
        cached = cache_get(key)
        if cached is not None:
            return jsonify(cached)
        session = get_session()
        formations = session.query(Formation).filter_by(pole_id=pole_id, is_active=True).all()
        result = [f.to_dict() for f in formations]
        session.close()
        cache_set(key, result, ttl=_CACHE_TTL)
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@formations_bp.route('/api/admin/poles', methods=['POST'])
@paseto_required
def create_pole():
    try:
        session = get_session()
        ok, _ = _is_admin(session)
        if not ok:
            return jsonify({'error': 'Accès non autorisé'}), 403
        data = request.get_json() or {}
        if not data.get('code') or not data.get('name'):
            session.close()
            return jsonify({'error': 'Code et nom requis'}), 400
        pole = Pole(
            code=data['code'].strip().upper(),
            name=data['name'].strip(),
            description=data.get('description', ''),
        )
        session.add(pole)
        session.commit()
        result = pole.to_dict()
        session.close()
        _invalidate_academic_cache()
        return jsonify(result), 201
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@formations_bp.route('/api/admin/poles/<int:pid>', methods=['PUT'])
@paseto_required
def update_pole(pid):
    try:
        session = get_session()
        ok, _ = _is_admin(session)
        if not ok:
            return jsonify({'error': 'Accès non autorisé'}), 403
        data = request.get_json() or {}
        pole = session.query(Pole).filter_by(id=pid).first()
        if not pole:
            session.close()
            return jsonify({'error': 'Pôle non trouvé'}), 404
        if 'name' in data:
            pole.name = data['name'].strip()
        if 'description' in data:
            pole.description = data['description']
        if 'is_active' in data:
            pole.is_active = bool(data['is_active'])
        session.commit()
        result = pole.to_dict()
        session.close()
        _invalidate_academic_cache()
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@formations_bp.route('/api/admin/poles/<int:pid>', methods=['DELETE'])
@paseto_required
def delete_pole(pid):
    try:
        session = get_session()
        ok, _ = _is_admin(session)
        if not ok:
            return jsonify({'error': 'Accès non autorisé'}), 403
        pole = session.query(Pole).filter_by(id=pid).first()
        if not pole:
            session.close()
            return jsonify({'error': 'Pôle non trouvé'}), 404
        pole.is_active = False
        session.commit()
        session.close()
        _invalidate_academic_cache()
        return jsonify({'message': 'Pôle désactivé'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ═══════════════════════════════════════════════════════════════════════════════
# NIVEAUX
# ═══════════════════════════════════════════════════════════════════════════════

@formations_bp.route('/api/niveaux', methods=['GET'])
@paseto_required
def get_niveaux():
    try:
        key = make_key('niveaux', 'all')
        cached = cache_get(key)
        if cached is not None:
            return jsonify(cached)
        session = get_session()
        niveaux = session.query(Niveau).filter_by(is_active=True).order_by(Niveau.code).all()
        result = [n.to_dict() for n in niveaux]
        session.close()
        cache_set(key, result, ttl=_CACHE_TTL)
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@formations_bp.route('/api/admin/niveaux', methods=['POST'])
@paseto_required
def create_niveau():
    try:
        session = get_session()
        ok, _ = _is_admin(session)
        if not ok:
            return jsonify({'error': 'Accès non autorisé'}), 403
        data = request.get_json() or {}
        if not data.get('code') or not data.get('name'):
            session.close()
            return jsonify({'error': 'Code et nom requis'}), 400
        niveau = Niveau(
            code=data['code'].strip().upper(),
            name=data['name'].strip(),
            description=data.get('description', ''),
        )
        session.add(niveau)
        session.commit()
        result = niveau.to_dict()
        session.close()
        _invalidate_academic_cache()
        return jsonify(result), 201
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@formations_bp.route('/api/admin/niveaux/<int:nid>', methods=['PUT'])
@paseto_required
def update_niveau(nid):
    try:
        session = get_session()
        ok, _ = _is_admin(session)
        if not ok:
            return jsonify({'error': 'Accès non autorisé'}), 403
        data = request.get_json() or {}
        niveau = session.query(Niveau).filter_by(id=nid).first()
        if not niveau:
            session.close()
            return jsonify({'error': 'Niveau non trouvé'}), 404
        if 'name' in data:
            niveau.name = data['name'].strip()
        if 'description' in data:
            niveau.description = data['description']
        if 'is_active' in data:
            niveau.is_active = bool(data['is_active'])
        session.commit()
        # Garder la colonne Formation.level (texte) synchronisée pour tout le
        # code existant qui la lit encore directement.
        if 'name' in data:
            session.query(Formation).filter_by(niveau_id=nid).update({'level': niveau.name})
            session.commit()
        result = niveau.to_dict()
        session.close()
        _invalidate_academic_cache()
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@formations_bp.route('/api/admin/niveaux/<int:nid>', methods=['DELETE'])
@paseto_required
def delete_niveau(nid):
    try:
        session = get_session()
        ok, _ = _is_admin(session)
        if not ok:
            return jsonify({'error': 'Accès non autorisé'}), 403
        niveau = session.query(Niveau).filter_by(id=nid).first()
        if not niveau:
            session.close()
            return jsonify({'error': 'Niveau non trouvé'}), 404
        niveau.is_active = False
        session.commit()
        session.close()
        _invalidate_academic_cache()
        return jsonify({'message': 'Niveau désactivé'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ═══════════════════════════════════════════════════════════════════════════════
# LECTURE
# ═══════════════════════════════════════════════════════════════════════════════

@formations_bp.route('/api/formations', methods=['GET'])
@paseto_required
def get_formations():
    try:
        key = make_key('formations', 'all')
        cached = cache_get(key)
        if cached is not None:
            return jsonify(cached)
        session = get_session()
        formations = session.query(Formation).filter_by(is_active=True).all()
        result = [f.to_dict() for f in formations]
        session.close()
        cache_set(key, result, ttl=_CACHE_TTL)
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@formations_bp.route('/api/formations/<int:formation_id>/semesters', methods=['GET'])
@paseto_required
def get_formation_semesters(formation_id):
    try:
        key = make_key('semesters', str(formation_id))
        cached = cache_get(key)
        if cached is not None:
            return jsonify(cached)
        session = get_session()
        semesters = session.query(Semester).filter_by(formation_id=formation_id, is_active=True).all()
        result = [s.to_dict() for s in semesters]
        session.close()
        cache_set(key, result, ttl=_CACHE_TTL)
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@formations_bp.route('/api/semesters/<int:semester_id>/ues', methods=['GET'])
@paseto_required
def get_semester_ues(semester_id):
    try:
        key = make_key('ues', 'sem', str(semester_id))
        cached = cache_get(key)
        if cached is not None:
            return jsonify(cached)
        session = get_session()
        ues = session.query(UE).filter_by(semester_id=semester_id, is_active=True).all()
        result = [ue.to_dict() for ue in ues]
        session.close()
        cache_set(key, result, ttl=_CACHE_TTL)
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
        key = make_key('ues', 'all')
        cached = cache_get(key)
        if cached is not None:
            return jsonify(cached)
        session = get_session()
        ues = session.query(UE).order_by(UE.name).all()
        result = [u.to_dict() for u in ues]
        session.close()
        cache_set(key, result, ttl=_CACHE_TTL)
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
        niveau_id = data.get('niveau_id') or None
        level = data.get('level', '')
        if niveau_id:
            niveau = session.query(Niveau).filter_by(id=niveau_id).first()
            if niveau:
                level = niveau.name
        f = Formation(
            code=data['code'], name=data['name'],
            level=level, department=data.get('department', ''),
            description=data.get('description', ''),
            pole_id=data.get('pole_id') or None,
            niveau_id=niveau_id,
        )
        session.add(f); session.commit()
        result = f.to_dict(); session.close()
        _invalidate_academic_cache()
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
        for field in ('name', 'level', 'department', 'description', 'is_active', 'pole_id'):
            if field in data: setattr(f, field, data[field])
        if 'niveau_id' in data:
            f.niveau_id = data['niveau_id'] or None
            if f.niveau_id:
                niveau = session.query(Niveau).filter_by(id=f.niveau_id).first()
                if niveau:
                    f.level = niveau.name
        session.commit(); result = f.to_dict(); session.close()
        _invalidate_academic_cache()
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
        _invalidate_academic_cache()
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
        _invalidate_academic_cache()
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
        _invalidate_academic_cache()
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
        _invalidate_academic_cache()
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
                name=data['name'], credits=data.get('credits', 6),
                ue_type=data.get('ue_type', 'obligatoire'))
        session.add(ue); session.commit(); result = ue.to_dict(); session.close()
        _invalidate_academic_cache()
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
        for field in ('name', 'credits', 'ue_type', 'is_active'):
            if field in data: setattr(ue, field, data[field])
        session.commit(); result = ue.to_dict(); session.close()
        _invalidate_academic_cache()
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
        _invalidate_academic_cache()
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
                coefficient=data.get('coefficient', 1),
                cc_percentage=data.get('cc_percentage', 40),
                ex_percentage=data.get('ex_percentage', 60))
        session.add(ec); session.commit(); result = ec.to_dict(); session.close()
        _invalidate_academic_cache()
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
        for field in ('name', 'cm', 'td', 'tp', 'tpe', 'vht', 'coefficient', 'cc_percentage', 'ex_percentage', 'is_active'):
            if field in data: setattr(ec, field, data[field])
        session.commit(); result = ec.to_dict(); session.close()
        _invalidate_academic_cache()
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
        _invalidate_academic_cache()
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
        ec = session.query(EC).filter_by(id=ec_id).first()
        session.add(ECAssignment(ec_id=ec_id, professor_id=prof_id))
        session.commit()
        try:
            from notif_bus import notify_user
            notify_user(prof_id, 'ec_assigned', 'Affecté à un EC',
                         f'Vous avez été affecté à l\'EC « {ec.code} — {ec.name} ».', priority='default', tags=['books'])
        except Exception:
            pass
        session.close()
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
        ec = session.query(EC).filter_by(id=eid).first()
        if not ec:
            session.close(); return jsonify({'error': 'EC non trouvé'}), 404
        if not session.query(User).filter_by(id=prof_id, role=UserRole.PROFESSOR).first():
            session.close(); return jsonify({'error': 'Professeur non trouvé'}), 404
        if session.query(ECAssignment).filter_by(ec_id=eid, professor_id=prof_id).first():
            session.close(); return jsonify({'error': 'Ce professeur est déjà affecté à cet EC'}), 400
        session.add(ECAssignment(ec_id=eid, professor_id=prof_id))
        session.commit()
        try:
            from notif_bus import notify_user
            notify_user(prof_id, 'ec_assigned', 'Affecté à un EC',
                         f'Vous avez été affecté à l\'EC « {ec.code} — {ec.name} ».', priority='default', tags=['books'])
        except Exception:
            pass
        session.close()
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
        ue = session.query(UE).filter_by(id=uid).first()
        session.add(StudentUEEnrollment(student_id=sid, ue_id=uid))
        session.commit()
        try:
            from notif_bus import notify_user
            notify_user(sid, 'ue_enrolled', 'Inscription à une UE',
                         f'Vous avez été inscrit à l\'UE « {ue.code} — {ue.name} ».', priority='default', tags=['bookmark'])
        except Exception:
            pass
        session.close()
        return jsonify({'success': True, 'message': 'Étudiant inscrit avec succès'}), 201
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@formations_bp.route('/api/admin/student_enrollments/bulk', methods=['POST'])
@paseto_required
def enroll_students_bulk():
    """Inscrit plusieurs étudiants à une même UE en un seul appel (Retour #2)
    — même logique unitaire que enroll_student_to_ue, bouclée sur une liste
    de student_ids, pour rendre les inscriptions de classes entières rapides."""
    try:
        session = get_session()
        ok, _ = _is_admin(session)
        if not ok: return jsonify({'error': 'Accès non autorisé'}), 403
        data = request.json or {}
        student_ids = data.get('student_ids') or []
        ue_id = data.get('ue_id')
        if not student_ids or not ue_id:
            session.close(); return jsonify({'error': 'Étudiants et UE requis'}), 400
        ue = session.query(UE).filter_by(id=ue_id).first()
        if not ue:
            session.close(); return jsonify({'error': 'UE non trouvée'}), 404

        enrolled, already, errors = [], [], []
        for sid in student_ids:
            student = session.query(User).filter_by(id=sid, role=UserRole.STUDENT).first()
            if not student:
                errors.append(f"Étudiant {sid} non trouvé"); continue
            if session.query(StudentUEEnrollment).filter_by(student_id=sid, ue_id=ue_id).first():
                already.append(sid); continue
            session.add(StudentUEEnrollment(student_id=sid, ue_id=ue_id))
            enrolled.append(sid)
        session.commit()
        try:
            from notif_bus import notify_user
            for sid in enrolled:
                notify_user(sid, 'ue_enrolled', 'Inscription à une UE',
                             f'Vous avez été inscrit à l\'UE « {ue.code} — {ue.name} ».', priority='default', tags=['bookmark'])
        except Exception:
            pass
        session.close()
        return jsonify({
            'success': True,
            'enrolled': len(enrolled),
            'already_enrolled': len(already),
            'errors': errors,
        }), 201
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
        ue = session.query(UE).filter_by(id=ue_id).first()
        session.add(StudentUEEnrollment(student_id=student_id, ue_id=ue_id))
        session.commit()
        try:
            from notif_bus import notify_user
            notify_user(student_id, 'ue_enrolled', 'Inscription à une UE',
                         f'Vous avez été inscrit à l\'UE « {ue.code} — {ue.name} ».', priority='default', tags=['bookmark'])
        except Exception:
            pass
        session.close()
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


# ═══════════════════════════════════════════════════════════════════════════════
# GROUPES DE SURVEILLANTS ↔ EC (Notes points 6, 7, 9)
# ═══════════════════════════════════════════════════════════════════════════════

@formations_bp.route('/api/admin/proctor_groups', methods=['GET'])
@paseto_required
def list_proctor_groups():
    try:
        session = get_session()
        ok, _ = _is_admin(session)
        if not ok: return jsonify({'error': 'Accès non autorisé'}), 403
        groups = session.query(ProctorGroup).order_by(ProctorGroup.name).all()
        result = [g.to_dict() for g in groups]
        session.close()
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@formations_bp.route('/api/admin/proctor_groups', methods=['POST'])
@paseto_required
def create_proctor_group():
    try:
        session = get_session()
        ok, admin = _is_admin(session)
        if not ok: return jsonify({'error': 'Accès non autorisé'}), 403
        data = request.json or {}
        name = (data.get('name') or '').strip()
        if not name:
            session.close(); return jsonify({'error': 'Nom du groupe requis'}), 400
        group = ProctorGroup(name=name, created_by_id=admin.id)
        session.add(group); session.commit()
        result = group.to_dict()
        session.close()
        return jsonify(result), 201
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@formations_bp.route('/api/admin/proctor_groups/<int:gid>', methods=['PUT'])
@paseto_required
def update_proctor_group(gid):
    try:
        session = get_session()
        ok, _ = _is_admin(session)
        if not ok: return jsonify({'error': 'Accès non autorisé'}), 403
        group = session.query(ProctorGroup).filter_by(id=gid).first()
        if not group: session.close(); return jsonify({'error': 'Groupe non trouvé'}), 404
        data = request.json or {}
        if 'name' in data and data['name'].strip():
            group.name = data['name'].strip()
        session.commit()
        result = group.to_dict()
        session.close()
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@formations_bp.route('/api/admin/proctor_groups/<int:gid>', methods=['DELETE'])
@paseto_required
def delete_proctor_group(gid):
    try:
        session = get_session()
        ok, _ = _is_admin(session)
        if not ok: return jsonify({'error': 'Accès non autorisé'}), 403
        group = session.query(ProctorGroup).filter_by(id=gid).first()
        if not group: session.close(); return jsonify({'error': 'Groupe non trouvé'}), 404
        session.delete(group); session.commit(); session.close()
        return jsonify({'success': True, 'message': 'Groupe supprimé'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@formations_bp.route('/api/admin/proctor_groups/<int:gid>/members', methods=['POST'])
@paseto_required
def add_proctor_group_member(gid):
    try:
        session = get_session()
        ok, _ = _is_admin(session)
        if not ok: return jsonify({'error': 'Accès non autorisé'}), 403
        group = session.query(ProctorGroup).filter_by(id=gid).first()
        if not group: session.close(); return jsonify({'error': 'Groupe non trouvé'}), 404
        data = request.json or {}
        proctor_ids = data.get('proctor_ids') or ([data['proctor_id']] if data.get('proctor_id') else [])
        if not proctor_ids:
            session.close(); return jsonify({'error': 'Surveillant(s) requis'}), 400
        added, already = 0, 0
        for pid in proctor_ids:
            proctor = session.query(User).filter_by(id=pid, role=UserRole.SURVEILLANT).first()
            if not proctor:
                continue
            if session.query(ProctorGroupMember).filter_by(group_id=gid, proctor_id=pid).first():
                already += 1
                continue
            session.add(ProctorGroupMember(group_id=gid, proctor_id=pid))
            added += 1
            try:
                from notif_bus import notify_user
                notify_user(pid, 'proctor_group_added', 'Ajouté à un groupe de surveillance',
                             f'Vous avez été ajouté au groupe « {group.name} ».', priority='default', tags=['busts_in_silhouette'])
            except Exception:
                pass
        session.commit()
        result = group.to_dict()
        session.close()
        return jsonify({'success': True, 'added': added, 'already': already, 'group': result}), 201
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@formations_bp.route('/api/admin/proctor_groups/<int:gid>/members/<int:mid>', methods=['DELETE'])
@paseto_required
def remove_proctor_group_member(gid, mid):
    try:
        session = get_session()
        ok, _ = _is_admin(session)
        if not ok: return jsonify({'error': 'Accès non autorisé'}), 403
        m = session.query(ProctorGroupMember).filter_by(id=mid, group_id=gid).first()
        if not m: session.close(); return jsonify({'error': 'Membre non trouvé'}), 404
        session.delete(m); session.commit(); session.close()
        return jsonify({'success': True, 'message': 'Membre retiré'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@formations_bp.route('/api/admin/proctor_groups/<int:gid>/ecs', methods=['POST'])
@paseto_required
def link_proctor_group_ec(gid):
    try:
        session = get_session()
        ok, _ = _is_admin(session)
        if not ok: return jsonify({'error': 'Accès non autorisé'}), 403
        group = session.query(ProctorGroup).filter_by(id=gid).first()
        if not group: session.close(); return jsonify({'error': 'Groupe non trouvé'}), 404
        data = request.json or {}
        ec_id = data.get('ec_id')
        if not ec_id: session.close(); return jsonify({'error': 'EC requis'}), 400
        ec = session.query(EC).filter_by(id=ec_id).first()
        if not ec:
            session.close(); return jsonify({'error': 'EC non trouvé'}), 404
        if session.query(ProctorGroupEC).filter_by(group_id=gid, ec_id=ec_id).first():
            session.close(); return jsonify({'error': 'Ce groupe est déjà rattaché à cet EC'}), 400
        session.add(ProctorGroupEC(group_id=gid, ec_id=ec_id))
        session.commit()
        try:
            from notif_bus import notify_user
            members = session.query(ProctorGroupMember).filter_by(group_id=gid).all()
            for m in members:
                notify_user(m.proctor_id, 'proctor_group_ec_added', 'Nouvel EC couvert par votre groupe',
                             f'Le groupe « {group.name} » (dont vous faites partie) surveille désormais l\'EC « {ec.code} — {ec.name} ».',
                             priority='default', tags=['bookmark'])
        except Exception:
            pass
        result = group.to_dict()
        session.close()
        return jsonify(result), 201
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@formations_bp.route('/api/admin/proctor_groups/<int:gid>/ecs/<int:ec_id>', methods=['DELETE'])
@paseto_required
def unlink_proctor_group_ec(gid, ec_id):
    try:
        session = get_session()
        ok, _ = _is_admin(session)
        if not ok: return jsonify({'error': 'Accès non autorisé'}), 403
        link = session.query(ProctorGroupEC).filter_by(group_id=gid, ec_id=ec_id).first()
        if not link: session.close(); return jsonify({'error': 'Rattachement non trouvé'}), 404
        session.delete(link); session.commit(); session.close()
        return jsonify({'success': True, 'message': 'Rattachement retiré'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
