#!/usr/bin/env python
"""Score de risque recalibré (18/11/2026) : catégories plafonnées, les signaux faibles ne saturent plus, détail « pourquoi ce score », notification sans rafale,
compteurs d'alertes persistants.

    cd cei-api-v2 && .venv-base/bin/python scripts/test_risk_score.py     (données temporaires, supprimées)
"""
import os, sys, uuid
from datetime import datetime, timedelta
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as A
from services import risk_score as R
from cache import _get_client, cache_delete
from models import (get_session, User, UserRole, Subject, OnlineExam, ExamStatus, ExamAttempt, AttemptStatus, ExamActivityLog)
from auth_paseto import create_access_token
from werkzeug.security import generate_password_hash

RESULTS = []
def check(nom, ok, detail=''):
    RESULTS.append((nom, bool(ok))); print(('  ✔ ' if ok else '  ✘ ') + nom + (f'  — {detail}' if detail and not ok else ''))

print('\n== Règles (calcul pur) ==')
W = {'sustained_audio_detected': 10, 'whisper_detected': 6, 'no_face_detected': 5, 'fullscreen_exit': 15, 'window_blur': 10, 'tab_switch': 10, 'gaze_away': 5,
     'pattern_multi_face_audio': 30, 'pattern_head_turned_talking': 20, 'suspect_object_confirmed': 28, 'multiple_faces': 20, 'identity_mismatch_sustained': 38, 'paste_attempt': 15}
weak = [(t, i * 10.0) for i, t in enumerate(['sustained_audio_detected', 'whisper_detected', 'no_face_detected', 'fullscreen_exit', 'window_blur', 'tab_switch', 'gaze_away'] * 40)]
sc, bd = R.replay(weak, W)
check("280 signaux faibles répétés (cas des 89 % du 17/11) : le score plafonne à 26 (6+6+10+4), très loin de 60", sc == 26, sc)
sc, _ = R.replay(weak + [('pattern_multi_face_audio', 5000.0)], W)
check("signaux faibles + UN signal fort isolé (motif combiné) : reste sous le seuil d'alerte (26 + 25 = 51)", sc == 51, sc)
sc, _ = R.replay(weak + [('pattern_multi_face_audio', 5000.0), ('multiple_faces', 5100.0)], W)
check("signaux faibles + deux familles de signaux forts concordantes : seuil d'alerte franchi", sc >= 60, sc)
sc, _ = R.replay([('identity_mismatch_sustained', 1.0), ('no_face_detected', 100.0), ('window_blur', 200.0)], W)
check("identité non confirmée (38) + un peu de bruit : alerte", sc >= 40 and sc <= 60, sc)
sc, _ = R.replay([('suspect_object_confirmed', 1.0), ('suspect_object_confirmed', 100.0)], W)
check("objet confirmé deux fois (cap 35) : visible (35) sans dépasser son plafond", sc == 35, sc)
sc, _ = R.replay([('paste_attempt', 1.0)] * 1 + [('paste_attempt', 100.0), ('paste_attempt', 200.0)], W)
check("presse-papiers répété : plafonné (15)", sc == 15, sc)
check("un événement inconnu compte dans « divers » (plafond 4)", R.replay([('evenement_inconnu', 1.0), ('evenement_inconnu', 50.0)], {}, {})[0] == 4)
check("jamais plus de 100", R.total_of({k: 999 for k in R.CATEGORIES}) == 100)
check("somme des plafonds faibles = 30 (< seuil 60)", sum(R.CATEGORIES[c][1] for c in ('audio', 'visage', 'focus', 'attention', 'divers')) == 30)
sc, bd = R.replay(weak + [('pattern_multi_face_audio', 5000.0)], W)
check("le détail liste les catégories par points décroissants, chacune ≤ son plafond", bd and bd[0]['points'] >= bd[-1]['points'] and all(b['points'] <= b['plafond'] for b in bd), bd)
check("le détail est lisible (libellé français)", all(b['libelle'] for b in bd))

print('\n== Route réelle (Redis) ==')
s = get_session(); tag = uuid.uuid4().hex[:6]; made = {'users': [], 'ex': [], 'sub': None}
c = A.app.test_client(); rc = _get_client()
check("Redis disponible pour ce test", rc is not None)
def mk(role, n):
    u = User(email=f'risk.{tag}.{n}@test.invalid', full_name=f'Risk Test {n}', role=role, is_active=True, email_verified=True, has_email=True, password_hash=generate_password_hash('x'))
    s.add(u); s.commit(); made['users'].append(u.id); return u
