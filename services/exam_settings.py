"""Réglages d'un examen modifiables APRÈS sa création (état Planifié ou En cours), avec les règles du « standard » :

  • Planifié / brouillon : tout se modifie (sujet inclus tant que personne n'a commencé).
  • En cours : seuls les réglages sans effet injuste sur ceux qui composent déjà — titre, consigne, seuils d'alerte, blocages
    (copier-coller, clic droit, téléchargement), calculatrice, 2e caméra, correction automatique ou planifiée, et la FIN (prolongation seule,
    jamais de raccourcissement). Sujet, début, pagination, ordre aléatoire, minuteur par page, vérification d'identité et scan de
    l'environnement sont verrouillés dès que l'examen est ouvert (même règle que Moodle : on ne change pas les règles en cours de partie).
  • Terminé : plus rien.

  policy(session, exam)  -> {'status', 'attempts', 'by_waves', 'locked': {champ: raison}}
  FIELD_RULES            -> validation/normalisation de chaque champ
"""
from datetime import timedelta


def _bool(v):
    return bool(v)


def _int_range(lo, hi, label):
    def f(v):
        try:
            n = int(v)
        except (TypeError, ValueError):
            raise ValueError(f"{label} : nombre entier attendu")
        if n < lo or n > hi:
            raise ValueError(f"{label} : entre {lo} et {hi}")
        return n
    return f


def _text(label, maxlen=None, required=False):
    def f(v):
        t = (v or '').strip() if isinstance(v, str) or v is None else str(v).strip()
        if required and not t:
            raise ValueError(f"{label} ne peut pas être vide")
        if maxlen and len(t) > maxlen:
            raise ValueError(f"{label} : {maxlen} caractères maximum")
        return t
    return f


def _opt_seconds(v):
    if not v:
        return None
    try:
        n = int(v)
    except (TypeError, ValueError):
        raise ValueError("Minuteur par page : nombre de secondes attendu")
    if n < 0 or n > 3600:
        raise ValueError("Minuteur par page : entre 0 et 3600 secondes")
    return n or None


def _client_sites(v):
    """Sites ouverts dans la fenêtre d'assistance de l'application CEI Examen : adresses https complètes, 5 au plus (liste ou texte, une par ligne)."""
    import ipaddress, json, re
    from urllib.parse import urlparse
    if v in (None, '', []):
        return None
    if isinstance(v, str):
        t = v.strip()
        try:
            v = json.loads(t) if t.startswith('[') else [x for x in re.split(r'[\n,;]+', t)]
        except ValueError:
            raise ValueError("Sites autorisés : liste d'adresses attendue")
    if not isinstance(v, list):
        raise ValueError("Sites autorisés : liste d'adresses attendue")
    out = []
    for x in v:
        x = str(x).strip()
        if not x:
            continue
        p = urlparse(x)
        host = (p.hostname or '').lower()
        bad = p.scheme != 'https' or not host or '.' not in host or p.username or p.password or len(x) > 300
        if not bad:
            try:
                ipaddress.ip_address(host)
                bad = True                                        # pas d'adresse IP brute : un nom de site reconnaissable
            except ValueError:
                pass
        if bad:
            raise ValueError(f"Site autorisé invalide « {x[:60]} » : adresse complète en https:// attendue (ex. https://jokkomeet.unchk.sn/)")
        norm = f"https://{host}" + (f":{p.port}" if p.port else '') + (p.path or '/') + (f"?{p.query}" if p.query else '')
        if norm not in out:
            out.append(norm)
    if len(out) > 5:
        raise ValueError("Sites autorisés : 5 au maximum")
    return json.dumps(out, ensure_ascii=False) if out else None


