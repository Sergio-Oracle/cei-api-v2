"""Application « CEI Examen » (client kiosque équivalent de Safe Exam Browser) : poignée de main signée et distribution.

  GET  /api/exam-client/challenge?exam_id=N     étudiant connecté : nonce à faire signer par le client
  POST /api/exam-client/verify                  vérifie la signature HMAC du client officiel, ouvre la porte « application » pour CET examen
  POST /api/exam_attempts/<id>/client_exit_code personnel (surveillant affecté, enseignant, admin) : code de sortie d'urgence ALÉATOIRE, à usage unique, propre à cet étudiant et cet examen
  POST /api/exam-client/exit                    le client envoie le code saisi ; vérifié ICI (jamais hors ligne), consommé à la première utilisation réussie
  GET  /api/exam-client/releases                public : versions téléchargeables (dossier CLIENT_DIST_DIR/latest.json)
  GET  /api/exam-client/download/<fichier>      public : téléchargement d'un fichier listé dans latest.json (jamais d'autre chemin)

Honnêteté sur la sécurité : la signature prouve que l'appelant connaît le secret embarqué dans le client (CEI_CLIENT_SECRET),
comme la clé de navigateur d'examen de SEB : elle relève le niveau d'effort d'une usurpation, elle ne la rend pas impossible.
"""
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from urllib.parse import quote

from flask import Blueprint, jsonify, request, send_from_directory

from auth_paseto import paseto_required, get_current_user_id, get_current_user_role
from extensions import limiter
from cache import cache_get, cache_set, cache_delete, cache_pop, cache_set_nx_value, _get_client
from models import get_session, User, OnlineExam, ExamAttempt, ExamActivityLog, AttemptStatus, ProctorAssignment
from helpers import utcnow

exam_client_bp = Blueprint('exam_client', __name__)

# Dossier SERVI DIRECTEMENT PAR NGINX (location /static/) : les installateurs (~120 Mo) ne passent jamais par les processus de l'API.
DIST_DIR = os.getenv('CLIENT_DIST_DIR', os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'static', 'downloads'))
_UA_RE = re.compile(r'CEIExamClient/(\d+(?:\.\d+)*)')


def _secret():
    return os.getenv('CEI_CLIENT_SECRET', '')


def _vtuple(v):
    return tuple(int(x) for x in re.findall(r'\d+', v or '0')[:4])


@exam_client_bp.route('/api/exam-client/challenge', methods=['GET'])
@paseto_required
def exam_client_challenge():
    user_id = get_current_user_id()
    nonce = secrets.token_hex(16)
    cache_set(f'cei:clientnonce:{user_id}:{nonce}', 1, 300)
    return jsonify({'nonce': nonce, 'user_id': user_id})


