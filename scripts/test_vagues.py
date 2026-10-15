#!/usr/bin/env python
"""Test des examens par vagues, par les VRAIES routes (client de test Flask).

    cd cei-api-v2 && .venv-base/bin/python scripts/test_vagues.py

Crée des comptes, un sujet et deux examens de test rattachés à un EC sans groupe de
surveillants (aucune notification vers de vrais utilisateurs), vérifie chaque règle,
puis supprime tout. Affiche « réussi / échoué » pour chaque point.
"""
import json, os, sys, uuid
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as A
import cache
from models import (get_session, User, UserRole, OnlineExam, ExamAttempt, ExamStatus, AttemptStatus, EC,
                    ProctorGroupEC, StudentUEEnrollment, ExamWave, ExamWaveStudent, ExamAccessCode,
                    ProctorAssignment, Subject)
from auth_paseto import create_access_token
from helpers import utcnow
from werkzeug.security import generate_password_hash
import services.exam_health as _health
_health.notify_anomalies = lambda: None

RESULTS = []
def check(nom, ok, detail=''):
    RESULTS.append((nom, bool(ok)))
    print(('  ✔ ' if ok else '  ✘ ') + nom + (f'  — {detail}' if detail and not ok else ''))

c = A.app.test_client()
s = get_session()
tag = uuid.uuid4().hex[:6]
made = {'users': [], 'subject': None, 'exams': []}
Z = lambda dt: dt.strftime('%Y-%m-%dT%H:%M:%S.000Z')
def H(u, role): return {'Authorization': 'Bearer ' + create_access_token(u.id, role, u.email)}