def _client_apps(v):
    """Applications (noms de processus, ex. « whatsapp ») que l'application CEI Examen laisse ouvertes : 10 au plus."""
    import json, re
    if v in (None, '', []):
        return None
    if isinstance(v, str):
        t = v.strip()
        try:
            v = json.loads(t) if t.startswith('[') else [x for x in re.split(r'[\n,;]+', t)]
        except ValueError:
            raise ValueError("Applications autorisées : liste de noms attendue")
    if not isinstance(v, list):
        raise ValueError("Applications autorisées : liste de noms attendue")
    out = []
    for x in v:
        x = str(x).strip()
        if not x:
            continue
        x = re.sub(r'\.(exe|app)$', '', x, flags=re.I)
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._ -]{0,59}', x):
            raise ValueError(f"Application autorisée invalide « {x[:40]} » : nom du programme attendu (lettres, chiffres, point, tiret)")
        if x.lower() not in [o.lower() for o in out]:
            out.append(x)
    if len(out) > 10:
        raise ValueError("Applications autorisées : 10 au maximum")
    return json.dumps(out, ensure_ascii=False) if out else None


FIELD_RULES = {
    'title': _text('Le titre', 200, required=True),
    'instructions': _text('La consigne', 5000),
    'max_tab_switches': _int_range(0, 50, 'Seuil de changements de fenêtre'),
    'max_no_face_count': _int_range(-1, 200, 'Seuil de visage absent'),
    'ban_on_devtools': _bool, 'auto_ban_enabled': _bool,
    'enable_copy_paste': _bool, 'enable_right_click': _bool, 'enable_file_download': _bool,
    'enable_calculator': _bool, 'allow_secondary_camera': _bool,
    'auto_correct': _bool,
    'randomize_questions': _bool,
    'questions_per_page': _int_range(0, 50, 'Questions par page'),
    'time_per_question_seconds': _opt_seconds,
    'require_biometric': _bool, 'run_environment_scan': _bool, 'require_client': _bool,
    'client_allowed_sites': _client_sites, 'client_allowed_apps': _client_apps, 'client_block_vm': _bool,
}

# Réglages qui changent les règles de ceux qui ont COMMENCÉ : verrouillés dès qu'une copie existe (ou que l'examen est en cours)
_STRUCTURAL = {
    'randomize_questions': "l'ordre des questions est déjà attribué aux étudiants",
    'questions_per_page': "la pagination est déjà attribuée aux étudiants",
    'time_per_question_seconds': "le minuteur par page changerait les règles pour ceux qui composent déjà",
    'require_biometric': "la vérification d'identité se fait à l'entrée ; elle ne peut plus changer une fois l'examen ouvert",
    'run_environment_scan': "le scan de l'environnement se fait à l'entrée ; il ne peut plus changer une fois l'examen ouvert",
    'require_client': "l'application CEI Examen s'exige à l'entrée ; elle ne peut plus changer une fois l'examen ouvert",
    'client_block_vm': "le refus des machines virtuelles se vérifie à l'entrée ; il ne peut plus changer une fois l'examen ouvert",
}


def policy(session, exam):
    from models import ExamAttempt, ExamStatus
    from services.exam_waves import exam_has_waves
    attempts = session.query(ExamAttempt).filter_by(exam_id=exam.id).count()
    by_waves = exam_has_waves(session, exam.id)
    locked = {}
    if exam.status == ExamStatus.CLOSED:
        for k in list(FIELD_RULES) + ['start_time', 'end_time', 'subject_id', 'scheduled_correction_at']:
            locked[k] = "l'examen est terminé"
        return {'status': exam.status.value, 'attempts': attempts, 'by_waves': by_waves, 'locked': locked}
    active = exam.status == ExamStatus.ACTIVE
    if active:
        locked['start_time'] = "l'examen est déjà ouvert (vous pouvez seulement le prolonger)"
        locked['subject_id'] = "l'examen est déjà ouvert : le sujet ne peut plus changer"
    elif attempts:
        locked['subject_id'] = "des copies existent déjà"
    if active or attempts:
        for k, why in _STRUCTURAL.items():
            locked[k] = why
    if by_waves and active:
        locked['end_time'] = "examen organisé par vagues : prolongez une vague depuis la gestion des vagues"
    return {'status': exam.status.value, 'attempts': attempts, 'by_waves': by_waves, 'locked': locked}


def current_value(exam, key):
    v = getattr(exam, key, None)
    if key in ('client_allowed_sites', 'client_allowed_apps'):      # listes enregistrées en JSON : rendues comme listes
        from models import _json_list
        return _json_list(v)
    if hasattr(v, 'isoformat'):
        return v.isoformat()
    return v


def describe(key, old, new):
    return f"{key} : {old} → {new}"
