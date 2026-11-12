#!/usr/bin/env python
"""Rattrapage = SESSION À PART (examen distinct), réservée aux étudiants choisis ; l'examen d'origine et ses vagues ne sont pas touchés.
    cd cei-api-v2 && .venv-base/bin/python scripts/test_rattrapage.py     (crée puis supprime ses propres comptes et examens de test)
    Sur un serveur réel : RATTRAPAGE_UE_ID=<UE de test> (comptes @temp.edu, aucun e-mail envoyé)."""
import os, sys, uuid
sys.path.insert(0, '.')
from app import app
from models import *
from routes.exams import get_session
from auth_paseto import create_access_token
from sqlalchemy import func
from datetime import datetime, timedelta

s = get_session(); sfx = uuid.uuid4().hex[:6]
_UE = os.environ.get('RATTRAPAGE_UE_ID')
row = (s.query(Subject, func.count(StudentUEEnrollment.id)).join(EC, EC.id == Subject.ec_id)
       .outerjoin(StudentUEEnrollment, StudentUEEnrollment.ue_id == EC.ue_id).filter(EC.ue_id != None)
       .group_by(Subject.id).order_by(func.count(StudentUEEnrollment.id)).first())
subj = row[0]; ue_id = subj.ec.ue_id
if _UE:
    subj = s.query(Subject).join(EC, EC.id == Subject.ec_id).filter(EC.ue_id == int(_UE)).first(); ue_id = int(_UE)
now = datetime.utcnow(); c = app.test_client(); made = {'users': [], 'exams': []}
def mk(tag, role=UserRole.STUDENT):
    u = User(email=f'zz_ratt_{tag}_{sfx}@' + ('temp.edu' if _UE else 'test.local'), full_name=f'ZZ Ratt {tag}', role=role, is_active=True,
             password_hash='x', has_email=not _UE)
    s.add(u); s.commit(); made['users'].append(u.id); return u
def H(u): return {'Authorization': 'Bearer ' + create_access_token(u.id, u.role.value, u.email)}
RES = []
def chk(n, ok, d=''):
    RES.append(bool(ok)); print(('  ✔ ' if ok else '  ✘ ') + n + (f'  — {d!r}' if not ok else ''))