@exam_client_bp.route('/api/exam-client/verify', methods=['POST'])
@paseto_required
def exam_client_verify():
    user_id = get_current_user_id()
    data = request.get_json(silent=True) or {}
    nonce, sig = str(data.get('nonce') or ''), str(data.get('sig') or '')
    version, platform = str(data.get('version') or ''), str(data.get('platform') or '')
    try:
        exam_id = int(data.get('exam_id'))
    except (TypeError, ValueError):
        return jsonify({'error': 'Examen manquant'}), 400
    secret = _secret()
    if not secret:
        return jsonify({'error': "L'application CEI Examen n'est pas encore configurée sur ce serveur."}), 503
    m = _UA_RE.search(request.headers.get('User-Agent', ''))
    if not m:
        return jsonify({'error': "Ce navigateur n'est pas l'application CEI Examen.", 'client_required': True}), 403
    key = f'cei:clientnonce:{user_id}:{nonce}'
    if not nonce or not cache_get(key):
        return jsonify({'error': 'Défi expiré, réessayez.'}), 400
    cache_delete(key)                                   # usage unique
    want = hmac.new(secret.encode(), f'{nonce}|{user_id}|{version}|{platform}'.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(want, sig) or m.group(1) != version:
        return jsonify({'error': "Signature de l'application invalide.", 'client_required': True}), 403
    # Poste utilisé à distance (bureau à distance, partage de contrôle) : refusé pour tout examen exigeant l'application
    if data.get('remote'):
        return jsonify({'error': "Une session de bureau à distance est détectée : l'examen doit être passé directement sur l'ordinateur de l'étudiant.",
                        'client_remote': True}), 403
    if data.get('vm'):
        s = get_session()
        try:
            ex = s.query(OnlineExam).filter_by(id=exam_id).first()
            if ex is not None and getattr(ex, 'client_block_vm', False):
                return jsonify({'error': "Cet examen n'autorise pas les machines virtuelles : utilisez un ordinateur physique.", 'client_vm_refused': True}), 403
        finally:
            s.close()
    minv = os.getenv('CEI_CLIENT_MIN_VERSION', '')
    if minv and _vtuple(version) < _vtuple(minv):
        return jsonify({'error': f"Version {version} trop ancienne : installez la version {minv} ou plus récente.", 'client_outdated': True,
                        'download_url': '/telecharger-client'}), 426
    cache_set(f'cei:client_ok:{user_id}:{exam_id}', {'version': version, 'platform': platform}, 14 * 3600)
    return jsonify({'ok': True, 'version': version, 'vm': bool(data.get('vm'))})


# ── Code de sortie d'urgence : aléatoire, à usage unique, vérifié par le serveur ───────────────────────────────────────────────
EXIT_CODE_TTL = 15 * 60          # secondes : un code non utilisé expire de lui-même
EXIT_MAX_TRIES = 5               # essais ratés avant que le code soit détruit (le surveillant en génère un autre)
_EXIT_ROLES = ('professor', 'admin', 'responsable_examen', 'surveillant')


def _exit_key(exam_id, student_id):
    return f'cei:exitcode:{int(exam_id)}:{int(student_id)}'


def _exit_digest(exam_id, student_id, code):
    """Empreinte du code (jamais le code en clair dans Redis), liée à l'examen et à l'étudiant et au secret du serveur."""
    return hmac.new(_secret().encode(), f'exitcode|{int(exam_id)}|{int(student_id)}|{code}'.encode(), hashlib.sha256).hexdigest()


def _bump_exit_fail(exam_id, student_id):
    """Compte atomique des essais ratés (fenêtre d'une durée de vie de code)."""
    c = _get_client()
    if c is None:
        return 1
    try:
        k = f'cei:exitfail:{int(exam_id)}:{int(student_id)}'
        n = c.incr(k)
        if n == 1:
            c.expire(k, EXIT_CODE_TTL)
        return int(n)
    except Exception:
        return 1


@exam_client_bp.route('/api/exam_attempts/<int:attempt_id>/client_exit_code', methods=['POST'])
@paseto_required
@limiter.limit('30 per minute')
def exam_client_issue_exit_code(attempt_id):
    """Génère un code de sortie d'urgence pour UN étudiant en cours d'examen. Même périmètre que l'envoi d'un avertissement :
    le surveillant ne peut le faire que pour les étudiants qui lui sont affectés, l'enseignant pour ses examens."""
    from services.ec_rights import can_view_exam
    user_id, role = get_current_user_id(), get_current_user_role()
    if role not in _EXIT_ROLES:
        return jsonify({'error': 'Accès réservé aux enseignants et surveillants'}), 403
    if not _secret():
        return jsonify({'error': "L'application CEI Examen n'est pas configurée sur ce serveur."}), 503
    session = get_session()
    try:
        att = session.query(ExamAttempt).filter_by(id=attempt_id).first()
        if not att:
            return jsonify({'error': 'Tentative introuvable'}), 404
        if role == 'professor' and not can_view_exam(session, att.exam, user_id):
            return jsonify({'error': 'Vous ne pouvez agir que sur vos propres examens'}), 403
        if role == 'surveillant':
            assigned = session.query(ProctorAssignment).filter_by(proctor_id=user_id, exam_id=att.exam_id).filter(
                (ProctorAssignment.attempt_id == attempt_id) | (ProctorAssignment.student_id == att.student_id)).first()
            if not assigned:
                return jsonify({'error': 'Cet étudiant ne vous est pas affecté'}), 403
        if att.status != AttemptStatus.IN_PROGRESS:
            return jsonify({'error': "Cet étudiant n'est pas en cours d'examen : aucun code de sortie n'est nécessaire."}), 409
        code = f'{secrets.randbelow(10 ** 8):08d}'
        key = _exit_key(att.exam_id, att.student_id)
        cache_delete(key); cache_delete(f'cei:exitfail:{att.exam_id}:{att.student_id}')      # un seul code actif par étudiant : le nouveau remplace l'ancien
        ok = cache_set_nx_value(key, {'h': _exit_digest(att.exam_id, att.student_id, code), 'by': user_id, 'at': utcnow().isoformat()}, EXIT_CODE_TTL)
        if ok is not True:
            return jsonify({'error': 'Service momentanément indisponible, réessayez dans un instant.'}), 503
        issuer = session.query(User).filter_by(id=user_id).first()
        session.add(ExamActivityLog(attempt_id=att.id, event_type='client_exit_code_issued',
                                    event_data=json.dumps({'by': user_id, 'by_name': issuer.full_name if issuer else '', 'expires_s': EXIT_CODE_TTL}, ensure_ascii=False)))
        session.commit()
        return jsonify({'code': f'{code[:4]}-{code[4:]}', 'expires_in': EXIT_CODE_TTL, 'student_name': att.student.full_name if att.student else ''})
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@exam_client_bp.route('/api/exam-client/exit', methods=['POST'])
@limiter.limit('60 per minute')
def exam_client_exit():
    """Vérifie le code de sortie saisi dans l'application. Appelé par le client lui-même (signature HMAC du secret embarqué + horodatage),
    pas par la page : l'étudiant ne peut donc ni lire ni contourner la vérification. Usage unique : la première réussite détruit le code."""
    data = request.get_json(silent=True) or {}
    secret = _secret()
    if not secret:
        return jsonify({'error': "L'application CEI Examen n'est pas configurée sur ce serveur."}), 503
    try:
        exam_id, student_id, ts = int(data.get('exam_id')), int(data.get('user_id')), int(data.get('ts'))
    except (TypeError, ValueError):
        return jsonify({'error': 'Demande invalide'}), 400
    code = re.sub(r'\D', '', str(data.get('code') or ''))
    version, platform, sig = str(data.get('version') or ''), str(data.get('platform') or ''), str(data.get('sig') or '')
    m = _UA_RE.search(request.headers.get('User-Agent', ''))
    want = hmac.new(secret.encode(), f'exit|{exam_id}|{student_id}|{data.get("code")}|{ts}|{version}|{platform}'.encode(), hashlib.sha256).hexdigest()
    if not m or m.group(1) != version or not hmac.compare_digest(want, sig) or abs(time.time() - ts / 1000.0) > 120:
        return jsonify({'error': 'Demande refusée.'}), 403
    key = _exit_key(exam_id, student_id)
    fails = _get_client().get(f'cei:exitfail:{exam_id}:{student_id}') if _get_client() else None
    if fails is not None and int(fails) >= EXIT_MAX_TRIES:
        cache_delete(key)
        return jsonify({'error': "Trop d'essais : ce code est annulé. Demandez-en un nouveau au surveillant.", 'locked': True}), 429
    stored = cache_get(key)
    session = get_session()
    try:
        att = session.query(ExamAttempt).filter_by(exam_id=exam_id, student_id=student_id).order_by(ExamAttempt.id.desc()).first()

        def _log(evt, extra=None):
            if att is not None:
                session.add(ExamActivityLog(attempt_id=att.id, event_type=evt, event_data=json.dumps(extra or {}, ensure_ascii=False)))
                session.commit()

        good = bool(stored) and len(code) == 8 and hmac.compare_digest(stored.get('h', ''), _exit_digest(exam_id, student_id, code))
        if not good:
            n = _bump_exit_fail(exam_id, student_id)
            _log('client_quit_code_failed', {'tries': n, 'expired_or_missing': not stored, 'via': 'server'})
            msg = 'Code incorrect' if stored else "Aucun code valide : il a expiré ou n'a pas été demandé au surveillant."
            return jsonify({'error': msg, 'tries_left': max(0, EXIT_MAX_TRIES - n)}), 403
        if cache_pop(key) is None:                                            # déjà consommé par une requête concurrente
            return jsonify({'error': 'Ce code a déjà été utilisé.'}), 403
        cache_delete(f'cei:exitfail:{exam_id}:{student_id}')
        _log('client_quit_code_used', {'by_code_issued_by': stored.get('by'), 'via': 'server'})
        try:
            import notif_bus
            name = att.student.full_name if att is not None and att.student else f'étudiant {student_id}'
            notif_bus.notify_exam(exam_id, 'client_exit_used', "Sortie d'urgence de CEI Examen", f"{name} a quitté l'application avec un code de sortie.", priority='high')
        except Exception:
            pass
        return jsonify({'ok': True})
    finally:
        session.close()


def _latest():
    try:
        with open(os.path.join(DIST_DIR, 'latest.json'), encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {'version': None, 'files': []}


@exam_client_bp.route('/api/exam-client/releases', methods=['GET'])
def exam_client_releases():
    d = _latest()
    files = []
    for x in d.get('files', []):
        p = os.path.join(DIST_DIR, x.get('name', ''))
        if os.path.isfile(p):
            files.append({'name': x['name'], 'platform': x.get('platform', ''), 'arch': x.get('arch', ''), 'kind': x.get('kind', ''), 'label': x.get('label', x['name']), 'size': os.path.getsize(p),
                          'sha256': x.get('sha256', ''), 'url': '/static/downloads/' + quote(x['name'])})
    return jsonify({'version': d.get('version'), 'files': files, 'notes': d.get('notes', '')})


@exam_client_bp.route('/api/exam-client/download/<path:name>', methods=['GET'])
def exam_client_download(name):
    listed = {x.get('name') for x in _latest().get('files', [])}
    if name not in listed or '/' in name or name.startswith('.'):
        return jsonify({'error': 'Fichier introuvable'}), 404
    return send_from_directory(DIST_DIR, name, as_attachment=True)
