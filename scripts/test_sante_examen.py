#!/usr/bin/env python
"""Santé de l'examen en cours (18/11/2026) : indicateurs, verdict, accès, alerte automatique à l'administrateur.

    cd cei-api-v2 && .venv-base/bin/python scripts/test_sante_examen.py     (données temporaires, supprimées)

Les problèmes du 17/11 (entrée bloquée, hors ligne, liaison vidéo coupée, flot d'alertes, appels en rafale) doivent être vus PAR LA PLATEFORME, pas découverts dans un rapport.
"""
import os, sys, uuid
from datetime import datetime, timedelta
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as A
from services import exam_vitals as EV
from cache import cache_delete
from models import (get_session, User, UserRole, Subject, OnlineExam, ExamStatus, ExamAttempt, AttemptStatus, ExamActivityLog, EC)
from auth_paseto import create_access_token
from werkzeug.security import generate_password_hash

RESULTS = []
def check(nom, ok, detail=''):
    RESULTS.append((nom, bool(ok))); print(('  ✔ ' if ok else '  ✘ ') + nom + (f'  — {detail}' if detail and not ok else ''))
s = get_session(); tag = uuid.uuid4().hex[:6]; made = {'users': [], 'ex': [], 'sub': None}
c = A.app.test_client()
def mk(role, n):
    u = User(email=f'san.{tag}.{n}@test.invalid', full_name=f'San {n}', role=role, is_active=True, email_verified=True, has_email=True, password_hash=generate_password_hash('x'))
    s.add(u); s.commit(); made['users'].append(u.id); return u
