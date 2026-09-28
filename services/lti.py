"""
CEI outil externe LTI 1.3 dans Moodle (phase 6 de la feuille de route).

Organisation retenue (décision du 28/09) : UNE activité « Examens CEI » par
cours Moodle. L'étudiant qui l'ouvre arrive sur la liste de ses examens CEI
de l'EC correspondant (cours Moodle = EC par son code) ; CEI crée lui-même
une colonne de notes par examen dans le carnet Moodle (LTI Assignment and
Grade Services) et y dépose les notes à la publication des résultats.

Chaque plateforme LTI est une MoodleInstance : issuer = base_url, points
d'accès Moodle standard (/mod/lti/auth.php, token.php, certs.php), client_id
et deployment_id saisis par l'admin après avoir enregistré CEI dans Moodle.
La clé RSA de CEI (signature des demandes de jeton) est générée au premier
usage dans lti_private.pem, propre à chaque serveur ; sa clé publique est
exposée en JWKS (/api/lti/jwks), que Moodle relit lui-même.
"""
import hashlib
import json
import os
import secrets
import stat
import threading
import time
import uuid
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit, urlencode, parse_qsl

import jwt
import requests
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt import PyJWKClient

from cache import cache_get, cache_set

LTI = 'https://purl.imsglobal.org/spec/lti/claim/'
AGS_CLAIM = 'https://purl.imsglobal.org/spec/lti-ags/claim/endpoint'
AGS_SCOPES = ('https://purl.imsglobal.org/spec/lti-ags/scope/lineitem '
              'https://purl.imsglobal.org/spec/lti-ags/scope/score')
SCORE_MAX = 20            # toutes les notes CEI sont sur 20
RESOURCE_PREFIX = 'cei-exam-'
TIMEOUT = 20

_KEY_PATH = os.getenv('LTI_PRIVATE_KEY_PATH',
                      os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'lti_private.pem'))
_key_lock = threading.Lock()
_private_key = None
_jwks_clients: dict = {}


class LtiError(Exception):
    """Lancement ou échange LTI refusé ; le message est destiné à l'utilisateur."""


# ── Clé de l'outil ──────────────────────────────────────────────────────────

def private_key():
    global _private_key
    if _private_key is not None:
        return _private_key
    with _key_lock:
        if _private_key is None:
            if not os.path.exists(_KEY_PATH):
                key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
                pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                        serialization.NoEncryption())
                fd = os.open(_KEY_PATH, os.O_WRONLY | os.O_CREAT | os.O_EXCL, stat.S_IRUSR | stat.S_IWUSR)
                with os.fdopen(fd, 'wb') as fh:
                    fh.write(pem)
            with open(_KEY_PATH, 'rb') as fh:
                _private_key = serialization.load_pem_private_key(fh.read(), password=None)
    return _private_key


def key_id() -> str:
    der = private_key().public_key().public_bytes(serialization.Encoding.DER,
                                                  serialization.PublicFormat.SubjectPublicKeyInfo)
    return hashlib.sha256(der).hexdigest()[:16]


def jwks() -> dict:
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private_key().public_key()))
    jwk.update({'kid': key_id(), 'alg': 'RS256', 'use': 'sig'})
    return {'keys': [jwk]}


# ── Plateformes ─────────────────────────────────────────────────────────────

def auth_url(inst) -> str:   return f"{inst.base_url}/mod/lti/auth.php"
def token_url(inst) -> str:  return f"{inst.base_url}/mod/lti/token.php"
def certs_url(inst) -> str:  return f"{inst.base_url}/mod/lti/certs.php"


def platform_for(session, issuer: str, client_id: str | None = None):
    from models import MoodleInstance
    issuer = (issuer or '').rstrip('/')
    q = session.query(MoodleInstance).filter(MoodleInstance.base_url == issuer,
                                             MoodleInstance.is_active.is_(True),
                                             MoodleInstance.lti_client_id.isnot(None))
    if client_id:
        q = q.filter(MoodleInstance.lti_client_id == client_id)
    inst = q.first()
    if not inst or not inst.lti_deployment_id:
        raise LtiError("Cette plateforme Moodle n'est pas configurée pour CEI (LTI). "
                       "L'administrateur CEI doit renseigner l'identifiant client et le déploiement dans la page Moodle.")
    return inst


# ── Enregistrement dynamique (LTI Advantage Dynamic Registration) ──────────

TOOL_CONFIG = 'https://purl.imsglobal.org/spec/lti-tool-configuration'


