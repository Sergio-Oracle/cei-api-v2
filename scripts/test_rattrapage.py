#!/usr/bin/env python
"""Rattrapage : le personnel choisit des étudiants qui n'ont pas composé (sans moyens, bloqués avant les questions) et leur ouvre un nouveau créneau.
    cd cei-api-v2 && .venv-base/bin/python scripts/test_rattrapage.py     (crée puis supprime ses propres comptes et examens de test)"""
import sys, uuid
sys.path.insert(0, '.')
from app import app
from models import *
from routes.exams import get_session
from auth_paseto import create_access_token
from sqlalchemy import func
from datetime import datetime, timedelta

s = get_session(); sfx = uuid.uuid4().hex[:6]
# UE la moins peuplée parmi celles qui ont un sujet : le test touche peu de comptes réels
row = (s.query(Subject, func.count(StudentUEEnrollment.id)).join(EC, EC.id == Subject.ec_id)
       .outerjoin(StudentUEEnrollment, StudentUEEnrollment.ue_id == EC.ue_id).filter(EC.ue_id != None)
       .group_by(Subject.id).order_by(func.count(StudentUEEnrollment.id)).first())
subj = row[0]; ue_id = subj.ec.ue_id
now = datetime.utcnow(); c = app.test_client(); made = {'users': [], 'exam': None}
def mk(tag, role=UserRole.STUDENT):
    u = User(email=f'zz_ratt_{tag}_{sfx}@test.local', full_name=f'ZZ Ratt {tag}', role=role, is_active=True, password_hash='x')
    s.add(u); s.commit(); made['users'].append(u.id); return u
def H(u): return {'Authorization': 'Bearer ' + create_access_token(u.id, u.role.value, u.email)}
RES = []
def chk(n, ok, d=''):
    RES.append(bool(ok)); print(('  ✔ ' if ok else '  ✘ ') + n + (f'  — {d!r}' if not ok else ''))
