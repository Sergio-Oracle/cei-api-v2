#!/usr/bin/env python
"""Note sur 20 : le total visé ne compte que les questions réellement posées, et l'IA ne peut plus compter deux fois.

    cd cei-api-v2 && .venv-base/bin/python scripts/test_bareme_correction.py

Reproduit l'anomalie de l'examen du 28/10/2026 : le barème décrivait 68 questions (14 + 6 points) alors que l'épreuve n'en
présentait que 56 (14 points). Les notes étaient donc sur 20 sans conversion (10,75 pour 43 bonnes réponses sur 56), puis
l'IA, à qui on laissait 6 points « restants », re-notait les questions déjà corrigées et ajoutait ses points (jusqu'à 20/20
avec des erreurs). Données temporaires, supprimées en fin de test.
"""
import os, sys, json, uuid
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as A
from routes import exams as R
from models import (get_session, User, UserRole, Subject, OnlineExam, ExamStatus, ExamAttempt, AttemptStatus, ExamActivityLog)
from werkzeug.security import generate_password_hash

RESULTS = []
def check(nom, ok, detail=''):
    RESULTS.append((nom, bool(ok)))
    print(('  ✔ ' if ok else '  ✘ ') + nom + (f'  — {detail!r}' if not ok else ''))

def qcm(n, pts='0,25', correct='C'):
    return f"Question {n} — Énoncé {n} ? ({pts} pts) [QCM]\nA) a\nB) b\nC) c\nD) d\n"
def bareme(n, pts='0,25', correct='C'):
    return f"Question {n} ({pts} pts) :\n  • Bonne réponse : {correct}) c\n\n"

s = get_session(); tag = uuid.uuid4().hex[:6]; made = {'users': [], 'ex': [], 'sub': []}
def mk(role, n):
    u = User(email=f'bar.{tag}.{n}@test.invalid', full_name=f'Bar Test {n}', role=role, is_active=True, email_verified=True,
             has_email=True, password_hash=generate_password_hash('x'))
    s.add(u); s.commit(); made['users'].append(u.id); return u
