#!/usr/bin/env python
"""Répétition générale : déroule un examen complet de A à Z avec des comptes de test
et dit, point par point, si tout fonctionne (réussi / échoué).

    cd cei-api-v2 && .venv-base/bin/python scripts/repetition_generale.py

Passe par les VRAIES routes de l'API (client de test Flask) : création du sujet et de
l'examen, démarrage, sauvegarde, soumission, correction automatique, temps
supplémentaire, clôture, copie vide, publication des notes, dates avec fuseau.
Tout ce qui est créé (comptes, sujet, examen, copies) est supprimé à la fin.
Aucune notification n'est envoyée à de vrais utilisateurs (le sujet de test est
rattaché à un EC sans groupe de surveillants, et l'alerte de santé est neutralisée).
"""
import json, os, re, sys, time, uuid
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as A
import cache, redis
from models import (get_session, User, UserRole, Subject, OnlineExam, ExamAttempt, ExamStatus,
                    AttemptStatus, EC, ProctorGroupEC, StudentUEEnrollment)
from auth_paseto import create_access_token
from helpers import utcnow
from werkzeug.security import generate_password_hash
import services.exam_health as _health

_health.notify_anomalies = lambda: None            # aucune alerte vers de vrais comptes
import routes.exams as _ex   # le filet importe notify_anomalies à l'appel : la neutralisation ci-dessus suffit

RESULTS = []
def check(nom, ok, detail=''):
    RESULTS.append((nom, bool(ok)))
    print(('  ✔ ' if ok else '  ✘ ') + nom + (f'  — {detail}' if detail and not ok else ''))

c = A.app.test_client()
s = get_session()
tag = uuid.uuid4().hex[:6]
made = {'users': [], 'subject': None, 'exam': None}

def H(uid, role, email):
    return {'Authorization': 'Bearer ' + create_access_token(uid, role, email)}

def attendre(fonction, delai=60, pas=1.0):
    t0 = time.time()
    while time.time() - t0 < delai:
        s.expire_all()
        r = fonction()
        if r: return r
        time.sleep(pas)
    return None

