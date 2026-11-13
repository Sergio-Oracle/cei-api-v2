#!/usr/bin/env python
"""Surveillants du test UFC (examen 181, 14/11/2026) : 2 comptes SURVEILLANT (mot de passe « passer »), un groupe rattaché à l'examen 181 seul,
pré-répartition des 30 auditeurs. Aucun e-mail ni notification envoyé (l'utilisateur écrit lui-même à Hassimiou Diallo).

    python scripts/surveillants_ufc_examen181.py            # simulation
    python scripts/surveillants_ufc_examen181.py --apply    # écrit"""
import os, sys, json, datetime
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as A  # noqa: F401
from extensions import bcrypt
from models import (get_session, User, UserRole, OnlineExam, ProctorGroup, ProctorGroupMember, ProctorGroupExam, ExamProctor, ProctorAssignment)
from services.proctor_service import group_conflicts, member_conflicts, sync_exam_proctors

APPLY = '--apply' in sys.argv
EXAM_ID, OWNER_ID, GROUP_NAME = 181, 88803, 'UFC - Test M1 SRIV'
PEOPLE = [('Cheikh Baba DIALLO', 'cheikhbabadiallo82@gmail.com'), ('Hawo LY', 'hawo.ly@unchk.edu.sn')]
s = get_session()
exam = s.get(OnlineExam, EXAM_ID)
if not exam or exam.status.value != 'scheduled':
    sys.exit(f"ARRÊT : examen 181 introuvable ou non planifié ({exam and exam.status})")
if s.query(ExamProctor).filter_by(exam_id=EXAM_ID).count() or s.query(ProctorGroupExam).filter_by(exam_id=EXAM_ID).count():
    sys.exit("ARRÊT : l'examen 181 a déjà des surveillants ou un groupe")
if s.query(ProctorGroup).filter_by(name=GROUP_NAME).first():
    sys.exit("ARRÊT : le groupe existe déjà")
exist = [u.email for u in s.query(User).filter(User.email.in_([e for _, e in PEOPLE]))]
if exist:
    sys.exit(f"ARRÊT : comptes déjà existants {exist}")
tpl = s.query(User).filter(User.role == UserRole.SURVEILLANT).order_by(User.id.desc()).first()
print("modèle d'un surveillant existant :", tpl.email, '| niveau', tpl.niveau, '| formation', tpl.formation_id, '| email_verified', tpl.email_verified, '| has_email', tpl.has_email, '| created_via', tpl.created_via)
print(f"À créer : {PEOPLE} ; groupe « {GROUP_NAME} » (propriétaire Hassimiou, id {OWNER_ID}) rattaché à l'examen {EXAM_ID} ({exam.start_time} → {exam.end_time})")
if not APPLY:
    sys.exit("Simulation : rien n'a été écrit.")
h = bcrypt.generate_password_hash('passer').decode()
ids = []
try:
    for name, mail in PEOPLE:
        u = User(email=mail, password_hash=h, full_name=name, role=UserRole.SURVEILLANT, is_active=True, email_verified=True, has_email=True, created_via='import_ufc')
        s.add(u); s.flush(); ids.append(u.id)
    g = ProctorGroup(name=GROUP_NAME, created_by_id=OWNER_ID); s.add(g); s.flush()
    for pid in ids:
        s.add(ProctorGroupMember(group_id=g.id, proctor_id=pid))
    if group_conflicts(s, g.id, [exam]):
        raise RuntimeError("conflit de planning pour ce groupe")
    s.add(ProctorGroupExam(group_id=g.id, exam_id=EXAM_ID)); s.commit()
    sync_exam_proctors(s, exam)          # surveillants de l'examen + pré-répartition des étudiants ; les notifications retournées ne sont PAS envoyées
    s.commit()
    print("OK : comptes", ids, "groupe", g.id)
except Exception as ex:
    s.rollback(); sys.exit(f"ANNULÉ : {ex}")
os.makedirs('backups', exist_ok=True)
json.dump({'user_ids': ids, 'group_id': g.id}, open(f"backups/surveillants_ufc_{datetime.datetime.utcnow():%Y%m%d_%H%M%S}.json", 'w'))
