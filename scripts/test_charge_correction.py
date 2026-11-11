#!/usr/bin/env python
"""Correction d'une promotion entière : mêmes volumes que les vrais devoirs, avec une IA qui se trompe volontairement.

    cd cei-api-v2 && .venv-base/bin/python scripts/test_charge_correction.py [nombre de copies, défaut 1500]
    (1499 = devoir « Introduction au droit » du 28/10 ; 56 = évaluation du 03/11 ; 122 = devoir de Droit des TIC)

Vraie correction (notation automatique + file parallèle + enregistrement en base), seule l'IA est remplacée : elle répond en
0,03 s, et pour une copie sur sept elle déraille (ligne pour une question déjà notée, doublon, points au-dessus du plafond,
« tu n'as pas répondu » sur une question répondue, raisonnement à voix haute). Chaque note est comparée à celle qu'on attend.
Données temporaires, supprimées en fin de test.
"""
import os, sys, json, time, uuid, random, threading
from datetime import datetime, timedelta

N = int(sys.argv[1]) if len(sys.argv) > 1 else 1500
os.environ['CORRECTION_WORKERS'] = '24'
os.environ.update({'AI_CONCURRENCY': '24', 'AI_ACQUIRE_TIMEOUT': '120', 'DB_POOL_SIZE': '30', 'DB_MAX_OVERFLOW': '10'})
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as A
import correction_worker as W
from routes import exams as R
from services import correction_queue as Q
from cache import _get_client
from models import (get_session, User, UserRole, Subject, OnlineExam, ExamStatus, ExamAttempt, AttemptStatus, ExamActivityLog)
from werkzeug.security import generate_password_hash

RESULTS = []
def check(nom, ok, detail=''):
    RESULTS.append((nom, bool(ok)))
    print(('  ✔ ' if ok else '  ✘ ') + nom + (f'  — {detail!r}' if not ok else ''))

s = get_session(); tag = uuid.uuid4().hex[:6]; made = {'users': [], 'ex': None, 'sub': None}
A._last_sweep[0] = time.monotonic() + 1e9          # pas de filet automatique pendant le test
rc = _get_client()

# Sujet réaliste : 20 QCM (0,25) + 20 vrai/faux (0,25) + 2 questions ouvertes (2,5 pts chacune) = 15 points
N_QCM, N_VF = 20, 20
content = ''.join(f"Question {i} — Énoncé {i} ? (0,25 pts) [QCM]\nA) a\nB) b\nC) c\nD) d\n\n" for i in range(1, N_QCM + 1))
content += ''.join(f"Question {i} — Affirmation {i}. (0,25 pts) [VF]\nVrai / Faux\n\n" for i in range(N_QCM + 1, N_QCM + N_VF + 1))
content += "Question 41 — Expliquez la règle de droit. (2,5 pts) [OUVERT]\n\nQuestion 42 — Donnez un exemple. (2,5 pts) [OUVERT]\n"
KEY_QCM = {i: 'ABCD'[i % 4] for i in range(1, N_QCM + 1)}
KEY_VF = {i: ('Vrai' if i % 2 else 'Faux') for i in range(N_QCM + 1, N_QCM + N_VF + 1)}
rub = '=== BARÈME ===\n\n' + ''.join(f"Question {i} (0,25 pts) :\n  • Bonne réponse : {KEY_QCM[i]}) x\n\n" for i in range(1, N_QCM + 1)) \
    + ''.join(f"Question {i} (0,25 pts) :\n  • Réponse : {KEY_VF[i]}\n\n" for i in range(N_QCM + 1, N_QCM + N_VF + 1)) \
    + "Question 41 (2,5 pts) :\n  • Critère : définition\n\nQuestion 42 (2,5 pts) :\n  • Critère : exemple\n"
TOTAL = 15.0

rnd = random.Random(2810)
def make_answers(i):
    ans = {}
    for q in range(1, N_QCM + 1):
        ans[f'pq_{q}'] = KEY_QCM[q] if rnd.random() < 0.55 else rnd.choice('ABCD')
    for q in range(N_QCM + 1, N_QCM + N_VF + 1):
        ans[f'pq_{q}'] = KEY_VF[q] if rnd.random() < 0.6 else ('Faux' if KEY_VF[q] == 'Vrai' else 'Vrai')
    ans['pq_41'] = 'Définition de la règle.' if i % 11 else ''
    ans['pq_42'] = 'Un exemple.'
    return ans