def iso(dt): return dt.isoformat() + 'Z'
try:
    adm = mk('adm', UserRole.ADMIN)
    a = mk('bloque'); b = mk('absent'); cc = mk('copie'); e = mk('autre')
    for u in (a, b, cc, e):
        s.add(StudentUEEnrollment(student_id=u.id, ue_id=ue_id))
    s.commit()
    ex = OnlineExam(subject_id=subj.id, title='Rattrapage test', duration_minutes=60, start_time=now - timedelta(hours=3), end_time=now - timedelta(hours=1),
                    status=ExamStatus.CLOSED, created_by_id=adm.id); s.add(ex); s.commit(); made['exam'] = ex.id; eid = ex.id
    att_a = ExamAttempt(exam_id=eid, student_id=a.id, status=AttemptStatus.AUTO_SUBMITTED, answers='{}', submitted_at=now - timedelta(hours=1))
    att_c = ExamAttempt(exam_id=eid, student_id=cc.id, status=AttemptStatus.SUBMITTED, answers='{"1":"réponse"}', exam_seen_at=now - timedelta(hours=2), submitted_at=now - timedelta(hours=1))
    s.add_all([att_a, att_c]); s.commit(); old_a = att_a.id

    print('== Qui peut passer le rattrapage ==')
    r = c.get(f'/api/online_exams/{eid}/makeup/candidates?limit=500', headers=H(adm)).get_json()
    ids = {x['id'] for x in r['candidates']}
    chk('candidats : l\'étudiant bloqué avant les questions et l\'absent', a.id in ids and b.id in ids, ids)
    chk('candidats : jamais celui qui a une copie', cc.id not in ids)
    r = c.get(f'/api/online_exams/{eid}/makeup/candidates?search=absent', headers=H(adm)).get_json()
    chk('recherche par nom', [x['id'] for x in r['candidates']] == [b.id], r)
    r = c.get(f'/api/online_exams/{eid}/makeup/candidates', headers=H(a))
    chk('un étudiant ne peut pas lister les candidats', r.status_code == 403, r.status_code)

    print('\n== Refus ==')
    start, end = now + timedelta(hours=1), now + timedelta(hours=3)
    body = lambda ids_, st=start, en=end: {'student_ids': ids_, 'start_time': iso(st), 'end_time': iso(en)}
    r = c.post(f'/api/online_exams/{eid}/makeup', json=body([cc.id]), headers=H(adm))
    chk('un étudiant qui a une copie ne peut pas être choisi', r.status_code == 400, (r.status_code, r.get_json()))
    r = c.post(f'/api/online_exams/{eid}/makeup', json=body([a.id], now - timedelta(hours=2), now - timedelta(hours=1)), headers=H(adm))
    chk('créneau dans le passé refusé', r.status_code == 400, r.get_json())
    r = c.post(f'/api/online_exams/{eid}/makeup', json=body([a.id], start, start + timedelta(minutes=20)), headers=H(adm))
    chk('créneau plus court que la durée refusé', r.status_code == 400, r.get_json())
    r = c.post(f'/api/online_exams/{eid}/makeup', json=body([]), headers=H(adm))
    chk('aucun étudiant : refusé', r.status_code == 400, r.get_json())
    r = c.post(f'/api/online_exams/{eid}/makeup', json=body([a.id]), headers=H(a))
    chk('un étudiant ne peut pas organiser un rattrapage', r.status_code == 403, r.status_code)

    print('\n== Organisation ==')
    r = c.post(f'/api/online_exams/{eid}/makeup', json=body([a.id, b.id]), headers=H(adm))
    j = r.get_json() or {}
    chk('rattrapage créé', r.status_code == 201 and j.get('students') == 2, (r.status_code, j))
    s.expire_all(); ex = s.get(OnlineExam, eid)
    chk('l\'examen est rouvert', ex.status == ExamStatus.ACTIVE and j.get('reopened') is True)
    chk('la fenêtre globale couvre le rattrapage', abs((ex.end_time - end).total_seconds()) < 2)
    chk('la tentative vide est retirée', s.query(ExamAttempt).filter_by(id=old_a).count() == 0)
    chk('la copie réelle de l\'autre étudiant est intacte', s.query(ExamAttempt).filter_by(id=att_c.id).count() == 1)
    waves = {w.name: w for w in s.query(ExamWave).filter_by(exam_id=eid).all()}
    chk('deux vagues : session normale + rattrapage', set(waves) == {'Session normale', 'Rattrapage'}, list(waves))
    members = lambda w: {sid for (sid,) in s.query(ExamWaveStudent.student_id).filter_by(wave_id=w.id)}
    chk('seuls les deux choisis sont dans le rattrapage', members(waves['Rattrapage']) == {a.id, b.id}, members(waves['Rattrapage']))
    chk('les autres restent dans la session normale', {cc.id, e.id} <= members(waves['Session normale']) and not ({a.id, b.id} & members(waves['Session normale'])))

    print('\n== Accès des étudiants ==')
    r = c.post(f'/api/online_exams/{eid}/start', json={}, headers=H(a))
    chk('choisi : « pas encore commencé » avant le créneau', r.status_code == 400 and 'starts_at' in (r.get_json() or {}), (r.status_code, r.get_json()))
    r = c.post(f'/api/online_exams/{eid}/start', json={}, headers=H(e))
    chk('non choisi, sans copie : examen terminé', r.status_code == 400 and 'terminé' in (r.get_json() or {}).get('error', ''), (r.status_code, r.get_json()))
    r = c.post(f'/api/online_exams/{eid}/start', json={}, headers=H(cc))
    chk('copie déjà rendue : examen toujours terminé pour lui', r.status_code == 400, (r.status_code, r.get_json()))
    wv = waves['Rattrapage']; wv.start_time = now - timedelta(minutes=1); s.commit()
    for u, nom in ((a, 'bloqué'), (b, 'absent')):
        r = c.post(f'/api/online_exams/{eid}/start', json={}, headers=H(u))
        chk(f'choisi ({nom}) : peut composer pendant le créneau', r.status_code in (200, 201), (r.status_code, r.get_json()))
    r = c.post(f'/api/online_exams/{eid}/start', json={}, headers=H(e))
    chk('non choisi : toujours refusé pendant le rattrapage', r.status_code == 400, (r.status_code, r.get_json()))
    chk('l\'étudiant choisi a une nouvelle tentative', s.query(ExamAttempt).filter_by(exam_id=eid, student_id=a.id).count() == 1)
finally:
    s.close(); s2 = get_session()
    try:
        if made['exam']:
            for att in s2.query(ExamAttempt).filter_by(exam_id=made['exam']).all():
                s2.query(ExamAccessCode).filter_by(attempt_id=att.id).delete(synchronize_session=False)
                s2.query(ExamActivityLog).filter_by(attempt_id=att.id).delete(synchronize_session=False)
                s2.query(ProctorAssignment).filter_by(attempt_id=att.id).delete(synchronize_session=False)
                s2.delete(att)
            s2.flush()
            s2.query(ExamWaveStudent).filter_by(exam_id=made['exam']).delete(synchronize_session=False)
            s2.query(ExamWave).filter_by(exam_id=made['exam']).delete(synchronize_session=False)
            s2.query(ProctorAssignment).filter_by(exam_id=made['exam']).delete(synchronize_session=False)
            x = s2.get(OnlineExam, made['exam'])
            if x: s2.delete(x)
            s2.flush()
        s2.query(StudentUEEnrollment).filter(StudentUEEnrollment.student_id.in_(made['users'])).delete(synchronize_session=False)
        for uid in made['users']:
            u = s2.get(User, uid)
            if u: s2.delete(u)
        s2.commit(); print('comptes et examens de test supprimés')
    except Exception as ex_:
        s2.rollback(); print('nettoyage incomplet :', ex_)
    s2.close()
ok = sum(RES); print(f'\n{ok}/{len(RES)} vérifications réussies'); sys.exit(0 if ok == len(RES) else 1)
