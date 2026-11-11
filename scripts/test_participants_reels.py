#!/usr/bin/env python
"""Un étudiant n'est compté (participants, statistiques) que lorsqu'il a RÉELLEMENT eu sa copie sous les yeux.

    cd cei-api-v2 && .venv-base/bin/python scripts/test_participants_reels.py

Vraie base, vraies routes (données temporaires supprimées en fin de test). Contexte : la tentative est créée au clic sur
« Démarrer », avant l'étape caméra/micro ; un étudiant bloqué là (problème technique) était compté comme participant et
recevait même 0/20 (devoir du 28/10 : 116 tentatives sur 1 499 sans aucune présence ni réponse).
"""
import os, sys, json, re, uuid
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as A
from models import (get_session, User, UserRole, Subject, OnlineExam, ExamStatus, ExamAttempt, AttemptStatus, ExamActivityLog)
from auth_paseto import create_access_token
from sqlalchemy import text

RESULTS = []
def check(nom, ok, detail=''):
    RESULTS.append((nom, bool(ok)))
    print(('  ✔ ' if ok else '  ✘ ') + nom + (f'  — {detail!r}' if not ok else ''))

tag = uuid.uuid4().hex[:6]
s = get_session(); made = {'users': [], 'sub': None, 'ex': None}
c = A.app.test_client()
def H(u): return {'Authorization': 'Bearer ' + create_access_token(u.id, u.role.value, u.email)}

