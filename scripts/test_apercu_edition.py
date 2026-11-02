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
    sid = mk_subject("Question 1 (5 pts) :\n  • Bonne réponse : A) oui\nQuestion 2 (5 pts) :\n  • Bonne réponse : B) non\nQuestion 3 (5 pts) :\n  • Bonne réponse : C) peut-être\n")
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
    chk("contrôle : sujet sain → 3 questions, aucune ligne cachée, barème d'accord", r.status_code == 200 and j['questions'] == 3 and j['hidden_lines'] == [] and j['ok'] and j['grading']['auto_count'] == 3, j)
    r = c.post('/api/subjects/check', headers=H, json={'content': TXT + "\nNB : justifiez chaque réponse.\n", 'subject_id': sid}); j = r.get_json()
    chk("contrôle : ligne qui ne serait affichée nulle part → signalée", any('justifiez' in h for h in j['hidden_lines']), j['hidden_lines'])
    r = c.post('/api/subjects/check', headers=H, json={'content': TXT + f"\n4. Q4. Question quatre ? [QCM]\n{PR}", 'subject_id': sid}); j = r.get_json()
    chk("contrôle : question ajoutée sans barème → erreur claire (« ne sera pas notée »)", any('Question 4' in i['message'] and 'notée automatiquement' in i['message'] for i in j['issues']) and not j['ok'], j['issues'])
    chk("contrôle : tableau de correction fourni (3 questions, bonnes réponses lues)", len(j['grading']['rows']) == 4 and j['grading']['rows'][0]['letters'] == ['A'], j['grading']['rows'][:1])

    # correction : propositions déplacées → le barème n'est plus d'accord ; corrigé par le tableau ; la note suit
    from routes.exams import _deterministic_grade
    MOVED = TXT.replace("2. Q2. Question deux ? [QCM]\nA. oui\nB. non\nC. peut-être\nD. jamais", "2. Q2. Question deux ? [QCM]\nA. non\nB. oui\nC. jamais\nD. peut-être")
    sid3 = mk_subject("=== BARÈME ===\n\nQuestion 1 (5 pts) :\n  • Bonne réponse : A) oui\n\nQuestion 2 (5 pts) :\n  • Bonne réponse : B) non\n\nQuestion 3 (10 pts) :\n  • Bonne réponse : C) peut-être\n\n──────────────────────────\nTOTAL : 20 / 20 points")
    j = c.post('/api/subjects/check', headers=H, json={'content': MOVED, 'subject_id': sid3}).get_json()
    chk("propositions déplacées : le barème désaccordé est signalé avec la nouvelle lettre", any('Question 2' in i['message'] and 'est maintenant' in i['message'] for i in j['issues']) and j['grading']['rows'][1]['suggest'] == 'A', j['issues'])
    j = c.post('/api/subjects/check', headers=H, json={'content': MOVED, 'subject_id': sid3, 'edits': {'2': {'letters': ['A']}}}).get_json()
    chk("le tableau corrige la bonne réponse : plus d'erreur, barème réécrit sans toucher aux autres questions",
        j['ok'] and 'Bonne réponse : A) non' in j['rubric_text'] and 'Bonne réponse : A) oui' in j['rubric_text'] and 'TOTAL : 20 / 20' in j['rubric_text'], j['issues'])
    j2 = c.post('/api/subjects/check', headers=H, json={'content': MOVED, 'subject_id': sid3, 'edits': {'1': {'points': 2}, '3': {'points': 8}}}).get_json()
    chk("points modifiés : barème et total réécrits (2 + 5 + 8 = 15)", 'Question 1 (2 pts)' in j2['rubric_text'] and 'Question 3 (8 pts)' in j2['rubric_text'] and 'TOTAL : 15 / 15' in j2['rubric_text'], j2['rubric_text'])
    r = c.put(f'/api/subjects/{sid3}', headers=H, json={'content': MOVED, 'rubric': j['rubric_text']})
    chk("enregistrement du sujet ET du barème", r.status_code == 200 and 'A) non' in (r.get_json()['subject']['rubric'] or ''))
    s.expire_all(); sub = s.get(Subject, sid3)
    score, mx, tot, _b, _n, _st = _deterministic_grade(sub.content, sub.rubric, {'pq_1': 'A', 'pq_2': 'A', 'pq_3': 'C'})
    chk("correction : l'étudiant qui répond A à Q2 (nouvelle place de la bonne réponse) a tout juste (20/20)", score == 20 and mx == 20, (score, mx))
    score, *_ = _deterministic_grade(sub.content, sub.rubric, {'pq_1': 'A', 'pq_2': 'B', 'pq_3': 'C'})
    chk("correction : l'ancienne lettre (B) n'est plus une bonne réponse (15/20)", score == 15, score)
    G8 = "1. Q1. Choisir ? [QCM]\nA. a\nB. b\nC. c\nD. d\nE. e\nF. f\nG. g\nH. h\n"
    sc, mx, *_ = _deterministic_grade(G8, "Question 1 (4 pts) :\n  • Bonne réponse : G) g\n", {'pq_1': 'G'})
    chk("QCM à 8 propositions : bonne réponse G reconnue par la correction", sc == 4 and mx == 4, (sc, mx))
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
