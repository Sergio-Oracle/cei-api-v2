#!/usr/bin/env python
"""Reprise d'un examen dont la copie n'a JAMAIS été affichée : aucun code, aucun appel, aucune notification (18/11/2026).

    cd cei-api-v2 && .venv-base/bin/python scripts/test_reprise_sans_code.py     (données temporaires, supprimées)

Constat (examen 192) : 1 étudiant sur 10 attendait plus de 5 min (jusqu'à 78) avant l'affichage de sa copie ; la plupart avaient rechargé la page, reçu « Vous avez quitté l'examen,
code de reprise », appelé leur surveillant (file d'attente qui grossit avec le nombre d'étudiants) et attendu sa réponse. La règle du code ne vaut que si la copie a été vue.
"""
import os, sys, uuid, json
from datetime import datetime, timedelta
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as A
import routes.exams as RX
from models import (get_session, User, UserRole, Subject, OnlineExam, ExamStatus, EC, StudentUEEnrollment, ExamAttempt, AttemptStatus, ExamActivityLog, ExamAccessCode)
from auth_paseto import create_access_token
from werkzeug.security import generate_password_hash

RESULTS = []
def check(nom, ok, detail=''):
    RESULTS.append((nom, bool(ok))); print(('  ✔ ' if ok else '  ✘ ') + nom + (f'  — {detail}' if detail and not ok else ''))
s = get_session(); tag = uuid.uuid4().hex[:6]; made = {'users': [], 'ex': [], 'sub': None}
c = A.app.test_client()
def mk(role, n):
    u = User(email=f'rep.{tag}.{n}@test.invalid', full_name=f'Rep Test {n}', role=role, is_active=True, email_verified=True, has_email=True, password_hash=generate_password_hash('x'))
    s.add(u); s.commit(); made['users'].append(u.id); return u