def expected_det(ans):
    pts = sum(0.25 for q in range(1, N_QCM + 1) if (ans.get(f'pq_{q}') or '').upper() == KEY_QCM[q])
    pts += sum(0.25 for q in range(N_QCM + 1, N_QCM + N_VF + 1) if ans.get(f'pq_{q}') == KEY_VF[q])
    return pts

def fake_ai(system_prompt, user_message, **kw):
    """Réponse de l'IA selon le numéro de copie (reconnu dans le message)."""
    time.sleep(0.03)
    m = re_search(r'Étudiant: Charge Test (\d+)', user_message)
    i = int(m) if m is not None else 0
    p41, p42 = (i % 6) * 0.5, ((i // 6) % 6) * 0.5          # 0 à 2,5 chacune
    body = f"### Question 41\nPoints : {p41:.2f}/2.50\nRetour 41.\n\n### Question 42\nPoints : {p42:.2f}/2.50\nRetour 42.\n\n"
    if i % 7 == 0:                                             # copie « piégée »
        mode = (i // 7) % 4
        if mode == 0: body = "### Question 1\nPoints : 0.25/0.25\nBonne réponse.\n\n### Question 20\nPoints : 0.25/0.25\nBien.\n\n" + body   # questions déjà notées
        if mode == 1: body += "### Question 41\nPoints : 2.50/2.50\nDoublon.\n\n"                                                          # doublon
        if mode == 2: body = body.replace(f"Points : {p41:.2f}/2.50", "Points : 9.00/2.50", 1)                                                  # plafond dépassé
        if mode == 3: body = "Attendez, regardons la copie. Voilà : " + body.replace('Retour 42.', "Tu n'as pas répondu à cette question.")  # raisonnement + faux « pas répondu »
    return body + f"Points obtenus : {p41 + p42:.2f}"

import re
def re_search(pat, text):
    m = re.search(pat, text or '')
    return m.group(1) if m else None

try:
    adm = User(email=f'chg.{tag}.adm@test.invalid', full_name='Charge Adm', role=UserRole.ADMIN, is_active=True, email_verified=True, has_email=True,
               password_hash=generate_password_hash('x')); s.add(adm); s.commit(); made['users'].append(adm.id)
    t0 = time.time()
    users = [User(email=f'chg.{tag}.{i}@test.invalid', full_name=f'Charge Test {i}', role=UserRole.STUDENT, is_active=True, email_verified=True,
                  has_email=True, password_hash='x') for i in range(N)]
    s.add_all(users); s.commit(); made['users'] += [u.id for u in users]
    sub = Subject(title='charge', content=content, rubric=rub, creator_id=adm.id); s.add(sub); s.commit(); made['sub'] = sub.id
    now = datetime.utcnow()
    ex = OnlineExam(subject_id=sub.id, title='charge', duration_minutes=60, start_time=now - timedelta(hours=3), end_time=now - timedelta(hours=1),
                    status=ExamStatus.CLOSED, created_by_id=adm.id, auto_correct=True); s.add(ex); s.commit(); made['ex'] = ex.id
    answers, atts = {}, []
    for i, u in enumerate(users):
        ans = make_answers(i); a = ExamAttempt(exam_id=ex.id, student_id=u.id, status=AttemptStatus.SUBMITTED,
                                               submitted_at=now - timedelta(hours=1), answers=json.dumps(ans)); atts.append((i, a)); answers[i] = ans
    s.add_all([a for _, a in atts]); s.commit()
    ids = {i: a.id for i, a in atts}
    print(f"{N} copies créées en {time.time() - t0:.1f} s (sujet : {N_QCM} QCM + {N_VF} vrai/faux + 2 ouvertes, total {TOTAL:g} points)")
    for k in (Q.QUEUE, Q.LEASE, Q.META, Q.ALIVE, Q.PAUSE): rc.delete(k)
    rc.delete(Q._exam_key(ex.id)); rc.delete(Q._failed_key(ex.id))
    real_ai = R.call_claude; R.call_claude = fake_ai

    print('\n== Correction de toute la promotion par le service parallèle ==')
    W.stop.clear(); W.WORKERS = 24
    ts = [threading.Thread(target=W.supervisor_loop, daemon=True)] + [threading.Thread(target=W.worker_loop, daemon=True) for _ in range(24)]
    for t in ts: t.start()
    t0 = time.time()
    while not Q.worker_alive() and time.time() - t0 < 20: time.sleep(0.2)
    t0 = time.time(); Q.enqueue(list(ids.values()), ex.id)
    while Q.pending_count(ex.id) > 0 and time.time() - t0 < 600: time.sleep(0.5)
    dur = time.time() - t0
    s.expire_all()
    rows = {r.id: r for r in s.query(ExamAttempt).filter(ExamAttempt.exam_id == ex.id).all()}
    check(f"{N} copies corrigées en {dur:.0f} s ({N / max(dur, .1) * 60:.0f} copies/min)", all(rows[i].score is not None for i in ids.values()) and dur < 600, f'{dur:.0f}s')
    check("aucune copie restée sans note", sum(1 for r in rows.values() if r.score is None) == 0)

    bad_dup = bad_note = bad_open = bad_range = bad_txt = bad_review = 0; worst = None; by_mode = {}
    for i, aid in ids.items():
        r = rows[aid]; qs = json.loads(r.question_scores or '[]'); nums = [q['num'] for q in qs]
        if len(nums) != len(set(nums)): bad_dup += 1
        if any(q['type'] == 'open' and int(q['num']) <= 40 for q in qs): bad_open += 1
        if not (0 <= r.score <= 20): bad_range += 1
        p41, p42 = (i % 6) * 0.5, ((i // 6) % 6) * 0.5
        has41 = bool(answers[i].get('pq_41'))
        mode = (i // 7) % 4 if i % 7 == 0 else None
        ai = (2.5 if mode == 2 else min(p41, 2.5)) + min(p42, 2.5)      # mode 2 : l'IA écrit 9 points sur 2,5 → plafonnés à 2,5
        exp = round((expected_det(answers[i]) + min(ai, 5.0)) / TOTAL * 20, 2)
        if abs(r.score - exp) > 0.011:
            bad_note += 1; worst = worst or (i, mode, r.score, exp); by_mode[mode] = by_mode.get(mode, 0) + 1
        fb = ' '.join((q.get('feedback') or '') for q in qs) + (r.feedback or '')
        if 'Attendez' in fb or 'regardons' in fb.lower(): bad_txt += 1
        if has41 and any(q['num'] == '41' and 'pas répondu' in (q.get('feedback') or '') for q in qs): bad_review += 1
    check("aucune question notée deux fois (une seule ligne par numéro)", bad_dup == 0, bad_dup)
    check("aucune ligne « ouverte » sur une question déjà notée automatiquement", bad_open == 0, bad_open)
    check("toutes les notes entre 0 et 20", bad_range == 0, bad_range)
    check("chaque note = ce qui est attendu (notation automatique + points de l'IA plafonnés, ramenés sur 20)", bad_note == 0, (bad_note, worst, by_mode))
    check("aucun raisonnement de l'IA dans les retours", bad_txt == 0, bad_txt)
    check("aucun « tu n'as pas répondu » sur une question répondue", bad_review == 0, bad_review)
    p = Q.pending_count(ex.id)
    check("la file est vide et aucun échec définitif", p == 0 and not rc.llen(Q._failed_key(ex.id)) if hasattr(rc, 'llen') else p == 0)
finally:
    try: W.stop.set()
    except Exception: pass
    try: R.call_claude = real_ai
    except Exception: pass
    s.rollback()
    try:
        if made['ex']:
            att = [x[0] for x in s.query(ExamAttempt.id).filter(ExamAttempt.exam_id == made['ex'])]
            for k in range(0, len(att), 500):
                s.query(ExamActivityLog).filter(ExamActivityLog.attempt_id.in_(att[k:k + 500])).delete(synchronize_session=False)
            s.query(ExamAttempt).filter(ExamAttempt.exam_id == made['ex']).delete(synchronize_session=False)
            s.query(OnlineExam).filter(OnlineExam.id == made['ex']).delete(synchronize_session=False)
        if made['sub']: s.query(Subject).filter(Subject.id == made['sub']).delete(synchronize_session=False)
        if made['users']:
            for k in range(0, len(made['users']), 500):
                s.query(User).filter(User.id.in_(made['users'][k:k + 500])).delete(synchronize_session=False)
        s.commit()
    except Exception as e:
        s.rollback(); print('nettoyage incomplet :', e)
    s.close()
bad = [n for n, ok in RESULTS if not ok]
print(f"\n{len(RESULTS) - len(bad)}/{len(RESULTS)} vérifications réussies" + (f" — ÉCHECS : {bad}" if bad else ''))
sys.exit(1 if bad else 0)