def iso(dt): return dt.isoformat() + 'Z'
try:
    adm = mk('adm', UserRole.ADMIN)
    a = mk('bloque'); b = mk('absent'); cc = mk('copie'); e = mk('autre'); f = mk('horsue')
    for u in (a, b, cc, e):
        s.add(StudentUEEnrollment(student_id=u.id, ue_id=ue_id))
    s.commit()
    ex = OnlineExam(subject_id=subj.id, title='Rattrapage test', duration_minutes=60, start_time=now - timedelta(hours=3), end_time=now - timedelta(hours=1),
                    status=ExamStatus.CLOSED, created_by_id=adm.id, auto_correct=True, enable_calculator=True, max_tab_switches=7)
    s.add(ex); s.commit(); made['exams'].append(ex.id); eid = ex.id
    att_a = ExamAttempt(exam_id=eid, student_id=a.id, status=AttemptStatus.AUTO_SUBMITTED, answers='{}', submitted_at=now - timedelta(hours=1))
    att_c = ExamAttempt(exam_id=eid, student_id=cc.id, status=AttemptStatus.SUBMITTED, answers='{"1":"réponse"}', exam_seen_at=now - timedelta(hours=2),
                        submitted_at=now - timedelta(hours=1), score=3.0)
    s.add_all([att_a, att_c]); s.commit(); id_a, id_c = att_a.id, att_c.id

    print('== Qui peut être admis ==')
    r = c.get(f'/api/online_exams/{eid}/makeup/candidates?limit=500', headers=H(adm)).get_json()
    ids = {x['id'] for x in r['candidates']}
    chk('par défaut : l\'étudiant empêché, l\'absent et l\'inscrit sans copie', {a.id, b.id, e.id} <= ids, ids)
    chk('par défaut : pas celui qui a composé', cc.id not in ids)
    chk('jamais un étudiant non inscrit', f.id not in ids)
    r = c.get(f'/api/online_exams/{eid}/makeup/candidates?limit=500&include_composed=1', headers=H(adm)).get_json()
    row_c = next((x for x in r['candidates'] if x['id'] == cc.id), None)
    chk('« tous les inscrits » : celui qui a composé apparaît, avec sa note', row_c and row_c['composed'] is True and row_c['score'] == 3.0, row_c)
    r = c.get(f'/api/online_exams/{eid}/makeup/candidates?search=absent', headers=H(adm)).get_json()
    chk('recherche par nom', [x['id'] for x in r['candidates']] == [b.id], r)
    chk('un étudiant ne peut pas lister les candidats', c.get(f'/api/online_exams/{eid}/makeup/candidates', headers=H(a)).status_code == 403)

    print('\n== Refus ==')
    start, end = now + timedelta(hours=1), now + timedelta(hours=3)
    body = lambda ids_, st=start, en=end: {'student_ids': ids_, 'start_time': iso(st), 'end_time': iso(en)}
    post = lambda ids_, st=start, en=end, who=adm, ex_=eid: c.post(f'/api/online_exams/{ex_}/makeups', json=body(ids_, st, en), headers=H(who))
    r = post([f.id]); chk('étudiant non inscrit refusé', r.status_code == 400, r.get_json())
    r = post([a.id], now - timedelta(hours=2), now - timedelta(hours=1)); chk('créneau dans le passé refusé', r.status_code == 400, r.get_json())
    r = post([a.id], start, start + timedelta(minutes=20)); chk('créneau plus court que la durée refusé', r.status_code == 400, r.get_json())
    r = post([]); chk('aucun étudiant : refusé', r.status_code == 400, r.get_json())
    r = post([a.id], who=a); chk('un étudiant ne peut pas organiser un rattrapage', r.status_code == 403, r.status_code)
    live = OnlineExam(subject_id=subj.id, title='Rattrapage test en cours', duration_minutes=60, start_time=now - timedelta(minutes=5),
                      end_time=now + timedelta(hours=1), status=ExamStatus.ACTIVE, created_by_id=adm.id)
    s.add(live); s.commit(); made['exams'].append(live.id)
    r = post([a.id], ex_=live.id); chk('examen pas encore terminé : rattrapage refusé', r.status_code == 400, r.get_json())

    print('\n== Création : une session à part ==')
    r = post([a.id, b.id, cc.id]); j = r.get_json() or {}
    chk('rattrapage créé', r.status_code == 201 and j.get('success'), (r.status_code, j))
    mid = (j.get('exam') or {}).get('id'); made['exams'].append(mid)
    s.expire_all(); m = s.get(OnlineExam, mid); base = s.get(OnlineExam, eid)
    chk('c\'est un examen distinct, rattaché à l\'examen d\'origine', mid and mid != eid and m.makeup_of_id == eid)
    chk('titre « … — Rattrapage »', m.title == 'Rattrapage test — Rattrapage', m.title)
    chk('ouvert, avec les horaires choisis', m.status == ExamStatus.ACTIVE and abs((m.start_time - start).total_seconds()) < 2 and abs((m.end_time - end).total_seconds()) < 2)
    chk('mêmes sujet, durée et réglages', m.subject_id == base.subject_id and m.duration_minutes == 60 and m.auto_correct and m.enable_calculator and m.max_tab_switches == 7)
    chk('résultats non publiés, sans correction planifiée héritée', not m.results_published and m.scheduled_correction_at is None)
    chk('l\'examen d\'origine n\'est PAS modifié (statut, horaires)', base.status == ExamStatus.CLOSED and abs((base.end_time - (now - timedelta(hours=1))).total_seconds()) < 5)
    chk('aucune vague créée, ni pour l\'origine ni pour le rattrapage', s.query(ExamWave).filter(ExamWave.exam_id.in_([eid, mid])).count() == 0)
    chk('aucune copie de l\'origine effacée', s.query(ExamAttempt).filter(ExamAttempt.id.in_([id_a, id_c])).count() == 2)
    chk('la liste du rattrapage contient les trois choisis', {x for (x,) in s.query(ExamMakeupStudent.student_id).filter_by(exam_id=mid)} == {a.id, b.id, cc.id})
    r = c.get(f'/api/online_exams/{eid}/makeups', headers=H(adm)).get_json()
    chk('l\'examen d\'origine liste son rattrapage', len(r['makeups']) == 1 and r['makeups'][0]['id'] == mid and r['makeups'][0]['students'] == 3, r)
    r = c.get(f'/api/online_exams/{mid}/makeup/candidates?limit=500', headers=H(adm)).get_json()
    chk('déjà admis : signalé dans la liste des candidats', all(x['makeup'] for x in c.get(f'/api/online_exams/{eid}/makeup/candidates?limit=500&include_composed=1', headers=H(adm)).get_json()['candidates'] if x['id'] in (a.id, b.id, cc.id)))
    r = post([e.id], ex_=mid); chk('pas de rattrapage d\'un rattrapage', r.status_code == 400, r.get_json())

    print('\n== Accès : seuls les choisis ==')
    ids_of = lambda u: [x['id'] for x in (c.get('/api/online_exams', headers=H(u)).get_json() or [])]
    chk('le rattrapage figure dans la liste d\'un étudiant choisi', mid in ids_of(b))
    chk('il ne figure PAS dans celle d\'un inscrit non choisi', mid not in ids_of(e))
    r = c.get(f'/api/online_exams/{mid}/details', headers=H(e)); chk('inscrit non choisi : ouverture refusée', r.status_code == 403, r.status_code)
    r = c.post(f'/api/online_exams/{mid}/start', json={}, headers=H(e)); chk('inscrit non choisi : démarrage refusé', r.status_code == 403, (r.status_code, r.get_json()))
    r = c.post(f'/api/online_exams/{mid}/start', json={}, headers=H(a))
    chk('choisi : « pas encore commencé » avant l\'heure', r.status_code == 400 and 'starts_at' in (r.get_json() or {}), (r.status_code, r.get_json()))
    r = c.post(f'/api/online_exams/{eid}/start', json={}, headers=H(a)); chk('l\'examen d\'origine reste terminé pour lui', r.status_code == 400, (r.status_code, r.get_json()))
    s.query(OnlineExam).filter(OnlineExam.id == mid).update({'start_time': now - timedelta(minutes=1)}); s.commit()
    for u, nom in ((a, 'empêché'), (b, 'absent'), (cc, 'a déjà composé')):
        r = c.post(f'/api/online_exams/{mid}/start', json={}, headers=H(u))
        chk(f'choisi ({nom}) : peut composer le rattrapage', r.status_code in (200, 201), (r.status_code, r.get_json()))
    chk('sa copie d\'origine est intacte', s.query(ExamAttempt).filter(ExamAttempt.id == id_c).count() == 1)
    chk('le rattrapage a ses propres copies', s.query(ExamAttempt).filter_by(exam_id=mid).count() == 3)
    r = c.post(f'/api/online_exams/{mid}/start', json={}, headers=H(e)); chk('non choisi : toujours refusé pendant le rattrapage', r.status_code == 403, r.status_code)

    print('\n== Ajout après coup ==')
    r = c.post(f'/api/online_exams/{mid}/makeup_students', json={'student_ids': [e.id]}, headers=H(adm))
    chk('ajouter un étudiant au rattrapage', r.status_code == 200 and r.get_json().get('added') == 1, (r.status_code, r.get_json()))
    r = c.post(f'/api/online_exams/{mid}/makeup_students', json={'student_ids': [e.id]}, headers=H(adm))
    chk('ajout déjà fait : rien en double', r.status_code == 200 and r.get_json().get('added') == 0, r.get_json())
    r = c.post(f'/api/online_exams/{mid}/makeup_students', json={'student_ids': [f.id]}, headers=H(adm))
    chk('ajout d\'un non-inscrit refusé', r.status_code == 400, r.get_json())
    r = c.post(f'/api/online_exams/{mid}/start', json={}, headers=H(e)); chk('l\'étudiant ajouté peut composer', r.status_code in (200, 201), (r.status_code, r.get_json()))
