"""
Client des webservices REST Moodle — plusieurs plateformes (Phase AA/AB).

Les plateformes sont enregistrées en base (MoodleInstance) par l'admin,
depuis la page Moodle, sans changement de code. Une plateforme déclarée
dans le .env (MOODLE_BASE_URL + MOODLE_WS_TOKEN, config d'origine de la
préprod) est reprise automatiquement en base au premier usage.

Correspondances vérifiées sur données réelles (Promo13 SEJA, 25/09) :
- cours Moodle ↔ EC CEI par code (shortname Moodle == EC.code) ;
- étudiants par email ; INE des étudiants dans idnumber ;
- les professeurs CEI existants n'ont pas de compte Moodle au même email :
  la matière d'un professeur passe par ses affectations EC dans CEI.

Moodle répond HTTP 200 même en cas d'erreur applicative : chaque réponse
est inspectée, jamais le seul code HTTP.
"""
import base64
import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import urlparse

import requests
from cryptography.fernet import Fernet, InvalidToken

from utils import extract_text_from_file

SUPPORTED_EXTENSIONS = {'pdf', 'docx', 'doc', 'txt', 'html', 'htm'}
MAX_MATERIALS_MB = 50  # même plafond cumulé que l'upload manuel de cours

# Fonctions utilisées aujourd'hui par CEI : leur absence casse une fonctionnalité.
REQUIRED_FUNCTIONS = [
    'core_webservice_get_site_info',
    'core_course_get_courses',
    'core_course_get_courses_by_field',
    'core_course_get_contents',
    'core_enrol_get_enrolled_users',
    # Création automatique des comptes à la connexion SSO (phase 1)
    'core_user_get_users_by_field',
    'core_enrol_get_users_courses',
    'core_enrol_get_enrolled_users_with_capability',
]
# Fonctions des phases suivantes (notes, calendrier) : signalées sans
# bloquer, pour que la plateforme soit prête le moment venu.
RECOMMENDED_FUNCTIONS = [
    'core_grades_update_grades',
    'core_grades_create_gradecategories',
    'core_calendar_create_calendar_events',
]


class MoodleError(Exception):
    pass


def is_enabled() -> bool:
    return os.getenv('MOODLE_SYNC_ENABLED', 'false').lower() == 'true'


# ── Token chiffré en base ────────────────────────────────────────────────────

