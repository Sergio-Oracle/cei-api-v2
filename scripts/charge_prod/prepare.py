"""PRÉPARE le test de charge en production (à exécuter SUR le serveur, dans l'environnement de l'API) :  .venv-base/bin/python scripts/charge_prod/prepare.py N P

Crée, dans le pôle de test (EC TST1112), N étudiants, P surveillants, 1 administrateur, un sujet QCM (correction automatique, sans IA) et un examen « TEST DE CHARGE ».
Aucun e-mail (has_email faux), aucune notification aux vrais comptes (alertes de santé neutralisées pour cet examen), refuse de s'exécuter si un vrai examen est en cours.
Écrit /tmp/charge_ctx.json (identifiants et jetons valables 60 min).  Nettoyage : cleanup.py.
"""
import os, sys, json, uuid, time
os.environ['PASETO_ACCESS_TTL_MIN'] = '60'
API = os.environ.get('CEI_API_DIR', '/home/serge/projet-cei/cei-api-v2'); sys.path.insert(0, API); os.chdir(API)
from app import app
from models import *
from auth_paseto import create_access_token
from cache import _get_client
from datetime import datetime, timedelta
from sqlalchemy import text
N = int(sys.argv[1]); P = int(sys.argv[2]); EC_CODE = 'TST1112'
s = get_session()
busy = s.execute(text("select count(*) from online_exams where status='ACTIVE' and end_time > (now() at time zone 'utc')")).scalar() + s.execute(text("select count(*) from exam_attempts where status='IN_PROGRESS'")).scalar()
if busy:
    print(json.dumps({'error': f'Un examen réel est en cours ({busy}) : test de charge refusé.'})); sys.exit(2)
tag = uuid.uuid4().hex[:6]; now = datetime.utcnow()
ec = s.query(EC).filter_by(code=EC_CODE).first()
mk = lambda role, n: User(email=f'zz_charge_{tag}_{n}@test.local', full_name=f'TEST DE CHARGE {n}', role=role, is_active=True, email_verified=True, has_email=False, password_hash='x')
adm = mk(UserRole.ADMIN, 'adm'); s.add(adm); s.commit()
svs = [mk(UserRole.SURVEILLANT, f'sv{i}') for i in range(P)]; s.add_all(svs); s.commit()
stu = []
for b in range(0, N, 1000):
    chunk = [mk(UserRole.STUDENT, f'e{i}') for i in range(b, min(N, b + 1000))]; s.add_all(chunk); s.commit(); stu += chunk
sub = Subject(title='[TEST DE CHARGE] sujet', content='1. Question un ? [QCM]\nA. a\nB. b\nC. c\n', rubric='Question 1 (1 pt) : A', creator_id=adm.id, ec_id=ec.id); s.add(sub); s.commit()
ex = OnlineExam(subject_id=sub.id, title='[TEST DE CHARGE — IGNORER]', duration_minutes=60, start_time=now - timedelta(minutes=10), end_time=now + timedelta(minutes=100), status=ExamStatus.ACTIVE, created_by_id=adm.id)
s.add(ex); s.commit()
for b in range(0, N, 1000): s.add_all([StudentUEEnrollment(student_id=u.id, ue_id=ec.ue_id) for u in stu[b:b + 1000]]); s.commit()
s.add_all([ExamProctor(exam_id=ex.id, proctor_id=v.id, assigned_by_id=adm.id) for v in svs]); s.commit()
for b in range(0, N, 1000): s.add_all([ProctorAssignment(exam_id=ex.id, proctor_id=svs[(b + i) % P].id, student_id=u.id) for i, u in enumerate(stu[b:b + 1000])]); s.commit()
rc = _get_client()
for code in ('entree_bloquee', 'entree_lente', 'hors_ligne', 'liaison_video', 'trop_d_alertes', 'application_instable', 'appels_attente'):
    rc.setex(f'cei:vitals:alert:{ex.id}:{code}', 7200, '1')                  # neutralise les notifications de santé : aucun vrai compte n'est dérangé
tok = lambda u: create_access_token(u.id, u.role.value, u.email)
ctx = {'tag': tag, 'exam': ex.id, 'sub': sub.id, 'adm_id': adm.id, 'adm': tok(adm), 'sv': [tok(v) for v in svs], 'sv_ids': [v.id for v in svs], 'stu': [tok(u) for u in stu], 'stu_ids': [u.id for u in stu]}
json.dump(ctx, open('/tmp/charge_ctx.json', 'w'))
print(json.dumps({'ok': True, 'exam': ex.id, 'tag': tag, 'students': N, 'supervisors': P}))
