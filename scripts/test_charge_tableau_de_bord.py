#!/usr/bin/env python
"""Tableau de bord, échéances par vague, verrou post-soumission, stats, plagiat : coût indépendant du nombre d'étudiants (19/11/2026, objectif 10 000 utilisateurs).

    cd cei-api-v2 && .venv-base/bin/python scripts/test_charge_tableau_de_bord.py     (données temporaires, supprimées)

Mesuré avant correction : la liste des examens d'un étudiant, pendant un examen PAR VAGUES de 3 000 copies, coûtait 11 968 requêtes SQL et 12 s (rafraîchie toutes les 30 s par chaque étudiant).
"""
import os, sys, uuid, json, threading, random
from datetime import datetime, timedelta
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as A
from sqlalchemy import event
from models import (get_session, engine, User, UserRole, Subject, OnlineExam, ExamStatus, EC, StudentUEEnrollment, ExamAttempt, AttemptStatus, ExamActivityLog,
                    ExamWave, ExamWaveStudent)
from auth_paseto import create_access_token
from werkzeug.security import generate_password_hash
from services import exam_closer as EC_, exam_waves as W

RESULTS = []
def check(nom, ok, detail=''):
    RESULTS.append((nom, bool(ok))); print(('  ✔ ' if ok else '  ✘ ') + nom + (f'  — {detail}' if detail and not ok else ''))
s = get_session(); tag = uuid.uuid4().hex[:6]; c = A.app.test_client(); MAIN = threading.get_ident(); cnt = {'n': 0}
@event.listens_for(engine, 'before_cursor_execute')
def _b(conn, cur, st, par, ctx, many):
    if threading.get_ident() == MAIN: cnt['n'] += 1
def H(u): return {'Authorization': 'Bearer ' + create_access_token(u.id, u.role.value, u.email)}
made = {'users': [], 'ex': [], 'sub': None}
def mk(role, n):
    u = User(email=f'cdb.{tag}.{n}@test.invalid', full_name=f'CDB {n}', role=role, is_active=True, email_verified=True, has_email=True, password_hash=generate_password_hash('x'))
    s.add(u); s.commit(); made['users'].append(u.id); return u
now = datetime.utcnow()
def wave_exam(n_students, title):
    """Examen par vagues en cours : V1 terminée depuis 2 min, V2 ouverte ; n étudiants en cours, moitié dans chaque vague."""
    ex = OnlineExam(subject_id=made['sub'], title=title, duration_minutes=60, start_time=now - timedelta(minutes=40), end_time=now + timedelta(hours=1), status=ExamStatus.ACTIVE, created_by_id=adm.id)
    s.add(ex); s.commit(); made['ex'].append(ex.id)
    w1 = ExamWave(exam_id=ex.id, position=1, name='V1', start_time=now - timedelta(minutes=40), end_time=now - timedelta(minutes=2))
    w2 = ExamWave(exam_id=ex.id, position=2, name='V2', start_time=now - timedelta(minutes=30), end_time=now + timedelta(hours=1)); s.add_all([w1, w2]); s.commit()
    studs = [mk(UserRole.STUDENT, f'{title}{i}') for i in range(n_students)]
    s.add_all([StudentUEEnrollment(student_id=u.id, ue_id=ec.ue_id) for u in studs]); s.commit()
    s.query(ExamWaveStudent).filter_by(exam_id=ex.id).delete(); s.commit()          # le rattachement automatique des inscrits a pu les placer : on fixe nous-mêmes la répartition
    s.add_all([ExamWaveStudent(wave_id=(w1 if i % 2 == 0 else w2).id, exam_id=ex.id, student_id=u.id) for i, u in enumerate(studs)]); s.commit()
    s.add_all([ExamAttempt(exam_id=ex.id, student_id=u.id, status=AttemptStatus.IN_PROGRESS, started_at=now - timedelta(minutes=30), exam_seen_at=now - timedelta(minutes=29),
                           last_seen_at=now, answers='{"1":"A"}') for u in studs]); s.commit()
    return ex, studs, w1, w2
