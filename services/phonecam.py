"""Caméra secondaire (smartphone) : état réel, dispense, contrôle d'accès à la copie (14/11/2026).

Incident du 14/11 (examen « TEST UFC », caméra secondaire exigée) : des étudiants sont entrés SANS caméra secondaire. Cause : l'obligation
n'était vérifiée côté page que si le scan de la pièce avait réussi (statut « ok ») ; en statut « dégradé » (connexion limitée, moteur IA
lent, caméra pas prête, aucune personne vue) le bouton « Continuer vers l'examen » s'activait quand même. Rien, côté serveur, ne la vérifiait.

Règle unique, appliquée par la page ET par le serveur : pour un examen avec `allow_secondary_camera`, la copie ne s'ouvre que si
  - le téléphone est RÉELLEMENT connecté à la salle LiveKit (vérifié en direct, pas seulement le drapeau posé au couplage), OU
  - le personnel (enseignant, surveillant affecté, administrateur, responsable d'examen) a DISPENSÉ cet étudiant (téléphone en panne,
    batterie vide… : sans cela un incident matériel bloquerait définitivement l'étudiant). La dispense est tracée (événement `phonecam_waived`).
Fail-open voulu : si LiveKit est injoignable on ne sait pas (None) et on NE bloque PAS — un incident serveur ne doit pas sortir les étudiants de l'examen.
"""
import json
import logging
import time
import urllib.request as urlreq

import jwt as pyjwt

from cache import cache_get, cache_set, cache_delete

log = logging.getLogger(__name__)

LINK_GRACE_SECONDS = 20          # le téléphone met quelques secondes à publier après l'échange du jeton (voir proctoring_routes)
WAIVE_TTL = 24 * 3600


def linked_key(attempt_id):
    return f'cei:phonecam:linked:{attempt_id}'


def waived_key(attempt_id):
    return f'cei:phonecam:waived:{attempt_id}'


def live_state(attempt_id, exam_id, student_id):
    """True = téléphone dans la salle ; False = absent (jamais couplé, ou parti) ; None = on ne sait pas (LiveKit injoignable / non configuré)."""
    data = cache_get(linked_key(attempt_id))
    if not data:
        return False                                  # jamais couplé (ou drapeau expiré après constat d'absence) : réponse certaine
    set_at = data.get('set_at', 0) if isinstance(data, dict) else 0
    from proctoring_routes import get_livekit_config
    config = get_livekit_config()
    if not all([config.get('url'), config.get('api_key'), config.get('api_secret')]):
        return None
    room = f'exam-{exam_id}'
    identity = f'student-{student_id}-phone'
    now = int(time.time())
    token = pyjwt.encode({'exp': now + 60, 'iss': config['api_key'], 'nbf': now, 'sub': 'admin',
                          'video': {'roomAdmin': True, 'room': room}}, config['api_secret'], algorithm='HS256')
    try:
        req = urlreq.Request(f"{config['api_url']}/twirp/livekit.RoomService/ListParticipants",
                             data=json.dumps({'room': room}).encode(),
                             headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'})
        with urlreq.urlopen(req, timeout=5) as resp:
            identities = [p.get('identity') for p in json.loads(resp.read()).get('participants', [])]
    except Exception as e:
        log.warning('phonecam: LiveKit injoignable (%s) — état inconnu', e)
        return None
    if identity in identities:
        return True
    if time.time() - set_at > LINK_GRACE_SECONDS:
        cache_delete(linked_key(attempt_id))          # constat d'absence au-delà du délai de grâce
    return False


def is_waived(session, attempt_id):
    """Dispense accordée par le personnel ? Redis d'abord (rapide), sinon le journal (durable : un redémarrage de Redis ne la perd pas)."""
    if cache_get(waived_key(attempt_id)):
        return True
    from models import ExamActivityLog
    hit = session.query(ExamActivityLog.id).filter_by(attempt_id=attempt_id, event_type='phonecam_waived').first()
    if hit:
        cache_set(waived_key(attempt_id), {'at': time.time()}, ttl=WAIVE_TTL)
        return True
    return False


def waive(session, attempt, by_user_id, by_name, reason=''):
    from models import ExamActivityLog
    session.add(ExamActivityLog(attempt_id=attempt.id, event_type='phonecam_waived',
                                event_data=json.dumps({'by': by_user_id, 'by_name': by_name, 'reason': (reason or '')[:200]}, ensure_ascii=False)))
    session.commit()
    cache_set(waived_key(attempt.id), {'at': time.time(), 'by': by_user_id}, ttl=WAIVE_TTL)


def access_blocked(session, attempt):
    """True seulement si l'examen exige la caméra secondaire, que l'étudiant n'est pas dispensé ET que son absence est CERTAINE."""
    exam = attempt.exam
    if not exam or not getattr(exam, 'allow_secondary_camera', False):
        return False
    state = live_state(attempt.id, attempt.exam_id, attempt.student_id)
    if state is not False:
        return False                                   # connecté, ou inconnu (fail-open)
    return not is_waived(session, attempt.id)