try:
    print('\n== Préparation ==')
    admin = s.query(User).filter_by(role=UserRole.ADMIN, is_active=True).order_by(User.id).first()
    ha = H(admin, 'admin')
    ec = (s.query(EC).filter(EC.ue_id.isnot(None), ~EC.id.in_(s.query(ProctorGroupEC.ec_id)),
                             ~EC.ue_id.in_(s.query(StudentUEEnrollment.ue_id)))      # UE sans aucun inscrit : seuls nos comptes de test
          .order_by(EC.id).first())
    assert ec, "aucun EC sans groupe de surveillants et sans inscrit pour héberger l'examen de test"
    studs = []
    for i in range(9):
        u = User(email=f'vagues.{tag}.{i}@test.invalid', full_name=f'Vagues Test {chr(65 + i)}', role=UserRole.STUDENT,
                 is_active=True, email_verified=True, has_email=True, password_hash=generate_password_hash('x'))
        s.add(u); s.flush(); studs.append(u); made['users'].append(u.id)
        s.add(StudentUEEnrollment(student_id=u.id, ue_id=ec.ue_id))
    s.commit()
    hs = {u.id: H(u, 'student') for u in studs}
    for u in studs:
        cache.cache_set(f'cei:biometric:verify:{u.id}', '1', ttl=600)

    content = '\n\n'.join(f"Question {n} — Question de test {n}\nA) Choix A\nB) Choix B" for n in range(1, 4))
    rubric = '=== BARÈME DE NOTATION ===\n' + '\n\n'.join(f"Question {n} (4 pts) :\n  Bonne réponse : B)" for n in range(1, 4)) + '\n\nTOTAL : 12 pts'
    r = c.post('/api/subjects', json={'title': f'Vagues {tag}', 'content': content, 'rubric': rubric, 'ec_id': ec.id}, headers=ha)
    check('création du sujet', r.status_code == 201, r.get_data(as_text=True)[:150])
    made['subject'] = r.get_json()['subject']['id']

    now = utcnow().replace(tzinfo=None)
    r = c.post('/api/online_exams', headers=ha, json={
        'subject_id': made['subject'], 'title': f'Vagues {tag}',
        'start_time': Z(now - timedelta(minutes=5)), 'end_time': Z(now + timedelta(minutes=60))})
    check("création de l'examen", r.status_code in (200, 201), r.get_data(as_text=True)[:200])
    eid = (r.get_json().get('exam') or r.get_json()).get('id'); made['exams'].append(eid)
    ex = s.get(OnlineExam, eid); ex.status = ExamStatus.ACTIVE; s.commit()
    duree = ex.duration_minutes

    print('\n== Création des vagues ==')
    r = c.post(f'/api/online_exams/{eid}/waves/plan', headers=ha, json={'count': 1, 'first_start': Z(now)})
    check('une seule vague refusée', r.status_code == 400)
    r = c.post(f'/api/online_exams/{eid}/waves/plan', headers=ha, json={'count': 3, 'first_start': Z(now - timedelta(minutes=5)), 'gap_minutes': 15, 'window_minutes': duree - 10})
    check('fenêtre plus courte que la durée refusée', r.status_code == 400, r.get_data(as_text=True)[:140])
    r = c.post(f'/api/online_exams/{eid}/waves/plan', headers=ha, json={'count': 3, 'first_start': Z(now - timedelta(minutes=5)), 'gap_minutes': 15})
    check('plan de 3 vagues créé', r.status_code == 201, r.get_data(as_text=True)[:200])
    j = r.get_json() or {}
    sizes = [w['students'] for w in j.get('waves', [])]
    mine = {u.id for u in studs}
    s.expire_all()
    eligible = {sid for (sid,) in s.query(ExamWaveStudent.student_id).filter_by(exam_id=eid)}
    check('tous les inscrits ont une vague, une seule', len(eligible) == s.query(ExamWaveStudent).filter_by(exam_id=eid).count() and mine <= eligible)
    check('répartition équilibrée (écart ≤ 1)', sizes and max(sizes) - min(sizes) <= 1, sizes)
    waves = s.query(ExamWave).filter_by(exam_id=eid).order_by(ExamWave.position).all()
    ex = s.get(OnlineExam, eid)
    check("l'enveloppe de l'examen couvre toutes les vagues", ex.start_time == waves[0].start_time and ex.end_time == waves[-1].end_time)
    check('vagues successives sans chevauchement', all(waves[i].end_time < waves[i + 1].start_time for i in range(2)))

    wave_of = {sid: w.position for w in waves for (sid,) in s.query(ExamWaveStudent.student_id).filter_by(wave_id=w.id)}
    w1 = [u for u in studs if wave_of[u.id] == 1]; w2 = [u for u in studs if wave_of[u.id] == 2]
    assert w1 and w2, 'il faut au moins un étudiant de test dans les vagues 1 et 2'

    print('\n== Ce que voit chaque étudiant ==')
    la = c.get('/api/online_exams', headers=hs[w1[0].id]).get_json()
    lb = c.get('/api/online_exams', headers=hs[w2[0].id]).get_json()
    ea = next(e for e in la if e['id'] == eid); eb = next(e for e in lb if e['id'] == eid)
    check('chaque étudiant voit le créneau de SA vague', ea['start_time'] != eb['start_time'] and ea['wave']['position'] == 1 and eb['wave']['position'] == 2,
          (ea['start_time'], eb['start_time']))
    check('la liste du professeur garde la fenêtre complète', next(e for e in c.get('/api/online_exams', headers=ha).get_json() if e['id'] == eid).get('wave') is None)

    print('\n== Accès ==')
    r = c.post(f'/api/online_exams/{eid}/start', json={}, headers=hs[w2[0].id])
    check("vague 2 : refus avant l'heure de sa vague", r.status_code == 400 and (r.get_json() or {}).get('starts_at'), r.get_data(as_text=True)[:160])
    r = c.get(f'/api/online_exams/{eid}/details', headers=hs[w2[0].id])
    check("vague 2 : détails refusés avant l'heure de sa vague", r.status_code == 403)
    r = c.get(f'/api/online_exams/{eid}/details', headers=hs[w1[0].id])
    check('vague 1 : détails accessibles', r.status_code == 200 and (r.get_json() or {}).get('wave', {}).get('position') == 1, r.get_data(as_text=True)[:140])
    r = c.post(f'/api/online_exams/{eid}/start', json={}, headers=hs[w1[0].id])
    check('vague 1 : démarrage accepté', r.status_code in (200, 201), r.get_data(as_text=True)[:160])
    att1 = (r.get_json().get('attempt') or {}).get('id')
    r = c.post(f'/api/online_exams/{eid}/start', json={}, headers=hs[w1[1].id] if len(w1) > 1 else hs[w1[0].id])
    check('second étudiant de la vague 1 : démarrage accepté', r.status_code in (200, 201, 403), r.get_data(as_text=True)[:100])

    extra = User(email=f'vagues.{tag}.x@test.invalid', full_name='Vagues Test Sans vague', role=UserRole.STUDENT,
                 is_active=True, email_verified=True, has_email=True, password_hash=generate_password_hash('x'))
    s.add(extra); s.flush(); made['users'].append(extra.id)
    s.add(StudentUEEnrollment(student_id=extra.id, ue_id=ec.ue_id)); s.commit()
    cache.cache_set(f'cei:biometric:verify:{extra.id}', '1', ttl=600)
    he = H(extra, 'student')
    r = c.post(f'/api/online_exams/{eid}/start', json={}, headers=he)
    check("étudiant inscrit après coup, sans vague : refus clair", r.status_code == 403 and (r.get_json() or {}).get('wave_missing'), r.get_data(as_text=True)[:160])
    le = next(e for e in c.get('/api/online_exams', headers=he).get_json() if e['id'] == eid)
    check("… et la liste le signale (wave_missing)", le.get('wave_missing') is True)
    sm = c.get(f'/api/online_exams/{eid}/waves', headers=ha).get_json()
    check("le tableau des vagues signale l'étudiant sans vague", sm['unassigned'] >= 1, sm['unassigned'])
    w3 = waves[2]
    r = c.post(f'/api/online_exams/{eid}/waves/{w3.id}/students', headers=ha, json={'student_ids': [extra.id]})
    check("rattachement manuel à une vague", r.status_code == 200 and r.get_json().get('moved') == 1, r.get_data(as_text=True)[:140])
    r = c.post(f'/api/online_exams/{eid}/waves/{w3.id}/students', headers=ha, json={'student_ids': [w1[0].id]})
    check("un étudiant qui a commencé ne peut plus changer de vague", r.status_code == 400, r.get_data(as_text=True)[:140])

    print('\n== Règles de cohérence ==')
    r = c.post(f'/api/online_exams/{eid}/waves/plan', headers=ha, json={'count': 2, 'first_start': Z(now)})
    check('vagues non redéfinissables après un démarrage', r.status_code == 400)
    r = c.delete(f'/api/online_exams/{eid}/waves', headers=ha)
    check('vagues non supprimables après un démarrage', r.status_code == 400)
    r = c.post(f'/api/online_exams/{eid}/extend', headers=ha, json={'extra_minutes': 10})
    check("prolongation globale refusée (examen par vagues)", r.status_code == 400)
    r = c.put(f'/api/admin/online_exams/{eid}', headers=ha, json={'start_time': Z(now), 'duration_minutes': 30})
    check("changement de fenêtre refusé via l'édition classique", r.status_code in (400, 403), r.status_code)
    s.expire_all(); ex = s.get(OnlineExam, eid)
    check("l'enveloppe n'a pas bougé", ex.start_time == waves[0].start_time and ex.end_time == waves[-1].end_time)

    print('\n== Échéance par vague ==')
    s.expire_all()
    s.get(ExamWave, waves[0].id).end_time = now - timedelta(minutes=2)   # la vague 1 est terminée, la 3 ne l'est pas
    s.commit()
    c.get('/api/online_exams', headers=ha)       # chaque chargement de liste applique les échéances
    c.get('/api/online_exams', headers=hs[w2[0].id])
    s.expire_all()
    a = s.get(ExamAttempt, att1)
    check("tentative de la vague terminée soumise automatiquement", a.status == AttemptStatus.AUTO_SUBMITTED, a.status)
    check("l'examen reste ouvert pour les vagues suivantes", s.get(OnlineExam, eid).status == ExamStatus.ACTIVE)

    s.get(ExamWave, waves[0].id).end_time = now + timedelta(minutes=30)
    s.commit()
    r = c.post(f'/api/online_exams/{eid}/start', json={}, headers=hs[w1[-1].id])
    check("un étudiant de la vague 1 démarre tant que sa vague est ouverte", r.status_code in (200, 201), r.get_data(as_text=True)[:140])
    att_x = (r.get_json().get('attempt') or {}).get('id')
    s.expire_all()
    s.get(ExamAttempt, att_x).extra_minutes = 40      # temps supplémentaire individuel
    s.get(ExamWave, waves[0].id).end_time = now - timedelta(minutes=2)
    s.commit()
    c.get('/api/online_exams', headers=ha)
    s.expire_all()
    check("temps supplémentaire individuel respecté après la fin de la vague", s.get(ExamAttempt, att_x).status == AttemptStatus.IN_PROGRESS)
    s.get(ExamAttempt, att_x).extra_minutes = 0; s.commit()
    c.get('/api/online_exams', headers=ha)
    s.expire_all()
    check("sans temps supplémentaire : soumise à l'échéance de la vague", s.get(ExamAttempt, att_x).status == AttemptStatus.AUTO_SUBMITTED)

    print('\n== Examen sans vague : comportement inchangé ==')
    r = c.post('/api/online_exams', headers=ha, json={
        'subject_id': made['subject'], 'title': f'Sans vague {tag}',
        'start_time': Z(now - timedelta(minutes=5)), 'end_time': Z(now + timedelta(minutes=60))})
    eid2 = (r.get_json().get('exam') or r.get_json()).get('id'); made['exams'].append(eid2)
    s.get(OnlineExam, eid2).status = ExamStatus.ACTIVE; s.commit()
    r = c.post(f'/api/online_exams/{eid2}/start', json={}, headers=hs[w2[0].id])
    check('sans vague : démarrage normal', r.status_code in (200, 201), r.get_data(as_text=True)[:140])
    ee = next(e for e in c.get('/api/online_exams', headers=hs[w2[0].id]).get_json() if e['id'] == eid2)
    check('sans vague : pas de champ wave', 'wave' not in ee or ee['wave'] is None)
    r = c.delete(f'/api/online_exams/{eid2}/waves', headers=ha)
    check('suppression de vagues inexistantes sans effet', r.status_code == 200)
    # vagues puis suppression sur un examen non commencé
    r = c.post('/api/online_exams', headers=ha, json={
        'subject_id': made['subject'], 'title': f'Suppr {tag}',
        'start_time': Z(now + timedelta(minutes=300)), 'end_time': Z(now + timedelta(minutes=360))})
    eid3 = (r.get_json().get('exam') or r.get_json()).get('id'); made['exams'].append(eid3)
    r = c.post(f'/api/online_exams/{eid3}/waves/plan', headers=ha, json={'count': 2, 'first_start': Z(now + timedelta(minutes=300)), 'order': 'aleatoire'})
    check('plan aléatoire créé', r.status_code == 201, r.get_data(as_text=True)[:140])
    r = c.delete(f'/api/online_exams/{eid3}/waves', headers=ha)
    check('suppression des vagues sur un examen non commencé', r.status_code == 200 and not r.get_json()['waves'])
