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
from models import (ExamProctor, get_session, User, UserRole, OnlineExam, ExamAttempt, ExamStatus, AttemptStatus, EC,
                    ProctorGroupEC, StudentUEEnrollment, ExamWave, ExamWaveStudent, ExamAccessCode,
                    ProctorAssignment, Subject)
from auth_paseto import create_access_token
from helpers import utcnow
from werkzeug.security import generate_password_hash
import services.exam_health as _health
_health.notify_anomalies = lambda: None
import services.moodle_sync as _ms
_ms.is_enabled = lambda: False      # le test ne doit rien publier dans un vrai calendrier Moodle

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
    s.expire_all()
    row = s.query(ExamWaveStudent).filter_by(exam_id=eid, student_id=extra.id).first()
    check("inscrit après coup : rattaché AUTOMATIQUEMENT à une vague qui n'a pas commencé", row is not None and s.get(ExamWave, row.wave_id).position in (2, 3),
          row and s.get(ExamWave, row.wave_id).position)
    sm = c.get(f'/api/online_exams/{eid}/waves', headers=ha).get_json()
    check("plus aucun étudiant sans vague", sm['unassigned'] == 0, sm['unassigned'])
    r = c.post(f'/api/online_exams/{eid}/start', json={}, headers=he)
    check("… il reçoit l'heure de sa vague (et non « aucune vague »)", r.status_code == 400 and (r.get_json() or {}).get('starts_at') and not (r.get_json() or {}).get('wave_missing'), r.get_data(as_text=True)[:160])
    le = next(e for e in c.get('/api/online_exams', headers=he).get_json() if e['id'] == eid)
    check("… et sa liste affiche sa vague", le.get('wave') and not le.get('wave_missing'))

    # plusieurs inscriptions d'un coup : réparties entre les vagues à venir, vague 1 (commencée) intacte
    before = {w.id: s.query(ExamWaveStudent).filter_by(wave_id=w.id).count() for w in waves}
    lot = []
    for i in range(5):
        u = User(email=f'vagues.{tag}.l{i}@test.invalid', full_name=f'Vagues Test Lot {i}', role=UserRole.STUDENT,
                 is_active=True, email_verified=True, has_email=True, password_hash=generate_password_hash('x'))
        s.add(u); s.flush(); made['users'].append(u.id); lot.append(u)
        s.add(StudentUEEnrollment(student_id=u.id, ue_id=ec.ue_id))
    s.commit()
    after = {w.id: s.query(ExamWaveStudent).filter_by(wave_id=w.id).count() for w in waves}
    check("inscriptions en lot : la vague commencée ne reçoit personne", after[waves[0].id] == before[waves[0].id], (before, after))
    check("inscriptions en lot : les 5 sont rattachés", sum(after.values()) - sum(before.values()) == 5, (before, after))
    check("inscriptions en lot : vagues à venir équilibrées (écart ≤ 1)", abs(after[waves[1].id] - after[waves[2].id]) <= 1, after)

    # filet de sécurité : un inscrit dont le rattachement aurait échappé est rattaché à son premier accès
    s.query(ExamWaveStudent).filter_by(exam_id=eid, student_id=extra.id).delete(); s.commit()
    le = next(e for e in c.get('/api/online_exams', headers=he).get_json() if e['id'] == eid)
    check("filet de sécurité : rattaché à son premier accès", le.get('wave') and not le.get('wave_missing'))

    # désinscrit : plus aucune vague (les non-inscrits ne sont jamais rattachés)
    ghost = User(email=f'vagues.{tag}.g@test.invalid', full_name='Vagues Test Non inscrit', role=UserRole.STUDENT,
                 is_active=True, email_verified=True, has_email=True, password_hash=generate_password_hash('x'))
    s.add(ghost); s.commit(); made['users'].append(ghost.id)
    check("un étudiant non inscrit à l'UE n'est jamais rattaché", s.query(ExamWaveStudent).filter_by(exam_id=eid, student_id=ghost.id).count() == 0)

    w3 = waves[2]
    cur = s.query(ExamWaveStudent).filter_by(exam_id=eid, student_id=extra.id).first()
    target = waves[1] if cur.wave_id == w3.id else w3
    r = c.post(f'/api/online_exams/{eid}/waves/{target.id}/students', headers=ha, json={'student_ids': [extra.id]})
    check("rattachement manuel à une autre vague", r.status_code == 200 and r.get_json().get('moved') == 1, r.get_data(as_text=True)[:140])
    r = c.post(f'/api/online_exams/{eid}/waves/{w3.id}/students', headers=ha, json={'student_ids': [w1[0].id]})
    check("un étudiant qui a commencé ne peut plus changer de vague", r.status_code == 400, r.get_data(as_text=True)[:140])

    print('\n== Règles de cohérence ==')
    r = c.post(f'/api/online_exams/{eid}/waves/plan', headers=ha, json={'count': 2, 'first_start': Z(now)})
    check('vagues non redéfinissables après un démarrage', r.status_code == 400)
    r = c.delete(f'/api/online_exams/{eid}/waves', headers=ha)
    check('vagues non supprimables après un démarrage', r.status_code == 400)
    r = c.put(f'/api/admin/online_exams/{eid}', headers=ha, json={'start_time': Z(now), 'duration_minutes': 30})
    check("reprogrammation refusée une fois l'examen démarré", r.status_code == 400, r.status_code)

    print('\n== Prolongation ==')
    s.expire_all()
    w1_end0, w2_end0, w3_end0 = (s.get(ExamWave, w.id).end_time for w in waves)
    r = c.post(f'/api/online_exams/{eid}/extend', headers=ha, json={'extra_minutes': 0})
    check("prolongation de 0 minute refusée", r.status_code == 400)
    r = c.post(f'/api/online_exams/{eid}/extend', headers=ha, json={'extra_minutes': 10})
    check("prolongation sans précision : la vague EN COURS", r.status_code == 200, r.get_data(as_text=True)[:140])
    s.expire_all()
    check("… sa fin avance de 10 min", s.get(ExamWave, waves[0].id).end_time == w1_end0 + timedelta(minutes=10))
    check("… les vagues à venir ne bougent pas", s.get(ExamWave, waves[1].id).end_time == w2_end0 and s.get(ExamWave, waves[2].id).end_time == w3_end0)
    check("… l'étudiant qui compose reçoit 10 min en plus", s.get(ExamAttempt, att1).extra_minutes == 10)
    r = c.post(f'/api/online_exams/{eid}/extend', headers=ha, json={'extra_minutes': 5, 'wave_id': waves[2].id})
    check("prolongation d'une vague désignée", r.status_code == 200)
    s.expire_all()
    check("… seule cette vague avance", s.get(ExamWave, waves[2].id).end_time == w3_end0 + timedelta(minutes=5) and s.get(ExamWave, waves[1].id).end_time == w2_end0)
    check("l'enveloppe suit la dernière vague", s.get(OnlineExam, eid).end_time == s.get(ExamWave, waves[2].id).end_time)
    s.get(ExamAttempt, att1).extra_minutes = 0; s.commit()
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
    print('\n== Surveillants : répartition vague par vague ==')
    from models import ExamProctor
    procs = []
    for i in range(2):
        pu = User(email=f'vagues.{tag}.p{i}@test.invalid', full_name=f'Vagues Test Surveillant {i}', role=UserRole.SURVEILLANT,
                  is_active=True, email_verified=True, has_email=True, password_hash=generate_password_hash('x'))
        s.add(pu); s.flush(); procs.append(pu); made['users'].append(pu.id)
    s.commit()
    r = c.post('/api/online_exams', headers=ha, json={
        'subject_id': made['subject'], 'title': f'Surv {tag}',
        'start_time': Z(now + timedelta(minutes=600)), 'end_time': Z(now + timedelta(minutes=660))})
    eid4 = (r.get_json().get('exam') or r.get_json()).get('id'); made['exams'].append(eid4)
    for pu in procs:
        s.add(ExamProctor(exam_id=eid4, proctor_id=pu.id, assigned_by_id=admin.id))
    s.commit()
    r = c.post(f'/api/online_exams/{eid4}/waves/plan', headers=ha, json={'count': 3, 'first_start': Z(now + timedelta(minutes=600)), 'order': 'aleatoire'})
    check('examen par vagues avec 2 surveillants', r.status_code == 201, r.get_data(as_text=True)[:140])
    s.expire_all()
    pos = {sid: p for sid, p in s.query(ExamWaveStudent.student_id, ExamWave.position).join(ExamWave, ExamWave.id == ExamWaveStudent.wave_id).filter(ExamWaveStudent.exam_id == eid4)}
    pa = s.query(ProctorAssignment).filter_by(exam_id=eid4).all()
    check('chaque étudiant a un surveillant', len(pa) == len(pos) and len(pos) >= 6, (len(pa), len(pos)))
    ok_bal = True
    for wv in (1, 2, 3):
        per = [sum(1 for a in pa if a.proctor_id == pu.id and pos.get(a.student_id) == wv) for pu in procs]
        ok_bal = ok_bal and max(per) - min(per) <= 1
    check('dans CHAQUE vague, les deux surveillants ont la même charge (écart ≤ 1)', ok_bal)

    print('\n== Reprogrammation avant le début ==')
    s.expire_all()
    w3a = s.query(ExamWave).filter_by(exam_id=eid3).order_by(ExamWave.position).all()
    d0 = [(w.start_time, w.end_time) for w in w3a]
    shift = timedelta(minutes=45)
    r = c.put(f'/api/admin/online_exams/{eid3}', headers=ha, json={'start_time': Z(w3a[0].start_time + shift), 'end_time': Z(w3a[-1].end_time + shift)})
    check("décaler la date de début décale TOUTES les vagues", r.status_code == 200, r.get_data(as_text=True)[:160])
    s.expire_all()
    w3b = s.query(ExamWave).filter_by(exam_id=eid3).order_by(ExamWave.position).all()
    check("… écarts et durées conservés", all(w3b[i].start_time == d0[i][0] + shift and w3b[i].end_time == d0[i][1] + shift for i in range(2)))
    check("… l'enveloppe suit", s.get(OnlineExam, eid3).start_time == w3b[0].start_time and s.get(OnlineExam, eid3).end_time == w3b[-1].end_time)
    r = c.put(f'/api/admin/online_exams/{eid3}', headers=ha, json={'end_time': Z(w3b[-1].end_time + shift)})
    check("changer seulement la fin est refusé avec une explication", r.status_code == 400 and 'vague' in (r.get_json() or {}).get('error', ''))
    dur0 = s.get(OnlineExam, eid3).duration_minutes
    b_len = [(w.end_time - w.start_time) for w in w3b]; b_gap = w3b[1].start_time - w3b[0].end_time
    r = c.put(f'/api/admin/online_exams/{eid3}', headers=ha, json={'duration_minutes': dur0 + 20})
    check("changer la durée recale les fenêtres", r.status_code == 200, r.get_data(as_text=True)[:140])
    s.expire_all()
    w3c = s.query(ExamWave).filter_by(exam_id=eid3).order_by(ExamWave.position).all()
    check("… chaque fenêtre est plus longue de 20 min, même écart entre vagues",
          all((w3c[i].end_time - w3c[i].start_time) == b_len[i] + timedelta(minutes=20) for i in range(2))
          and (w3c[1].start_time - w3c[0].end_time) == b_gap)
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
            s.query(ExamProctor).filter_by(exam_id=eid_).delete(synchronize_session=False)
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