try:
    print('\n== Préparation ==')
    admin = s.query(User).filter_by(role=UserRole.ADMIN, is_active=True).order_by(User.id).first()
    ha = H(admin.id, 'admin', admin.email)
    ec = (s.query(EC).filter(EC.ue_id.isnot(None), ~EC.id.in_(s.query(ProctorGroupEC.ec_id)))
          .order_by(EC.id).first())
    assert ec, "aucun EC sans groupe de surveillants pour héberger l'examen de test"
    studs = []
    for i in range(5):
        u = User(email=f'repetition.{tag}.{i}@test.invalid', full_name=f'Test Répétition {i}', role=UserRole.STUDENT,
                 is_active=True, email_verified=True, has_email=True, password_hash=generate_password_hash('x'))
        s.add(u); s.flush(); studs.append(u); made['users'].append(u.id)
        s.add(StudentUEEnrollment(student_id=u.id, ue_id=ec.ue_id))
    s.commit()

    content = '\n\n'.join(f"Question {n} — Question de test {n}\nA) Choix A\nB) Choix B\nC) Choix C" for n in range(1, 6))
    rubric = '=== BARÈME DE NOTATION ===\n' + '\n\n'.join(
        f"Question {n} (4 pts) :\n  Bonne réponse : B) — justification." for n in range(1, 6)) + '\n\nTOTAL : 20 pts'
    r = c.post('/api/subjects', json={'title': f'Répétition {tag}', 'content': content, 'rubric': rubric, 'ec_id': ec.id}, headers=ha)
    check('création du sujet', r.status_code == 201, r.get_data(as_text=True)[:150])
    made['subject'] = r.get_json()['subject']['id']

    now = utcnow().replace(tzinfo=None)
    r = c.post('/api/online_exams', headers=ha, json={
        'subject_id': made['subject'], 'title': f'Répétition {tag}',
        'start_time': (now - timedelta(minutes=5)).strftime('%Y-%m-%dT%H:%M:%S.000Z'),
        'end_time': (now + timedelta(minutes=60)).strftime('%Y-%m-%dT%H:%M:%S.000Z'),
        'auto_correct': True})
    check('création de l\'examen (heures UTC avec Z)', r.status_code in (200, 201), r.get_data(as_text=True)[:200])
    made['exam'] = (r.get_json().get('exam') or r.get_json()).get('id')
    eid = made['exam']
    ex = s.get(OnlineExam, eid); ex.status = ExamStatus.ACTIVE; ex.auto_correct = True; s.commit()

    lst = c.get('/api/online_exams', headers=ha).get_data(as_text=True)
    dates = re.findall(r'"(\d{4}-\d\d-\d\dT[\d:.]+(?:Z|[+-]\d\d:\d\d)?)"', lst)
    check('toutes les dates de la liste portent un fuseau', dates and all(d.endswith('Z') or '+' in d[10:] for d in dates),
          [d for d in dates if not (d.endswith('Z') or '+' in d[10:])][:2])

    print('\n== Déroulement des étudiants ==')
    hs = [H(u.id, 'student', u.email) for u in studs]
    att = []
    for i, u in enumerate(studs):
        cache.cache_set(f'cei:biometric:verify:{u.id}', '1', ttl=300)
        r = c.post(f'/api/online_exams/{eid}/start', json={}, headers=hs[i])
        ok = r.status_code in (200, 201)
        check(f'démarrage étudiant {i}', ok, r.get_data(as_text=True)[:160])
        j = r.get_json() or {}
        att.append((j.get('attempt') or j).get('id') or (j.get('attempt_id')))
    if not all(att):
        raise SystemExit('démarrage impossible — voir ci-dessus')

    bonnes = {f'pq_{n}': 'B' for n in range(1, 6)}
    moitie = dict(bonnes); moitie['pq_4'] = 'A'; moitie['pq_5'] = 'C'     # 3 bonnes sur 5 = 12/20
    partielle = {'pq_1': 'B', 'pq_2': 'B'}                                  # 2 bonnes = 8/20
    # 0 : parfaite ; 1 : 12/20 ; 2 : copie vide (jamais de réponse) ; 3 : temps sup. long ; 4 : temps sup. court
    r = c.post(f'/api/exam_attempts/{att[0]}/save', json={'answers': json.dumps(bonnes)}, headers=hs[0])
    check('sauvegarde des réponses', r.status_code == 200)
    r = c.post(f'/api/exam_attempts/{att[0]}/submit', json={'answers': json.dumps(bonnes)}, headers=hs[0])
    check('soumission étudiant 0', r.status_code == 200, r.get_data(as_text=True)[:120])
    r = c.post(f'/api/exam_attempts/{att[1]}/submit', json={'answers': json.dumps(moitie)}, headers=hs[1])
    check('soumission étudiant 1', r.status_code == 200)
    r = c.post(f'/api/exam_attempts/{att[0]}/submit', json={}, headers=hs[0])
    check('seconde soumission refusée proprement (déjà soumise)', r.status_code == 400 and (r.get_json() or {}).get('already_submitted'))
    for i, rep in ((3, partielle), (4, partielle)):
        c.post(f'/api/exam_attempts/{att[i]}/save', json={'answers': json.dumps(rep)}, headers=hs[i])

    print('\n== Correction automatique à la soumission ==')
    def corrige(i):
        a = s.get(ExamAttempt, att[i]); return a if a and a.corrected_at else None
    a0 = attendre(lambda: corrige(0)); a1 = attendre(lambda: corrige(1))
    check('étudiant 0 corrigé 20/20', a0 and abs(a0.score - 20) < 0.01, a0 and a0.score)
    check('étudiant 1 corrigé 12/20 (points lus dans le barème)', a1 and abs(a1.score - 12) < 0.01, a1 and a1.score)

    print('\n== Fin d\'examen : temps supplémentaire, copie vide, clôture ==')
    s.expire_all()
    ex = s.get(OnlineExam, eid); ex.end_time = now - timedelta(minutes=10); ex.status = ExamStatus.CLOSED
    s.get(ExamAttempt, att[3]).extra_minutes = 30
    s.get(ExamAttempt, att[4]).extra_minutes = 5
    s.commit()
    redis.Redis().delete('cei:sweep:uncorrected')
    _ex._sweep_uncorrected()
    s.expire_all()
    a2, a3, a4 = (s.get(ExamAttempt, att[i]) for i in (2, 3, 4))
    check('étudiant 3 : temps supplémentaire respecté (reste en cours)', a3.status == AttemptStatus.IN_PROGRESS, a3.status)
    check('étudiants 2 et 4 : échéance dépassée → clôturés', a2.status == AttemptStatus.AUTO_SUBMITTED and a4.status == AttemptStatus.AUTO_SUBMITTED, (a2.status, a4.status))
    for i in (2, 4):                                    # laisse passer le délai de 2 min du filet
        s.get(ExamAttempt, att[i]).submitted_at = now - timedelta(minutes=8)
    s.commit()
    redis.Redis().delete('cei:sweep:uncorrected')
    _ex._sweep_uncorrected()
    s.expire_all()
    a2, a4 = s.get(ExamAttempt, att[2]), s.get(ExamAttempt, att[4])
    check('étudiant 2 : copie vide notée 0/20', a2.corrected_at is not None and a2.score == 0, a2.score)
    check('étudiant 4 : copie rendue corrigée 8/20', a4.corrected_at is not None and abs((a4.score or 0) - 8) < 0.01, a4.score)

    print('\n== Notes cachées puis publiées ==')
    r = c.get('/api/student/online_results', headers=hs[0])
    avant = r.get_data(as_text=True)
    check('note invisible avant publication', r.status_code == 200 and '"score": 20' not in avant and '"score":20' not in avant)
    r = c.put(f'/api/online_exams/{eid}/publish-results', json={'published': True}, headers=ha)
    check('publication des résultats', r.status_code == 200, r.get_data(as_text=True)[:120])
    r = c.get('/api/student/online_results', headers=hs[0])
    check('note visible après publication', r.status_code == 200 and '20' in r.get_data(as_text=True))
    ex = s.get(OnlineExam, eid); s.refresh(ex)
    check('date de publication enregistrée', ex.results_published_at is not None)
