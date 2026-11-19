"""Test technique de l'étudiant avant l'examen (18/11/2026) — voir services/tech_test.py.

  POST /api/tech_test/livekit_token         jeton pour une salle PRIVÉE de test (jamais la salle d'examen)
  POST /api/tech_test/verify                vérifie CÔTÉ SERVEUR ce que le serveur vidéo reçoit de l'étudiant (caméra, micro, écran)
  POST /api/tech_test/result                enregistre le résultat (statut recalculé par le serveur)
  GET  /api/tech_test/me                    dernier résultat de l'étudiant connecté
  GET  /api/online_exams/<id>/readiness     surveillant / enseignant : prêts, à surveiller, non prêts, jamais testés
  GET  /api/online_exams/<id>/vitals        administrateur / responsable / enseignant : santé de l'examen en cours (voir services/exam_vitals.py)
"""
import json
import time
import urllib.request as urlreq

import jwt as pyjwt
from flask import Blueprint, jsonify, request

from auth_paseto import paseto_required, get_current_user_id, get_current_user_role
from cache import _get_client
from extensions import limiter
from models import (get_session, OnlineExam, User, UserRole, ExamProctor, ProctorAssignment, StudentUEEnrollment)
from services import tech_test as TT
from services.ec_rights import can_view_exam

tech_test_bp = Blueprint('tech_test', __name__)
STAFF = ('professor', 'admin', 'responsable_examen', 'surveillant', 'superviseur')


def _cache_get(key):
    from cache import cache_get
    try:
        return cache_get(key)
    except Exception:
        return None


def _cache_set(key, value, ttl):
    from cache import cache_set
    try:
        cache_set(key, value, ttl)
    except Exception:
        pass


def _lk():
    import os
    from proctoring_routes import get_livekit_config
    return get_livekit_config()


def _room(uid):
    return f'techtest-{int(uid)}'


def _rec_key(uid):
    return f'cei:techtest:{int(uid)}'


def _verify_key(uid):
    return f'cei:techtest:verify:{int(uid)}'


def _student_view(problems):
    """Ce que reçoit l'étudiant : le message simple seulement (le détail technique `staff` reste réservé au surveillant)."""
    return [{k: v for k, v in p.items() if k != 'staff'} for p in (problems or [])]


def list_participants(room):
    """Participants de la salle vus par le serveur vidéo (API d'administration). None si injoignable."""
    cfg = _lk()
    if not (cfg['api_url'] and cfg['api_key'] and cfg['api_secret']):
        return None
    tok = pyjwt.encode({'iss': cfg['api_key'], 'exp': int(time.time()) + 60, 'video': {'roomAdmin': True, 'room': room}}, cfg['api_secret'], algorithm='HS256')
    req = urlreq.Request(cfg['api_url'].rstrip('/') + '/twirp/livekit.RoomService/ListParticipants', data=json.dumps({'room': room}).encode(),
                         headers={'Authorization': 'Bearer ' + tok, 'Content-Type': 'application/json'})
    try:
        with urlreq.urlopen(req, timeout=8) as r:
            return json.load(r).get('participants', [])
    except Exception:
        return None


@tech_test_bp.route('/api/tech_test/livekit_token', methods=['POST'])
@paseto_required
@limiter.exempt
def tech_test_token():
    uid = get_current_user_id()
    cfg = _lk()
    if not all([cfg['url'], cfg['api_key'], cfg['api_secret']]):
        return jsonify({'error': 'LiveKit non configuré sur le serveur'}), 503
    from proctoring_routes import generate_livekit_token
    tok = generate_livekit_token(cfg['api_key'], cfg['api_secret'], f'techtest-{uid}', _room(uid), can_publish=True, can_subscribe=False, ttl=900)
    return jsonify({'ws_url': cfg['url'], 'token': tok})


@tech_test_bp.route('/api/tech_test/verify', methods=['POST'])
@paseto_required
@limiter.exempt
def tech_test_verify():
    uid = get_current_user_id()
    parts = list_participants(_room(uid))
    if parts is None:
        return jsonify({'error': 'Serveur vidéo injoignable', 'reachable': False}), 503
    mine = next((p for p in parts if p.get('identity') == f'techtest-{uid}'), None)
    live = TT.summarize_participant(mine)
    live['connected'] = mine is not None
    c = _get_client()
    if c is not None and mine is not None:
        try:
            c.setex(_verify_key(uid), TT.VERIFY_TTL, json.dumps(live))
        except Exception:
            pass
    return jsonify({'reachable': True, **live})


@tech_test_bp.route('/api/tech_test/result', methods=['POST'])
@paseto_required
@limiter.exempt
def tech_test_result():
    uid = get_current_user_id()
    data = request.get_json(silent=True) or {}
    checks = data.get('checks') if isinstance(data.get('checks'), dict) else {}
    c = _get_client()
    live = None
    if c is not None:
        try:
            raw = c.get(_verify_key(uid))
            live = json.loads(raw) if raw else None
        except Exception:
            live = None
    status, problems = TT.evaluate(checks, live)
    rec = {'ts': time.time(), 'status': status, 'problems': problems, 'live': live,
           'client': {k: (checks.get('client') or {}).get(k) for k in ('is_client', 'version', 'platform')} if isinstance(checks.get('client'), dict) else {},
           'ua': str(data.get('ua') or '')[:160],
           'camera': {k: checks['camera'].get(k) for k in ('width', 'height', 'brightness', 'face')} if isinstance(checks.get('camera'), dict) else {},
           'network': {k: checks['network'].get(k) for k in ('latency_ms', 'down_mbps')} if isinstance(checks.get('network'), dict) else {}}
    if c is not None:
        try:
            c.setex(_rec_key(uid), TT.KEY_TTL, TT.dumps(rec))
        except Exception:
            pass
    return jsonify({'status': status, 'problems': _student_view(problems)})


