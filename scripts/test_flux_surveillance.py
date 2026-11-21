#!/usr/bin/env python
"""Flux de surveillance — messages de l'étudiant et alertes de l'agent (21/11/2026).
    cd cei-api-v2 && .venv-base/bin/python scripts/test_flux_surveillance.py   (crée puis supprime ses propres données)

Défauts corrigés :
 - message de l'étudiant renvoyé automatiquement après une coupure : un même identifiant n'est jamais enregistré deux fois ;
 - le personnel voyait « [DEMANDE_APPEL] … » / « [FIN_APPEL] … » tels quels, et une demande d'appel vocal ne déclenchait aucune alerte ;
 - un message validé juste après une relève (horodaté AVANT elle) n'était jamais renvoyé au surveillant : recouvrement de 15 s ;
 - alertes de l'agent : l'état « lue » (par étudiant, jamais effacé) rendait invisibles toutes les alertes suivantes d'un étudiant déjà lu ;
   et le serveur renvoyait les 50 plus ANCIENNES alertes au lieu des 50 plus récentes."""
import sys, os, uuid, json
sys.path.insert(0, '.')
os.environ['AGENT_SECRET_KEY'] = 'secret-de-test-' + uuid.uuid4().hex
from app import app
import proctoring_routes as PR
from models import *
from routes.exams import get_session
from auth_paseto import create_access_token
from datetime import datetime, timedelta

s = get_session(); sfx = uuid.uuid4().hex[:6]; now = datetime.utcnow(); c = app.test_client(use_cookies=False); RES = []; ids = {'users': [], 'ex': [], 'sub': [], 'att': []}
def chk(n, ok, d=''):
    RES.append(bool(ok)); print(('  ✔ ' if ok else '  ✘ ') + n + (f'  — {str(d)[:200]}' if d and not ok else ''))
def mk(role, name):
    u = User(email=f'zz_{name}_{sfx}@test.local', full_name=f'ZZ {name}', role=role, is_active=True, password_hash='x'); s.add(u); s.commit(); ids['users'].append(u.id); return u