finally:
    print('\n== Nettoyage ==')
    try:
        s.rollback()
        eid = made['exam']
        if eid:
            s.query(ExamAttempt).filter_by(exam_id=eid).delete(synchronize_session=False)
            from models import ExamProctor, ProctorAssignment, ProctorGroupExam
            s.query(ProctorAssignment).filter_by(exam_id=eid).delete(synchronize_session=False)
            s.query(ExamProctor).filter_by(exam_id=eid).delete(synchronize_session=False)
            s.query(ProctorGroupExam).filter_by(exam_id=eid).delete(synchronize_session=False)
            s.query(OnlineExam).filter_by(id=eid).delete(synchronize_session=False)
        if made['subject']:
            s.query(Subject).filter_by(id=made['subject']).delete(synchronize_session=False)
        if made['users']:
            s.query(StudentUEEnrollment).filter(StudentUEEnrollment.student_id.in_(made['users'])).delete(synchronize_session=False)
            s.query(User).filter(User.id.in_(made['users'])).delete(synchronize_session=False)
        s.commit()
        r_ = redis.Redis()
        for uid in made['users']:
            r_.delete(f'cei:biometric:verify:{uid}', f'cei:notif:user:{uid}', f'cei:notif:history:{uid}', f'cei:session:active:{uid}')
        r_.delete('cei:sweep:uncorrected')
        print('  données de test supprimées')
    except Exception as e:
        print('  ⚠ nettoyage incomplet :', e)

echec = [n for n, ok in RESULTS if not ok]
print(f"\n{'RÉPÉTITION RÉUSSIE' if not echec and RESULTS else 'RÉPÉTITION ÉCHOUÉE'} — {len(RESULTS) - len(echec)}/{len(RESULTS)} points")
for n in echec: print('  à corriger :', n)
sys.exit(1 if echec or not RESULTS else 0)
