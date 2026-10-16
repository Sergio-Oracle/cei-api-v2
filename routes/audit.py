"""Journal d'activité des comptes à privilèges — fonctionnalité DISCRÈTE.

Aucune page, aucun menu, aucune documentation publique n'y mène. Pour tout compte qui n'est pas le
propriétaire de la plateforme (voir services.audit.viewer_ids), chaque route répond exactement comme une
route inexistante (404 « Ressource introuvable ») : rien ne révèle que la fonction existe.

Routes (lecture seule — aucune route ne modifie ni ne supprime une trace) :
  GET /api/audit/access      accès autorisé ?
  GET /api/audit/accounts    administrateurs et responsables d'examen : en ligne, dernière activité, volumes
  GET /api/audit/activity    journal filtrable (compte, nature, dates, texte, refus seulement)
  GET /api/audit/export      même journal en CSV
"""
import csv
import io
import json
from datetime import datetime, timedelta

from flask import Blueprint, Response, jsonify, request
from sqlalchemy import func, or_

from auth_paseto import get_current_user_id, get_current_user_role, paseto_required
from extensions import limiter
from models import StaffAuditLog, User, UserRole, get_session
from services.audit import AUDITED_ROLES, ONLINE_SECONDS, _redis, viewer_ids

audit_bp = Blueprint('audit', __name__)

KINDS = ('create', 'update', 'delete', 'read', 'login', 'login_failed', 'logout', 'other')


def _hidden():
    return jsonify({'error': 'Ressource introuvable'}), 404


def _allowed(session) -> bool:
    return get_current_user_role() == 'admin' and get_current_user_id() in viewer_ids(session)


def _day(raw, end=False):
    if not raw:
        return None
    try:
        d = datetime.strptime(raw[:10], '%Y-%m-%d')
    except ValueError:
        return None
    return d + timedelta(days=1) if end else d


def _filtered(session):
    q = session.query(StaffAuditLog)
    a = request.args
    if a.get('user_id', type=int):
        q = q.filter(StaffAuditLog.user_id == a.get('user_id', type=int))
    if a.get('role') in AUDITED_ROLES:
        q = q.filter(StaffAuditLog.role == a.get('role'))
    kinds = [k for k in (a.get('kind') or '').split(',') if k in KINDS]
    if kinds:
        q = q.filter(StaffAuditLog.kind.in_(kinds))
    if _day(a.get('date_from')):
        q = q.filter(StaffAuditLog.created_at >= _day(a.get('date_from')))
    if _day(a.get('date_to')):
        q = q.filter(StaffAuditLog.created_at < _day(a.get('date_to'), end=True))
    if a.get('failures') in ('1', 'true'):
        q = q.filter(or_(StaffAuditLog.status_code >= 400, StaffAuditLog.kind == 'login_failed'))
    text = (a.get('q') or '').strip()
    if text:
        like = f'%{text}%'
        q = q.filter(or_(StaffAuditLog.label.ilike(like), StaffAuditLog.target_label.ilike(like),
                         StaffAuditLog.user_name.ilike(like), StaffAuditLog.path.ilike(like),
                         StaffAuditLog.details.ilike(like)))
    return q


def _row(r):
    try:
        details = json.loads(r.details) if r.details else None
    except ValueError:
        details = None
    return {
        'id': r.id, 'created_at': r.created_at, 'user_id': r.user_id, 'user_name': r.user_name,
        'user_email': r.user_email, 'role': r.role, 'kind': r.kind, 'label': r.label, 'method': r.method,
        'path': r.path, 'status_code': r.status_code, 'target_type': r.target_type, 'target_id': r.target_id,
        'target_label': r.target_label, 'ip': r.ip, 'user_agent': r.user_agent, 'details': details,
    }


@audit_bp.route('/api/audit/access', methods=['GET'])
@paseto_required
@limiter.exempt
def audit_access():
    session = get_session()
    try:
        return jsonify({'allowed': True}) if _allowed(session) else _hidden()
    finally:
        session.close()