finally:
    s.close(); s2 = get_session()
    try:
        for xid in [i for i in made['exams'] if i]:
            for att in s2.query(ExamAttempt).filter_by(exam_id=xid).all():
                s2.query(ExamAccessCode).filter_by(attempt_id=att.id).delete(synchronize_session=False)
                s2.query(ExamActivityLog).filter_by(attempt_id=att.id).delete(synchronize_session=False)
                s2.query(ProctorAssignment).filter_by(attempt_id=att.id).delete(synchronize_session=False)
                s2.delete(att)
            s2.flush()
            s2.query(ExamMakeupStudent).filter_by(exam_id=xid).delete(synchronize_session=False)
            s2.query(ProctorAssignment).filter_by(exam_id=xid).delete(synchronize_session=False)
            s2.query(ExamProctor).filter_by(exam_id=xid).delete(synchronize_session=False)
        s2.flush()
        for xid in [i for i in made['exams'] if i][::-1]:        # rattrapage d'abord (il référence l'examen d'origine)
            x = s2.get(OnlineExam, xid)
            if x: s2.delete(x); s2.flush()
        s2.query(StudentUEEnrollment).filter(StudentUEEnrollment.student_id.in_(made['users'])).delete(synchronize_session=False)
        for uid in made['users']:
            u = s2.get(User, uid)
            if u: s2.delete(u)
        s2.commit(); print('comptes et examens de test supprimés')
    except Exception as ex_:
        s2.rollback(); print('nettoyage incomplet :', ex_)
    s2.close()
ok = sum(RES); print(f'\n{ok}/{len(RES)} vérifications réussies'); sys.exit(0 if ok == len(RES) else 1)
