#!/usr/bin/env python
"""Appels étudiant / surveillant : un appel TERMINÉ, refusé ou abandonné ne sonne plus jamais chez l'étudiant.

    cd cei-api-v2 && .venv-base/bin/python scripts/test_appel_termine.py

Reproduit le défaut constaté : les clients renvoient le dernier horodatage reçu AVEC un « Z » ; Python < 3.11
refusait ce « Z » et le filtre `since` était ignoré, donc tout l'historique (appels compris) revenait à chaque
interrogation. Données temporaires, supprimées en fin de test.
"""
import os, sys, json, time, uuid
from datetime import datetime, timedelta
from urllib.parse import quote

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as A
from models import (get_session, User, UserRole, Subject, OnlineExam, ExamStatus, ExamAttempt, AttemptStatus, ExamActivityLog)
from auth_paseto import create_access_token
from werkzeug.security import generate_password_hash

RESULTS = []
def check(nom, ok, detail=''):
    RESULTS.append((nom, bool(ok)))
    print(('  ✔ ' if ok else '  ✘ ') + nom + (f'  — {detail}' if detail and not ok else ''))

s = get_session(); tag = uuid.uuid4().hex[:6]; ids = {}
c = A.app.test_client()
def mk(role, n):
    u = User(email=f'appel.{tag}.{n}@test.invalid', full_name=f'Appel Test {n}', role=role, is_active=True, email_verified=True,
             has_email=True, password_hash=generate_password_hash('x'))
    s.add(u); s.commit(); return u
def H(u): return {'Authorization': 'Bearer ' + create_access_token(u.id, u.role.value, u.email)}

