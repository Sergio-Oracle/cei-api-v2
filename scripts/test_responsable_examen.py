#!/usr/bin/env python
"""Test du rôle « Responsable d'examen » par les VRAIES routes (client de test Flask).

    cd cei-api-v2 && .venv-base/bin/python scripts/test_responsable_examen.py

Vérifie : création du compte par l'administrateur, connexion, tout ce que fait l'administrateur
(sujets, examens, vagues, publication... y compris sur les examens d'un professeur et sur un EC sans
affectation), refus de la gestion des utilisateurs, et absence de régression pour professeur/admin.
Crée des comptes, un sujet et des examens de test (sur un EC sans groupe de surveillants), puis supprime tout.
"""
import os, sys, uuid
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as A
import services.moodle_sync as _ms
_ms.is_enabled = lambda: False
import services.exam_waves as _ew
_ew._send_notices = lambda notices: None
import services.exam_health as _health
_health.notify_anomalies = lambda: None
from models import (get_session, User, UserRole, OnlineExam, ExamAttempt, EC, ProctorGroupEC, ECAssignment,
                    ExamWave, ExamWaveStudent, ProctorAssignment, Subject, StudentUEEnrollment)
from auth_paseto import create_access_token
from helpers import utcnow
from werkzeug.security import generate_password_hash

RESULTS = []
def check(nom, ok, detail=''):
    RESULTS.append((nom, bool(ok)))
    print(('  ✔ ' if ok else '  ✘ ') + nom + (f'  — {detail}' if detail and not ok else ''))

c = A.app.test_client()
s = get_session()
tag = uuid.uuid4().hex[:6]
made = {'users': [], 'subjects': [], 'exams': []}
Z = lambda dt: dt.strftime('%Y-%m-%dT%H:%M:%S.000Z')
def H(u): return {'Authorization': 'Bearer ' + create_access_token(u.id, u.role.value, u.email)}
def mk(role, name):
    u = User(email=f'resp.{tag}.{name}@test.invalid', full_name=f'Resp Test {name}', role=role, is_active=True,
             email_verified=True, has_email=True, password_hash=generate_password_hash('MotDePasse-Test-1'))
    s.add(u); s.commit(); made['users'].append(u.id); return u