try:
    adm = mk(UserRole.ADMIN, 'adm')
    ec = s.query(EC).filter(EC.ue_id.isnot(None)).first()
    sub = Subject(title='cdb', content='1. Q ? [QCM]\nA. a\nB. b\n', rubric='Question 1 (1 pt) : A', creator_id=adm.id, ec_id=ec.id); s.add(sub); s.commit(); made['sub'] = sub.id

    print('\n== Liste des examens (étudiant) : coût indépendant du nombre d\'étudiants ==')
    exA, stA, _, _ = wave_exam(8, 'petit')
    exB, stB, _, _ = wave_exam(160, 'grand')
    # les deux examens sont visibles par leurs étudiants ; on mesure le même appel dans les deux cas (la 1re passe applique les échéances, on mesure la 2e)
    pet, gr = stA[1], stB[1]                                # étudiants de la vague OUVERTE
    c.get('/api/online_exams', headers=H(pet)); c.get('/api/online_exams', headers=H(gr))
    cnt['n'] = 0; r1 = c.get('/api/online_exams', headers=H(pet)); n1 = cnt['n']
    cnt['n'] = 0; r2 = c.get('/api/online_exams', headers=H(gr)); n2 = cnt['n']
    check("la liste répond (200) dans les deux cas", r1.status_code == 200 and r2.status_code == 200, (r1.status_code, r2.status_code))
    check(f"même nombre de requêtes SQL avec 8 ou 160 copies en cours ({n1} / {n2}) et peu nombreuses (< 60)", abs(n1 - n2) <= 3 and n2 < 60, (n1, n2))

    print('\n== Échéance de la vague : soumission des seules copies échues ==')
    s.expire_all()
    done1 = [s.query(ExamAttempt).filter_by(exam_id=exB.id, student_id=u.id).one() for u in stB[0::2]]       # vague 1 (terminée)
    open2 = [s.query(ExamAttempt).filter_by(exam_id=exB.id, student_id=u.id).one() for u in stB[1::2]]       # vague 2 (ouverte)
    check("vague terminée : toutes les copies soumises automatiquement", all(a.status == AttemptStatus.AUTO_SUBMITTED and a.submitted_at for a in done1), [a.status for a in done1][:3])
    check("vague ouverte : aucune copie touchée", all(a.status == AttemptStatus.IN_PROGRESS for a in open2))

    print('\n== Temps accordé : respecté ; crédit de pause : non compté ==')
    exC, stC, w1C, _ = wave_exam(6, 'extra')
    atts = [s.query(ExamAttempt).filter_by(exam_id=exC.id, student_id=u.id).one() for u in stC[0::2]]         # 3 copies de la vague terminée
    atts[0].extra_minutes = 30                                  # temps accordé : reste en cours
    atts[1].extra_minutes = 3; atts[1].pause_used = True        # seulement le crédit de pause : échue
    s.commit()
    c.get('/api/online_exams', headers=H(stC[1]))
    s.expire_all()
    st_ = [s.get(ExamAttempt, a.id).status for a in atts]
    check("temps accordé (30 min) : toujours en cours", st_[0] == AttemptStatus.IN_PROGRESS, st_)
    check("crédit de pause seul : soumise", st_[1] == AttemptStatus.AUTO_SUBMITTED, st_)
    check("sans temps accordé : soumise", st_[2] == AttemptStatus.AUTO_SUBMITTED, st_)

    print('\n== Équivalence avec l\'ancien calcul d\'échéance (cas aléatoires) ==')
    exD, stD, w1D, w2D = wave_exam(40, 'equiv')
    rng = random.Random(1911)
    rows = s.query(ExamAttempt).filter_by(exam_id=exD.id).all()
    for a in rows:
        a.extra_minutes = rng.choice([0, 0, 3, 10, 30, 90]); a.pause_used = rng.random() < 0.3
    s.commit(); s.expire_all()
    exD = s.get(OnlineExam, exD.id); ref = set(); rows = s.query(ExamAttempt).filter_by(exam_id=exD.id).all()
    for grace in (0, 60, 1200):
        ref = {a.id for a in rows if datetime.utcnow() > W.attempt_deadline(s, exD, a) + timedelta(seconds=grace)}
        got, n = EC_.expired_attempt_ids(s, exD, datetime.utcnow(), grace)
        check(f"marge {grace} s : mêmes copies échues que l'ancien calcul ({len(ref)} sur {n})", set(got) == ref and n == len(rows), (len(got), len(ref)))
    check("EXISTS (wave_expiry_possible) cohérent avec le calcul complet", EC_.wave_expiry_possible(s, exD, datetime.utcnow()) == bool({a.id for a in rows if datetime.utcnow() > W.attempt_deadline(s, exD, a)}))

    print('\n== Verrou après soumission ==')
    ex1 = made['ex'][0]
    u_lock, u_old, u_auto = stA[1], stA[3], stA[5]          # vague OUVERTE : le verrou ne dépasse jamais la fin de la vague de l'étudiant
    for u, mins, stt in ((u_lock, 1, AttemptStatus.SUBMITTED), (u_old, 10, AttemptStatus.SUBMITTED), (u_auto, 1, AttemptStatus.AUTO_SUBMITTED)):
        a = s.query(ExamAttempt).filter_by(exam_id=ex1, student_id=u.id).one(); a.status = stt; a.submitted_at = datetime.utcnow() - timedelta(minutes=mins)
    s.commit()
    # l'examen « petit » finit dans 1 h : le verrou de 3 min s'applique
    r = c.get('/api/student/post_submit_lock', headers=H(u_lock)); check("soumise il y a 1 min : verrouillé", r.status_code == 200 and r.get_json()['locked'] is True, r.get_json())
    r = c.get('/api/student/post_submit_lock', headers=H(u_old)); check("soumise il y a 10 min : libre", r.get_json()['locked'] is False, r.get_json())
    r = c.get('/api/student/post_submit_lock', headers=H(u_auto)); check("soumise automatiquement : pas de verrou (comportement conservé)", r.get_json()['locked'] is False, r.get_json())
    cnt['n'] = 0; c.get('/api/student/post_submit_lock', headers=H(u_old)); check(f"un étudiant libre coûte peu de requêtes ({cnt['n']} ≤ 4)", cnt['n'] <= 4, cnt['n'])

    print('\n== Statistiques de l\'examen ==')
    ex2 = made['ex'][1]
    base = s.query(ExamAttempt).filter_by(exam_id=ex2, status=AttemptStatus.AUTO_SUBMITTED).count()      # copies déjà soumises par l'échéance de la vague 1
    for i, a in enumerate(s.query(ExamAttempt).filter_by(exam_id=ex2, status=AttemptStatus.IN_PROGRESS).order_by(ExamAttempt.id).limit(20)):
        a.status = AttemptStatus.SUBMITTED; a.submitted_at = now; a.score = [4, 8, 12, 15, 18][i % 5]; a.risk_score = 80 if i < 4 else 10
    s.commit()
    r = c.get(f'/api/online_exams/{ex2}/stats', headers=H(adm)); j = r.get_json() or {}
    check("stats : 200 et totaux cohérents", r.status_code == 200 and j.get('submitted') == 20 + base and j.get('corrected') == 20, j)
    check("stats : moyenne, distribution, risque élevé", j.get('avg_score') == round(sum([4, 8, 12, 15, 18] * 4) / 20, 2) and j.get('distribution') == [4, 4, 4, 4, 4] and j.get('high_risk_count') == 4, j)

    print('\n== Contrôle de plagiat borné ==')
    ex3 = made['ex'][2]
    txt = json.dumps({'1': 'Les enzymes sont des catalyseurs biologiques qui accélèrent les réactions chimiques dans la cellule vivante.'})
    for a in s.query(ExamAttempt).filter_by(exam_id=ex3).order_by(ExamAttempt.id).limit(2):
        a.status = AttemptStatus.SUBMITTED; a.answers = txt
    s.commit()
    r = c.get(f'/api/online_exams/{ex3}/plagiarism-check', headers=H(adm)); j = r.get_json() or {}
    check("deux copies identiques : détectées, résultat complet", r.status_code == 200 and j.get('total_pairs') == 1 and j.get('partial') is False, j)
    os.environ['PLAGIARISM_BUDGET_S'] = '-1'
    r = c.get(f'/api/online_exams/{ex3}/plagiarism-check', headers=H(adm)); j = r.get_json() or {}
    check("budget de temps épuisé : réponse rapide signalée « partielle »", r.status_code == 200 and j.get('partial') is True, j)
    os.environ.pop('PLAGIARISM_BUDGET_S', None)
