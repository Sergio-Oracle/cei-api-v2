#!/usr/bin/env python
"""Avis à l'étudiant quand une note déjà publiée change (réclamation « mail 5, site 7,75 »).

    cd cei-api-v2 && .venv-base/bin/python scripts/test_avis_changement_note.py

Vraie base (données temporaires supprimées en fin de test) ; seuls l'envoi d'e-mail et la cloche sont interceptés.
"""
import os, sys, json, time, uuid
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as A
from services import grade_change_notice as G
from models import get_session, User, UserRole, Subject, OnlineExam, ExamStatus, ExamAttempt, AttemptStatus, ExamActivityLog
import utils, notif_bus

RESULTS = []
def check(nom, ok, detail=''):
    RESULTS.append((nom, bool(ok)))
    print(('  ✔ ' if ok else '  ✘ ') + nom + (f'  — {detail!r}' if not ok else ''))

# ── 1. Décisions pures ────────────────────────────────────────────────────────
print('== Décisions ==')
check('résultats non publiés : aucun avis', not G.should_notify(5, 7.75, False))
check('note inchangée : aucun avis', not G.should_notify(7.75, 7.75, True))
check('écart de 0,004 point : aucun avis', not G.should_notify(7.75, 7.754, True))
check('5 → 7,75 publié : avis', G.should_notify(5, 7.75, True))
check('pas de note avant, une note après la publication : avis', G.should_notify(None, 12, True))
check('pas de note après : aucun avis', not G.should_notify(5, None, True))
check('format « 7,75 » et « 12 »', G.fmt(7.75) == '7,75' and G.fmt(12.0) == '12' and G.fmt(10.5) == '10,5')
check('message avec ancienne et nouvelle note', '5/20 → 7,75/20' in G.message(5, 7.75, 'Droit'))

# ── 2. Avec la vraie base ─────────────────────────────────────────────────────
tag = uuid.uuid4().hex[:8]
s = get_session()
made = {'users': [], 'sub': None, 'ex': None}
mails, bells = [], []
utils.send_grade_updated_email = lambda to, name, title, old, new, pid: mails.append((to, old, new, pid))
notif_bus.notify_user = lambda uid, typ, title, msg, *a, **k: bells.append((uid, typ, msg))

def attente(n=60):
    for _ in range(n * 10):
        with G._lock:
            fini = not G._pending and (G._worker is None or not G._worker.is_alive())
        if fini: return
        time.sleep(0.1)

def etat(att_id, score, published=None):
    s2 = get_session()
    try:
        a = s2.query(ExamAttempt).filter_by(id=att_id).first(); a.score = score
        if published is not None: a.exam.results_published = published
        s2.commit()
    finally: s2.close()

