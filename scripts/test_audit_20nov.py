#!/usr/bin/env python
"""Audit du 20/11/2026 : purge des jetons expirés + accueil du surveillant en chargement groupé.

    cd cei-api-v2 && .venv-base/bin/python scripts/test_audit_20nov.py     (données temporaires, supprimées)
"""
import os, sys, uuid, threading, json
from datetime import datetime, timedelta, timezone
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as A
from sqlalchemy import event
from models import (get_session, engine, User, UserRole, Subject, OnlineExam, ExamStatus, EC, StudentUEEnrollment, ExamAttempt, AttemptStatus, ExamProctor, ProctorAssignment, TokenBlocklist)
from auth_paseto import create_access_token
from werkzeug.security import generate_password_hash
from services import exam_closer

RESULTS = []
def check(nom, ok, detail=''):
    RESULTS.append((nom, bool(ok))); print(('  ✔ ' if ok else '  ✘ ') + nom + (f'  — {detail}' if detail and not ok else ''))
s = get_session(); tag = uuid.uuid4().hex[:6]; c = A.app.test_client(); MAIN = threading.get_ident(); cnt = {'n': 0}
@event.listens_for(engine, 'before_cursor_execute')
def _b(conn, cur, st, par, ctx, many):
    if threading.get_ident() == MAIN: cnt['n'] += 1
made = {'users': [], 'ex': None, 'sub': None, 'tok': []}
def mk(role, n):
    u = User(email=f'au.{tag}.{n}@test.invalid', full_name=f'Aud {n}', role=role, is_active=True, email_verified=True, has_email=True, password_hash=generate_password_hash('x'))
    s.add(u); s.commit(); made['users'].append(u.id); return u
