"""Test technique avant l'examen (18/11/2026).

Rapport du surveillant sur l'examen du 17/11 : caméra « activée » sans image, vignettes bloquées, éclairage faible, connexion instable, étudiants qui découvrent
l'interface le jour même. Le test reproduit CE QUE L'EXAMEN EXIGE, y compris l'envoi réel de la caméra et de l'écran vers le serveur vidéo, vérifié CÔTÉ SERVEUR
(le navigateur ne peut pas se déclarer « prêt » tout seul). Résultat conservé 14 jours et visible du surveillant : prêts / à surveiller / non prêts / jamais testés.
"""
import json
import time

KEY_TTL = 14 * 24 * 3600
VERIFY_TTL = 30 * 60
MIN_BRIGHTNESS, WARN_BRIGHTNESS, MAX_BRIGHTNESS = 40, 70, 235
MIN_MIC_LEVEL = 0.02
MIN_DOWN_MBPS, WARN_DOWN_MBPS, MAX_LATENCY_MS = 1.0, 2.5, 800
MIN_CLIENT_VERSION = (1, 8, 0)
FRESH_SECONDS = 24 * 3600          # un test de moins de 24 h compte comme « récent »


def _num(v, default=None):
    try:
        f = float(v)
        return f if f == f and abs(f) < 1e12 else default
    except (TypeError, ValueError):
        return default


def _vt(v):
    try:
        return tuple(int(x) for x in str(v).split('.')[:3])
    except ValueError:
        return None