def _fernet() -> Fernet:
    secret = os.getenv('SECRET_KEY')
    if not secret:
        raise RuntimeError('[moodle_sync] SECRET_KEY manquante — impossible de chiffrer les tokens Moodle')
    # Clé dérivée de SECRET_KEY (déjà obligatoire) : pas de nouvelle variable
    # à oublier en production. Si SECRET_KEY change, les tokens sont à ressaisir.
    digest = hashlib.sha256(f'cei-moodle-token:{secret}'.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_token(token: str) -> str:
    return _fernet().encrypt(token.encode()).decode()


def decrypt_token(encrypted: str) -> str:
    try:
        return _fernet().decrypt(encrypted.encode()).decode()
    except InvalidToken as e:
        raise MoodleError('Token illisible (SECRET_KEY modifiée ?) — ressaisir le token de cette plateforme') from e


def normalize_base_url(url: str) -> str:
    url = (url or '').strip().rstrip('/')
    parsed = urlparse(url)
    if parsed.scheme not in ('https', 'http') or not parsed.netloc:
        raise ValueError('Adresse invalide — attendu : https://moodle.exemple.sn')
    return url


# ── Client d'une plateforme ──────────────────────────────────────────────────

class _HTMLText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts, self._skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style'):
            self._skip += 1
        elif tag in ('p', 'br', 'div', 'li', 'tr', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6'):
            self.parts.append('\n')

    def handle_endtag(self, tag):
        if tag in ('script', 'style') and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def _html_to_text(raw: bytes) -> str:
    parser = _HTMLText()
    parser.feed(raw.decode('utf-8', errors='replace'))
    lines = (line.strip() for line in ''.join(parser.parts).splitlines())
    return '\n'.join(line for line in lines if line)


def _flatten(params: dict, prefix: str = '') -> dict:
    """{'courseids': [3, 4]} -> {'courseids[0]': 3, 'courseids[1]': 4} —
    notation imbriquée attendue par le protocole REST de Moodle."""
    flat = {}
    for key, value in params.items():
        name = f'{prefix}[{key}]' if prefix else str(key)
        if isinstance(value, dict):
            flat.update(_flatten(value, name))
        elif isinstance(value, (list, tuple)):
            flat.update(_flatten(dict(enumerate(value)), name))
        elif isinstance(value, bool):
            flat[name] = int(value)
        else:
            flat[name] = value
    return flat


class MoodleClient:
    def __init__(self, base_url: str, token: str):
        self.base_url = normalize_base_url(base_url)
        self.token = token

    def call(self, wsfunction: str, params: dict | None = None, timeout: int = 60):
        data = {'wstoken': self.token, 'moodlewsrestformat': 'json', 'wsfunction': wsfunction}
        data.update(_flatten(params or {}))
        try:
            resp = requests.post(f'{self.base_url}/webservice/rest/server.php', data=data, timeout=timeout)
        except requests.RequestException as e:
            raise MoodleError(f'Moodle injoignable ({wsfunction}) : {e}') from e
        try:
            payload = resp.json()
        except ValueError as e:
            raise MoodleError(f'Réponse Moodle non JSON ({wsfunction}, HTTP {resp.status_code})') from e
        if isinstance(payload, dict) and 'exception' in payload:
            raise MoodleError(f"{wsfunction} : {payload.get('errorcode')} — {payload.get('message')}")
        return payload

    # ── Lecture ──

    def site_info(self) -> dict:
        return self.call('core_webservice_get_site_info')

    def diagnose(self) -> dict:
        """Vérifie qu'une plateforme est prête pour CEI, avec les pièges déjà
        rencontrés sur Promo13 SEJA : fonctions absentes du service externe,
        case « Peut télécharger des fichiers » non cochée."""
        info = self.site_info()
        available = {f['name'] for f in info.get('functions', [])}
        missing = [f for f in REQUIRED_FUNCTIONS if f not in available]
        missing_reco = [f for f in RECOMMENDED_FUNCTIONS if f not in available]
        problems = []
        if missing:
            problems.append(f"Fonctions manquantes dans le service externe : {', '.join(missing)}")
        if not info.get('downloadfiles'):
            problems.append("Le service externe n'autorise pas le téléchargement de fichiers "
                            "(cocher « Peut télécharger des fichiers » sur le service)")
        return {
            'ok': not problems,
            'problems': problems,
            'warnings': ([f"Fonctions utiles aux prochaines étapes absentes : {', '.join(missing_reco)}"]
                         if missing_reco else []),
            'site': {
                'sitename': info.get('sitename'),
                'release': info.get('release'),
                'username': info.get('username'),
                'functions_count': len(available),
            },
            'reminder': "Capacité mod/book:read requise sur le rôle du compte de service pour lire "
                        "les chapitres de Livre (non vérifiable à distance).",
        }

    def find_person(self, email: str) -> dict | None:
        """Personne Moodle et ses cours, avec ceux qu'elle enseigne.
        Recherche par identifiant de connexion (égal à l'email UNCHK) : la
        recherche par champ email ne renvoie rien sur ces plateformes (vérifié
        le 25/09). Le rôle par cours vient de la capacité mod/assign:grade
        (enseignant, éditeur ou non), en un seul appel pour tous les cours —
        core_user_get_course_user_profiles ne donne qu'un profil par personne,
        pas un rôle par cours."""
        email = email.strip().lower()
        users = self.call('core_user_get_users_by_field', {'field': 'username', 'values': [email]}) \
            or self.call('core_user_get_users_by_field', {'field': 'email', 'values': [email]})
        if not users:
            return None
        user = users[0]
        courses = [c for c in self.call('core_enrol_get_users_courses', {'userid': user['id']}) if c.get('id') != 1]
        teaching = set()
        if courses:
            res = self.call('core_enrol_get_enrolled_users_with_capability', {'coursecapabilities': [
                {'courseid': c['id'], 'capabilities': ['mod/assign:grade']} for c in courses]})
            teaching_ids = {r['courseid'] for r in res for u in r.get('users', []) if u.get('id') == user['id']}
            teaching = {c['shortname'] for c in courses if c['id'] in teaching_ids}
        return {
            'moodle_id': user['id'],
            'fullname': (user.get('fullname') or '').strip(),
            'suspended': bool(user.get('suspended')),
            # Formation de l'étudiant : champ « Département » du profil Moodle
            # (AES, SJ, DIL, SPO, SEG… — vérifié sur 8 667 étudiants le 25/09)
            'department': (user.get('department') or '').strip().upper(),
            'course_codes': {c['shortname'] for c in courses},
            'teaching_codes': teaching,
        }

    def teachers_by_email(self) -> dict:
        """{email: [codes des cours enseignés]} pour TOUS les cours, en un seul
        appel (~4 s pour 118 cours et 229 enseignants, mesuré le 25/09)."""
        courses = self.list_courses()
        if not courses:
            return {}
        code = {c['id']: c['shortname'] for c in courses}
        res = self.call('core_enrol_get_enrolled_users_with_capability', {'coursecapabilities': [
            {'courseid': c['id'], 'capabilities': ['mod/assign:grade']} for c in courses]}, timeout=180)
        teachers = {}
        for r in res:
            for u in r.get('users', []):
                email = (u.get('email') or '').strip().lower()
                if email:
                    teachers.setdefault(email, set()).add(code[r['courseid']])
        return {e: sorted(c) for e, c in teachers.items()}

    def list_courses(self) -> list[dict]:
        """Tous les cours visibles, hors page d'accueil du site (id=1)."""
        return [c for c in self.call('core_course_get_courses') if c.get('id') != 1]

    def find_course_by_code(self, code: str) -> dict | None:
        res = self.call('core_course_get_courses_by_field', {'field': 'shortname', 'value': code})
        courses = res.get('courses', []) if isinstance(res, dict) else []
        return courses[0] if courses else None

    def course_materials(self, course_id: int, include_unsupported: bool = False) -> list[dict]:
        """Fichiers exploitables par l'IA (PDF/DOCX/DOC/TXT, chapitres HTML).
        include_unsupported=True ajoute les autres fichiers (supported=False),
        pour les montrer grisés à l'enseignant — jamais téléchargés.
        core_course_get_contents est la seule source : la matière est surtout
        dans des Dossiers et des Livres, et mod_folder_get_folders_by_courses
        ne renvoie PAS le contenu des dossiers."""
        materials = []
        for section in self.call('core_course_get_contents', {'courseid': course_id}):
            for module in section.get('modules', []) or []:
                for item in module.get('contents', []) or []:
                    if item.get('type') != 'file':
                        continue
                    filename = item.get('filename', '')
                    ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
                    supported = ext in SUPPORTED_EXTENSIONS
                    if not supported and not include_unsupported:
                        continue
                    materials.append({
                        'supported': supported,
                        # Chapitre de livre : le fichier s'appelle index.html,
                        # son titre est dans 'content'.
                        'title': (item.get('content') if ext in ('html', 'htm') else None) or filename,
                        'fileurl': item.get('fileurl'),
                        'filename': filename,
                        'extension': ext,
                        'filesize': item.get('filesize') or 0,
                        'timemodified': item.get('timemodified'),
                        'section': section.get('name'),
                        'module': module.get('name'),
                        'modname': module.get('modname'),
                        'visible': bool(module.get('visible', 1)),
                    })
        return materials

    def enrolled_students(self, course_id: int) -> list[dict]:
        """Étudiants inscrits (actifs). Filtrer par capacité mod/assign:submit
        plutôt que de demander le champ roles : ~11 s au lieu de ~18 s sur un
        cours de ~3 400 inscrits (mesuré le 25/09)."""
        users = self.call('core_enrol_get_enrolled_users', {
            'courseid': course_id,
            'options': [
                {'name': 'onlyactive', 'value': 1},
                {'name': 'withcapability', 'value': 'mod/assign:submit'},
                {'name': 'userfields', 'value': 'id,email,fullname,department'},
            ],
        }, timeout=120)
        return [
            {'moodle_id': u.get('id'), 'email': (u.get('email') or '').strip().lower(),
             'fullname': (u.get('fullname') or '').strip(),
             'department': (u.get('department') or '').strip().upper()}
            for u in users
        ]

    def course_teachers(self, course_id: int) -> list[dict]:
        """Enseignants d'un cours (éditeurs ou non) : capacité mod/assign:grade."""
        res = self.call('core_enrol_get_enrolled_users_with_capability', {
            'coursecapabilities': [{'courseid': course_id, 'capabilities': ['mod/assign:grade']}]})
        users = res[0].get('users', []) if res else []
        return [
            {'moodle_id': u.get('id'), 'email': (u.get('email') or '').strip().lower(),
             'fullname': (u.get('fullname') or '').strip()}
            for u in users if u.get('email')
        ]

    # ── Téléchargement + extraction ──

    def _download(self, fileurl: str, max_bytes: int) -> bytes:
        # Le token n'est envoyé qu'à cette plateforme, jamais à un autre hôte.
        if urlparse(fileurl).netloc != urlparse(self.base_url).netloc:
            raise MoodleError('URL de fichier hors de la plateforme Moodle — refusée')
        try:
            with requests.get(fileurl, params={'token': self.token}, stream=True, timeout=120) as resp:
                if resp.status_code != 200:
                    raise MoodleError(f'Téléchargement refusé (HTTP {resp.status_code})')
                chunks, size = [], 0
                for chunk in resp.iter_content(64 * 1024):
                    size += len(chunk)
                    if size > max_bytes:
                        raise MoodleError(f'Fichier trop volumineux (max {MAX_MATERIALS_MB} Mo cumulés)')
                    chunks.append(chunk)
        except requests.RequestException as e:
            raise MoodleError(f'Téléchargement impossible : {e}') from e
        raw = b''.join(chunks)
        # Un token invalide ou un droit manquant renvoie une erreur JSON à la place du fichier
        if raw[:1] == b'{' and b'"errorcode"' in raw[:500]:
            raise MoodleError('Moodle a refusé le téléchargement (token ou droits)')
        return raw

    def extract_materials(self, course_id: int, fileurls: list[str]) -> list[dict]:
        """Télécharge et extrait le texte des fichiers demandés. Seuls les
        fichiers appartenant réellement à ce cours sont acceptés : la liste est
        re-résolue côté serveur, jamais prise telle quelle du client. Un
        fichier en échec est signalé via 'error' sans interrompre les autres ;
        seul le dépassement du plafond cumulé interrompt tout."""
        available = {m['fileurl']: m for m in self.course_materials(course_id)}
        unknown = [u for u in fileurls if u not in available]
        if unknown:
            raise MoodleError(f"{len(unknown)} fichier(s) n'appartiennent pas à ce cours Moodle")

        budget = MAX_MATERIALS_MB * 1024 * 1024
        results = []
        for url in dict.fromkeys(fileurls):
            meta = available[url]
            entry = {'filename': meta['filename'], 'module': meta['module'], 'text': '', 'error': None}
            try:
                raw = self._download(url, budget)
            except MoodleError as e:
                if 'trop volumineux' in str(e):
                    raise
                entry['error'] = str(e)
                results.append(entry)
                continue
            budget -= len(raw)
            if meta['extension'] in ('html', 'htm'):
                text = _html_to_text(raw)
            else:
                fd, path = tempfile.mkstemp(suffix=f".{meta['extension']}")
                try:
                    with os.fdopen(fd, 'wb') as fh:
                        fh.write(raw)
                    text = extract_text_from_file(path) or ''
                finally:
                    os.remove(path)
            entry['text'] = text.strip()
            if not entry['text']:
                entry['error'] = 'Aucun texte extractible'
            results.append(entry)
        return results


# ── Plateformes enregistrées ─────────────────────────────────────────────────

def client_for(instance) -> MoodleClient:
    return MoodleClient(instance.base_url, decrypt_token(instance.token_encrypted))


def record_check(session, instance, diagnosis: dict | None, error: str | None = None):
    instance.last_check_at = datetime.now(timezone.utc)
    instance.last_check_ok = bool(diagnosis and diagnosis['ok'])
    instance.last_check_info = json.dumps(diagnosis if diagnosis else {'ok': False, 'problems': [error]})
    session.commit()


def _bootstrap_from_env(session):
    """Reprend en base la plateforme déclarée dans le .env (config d'origine
    de la préprod), pour que rien ne casse au passage en base. Seulement
    quand la table est vide : une plateforme supprimée par l'admin ne
    réapparaît pas d'elle-même."""
    from models import MoodleInstance
    url, token = os.getenv('MOODLE_BASE_URL'), os.getenv('MOODLE_WS_TOKEN')
    if not url or not token or session.query(MoodleInstance).count():
        return
    url = normalize_base_url(url)
    try:
        name = MoodleClient(url, token).site_info().get('sitename') or url
    except MoodleError:
        name = url
    session.add(MoodleInstance(name=name, base_url=url, token_encrypted=encrypt_token(token),
                               token_last4=token[-4:]))
    session.commit()


def active_instances(session) -> list:
    from models import MoodleInstance
    _bootstrap_from_env(session)
    return session.query(MoodleInstance).filter_by(is_active=True).order_by(MoodleInstance.id).all()


# ── Liste des enseignants Moodle, en cache ───────────────────────────────────
# Sert à reconnaître un compte CEI étudiant qui enseigne dans Moodle, à
# CHAQUE connexion. Interroger Moodle par personne ferait des milliers
# d'appels à l'ouverture d'un examen : la liste complète est obtenue en un
# appel par plateforme, gardée 7 jours, rafraîchie en arrière-plan toutes les
# 6 heures. Une connexion ne l'attend jamais : sans liste disponible, elle
# se fait simplement sans cette vérification.

_TEACHERS_KEY = 'cei:moodle:teachers'
_TEACHERS_FRESH_KEY = 'cei:moodle:teachers:fresh'
_TEACHERS_LOCK_KEY = 'cei:moodle:teachers:lock'


def refresh_teacher_map(session) -> dict | None:
    from cache import cache_set
    merged, ok = {}, False
    for inst in active_instances(session):
        try:
            for email, codes in client_for(inst).teachers_by_email().items():
                merged.setdefault(email, set()).update(codes)
            ok = True
        except MoodleError as e:
            print(f'[moodle_sync] liste des enseignants indisponible sur {inst.name} : {e}')
    if not ok:
        return None  # on garde l'ancienne liste plutôt que de l'effacer
    data = {e: sorted(c) for e, c in merged.items()}
    cache_set(_TEACHERS_KEY, data, ttl=7 * 86400)
    cache_set(_TEACHERS_FRESH_KEY, 1, ttl=6 * 3600)
    return data


def _refresh_teacher_map_background():
    from models import get_session
    session = get_session()
    try:
        refresh_teacher_map(session)
    except Exception as e:
        print(f'[moodle_sync] rafraîchissement de la liste des enseignants échoué : {e}')
    finally:
        session.close()


def teacher_map() -> dict:
    """{email: [codes]} — ne bloque jamais : renvoie la liste en cache (même
    un peu ancienne) et lance un rafraîchissement en arrière-plan si besoin."""
    import threading
    from cache import cache_get, cache_set_nx
    if not is_enabled():
        return {}
    data = cache_get(_TEACHERS_KEY)
    if cache_get(_TEACHERS_FRESH_KEY) is None and cache_set_nx(_TEACHERS_LOCK_KEY, 300):
        threading.Thread(target=_refresh_teacher_map_background, daemon=True).start()
    return data or {}


def find_course_for_ec(session, ec_code: str):
    """(plateforme, client, cours) du premier Moodle actif ayant un cours de
    ce code, ou None. Les codes en double entre plateformes sont signalés
    sur la page de correspondance."""
    instances = active_instances(session)
    errors = []
    for instance in instances:
        try:
            client = client_for(instance)
            course = client.find_course_by_code(ec_code)
        except MoodleError as e:
            errors.append(f'{instance.name} : {e}')
            continue
        if course:
            return instance, client, course
    # Une plateforme injoignable ne masque pas les autres ; on n'échoue que
    # si aucune n'a pu répondre.
    if instances and len(errors) == len(instances):
        raise MoodleError(' ; '.join(errors))
    return None