@audit_bp.route('/api/audit/accounts', methods=['GET'])
@paseto_required
@limiter.exempt
def audit_accounts():
    session = get_session()
    try:
        if not _allowed(session):
            return _hidden()
        users = (session.query(User).filter(User.role.in_([UserRole.ADMIN, UserRole.RESPONSABLE_EXAMEN]))
                 .order_by(User.role, func.lower(User.full_name)).all())
        ids = [u.id for u in users]
        today = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        day_ago = datetime.utcnow() - timedelta(hours=24)
        totals = dict(session.query(StaffAuditLog.user_id, func.count(StaffAuditLog.id))
                      .filter(StaffAuditLog.user_id.in_(ids), StaffAuditLog.kind.notin_(['login', 'login_failed', 'logout', 'read']))
                      .group_by(StaffAuditLog.user_id).all()) if ids else {}
        todays = dict(session.query(StaffAuditLog.user_id, func.count(StaffAuditLog.id))
                      .filter(StaffAuditLog.user_id.in_(ids), StaffAuditLog.created_at >= today,
                              StaffAuditLog.kind.notin_(['login', 'login_failed', 'logout', 'read']))
                      .group_by(StaffAuditLog.user_id).all()) if ids else {}
        failed = dict(session.query(StaffAuditLog.user_id, func.count(StaffAuditLog.id))
                      .filter(StaffAuditLog.user_id.in_(ids), StaffAuditLog.created_at >= day_ago,
                              StaffAuditLog.kind == 'login_failed').group_by(StaffAuditLog.user_id).all()) if ids else {}
        last_ids = dict(session.query(StaffAuditLog.user_id, func.max(StaffAuditLog.id))
                        .filter(StaffAuditLog.user_id.in_(ids)).group_by(StaffAuditLog.user_id).all()) if ids else {}
        last_rows = {r.user_id: r for r in session.query(StaffAuditLog).filter(StaffAuditLog.id.in_(list(last_ids.values()))).all()} if last_ids else {}
        r = _redis()
        now = datetime.utcnow()
        out = []
        for u in users:
            seen = None
            if r is not None:
                try:
                    raw = r.get(f'cei:audit:seen:{u.id}')
                    seen = datetime.fromisoformat(raw) if raw else None
                except Exception:
                    seen = None
            last = last_rows.get(u.id)
            if seen is None and last is not None:
                seen = last.created_at
            online = bool(seen and (now - seen).total_seconds() <= ONLINE_SECONDS)
            out.append({
                'id': u.id, 'name': u.full_name, 'email': u.email, 'role': u.role.value, 'is_active': u.is_active,
                'last_login': u.last_login, 'last_seen': seen, 'online': online,
                'actions_total': totals.get(u.id, 0), 'actions_today': todays.get(u.id, 0),
                'failed_logins_24h': failed.get(u.id, 0),
                'last_action': ({'label': last.label, 'target_label': last.target_label, 'at': last.created_at, 'ip': last.ip}
                                if last else None),
            })
        return jsonify({'accounts': out, 'online': sum(1 for a in out if a['online'])})
    finally:
        session.close()


@audit_bp.route('/api/audit/activity', methods=['GET'])
@paseto_required
@limiter.exempt
def audit_activity():
    session = get_session()
    try:
        if not _allowed(session):
            return _hidden()
        page = max(1, request.args.get('page', 1, type=int))
        limit = min(100, max(1, request.args.get('limit', 50, type=int)))
        q = _filtered(session)
        total = q.count()
        rows = q.order_by(StaffAuditLog.id.desc()).offset((page - 1) * limit).limit(limit).all()
        return jsonify({'rows': [_row(r) for r in rows], 'total': total, 'page': page, 'limit': limit,
                        'total_pages': max(1, (total + limit - 1) // limit)})
    finally:
        session.close()


@audit_bp.route('/api/audit/export', methods=['GET'])
@paseto_required
@limiter.exempt
def audit_export():
    session = get_session()
    try:
        if not _allowed(session):
            return _hidden()
        rows = _filtered(session).order_by(StaffAuditLog.id.desc()).limit(20000).all()
        buf = io.StringIO()
        w = csv.writer(buf, delimiter=';')
        w.writerow(['Date (UTC)', 'Compte', 'E-mail', 'Rôle', 'Nature', 'Action', 'Cible', 'Statut HTTP', 'Adresse IP', 'Détails'])
        for r in rows:
            w.writerow([r.created_at.strftime('%Y-%m-%d %H:%M:%S') if r.created_at else '', r.user_name, r.user_email, r.role,
                        r.kind, r.label, r.target_label or '', r.status_code, r.ip, r.details or ''])
        data = '﻿' + buf.getvalue()
        return Response(data, mimetype='text/csv; charset=utf-8',
                        headers={'Content-Disposition': f'attachment; filename=journal_activite_{datetime.utcnow():%Y%m%d_%H%M}.csv'})
    finally:
        session.close()