def H(u): return {'Authorization': 'Bearer ' + create_access_token(u.id, u.role.value, u.email)}
AG = {'X-Agent-Secret': os.environ['AGENT_SECRET_KEY']}
try:
    prof, stu, adm = mk(UserRole.PROFESSOR, 'prof'), mk(UserRole.STUDENT, 'stu'), mk(UserRole.ADMIN, 'adm')
    sub = Subject(title='ZZ flux', content='Question 1. a ? [QCM]\nA. a\nB. b\n', rubric='', creator_id=prof.id); s.add(sub); s.commit(); ids['sub'].append(sub.id)
    ex = OnlineExam(subject_id=sub.id, title='ZZ flux', duration_minutes=60, start_time=now, end_time=now + timedelta(hours=1), status=ExamStatus.ACTIVE, created_by_id=prof.id); s.add(ex); s.commit(); ids['ex'].append(ex.id)
    at = ExamAttempt(exam_id=ex.id, student_id=stu.id, status=AttemptStatus.IN_PROGRESS, started_at=now); s.add(at); s.commit(); ids['att'].append(at.id)
    post = lambda **b: c.post(f'/api/exam_attempts/{at.id}/student_message', headers=H(stu), json=b)
    def logs(t=('student_message',)):
        s.expire_all(); return s.query(ExamActivityLog).filter(ExamActivityLog.attempt_id == at.id, ExamActivityLog.event_type.in_(t)).all()
    get_msgs = lambda since=None: (c.get(f'/api/online_exams/{ex.id}/student_messages' + (f'?since={since}' if since else ''), headers=H(adm)).get_json() or {}).get('messages', [])

    print('== Message de l\'étudiant : renvoi automatique sans doublon ==')
    e1 = 's' + uuid.uuid4().hex[:16]
    r = post(message='Bonjour', eid=e1); chk("1er envoi accepté", r.status_code == 200 and not (r.get_json() or {}).get('duplicate'), r.get_json())
    r = post(message='Bonjour', eid=e1); chk("renvoi automatique avec le même identifiant : accepté, marqué doublon", r.status_code == 200 and (r.get_json() or {}).get('duplicate') is True, r.get_json())
    chk("… UN SEUL enregistrement", len(logs()) == 1, len(logs()))
    post(message='Bonjour', eid='s' + uuid.uuid4().hex[:16]); chk("un autre identifiant avec le même texte = vrai second message", len(logs()) == 2)
    post(message='Ancien client'); post(message='Ancien client'); chk("sans identifiant (ancien client) : comportement d'avant conservé", len(logs()) == 4)
    chk("message vide toujours refusé", post(message='   ', eid='s' + uuid.uuid4().hex[:16]).status_code == 400)

    print('\n== Ce que voit le personnel ==')
    post(message='[DEMANDE_APPEL] Je souhaite poser une question verbalement au surveillant.', eid='s' + uuid.uuid4().hex[:16])
    post(message='[FIN_APPEL] Appel privé terminé.', eid='s' + uuid.uuid4().hex[:16])
    ms = get_msgs()
    voice = [m for m in ms if m['type'] == 'call_request']; ended = [m for m in ms if m['type'] == 'call_ended']
    chk("« [DEMANDE_APPEL] … » devient une demande d'appel vocal (type call_request, motif voice)", len(voice) == 1 and voice[0]['reason'] == 'voice', voice)
    chk("… avec un texte lisible, sans crochets", voice and '[' not in voice[0]['message'] and 'appel vocal' in voice[0]['message'].lower(), voice)
    chk("« [FIN_APPEL] … » devient une information de fin d'appel (type call_ended)", len(ended) == 1 and '[' not in ended[0]['message'], ended)
    chk("aucun message ne montre encore un marqueur entre crochets", not [m for m in ms if m['message'].lstrip().startswith('[')], [m['message'] for m in ms])
    chk("les messages ordinaires restent inchangés (type message)", [m for m in ms if m['message'] == 'Bonjour' and m['type'] == 'message'])
    s.add(ExamActivityLog(attempt_id=at.id, event_type='student_call_request', event_data=json.dumps({'message': 'reprise', 'student_name': 'ZZ stu'}))); s.commit()
    cr = [m for m in get_msgs() if m['type'] == 'call_request' and m.get('reason') == 'resume']
    chk("la demande d'appel de REPRISE après déconnexion garde son motif (resume)", len(cr) == 1, cr)

    print('\n== Relève du surveillant : plus de message perdu ==')
    since = (datetime.utcnow()).isoformat() + 'Z'
    for secs, txt in ((10, 'validé juste après la relève'), (40, 'très ancien')):
        s.add(ExamActivityLog(attempt_id=at.id, event_type='student_message', timestamp=datetime.utcnow() - timedelta(seconds=secs), event_data=json.dumps({'message': txt, 'student_name': 'ZZ stu'}))); s.commit()
    got = [m['message'] for m in get_msgs(since)]
    chk("un message horodaté 10 s AVANT la relève est quand même renvoyé (recouvrement de 15 s)", 'validé juste après la relève' in got, got)
    chk("… mais pas un message de 40 s", 'très ancien' not in got, got)

    print('\n== Alertes de l\'agent ==')
    import redis as _r
    def purge():
        rr = PR._redis_alerts()
        for it in rr.lrange(PR._ALERTS_KEY, 0, -1):
            try:
                if json.loads(it).get('attempt_id') == at.id: rr.lrem(PR._ALERTS_KEY, 0, it)
            except Exception: pass
        rr.srem(PR._READ_KEY, str(at.id)); rr.close()
    purge()
    alert = lambda i, ts: dict(exam_id=ex.id, exam_title='ZZ', attempt_id=at.id, student_name='ZZ stu', risk_score=40 + i, level='high', alert_type='test', timestamp=ts)
    t0 = datetime.utcnow()
    r = c.post('/api/agent/alerts', headers=AG, json=alert(0, t0.isoformat())); chk("l'agent pousse une alerte", r.status_code == 200, r.get_json())
    al = lambda: (c.get(f'/api/agent/alerts?exam_id={ex.id}', headers=H(adm)).get_json() or {})
    chk("elle est visible", al().get('total_unread', 0) >= 1, al())
    r = c.post('/api/agent/alerts/read', headers=H(adm), json={'attempt_ids': [at.id]}); chk("le surveillant la marque « lue »", r.status_code == 200)
    chk("… elle disparaît", al().get('total_unread', 0) == 0, al())
    c.post('/api/agent/alerts', headers=AG, json=alert(1, (t0 + timedelta(minutes=10)).isoformat()))
    chk("une NOUVELLE alerte du même étudiant est de nouveau visible (avant : jamais, état « lue » permanent)", al().get('total_unread', 0) >= 1, al())
    purge()
    codes = [c.post('/api/agent/alerts', headers=AG, json=alert(i, (t0 + timedelta(minutes=i + 1)).isoformat())).status_code for i in range(55)]
    chk("55 alertes poussées sans refus", set(codes) == {200}, codes)
    res = al(); tss = [a['timestamp'].rstrip('Z') for a in res.get('alerts', [])]      # le serveur ajoute « Z » aux dates en sortie
    newest = (t0 + timedelta(minutes=55)).isoformat(); oldest = (t0 + timedelta(minutes=1)).isoformat()
    chk("55 alertes non lues : 50 renvoyées", len(tss) == 50, len(tss))
    chk("… ce sont les plus RÉCENTES (la dernière y est, la toute première non)", newest in tss and oldest not in tss, (min(tss) if tss else None, max(tss) if tss else None))
    chk("… et le total non lu reste exact", res.get('total_unread') == 55, res.get('total_unread'))
    chk("push sans le secret de l'agent : refusé", c.post('/api/agent/alerts', json=alert(0, t0.isoformat())).status_code == 403)
finally:
    try:
        purge()
    except Exception: pass
    try:
        s.rollback()
        s.query(ExamActivityLog).filter(ExamActivityLog.attempt_id.in_(ids['att'])).delete(synchronize_session=False)
        s.query(ExamAttempt).filter(ExamAttempt.id.in_(ids['att'])).delete(synchronize_session=False)
        s.query(OnlineExam).filter(OnlineExam.id.in_(ids['ex'])).delete(synchronize_session=False)
        s.query(Subject).filter(Subject.id.in_(ids['sub'])).delete(synchronize_session=False)
        s.query(User).filter(User.id.in_(ids['users'])).delete(synchronize_session=False); s.commit()
    except Exception as e:
        print('nettoyage :', e)
    s.close()
print(f"\n{sum(RES)}/{len(RES)} vérifications réussies"); sys.exit(0 if all(RES) else 1)
