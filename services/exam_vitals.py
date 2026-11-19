"""Santé d'un examen en cours (18/11/2026) — contrôle permanent, en direct et après coup.

Pourquoi : à l'examen du 17/11, les problèmes (attentes de plusieurs minutes avant l'affichage de la copie, liaison vidéo des surveillants coupée, flot d'alertes, étudiants
« hors ligne ») n'ont été découverts qu'APRÈS coup, par les surveillants, dans un rapport. Ici ils sont mesurés par la plateforme elle-même, minute par minute, comparés à des
seuils, et l'administrateur est prévenu AVANT que les surveillants ne s'en aperçoivent.

Tout vient de la base (aucune lecture de journaux) : une seule requête agrégée par indicateur, donc sans effet notable même avec 5 000 tentatives.
Les textes sont en français simple, sans terme technique : ils s'adressent à un responsable, pas à un ingénieur.
"""
import logging
from datetime import datetime, timedelta

from sqlalchemy import text

log = logging.getLogger('cei.vitals')

# Seuils (réglables ici, un seul endroit)
MIN_IN_PROGRESS = 10            # en dessous, les pourcentages ne veulent rien dire : aucun verdict
STUCK_ENTRY_AFTER_S = 180       # une copie non affichée 3 minutes après « Démarrer » = étudiant bloqué à l'entrée
STUCK_ENTRY_PCT_CRIT = 10.0
STUCK_ENTRY_MIN_CRIT = 5
ENTRY_P90_ATTENTION_S = 300
OFFLINE_AFTER_S = 90            # sans signe de vie depuis 90 s = hors ligne
OFFLINE_PCT_CRIT = 15.0
VIDEO_LINK_PCT_ATTENTION = 20.0
HIGH_RISK_PCT_ATTENTION = 30.0
CALLS_30MIN_ATTENTION = 5
CRASH_PCT_ATTENTION = 5.0
CACHE_S = 45                    # durée de partage du résultat entre toutes les pages ouvertes
WATCH_COOLDOWN_S = 600          # une même alerte critique n'est renvoyée à l'administrateur qu'une fois toutes les 10 minutes

LEVEL_ORDER = {'ok': 0, 'attention': 1, 'critique': 2}


def _scalar(s, sql, **p):
    return s.execute(text(sql), p).scalar() or 0


