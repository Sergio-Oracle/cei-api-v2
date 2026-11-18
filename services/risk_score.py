"""Score de risque d'une tentative — recalibré le 18/11/2026.

Constat (examen 192 du 17/11 : 1 075 tentatives) : 89 % des étudiants avaient un score ≥ 81. Chaque événement ajoutait son poids sans limite jusqu'à 100,
or les signaux FAIBLES et bruyants se répètent chez presque tout le monde (30 938 « audio soutenu » pour 819 étudiants, 20 354 « visage absent » pour 682…).
Le score ne distinguait plus personne et deux étudiants aux indicateurs proches pouvaient avoir 30 et 100.

Principe : les événements sont regroupés en CATÉGORIES, et chaque catégorie a un PLAFOND de points.
  - Signaux faibles (audio, visage absent, perte de focus, regard, divers) : plafonds bas ; leur somme maximale (30) reste très loin du seuil d'alerte (60).
  - Signaux forts (objet, plusieurs personnes, signaux combinés, presse-papiers, identité) : plafonds plus hauts. Un signal fort ISOLÉ n'atteint pas 60 (25 + 30 = 55) :
    il faut deux familles de signaux forts concordantes, ou un objet confirmé répété, ou une identité non confirmée.
Le détail par catégorie est conservé (Redis) et affiché au surveillant (« pourquoi ce score »).
Aucune exclusion automatique n'est liée à ce score.
"""
import json
import logging

log = logging.getLogger('cei.risk')

# catégorie -> (libellé affiché, plafond de points)
CATEGORIES = {
    'audio':      ('Voix / chuchotements', 6),
    'visage':     ('Visage absent ou masqué', 6),
    'focus':      ('Perte de focus / plein écran', 10),
    'attention':  ('Regard / tête', 4),
    'divers':     ('Autres signaux', 4),
    'objet':      ('Objet suspect', 35),
    'personnes':  ('Plusieurs personnes / écrans', 25),
    'motif':      ('Signaux combinés', 25),
    'identite':   ('Identité non confirmée', 40),
    'presse':     ('Presse-papiers', 15),
}

EVENT_CATEGORY = {
    'sustained_audio_detected': 'audio', 'whisper_detected': 'audio', 'talking_detected': 'audio', 'audio_suspicious': 'audio',
    'no_face_detected': 'visage', 'face_absent': 'visage', 'face_covered': 'visage', 'camera_blocked': 'visage',
    'tab_switch': 'focus', 'window_blur': 'focus', 'fullscreen_exit': 'focus', 'tab_closed': 'focus',
    'gaze_away': 'attention', 'head_turned': 'attention',
    'suspect_object_detected': 'objet', 'suspect_object_confirmed': 'objet',
    'multiple_faces': 'personnes', 'env_scan_person_detected': 'personnes', 'multi_screen_detected': 'personnes',
    'pattern_gaze_talk_mouth': 'motif', 'pattern_multi_face_audio': 'motif', 'pattern_object_gaze_away': 'motif',
    'pattern_mouth_covered_audio': 'motif', 'pattern_head_turned_talking': 'motif', 'pattern_whisper_gaze': 'motif',
    'identity_mismatch_sustained': 'identite',
    'copy_attempt': 'presse', 'cut_attempt': 'presse', 'paste_attempt': 'presse',
}

KEY_TTL = 7 * 24 * 3600
THRESHOLD_ALERT = 60          # seuil d'alerte de l'agent (aligné sur RISK_ALERT)


def category_of(event_type):
    return EVENT_CATEGORY.get(event_type, 'divers')


def total_of(points):
    """Score 0-100 : somme des catégories, chacune plafonnée."""
    t = 0.0
    for cat, pts in (points or {}).items():
        cap = CATEGORIES.get(cat, CATEGORIES['divers'])[1]
        t += min(float(pts), cap)
    return int(min(100, round(t)))


def breakdown_of(points):
    """Détail lisible : [{categorie, libelle, points, plafond}] trié par points décroissants (catégories non nulles seulement)."""
    out = []
    for cat, pts in (points or {}).items():
        label, cap = CATEGORIES.get(cat, CATEGORIES['divers'])
        p = min(float(pts), cap)
        if p > 0:
            out.append({'categorie': cat, 'libelle': label, 'points': int(round(p)), 'plafond': cap})
    out.sort(key=lambda x: -x['points'])
    return out


def _key(attempt_id):
    return f'cei:risk:{int(attempt_id)}'


def apply_event(client, attempt_id, event_type, weight):
    """Ajoute `weight` points à la catégorie de l'événement (plafonnée) et renvoie le nouveau score 0-100,
    ou None si Redis est indisponible (l'appelant retombe alors sur l'ancien calcul cumulatif)."""
    if client is None:
        return None
    try:
        key = _key(attempt_id)
        cat = category_of(event_type)
        if weight and weight > 0:
            cap = CATEGORIES[cat][1]
            pts = float(client.hincrbyfloat(key, cat, float(weight)))
            if pts > cap:
                client.hset(key, cat, cap)          # on ne laisse pas dériver au-delà du plafond
            client.expire(key, KEY_TTL)
        raw = client.hgetall(key) or {}
        points = {(k.decode() if isinstance(k, bytes) else k): float(v) for k, v in raw.items()}
        return total_of(points)
    except Exception as e:
        log.warning('risk apply_event: %s', e)
        return None


def read_breakdowns(client, attempt_ids):
    """{attempt_id: [détail]} pour une liste de tentatives (un seul aller-retour Redis)."""
    out = {}
    if client is None or not attempt_ids:
        return out
    try:
        pipe = client.pipeline()
        ids = list(attempt_ids)
        for i in ids:
            pipe.hgetall(_key(i))
        for i, raw in zip(ids, pipe.execute()):
            if raw:
                points = {(k.decode() if isinstance(k, bytes) else k): float(v) for k, v in raw.items()}
                out[i] = breakdown_of(points)
    except Exception as e:
        log.warning('risk read_breakdowns: %s', e)
    return out


def replay(events, weights, dedup_seconds=None):
    """Rejoue une liste d'événements [(event_type, timestamp_en_secondes)] avec les poids donnés : renvoie (score, détail).
    Sert aux tests et à la simulation sur l'historique ; `dedup_seconds` imite le garde-fou anti-doublon en ligne."""
    points, last = {}, {}
    dedup_seconds = dedup_seconds or {}
    for et, ts in sorted(events, key=lambda e: e[1]):
        w = weights.get(et, 5)
        win = dedup_seconds.get(et, 2)
        if et in last and ts - last[et] < win:
            continue
        last[et] = ts
        if w > 0:
            points[category_of(et)] = points.get(category_of(et), 0) + w
    return total_of(points), breakdown_of(points)
