"""
Client des webservices REST Moodle (compte de service cei-integration,
service externe "CEI Sync") — Phase AA du plan.

Correspondances vérifiées sur données réelles (Promo13 SEJA, 25/09) :
- cours Moodle ↔ EC CEI par code (shortname Moodle == EC.code) — 70/118 ;
  aucun cours ne correspond à un code d'UE ;
- étudiants par email (96 % des inscriptions retrouvées) ;
- les professeurs CEI n'ont PAS de compte Moodle au même email (0/24) :
  on ne passe jamais par l'identité Moodle d'un professeur, mais par ses
  affectations EC dans CEI (ECAssignment → EC.code → cours Moodle).

Config 100% .env, échec explicite si une variable manque (même règle que
oidc_keycloak.py). Moodle répond HTTP 200 même en cas d'erreur applicative :
chaque réponse est inspectée, jamais le seul code HTTP.
"""
import os
import tempfile
from html.parser import HTMLParser
from urllib.parse import urlparse

import requests

from utils import extract_text_from_file

SUPPORTED_EXTENSIONS = {'pdf', 'docx', 'doc', 'txt', 'html', 'htm'}
MAX_MATERIALS_MB = 50  # même plafond cumulé que l'upload manuel de cours


class MoodleError(Exception):
    pass


def is_enabled() -> bool:
    return os.getenv('MOODLE_SYNC_ENABLED', 'false').lower() == 'true'


def _require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"[moodle_sync] Variable d'environnement manquante : {name}")
    return value


def _base_url() -> str:
    return _require_env('MOODLE_BASE_URL').rstrip('/')


def _token() -> str:
    return _require_env('MOODLE_WS_TOKEN')


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


def call(wsfunction: str, params: dict | None = None, timeout: int = 60):
    data = {'wstoken': _token(), 'moodlewsrestformat': 'json', 'wsfunction': wsfunction}
    data.update(_flatten(params or {}))
    try:
        resp = requests.post(f'{_base_url()}/webservice/rest/server.php', data=data, timeout=timeout)
    except requests.RequestException as e:
        raise MoodleError(f'Moodle injoignable ({wsfunction}) : {e}') from e
    try:
        payload = resp.json()
    except ValueError as e:
        raise MoodleError(f'Réponse Moodle non JSON ({wsfunction}, HTTP {resp.status_code})') from e
    if isinstance(payload, dict) and 'exception' in payload:
        raise MoodleError(f"{wsfunction} : {payload.get('errorcode')} — {payload.get('message')}")
    return payload


# ── Lecture ──────────────────────────────────────────────────────────────────

def site_info() -> dict:
    info = call('core_webservice_get_site_info')
    return {
        'sitename': info.get('sitename'),
        'siteurl': info.get('siteurl'),
        'release': info.get('release'),
        'username': info.get('username'),
        'functions_count': len(info.get('functions', [])),
    }


def list_courses() -> list[dict]:
    """Tous les cours visibles, hors page d'accueil du site (id=1)."""
    return [c for c in call('core_course_get_courses') if c.get('id') != 1]


def find_course_by_code(code: str) -> dict | None:
    res = call('core_course_get_courses_by_field', {'field': 'shortname', 'value': code})
    courses = res.get('courses', []) if isinstance(res, dict) else []
    return courses[0] if courses else None


def course_materials(course_id: int) -> list[dict]:
    """Fichiers exploitables par l'IA (PDF/DOCX/DOC/TXT, chapitres HTML) d'un
    cours. core_course_get_contents est la seule source : sur ce Moodle la
    matière est surtout dans des Dossiers et des Livres, et
    mod_folder_get_folders_by_courses ne renvoie PAS le contenu des dossiers."""
    materials = []
    for section in call('core_course_get_contents', {'courseid': course_id}):
        for module in section.get('modules', []) or []:
            for item in module.get('contents', []) or []:
                if item.get('type') != 'file':
                    continue
                filename = item.get('filename', '')
                ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
                if ext not in SUPPORTED_EXTENSIONS:
                    continue
                materials.append({
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


def enrolled_students(course_id: int) -> list[dict]:
    """Étudiants inscrits (actifs) d'un cours. Filtrer par capacité
    mod/assign:submit plutôt que de demander le champ roles : ~11 s au lieu
    de ~18 s sur un cours SEJA de ~3 400 inscrits (mesuré le 25/09)."""
    users = call('core_enrol_get_enrolled_users', {
        'courseid': course_id,
        'options': [
            {'name': 'onlyactive', 'value': 1},
            {'name': 'withcapability', 'value': 'mod/assign:submit'},
            {'name': 'userfields', 'value': 'id,email,fullname'},
        ],
    }, timeout=120)
    return [
        {'moodle_id': u.get('id'), 'email': (u.get('email') or '').strip().lower(), 'fullname': (u.get('fullname') or '').strip()}
        for u in users
    ]


# ── Téléchargement + extraction ──────────────────────────────────────────────

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


def _download(fileurl: str, max_bytes: int) -> bytes:
    # Le token n'est envoyé qu'au Moodle configuré, jamais à un autre hôte.
    if urlparse(fileurl).netloc != urlparse(_base_url()).netloc:
        raise MoodleError('URL de fichier hors du Moodle configuré — refusée')
    try:
        with requests.get(fileurl, params={'token': _token()}, stream=True, timeout=120) as resp:
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
    # Un token invalide/expiré renvoie une page d'erreur JSON au lieu du fichier
    if raw[:1] == b'{' and b'"errorcode"' in raw[:500]:
        raise MoodleError('Moodle a refusé le téléchargement (token ou droits)')
    return raw


def extract_materials(course_id: int, fileurls: list[str]) -> list[dict]:
    """Télécharge et extrait le texte des fichiers demandés. Seuls les
    fichiers appartenant réellement à ce cours sont acceptés : la liste est
    re-résolue côté serveur, jamais prise telle quelle du client."""
    available = {m['fileurl']: m for m in course_materials(course_id)}
    unknown = [u for u in fileurls if u not in available]
    if unknown:
        raise MoodleError(f"{len(unknown)} fichier(s) n'appartiennent pas à ce cours Moodle")

    # Un fichier en échec (ex. chapitre de Livre refusé par Moodle) est
    # signalé via 'error' sans interrompre les autres : l'appelant décide
    # s'il reste assez de matière. Seul le dépassement du plafond cumulé
    # interrompt tout, pour ne jamais dépasser 50 Mo au total.
    budget = MAX_MATERIALS_MB * 1024 * 1024
    results = []
    for url in dict.fromkeys(fileurls):
        meta = available[url]
        entry = {'filename': meta['filename'], 'module': meta['module'], 'text': '', 'error': None}
        try:
            raw = _download(url, budget)
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
