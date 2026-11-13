#!/usr/bin/env python
"""Mesure, avec la VRAIE IA, le nombre de questions livrées par « generate-full-exam » (cas de l'examen 170 : 20 questions,
QCM + Vrai/Faux + Texte à trous, 20 points).   .venv-base/bin/python scripts/mesure_generation_reelle.py [nb_essais] [nb_questions]"""
import os, sys, re, time, uuid
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as A
from models import get_session, User, UserRole
from auth_paseto import create_access_token
N = int(sys.argv[1]) if len(sys.argv) > 1 else 3
Q = int(sys.argv[2]) if len(sys.argv) > 2 else 20
s = get_session(); tag = uuid.uuid4().hex[:6]
adm = User(email=f'mes_{tag}@temp.edu', password_hash='x', full_name='Mesure', role=UserRole.ADMIN, has_email=False, email_verified=True); s.add(adm); s.commit()
H = {'Authorization': 'Bearer ' + create_access_token(adm.id, 'admin', adm.email)}
c = A.app.test_client()
try:
    for i in range(N):
        body = {'suggestion': {'title': 'Bonnes pratiques d’usage, entretien et sécurité de l’environnement numérique',
            'question_types': 'Questions à Choix Multiples (une seule bonne réponse), Vrai / Faux, Texte à trous (menus déroulants)',
            'question_count': Q, 'total_points': 20, 'difficulty': 'Difficile', 'duration': 90, 'student_level': 'Licence 1',
            'key_points': ['Entretien matériel (ventilateur, poussière, clavier)', 'Mises à jour et sécurité', 'Sauvegarde 3-2-1', 'Périphériques USB', 'Surtensions et onduleur'],
            'description': "Usage, entretien et sécurité de l'environnement numérique d'un étudiant de première année."}}
        t = time.time(); r = c.post('/api/subjects/generate-full-exam', headers=H, json=body); j = r.get_json() or {}
        cont = j.get('content', '')
        nq = len(re.findall(r'(?m)^Question \d+ —', cont))
        parts = re.findall(r'(?m)^Partie[^\n]*', cont)
        print(f"essai {i+1}: HTTP {r.status_code} | {nq} questions | {time.time()-t:.0f}s | parties={[p[:70] for p in parts]} | avertissements={j.get('type_warnings')}", flush=True)
finally:
    s.rollback(); u = s.get(User, adm.id)
    if u: s.delete(u); s.commit()