def H(u): return {'Authorization': 'Bearer ' + create_access_token(u.id, u.role.value, u.email)}
notifs = []
orig = RX._notify_resume
RX._notify_resume = lambda *a, **k: (notifs.append(1), orig(*a, **k))[1]
try:
    adm = mk(UserRole.ADMIN, 'adm'); st = [mk(UserRole.STUDENT, f'e{i}') for i in range(3)]
    ec = s.query(EC).filter(EC.ue_id.isnot(None)).first()
    sub = Subject(title='rep', content='1. Q ? [QCM]\nA. a\nB. b\n', rubric='Question 1 (1 pt) : A', creator_id=adm.id, ec_id=ec.id); s.add(sub); s.commit(); made['sub'] = sub.id
    now = datetime.utcnow()
    ex = OnlineExam(subject_id=sub.id, title='rep', duration_minutes=60, start_time=now - timedelta(minutes=5), end_time=now + timedelta(hours=2), status=ExamStatus.ACTIVE, created_by_id=adm.id)
    s.add(ex); s.commit(); made['ex'].append(ex.id)
    for u in st: s.add(StudentUEEnrollment(student_id=u.id, ue_id=ec.ue_id))
    s.commit()
    URL = f'/api/online_exams/{ex.id}/start'

    print('\n== Copie jamais affichée : reprise immédiate ==')
    r = c.post(URL, headers=H(st[0]), json={}); j = r.get_json() or {}
    check("premier démarrage : copie créée", r.status_code in (200, 201) and j.get('attempt'), (r.status_code, j))
    aid = j['attempt']['id']
    for k in range(3):                                                  # l'étudiant recharge la page 3 fois (autorisation accordée, onglet refermé…)
        r = c.post(URL, headers=H(st[0]), json={}); j = r.get_json() or {}
        check(f"rechargement n°{k+1} : accepté tout de suite, SANS code de reprise", r.status_code == 200 and j.get('success') and j.get('continuing') and not j.get('code_required'), (r.status_code, j))
    check("… même tentative, aucune nouvelle copie", s.query(ExamAttempt).filter_by(exam_id=ex.id, student_id=st[0].id).count() == 1)
    check("… aucun code généré, aucune notification envoyée au surveillant (rien à surveiller : la copie n'a pas été vue)", s.query(ExamAccessCode).filter_by(attempt_id=aid).count() == 0 and not notifs, (notifs,))
    r = c.get('/api/online_exams', headers=H(st[0])); mine = [e for e in (r.get_json() or []) if e['id'] == ex.id][0]['my_attempt']
    check("le tableau de bord sait que la copie n'a jamais été affichée (never_seen) : plus de « Vous avez quitté l'examen »", mine.get('never_seen') is True, mine)

    print('\n== Copie déjà vue : le code de reprise reste exigé ==')
    r = c.post(URL, headers=H(st[1]), json={}); a2 = (r.get_json() or {})['attempt']['id']
    a = s.get(ExamAttempt, a2); a.exam_seen_at = now; s.commit()
    r = c.post(URL, headers=H(st[1]), json={}); j = r.get_json() or {}
    check("copie affichée puis page quittée : code de reprise exigé (403, code fourni)", r.status_code == 403 and j.get('code_required') and j.get('code'), (r.status_code, j))
    r = c.post(URL, headers=H(st[1]), json={'access_code': j.get('code')}); check("… avec son code : reprise acceptée", r.status_code == 200 and (r.get_json() or {}).get('continuing'), (r.status_code, r.get_json()))
    check("… et le surveillant est notifié de la reprise (comportement conservé)", len(notifs) == 1, notifs)
    r = c.get('/api/online_exams', headers=H(st[1])); mine = [e for e in (r.get_json() or []) if e['id'] == ex.id][0]['my_attempt']
    check("tableau de bord : copie vue → never_seen faux", mine.get('never_seen') is False, mine)

    print('\n== Copie jamais « vue » mais avec des réponses (ancienne tentative) : code exigé ==')
    r = c.post(URL, headers=H(st[2]), json={}); a3 = (r.get_json() or {})['attempt']['id']
    a = s.get(ExamAttempt, a3); a.answers = json.dumps({'reponse': 'du texte réellement saisi'}); s.commit()
    r = c.post(URL, headers=H(st[2]), json={}); j = r.get_json() or {}
    check("des réponses existent : on ne considère JAMAIS la copie comme non vue → code exigé", r.status_code == 403 and j.get('code_required'), (r.status_code, j))

    print('\n== Garde-fous inchangés ==')
    a = s.get(ExamAttempt, aid); a.status = AttemptStatus.SUBMITTED; s.commit()
    r = c.post(URL, headers=H(st[0]), json={}); check("copie déjà remise : toujours refusée", r.status_code == 400, r.status_code)
    a.status = AttemptStatus.BANNED; s.commit()
    r = c.post(URL, headers=H(st[0]), json={}); check("étudiant exclu : toujours refusé", r.status_code == 403 and (r.get_json() or {}).get('banned'), (r.status_code, r.get_json()))
finally:
    RX._notify_resume = orig
    s.rollback()
    try:
        ids = made['ex']
        if ids:
            att = [a.id for a in s.query(ExamAttempt).filter(ExamAttempt.exam_id.in_(ids))]
            if att:
                s.query(ExamAccessCode).filter(ExamAccessCode.attempt_id.in_(att)).delete(synchronize_session=False)
                s.query(ExamActivityLog).filter(ExamActivityLog.attempt_id.in_(att)).delete(synchronize_session=False)
            s.query(ExamAttempt).filter(ExamAttempt.exam_id.in_(ids)).delete(synchronize_session=False)
            s.query(OnlineExam).filter(OnlineExam.id.in_(ids)).delete(synchronize_session=False)
        if made['users']: s.query(StudentUEEnrollment).filter(StudentUEEnrollment.student_id.in_(made['users'])).delete(synchronize_session=False)
        if made['sub']: s.query(Subject).filter_by(id=made['sub']).delete()
        if made['users']: s.query(User).filter(User.id.in_(made['users'])).delete(synchronize_session=False)
        s.commit()
    except Exception as ex_:
        s.rollback(); print('nettoyage incomplet :', ex_)
    s.close()
bad = [n for n, ok in RESULTS if not ok]
print(f"\n{len(RESULTS) - len(bad)}/{len(RESULTS)} vérifications réussies" + (f" — ÉCHECS : {bad}" if bad else ''))
sys.exit(1 if bad else 0)