def register_dynamic(session, openid_configuration: str, registration_token: str, tool_base: str):
    """L'admin colle l'adresse d'enregistrement de CEI dans Moodle ; Moodle
    ouvre /api/lti/register avec son adresse de configuration. CEI s'y
    enregistre et retient lui-même l'identifiant client et le déploiement :
    rien à recopier. Seules les plateformes déjà ajoutées dans CEI (page
    Moodle) sont acceptées, et CEI ne contacte que leur propre adresse."""
    from models import MoodleInstance
    host = urlsplit(openid_configuration or '').netloc.lower()
    inst = next((i for i in session.query(MoodleInstance).filter_by(is_active=True).all()
                 if urlsplit(i.base_url).netloc.lower() == host), None) if host else None
    if not inst:
        raise LtiError("Cette plateforme Moodle n'est pas encore déclarée dans CEI. Ajoutez-la d'abord dans "
                       "CEI (Administration → Moodle → Plateformes), puis recommencez l'enregistrement.")
    r = requests.get(openid_configuration, timeout=TIMEOUT)
    if r.status_code != 200:
        raise LtiError(f"Configuration Moodle illisible ({r.status_code}).")
    cfg = r.json()
    if (cfg.get('issuer') or '').rstrip('/') != inst.base_url:
        raise LtiError("L'émetteur annoncé par Moodle ne correspond pas à la plateforme déclarée dans CEI.")
    domain = urlsplit(tool_base).netloc
    payload = {
        'application_type': 'web',
        'response_types': ['id_token'],
        'grant_types': ['implicit', 'client_credentials'],
        'initiate_login_uri': f"{tool_base}/api/lti/login",
        'redirect_uris': [f"{tool_base}/api/lti/launch"],
        'client_name': "CEI — Centre d'Examen Intelligent",
        'jwks_uri': f"{tool_base}/api/lti/jwks",
        'token_endpoint_auth_method': 'private_key_jwt',
        'scope': AGS_SCOPES,
        TOOL_CONFIG: {
            'domain': domain,
            'target_link_uri': f"{tool_base}/api/lti/launch",
            'description': "Examens en ligne surveillés de l'UNCHK (CEI).",
            'claims': ['iss', 'sub', 'name', 'given_name', 'family_name', 'email'],
            'messages': [{'type': 'LtiResourceLinkRequest', 'target_link_uri': f"{tool_base}/api/lti/launch",
                          'label': 'CEI'}],
        },
    }
    headers = {'Content-Type': 'application/json', 'Accept': 'application/json'}
    if registration_token:
        headers['Authorization'] = f"Bearer {registration_token}"
    r = requests.post(cfg['registration_endpoint'], json=payload, headers=headers, timeout=TIMEOUT)
    if r.status_code >= 300:
        raise LtiError(f"Moodle refuse l'enregistrement ({r.status_code}) : {r.text[:300]}")
    reg = r.json()
    deployment = str((reg.get(TOOL_CONFIG) or {}).get('deployment_id') or '')
    if not reg.get('client_id') or not deployment:
        raise LtiError("Réponse d'enregistrement Moodle incomplète (identifiant client ou déploiement absent).")
    inst.lti_client_id, inst.lti_deployment_id = reg['client_id'], deployment
    if deployment.isdigit():
        inst.lti_type_id = int(deployment)   # Moodle : deployment_id = id de l'outil
    session.commit()
    return inst


def return_url(inst, claims) -> str:
    """Adresse de retour vers Moodle (bouton « Retour à Moodle », déconnexion,
    flèche Retour) : celle fournie par Moodle si elle est bien sur la
    plateforme, sinon la page du cours."""
    url = ((claims.get(LTI + 'launch_presentation') or {}).get('return_url') or '').strip()
    if url and urlsplit(url).netloc.lower() == urlsplit(inst.base_url).netloc.lower():
        return url
    course_id = str((claims.get(LTI + 'context') or {}).get('id') or '')
    return f"{inst.base_url}/course/view.php?id={course_id}" if course_id.isdigit() else f"{inst.base_url}/my/"


# ── Connexion (OIDC third-party initiated login) ────────────────────────────

def login_redirect(session, params: dict, launch_url: str) -> str:
    inst = platform_for(session, params.get('iss'), params.get('client_id'))
    state, nonce = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    cache_set(f"cei:lti:state:{state}", {'nonce': nonce, 'instance_id': inst.id}, ttl=300)
    query = {
        'scope': 'openid', 'response_type': 'id_token', 'response_mode': 'form_post', 'prompt': 'none',
        'client_id': inst.lti_client_id, 'redirect_uri': launch_url,
        'login_hint': params.get('login_hint', ''), 'state': state, 'nonce': nonce,
    }
    if params.get('lti_message_hint'):
        query['lti_message_hint'] = params['lti_message_hint']
    return f"{auth_url(inst)}?{urlencode(query)}"


