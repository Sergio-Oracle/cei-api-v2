"""Crée les comptes et l'examen de la répétition générale en navigateur (serveur LOCAL). Affiche un JSON.  Usage : .venv-base/bin/python scripts/e2e/setup.py"""
import sys, os, json, uuid
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from app import app
from models import *
from flask_bcrypt import Bcrypt
from datetime import datetime, timedelta
PW = 'E2eTest!2026'
pw = Bcrypt().generate_password_hash(PW).decode()
s = get_session(); tag = uuid.uuid4().hex[:6]; now = datetime.utcnow()
ec = s.query(EC).filter(EC.ue_id.isnot(None)).first()
mk = lambda role, n: User(email=f'zz_rg_{n.lower()}_{tag}@test.local', full_name=f'ZZ RG {n}', role=role, is_active=True, email_verified=True, has_email=True, password_hash=pw)
adm = mk(UserRole.ADMIN, 'adm'); studs = {n: mk(UserRole.STUDENT, n) for n in ('entree', 'cam', 'sub1', 'sub2', 'tt1', 'tt2', 'remise', 'perm', 'apps1', 'apps2', 'dup1', 'atk1', 'blip1', 'math1')}
s.add_all([adm, *studs.values()]); s.commit()
sub = Subject(title='ZZ répétition', content='1. Question un ? [QCM]\nA. a\nB. b\nC. c\n', rubric='Question 1 (1 pt) : A', creator_id=adm.id, ec_id=ec.id); s.add(sub); s.commit()
for u in studs.values(): s.add(StudentUEEnrollment(student_id=u.id, ue_id=ec.ue_id))
s.commit()
ex = OnlineExam(subject_id=sub.id, title='ZZ répétition générale', duration_minutes=60, start_time=now - timedelta(minutes=5), end_time=now + timedelta(hours=3), status=ExamStatus.ACTIVE, created_by_id=adm.id)
s.add(ex); s.commit()
# Second sujet (scénario 10) : un calcul à variables, un QCM à formules et un Maths / Programmation, comme sur la capture du 24/11/2026
MATH = (
    "1. Calculez l'intégrale \\(I=\\displaystyle\\int_{0}^{\\,{a}} e^{\\,{b}x}\\,dx\\). [CALCULEE]\nVariables : a = 1..5 | b = 1..3\n"
    "2. Quelle est la limite de \\(\\frac{1}{n}\\) quand \\(n\\to\\infty\\) ? [QCM]\nA. \\(0\\)\nB. \\(1\\)\nC. \\(\\infty\\)\n"
    "3. Justifier l'in-intégrabilité de la fonction de Dirichlet sur \\([0,1]\\) [CODE]\nRédiger une preuve formelle (au format LaTeX) qui :\n"
    "* définit les sommes supérieures et inférieures de Darboux pour la fonction \\(g(x)=\\begin{cases}1 & x\\in\\mathbb{Q}\\\\0 & x\\notin\\mathbb{Q}\\end{cases}\\),\n"
    "* montre que, pour toute subdivision, la somme supérieure vaut \\(1\\) et la somme inférieure vaut \\(0\\),\n"
    "* conclut que la fonction n'est pas intégrable au sens de Riemann.\n")
sub2 = Subject(title='ZZ maths', content=MATH, rubric='Question 1 (1 pt) : Formule : a*b\nQuestion 2 (1 pt) : A\nQuestion 3 (1 pt) : preuve', creator_id=adm.id, ec_id=ec.id); s.add(sub2); s.commit()
ex2 = OnlineExam(subject_id=sub2.id, title='ZZ maths', duration_minutes=60, start_time=now - timedelta(minutes=5), end_time=now + timedelta(hours=3), status=ExamStatus.ACTIVE, created_by_id=adm.id)
s.add(ex2); s.commit()
print(json.dumps({'exam2': ex2.id, 'sub2': sub2.id, 'exam': ex.id, 'sub': sub.id, 'adm': adm.email, 'pw': PW, 'students': {n: u.email for n, u in studs.items()}, 'uids': [adm.id] + [u.id for u in studs.values()]}))
