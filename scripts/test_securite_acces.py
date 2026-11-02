#!/usr/bin/env python
"""Contrôles d'accès (audit du 02/11/2026) : rôle et périmètre sur les routes sensibles, secret de l'agent, erreurs 500.
    cd cei-api-v2 && .venv-base/bin/python scripts/test_securite_acces.py     (crée puis supprime ses propres comptes et examens)"""
import sys, uuid, os
sys.path.insert(0,'.')
from app import app, _TECH_ERR
from models import *
from routes.exams import get_session
from auth_paseto import create_access_token
from datetime import datetime, timedelta
s=get_session(); sfx=uuid.uuid4().hex[:6]
subj=s.query(Subject).join(EC, EC.id==Subject.ec_id).filter(EC.ue_id!=None).first()
now=datetime.utcnow(); c=app.test_client(); mk={'users':[],'exams':[]}
def mkuser(tag, role):
    u=User(email=f'zz_sec_{tag}_{sfx}@test.local', full_name=f'ZZ {tag}', role=role, is_active=True, password_hash='x')
    s.add(u); s.commit(); mk['users'].append(u.id); return u
def H(u): return {'Authorization':'Bearer '+create_access_token(u.id,u.role.value,u.email)}
RES=[]
def chk(n,ok,d=''):
    RES.append(bool(ok)); print(('  ✔ ' if ok else '  ✘ ')+n+(f'  — {d}' if d and not ok else ''))
try:
    stu=mkuser('etu',UserRole.STUDENT); insc=mkuser('insc',UserRole.STUDENT); prof=mkuser('prof',UserRole.PROFESSOR); surv=mkuser('surv',UserRole.SURVEILLANT)
    s.add(StudentUEEnrollment(student_id=insc.id, ue_id=subj.ec.ue_id)); s.commit()
    e=OnlineExam(subject_id=subj.id,title='TEST sécurité',duration_minutes=60,start_time=now-timedelta(minutes=5),end_time=now+timedelta(minutes=55),status=ExamStatus.ACTIVE,created_by_id=insc.id)
    s.add(e); s.commit(); mk['exams'].append(e.id); eid=e.id
    a=ExamAttempt(exam_id=eid,student_id=insc.id,status=AttemptStatus.IN_PROGRESS,started_at=now); s.add(a); s.commit(); aid=a.id
    r=c.post(f'/api/online_exams/{eid}/proctor_heartbeat',headers=H(stu),json={}); chk("Un étudiant ne peut pas envoyer de battement de surveillant (403)", r.status_code==403, r.status_code)
    r=c.post(f'/api/online_exams/{eid}/proctor_heartbeat',headers=H(surv),json={}); chk("Un surveillant peut (200)", r.status_code==200, r.status_code)
    r=c.get(f'/api/subjects/{subj.id}/media',headers=H(stu)); chk("Médias d'un sujet : étudiant non inscrit refusé (403)", r.status_code==403, r.status_code)
    r=c.get(f'/api/subjects/{subj.id}/media',headers=H(insc)); chk("Médias d'un sujet : étudiant inscrit à l'examen ouvert accepté", r.status_code==200, r.status_code)
    r=c.get(f'/api/subjects/{subj.id}/media',headers=H(prof)); chk("Médias d'un sujet : enseignant accepté", r.status_code==200, r.status_code)
    r=c.get(f'/api/exam_attempts/{aid}/integrity-report',headers=H(prof)); chk("Rapport d'intégrité : enseignant étranger à l'EC refusé (403)", r.status_code==403, r.status_code)
    r=c.get(f'/api/exam_attempts/{aid}/proctor-notes',headers=H(prof)); chk("Notes de surveillance : enseignant étranger à l'EC refusé (403)", r.status_code==403, r.status_code)
    r=c.get(f'/api/exam_attempts/{aid}/proctor-notes',headers=H(surv)); chk("Notes de surveillance : surveillant non affecté refusé (403)", r.status_code==403, r.status_code)
    r=c.get(f'/api/exam_attempts/{aid}/proctor-notes',headers=H(stu)); chk("Notes de surveillance : étudiant refusé (403)", r.status_code==403, r.status_code)
    # secret de l'agent
    old=os.environ.pop('AGENT_SECRET_KEY',None)
    r=c.get('/api/agent/active_exams',headers={'X-Agent-Secret':'changeme-agent-secret-key'}); chk("Agent : l'ancien secret par défaut n'ouvre plus rien (403)", r.status_code==403, r.status_code)
    r=c.get('/api/agent/active_exams'); chk("Agent : sans secret (403)", r.status_code==403, r.status_code)
    os.environ['AGENT_SECRET_KEY']='secret-de-test-123'
    r=c.get('/api/agent/active_exams',headers={'X-Agent-Secret':'mauvais'}); chk("Agent : mauvais secret (403)", r.status_code==403, r.status_code)
    r=c.get('/api/agent/active_exams',headers={'X-Agent-Secret':'secret-de-test-123'}); chk("Agent : bon secret (200)", r.status_code==200, r.status_code)
    if old is not None: os.environ['AGENT_SECRET_KEY']=old
    else: os.environ.pop('AGENT_SECRET_KEY',None)
    chk("Erreurs 500 : les textes techniques sont reconnus (masqués)", all(_TECH_ERR.search(t) for t in ['(psycopg2.errors.UndefinedColumn) column "x" does not exist','\'NoneType\' object has no attribute \'id\'','KeyError: 3','File "/x/y.py", line 4']))
    chk("Erreurs 500 : un message métier en français n'est pas masqué", not any(_TECH_ERR.search(t) for t in ["Impossible d'envoyer l'e-mail pour le moment","Mot de passe actuel incorrect"]))
finally:
    s.close(); s2=get_session()
    for i in mk['exams']:
        for a in s2.query(ExamAttempt).filter_by(exam_id=i).all(): s2.delete(a)
        s2.flush(); x=s2.get(OnlineExam,i)
        if x: s2.delete(x)
    s2.flush(); s2.query(StudentUEEnrollment).filter(StudentUEEnrollment.student_id.in_(mk['users'])).delete(synchronize_session=False)
    for uid in mk['users']:
        u=s2.get(User,uid)
        if u: s2.delete(u)
    s2.commit(); print('comptes et examens de test supprimés')
ok=sum(RES); print(f'\n{ok}/{len(RES)} vérifications réussies'); sys.exit(0 if ok==len(RES) else 1)
