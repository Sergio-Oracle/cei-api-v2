#!/usr/bin/env python
"""Avis de changement de note par les VRAIES routes (note globale et note par question), résultats publiés.
Données temporaires supprimées en fin de test ; e-mail et cloche interceptés."""
import os, sys, json, time, uuid
from datetime import datetime, timedelta
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as A
from services import grade_change_notice as G
G.DELAY = 0
from models import *
import utils, notif_bus, auth_paseto
mails=[]; bells=[]; RES=[]
def check(n,ok):
    RES.append(bool(ok)); print(('  ✔ ' if ok else '  ✘ ')+n)
utils.send_grade_updated_email=lambda to,n,t,o,nw,p: mails.append((o,nw))
notif_bus.notify_user=lambda uid,typ,t,m,*a,**k: bells.append(m)
tag=uuid.uuid4().hex[:6]; s=get_session(); made=[]
try:
    prof=User(email=f'e2e.{tag}.p@test.invalid',full_name='E2E Prof',role=UserRole.ADMIN,is_active=True,email_verified=True,has_email=True,password_hash='x')
    st=User(email=f'e2e.{tag}.s@test.invalid',full_name='E2E Etu',role=UserRole.STUDENT,is_active=True,email_verified=True,has_email=True,password_hash='x')
    s.add_all([prof,st]); s.commit()
    sub=Subject(title='e2e',content='Question 1 (2 pts) : x',rubric='Question 1 : x',creator_id=prof.id); s.add(sub); s.commit()
    now=datetime.utcnow()
    ex=OnlineExam(subject_id=sub.id,title='E2E avis',duration_minutes=60,start_time=now-timedelta(hours=3),end_time=now-timedelta(hours=1),status=ExamStatus.CLOSED,created_by_id=prof.id,results_published=True); s.add(ex); s.commit()
    at=ExamAttempt(exam_id=ex.id,student_id=st.id,status=AttemptStatus.SUBMITTED,submitted_at=now-timedelta(hours=1),score=5.0,
                   question_scores=json.dumps([{'num':1,'type':'open','max':2.0,'score':1.0,'feedback':''}])); s.add(at); s.commit()
    tok=auth_paseto.create_access_token(prof.id,'admin',prof.email)
    c=A.app.test_client(); H={'Authorization':f'Bearer {tok}'}
    r=c.put(f'/api/exam_attempts/{at.id}/manual-grade',json={'score':7.75,'feedback':'ok'},headers=H)
    time.sleep(3)
    check('route note globale : 5 → 7,75, e-mail + cloche', r.status_code==200 and mails==[(5.0,7.75)] and len(bells)==1 and '5/20 → 7,75/20' in bells[0])
    mails.clear(); bells.clear()
    r=c.put(f'/api/exam_attempts/{at.id}/manual-grade',json={'score':7.75,'feedback':'ok'},headers=H); time.sleep(3)
    check('même note renvoyée : aucun nouvel avis', not mails and not bells)
    # question par question (la note globale a vidé le détail : on le remet)
    s.expire_all(); a=s.get(ExamAttempt,at.id); a.question_scores=json.dumps([{'num':1,'type':'open','max':2.0,'score':1.0,'feedback':''}]); s.commit()
    mails.clear(); bells.clear()
    r=c.put(f'/api/exam_attempts/{at.id}/question-grades',json={'scores':[{'num':1,'score':2.0}]},headers=H); time.sleep(3)
    check('route note par question : 7,75 → 20, e-mail + cloche', r.status_code==200 and mails==[(7.75,20.0)] and len(bells)==1)
finally:
    s.rollback()
    s.query(ExamActivityLog).filter(ExamActivityLog.attempt_id.in_([x.id for x in s.query(ExamAttempt).filter_by(exam_id=ex.id)])).delete(synchronize_session=False)
    s.query(ExamAttempt).filter_by(exam_id=ex.id).delete(); s.query(OnlineExam).filter_by(id=ex.id).delete(); s.query(Subject).filter_by(id=sub.id).delete()
    s.query(User).filter(User.id.in_([prof.id,st.id])).delete(synchronize_session=False); s.commit()
    from cache import _get_client
    rc=_get_client()
    for k in rc.scan_iter('cei:gradechg:*'): rc.delete(k)

print(f"\n{sum(RES)}/{len(RES)} vérifications réussies"); sys.exit(0 if all(RES) and len(RES)==3 else 1)