def validate_launch(session, id_token: str, state: str) -> tuple:
    """(plateforme, claims) d'un lancement valide. Le state est à usage
    unique et porte le nonce attendu ; signature vérifiée sur les clés
    publiques de la plateforme."""
    from models import MoodleInstance
    from cache import cache_delete
    entry = cache_get(f"cei:lti:state:{state}") if state else None
    if not entry:
        raise LtiError("Lien de lancement expiré. Rouvrez l'activité depuis Moodle.")
    cache_delete(f"cei:lti:state:{state}")
    inst = session.get(MoodleInstance, entry['instance_id'])
    if not inst:
        raise LtiError("Plateforme Moodle inconnue.")
    client = _jwks_clients.setdefault(certs_url(inst), PyJWKClient(certs_url(inst), cache_keys=True, lifespan=3600))
    try:
        key = client.get_signing_key_from_jwt(id_token).key
        claims = jwt.decode(id_token, key, algorithms=['RS256'], audience=inst.lti_client_id,
                            issuer=inst.base_url, options={'require': ['exp', 'iat', 'sub']})
    except jwt.PyJWTError as e:
        raise LtiError(f"Lancement refusé : jeton Moodle invalide ({e}).")
    if claims.get('nonce') != entry['nonce']:
        raise LtiError("Lancement refusé : nonce invalide.")
    if str(claims.get(LTI + 'deployment_id')) != str(inst.lti_deployment_id):
        raise LtiError("Lancement refusé : déploiement Moodle non reconnu par CEI.")
    if claims.get(LTI + 'version') != '1.3.0':
        raise LtiError("Lancement refusé : seule la version LTI 1.3 est prise en charge.")
    if claims.get(LTI + 'message_type') != 'LtiResourceLinkRequest':
        raise LtiError("Type de lancement non pris en charge par CEI.")
    _learn_type_id(session, inst, claims)
    return inst, claims


def _learn_type_id(session, inst, claims) -> None:
    """Moodle indique dans le lancement l'URL des colonnes de notes, qui porte
    l'id de l'outil (type_id) : on le retient s'il n'a pas été saisi."""
    url = (claims.get(AGS_CLAIM) or {}).get('lineitems') or ''
    type_id = dict(parse_qsl(urlsplit(url).query)).get('type_id')
    if type_id and type_id.isdigit() and not inst.lti_type_id:
        inst.lti_type_id = int(type_id)
        session.commit()


def is_instructor(claims) -> bool:
    roles = claims.get(LTI + 'roles') or []
    return any(r.endswith(('#Instructor', '#Administrator', '#ContentDeveloper', '#TeachingAssistant'))
               or '/membership/Instructor' in r for r in roles)


# ── Jeton de service et colonnes de notes (AGS) ────────────────────────────

def service_token(inst) -> str:
    key = f"cei:lti:token:{inst.id}"
    cached = cache_get(key)
    if cached:
        return cached['access_token']
    now = int(time.time())
    assertion = jwt.encode({'iss': inst.lti_client_id, 'sub': inst.lti_client_id, 'aud': token_url(inst),
                            'iat': now, 'exp': now + 300, 'jti': str(uuid.uuid4())},
                           private_key(), algorithm='RS256', headers={'kid': key_id()})
    r = requests.post(token_url(inst), timeout=TIMEOUT, data={
        'grant_type': 'client_credentials',
        'client_assertion_type': 'urn:ietf:params:oauth:client-assertion-type:jwt-bearer',
        'client_assertion': assertion, 'scope': AGS_SCOPES,
    })
    if r.status_code != 200:
        raise LtiError(f"Moodle refuse le jeton de service ({r.status_code}) : {r.text[:300]}")
    data = r.json()
    cache_set(key, {'access_token': data['access_token']}, ttl=max(60, int(data.get('expires_in', 3600)) - 120))
    return data['access_token']


def _with_path(url: str, suffix: str, extra: dict | None = None) -> str:
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query))
    query.update(extra or {})
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip('/') + suffix, urlencode(query), ''))


def lineitems_url(inst, course_id: int) -> str:
    if not inst.lti_type_id:
        raise LtiError("Identifiant de l'outil CEI dans Moodle (type_id) inconnu : ouvrez une fois l'activité "
                       "« Examens CEI » depuis Moodle, ou saisissez-le dans la page Moodle de CEI.")
    return f"{inst.base_url}/mod/lti/services.php/{course_id}/lineitems?type_id={inst.lti_type_id}"


def _ags(inst, method: str, url: str, media: str, body=None):
    headers = {'Authorization': f"Bearer {service_token(inst)}", 'Accept': media}
    if body is not None:
        headers['Content-Type'] = media
    r = requests.request(method, url, headers=headers, timeout=TIMEOUT,
                         data=json.dumps(body) if body is not None else None)
    if r.status_code >= 300:
        raise LtiError(f"Moodle refuse la requête de notes ({r.status_code}) : {r.text[:300]}")
    return r.json() if r.content else None