def H(u): return {'Authorization': 'Bearer ' + create_access_token(u.id, u.role.value, u.email)}
try:
    adm = mk(UserRole.ADMIN, 'adm'); sv = mk(UserRole.SURVEILLANT, 'sv'); stu = mk(UserRole.STUDENT, 'e'); other = mk(UserRole.PROFESSOR, 'prof')
    ec = s.query(EC).filter(EC.ue_id.isnot(None)).first()
    sub = Subject(title='san', content='1. Q ? [QCM]\nA. a\nB. b\n', rubric='Question 1 (1 pt) : A', creator_id=adm.id, ec_id=ec.id); s.add(sub); s.commit(); made['sub'] = sub.id
    now = datetime.utcnow()
    def exam(title):
        e = OnlineExam(subject_id=sub.id, title=title, duration_minutes=60, start_time=now - timedelta(minutes=30), end_time=now + timedelta(hours=1), status=ExamStatus.ACTIVE, created_by_id=adm.id)
        s.add(e); s.commit(); made['ex'].append(e.id); return e
    def att(e, n, **kw):
        out = []
        for i in range(n):
            u = mk(UserRole.STUDENT, f'{e.id}_{len(made["users"])}')
            a = ExamAttempt(exam_id=e.id, student_id=u.id, status=kw.get('status', AttemptStatus.IN_PROGRESS), started_at=kw.get('started', now - timedelta(minutes=20)),
                            exam_seen_at=kw.get('seen', now - timedelta(minutes=19)), last_seen_at=kw.get('last', now - timedelta(seconds=20)), risk_score=kw.get('risk', 10), answers='{}')
            s.add(a); s.commit(); out.append(a)
        return out
    def ev(a, t, ago_min=1):
        s.add(ExamActivityLog(attempt_id=a.id, event_type=t, event_data='x', timestamp=now - timedelta(minutes=ago_min))); s.commit()

    print('\n== Examen sain ==')
    e0 = exam('sain'); att(e0, 30)
    h = EV.compute(s, e0.id, now)
    check("30 étudiants en bonne santé : verdict « ok », aucun problème", h['niveau'] == 'ok' and not h['problems'], h)
    check("les indicateurs sont renseignés (30 en cours, entrée ≈ 60 s)", h['kpi']['in_progress'] == 30 and h['kpi']['entry_p50_s'] == 60, h['kpi'])

    print('\n== Entrée bloquée (le problème de l\'attente avant l\'affichage de la copie) ==')
    e1 = exam('entrée'); att(e1, 20); att(e1, 6, seen=None, started=now - timedelta(minutes=8), last=None)
    h = EV.compute(s, e1.id, now)
    check("6 étudiants sur 26 bloqués depuis 8 min : CRITIQUE « entrée bloquée »", h['niveau'] == 'critique' and any(p['code'] == 'entree_bloquee' for p in h['problems']), h['problems'])
    check("le message est en français simple et donne un conseil", all(p['message'] and p['conseil'] for p in h['problems']))
    e1b = exam('entrée récente'); att(e1b, 20); att(e1b, 6, seen=None, started=now - timedelta(minutes=1), last=None)
    check("des étudiants qui viennent juste de démarrer (1 min) ne sont PAS comptés bloqués", not any(p['code'] == 'entree_bloquee' for p in EV.compute(s, e1b.id, now)['problems']))
    e1c = exam('entrée peu'); att(e1c, 40); att(e1c, 2, seen=None, started=now - timedelta(minutes=9), last=None)
    check("2 bloqués sur 42 (< seuils) : pas de fausse alerte critique", EV.compute(s, e1c.id, now)['niveau'] != 'critique')

    print('\n== Hors ligne, liaison vidéo, alertes, appels, application ==')
    e2 = exam('hors ligne'); a2 = att(e2, 20) + att(e2, 6, last=now - timedelta(minutes=5))
    h = EV.compute(s, e2.id, now); check("6 étudiants sur 26 sans signe de vie depuis 5 min : CRITIQUE « hors ligne »", any(p['code'] == 'hors_ligne' and p['niveau'] == 'critique' for p in h['problems']), h['problems'])
    e3 = exam('liaison'); a3 = att(e3, 20)
    for a in a3[:6]: ev(a, 'live_monitoring_unavailable')
    h = EV.compute(s, e3.id, now); check("liaison vidéo coupée pour 30 % : « attention » (pas critique : elle se rétablit seule)", h['niveau'] == 'attention' and any(p['code'] == 'liaison_video' for p in h['problems']), h['problems'])
    e4 = exam('risque'); att(e4, 12, risk=90); att(e4, 18, risk=10)
    h = EV.compute(s, e4.id, now); check("40 % à risque élevé (le cas du 17/11) : « trop d'alertes »", any(p['code'] == 'trop_d_alertes' for p in h['problems']), h['problems'])
    e5 = exam('appels'); a5 = att(e5, 20)
    for a in a5[:6]: ev(a, 'student_call_request', 5)
    check("6 demandes d'appel en 30 min : « appels en attente »", any(p['code'] == 'appels_attente' for p in EV.compute(s, e5.id, now)['problems']))
    e6 = exam('application'); a6 = att(e6, 20)
    for a in a6[:3]: ev(a, 'client_unclean_exit', 10)
    check("application fermée de façon inattendue chez 15 % : « application instable »", any(p['code'] == 'application_instable' for p in EV.compute(s, e6.id, now)['problems']))
    e7 = exam('petit'); att(e7, 5, risk=95, last=now - timedelta(minutes=9))
    check("moins de 10 étudiants : aucun pourcentage commenté (pas de verdict sur un si petit effectif)", EV.compute(s, e7.id, now)['niveau'] == 'ok')

    print('\n== Accès ==')
    r = c.get(f'/api/online_exams/{e1.id}/vitals', headers=H(adm)); check("l'administrateur lit la santé de l'examen", r.status_code == 200 and r.get_json()['niveau'] == 'critique', r.status_code)
    r = c.get(f'/api/online_exams/{e1.id}/vitals', headers=H(sv)); check("un surveillant n'y a pas accès (403)", r.status_code == 403)
    r = c.get(f'/api/online_exams/{e1.id}/vitals', headers=H(stu)); check("un étudiant non plus (403)", r.status_code == 403)
    r = c.get(f'/api/online_exams/{e1.id}/vitals', headers=H(other)); check("un enseignant étranger à l'examen est refusé", r.status_code in (403, 404), r.status_code)
    r = c.get('/api/online_exams/99999999/vitals', headers=H(adm)); check("examen inconnu : 404 propre", r.status_code == 404)

    print('\n== Alerte automatique à l\'administrateur ==')
    import notif_bus
    sent = []
    orig = notif_bus.notify_admins
    notif_bus.notify_admins = lambda *a, **k: sent.append(a[1])
    try:
        for e in (e1, e2):
            for code in ('entree_bloquee', 'hors_ligne'): cache_delete(f'cei:vitals:alert:{e.id}:{code}')
        r1 = [x for x in EV.watch_once(now) if x[0] in (e1.id, e2.id)]
        check("un problème critique envoie UNE alerte à l'administrateur par examen concerné", sorted(set(x[0] for x in r1)) == sorted([e1.id, e2.id]) and len(sent) >= 2, (r1, sent))
        n = len(sent)
        r2 = [x for x in EV.watch_once(now) if x[0] in (e1.id, e2.id)]
        check("la minute suivante : pas de doublon (une alerte par tranche de 10 minutes)", r2 == [] and len(sent) == n, r2)
        check("les alertes « attention » ne dérangent pas l'administrateur (seulement le critique)", not any(x[0] in (e3.id, e4.id, e5.id, e6.id) for x in r1))
    finally:
        notif_bus.notify_admins = orig
finally:
    s.rollback()
    try:
        ids = made['ex']
        if ids:
            att_ids = [a.id for a in s.query(ExamAttempt).filter(ExamAttempt.exam_id.in_(ids))]
            if att_ids: s.query(ExamActivityLog).filter(ExamActivityLog.attempt_id.in_(att_ids)).delete(synchronize_session=False)
            s.query(ExamAttempt).filter(ExamAttempt.exam_id.in_(ids)).delete(synchronize_session=False)
            s.query(OnlineExam).filter(OnlineExam.id.in_(ids)).delete(synchronize_session=False)
            for i in ids:
                for code in ('entree_bloquee', 'hors_ligne', 'liaison_video', 'trop_d_alertes', 'appels_attente', 'application_instable', 'entree_lente'): cache_delete(f'cei:vitals:alert:{i}:{code}')
        if made['sub']: s.query(Subject).filter_by(id=made['sub']).delete()
        if made['users']: s.query(User).filter(User.id.in_(made['users'])).delete(synchronize_session=False)
        s.commit()
    except Exception as ex_:
        s.rollback(); print('nettoyage incomplet :', ex_)
    s.close()
bad = [n for n, ok in RESULTS if not ok]
print(f"\n{len(RESULTS) - len(bad)}/{len(RESULTS)} vérifications réussies" + (f" — ÉCHECS : {bad}" if bad else ''))
sys.exit(1 if bad else 0)