try:
    print('\n== Création du compte ==')
    admin = s.query(User).filter_by(role=UserRole.ADMIN, is_active=True).order_by(User.id).first()
    ha = H(admin)
    r = c.post('/api/admin/users', headers=ha, json={'email': f'resp.{tag}.mgr@test.invalid', 'full_name': 'Resp Test mgr',
               'name': 'Resp Test mgr', 'password': 'MotDePasse-Test-1', 'role': 'responsable_examen'})
    check("l'administrateur crée un responsable d'examen", r.status_code in (200, 201), r.get_data(as_text=True)[:200])
    mgr = s.query(User).filter_by(email=f'resp.{tag}.mgr@test.invalid').first()
    if mgr: made['users'].append(mgr.id)
    check("le rôle est enregistré", mgr is not None and mgr.role == UserRole.RESPONSABLE_EXAMEN)
    r = c.post('/api/auth/login', json={'email': f'resp.{tag}.mgr@test.invalid', 'password': 'MotDePasse-Test-1'})
    check("il peut se connecter", r.status_code == 200, r.get_data(as_text=True)[:160])
    body = r.get_json() or {}
    check("la réponse porte le rôle responsable_examen", (body.get('user') or body).get('role') == 'responsable_examen', body.get('user') or body)

    prof = mk(UserRole.PROFESSOR, 'prof'); hp = H(prof)
    hm = H(mgr)
    stud = mk(UserRole.STUDENT, 'etu')

    print('\n== Gestion des utilisateurs : refusée ==')
    r = c.get('/api/admin/users?role=professor', headers=hm)
    check("liste des enseignants (lecture, pour les affectations)", r.status_code == 200, r.status_code)
    check("liste sans filtre de rôle refusée", c.get('/api/admin/users', headers=hm).status_code == 403)
    check("liste des étudiants refusée", c.get('/api/admin/users?role=student', headers=hm).status_code == 403)
    check("création de compte refusée", c.post('/api/admin/users', headers=hm, json={'email': f'x.{tag}@test.invalid', 'full_name': 'X', 'password': 'MotDePasse-Test-1', 'role': 'student'}).status_code == 403)
    check("modification de compte refusée", c.put(f'/api/admin/users/{stud.id}', headers=hm, json={'role': 'admin'}).status_code == 403)
    check("suppression de compte refusée", c.delete(f'/api/admin/users/{stud.id}', headers=hm).status_code == 403)
    check("compteurs par rôle refusés", c.get('/api/admin/users/role-counts', headers=hm).status_code == 403)
    check("import de comptes refusé", c.post('/api/admin/users/import-csv', headers=hm).status_code == 403)
    check("modèle d'import de comptes refusé", c.get('/api/admin/users/csv-template', headers=hm).status_code == 403)
    check("création d'étudiant sans e-mail refusée", c.post('/api/admin/users/student-no-email', headers=hm, json={}).status_code == 403)
    s.expire_all()
    check("aucun compte n'a été modifié", s.get(User, stud.id).role == UserRole.STUDENT)
    check("le professeur reste refusé sur l'administration", c.get('/api/admin/dashboard', headers=hp).status_code == 403)

    print('\n== Droits de l\'administrateur ==')
    check("tableau de bord administrateur", c.get('/api/admin/dashboard', headers=hm).status_code == 200)
    check("historique des examens", c.get('/api/admin/exams_history', headers=hm).status_code == 200)
    check("modèle d'import de la maquette", c.get('/api/admin/maquette/csv-template', headers=hm).status_code == 200)
    check("liste des EC", c.get('/api/ecs', headers=hm).status_code == 200)
    check("statistiques d'inscriptions", c.get('/api/admin/enrollments/stats', headers=hm).status_code == 200)
    check("réclamations", c.get('/api/reclamations', headers=hm).status_code == 200)
    check("analytique", c.get('/api/professor/analytics', headers=hm).status_code == 200)
    check("notifications du personnel", c.get('/api/notifications', headers=hm).status_code == 200)

    print('\n== Il agit à la place d\'un enseignant ==')
    ec = (s.query(EC).filter(EC.ue_id.isnot(None), ~EC.id.in_(s.query(ProctorGroupEC.ec_id)),
                             ~EC.ue_id.in_(s.query(StudentUEEnrollment.ue_id)),
                             ~EC.id.in_(s.query(ECAssignment.ec_id))).order_by(EC.id).first())
    assert ec, "aucun EC libre (sans groupe, sans inscrit, sans affectation)"
    content = '\n\n'.join(f"Question {n} — Question de test {n}\nA) Choix A\nB) Choix B" for n in range(1, 3))
    rubric = '=== BARÈME DE NOTATION ===\n' + '\n\n'.join(f"Question {n} (4 pts) :\n  Bonne réponse : B)" for n in range(1, 3)) + '\n\nTOTAL : 8 pts'
    r = c.post('/api/subjects', json={'title': f'Resp {tag}', 'content': content, 'rubric': rubric, 'ec_id': ec.id}, headers=hm)
    check("il crée un sujet sur un EC où personne n'est affecté", r.status_code == 201, r.get_data(as_text=True)[:160])
    if r.status_code == 201: made['subjects'].append(r.get_json()['subject']['id'])
    # sujet et examen créés par le professeur (affecté), pour que le responsable agisse dessus
    s.add(ECAssignment(ec_id=ec.id, professor_id=prof.id)); s.commit()
    r = c.post('/api/subjects', json={'title': f'Prof {tag}', 'content': content, 'rubric': rubric, 'ec_id': ec.id}, headers=hp)
    check("le professeur affecté crée son sujet", r.status_code == 201, r.get_data(as_text=True)[:160])
    sid_prof = r.get_json()['subject']['id']; made['subjects'].append(sid_prof)
    now = utcnow().replace(tzinfo=None)
    r = c.post('/api/online_exams', headers=hp, json={'subject_id': sid_prof, 'title': f'Examen prof {tag}',
               'start_time': Z(now + timedelta(minutes=30)), 'end_time': Z(now + timedelta(minutes=90))})
    eid = (r.get_json().get('exam') or r.get_json()).get('id'); made['exams'].append(eid)
    check("examen créé par le professeur", r.status_code in (200, 201))
    check("le responsable voit les sujets de tous", any(x['id'] == sid_prof for x in c.get('/api/subjects', headers=hm).get_json()))
    check("le responsable voit l'examen du professeur", any(e['id'] == eid for e in c.get('/api/online_exams', headers=hm).get_json()))
    r = c.put(f'/api/admin/online_exams/{eid}', headers=hm, json={'title': f'Examen prof {tag} (modifié)'})
    check("il modifie l'examen du professeur", r.status_code == 200, r.get_data(as_text=True)[:140])
    r = c.post(f'/api/online_exams/{eid}/waves/plan', headers=hm, json={'count': 2, 'first_start': Z(now + timedelta(minutes=30))})
    check("il organise des vagues (aucun inscrit → refus attendu, mais accès accordé)", r.status_code in (201, 400) and r.status_code != 403, r.get_data(as_text=True)[:140])
    r = c.post(f'/api/online_exams/{eid}/activate', headers=hm)
    check("il active l'examen du professeur", r.status_code == 200, r.get_data(as_text=True)[:140])
    check("le professeur voit toujours son examen", any(e['id'] == eid for e in c.get('/api/online_exams', headers=hp).get_json()))
    r = c.get(f'/api/online_exams/{eid}/active_proctoring', headers=hm)
    check("il consulte la surveillance", r.status_code == 200, r.get_data(as_text=True)[:140])
    r = c.post(f'/api/online_exams/{eid}/extend', headers=hm, json={'extra_minutes': 10})
    check("il prolonge l'examen", r.status_code == 200, r.get_data(as_text=True)[:140])
    r = c.post(f'/api/online_exams/{eid}/close', headers=hm)
    check("il clôture l'examen", r.status_code == 200, r.get_data(as_text=True)[:140])
    r = c.put(f'/api/online_exams/{eid}/publish-results', headers=hm, json={'published': True})
    check("il publie les résultats", r.status_code == 200, r.get_data(as_text=True)[:140])

    # examen créé par le responsable : un autre professeur ne peut pas y toucher
    r = c.post('/api/online_exams', headers=hm, json={'subject_id': sid_prof, 'title': f'Examen resp {tag}',
               'start_time': Z(now + timedelta(minutes=300)), 'end_time': Z(now + timedelta(minutes=360))})
    eid2 = (r.get_json().get('exam') or r.get_json()).get('id'); made['exams'].append(eid2)
    check("il crée un examen", r.status_code in (200, 201), r.get_data(as_text=True)[:140])
    other = mk(UserRole.PROFESSOR, 'autre')
    check("un professeur étranger à cet examen reste refusé", c.post(f'/api/online_exams/{eid2}/activate', headers=H(other)).status_code == 403)

    print('\n== Administrateur : aucune régression ==')
    check("tableau de bord", c.get('/api/admin/dashboard', headers=ha).status_code == 200)
    check("liste des comptes", c.get('/api/admin/users', headers=ha).status_code == 200)
    r = c.get('/api/admin/users/role-counts', headers=ha)
    check("compteurs par rôle (avec le nouveau rôle)", r.status_code == 200 and 'responsable_examen' in ((r.get_json() or {}).get('roles') or {}), r.get_data(as_text=True)[:200])
finally:
    s.rollback()
    try:
        for e_ in made['exams']:
            ids = [a.id for a in s.query(ExamAttempt.id).filter_by(exam_id=e_)]
            s.query(ProctorAssignment).filter_by(exam_id=e_).delete(synchronize_session=False)
            if ids: s.query(ExamAttempt).filter(ExamAttempt.id.in_(ids)).delete(synchronize_session=False)
            s.query(ExamWaveStudent).filter_by(exam_id=e_).delete(synchronize_session=False)
            s.query(ExamWave).filter_by(exam_id=e_).delete(synchronize_session=False)
            ex = s.get(OnlineExam, e_)
            if ex: s.delete(ex)
        s.commit()
        for uid in made['users']:
            s.query(ECAssignment).filter_by(professor_id=uid).delete(synchronize_session=False)
        s.commit()
        s.query(Subject).filter(Subject.creator_id.in_(made['users'])).delete(synchronize_session=False)
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