def H(u): return {'Authorization': 'Bearer ' + create_access_token(u.id, u.role.value, u.email)}
try:
    adm = mk(UserRole.ADMIN, 'adm'); st = mk(UserRole.STUDENT, 'e1')
    sub = Subject(title='risk test', content='1. Q ? [QCM]\nA. a\nB. b\n', rubric='Question 1 (1 pt) : A', creator_id=adm.id); s.add(sub); s.commit(); made['sub'] = sub.id
    now = datetime.utcnow()
    ex = OnlineExam(subject_id=sub.id, title='risk', duration_minutes=60, start_time=now - timedelta(minutes=5), end_time=now + timedelta(hours=1), status=ExamStatus.ACTIVE, created_by_id=adm.id)
    s.add(ex); s.commit(); made['ex'].append(ex.id)
    at = ExamAttempt(exam_id=ex.id, student_id=st.id, status=AttemptStatus.IN_PROGRESS, started_at=now, exam_seen_at=now, answers='{}'); s.add(at); s.commit()
    rc.delete(f'cei:risk:{at.id}')
    def post(et):
        cache_delete(f'cei:proctev:dedup:{at.id}:{et}')                 # on contourne l'anti-doublon pour tester le plafonnement
        return c.post(f'/api/exam_attempts/{at.id}/proctoring_event', json={'event_type': et, 'event_data': 'x'}, headers=H(st))
    last = None
    for _ in range(40):
        last = post('sustained_audio_detected'); last = post('no_face_detected'); last = post('window_blur')
    j = last.get_json() or {}
    check("120 événements faibles envoyés par la vraie route : score = 6 + 6 + 10 = 22", last.status_code == 200 and j.get('risk_score') == 22, (last.status_code, j))
    s.expire_all(); check("le score est bien enregistré sur la tentative", s.get(ExamAttempt, at.id).risk_score == 22)
    j = post('pattern_multi_face_audio').get_json() or {}
    check("un motif combiné s'ajoute (25 max) : 47, pas de saturation", j.get('risk_score') == 47, j)
    j = post('multiple_faces').get_json() or {}
    check("une deuxième famille forte : 22 + 25 + 20 = 67 (alerte)", j.get('risk_score') == 67, j)
    check("le score ne dépasse jamais 100 même avec tous les signaux", all((post(t).get_json() or {}).get('risk_score', 0) <= 100 for t in ('identity_mismatch_sustained', 'suspect_object_confirmed', 'paste_attempt', 'camera_blocked')))

    # détail visible du surveillant
    bdv = R.read_breakdowns(rc, [at.id]).get(at.id, [])
    check("le détail « pourquoi ce score » est disponible pour la liste de surveillance", any(b['categorie'] == 'motif' for b in bdv) and all(b['points'] <= b['plafond'] for b in bdv), bdv)
    r = c.get(f'/api/online_exams/{ex.id}/proctor_attempts', headers=H(adm)) if False else None

    print('\n== Notification : plus de rafale ==')
    sent = []
    import notif_bus
    orig = notif_bus.notify_exam
    notif_bus.notify_exam = lambda *a, **k: sent.append(a[1] if len(a) > 1 else k)
    try:
        rc.delete(f'cei:riskalert:{at.id}')
        for _ in range(6): post('suspect_object_confirmed')
        hr = [x for x in sent if x == 'high_risk']
        check("6 événements alors que le score est déjà ≥ 75 : UNE seule notification (avant : une par événement)", len(hr) == 1, len(hr))
    finally:
        notif_bus.notify_exam = orig

    print('\n== Compteurs d\'alertes persistants ==')
    os.environ['AGENT_SECRET'] = os.environ.get('AGENT_SECRET', 'secret-test')
    import proctoring_routes as P
    P._agent_auth = lambda: True
    for k in (f'cei:agent:alerts:{ex.id}:sent', f'cei:agent:alerts:{ex.id}:students'): rc.delete(k)
    for who in (at.id, at.id, at.id + 1):
        c.post('/api/agent/alerts', json={'exam_id': ex.id, 'attempt_id': who, 'student_name': 'X', 'risk_score': 70})
    check("3 alertes pour 2 étudiants : 3 envoyées, 2 étudiants distincts alertés", int(rc.get(f'cei:agent:alerts:{ex.id}:sent') or 0) == 3 and rc.scard(f'cei:agent:alerts:{ex.id}:students') == 2)
    for k in (f'cei:agent:alerts:{ex.id}:sent', f'cei:agent:alerts:{ex.id}:students'): rc.delete(k)
finally:
    s.rollback()
    try:
        ids = made['ex']
        if ids:
            att_ids = [a.id for a in s.query(ExamAttempt).filter(ExamAttempt.exam_id.in_(ids))]
            for i in att_ids:
                try: rc.delete(f'cei:risk:{i}'); rc.delete(f'cei:riskalert:{i}')
                except Exception: pass
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
print(f"\n{len(RESULTS) - len(bad)}/{len(RESULTS)} vérifications réussies" + (f" — ÉCHECS : {bad}" if bad else ''))
sys.exit(1 if bad else 0)