finally:
    s.rollback()
    try:
        from models import ExamAccessCode
        ids = made['ex']
        if ids:
            att = [a.id for a in s.query(ExamAttempt.id).filter(ExamAttempt.exam_id.in_(ids))]
            for b in range(0, len(att), 500):
                part = att[b:b + 500]
                s.query(ExamAccessCode).filter(ExamAccessCode.attempt_id.in_(part)).delete(synchronize_session=False)
                s.query(ExamActivityLog).filter(ExamActivityLog.attempt_id.in_(part)).delete(synchronize_session=False)
            s.query(ExamAttempt).filter(ExamAttempt.exam_id.in_(ids)).delete(synchronize_session=False)
            s.query(ExamWaveStudent).filter(ExamWaveStudent.exam_id.in_(ids)).delete(synchronize_session=False)
            s.query(ExamWave).filter(ExamWave.exam_id.in_(ids)).delete(synchronize_session=False)
            s.query(OnlineExam).filter(OnlineExam.id.in_(ids)).delete(synchronize_session=False)
        if made['users']: s.query(StudentUEEnrollment).filter(StudentUEEnrollment.student_id.in_(made['users'])).delete(synchronize_session=False)
        if made['sub']: s.query(Subject).filter_by(id=made['sub']).delete()
        if made['users']: s.query(User).filter(User.id.in_(made['users'])).delete(synchronize_session=False)
        s.commit()
    except Exception as e:
        s.rollback(); print('nettoyage incomplet :', e)
    s.close()
bad = [n for n, ok in RESULTS if not ok]
print(f"\n{len(RESULTS) - len(bad)}/{len(RESULTS)} vérifications réussies" + (f" — ÉCHECS : {bad}" if bad else ''))
sys.exit(1 if bad else 0)
