#!/usr/bin/env python
"""Messages et appels du surveillant : un événement n'est enregistré et livré qu'UNE fois (21/11/2026).
    cd cei-api-v2 && .venv-base/bin/python scripts/test_evenements_surveillant.py   (crée puis supprime ses propres données)

Cause du doublon : l'événement part par deux voies (canal direct + enregistrement relevé toutes les 30 s) ; la protection de l'étudiant ne tenait que 10 s.
Correctif : identifiant d'événement (eid) commun aux deux voies, rendu par pending_messages, et jamais enregistré deux fois côté serveur."""
import sys, uuid, json
sys.path.insert(0, '.')
from app import app
from models import *
from routes.exams import get_session
from auth_paseto import create_access_token
from datetime import datetime, timedelta

s = get_session(); sfx = uuid.uuid4().hex[:6]; now = datetime.utcnow(); c = app.test_client(use_cookies=False); RES = []; ids = {'users': [], 'ex': [], 'sub': [], 'att': []}
def chk(n, ok, d=''):
    RES.append(bool(ok)); print(('  ✔ ' if ok else '  ✘ ') + n + (f'  — {str(d)[:200]}' if d and not ok else ''))
def mk(role, name):
    u = User(email=f'zz_{name}_{sfx}@test.local', full_name=f'ZZ {name}', role=role, is_active=True, password_hash='x'); s.add(u); s.commit(); ids['users'].append(u.id); return u
def H(u): return {'Authorization': 'Bearer ' + create_access_token(u.id, u.role.value, u.email)}
try:
    prof, stu, sv = mk(UserRole.PROFESSOR, 'prof'), mk(UserRole.STUDENT, 'stu'), mk(UserRole.SURVEILLANT, 'sv')
    sub = Subject(title='ZZ evt', content='Question 1. a ? [QCM]\nA. a\nB. b\n', rubric='', creator_id=prof.id); s.add(sub); s.commit(); ids['sub'].append(sub.id)
    ex = OnlineExam(subject_id=sub.id, title='ZZ evt', duration_minutes=60, start_time=now, end_time=now + timedelta(hours=1), status=ExamStatus.ACTIVE, created_by_id=prof.id); s.add(ex); s.commit(); ids['ex'].append(ex.id)
    at = ExamAttempt(exam_id=ex.id, student_id=stu.id, status=AttemptStatus.IN_PROGRESS, started_at=now); s.add(at); s.commit(); ids['att'].append(at.id)
    s.add(ProctorAssignment(proctor_id=sv.id, exam_id=ex.id, student_id=stu.id, attempt_id=at.id)); s.commit()
    send = lambda who, **b: c.post(f'/api/exam_attempts/{at.id}/send_warning', headers=H(who), json=b)
    def logs(t):
        s.expire_all(); return s.query(ExamActivityLog).filter_by(attempt_id=at.id, event_type=t).all()
    since = (now - timedelta(seconds=5)).isoformat() + 'Z'
    poll = lambda: (c.get(f'/api/exam_attempts/{at.id}/pending_messages?since={since}', headers=H(stu)).get_json() or {}).get('messages', [])

    print('== Message ==')
    e1 = 'm' + uuid.uuid4().hex[:16]
    r = send(prof, type='message', message='Bonjour', eid=e1); chk("1er envoi accepté", r.status_code == 200 and not (r.get_json() or {}).get('duplicate'), r.get_json())
    r = send(prof, type='message', message='Bonjour', eid=e1); chk("même identifiant renvoyé (double clic / nouvel essai réseau) : accepté mais marqué doublon", r.status_code == 200 and (r.get_json() or {}).get('duplicate') is True, r.get_json())
    chk("… et UN SEUL enregistrement", len(logs('teacher_message')) == 1, len(logs('teacher_message')))
    r = send(prof, type='message', message='Bonjour', eid='m' + uuid.uuid4().hex[:16]); chk("un autre identifiant avec le même texte = vrai second message (deux enregistrements)", r.status_code == 200 and len(logs('teacher_message')) == 2)
    send(prof, type='message', message='Sans id'); send(prof, type='message', message='Sans id')
    chk("sans identifiant (ancien client) : comportement d'avant conservé, pas de refus", len(logs('teacher_message')) == 4)
    send(prof, type='message', message='court', eid='abc'); send(prof, type='message', message='court', eid='abc')
    chk("identifiant invalide (trop court) ignoré, pas de blocage", len(logs('teacher_message')) == 6)
    send(prof, type='message', message='x', eid="a;drop table--"); chk("identifiant avec caractères interdits ignoré", len(logs('teacher_message')) == 7)
    r = send(sv, type='message', message='Du surveillant', eid='m' + uuid.uuid4().hex[:16]); chk("le surveillant affecté envoie aussi avec identifiant", r.status_code == 200 and not (r.get_json() or {}).get('duplicate'))
    e1b = e1
    r = send(sv, type='message', message='Autre', eid=e1b); chk("l'identifiant est propre à la tentative, pas à l'expéditeur (même id = doublon)", (r.get_json() or {}).get('duplicate') is True)

    print('\n== Relève de l\'étudiant ==')
    ms = poll(); mine = [m for m in ms if m.get('eid') == e1]
    chk("pending_messages rend l'identifiant d'événement", len(mine) == 1, ms[:2])
    chk("… une seule fois pour cet événement", len(mine) == 1)

    print('\n== Appel privé ==')
    ec = 'c' + uuid.uuid4().hex[:16]
    send(prof, type='private_call', message='Appel', eid=ec); send(prof, type='private_call', message='Appel', eid=ec)
    chk("appel lancé deux fois avec le même identifiant : UN seul enregistrement", len(logs('teacher_private_call')) == 1, len(logs('teacher_private_call')))
    ee = 'end-' + ec
    send(prof, type='end_call', message='Fin', eid=ee); send(prof, type='end_call', message='Fin', eid=ee)
    chk("fin d'appel envoyée deux fois : UN seul enregistrement", len(logs('teacher_end_call')) == 1, len(logs('teacher_end_call')))
    ms = poll()
    chk("la relève ne rend pas l'appel terminé", not [m for m in ms if m.get('type') == 'private_call'], ms)
    chk("… mais rend la fin d'appel avec son identifiant", [m for m in ms if m.get('type') == 'end_call' and m.get('eid') == ee])

    print('\n== Accès ==')
    other = mk(UserRole.PROFESSOR, 'prof2')
    chk("un enseignant étranger à l'examen reste refusé", send(other, type='message', message='x', eid='m' + uuid.uuid4().hex[:16]).status_code == 403)
    chk("un étudiant ne peut pas envoyer", send(stu, type='message', message='x').status_code == 403)
finally:
    try:
        s.rollback()
        s.query(ExamActivityLog).filter(ExamActivityLog.attempt_id.in_(ids['att'])).delete(synchronize_session=False)
        s.query(ProctorAssignment).filter(ProctorAssignment.exam_id.in_(ids['ex'])).delete(synchronize_session=False)
        s.query(ExamAttempt).filter(ExamAttempt.id.in_(ids['att'])).delete(synchronize_session=False)
        s.query(OnlineExam).filter(OnlineExam.id.in_(ids['ex'])).delete(synchronize_session=False)
        s.query(Subject).filter(Subject.id.in_(ids['sub'])).delete(synchronize_session=False)
        s.query(User).filter(User.id.in_(ids['users'])).delete(synchronize_session=False); s.commit()
    except Exception as e:
        print('nettoyage :', e)
    s.close()
print(f"\n{sum(RES)}/{len(RES)} vérifications réussies"); sys.exit(0 if all(RES) else 1)
