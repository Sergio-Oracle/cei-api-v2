#!/usr/bin/env python
"""Accès étudiant à un examen : réservé aux inscrits à l'UE de l'EC (incident du 02/11/2026, examen 152).
    cd cei-api-v2 && .venv-base/bin/python scripts/test_acces_examen.py     (crée puis supprime ses propres comptes et examens de test)"""
import sys, uuid
sys.path.insert(0,'.')
from app import app
from models import *
from routes.exams import get_session
from auth_paseto import create_access_token
from datetime import datetime, timedelta
s=get_session()
sfx=uuid.uuid4().hex[:6]
subj=s.query(Subject).join(EC, EC.id==Subject.ec_id).filter(EC.ue_id!=None).first()
ue_id=subj.ec.ue_id
now=datetime.utcnow()
c=app.test_client()
mk_ids={'users':[], 'exams':[]}
def mkuser(tag, role=UserRole.STUDENT):
    u=User(email=f'zz_acces_{tag}_{sfx}@test.local', full_name=f'ZZ {tag}', role=role, is_active=True, password_hash='x')
    s.add(u); s.commit(); mk_ids['users'].append(u.id); return u
def H(u): return {'Authorization':'Bearer '+create_access_token(u.id,u.role.value,u.email)}
RES=[]
def chk(n,ok,d=''):
    RES.append(bool(ok)); print(('  ✔ ' if ok else '  ✘ ')+n+(f'  — {d}' if d and not ok else ''))
try:
    insc=mkuser('inscrit'); etr=mkuser('etranger'); vag=mkuser('vague')
    s.add(StudentUEEnrollment(student_id=insc.id, ue_id=ue_id)); s.commit()
    e=OnlineExam(subject_id=subj.id,title='TEST accès',duration_minutes=60,start_time=now-timedelta(minutes=5),end_time=now+timedelta(minutes=55),
                 status=ExamStatus.ACTIVE,created_by_id=insc.id)
    s.add(e); s.commit(); mk_ids['exams'].append(e.id); eid=e.id
    chk("Par défaut un examen est réservé aux inscrits", e.to_dict()['restrict_to_enrolled'] is True)
    ids=lambda u:[x['id'] for x in (c.get('/api/online_exams',headers=H(u)).get_json() or [])]
    chk("Liste : l'inscrit voit l'examen", eid in ids(insc))
    chk("Liste : le non-inscrit ne le voit pas", eid not in ids(etr))
    r=c.get(f'/api/online_exams/{eid}/details',headers=H(etr)); chk("Ouverture par lien direct : refusée (403, not_enrolled)", r.status_code==403 and (r.get_json() or {}).get('not_enrolled'), (r.status_code,r.get_json()))
    r=c.post(f'/api/online_exams/{eid}/start',headers=H(etr),json={}); chk("Démarrage par lien direct : refusé (403, not_enrolled)", r.status_code==403 and (r.get_json() or {}).get('not_enrolled'), (r.status_code,r.get_json()))
    chk("Aucune copie créée pour le non-inscrit", s.query(ExamAttempt).filter_by(exam_id=eid,student_id=etr.id).count()==0)
    r=c.post(f'/api/online_exams/{eid}/start',headers=H(insc),json={}); chk("Démarrage de l'inscrit : accepté", r.status_code in (200,201), (r.status_code,r.get_json()))
    r=c.get(f'/api/online_exams/{eid}/details',headers=H(insc)); chk("Ouverture de l'inscrit : acceptée", r.status_code==200, r.status_code)
    # vague : un étudiant rattaché par le personnel garde l'accès
    w=ExamWave(exam_id=eid,name='V1',start_time=now-timedelta(minutes=5),end_time=now+timedelta(minutes=55)); s.add(w); s.commit()
    s.add(ExamWaveStudent(wave_id=w.id,exam_id=eid,student_id=vag.id)); s.commit()
    r=c.get(f'/api/online_exams/{eid}/details',headers=H(vag)); chk("Étudiant rattaché à une vague par le personnel : accès conservé", r.status_code==200, (r.status_code,r.get_json()))
    r=c.get(f'/api/online_exams/{eid}/details',headers=H(etr)); chk("Un étranger reste refusé même quand l'examen a des vagues", r.status_code==403, r.status_code)
    s.query(ExamWaveStudent).filter_by(wave_id=w.id).delete(); s.delete(w); s.commit()
    # levée de la restriction par l'enseignant
    e=s.get(OnlineExam,eid); e.restrict_to_enrolled=False; s.commit()
    chk("Restriction levée : le non-inscrit voit l'examen", eid in ids(etr))
    r=c.get(f'/api/online_exams/{eid}/details',headers=H(etr)); chk("Restriction levée : ouverture acceptée", r.status_code==200, r.status_code)
    e=s.get(OnlineExam,eid); e.restrict_to_enrolled=True; s.commit()
    # réglage via l'API
    adm=s.query(User).filter(User.role==UserRole.ADMIN).first()
    r=c.put(f'/api/admin/online_exams/{eid}',headers=H(adm),json={'restrict_to_enrolled':False}); s.expire_all()
    chk("Le réglage se modifie par l'API des réglages", r.status_code==200 and s.get(OnlineExam,eid).restrict_to_enrolled is False, (r.status_code,r.get_json()))
finally:
    s.close(); s2=get_session()
    for i in mk_ids['exams']:
        for m in (ExamActivityLog,) if False else ():
            pass
        for a in s2.query(ExamAttempt).filter_by(exam_id=i).all(): s2.delete(a)
        s2.flush()
        for w in s2.query(ExamWave).filter_by(exam_id=i).all(): s2.delete(w)
        x=s2.get(OnlineExam,i)
        if x: s2.delete(x)
    s2.flush()
    s2.query(StudentUEEnrollment).filter(StudentUEEnrollment.student_id.in_(mk_ids['users'])).delete(synchronize_session=False)
    for uid in mk_ids['users']:
        u=s2.get(User,uid)
        if u: s2.delete(u)
    s2.commit(); print('comptes et examens de test supprimés')
ok=sum(RES); print(f'\n{ok}/{len(RES)} vérifications réussies'); sys.exit(0 if ok==len(RES) else 1)