try:
    st = mk(UserRole.STUDENT, 'etu'); ad = mk(UserRole.ADMIN, 'adm'); ids['users'] = [st.id, ad.id]
    sub = Subject(title='appel test', content='Question 1', rubric='x', creator_id=ad.id); s.add(sub); s.commit(); ids['sub'] = sub.id
    now = datetime.utcnow()
    ex = OnlineExam(subject_id=sub.id, title='appel test', duration_minutes=60, start_time=now, end_time=now + timedelta(hours=1),
                    status=ExamStatus.ACTIVE, created_by_id=ad.id); s.add(ex); s.commit(); ids['ex'] = ex.id
    at = ExamAttempt(exam_id=ex.id, student_id=st.id, status=AttemptStatus.IN_PROGRESS); s.add(at); s.commit(); ids['at'] = at.id
    hs, ha = H(st), H(ad)
    P = f'/api/exam_attempts/{at.id}/pending_messages'
    def poll(since=None):
        r = c.get(P + (f'?since={quote(since)}' if since else ''), headers=hs).get_json()
        return r['messages']
    def teacher(kind, text):
        r = c.post(f'/api/exam_attempts/{at.id}/send_warning', headers=ha, json={'message': text, 'type': kind}); time.sleep(0.03); return r.status_code
    def types(ms): return [m['type'] for m in ms]

    print('\n== Appel raté puis terminé ==')
    check("le surveillant appelle", teacher('private_call', 'Appel privé initié par le surveillant') == 200)
    m1 = poll()
    check("l'étudiant reçoit l'appel (une fois)", types(m1) == ['private_call'], types(m1))
    since = m1[-1]['timestamp']
    check("l'horodatage reçu porte un « Z » (cas réel)", since.endswith('Z'), since)
    check("interrogation suivante avec ce repère : plus rien", poll(since) == [])
    check("… et encore une fois : toujours rien", poll(since) == [])
    check("le surveillant raccroche", teacher('end_call', 'Appel privé terminé') == 200)
    m2 = poll(since)
    check("l'étudiant reçoit seulement la fin d'appel", types(m2) == ['end_call'], types(m2))
    since = m2[-1]['timestamp']
    for i in range(3):
        check(f"après la fin : interrogation {i + 1} sans aucun événement", poll(since) == [])
    check("rechargement de la page (sans repère) : l'appel terminé ne sonne pas", 'private_call' not in types(poll()))

    print('\n== Appel et fin dans le même intervalle ==')
    since = None
    teacher('private_call', 'Appel privé initié par le surveillant'); teacher('end_call', 'Appel privé terminé')
    ms = poll(m2[-1]['timestamp'])
    check("l'étudiant qui n'a rien vu n'est pas dérangé par un appel déjà fini", 'private_call' not in types(ms), types(ms))

    print('\n== Appel en cours, étudiant qui recharge ==')
    time.sleep(0.05); teacher('private_call', 'Appel privé initié par le surveillant')
    check("page rechargée : l'appel encore en cours sonne", types(poll()).count('private_call') == 1)
    last = s.query(ExamActivityLog).filter_by(attempt_id=at.id, event_type='teacher_private_call').order_by(ExamActivityLog.id.desc()).first()
    last.timestamp = datetime.utcnow() - timedelta(seconds=200); s.commit()
    check("appel resté sans réponse depuis plus de 90 s : ne sonne plus", 'private_call' not in types(poll()))
    teacher('end_call', 'Appel privé terminé')

    print('\n== Avertissements et messages ==')
    teacher('warning', 'Attention : regardez votre écran')
    ms = poll(); check("avertissement reçu une fois", types(ms).count('warning') == 1, types(ms))
    since = ms[-1]['timestamp']
    check("… jamais rejoué avec le repère", poll(since) == [])
    old = ExamActivityLog(attempt_id=at.id, event_type='teacher_warning', event_data=json.dumps({'message': 'ancien'}),
                          timestamp=datetime.utcnow() - timedelta(minutes=30)); s.add(old); s.commit()
    check("l'historique ancien n'est pas rejoué à l'ouverture de la page", all(m['message'] != 'ancien' for m in poll()))
    check("repère illisible : jamais tout l'historique", all(m['message'] != 'ancien' for m in poll('n-importe-quoi')))

    print('\n== Côté surveillant : demandes d\'appel ==')
    c.post(f'/api/exam_attempts/{at.id}/student_message', headers=hs, json={'message': '[DEMANDE_APPEL] Je souhaite poser une question verbalement au surveillant.'})
    T = f'/api/online_exams/{ex.id}/student_messages'
    r = c.get(T, headers=ha).get_json()['messages']
    check("le surveillant voit la demande", len(r) >= 1)
    ts = r[0]['timestamp']
    check("avec le repère (« Z »), plus rien de ce qu'il a déjà vu", c.get(T + f'?since={quote(ts)}', headers=ha).get_json()['messages'] == [])
    time.sleep(0.05); c.post(f'/api/exam_attempts/{at.id}/student_message', headers=hs, json={'message': 'Une autre question'})
    nxt = c.get(T + f'?since={quote(ts)}', headers=ha).get_json()['messages']
    check("… seulement les nouveaux messages", len(nxt) == 1 and nxt[0]['message'] == 'Une autre question', nxt)

    print('\n== Correction planifiée (même cause : « Z » refusé par Python 3.10) ==')
    Z = lambda dt: dt.strftime('%Y-%m-%dT%H:%M:%S.000Z')
    r = c.post('/api/online_exams', headers=ha, json={'subject_id': sub.id, 'title': f'corr planifiée {tag}',
               'start_time': Z(now + timedelta(hours=2)), 'end_time': Z(now + timedelta(hours=3)),
               'scheduled_correction_at': Z(now + timedelta(hours=4))})
    ex2 = (r.get_json().get('exam') or r.get_json()) if r.status_code in (200, 201) else {}
    if ex2.get('id'): ids['ex2'] = ex2['id']
    check("création d'un examen avec correction planifiée (« Z » du navigateur)", r.status_code in (200, 201), r.get_data(as_text=True)[:140])
    if ex2.get('id'):
        r = c.put(f"/api/admin/online_exams/{ex2['id']}", headers=ha, json={'scheduled_correction_at': Z(now + timedelta(hours=5))})
        check("modification de la correction planifiée", r.status_code == 200, r.get_data(as_text=True)[:140])
        s.expire_all()
        check("heure enregistrée en UTC", abs((s.get(OnlineExam, ex2['id']).scheduled_correction_at - (now + timedelta(hours=5))).total_seconds()) < 2)
finally:
    s.rollback()
    if 'ex2' in ids: s.query(OnlineExam).filter_by(id=ids['ex2']).delete(); s.commit()
    if 'at' in ids:
        s.query(ExamActivityLog).filter_by(attempt_id=ids['at']).delete(); s.query(ExamAttempt).filter_by(id=ids['at']).delete()
    if 'ex' in ids: s.query(OnlineExam).filter_by(id=ids['ex']).delete()
    if 'sub' in ids: s.query(Subject).filter_by(id=ids['sub']).delete()
    s.commit()
    for u in ids.get('users', []): s.query(User).filter_by(id=u).delete()
    from models import StaffAuditLog as _SAL
    s.query(_SAL).filter(_SAL.user_email.like('%@test.invalid')).delete(synchronize_session=False); s.commit()   # traces d'audit des comptes de test
    s.commit(); print('\n(nettoyage terminé)')

ok = sum(1 for _, r in RESULTS if r)
print(f'\n{ok}/{len(RESULTS)} points réussis')
for n, r in RESULTS:
    if not r: print('  ÉCHEC :', n)
sys.exit(0 if ok == len(RESULTS) else 1)