def compute(session, exam_id, now=None):
    """Indicateurs + verdict d'un examen. `now` injectable pour les tests."""
    now = now or datetime.utcnow()
    try:
        # Délai maximal de 4 s pour chaque requête de CE contrôle : si la base est chargée, il s'interrompt au lieu de s'empiler avec le reste (un outil de surveillance ne doit
        # jamais aggraver une surcharge). La transaction est celle du contrôle seul ; elle se termine avec la session.
        session.execute(text("SET LOCAL statement_timeout = 4000"))
    except Exception:
        pass
    p = {'e': exam_id, 'now': now}
    base = "FROM exam_attempts a WHERE a.exam_id = :e"
    started = _scalar(session, f"SELECT count(*) {base}", **p)
    in_progress = _scalar(session, f"SELECT count(*) {base} AND a.status = 'IN_PROGRESS' AND a.exam_seen_at IS NOT NULL", **p)
    seen = _scalar(session, f"SELECT count(*) {base} AND a.exam_seen_at IS NOT NULL", **p)
    submitted = _scalar(session, f"SELECT count(*) {base} AND a.status IN ('SUBMITTED','AUTO_SUBMITTED')", **p)
    stuck = _scalar(session, f"SELECT count(*) {base} AND a.status = 'IN_PROGRESS' AND a.exam_seen_at IS NULL "
                             f"AND a.started_at < :now - make_interval(secs => {STUCK_ENTRY_AFTER_S})", **p)
    row = session.execute(text(
        "SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY extract(epoch FROM (a.exam_seen_at - a.started_at))), "
        "       percentile_cont(0.9) WITHIN GROUP (ORDER BY extract(epoch FROM (a.exam_seen_at - a.started_at))), "
        "       count(*) FILTER (WHERE a.exam_seen_at - a.started_at > interval '5 minutes') "
        "FROM exam_attempts a WHERE a.exam_id = :e AND a.exam_seen_at IS NOT NULL AND a.started_at IS NOT NULL"), p).fetchone()
    entry_p50, entry_p90, entry_slow = (float(row[0]) if row[0] is not None else None, float(row[1]) if row[1] is not None else None, int(row[2] or 0))
    offline = _scalar(session, f"SELECT count(*) {base} AND a.status = 'IN_PROGRESS' AND a.exam_seen_at IS NOT NULL "
                               f"AND (a.last_seen_at IS NULL OR a.last_seen_at < :now - make_interval(secs => {OFFLINE_AFTER_S}))", **p)
    ev = lambda types, mins: _scalar(session,
        "SELECT count(DISTINCT l.attempt_id) FROM exam_activity_logs l JOIN exam_attempts a ON a.id = l.attempt_id "
        "WHERE a.exam_id = :e AND a.status = 'IN_PROGRESS' AND l.event_type = ANY(:t) AND l.timestamp > :now - make_interval(mins => :m)",
        t=list(types), m=mins, **p)
    video_link = ev(['live_monitoring_unavailable'], 10)
    camera_down = ev(['camera_unavailable'], 10)
    crashed = ev(['client_crash', 'client_unclean_exit', 'client_error'], 60)
    perm_err = ev(['permission_error'], 60)
    calls = _scalar(session, "SELECT count(*) FROM exam_activity_logs l JOIN exam_attempts a ON a.id = l.attempt_id "
                             "WHERE a.exam_id = :e AND l.event_type = 'student_call_request' AND l.timestamp > :now - interval '30 minutes'", **p)
    high_risk = _scalar(session, f"SELECT count(*) {base} AND a.status = 'IN_PROGRESS' AND a.exam_seen_at IS NOT NULL AND coalesce(a.risk_score, 0) >= 60", **p)

    pct = lambda n: round(100.0 * n / in_progress, 1) if in_progress else 0.0
    k = {
        'started': started, 'seen': seen, 'in_progress': in_progress, 'submitted': submitted,
        'stuck_entry': stuck, 'stuck_entry_pct': round(100.0 * stuck / started, 1) if started else 0.0,
        'entry_p50_s': None if entry_p50 is None else round(entry_p50), 'entry_p90_s': None if entry_p90 is None else round(entry_p90), 'entry_slow': entry_slow,
        'offline': offline, 'offline_pct': pct(offline),
        'video_link_down': video_link, 'video_link_pct': pct(video_link),
        'camera_down': camera_down, 'camera_down_pct': pct(camera_down),
        'client_crashed': crashed, 'client_crashed_pct': pct(crashed),
        'permission_errors': perm_err, 'call_requests_30min': calls,
        'high_risk': high_risk, 'high_risk_pct': pct(high_risk),
    }
    problems = []
    add = lambda code, level, msg, advice: problems.append({'code': code, 'niveau': level, 'message': msg, 'conseil': advice})
    if stuck >= STUCK_ENTRY_MIN_CRIT and k['stuck_entry_pct'] >= STUCK_ENTRY_PCT_CRIT:
        add('entree_bloquee', 'critique', f"{stuck} étudiants ({k['stuck_entry_pct']:.0f} %) attendent depuis plus de 3 minutes l'affichage de leur copie.",
            "Demander aux surveillants de leur écrire : actualiser la page puis « Reprendre ». Vérifier que le site répond.")
    elif entry_p90 is not None and entry_p90 > ENTRY_P90_ATTENTION_S and seen >= MIN_IN_PROGRESS:
        add('entree_lente', 'attention', f"L'entrée en examen est lente : 1 étudiant sur 10 a attendu plus de {round(entry_p90 / 60)} minutes.",
            "Surveiller ; si cela persiste, consulter le détail des étapes d'entrée dans le journal des étudiants concernés.")
    if in_progress >= MIN_IN_PROGRESS:
        if k['offline_pct'] >= OFFLINE_PCT_CRIT:
            add('hors_ligne', 'critique', f"{offline} étudiants ({k['offline_pct']:.0f} %) sont hors ligne en même temps.",
                "Probable coupure réseau côté étudiants ou serveur surchargé : vérifier l'état du site et prévenir les surveillants.")
        if k['video_link_pct'] >= VIDEO_LINK_PCT_ATTENTION:
            add('liaison_video', 'attention', f"La liaison vidéo est dégradée pour {video_link} étudiants ({k['video_link_pct']:.0f} %).",
                "Les surveillants peuvent voir des vignettes sans image ; elles se rétablissent seules. Vérifier le serveur vidéo si cela dure.")
        if k['high_risk_pct'] >= HIGH_RISK_PCT_ATTENTION:
            add('trop_d_alertes', 'attention', f"{high_risk} étudiants ({k['high_risk_pct']:.0f} %) sont classés à risque élevé : c'est anormalement haut.",
                "Les réglages de détection sont probablement trop sensibles pour cet examen : ne pas exclure sur le seul score.")
        if k['client_crashed_pct'] >= CRASH_PCT_ATTENTION:
            add('application_instable', 'attention', f"L'application CEI Examen s'est fermée de façon inattendue chez {crashed} étudiants ({k['client_crashed_pct']:.0f} %) cette dernière heure.",
                "Relever la version utilisée dans leur journal ; une mise à jour de l'application est peut-être nécessaire.")
    if calls >= CALLS_30MIN_ATTENTION:
        add('appels_attente', 'attention', f"{calls} demandes d'appel d'étudiants ces 30 dernières minutes.",
            "Des étudiants sont probablement bloqués : consulter la liste des étudiants hors ligne ou en attente.")
    level = max((x['niveau'] for x in problems), key=lambda l: LEVEL_ORDER[l], default='ok')
    return {'exam_id': exam_id, 'computed_at': now.isoformat() + 'Z', 'niveau': level, 'problems': problems, 'kpi': k,
            'verdict': {'ok': "Tout va bien.", 'attention': "Quelques points à surveiller.", 'critique': "Un problème demande une action immédiate."}[level]}