@tech_test_bp.route('/api/tech_test/me', methods=['GET'])
@paseto_required
@limiter.exempt
def tech_test_me():
    uid = get_current_user_id()
    c = _get_client()
    rec = None
    if c is not None:
        try:
            raw = c.get(_rec_key(uid))
            rec = json.loads(raw) if raw else None
        except Exception:
            rec = None
    if rec:
        rec = {**rec, 'problems': _student_view(rec.get('problems'))}
    return jsonify({'status': TT.status_of_record(rec), 'record': rec})


@tech_test_bp.route('/api/online_exams/<int:exam_id>/readiness', methods=['GET'])
@paseto_required
@limiter.exempt
def exam_readiness(exam_id):
    user_id, role = get_current_user_id(), get_current_user_role()
    if role not in STAFF:
        return jsonify({'error': 'Accès réservé au personnel'}), 403
    s = get_session()
    try:
        exam = s.query(OnlineExam).filter_by(id=exam_id).first()
        if not exam:
            return jsonify({'error': 'Examen introuvable'}), 404
        if role == 'surveillant' and not s.query(ExamProctor.id).filter_by(exam_id=exam_id, proctor_id=user_id).first():
            return jsonify({'error': "Vous n'êtes pas affecté à cet examen"}), 403
        if role == 'professor' and not can_view_exam(s, exam, user_id):
            return jsonify({'error': 'Accès refusé'}), 403
        ec = exam.subject.ec if exam.subject else None
        if not ec or not ec.ue_id:
            return jsonify({'counts': {}, 'students': [], 'total': 0})
        mine_set = set()
        if role == 'surveillant':
            mine_set = {r[0] for r in s.query(ProctorAssignment.student_id).filter(ProctorAssignment.exam_id == exam_id, ProctorAssignment.proctor_id == user_id, ProctorAssignment.student_id.isnot(None))}
        cache_key = f'cei:readiness:{exam_id}:{"all" if not (role == "surveillant" and mine_set) else "scoped"}'
        # Liste de TOUS les inscrits avec leur statut : calculée une fois par minute et partagée (jusqu'à 5 000 lignes + autant de lectures Redis, rappelées par chaque personne
        # ayant la page ouverte, toutes les minutes). Chaque surveillant n'en prend que sa part, en mémoire.
        full = _cache_get(f'cei:readiness:{exam_id}:full')
        if full is None:
            rows = (s.query(User.id, User.full_name, User.email).join(StudentUEEnrollment, StudentUEEnrollment.student_id == User.id)
                    .filter(StudentUEEnrollment.ue_id == ec.ue_id, User.is_active == True, User.role == UserRole.STUDENT).all())   # noqa: E712
            c = _get_client()
            recs = {}
            if c is not None and rows:
                try:
                    pipe = c.pipeline()
                    for r in rows:
                        pipe.get(_rec_key(r.id))
                    for r, raw in zip(rows, pipe.execute()):
                        if raw:
                            recs[r.id] = json.loads(raw)
                except Exception:
                    recs = {}
            full = []
            for r in rows:
                rec = recs.get(r.id) or {}
                full.append({'student_id': r.id, 'name': r.full_name, 'email': r.email, 'status': TT.status_of_record(recs.get(r.id)),
                             'tested_at': rec.get('ts'), 'problems': [p.get('staff') or p['message'] for p in (rec.get('problems') or [])][:3]})
            _cache_set(f'cei:readiness:{exam_id}:full', full, 60)
        if role == 'surveillant' and mine_set:
            full = [x for x in full if x['student_id'] in mine_set]
        counts = {'ready': 0, 'warn': 0, 'not_ready': 0, 'stale': 0, 'never': 0}
        out = []
        for x in full:
            counts[x['status']] = counts.get(x['status'], 0) + 1
            if x['status'] != 'ready':
                out.append(x)
        order = {'not_ready': 0, 'warn': 1, 'stale': 2, 'never': 3}
        out.sort(key=lambda x: (order.get(x['status'], 9), x['name'] or ''))
        return jsonify({'total': len(full), 'counts': counts, 'students': out[:400], 'truncated': len(out) > 400})
    finally:
        s.close()


@tech_test_bp.route('/api/online_exams/<int:exam_id>/vitals', methods=['GET'])
@paseto_required
@limiter.exempt
def exam_vitals(exam_id):
    """Santé de l'examen (entrée en examen, liaison vidéo, hors ligne, alertes, appels) et verdict. Pas pour les surveillants (ils ont la vue de leur salle)."""
    user_id, role = get_current_user_id(), get_current_user_role()
    if role not in ('admin', 'responsable_examen', 'professor', 'superviseur'):
        return jsonify({'error': 'Accès réservé'}), 403
    s = get_session()
    try:
        exam = s.query(OnlineExam).filter_by(id=exam_id).first()
        if not exam:
            return jsonify({'error': 'Examen introuvable'}), 404
        if role == 'professor' and not can_view_exam(s, exam, user_id):
            return jsonify({'error': 'Accès refusé'}), 403
        from services import exam_vitals as EV
        from cache import cache_get, cache_set
        key = f'cei:vitals:cache:{exam_id}'
        h = cache_get(key)                                   # résultat partagé : N personnes qui regardent = UN seul calcul par 45 s
        if not h:
            try:
                h = EV.compute(s, exam_id)
                cache_set(key, h, EV.CACHE_S)
            except Exception:
                return jsonify({'error': 'Santé momentanément indisponible'}), 503
        return jsonify(h)
    finally:
        s.close()
