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
    check('statistiques : 4 participants (rien sur les tentatives jamais affichées)', r['total'] == 4 and 'not_reached' not in r, (r['total'], r.get('not_reached')))
    check('statistiques : la moyenne ne compte pas les 0/20 des copies jamais affichées', r['avg_score'] == 14.0 and r['submitted'] == 1, (r['avg_score'], r['submitted']))
    check('statistiques : « en cours » = ceux qui ont vu leur copie (pas ceux bloqués avant)', r['in_progress'] == 3, r['in_progress'])
    r = c.get(f'/api/online_exams/{ex.id}/attempts?limit=50', headers=H(adm)).get_json()
    check('liste des copies : participants_count = 4, aucun compteur de « non affichées »', r['participants_count'] == 4 and 'not_reached_count' not in r, (r['participants_count'], r.get('not_reached_count')))
    check('liste des copies : les tentatives jamais affichées n\'y figurent pas', r['total'] == 4 and all(a['exam_seen'] is True for a in r['attempts']), r['total'])
    r = c.get(f'/api/online_exams/{ex.id}/attempts?seen=0', headers=H(adm)).get_json()
    check('même avec l\'ancien filtre seen=0 : jamais de tentative jamais affichée', r['total'] == 4 and all(a['exam_seen'] is True for a in r['attempts']))
    r = c.get(f'/api/online_exams/{ex.id}/attempts?seen=1', headers=H(adm)).get_json()
    check('filtre seen=1 : seulement les 4 affichées', r['total'] == 4 and all(a['exam_seen'] is True for a in r['attempts']))

    print('\n== Classement des anciennes tentatives (migration) ==')
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'models.py'), encoding='utf-8').read()
    m = re.search(r'"UPDATE exam_attempts SET exam_seen_at = (.*?)"\s*\n\s*"WHERE (.*?)"\s*\n\s*"(OR \(answers.*?)"\)', src, re.S)
    check('la migration de classement est présente dans models.py', bool(m))
    old_ids = {}
    def mk_old(label, **kw):
        # Un étudiant distinct par ancienne tentative : l'index unique (examen, étudiant) interdit deux tentatives du même étudiant.
        u = User(email=f'part.{tag}.old.{label}@test.invalid', full_name=f'Part Old {label}', role=UserRole.STUDENT, is_active=True, email_verified=True, has_email=True, password_hash='x')
        s.add(u); s.commit(); made['users'].append(u.id)
        a = ExamAttempt(exam_id=ex.id, student_id=u.id, status=AttemptStatus.SUBMITTED, answers=kw.pop('answers', '{}'), **kw)
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
    s.query(ExamAttempt).filter(ExamAttempt.id.in_(list(old_ids.values()))).delete(synchronize_session=False); s.commit()   # copies de la section « migration »
    print('\n== Un étudiant qui n\'a jamais vu sa copie ne subit rien : ni 0/20, ni « à corriger », ni code de reprise ==')
    from routes import exams as R
    from services import exam_closer as EC
    import correction_worker as W
    # remise à zéro : 1 et 7 = jamais affichées, soumises sans note ; 4 = vue et soumise sans note (à corriger) ; 0 = vue, en cours
    s.query(ExamAttempt).filter(ExamAttempt.id.in_([ids[1], ids[7]])).update({'status': AttemptStatus.AUTO_SUBMITTED, 'score': None, 'submitted_at': now}, synchronize_session=False)
    s.query(ExamAttempt).filter(ExamAttempt.id == ids[4]).update({'score': None, 'status': AttemptStatus.SUBMITTED}, synchronize_session=False)
    s.commit(); s.expire_all()
    a1, a4 = s.get(ExamAttempt, ids[1]), s.get(ExamAttempt, ids[4])
    check('jamais affichée + aucune réponse : reconnue comme « n\'a pas composé »', R._never_seen(a1) and not R._never_seen(a4))
    R._run_auto_correction(ids[1]); s.expire_all()
    check('correction automatique : aucune note (surtout pas 0/20) pour une copie jamais affichée', s.get(ExamAttempt, ids[1]).score is None)
    r = c.post(f'/api/exam_attempts/{ids[1]}/correct', json={}, headers=H(adm))
    check('bouton « Corriger » sur une copie jamais affichée : refusé avec explication', r.status_code == 400 and 'jamais été affichée' in str(r.get_json()), (r.status_code, r.get_json()))
    check('file de correction : la copie jamais affichée est classée « rien à faire »', W._state(ids[1]) == (False, True), W._state(ids[1]))
    check('file de correction : une copie vue et soumise reste à corriger', W._state(ids[4]) == (True, False), W._state(ids[4]))
    r = c.get(f'/api/online_exams/{ex.id}/attempts?limit=50', headers=H(adm)).get_json()
    check('« à corriger » : seulement la copie vue (1), pas les 2 jamais affichées', r['needs_correction_count'] == 1 and sum(1 for x in r['attempts'] if x['needs_correction']) == 1, (r['needs_correction_count']))

    # signal « copie affichée » manqué MAIS vraies réponses enregistrées : c'est une vraie copie, elle doit être corrigée
    s.query(ExamAttempt).filter(ExamAttempt.id == ids[4]).update({'exam_seen_at': None, 'answers': '{"pq_1":"A"}'}, synchronize_session=False); s.commit(); s.expire_all()
    check('copie avec réponses mais sans signal « affichée » : reste une vraie copie', not R._never_seen(s.get(ExamAttempt, ids[4])))
    r = c.get(f'/api/online_exams/{ex.id}/attempts?limit=50', headers=H(adm)).get_json()
    check('… et reste « à corriger » (jamais exclue à tort)', r['needs_correction_count'] == 1, r['needs_correction_count'])
    s.query(ExamAttempt).filter(ExamAttempt.id == ids[4]).update({'exam_seen_at': datetime.utcnow()}, synchronize_session=False); s.commit()

    print('\n== Reprise : comme avant, le code de reprise est exigé (jamais vu ou déjà vu) ==')
    s.query(ExamAttempt).filter(ExamAttempt.id.in_([ids[1], ids[0]])).update({'status': AttemptStatus.IN_PROGRESS, 'submitted_at': None}, synchronize_session=False); s.commit()
    r = c.post(f'/api/online_exams/{ex.id}/start', json={}, headers=H(studs[1]))
    check('étudiant qui n\'a jamais vu sa copie : le code de reprise reste exigé (comportement d\'origine)', r.status_code == 403 and r.get_json().get('code_required') is True, (r.status_code, r.get_json()))
    r = c.post(f'/api/online_exams/{ex.id}/start', json={}, headers=H(studs[0]))
    check('étudiant qui a déjà vu sa copie : le code de reprise reste exigé', r.status_code == 403 and r.get_json().get('code_required') is True, (r.status_code, r.get_json()))

    print('\n== Surveillance en direct ==')
    r = c.get(f'/api/online_exams/{ex.id}/active_proctoring', headers=H(adm)).get_json()
    rows = {x['attempt_id']: x for x in (r.get('students') or r.get('attempts') or [])} if isinstance(r, dict) else {}
    check('liste live : l\'étudiant qui a vu sa copie y est, celui qui ne l\'a jamais vue n\'y est pas', ids[0] in rows and ids[1] not in rows, list(r.keys()) if isinstance(r, dict) else r)

    r = c.get('/api/student/exam-history', headers=H(studs[1])).get_json()
    check('historique de l\'étudiant qui n\'a jamais vu sa copie : cet examen n\'y figure pas', all(h['exam_id'] != ex.id for h in r['history']), r)
    r = c.get('/api/student/exam-history', headers=H(studs[0])).get_json()
    check('historique de l\'étudiant qui a vu sa copie : l\'examen y figure', any(h['exam_id'] == ex.id for h in r['history']), r)

    print('\n== Clôture à l\'heure de fin : une copie jamais affichée est soumise SANS note ==')
    s.query(ExamAttempt).filter(ExamAttempt.id.in_([ids[1], ids[7]])).update({'status': AttemptStatus.IN_PROGRESS, 'submitted_at': None, 'score': None}, synchronize_session=False)
    s.query(OnlineExam).filter(OnlineExam.id == ex.id).update({'end_time': now - timedelta(hours=1), 'auto_correct': True}, synchronize_session=False); s.commit()
    enq = []
    from services import correction_queue as _cq
    _real_alive, _real_enq = _cq.worker_alive, _cq.enqueue
    _cq.worker_alive = lambda: True; _cq.enqueue = lambda ids_, exam_id, *a, **k: enq.append(list(ids_)) or len(ids_)
    try: EC.close_expired(grace=0)
    finally: _cq.worker_alive, _cq.enqueue = _real_alive, _real_enq
    s.expire_all()
    u1, u7 = s.get(ExamAttempt, ids[1]), s.get(ExamAttempt, ids[7])
    check('clôture : copies jamais affichées soumises, sans note', u1.status == AttemptStatus.AUTO_SUBMITTED and u7.status == AttemptStatus.AUTO_SUBMITTED and u1.score is None and u7.score is None, (u1.status, u1.score))
    flat = [i for batch in enq for i in batch]
    check('clôture : seules les copies vues sont confiées à la correction', ids[1] not in flat and ids[7] not in flat and ids[0] in flat, flat)
finally:
    try:
        s.rollback()
        if made['ex']:
            att = [x[0] for x in s.query(ExamAttempt.id).filter(ExamAttempt.exam_id == made['ex'])]
            if att: s.query(ExamActivityLog).filter(ExamActivityLog.attempt_id.in_(att)).delete(synchronize_session=False)
            if att:
                from models import ExamAccessCode
                s.query(ExamAccessCode).filter(ExamAccessCode.attempt_id.in_(att)).delete(synchronize_session=False)
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
