#!/usr/bin/env python
"""Réglages d'un examen après sa création (Planifié / En cours / Terminé) : règles de verrouillage du « standard ».
    cd cei-api-v2 && .venv-base/bin/python scripts/test_exam_settings.py     (crée puis supprime ses propres examens de test)"""
import sys, json
sys.path.insert(0,'.')
from app import app
from models import *
from routes.exams import get_session
from auth_paseto import create_access_token
from datetime import datetime, timedelta
s=get_session()
adm=s.query(User).filter(User.role==UserRole.ADMIN).first()
subj=s.query(Subject).first()
tok=create_access_token(adm.id, adm.role.value, adm.email)
H={'Authorization':'Bearer '+tok}
now=datetime.utcnow()
ex=[]
def mk(status, start, dur):
    e=OnlineExam(subject_id=subj.id,title='TEST réglages',duration_minutes=dur,start_time=start,end_time=start+timedelta(minutes=dur),status=status,created_by_id=adm.id)
    s.add(e); s.commit(); ex.append(e.id); return e.id
c=app.test_client()
def put(i,d): 
    r=c.put(f'/api/admin/online_exams/{i}',headers=H,json=d); return r.status_code,(r.get_json() or {})
RES=[]
def chk(n,ok,d=''):
    RES.append(bool(ok)); print(('  ✔ ' if ok else '  ✘ ')+n+(f'  — {d}' if d and not ok else ''))
try:
    a=mk(ExamStatus.SCHEDULED, now+timedelta(days=2), 60)
    chk("Planifié : titre, seuils, pagination, aléatoire, calculatrice, correction auto modifiables", put(a,{'title':'Nouveau titre','max_tab_switches':5,'questions_per_page':3,'randomize_questions':True,'enable_calculator':True,'auto_correct':True})[0]==200)
    chk("Planifié : valeur invalide refusée (questions par page 999)", put(a,{'questions_per_page':999})[0]==400)
    code,j=put(a,{'end_time':(now+timedelta(days=2,minutes=90)).isoformat()+'Z'}); chk("Planifié : fin repoussée, durée recalculée (90 min)", code==200 and j['exam']['duration_minutes']==90, (code,j))
    b=mk(ExamStatus.ACTIVE, now-timedelta(minutes=10), 60)
    chk("En cours : seuils, blocages, correction auto, titre modifiables", put(b,{'max_tab_switches':4,'enable_copy_paste':True,'enable_right_click':True,'auto_correct':True,'title':'x en cours'})[0]==200)
    chk("En cours : pagination verrouillée (409)", put(b,{'questions_per_page':9})[0]==409)
    chk("En cours : ordre aléatoire verrouillé (409)", put(b,{'randomize_questions':True})[0]==409)
    chk("En cours : vérification d'identité verrouillée (409)", put(b,{'require_biometric':True})[0]==409)
    chk("En cours : date de début verrouillée (409)", put(b,{'start_time':(now+timedelta(hours=3)).isoformat()+'Z'})[0]==409)
    chk("En cours : fin raccourcie refusée (409)", put(b,{'end_time':(now+timedelta(minutes=10)).isoformat()+'Z'})[0]==409)
    code,j=put(b,{'end_time':(now+timedelta(minutes=80)).isoformat()+'Z'}); chk("En cours : prolongation acceptée (durée 90)", code==200 and j['exam']['duration_minutes']==90, (code,j))
    chk("En cours : changement de sujet verrouillé (409)", put(b,{'subject_id':subj.id+9999})[0]==409)
    chk("En cours : renvoyer la MÊME valeur d'un champ verrouillé n'est pas une erreur", put(b,{'questions_per_page':5,'title':'x en cours'})[0]==200)
    d=mk(ExamStatus.CLOSED, now-timedelta(days=3), 60)
    chk("Terminé : plus rien ne se modifie (400)", put(d,{'title':'x'})[0]==400)
    r=c.get(f'/api/admin/online_exams/{b}/settings',headers=H); j=r.get_json()
    chk("Politique « En cours » exposée au site (champs verrouillés + raison)", r.status_code==200 and {'questions_per_page','randomize_questions','start_time','subject_id'} <= set(j['locked']))
    r=c.get(f'/api/admin/online_exams/{a}/settings',headers=H); j=r.get_json()
    chk("Politique « Planifié » : rien de verrouillé", r.status_code==200 and j['locked']=={})
finally:
    s.close(); s2=get_session()
    for i in ex:
        e=s2.get(OnlineExam,i)
        if e: s2.delete(e)
    s2.commit(); print('examens de test supprimés')

ok=sum(RES)
print(f'\n{ok}/{len(RES)} vérifications réussies')
sys.exit(0 if ok==len(RES) else 1)