finally:
    s.rollback()
    try:
        for eid_ in made['exams']:
            ids = [a.id for a in s.query(ExamAttempt.id).filter_by(exam_id=eid_)]
            s.query(ProctorAssignment).filter_by(exam_id=eid_).delete(synchronize_session=False)
            if ids:
                from models import ExamActivityLog, CameraLog
                s.query(CameraLog).filter(CameraLog.attempt_id.in_(ids)).delete(synchronize_session=False)
                s.query(ExamActivityLog).filter(ExamActivityLog.attempt_id.in_(ids)).delete(synchronize_session=False)
                s.query(ExamAccessCode).filter(ExamAccessCode.attempt_id.in_(ids)).delete(synchronize_session=False)
                s.query(ExamAttempt).filter(ExamAttempt.id.in_(ids)).delete(synchronize_session=False)
            s.query(ExamWaveStudent).filter_by(exam_id=eid_).delete(synchronize_session=False)
            s.query(ExamWave).filter_by(exam_id=eid_).delete(synchronize_session=False)
            ex_ = s.get(OnlineExam, eid_)
            if ex_:
                s.delete(ex_)
        s.commit()
        if made['subject']:
            sub = s.get(Subject, made['subject'])
            if sub: s.delete(sub)
        for uid in made['users']:
            s.query(StudentUEEnrollment).filter_by(student_id=uid).delete(synchronize_session=False)
        s.commit()
        for uid in made['users']:
            u = s.get(User, uid)
            if u: s.delete(u)
        s.commit()
        print('\n(nettoyage terminé)')
    except Exception as e:
        s.rollback(); print('Nettoyage incomplet :', e)

ok = sum(1 for _, r in RESULTS if r)
print(f'\n{ok}/{len(RESULTS)} points réussis')
for n, r in RESULTS:
    if not r: print('  ÉCHEC :', n)
sys.exit(0 if ok == len(RESULTS) else 1)
