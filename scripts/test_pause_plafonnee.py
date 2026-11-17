#!/usr/bin/env python
"""La pause de 3 minutes ne repousse jamais la fin au-delà de la clôture ; le temps accordé par un surveillant, lui, reste (17/11/2026).

    cd cei-api-v2 && .venv-base/bin/python scripts/test_pause_plafonnee.py     (données temporaires, supprimées)

Constat (examen 182, clos à 9 h 45) : « Pause (3 min) » créditait extra_minutes comme le temps accordé, donc deux étudiantes ont composé jusqu'à 9 h 48.
"""
import os, sys, uuid
from datetime import datetime, timedelta
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as A
from services import exam_closer as EC
from services.deadline_cap import deadline_extra_minutes, closes_in_seconds, PAUSE_CREDIT_MINUTES
from cache import cache_delete
from models import (get_session, User, UserRole, Subject, OnlineExam, ExamStatus, ExamAttempt, AttemptStatus)
from auth_paseto import create_access_token
from werkzeug.security import generate_password_hash

RESULTS = []
def check(nom, ok, detail=''):
    RESULTS.append((nom, bool(ok))); print(('  ✔ ' if ok else '  ✘ ') + nom + (f'  — {detail}' if detail and not ok else ''))
s = get_session(); tag = uuid.uuid4().hex[:6]; made = {'users': [], 'ex': [], 'sub': None}
c = A.app.test_client()
EC._livekit = lambda *a, **k: {}                                   # LiveKit simulé
def mk(role, n):
    u = User(email=f'pau.{tag}.{n}@test.invalid', full_name=f'Pause Test {n}', role=role, is_active=True, email_verified=True, has_email=True, password_hash=generate_password_hash('x'))
    s.add(u); s.commit(); made['users'].append(u.id); return u
def H(u): return {'Authorization': 'Bearer ' + create_access_token(u.id, u.role.value, u.email)}

class Fake:                                                         # tentative factice pour la fonction pure
    def __init__(self, extra, pause): self.extra_minutes, self.pause_used = extra, pause
