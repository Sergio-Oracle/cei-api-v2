#!/usr/bin/env python
"""Édition du sujet depuis « Prévisualiser » : droits, verrou, validation de l'affichage, contrôle du brouillon.
    cd cei-api-v2 && .venv-base/bin/python scripts/test_apercu_edition.py     (crée puis supprime ses propres sujets / examens de test)"""
import sys
sys.path.insert(0, '.')
from app import app
from models import *
from routes.exams import get_session
from auth_paseto import create_access_token
from datetime import datetime, timedelta

s = get_session()
adm = s.query(User).filter(User.role == UserRole.ADMIN).first()
stu = s.query(User).filter(User.role == UserRole.STUDENT).first()
H = {'Authorization': 'Bearer ' + create_access_token(adm.id, adm.role.value, adm.email)}
HS = {'Authorization': 'Bearer ' + create_access_token(stu.id, stu.role.value, stu.email)}
c = app.test_client()
now = datetime.utcnow()
PR = "A. oui\nB. non\nC. peut-être\nD. jamais\n"
TXT = ("Partie A — Les feux\nOn note r = « le feu est rouge ».\n\n"
       f"1. Q1. Question un ? [QCM]\n{PR}\n2. Q2. Question deux ? [QCM]\n{PR}\n3. Q3. Question trois ? [QCM]\n{PR}")
RES, subs, exs = [], [], []
def chk(n, ok, d=''):
    RES.append(bool(ok)); print(('  ✔ ' if ok else '  ✘ ') + n + (f'  — {d}' if d and not ok else ''))
def mk_subject(rubric=''):
    x = Subject(title='TEST aperçu', content=TXT, rubric=rubric, creator_id=adm.id, is_active=True)
    s.add(x); s.commit(); subs.append(x.id); return x.id
def mk_exam(sid, status):
    e = OnlineExam(subject_id=sid, title='TEST aperçu', duration_minutes=30, start_time=now + timedelta(days=2),
                   end_time=now + timedelta(days=2, minutes=30), status=status, created_by_id=adm.id)
    s.add(e); s.commit(); exs.append(e.id); return e.id
try:
    sid = mk_subject("Question 1 (5 pts) :\nQuestion 2 (5 pts) :\nQuestion 3 (5 pts) :\n")
    eid = mk_exam(sid, ExamStatus.SCHEDULED)
    r = c.get(f'/api/online_exams/{eid}/preview', headers=H); j = r.get_json() or {}
    chk("aperçu : l'enseignant peut modifier un sujet d'examen planifié", r.status_code == 200 and j['subject_edit']['can_edit'] and j['subject_edit']['subject_id'] == sid, j.get('subject_edit'))
    chk("aperçu : pas encore validé", j['subject_edit']['validated_at'] is None)

    r = c.post(f'/api/online_exams/{eid}/subject_validation', headers=H, json={'validated': True}); j = r.get_json() or {}
    chk("validation de l'affichage enregistrée (date + validateur)", r.status_code == 200 and j['validated_at'] and j['validated_by'], j)
    j = c.get(f'/api/online_exams/{eid}/preview', headers=H).get_json()
    chk("aperçu suivant : affiche « validé »", j['subject_edit']['validated_at'] is not None and j['subject_validated_at'] is not None)
    chk("un étudiant ne peut pas valider (403)", c.post(f'/api/online_exams/{eid}/subject_validation', headers=HS, json={}).status_code == 403)

    r = c.put(f'/api/subjects/{sid}', headers=H, json={'content': TXT})
    s.expire_all(); chk("enregistrer le MÊME texte garde la validation", r.status_code == 200 and s.get(OnlineExam, eid).subject_validated_at is not None)
    new = TXT.replace('Question un ?', 'Question un modifiée ?')
    r = c.put(f'/api/subjects/{sid}', headers=H, json={'content': new})
    s.expire_all(); chk("modifier le texte remet la validation à zéro", r.status_code == 200 and s.get(OnlineExam, eid).subject_validated_at is None and 'modifiée' in s.get(Subject, sid).content)
    j = c.get(f'/api/online_exams/{eid}/preview', headers=H).get_json()
    chk("l'aperçu reflète le texte modifié", 'modifiée' in str(j['preview']['pages']['p1_blocks']))

    # contrôle d'un brouillon
    r = c.post('/api/subjects/check', headers=H, json={'content': TXT, 'subject_id': sid}); j = r.get_json() or {}
    chk("contrôle : sujet sain → 3 questions, aucune ligne cachée, barème d'accord", r.status_code == 200 and j['questions'] == 3 and j['hidden_lines'] == [] and j['rubric'] is None and j['ok'], j)
    r = c.post('/api/subjects/check', headers=H, json={'content': TXT + "\nNB : justifiez chaque réponse.\n", 'subject_id': sid}); j = r.get_json()
    chk("contrôle : ligne qui ne serait affichée nulle part → signalée", any('justifiez' in h for h in j['hidden_lines']), j['hidden_lines'])
    r = c.post('/api/subjects/check', headers=H, json={'content': TXT + f"\n4. Q4. Question quatre ? [QCM]\n{PR}", 'subject_id': sid}); j = r.get_json()
    chk("contrôle : question ajoutée sans barème → avertissement barème", j['rubric'] and j['rubric']['missing_in_rubric'] == [4] and any('barème' in i['message'] for i in j['issues']), j.get('rubric'))
    glued = TXT.replace("« le feu est rouge ».\n\n1. Q1.", "« le feu est rouge ». 1. Q1.")
    j = c.post('/api/subjects/check', headers=H, json={'content': glued}).get_json()
    chk("contrôle : énoncé recollé à Q1 → 3 questions lues quand même", j['questions'] == 3, j['questions'])
    chk("contrôle : refusé à un étudiant (403)", c.post('/api/subjects/check', headers=HS, json={'content': TXT}).status_code == 403)
    chk("contrôle : contenu invalide refusé (400)", c.post('/api/subjects/check', headers=H, json={'content': 5}).status_code == 400)

    # sujet verrouillé : examen actif lié
    sid2 = mk_subject(); eid2 = mk_exam(sid2, ExamStatus.ACTIVE)
    j = c.get(f'/api/online_exams/{eid2}/preview', headers=H).get_json()
    chk("examen actif : sujet verrouillé, raison donnée", j['subject_edit']['can_edit'] is False and 'actif' in (j['subject_edit']['reason'] or ''), j['subject_edit'])
    chk("examen actif : l'enregistrement est refusé (403)", c.put(f'/api/subjects/{sid2}', headers=H, json={'content': new}).status_code == 403)
finally:
    s.close(); s2 = get_session()
    for i in exs:
        e = s2.get(OnlineExam, i)
        if e: s2.delete(e)
    s2.commit()
    for i in subs:
        x = s2.get(Subject, i)
        if x: s2.delete(x)
    s2.commit(); print('sujets et examens de test supprimés')

ok = sum(RES); print(f'\n{ok}/{len(RES)} vérifications réussies')
sys.exit(0 if ok == len(RES) else 1)