try:
    adm = User(email=f'part.{tag}.adm@test.invalid', full_name='Part Adm', role=UserRole.ADMIN, is_active=True, email_verified=True, has_email=True, password_hash='x')
    studs = [User(email=f'part.{tag}.{i}@test.invalid', full_name=f'Part Etu {i}', role=UserRole.STUDENT, is_active=True, email_verified=True, has_email=True, password_hash='x') for i in range(8)]
    s.add_all([adm] + studs); s.commit(); made['users'] = [adm.id] + [u.id for u in studs]
    sub = Subject(title='part', content='Question 1 (1 pt) : x', rubric='Question 1 : x', creator_id=adm.id); s.add(sub); s.commit(); made['sub'] = sub.id
    now = datetime.utcnow()
    ex = OnlineExam(subject_id=sub.id, title='Participants réels', duration_minutes=60, start_time=now - timedelta(minutes=10), end_time=now + timedelta(hours=1),
                    status=ExamStatus.ACTIVE, created_by_id=adm.id); s.add(ex); s.commit(); made['ex'] = ex.id
    atts = [ExamAttempt(exam_id=ex.id, student_id=u.id, status=AttemptStatus.IN_PROGRESS, answers='{}') for u in studs]
    s.add_all(atts); s.commit(); ids = [a.id for a in atts]

    print('== Au départ : tentatives créées, personne n\'a encore vu sa copie ==')
    d = ex.to_dict()
    check('carte d\'examen : 0 participant alors que 8 tentatives existent', d['attempts_count'] == 0, d['attempts_count'])
    check('chaque tentative annonce exam_seen = false', all(a.to_dict()['exam_seen'] is False for a in atts))

    print('\n== Les façons de « voir sa copie » ==')
    r = c.post(f'/api/exam_attempts/{ids[0]}/seen', json={}, headers=H(studs[0]))
    check('appel « copie affichée » : accepté', r.status_code == 200 and r.get_json().get('exam_seen') is True, r.get_json())
    s.expire_all(); t1 = s.get(ExamAttempt, ids[0]).exam_seen_at
    c.post(f'/api/exam_attempts/{ids[0]}/seen', json={}, headers=H(studs[0])); s.expire_all()
    check('idempotent : le premier instant est conservé', s.get(ExamAttempt, ids[0]).exam_seen_at == t1 and t1 is not None)
    r = c.post(f'/api/exam_attempts/{ids[1]}/seen', json={}, headers=H(studs[0]))
    check('un étudiant ne peut pas marquer la copie d\'un autre', r.status_code == 404)
    c.post(f'/api/exam_attempts/{ids[2]}/heartbeat', json={}, headers=H(studs[2])); s.expire_all()
    check('secours : le heartbeat de la page d\'examen compte comme copie affichée', s.get(ExamAttempt, ids[2]).exam_seen_at is not None)
    c.post(f'/api/exam_attempts/{ids[3]}/save', json={'answers': json.dumps({'pq_1': ''})}, headers=H(studs[3])); s.expire_all()
    check('sauvegarde de réponses VIDES : ne prouve rien', s.get(ExamAttempt, ids[3]).exam_seen_at is None)
    c.post(f'/api/exam_attempts/{ids[3]}/save', json={'answers': json.dumps({'pq_1': 'A'})}, headers=H(studs[3])); s.expire_all()
    check('secours : une réponse enregistrée prouve que la copie a été vue', s.get(ExamAttempt, ids[3]).exam_seen_at is not None)
    # 4 vus (0, 2, 3 + on ajoute 4), 4 jamais vus (1, 5, 6, 7) ; 5 et 6 soumises vides (échéance), 7 reste en cours
    c.post(f'/api/exam_attempts/{ids[4]}/seen', json={}, headers=H(studs[4]))
    s.query(ExamAttempt).filter(ExamAttempt.id.in_([ids[5], ids[6]])).update({'status': AttemptStatus.AUTO_SUBMITTED, 'score': 0.0, 'submitted_at': now}, synchronize_session=False)
    s.query(ExamAttempt).filter(ExamAttempt.id == ids[4]).update({'status': AttemptStatus.SUBMITTED, 'score': 14.0, 'submitted_at': now}, synchronize_session=False)
    s.commit(); s.expire_all()

    print('\n== Les compteurs ne retiennent que les 4 étudiants qui ont vu leur copie ==')
    ex = s.get(OnlineExam, ex.id)
    check('carte d\'examen : 4 participants', ex.to_dict()['attempts_count'] == 4, ex.to_dict()['attempts_count'])
    r = c.get(f'/api/online_exams/{ex.id}/stats', headers=H(adm)).get_json()
    check('statistiques : 4 participants, 4 jamais affichées signalées à part', r['total'] == 4 and r['not_reached'] == 4, (r['total'], r.get('not_reached')))
    check('statistiques : la moyenne ne compte pas les 0/20 des copies jamais affichées', r['avg_score'] == 14.0 and r['submitted'] == 1, (r['avg_score'], r['submitted']))
    check('statistiques : « en cours » = ceux qui ont vu leur copie (pas ceux bloqués avant)', r['in_progress'] == 3, r['in_progress'])
    r = c.get(f'/api/online_exams/{ex.id}/attempts?limit=50', headers=H(adm)).get_json()
    check('liste des copies : participants_count = 4, not_reached_count = 4', r['participants_count'] == 4 and r['not_reached_count'] == 4, (r['participants_count'], r['not_reached_count']))
    check('liste des copies : TOUTES les tentatives restent visibles, marquées', r['total'] == 8 and sum(1 for a in r['attempts'] if a['exam_seen'] is False) == 4)
    r = c.get(f'/api/online_exams/{ex.id}/attempts?seen=0', headers=H(adm)).get_json()
    check('filtre seen=0 : seulement les 4 jamais affichées', r['total'] == 4 and all(a['exam_seen'] is False for a in r['attempts']))
    r = c.get(f'/api/online_exams/{ex.id}/attempts?seen=1', headers=H(adm)).get_json()
    check('filtre seen=1 : seulement les 4 affichées', r['total'] == 4 and all(a['exam_seen'] is True for a in r['attempts']))

    print('\n== Classement des anciennes tentatives (migration) ==')
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'models.py'), encoding='utf-8').read()
    m = re.search(r'"UPDATE exam_attempts SET exam_seen_at = (.*?)"\s*\n\s*"WHERE (.*?)"\s*\n\s*"(OR \(answers.*?)"\)', src, re.S)
    check('la migration de classement est présente dans models.py', bool(m))
    old_ids = {}
    def mk_old(label, **kw):
        a = ExamAttempt(exam_id=ex.id, student_id=studs[7].id, status=AttemptStatus.SUBMITTED, answers=kw.pop('answers', '{}'), **kw)
        s.add(a); s.commit(); old_ids[label] = a.id
    mk_old('hb', started_at=datetime(2026, 11, 1), last_seen_at=datetime(2026, 11, 1, 10))
    mk_old('rep', started_at=datetime(2026, 11, 1), answers='{"pq_1":"B"}')
    mk_old('vide', started_at=datetime(2026, 11, 1))
    mk_old('ancien', started_at=datetime(2026, 9, 1))
    if m:
        s.query(ExamAttempt).filter(ExamAttempt.id.in_(old_ids.values())).update({'exam_seen_at': None}, synchronize_session=False); s.commit()
        sql = f"UPDATE exam_attempts SET exam_seen_at = {m.group(1)} WHERE id = ANY(:ids) AND ({m.group(2)} {m.group(3)})"
        s.execute(text(sql), {'ids': list(old_ids.values())}); s.commit(); s.expire_all()
        seen = {k: s.get(ExamAttempt, v).exam_seen_at is not None for k, v in old_ids.items()}
        check('ancienne tentative AVEC signal de présence : vue', seen['hb'])
        check('ancienne tentative AVEC réponse : vue', seen['rep'])
        check('ancienne tentative d\'avant le heartbeat (09/10) : vue (rien à reprocher aux anciennes données)', seen['ancien'])
        check('ancienne tentative récente SANS présence ni réponse : jamais affichée', not seen['vide'], seen)
finally:
    try:
        s.rollback()
        if made['ex']:
            att = [x[0] for x in s.query(ExamAttempt.id).filter(ExamAttempt.exam_id == made['ex'])]
            if att: s.query(ExamActivityLog).filter(ExamActivityLog.attempt_id.in_(att)).delete(synchronize_session=False)
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