def evaluate(checks, live):
    """checks : relevés du navigateur (non fiables) ; live : vérification SERVEUR de la diffusion {camera, mic, screen, width, height} ou None.
    Retourne (statut, problèmes) : statut 'ready' | 'warn' | 'not_ready'. Chaque problème : {code, niveau 'bloquant'|'attention', message}."""
    checks = checks if isinstance(checks, dict) else {}
    live = live if isinstance(live, dict) else None
    problems = []
    sub = lambda k: checks.get(k) if isinstance(checks.get(k), dict) else {}      # un relevé mal formé (venu du navigateur) vaut « absent », jamais une erreur

    def add(code, level, msg, staff=None):
        # `message` : ce que lit l'ÉTUDIANT (langage simple, aucun terme technique) ; `staff` : détail technique réservé au surveillant.
        problems.append({'code': code, 'niveau': level, 'message': msg, 'staff': staff or msg})

    cam = sub('camera')
    if not cam.get('ok'):
        add('camera', 'bloquant', "La caméra n'est pas accessible : autorisez-la dans le navigateur et fermez les autres applications qui l'utilisent (Teams, Zoom…).")
    else:
        b = _num(cam.get('brightness'))
        if b is not None and b < MIN_BRIGHTNESS:
            add('lumiere', 'bloquant', "Image presque noire : placez-vous face à une fenêtre ou une lampe (la lumière doit venir de face, jamais de derrière).")
        elif b is not None and b < WARN_BRIGHTNESS:
            add('lumiere', 'attention', "Éclairage faible : augmentez la lumière devant vous pour éviter de fausses alertes « visage absent ».")
        elif b is not None and b > MAX_BRIGHTNESS:
            add('lumiere', 'attention', "Image trop claire (contre-jour ou lumière directe) : évitez de vous placer devant une fenêtre.")
        if cam.get('face') is False:
            add('visage', 'attention', "Aucun visage détecté pendant le test : placez-vous face à la caméra, visage entier visible.")

    mic = sub('micro')
    if not mic.get('ok'):
        add('micro', 'bloquant', "Le micro n'est pas accessible : autorisez-le dans le navigateur.")
    elif (_num(mic.get('level'), 0) or 0) < MIN_MIC_LEVEL:
        add('micro_silence', 'attention', "Aucun son capté : parlez pendant le test ou vérifiez que le micro n'est pas coupé.")

    scr = sub('screen')
    if not scr.get('ok'):
        add('ecran', 'bloquant', "Le partage d'écran complet n'a pas été accepté : choisissez « Écran entier ».")

    net = sub('network')
    lat, down = _num(net.get('latency_ms')), _num(net.get('down_mbps'))
    if lat is None or down is None:
        add('reseau', 'attention', "Le test de débit n'a pas abouti : vérifiez votre connexion.")
    else:
        if down < MIN_DOWN_MBPS or lat > MAX_LATENCY_MS:
            add('reseau', 'bloquant', "Votre connexion Internet est trop lente pour l'examen : rapprochez-vous de votre box ou routeur et fermez tout ce qui télécharge.",
                f"Connexion insuffisante ({down:.1f} Mbit/s, {int(lat)} ms)")
        elif down < WARN_DOWN_MBPS:
            add('reseau', 'attention', "Votre connexion Internet est juste suffisante : évitez toute autre utilisation d'Internet pendant l'examen.",
                f"Connexion juste suffisante ({down:.1f} Mbit/s)")

    cl = sub('client')
    if cl.get('is_client'):
        v = _vt(cl.get('version'))
        if v is not None and v < MIN_CLIENT_VERSION:
            add('version', 'bloquant', "Votre application CEI Examen n'est pas à jour : fermez-la puis rouvrez-la pour qu'elle se mette à jour, ou téléchargez la dernière version.",
                f"Application CEI Examen {cl.get('version')} trop ancienne (partage d'écran incomplet)")

    if not live:
        add('diffusion', 'bloquant', "Le test n'est pas allé jusqu'au bout : refaites-le en entier, jusqu'au message de résultat.",
            "Diffusion vers le serveur vidéo non vérifiée (test incomplet)")
    else:
        if not live.get('camera'):
            add('diffusion_camera', 'bloquant', "Votre image ne peut pas être transmise au surveillant avec la connexion actuelle. Essayez avec un autre réseau (par exemple le partage de connexion de votre téléphone), puis refaites le test.",
                "Caméra non reçue par le serveur vidéo (réseau ou pare-feu bloquant l'envoi)")
        if not live.get('screen'):
            add('diffusion_ecran', 'bloquant', "Votre écran ne peut pas être transmis au surveillant. Refaites le test en acceptant le partage de l'écran entier, ou essayez avec un autre réseau.",
                "Écran non reçu par le serveur vidéo")
        if not live.get('mic'):
            add('diffusion_micro', 'attention', "Votre voix ne semble pas transmise au surveillant : vérifiez que votre micro n'est pas coupé, puis refaites le test.",
                "Micro non reçu par le serveur vidéo")

    if any(p['niveau'] == 'bloquant' for p in problems):
        return 'not_ready', problems
    return ('warn' if problems else 'ready'), problems


def summarize_participant(participant):
    """Réponse ListParticipants de LiveKit (un participant) → {camera, mic, screen, width, height} d'après les pistes REÇUES par le serveur."""
    out = {'camera': False, 'mic': False, 'screen': False, 'width': 0, 'height': 0}
    for t in (participant or {}).get('tracks', []) or []:
        typ, src = str(t.get('type', '')).upper(), str(t.get('source', '')).upper()
        if t.get('muted'):
            continue
        if typ == 'AUDIO':
            out['mic'] = True
        elif typ == 'VIDEO' and src == 'SCREEN_SHARE':
            out['screen'] = True
        elif typ == 'VIDEO':
            w, h = int(t.get('width') or 0), int(t.get('height') or 0)
            out['camera'] = True
            out['width'], out['height'] = max(out['width'], w), max(out['height'], h)
    return out


def status_of_record(rec, now=None):
    """Statut vu du surveillant à partir de l'enregistrement conservé : 'ready' | 'warn' | 'not_ready' | 'stale' (test ancien) | 'never'."""
    if not rec:
        return 'never'
    now = now or time.time()
    if now - float(rec.get('ts', 0)) > FRESH_SECONDS:
        return 'stale'
    return rec.get('status', 'never')


def dumps(rec):
    return json.dumps(rec, ensure_ascii=False)