ai_calls = []
real_ai = R.call_claude
try:
    adm = mk(UserRole.ADMIN, 'adm'); st = [mk(UserRole.STUDENT, f'e{i}') for i in range(6)]
    now = datetime.utcnow()
    def setup(content, rubric, title):
        sub = Subject(title=title, content=content, rubric=rubric, creator_id=adm.id); s.add(sub); s.commit(); made['sub'].append(sub.id)
        ex = OnlineExam(subject_id=sub.id, title=title, duration_minutes=60, start_time=now - timedelta(hours=3), end_time=now - timedelta(hours=1),
                        status=ExamStatus.CLOSED, created_by_id=adm.id, auto_correct=True); s.add(ex); s.commit(); made['ex'].append(ex.id)
        return sub, ex
    def attempt(ex, u, answers):
        a = ExamAttempt(exam_id=ex.id, student_id=u.id, status=AttemptStatus.SUBMITTED, submitted_at=now - timedelta(hours=1),
                        answers=json.dumps(answers)); s.add(a); s.commit(); return a

    print('\n== Barème plus long que l\'épreuve (cas du 28/10) ==')
    content = ''.join(qcm(i) for i in range(1, 5))                                     # 4 questions posées : 1 point
    rub = '=== BARÈME ===\n\n' + ''.join(bareme(i) for i in range(1, 5)) + bareme(5, '0,5') + bareme(6, '0,5')   # + 1 point jamais posé
    sub, ex = setup(content, rub, 'bar 1')
    pm = R._question_points_map(sub.content, sub.rubric)
    check("seules les questions posées comptent", sorted(pm) == ['1', '2', '3', '4'] and round(sum(pm.values()), 2) == 1.0, pm)
    a = attempt(ex, st[0], {'pq_1': 'C', 'pq_2': 'C', 'pq_3': 'C', 'pq_4': 'A'})      # 3 bonnes sur 4
    def fake_ai(*args, **kwargs):
        ai_calls.append(1)
        return "Question 1 | Points : 0.25/0.25\nQuestion 2 | Points : 0.25/0.25\nQuestion 3 | Points : 0.25/0.25\nQuestion 4 | Points : 0.25/0.25\nQuestion 5 | Points : 0.50/0.50"
    R.call_claude = fake_ai
    R._run_auto_correction(a.id); s.expire_all(); a = s.get(ExamAttempt, a.id)
    check("3 bonnes réponses sur 4 = 15/20 (et non 7,5 ni 20)", a.score == 15.0, a.score)
    check("l'IA n'est pas appelée : plus rien à lui confier", not ai_calls, ai_calls)
    check("aucune question notée deux fois", len(json.loads(a.question_scores)) == 4, len(json.loads(a.question_scores)))

    print('\n== Recalcul des notes déjà publiées avec l\'ancienne méthode ==')
    from services.grade_recompute import recompute
    import tempfile, glob
    infl = [{'num': str(i), 'type': 'qcm', 'max': 0.25, 'score': (0.25 if i < 4 else 0.0), 'correct': i < 4, 'given': 'C'} for i in range(1, 5)] \
         + [{'num': str(i), 'type': 'open', 'max': 0.25, 'score': 0.25, 'feedback': 'x'} for i in range(1, 5)]
    def stored(u, score, qscores, by=121):
        x = attempt(ex, u, {'pq_1': 'C', 'pq_2': 'C', 'pq_3': 'C', 'pq_4': 'A'})
        x.score = score; x.feedback = 'ancien'; x.question_scores = qscores; x.corrected_by_id = None; s.commit(); return x
    inflated = stored(st[2], 20.0, json.dumps(infl))                       # points comptés deux fois
    manual = stored(st[3], 12.5, None)                                      # note saisie à la main
    edited_q = json.loads(json.dumps(infl)); edited_q[0]['score'] = 0.0     # détail retouché à la main
    edited = stored(st[4], 9.0, json.dumps(edited_q))
    blank = attempt(ex, st[5], {}); blank.score = 0.0; blank.question_scores = json.dumps([]); s.commit()
    r = recompute(s, ex.id, apply=False); s.expire_all()
    check("simulation : rien n'est écrit", s.get(ExamAttempt, inflated.id).score == 20.0 and r['changed'] == 1, r['changed'])
    check("copie vide, note manuelle et détail retouché laissés tels quels", r['skipped_empty'] == 1 and r['skipped_manual'] == 1 and r['skipped_edited'] == 1, (r['skipped_empty'], r['skipped_manual'], r['skipped_edited']))
    tmp = tempfile.mkdtemp()
    r = recompute(s, ex.id, apply=True, backup_dir=tmp); s.expire_all()
    inflated = s.get(ExamAttempt, inflated.id)
    check("la note gonflée (20) redevient 15/20", inflated.score == 15.0, inflated.score)
    check("le détail ne compte plus chaque question qu'une fois", len(json.loads(inflated.question_scores)) == 4)
    check("note manuelle et copie vide intactes", s.get(ExamAttempt, manual.id).score == 12.5 and s.get(ExamAttempt, blank.id).score == 0.0 and s.get(ExamAttempt, edited.id).score == 9.0)
    saved = json.load(open(r['backup']))
    check("l'ancienne valeur est sauvegardée dans un fichier", len(saved) == 1 and saved[0]['score'] == 20.0, saved)
    check("la modification est tracée dans l'historique de la copie", s.query(ExamActivityLog).filter_by(attempt_id=inflated.id, event_type='grade_recomputed').count() == 1)

    print('\n== Sujet avec une vraie question ouverte : points de l\'IA plafonnés ==')
    content2 = qcm(1, '1') + "Question 2 — Expliquez la règle de droit. (1 pts) [OUVERT]\n"
    rub2 = '=== BARÈME ===\n\n' + bareme(1, '1') + "Question 2 (1 pts) :\n  • Réponse attendue : définition\n"
    sub2, ex2 = setup(content2, rub2, 'bar 2')
    b = attempt(ex2, st[1], {'pq_1': 'C', 'pq_2': 'La règle de droit est générale.'})
    ai_calls.clear()
    FMT = "### Question 2\nPoints : 5.00/1.00\nTrès bien.\n\nPoints obtenus : 5.00"      # IA qui dépasse son plafond
    R.call_claude = lambda *a, **k: (ai_calls.append(1) or FMT)
    R._run_auto_correction(b.id); s.expire_all(); b = s.get(ExamAttempt, b.id)
    check("l'IA corrige la question ouverte", len(ai_calls) == 1)
    check("ses points sont plafonnés à ce qui lui revenait : 1 + 1 = 20/20, pas plus", b.score == 20.0, b.score)
    c = attempt(ex2, st[0], {'pq_1': 'A', 'pq_2': 'La règle de droit est générale.'})
    R.call_claude = lambda *a, **k: FMT
    R._run_auto_correction(c.id); s.expire_all(); c = s.get(ExamAttempt, c.id)
    check("QCM faux + ouverte entièrement juste = 10/20 (aucune note gonflée par l'IA)", c.score == 10.0, c.score)
finally:
    R.call_claude = real_ai
    s.rollback()
    try:
        if made['ex']:
            att = [a.id for a in s.query(ExamAttempt).filter(ExamAttempt.exam_id.in_(made['ex']))]
            if att: s.query(ExamActivityLog).filter(ExamActivityLog.attempt_id.in_(att)).delete(synchronize_session=False)
            s.query(ExamAttempt).filter(ExamAttempt.exam_id.in_(made['ex'])).delete(synchronize_session=False)
            s.query(OnlineExam).filter(OnlineExam.id.in_(made['ex'])).delete(synchronize_session=False)
        if made['sub']: s.query(Subject).filter(Subject.id.in_(made['sub'])).delete(synchronize_session=False)
        if made['users']: s.query(User).filter(User.id.in_(made['users'])).delete(synchronize_session=False)
        s.commit()
    except Exception as e:
        s.rollback(); print('nettoyage incomplet :', e)
    s.close()
bad = [n for n, ok in RESULTS if not ok]
print(f"\n{len(RESULTS) - len(bad)}/{len(RESULTS)} vérifications OK" + (f" — ÉCHECS : {bad}" if bad else ''))
sys.exit(1 if bad else 0)
