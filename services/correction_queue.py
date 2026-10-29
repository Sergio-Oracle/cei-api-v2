"""File d'attente de correction IA : les copies à corriger sont déposées ici et traitées en parallèle par le service
`correction_worker.py` (processus à part), au lieu d'un fil par copie dans l'API ou d'une boucle copie après copie.

La façon de corriger ne change pas : le service appelle les mêmes fonctions que l'API (`_run_auto_correction`,
`_run_ai_correction`). Ce module ne fait que répartir le travail :

- file Redis, une entrée par copie (jamais deux fois : redépôt sans effet), copies déjà corrigées ignorées ;
- verrou temporaire par copie en cours : un service arrêté en pleine correction ne la perd pas, elle revient dans la file ;
- reprises automatiques espacées (30 s, 2 min, 5 min, 15 min) avant de déclarer un échec définitif ;
- disjoncteur : si le fournisseur IA ne répond plus, le service se met en pause une minute au lieu de tout faire échouer ;
- si le service ne tourne pas, les producteurs gardent leur ancien comportement (voir `worker_alive`).
"""
import json
import time

from cache import _get_client

QUEUE = 'cei:corr:q'            # zset : copie -> heure à laquelle elle peut être prise
LEASE = 'cei:corr:lease'        # zset : copie en cours -> fin du verrou
META = 'cei:corr:meta'          # hash : copie -> {"by": id de l'utilisateur ou null, "exam": id, "tries": n}
ALIVE = 'cei:corr:alive'        # présent tant que le service tourne
PAUSE = 'cei:corr:pause'        # présent pendant une pause du disjoncteur
LEASE_SECONDS = 15 * 60
RETRY_DELAYS = [30, 120, 300, 900]
MAX_TRIES = len(RETRY_DELAYS) + 1


def _exam_key(exam_id) -> str:
    return f'cei:corr:exam:{exam_id}'


def worker_alive() -> bool:
    c = _get_client()
    try:
        return bool(c and c.exists(ALIVE))
    except Exception:
        return False


def _failed_key(exam_id) -> str:
    return f'cei:corr:failed:{exam_id}'


def failed_ids(exam_id: int) -> set:
    c = _get_client()
    return {int(x) for x in c.smembers(_failed_key(exam_id))} if c else set()


def enqueue(attempt_ids, exam_id: int, by_id=None, force: bool = False) -> int:
    """Dépose des copies dans la file. Renvoie le nombre de copies réellement ajoutées (déjà présentes : ignorées).
    Une copie abandonnée après tous ses essais n'est pas redéposée par le filet automatique (sinon elle serait
    reprise sans fin) : seule une demande explicite (`force=True`) la remet en file."""
    c = _get_client()
    if c is None or not attempt_ids:
        return 0
    if force:
        c.delete(_failed_key(exam_id))
    else:
        skip = failed_ids(exam_id)
        attempt_ids = [a for a in attempt_ids if int(a) not in skip]
        if not attempt_ids:
            return 0
    now = time.time()
    added = 0
    pipe = c.pipeline()
    for aid in attempt_ids:
        pipe.zadd(QUEUE, {str(aid): now}, nx=True)
    results = pipe.execute()
    pipe = c.pipeline()
    for aid, ok in zip(attempt_ids, results):
        if ok:
            added += 1
            pipe.hset(META, str(aid), json.dumps({'by': by_id, 'exam': exam_id, 'tries': 0}))
            pipe.sadd(_exam_key(exam_id), str(aid))
    pipe.expire(_exam_key(exam_id), 7 * 86400)
    pipe.execute()
    return added


_POP = """
local now = tonumber(ARGV[1])
local ids = redis.call('ZRANGEBYSCORE', KEYS[1], '-inf', now, 'LIMIT', 0, 1)
if #ids == 0 then return false end
redis.call('ZREM', KEYS[1], ids[1])
redis.call('ZADD', KEYS[2], now + tonumber(ARGV[2]), ids[1])
return ids[1]
"""


def pop():
    """Prend la prochaine copie prête (et pose son verrou). Renvoie (id de copie, métadonnées) ou None."""
    c = _get_client()
    if c is None:
        return None
    aid = c.eval(_POP, 2, QUEUE, LEASE, time.time(), LEASE_SECONDS)
    if not aid:
        return None
    raw = c.hget(META, aid)
    meta = json.loads(raw) if raw else {'by': None, 'exam': None, 'tries': 0}
    return int(aid), meta


def done(attempt_id: int, meta: dict) -> None:
    """Copie terminée (corrigée ou abandonnée) : retirée de partout."""
    c = _get_client()
    c.zrem(LEASE, str(attempt_id))
    c.hdel(META, str(attempt_id))
    if meta.get('exam') is not None:
        c.srem(_exam_key(meta['exam']), str(attempt_id))


def retry(attempt_id: int, meta: dict) -> bool:
    """Remet la copie dans la file avec un délai ; False si le nombre d'essais est épuisé."""
    c = _get_client()
    tries = int(meta.get('tries', 0)) + 1
    c.zrem(LEASE, str(attempt_id))
    if tries >= MAX_TRIES:
        return False
    meta['tries'] = tries
    c.hset(META, str(attempt_id), json.dumps(meta))
    c.zadd(QUEUE, {str(attempt_id): time.time() + RETRY_DELAYS[tries - 1]})
    return True


def reap() -> int:
    """Remet dans la file les copies dont le verrou a expiré (service arrêté en pleine correction)."""
    c = _get_client()
    expired = c.zrangebyscore(LEASE, '-inf', time.time())
    for aid in expired:
        if c.zrem(LEASE, aid):
            c.zadd(QUEUE, {aid: time.time()}, nx=True)
    return len(expired)


def fail(attempt_id: int, meta: dict) -> None:
    """Copie abandonnée : retirée de la file, mémorisée comme échec."""
    c = _get_client()
    c.zrem(LEASE, str(attempt_id))
    c.hdel(META, str(attempt_id))
    if meta.get('exam') is not None:
        c.srem(_exam_key(meta['exam']), str(attempt_id))
        c.sadd(_failed_key(meta['exam']), str(attempt_id))
        c.expire(_failed_key(meta['exam']), 6 * 3600)


def pending_count(exam_id: int) -> int:
    c = _get_client()
    return int(c.scard(_exam_key(exam_id))) if c else 0


def running_count(exam_id=None) -> int:
    c = _get_client()
    return int(c.zcard(LEASE)) if c else 0


def queue_size() -> int:
    c = _get_client()
    return int(c.zcard(QUEUE)) if c else 0


def set_batch(exam_id: int, by_id) -> None:
    """Mémorise qu'un lot est en cours pour cet examen : l'enseignant sera prévenu quand il sera terminé."""
    c = _get_client()
    if c is not None and by_id:
        c.setex(f'cei:corr:batch:{exam_id}', 3 * 86400, str(by_id))


def pop_batch(exam_id: int):
    """Renvoie (et efface) l'utilisateur à prévenir si le lot de cet examen est terminé, sinon None."""
    c = _get_client()
    if c is None or pending_count(exam_id) > 0:
        return None
    raw = c.getdel(f'cei:corr:batch:{exam_id}')
    return int(raw) if raw else None


def reap_all() -> int:
    """Au démarrage du service (une seule instance) : toute copie « en cours » revient dans la file."""
    c = _get_client()
    n = 0
    for aid in c.zrange(LEASE, 0, -1):
        if c.zrem(LEASE, aid):
            c.zadd(QUEUE, {aid: time.time()}, nx=True)
            n += 1
    return n