try:
    adm = User(email=f'avis.{tag}.adm@test.invalid', full_name='Avis Adm', role=UserRole.ADMIN, is_active=True, email_verified=True, has_email=True, password_hash='x')
    s.add(adm); s.commit(); made['users'].append(adm.id)
    studs = [User(email=f'avis.{tag}.{i}@test.invalid', full_name=f'Avis {i}', role=UserRole.STUDENT, is_active=True, email_verified=True, has_email=True, password_hash='x') for i in range(6)]
    temp = User(email=f'avis.{tag}.t@temp.edu', full_name='Temp', role=UserRole.STUDENT, is_active=True, email_verified=True, has_email=True, password_hash='x')
    s.add_all(studs + [temp]); s.commit(); made['users'] += [u.id for u in studs] + [temp.id]
    sub = Subject(title='avis', content='Question 1 (1 pt) : x', rubric='Question 1 : x', creator_id=adm.id); s.add(sub); s.commit(); made['sub'] = sub.id
    now = datetime.utcnow()
    ex = OnlineExam(subject_id=sub.id, title='Avis test', duration_minutes=60, start_time=now - timedelta(hours=3), end_time=now - timedelta(hours=1),
                    status=ExamStatus.CLOSED, created_by_id=adm.id, results_published=True); s.add(ex); s.commit(); made['ex'] = ex.id
    atts = [ExamAttempt(exam_id=ex.id, student_id=u.id, status=AttemptStatus.SUBMITTED, submitted_at=now - timedelta(hours=1), score=5.0) for u in studs + [temp]]
    s.add_all(atts); s.commit(); ids = [a.id for a in atts]

    print('\n== Une correction après publication ==')
    etat(ids[0], 7.75); G.schedule(ids[0], 5.0, True, delay=0); attente()
    check('un e-mail : 5 → 7,75, adressé à l\'étudiant', mails == [(studs[0].email, 5.0, 7.75, ids[0])], mails)
    check('une notification dans la cloche, avec la nouvelle note', len(bells) == 1 and '7,75/20' in bells[0][2] and bells[0][0] == studs[0].id, bells)
    s.expire_all()
    journal = s.query(ExamActivityLog).filter_by(attempt_id=ids[0], event_type='grade_change_notified').all()
    check('trace dans l\'historique de la copie', len(journal) == 1 and json.loads(journal[0].event_data)['new_score'] == 7.75)

    print('\n== Le même changement n\'est jamais annoncé deux fois ==')
    G.schedule(ids[0], 5.0, True, delay=0); attente()
    check('aucun deuxième e-mail pour la même note', len(mails) == 1, mails)

    print('\n== Plusieurs ajustements de suite = un seul avis ==')
    mails.clear(); bells.clear()
    etat(ids[1], 6.0); G.schedule(ids[1], 5.0, True, delay=1)
    etat(ids[1], 8.5); G.schedule(ids[1], 6.0, True, delay=1)
    etat(ids[1], 9.25); G.schedule(ids[1], 8.5, True, delay=1); attente()
    check('un seul e-mail, de la note de départ (5) à la note finale (9,25)', mails == [(studs[1].email, 5.0, 9.25, ids[1])], mails)

    print('\n== Cas où rien ne doit partir ==')
    mails.clear(); bells.clear()
    G.schedule(ids[2], 5.0, True, delay=0); attente()                         # note inchangée
    check('note inchangée : aucun e-mail', not mails and not bells)
    etat(ids[3], 7.0, published=False); G.schedule(ids[3], 5.0, False, delay=0); attente()
    check('résultats non publiés : aucun e-mail', not mails and not bells)
    G.schedule(ids[3], 5.0, True, delay=0); attente()                         # appelant qui se trompe : le fil revérifie en base
    check('résultats dépubliés entre-temps : le fil revérifie et n\'envoie rien', not mails and not bells)
    etat(ids[3], 7.0, published=True)
    etat(ids[6], 9.0); G.schedule(ids[6], 5.0, True, delay=0); attente()
    check('adresse @temp.edu : cloche seulement, pas d\'e-mail', not mails and len(bells) == 1, (mails, bells))

    print('\n== Copie corrigée après la publication (aucune note avant) ==')
    mails.clear(); bells.clear()
    etat(ids[4], 14.0); G.schedule(ids[4], None, True, delay=0); attente()
    check('avis « copie corrigée » avec la note, sans ancienne note', mails == [(studs[4].email, None, 14.0, ids[4])] and 'a été corrigée : 14/20' in bells[0][2], (mails, bells))

    print('\n== Rafale : 1 500 notes modifiées d\'un coup ==')
    mails.clear(); bells.clear()
    t0 = time.time(); threads_avant = len([t for t in __import__('threading').enumerate()])
    etat(ids[5], 11.0)
    for k in range(1500):
        G.schedule(ids[5] if k == 0 else 10 ** 9 + k, 5.0, True, delay=0)      # 1 499 copies inexistantes : ignorées sans erreur
    threads_pendant = len([t for t in __import__('threading').enumerate()])
    attente(120)
    check('un seul fil d\'arrière-plan pour toute la rafale', threads_pendant - threads_avant <= 1, threads_pendant - threads_avant)
    check('la vraie copie est avertie, les copies inexistantes ignorées', len(mails) == 1 and mails[0][3] == ids[5], len(mails))
    check(f'rafale traitée en {time.time() - t0:.1f} s sans exception', time.time() - t0 < 120)
finally:
    try:
        s.rollback()
        att = [x[0] for x in s.query(ExamAttempt.id).filter(ExamAttempt.exam_id == made['ex'])] if made['ex'] else []
        if att: s.query(ExamActivityLog).filter(ExamActivityLog.attempt_id.in_(att)).delete(synchronize_session=False)
        if made['ex']:
            s.query(ExamAttempt).filter(ExamAttempt.exam_id == made['ex']).delete(synchronize_session=False)
            s.query(OnlineExam).filter(OnlineExam.id == made['ex']).delete(synchronize_session=False)
        if made['sub']: s.query(Subject).filter(Subject.id == made['sub']).delete(synchronize_session=False)
        if made['users']: s.query(User).filter(User.id.in_(made['users'])).delete(synchronize_session=False)
        s.commit()
    except Exception as e:
        s.rollback(); print('nettoyage incomplet :', e)
    s.close()
    try:
        from cache import _get_client
        rc = _get_client()
        for k in rc.scan_iter('cei:gradechg:*'): rc.delete(k)
    except Exception: pass
bad = [n for n, ok in RESULTS if not ok]
print(f"\n{len(RESULTS) - len(bad)}/{len(RESULTS)} vérifications réussies" + (f" — ÉCHECS : {bad}" if bad else ''))
sys.exit(1 if bad else 0)
