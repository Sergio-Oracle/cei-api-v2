"""Avis à l'étudiant quand une note DÉJÀ PUBLIÉE change.

Cas réel (réclamation de novembre 2026) : l'étudiant reçoit « 5/20 » par e-mail à la publication, puis le professeur
corrige la copie et le site affiche 7,75 — sans aucun nouveau message. L'étudiant croit à une erreur de la plateforme.

Règles :
  - rien n'est envoyé tant que les résultats ne sont pas publiés (l'étudiant n'a vu aucune note) ;
  - rien n'est envoyé si la note ne change pas réellement (écart < 0,01) ;
  - plusieurs corrections rapprochées d'une même copie (le professeur ajuste question après question) donnent UN SEUL
    avis, envoyé quand la dernière date de plus de DELAY secondes, avec la note de départ et la note finale ;
  - une même nouvelle note n'est jamais annoncée deux fois (verrou Redis, valable entre processus) ;
  - l'envoi est sérialisé dans un seul fil d'arrière-plan, une copie après l'autre : un recalcul de 1 500 notes
    n'ouvre pas 1 500 connexions SMTP ni 1 500 fils ;
  - jamais bloquant, jamais d'exception vers l'appelant : la note est déjà enregistrée, l'avis est un plus.
"""
import json
import threading
import time

DELAY = 30            # secondes d'attente après la dernière modification d'une copie
_MIN_CHANGE = 0.01    # écart minimal (points sur 20) pour parler de « changement »

_lock = threading.Lock()
_pending = {}         # attempt_id -> {'old': note de départ ou None, 'due': instant d'envoi}
_worker = None


def fmt(x):
    """12.0 -> « 12 », 7.75 -> « 7,75 » (comme le site)."""
    s = f"{float(x):.2f}".rstrip('0').rstrip('.')
    return s.replace('.', ',')


def should_notify(old, new, published) -> bool:
    """Décision pure (testable) : faut-il prévenir l'étudiant ?"""
    if not published or new is None:
        return False
    if old is None:
        return True        # note publiée qui n'existait pas encore (copie corrigée après la publication)
    return abs(float(new) - float(old)) >= _MIN_CHANGE


def message(old, new, exam_title) -> str:
    if old is None:
        return f"Votre copie « {exam_title} » a été corrigée : {fmt(new)}/20."
    return f"Votre note pour « {exam_title} » a été mise à jour : {fmt(old)}/20 → {fmt(new)}/20."


def schedule(attempt_id, old_score, published=True, delay=None):
    """À appeler APRÈS l'enregistrement d'une note. `old_score` = la note avant la modification (None si aucune).
    `published` : exam.results_published (évite de planifier pour rien)."""
    try:
        if not published or attempt_id is None:
            return
        d = DELAY if delay is None else delay
        with _lock:
            cur = _pending.get(attempt_id)
            # la note de DÉPART est celle d'avant la première modification de la fenêtre
            _pending[attempt_id] = {'old': cur['old'] if cur else old_score, 'due': time.time() + d}
        _ensure_worker()
    except Exception as e:                               # pragma: no cover - ne jamais gêner l'appelant
        print(f"[grade_change_notice] planification impossible : {e}")


def _ensure_worker():
    global _worker
    with _lock:
        if _worker is not None and _worker.is_alive():
            return
        _worker = threading.Thread(target=_run, name='grade_change_notice', daemon=True)
        _worker.start()


def _run():
    global _worker
    while True:
        with _lock:
            now = time.time()
            due = [a for a, v in _pending.items() if v['due'] <= now]
            job = None
            if due:
                a = due[0]
                job = (a, _pending.pop(a)['old'])
            elif not _pending:
                _worker = None                           # déclaré fini SOUS le verrou : une planification suivante en relance un
                return                                   # plus rien à faire : le fil se termine
        if job is None:
            time.sleep(1.0)
            continue
        try:
            sent = _send(*job)
        except Exception as e:
            sent = False
            print(f"[grade_change_notice] copie {job[0]} : {e}")
        if sent:
            time.sleep(0.15)                             # cadence douce vers le serveur SMTP (seulement après un vrai envoi)


def _dedupe(attempt_id, new) -> bool:
    """True si on peut envoyer (première fois pour cette copie et cette note)."""
    try:
        from cache import _get_client
        r = _get_client()
        if r is None:
            return True
        return bool(r.set(f"cei:gradechg:{attempt_id}:{float(new):.2f}", '1', nx=True, ex=3600))
    except Exception:
        return True


def _send(attempt_id, old):
    from models import get_session, ExamAttempt, ExamActivityLog
    s = get_session()
    try:
        att = s.query(ExamAttempt).filter_by(id=attempt_id).first()
        if not att or att.exam is None:
            return False
        new = att.score
        if not should_notify(old, new, bool(att.exam.results_published)):
            return False
        if not _dedupe(attempt_id, new):
            return False
        title = att.exam.title or 'votre examen'
        student = att.student
        msg = message(old, new, title)
        if student is not None:
            try:
                from notif_bus import notify_user
                notify_user(student.id, 'grade_updated', 'Note mise à jour', msg,
                            extra={'exam_id': att.exam_id, 'attempt_id': att.id})
            except Exception as e:
                print(f"[grade_change_notice] cloche copie {attempt_id} : {e}")
            email = (student.email or '').strip()
            if email and '@temp.edu' not in email:
                try:
                    from utils import send_grade_updated_email
                    send_grade_updated_email(email, student.full_name, f"{title} (Examen en ligne)", old, new, att.id)
                except Exception as e:
                    print(f"[grade_change_notice] e-mail copie {attempt_id} : {e}")
        try:
            s.add(ExamActivityLog(attempt_id=att.id, event_type='grade_change_notified', event_data=json.dumps(
                {'old_score': old, 'new_score': new, 'timestamp': time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime())})))
            s.commit()
        except Exception:
            s.rollback()
        return True
    finally:
        s.close()