def H(u): return {'Authorization': 'Bearer ' + create_access_token(u.id, u.role.value, u.email)}
try:
    adm = mk(UserRole.ADMIN, 'adm'); sv = mk(UserRole.SURVEILLANT, 'sv'); other = mk(UserRole.SURVEILLANT, 'sv2')

    print('\n== Purge des jetons de renouvellement expirés ==')
    exam_closer.purge_expired_tokens(batch=20000, max_batches=1000)       # la base locale contient d'anciens jetons échus : on les retire d'abord pour isoler le test
    now = datetime.now(timezone.utc)
    spec = [('vieux1', now - timedelta(days=3)), ('vieux2', now - timedelta(days=2)), ('vieux3', now - timedelta(days=5)), ('recent', now - timedelta(hours=1)), ('futur', now + timedelta(days=2))]
    for nom, exp in spec:
        t = TokenBlocklist(token_hash=(uuid.uuid4().hex + uuid.uuid4().hex)[:64], user_id=adm.id, expires_at=exp); s.add(t); s.commit(); made['tok'].append((nom, t.id))
    n = exam_closer.purge_expired_tokens(batch=2, max_batches=10)          # lots de 2 : la boucle par lots est exercée
    left = {nom for nom, i in made['tok'] if s.get(TokenBlocklist, i) is not None}
    s.expire_all(); left = {nom for nom, i in made['tok'] if s.query(TokenBlocklist.id).filter_by(id=i).first()}
    check("les 3 jetons échus depuis plus d'un jour sont supprimés (par lots de 2)", n == 3 and not (left & {'vieux1', 'vieux2', 'vieux3'}), (n, left))
    check("le jeton échu depuis 1 h et celui encore valide sont CONSERVÉS", {'recent', 'futur'} <= left, left)
    check("relancée : rien de plus à supprimer", exam_closer.purge_expired_tokens() == 0)

    print('\n== Alerte « sauvegarde de la base manquante » ==')
    import tempfile, time as _t, notif_bus
    bdir = tempfile.mkdtemp(); sent = []
    _orig = notif_bus.notify_admins; notif_bus.notify_admins = lambda *a, **k: sent.append(a)
    try:
        os.environ['BACKUP_DIR'] = bdir
        os.environ.pop('CEI_BACKUP_WATCH', None)
        check("contrôle inactif par défaut (poste de développement) : aucune alerte", exam_closer.check_backup_fresh() is False and not sent)
        os.environ['CEI_BACKUP_WATCH'] = '1'
        check("actif, aucune sauvegarde : alerte envoyée", exam_closer.check_backup_fresh() is True and len(sent) == 1 and 'aucune sauvegarde' in sent[0][2], sent)
        open(os.path.join(bdir, 'LAST_OK'), 'w').close()
        check("sauvegarde récente : aucune alerte", exam_closer.check_backup_fresh() is False and len(sent) == 1)
        old = _t.time() - 40 * 3600; os.utime(os.path.join(bdir, 'LAST_OK'), (old, old))
        check("sauvegarde vieille de 40 h : alerte avec l'âge", exam_closer.check_backup_fresh() is True and len(sent) == 2 and '40 h' in sent[1][2], sent[-1:])
    finally:
        notif_bus.notify_admins = _orig; os.environ.pop('CEI_BACKUP_WATCH', None); os.environ.pop('BACKUP_DIR', None)

    print('\n== Accueil du surveillant : même résultat, requêtes bornées ==')
    ec = s.query(EC).filter(EC.ue_id.isnot(None)).first()
    sub = Subject(title='au', content='1. Q ? [QCM]\nA. a\nB. b\n', rubric='Question 1 (1 pt) : A', creator_id=adm.id, ec_id=ec.id); s.add(sub); s.commit(); made['sub'] = sub.id
    now_n = datetime.utcnow()
    ex = OnlineExam(subject_id=sub.id, title='au', duration_minutes=60, start_time=now_n - timedelta(minutes=20), end_time=now_n + timedelta(hours=1), status=ExamStatus.ACTIVE, created_by_id=adm.id)
    s.add(ex); s.commit(); made['ex'] = ex.id
    s.add(ExamProctor(exam_id=ex.id, proctor_id=sv.id, assigned_by_id=adm.id)); s.add(ExamProctor(exam_id=ex.id, proctor_id=other.id, assigned_by_id=adm.id)); s.commit()
    stu = [mk(UserRole.STUDENT, f'e{i}') for i in range(48)]
    kinds = {}
    for i, u in enumerate(stu):
        k = i % 4        # 0 : jamais commencé ; 1 : copie JAMAIS affichée (sans réponse) ; 2 : en cours (vue) ; 3 : rendue
        kinds[u.id] = k
        a = None
        if k == 1: a = ExamAttempt(exam_id=ex.id, student_id=u.id, status=AttemptStatus.IN_PROGRESS, answers='{}', exam_seen_at=None, risk_score=7)
        if k == 2: a = ExamAttempt(exam_id=ex.id, student_id=u.id, status=AttemptStatus.IN_PROGRESS, answers='{"1":"A"}', exam_seen_at=now_n, risk_score=40 + i)
        if k == 3: a = ExamAttempt(exam_id=ex.id, student_id=u.id, status=AttemptStatus.SUBMITTED, answers='{"1":"B"}', exam_seen_at=now_n, risk_score=5)
        if a is not None: s.add(a)
        s.add(ProctorAssignment(exam_id=ex.id, proctor_id=(sv if i < 40 else other).id, student_id=u.id))
    s.commit()
    # une copie « jamais affichée » MAIS avec une vraie réponse : elle compte comme commencée (règle _never_seen)
    odd = stu[1]; a = s.query(ExamAttempt).filter_by(exam_id=ex.id, student_id=odd.id).one(); a.answers = '{"1":"C"}'; s.commit()
    cnt['n'] = 0; r = c.get('/api/surveillant/exams', headers=H(sv)); q = cnt['n']; j = r.get_json() or {}
    mine = {x['student_id']: x for e in j.get('exams', []) for x in e['my_students']}
    check("200 et exactement les 40 étudiants de CE surveillant (pas ceux de l'autre)", r.status_code == 200 and len(mine) == 40 and not any(u.id in mine for u in stu[40:]), (r.status_code, len(mine)))
    ok = True; bad = []
    for u in stu[:40]:
        k = kinds[u.id]; x = mine[u.id]
        want = {0: 'not_started', 1: 'not_started', 2: 'in_progress', 3: 'submitted'}[k]
        if u.id == odd.id: want = 'in_progress'
        if x['status'] != want: ok = False; bad.append((u.id, k, x['status'], want))
    check("statuts identiques à l'ancien calcul (jamais commencé / jamais affiché / en cours / rendu / affichage manqué mais réponse)", ok, bad[:3])
    check("score de risque et nom présents pour les copies commencées", all(mine[u.id]['student_name'].startswith('Aud') for u in stu[:40]) and any(mine[u.id]['risk_score'] >= 40 for u in stu[:40] if kinds[u.id] == 2))
    check(f"requêtes bornées indépendamment du nombre d'étudiants ({q} pour 40 étudiants, avant : ≈ 3 par étudiant)", q <= 25, q)
finally:
    s.rollback()
    try:
        s.query(TokenBlocklist).filter(TokenBlocklist.user_id.in_(made['users'])).delete(synchronize_session=False)
        if made['ex']:
            ids = [a.id for a in s.query(ExamAttempt.id).filter_by(exam_id=made['ex'])]
            s.query(ProctorAssignment).filter_by(exam_id=made['ex']).delete(); s.query(ExamAttempt).filter_by(exam_id=made['ex']).delete()
            s.query(ExamProctor).filter_by(exam_id=made['ex']).delete(); s.query(OnlineExam).filter_by(id=made['ex']).delete()
        if made['sub']: s.query(Subject).filter_by(id=made['sub']).delete()
        s.query(User).filter(User.id.in_(made['users'])).delete(synchronize_session=False); s.commit()
    except Exception as e:
        s.rollback(); print('nettoyage incomplet :', e)
    s.close()
bad = [n for n, ok in RESULTS if not ok]
print(f"\n{len(RESULTS) - len(bad)}/{len(RESULTS)} vérifications réussies" + (f" — ÉCHECS : {bad}" if bad else ''))
sys.exit(1 if bad else 0)