try:
    print('\n== Règle de calcul ==')
    check("la pause seule (3 min) ne compte plus", deadline_extra_minutes(Fake(3, True)) == 0)
    check("temps accordé (+4) sans pause : intégralement compté", deadline_extra_minutes(Fake(4, False)) == 4)
    check("temps accordé (+10) ET pause : seul le temps accordé compte (10)", deadline_extra_minutes(Fake(13, True)) == 10)
    check("aucune valeur négative, même sur des données incohérentes", deadline_extra_minutes(Fake(1, True)) == 0 and deadline_extra_minutes(Fake(None, None)) == 0)
    check("le crédit retiré est celui de la pause (3 min)", PAUSE_CREDIT_MINUTES == 3)

    adm = mk(UserRole.ADMIN, 'adm'); st = [mk(UserRole.STUDENT, f'e{i}') for i in range(5)]
    sub = Subject(title='pause test', content='1. Q ? [QCM]\nA. a\nB. b\n', rubric='Question 1 (1 pt) : A', creator_id=adm.id); s.add(sub); s.commit(); made['sub'] = sub.id
    now = datetime.utcnow()
    def exam(title, end):
        e = OnlineExam(subject_id=sub.id, title=title, duration_minutes=60, start_time=now - timedelta(hours=2), end_time=end, status=ExamStatus.ACTIVE, created_by_id=adm.id)
        s.add(e); s.commit(); made['ex'].append(e.id); return e
    def att(e, u, extra=0, pause=False):
        a = ExamAttempt(exam_id=e.id, student_id=u.id, status=AttemptStatus.IN_PROGRESS, extra_minutes=extra, pause_used=pause, started_at=now - timedelta(hours=1),
                        exam_seen_at=now - timedelta(hours=1), answers='{"reponse":"x"}')
        s.add(a); s.commit(); return a

    print('\n== Le fermeur automatique à l\'heure de fin ==')
    e1 = exam('pause 1', now - timedelta(minutes=3))
    a_pause, a_accorde, a_les_deux, a_rien = att(e1, st[0], 3, True), att(e1, st[1], 10, False), att(e1, st[2], 13, True), att(e1, st[3], 0, False)
    cache_delete(f'cei:room_closed:{e1.id}'); EC.close_expired(); s.expire_all()
    status = lambda a: s.get(ExamAttempt, a.id).status
    check("étudiant avec SEULEMENT la pause : soumis à l'heure de fin (plus de 3 minutes en plus)", status(a_pause) == AttemptStatus.AUTO_SUBMITTED)
    check("étudiant sans rien : soumis", status(a_rien) == AttemptStatus.AUTO_SUBMITTED)
    check("temps accordé par un surveillant (+10) : la copie reste ouverte", status(a_accorde) == AttemptStatus.IN_PROGRESS)
    check("temps accordé (+10) ET pause : la copie reste ouverte (le temps accordé est conservé)", status(a_les_deux) == AttemptStatus.IN_PROGRESS)

    print('\n== Temps restant donné à la page de l\'étudiant ==')
    e2 = exam('pause 2', now + timedelta(minutes=10))
    b1, b2, b3 = att(e2, st[0], 3, True), att(e2, st[1], 4, False), att(e2, st[2], 0, False)
    near = lambda v, t: v is not None and abs(v - t) <= 5
    check("avec la pause : la clôture reste dans 10 min (600 s), pas 13", near(closes_in_seconds(s, e2, b1), 600), closes_in_seconds(s, e2, b1))
    check("avec +4 min accordées : la clôture est dans 14 min (840 s)", near(closes_in_seconds(s, e2, b2), 840), closes_in_seconds(s, e2, b2))
    check("sans rien : 600 s", near(closes_in_seconds(s, e2, b3), 600))
    check("une fois l'heure passée : 0, jamais négatif", closes_in_seconds(s, e1, s.get(ExamAttempt, a_pause.id)) == 0)

    print('\n== Routes lues par la page ==')
    r = c.get(f'/api/exam_attempts/{b1.id}/subject', headers=H(st[0])); j = r.get_json() or {}
    check("la lecture périodique (/subject) renvoie closes_in_seconds ≈ 600", r.status_code == 200 and near(j.get('closes_in_seconds'), 600), (r.status_code, j.get('closes_in_seconds'), j.get('error')))
    r = c.post(f'/api/exam_attempts/{b2.id}/seen', headers=H(st[1])); j = r.get_json() or {}
    check("la confirmation d'affichage (/seen) renvoie closes_in_seconds ≈ 840", r.status_code == 200 and near(j.get('closes_in_seconds'), 840), (r.status_code, j.get('closes_in_seconds'), j.get('error')))
finally:
    s.rollback()
    try:
        from models import ExamActivityLog
        ids = made['ex']
        if ids:
            att_ids = [a.id for a in s.query(ExamAttempt).filter(ExamAttempt.exam_id.in_(ids))]
            if att_ids: s.query(ExamActivityLog).filter(ExamActivityLog.attempt_id.in_(att_ids)).delete(synchronize_session=False)
            s.query(ExamAttempt).filter(ExamAttempt.exam_id.in_(ids)).delete(synchronize_session=False)
            s.query(OnlineExam).filter(OnlineExam.id.in_(ids)).delete(synchronize_session=False)
        if made['sub']: s.query(Subject).filter_by(id=made['sub']).delete()
        if made['users']: s.query(User).filter(User.id.in_(made['users'])).delete(synchronize_session=False)
        s.commit()
    except Exception as ex_:
        s.rollback(); print('nettoyage incomplet :', ex_)
    s.close()
bad = [n for n, ok in RESULTS if not ok]
print(f"\n{len(RESULTS) - len(bad)}/{len(RESULTS)} vérifications OK" + (f" — ÉCHECS : {bad}" if bad else ''))
sys.exit(1 if bad else 0)