def ensure_lineitem(session, exam, inst, course_id: int) -> str:
    """URL de la colonne de notes de cet examen, créée si besoin (une seule,
    retrouvée par resourceId si CEI l'a déjà créée)."""
    from models import LtiLineItem
    row = session.query(LtiLineItem).filter_by(exam_id=exam.id).first()
    if row and row.instance_id == inst.id and row.moodle_course_id == course_id:
        return row.lineitem_url
    base = lineitems_url(inst, course_id)
    resource_id = f"{RESOURCE_PREFIX}{exam.id}"
    found = _ags(inst, 'GET', _with_path(base, '', {'resource_id': resource_id}),
                 'application/vnd.ims.lis.v2.lineitemcontainer+json') or []
    if found:
        url = found[0]['id']
    else:
        created = _ags(inst, 'POST', base, 'application/vnd.ims.lis.v2.lineitem+json', {
            'scoreMaximum': SCORE_MAX, 'label': f"Examen CEI – {exam.title}"[:255],
            'resourceId': resource_id, 'tag': 'cei',
        })
        url = created['id']
    if not row:
        row = LtiLineItem(exam_id=exam.id, instance_id=inst.id, moodle_course_id=course_id, lineitem_url=url)
        session.add(row)
    else:
        row.instance_id, row.moodle_course_id, row.lineitem_url = inst.id, course_id, url
    session.commit()
    return url


def push_exam_scores(session, exam_id: int, dry_run: bool = False) -> dict:
    """Dépose dans Moodle les notes publiées d'un examen. Renvoie un bilan :
    {pushed, not_in_moodle, skipped, error}."""
    from models import OnlineExam, Subject, EC, ExamAttempt, User, LtiLineItem
    from services import moodle_sync
    report = {'exam_id': exam_id, 'pushed': 0, 'not_in_moodle': [], 'skipped': None, 'error': None}
    exam = session.get(OnlineExam, exam_id)
    if not exam or not exam.results_published:
        report['skipped'] = 'résultats non publiés'
        return report
    subject = session.get(Subject, exam.subject_id)
    ec = session.get(EC, subject.ec_id) if subject and subject.ec_id else None
    if not ec:
        report['skipped'] = 'sujet sans EC'
        return report
    found = moodle_sync.find_course_for_ec(session, ec.code)
    if not found:
        report['skipped'] = f'aucun cours Moodle {ec.code}'
        return report
    inst, client, course = found
    if not (inst.lti_client_id and inst.lti_deployment_id):
        report['skipped'] = f'LTI non configuré sur {inst.name}'
        return report

    attempts = (session.query(ExamAttempt, User.email)
                .join(User, User.id == ExamAttempt.student_id)
                .filter(ExamAttempt.exam_id == exam_id, ExamAttempt.score.isnot(None)).all())
    moodle_ids = {s['email']: s['moodle_id'] for s in client.enrolled_students(course['id'])}
    scores = []
    for att, email in attempts:
        mid = moodle_ids.get((email or '').strip().lower())
        if mid:
            scores.append((mid, max(0.0, min(float(att.score), SCORE_MAX))))
        else:
            report['not_in_moodle'].append(email)
    if dry_run:
        report['pushed'] = len(scores)
        return report
    try:
        url = ensure_lineitem(session, exam, inst, course['id'])
        stamp = datetime.now(timezone.utc).isoformat()
        for mid, score in scores:
            _ags(inst, 'POST', _with_path(url, '/scores'), 'application/vnd.ims.lis.v1.score+json', {
                'userId': str(mid), 'scoreGiven': round(score, 2), 'scoreMaximum': SCORE_MAX,
                'activityProgress': 'Completed', 'gradingProgress': 'FullyGraded', 'timestamp': stamp,
            })
            report['pushed'] += 1
        row = session.query(LtiLineItem).filter_by(exam_id=exam_id).first()
        row.pushed_at, row.pushed_count, row.last_error = datetime.now(timezone.utc), report['pushed'], None
        session.commit()
    except LtiError as e:
        session.rollback()
        report['error'] = str(e)
        row = session.query(LtiLineItem).filter_by(exam_id=exam_id).first()
        if row:
            row.last_error = str(e)[:1000]
            session.commit()
    return report


def schedule_push(exam_id: int) -> None:
    """À appeler après la publication des résultats — arrière-plan, jamais bloquant."""
    from services import moodle_sync
    if not moodle_sync.is_enabled():
        return

    def run():
        from models import get_session
        session = get_session()
        try:
            rep = push_exam_scores(session, exam_id)
            if rep['error'] or rep['pushed']:
                print(f"[lti] notes examen {exam_id} : {rep['pushed']} déposée(s), erreur={rep['error']}")
        except Exception as e:
            print(f"[lti] notes examen {exam_id} : {e}")
        finally:
            session.close()

    threading.Thread(target=run, daemon=True, name='lti-grades').start()
