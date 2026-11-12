#!/usr/bin/env python
"""L'horloge de l'examen démarre à l'AFFICHAGE de la copie, pas au clic sur « Démarrer ».

    cd cei-api-v2 && .venv-base/bin/python scripts/test_horloge_examen.py

Contexte : la tentative est créée au clic sur « Démarrer », avant l'étape caméra / micro / réseau. Un étudiant retenu 10 minutes à cette
étape perdait 10 minutes de composition. Vraie base, vraies routes (données temporaires supprimées en fin de test)."""
import os, sys, uuid
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as A
from models import (get_session, User, UserRole, Subject, OnlineExam, ExamStatus, ExamAttempt, AttemptStatus)
from auth_paseto import create_access_token

RESULTS = []
def check(nom, ok, detail=''):
    RESULTS.append((nom, bool(ok)))
    print(('  ✔ ' if ok else '  ✘ ') + nom + (f'  — {detail!r}' if not ok else ''))

tag = uuid.uuid4().hex[:6]
s = get_session(); made = {'users': [], 'sub': None, 'ex': None}
c = A.app.test_client()
def H(u): return {'Authorization': 'Bearer ' + create_access_token(u.id, u.role.value, u.email)}

try:
    adm = User(email=f'hor.{tag}.adm@test.invalid', full_name='Hor Adm', role=UserRole.ADMIN, is_active=True, email_verified=True, has_email=True, password_hash='x')
    studs = [User(email=f'hor.{tag}.{i}@test.invalid', full_name=f'Hor Etu {i}', role=UserRole.STUDENT, is_active=True, email_verified=True, has_email=True, password_hash='x') for i in range(3)]
    s.add_all([adm] + studs); s.commit(); made['users'] = [adm.id] + [u.id for u in studs]
    sub = Subject(title='hor', content='Question 1 (1 pt) : x', rubric='Question 1 : x', creator_id=adm.id); s.add(sub); s.commit(); made['sub'] = sub.id
    now = datetime.utcnow()
    ex = OnlineExam(subject_id=sub.id, title='Horloge', duration_minutes=60, start_time=now - timedelta(hours=1), end_time=now + timedelta(hours=2),
                    status=ExamStatus.ACTIVE, created_by_id=adm.id); s.add(ex); s.commit(); made['ex'] = ex.id
    # a : a cliqué « Démarrer » il y a 10 minutes, bloqué à l'étape caméra (copie jamais affichée)
    a = ExamAttempt(exam_id=ex.id, student_id=studs[0].id, status=AttemptStatus.IN_PROGRESS, answers='{}', started_at=now - timedelta(minutes=10))
    # b : copie affichée il y a 2 minutes, cliqué il y a 12 minutes
    b = ExamAttempt(exam_id=ex.id, student_id=studs[1].id, status=AttemptStatus.IN_PROGRESS, answers='{}', started_at=now - timedelta(minutes=12),
                    exam_seen_at=now - timedelta(minutes=2))
    # c : signal « affichée » manqué mais des réponses existent : il compose, l'horloge court depuis le clic
    c_ = ExamAttempt(exam_id=ex.id, student_id=studs[2].id, status=AttemptStatus.IN_PROGRESS, answers='{"1":"réponse"}', started_at=now - timedelta(minutes=10))
    s.add_all([a, b, c_]); s.commit()

    print('== Le temps écoulé de la tentative ==')
    check('copie jamais affichée : 0 seconde écoulée (le temps passé à l\'étape caméra ne compte pas)', a.clock_elapsed_seconds() == 0.0, a.clock_elapsed_seconds())
    check('copie affichée il y a 2 min : ≈ 120 s (pas 12 min)', 115 <= b.clock_elapsed_seconds() <= 125, b.clock_elapsed_seconds())
    check('réponses sans signal « affichée » : l\'horloge court depuis le clic (≈ 600 s)', 595 <= c_.clock_elapsed_seconds() <= 610, c_.clock_elapsed_seconds())
    check('to_dict de la tentative : elapsed_seconds suit la même règle', a.to_dict()['elapsed_seconds'] == 0.0 and 115 <= b.to_dict()['elapsed_seconds'] <= 125)

    print('\n== Ce que reçoit la page de l\'étudiant ==')
    r = c.get(f'/api/exam_attempts/{a.id}/subject', headers=H(studs[0])).get_json()
    check('sujet : elapsed_seconds = 0 pour la copie jamais affichée', r.get('elapsed_seconds') == 0.0, r.get('elapsed_seconds'))
    r = c.get(f'/api/exam_attempts/{b.id}/subject', headers=H(studs[1])).get_json()
    check('sujet : elapsed_seconds ≈ 120 pour la copie affichée il y a 2 min', 115 <= (r.get('elapsed_seconds') or 0) <= 125, r.get('elapsed_seconds'))

    print('\n== L\'horloge démarre à l\'appel « copie affichée » ==')
    r = c.post(f'/api/exam_attempts/{a.id}/seen', json={}, headers=H(studs[0]))
    j = r.get_json() or {}
    check('« copie affichée » : accepté, avec le temps écoulé de référence (≈ 0)', r.status_code == 200 and j.get('exam_seen') is True and 0 <= (j.get('elapsed_seconds') or 0) < 5 and j.get('server_now'), (r.status_code, j))
    s.expire_all(); a = s.get(ExamAttempt, a.id)
    check('l\'instant d\'affichage est enregistré', a.exam_seen_at is not None and (datetime.utcnow() - a.exam_seen_at).total_seconds() < 10)
    first = a.exam_seen_at
    r = c.post(f'/api/exam_attempts/{a.id}/seen', json={}, headers=H(studs[0]))
    s.expire_all(); a = s.get(ExamAttempt, a.id)
    check('un second appel ne remet pas l\'horloge à zéro (le premier instant est conservé)', a.exam_seen_at == first)
    s.query(ExamAttempt).filter(ExamAttempt.id == a.id).update({'exam_seen_at': datetime.utcnow() - timedelta(minutes=5)}); s.commit()
    r = c.post(f'/api/exam_attempts/{a.id}/seen', json={}, headers=H(studs[0])).get_json()
    check('reprise : le temps écoulé renvoyé reste celui du premier affichage (≈ 300 s)', 295 <= (r.get('elapsed_seconds') or 0) <= 306, r.get('elapsed_seconds'))

    print('\n== Durée de composition affichée aux enseignants ==')
    s.query(ExamAttempt).filter(ExamAttempt.id == b.id).update({'status': AttemptStatus.SUBMITTED, 'submitted_at': now}); s.commit()
    r = c.get(f'/api/online_exams/{ex.id}/stats', headers=H(adm)).get_json()
    check('durée moyenne = depuis l\'affichage de la copie (≈ 2 min), pas depuis le clic (12 min)', r.get('avg_duration_min') is not None and 1 <= r['avg_duration_min'] <= 3, r.get('avg_duration_min'))
finally:
    try:
        if made['ex']:
            s.query(ExamAttempt).filter(ExamAttempt.exam_id == made['ex']).delete(synchronize_session=False)
            s.query(OnlineExam).filter(OnlineExam.id == made['ex']).delete(synchronize_session=False)
        if made['sub']: s.query(Subject).filter(Subject.id == made['sub']).delete(synchronize_session=False)
        if made['users']: s.query(User).filter(User.id.in_(made['users'])).delete(synchronize_session=False)
        s.commit()
    except Exception as e:
        s.rollback(); print('nettoyage incomplet :', e)
    s.close()
bad = [n for n, ok in RESULTS if not ok]
print(f"\n{len(RESULTS) - len(bad)}/{len(RESULTS)} vérifications réussies" + (f" — ÉCHECS : {bad}" if bad else ''))
sys.exit(1 if bad else 0)