def watch_once(now=None):
    """Appelée par la tâche de fond (toutes les ~60 s) : pour chaque examen en cours, prévient l'administrateur d'un problème CRITIQUE (une fois par tranche de 10 minutes)."""
    from cache import cache_set_nx
    from models import get_session, OnlineExam, ExamStatus
    now = now or datetime.utcnow()
    s = get_session()
    sent = []
    try:
        exams = s.query(OnlineExam).filter(OnlineExam.status == ExamStatus.ACTIVE, OnlineExam.start_time <= now, OnlineExam.end_time >= now - timedelta(minutes=30)).all()
        for ex in exams:
            try:
                h = compute(s, ex.id, now)
                try:
                    from cache import cache_set
                    cache_set(f'cei:vitals:cache:{ex.id}', h, CACHE_S)       # amorce le cache lu par les pages (un seul calcul par minute, quel que soit le nombre de personnes qui regardent)
                except Exception:
                    pass
            except Exception as e:                           # un examen en défaut ne doit pas empêcher de surveiller les autres
                log.warning('santé examen %s : %s', ex.id, e); s.rollback(); continue
            for pb in h['problems']:
                if pb['niveau'] != 'critique':
                    continue
                if not cache_set_nx(f"cei:vitals:alert:{ex.id}:{pb['code']}", WATCH_COOLDOWN_S):
                    continue
                try:
                    from notif_bus import notify_admins
                    notify_admins('exam_health', f"Santé de l'examen « {ex.title} »", f"{pb['message']} {pb['conseil']}", priority='urgent', tags=['rotating_light'])
                except Exception as e:
                    log.warning('notification santé : %s', e)
                sent.append((ex.id, pb['code']))
    finally:
        s.close()
    return sent
