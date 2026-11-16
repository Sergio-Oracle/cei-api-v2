#!/usr/bin/env python
"""Messagerie du personnel d'un examen + appel du surveillant vers l'enseignant / responsable (14-16/11/2026).
    cd cei-api-v2 && .venv-base/bin/python scripts/test_messagerie_personnel.py   (crée puis supprime ses propres données)"""
import sys, uuid
sys.path.insert(0, '.')
from app import app
import proctoring_routes as PR
from models import *
from routes.exams import get_session
from auth_paseto import create_access_token
from datetime import datetime, timedelta
import notif_bus

s = get_session(); sfx = uuid.uuid4().hex[:6]; now = datetime.utcnow(); c = app.test_client(use_cookies=False); RES = []; ids = {'users': [], 'ex': [], 'sub': []}
def chk(n, ok, d=''):
    RES.append(bool(ok)); print(('  ✔ ' if ok else '  ✘ ') + n + (f'  — {str(d)[:200]}' if d and not ok else ''))
def mk(role, name):
    u = User(email=f'zz_{name}_{sfx}@test.local', full_name=f'ZZ {name}', role=role, is_active=True, password_hash='x'); s.add(u); s.commit(); ids['users'].append(u.id); return u
def H(u): return {'Authorization': 'Bearer ' + create_access_token(u.id, u.role.value, u.email)}
real_cfg, real_notify = PR.get_livekit_config, notif_bus.notify_user
PR.get_livekit_config = lambda: {'url': 'wss://lk.test', 'api_url': 'https://lk.test', 'api_key': 'k', 'api_secret': 's' * 40}
sent = []
notif_bus.notify_user = lambda uid, ev, title, msg, priority='default', tags=None, extra=None: sent.append((uid, ev, title, msg, extra))
try:
    adm = s.query(User).filter(User.role == UserRole.ADMIN).first()
    prof, prof2 = mk(UserRole.PROFESSOR, 'prof'), mk(UserRole.PROFESSOR, 'prof2')
    resp = mk(UserRole.RESPONSABLE_EXAMEN, 'resp')
    sv, sv2, sv3 = mk(UserRole.SURVEILLANT, 'sv'), mk(UserRole.SURVEILLANT, 'sv2'), mk(UserRole.SURVEILLANT, 'sv3')
    stu = mk(UserRole.STUDENT, 'stu')
    sub = Subject(title='ZZ chat', content='Question 1. a ? [QCM]\nA. a\nB. b\n', rubric='', creator_id=prof.id); s.add(sub); s.commit(); ids['sub'].append(sub.id)
    mkex = lambda t: OnlineExam(subject_id=sub.id, title=t, duration_minutes=60, start_time=now, end_time=now + timedelta(hours=1), status=ExamStatus.ACTIVE, created_by_id=prof.id)
    ex, ex2 = mkex('ZZ chat 1'), mkex('ZZ chat 2'); s.add_all([ex, ex2]); s.commit(); ids['ex'] += [ex.id, ex2.id]
    s.add_all([ExamProctor(exam_id=ex.id, proctor_id=sv.id, assigned_by_id=prof.id), ExamProctor(exam_id=ex.id, proctor_id=sv2.id, assigned_by_id=prof.id),
               ExamProctor(exam_id=ex2.id, proctor_id=sv3.id, assigned_by_id=prof.id)]); s.commit()
    url = f'/api/online_exams/{ex.id}/staff_chat'

    print('== Qui peut écrire / lire ==')
    chk("le responsable, l'enseignant créateur, l'administrateur et les surveillants affectés y accèdent",
        all(c.get(url, headers=H(u)).status_code == 200 for u in (resp, prof, sv, sv2)) and c.get(url, headers=H(adm)).status_code == 200)
    chk("un étudiant : refusé", c.get(url, headers=H(stu)).status_code == 403)
    chk("un surveillant d'un AUTRE examen : refusé", c.get(url, headers=H(sv3)).status_code == 403)
    chk("un enseignant étranger à l'examen : refusé", c.get(url, headers=H(prof2)).status_code == 403)

    print('\n== Messages ==')
    r = c.post(url, json={'body': 'Bonjour à toute l\'équipe'}, headers=H(resp)); j = r.get_json() or {}
    chk("le responsable écrit à toute l'équipe", r.status_code == 200 and j.get('message', {}).get('recipient_id') is None, j)
    got = {u.id: c.get(url, headers=H(u)).get_json() for u in (sv, sv2, prof, resp)}
    chk("tous les membres le voient", all(any('toute l' in m['body'] for m in g['messages']) for g in got.values()))
    chk("les administrateurs qui n'ont PAS participé ne sont ni listés ni notifiés (pas de bruit)", adm.id not in {p['id'] for p in got[sv.id]['participants']} and adm.id not in {x[0] for x in sent if x[1] == 'staff_message'}, got[sv.id]['participants'])
    chk("la liste des membres est donnée à l'ouverture (nom + rôle)", {p['role'] for p in got[sv.id]['participants']} >= {'surveillant', 'responsable_examen', 'professor'}, got[sv.id]['participants'])
    chk("les notifications partent vers l'équipe, pas vers l'expéditeur", {x[0] for x in sent if x[1] == 'staff_message'} >= {sv.id, sv2.id, prof.id} and resp.id not in {x[0] for x in sent if x[1] == 'staff_message'})
    sent.clear()
    r = c.post(url, json={'body': 'Surveillante 1, pouvez-vous vérifier la salle ?', 'to': sv.id}, headers=H(prof))
    chk("l'enseignant écrit à UN surveillant", r.status_code == 200 and r.get_json()['message']['recipient_id'] == sv.id, r.get_json())
    seen = lambda u: [m['body'] for m in c.get(url, headers=H(u)).get_json()['messages']]
    chk("seul ce surveillant (et l'expéditeur) voit le message privé", any('vérifier la salle' in b for b in seen(sv)) and any('vérifier la salle' in b for b in seen(prof)) and not any('vérifier la salle' in b for b in seen(sv2)) and not any('vérifier la salle' in b for b in seen(resp)))
    chk("seul le destinataire est notifié du message privé", [x[0] for x in sent if x[1] == 'staff_message'] == [sv.id] and sent[0][4]['direct'] is True, sent)
    r = c.post(url, json={'body': 'Bien reçu, je vérifie'}, headers=H(sv))
    chk("le surveillant RÉPOND (échange dans les deux sens)", r.status_code == 200)
    last = c.get(url, headers=H(sv)).get_json()['messages'][-1]['id']
    chk("lecture incrémentale : since_id ne renvoie que les nouveaux", c.get(url + f'?since_id={last}', headers=H(prof)).get_json()['messages'] == [], c.get(url + f'?since_id={last}', headers=H(prof)).get_json())
    c.post(url, json={'body': 'nouveau'}, headers=H(resp))
    chk("… et renvoie le nouveau message", [m['body'] for m in c.get(url + f'?since_id={last}', headers=H(sv)).get_json()['messages']] == ['nouveau'])

    print('\n== Garde-fous ==')
    chk("message vide refusé", c.post(url, json={'body': '   '}, headers=H(resp)).status_code == 400)
    chk("message trop long refusé", c.post(url, json={'body': 'x' * 1001}, headers=H(resp)).status_code == 400)
    chk("destinataire hors équipe refusé", c.post(url, json={'body': 'a', 'to': prof2.id}, headers=H(resp)).status_code == 400)
    chk("on ne s'écrit pas à soi-même", c.post(url, json={'body': 'a', 'to': resp.id}, headers=H(resp)).status_code == 400)
    chk("un étudiant ne peut pas écrire", c.post(url, json={'body': 'a'}, headers=H(stu)).status_code == 403)
    chk("le HTML est conservé comme TEXTE (affiché échappé par la page)", c.post(url, json={'body': '<b>x</b>'}, headers=H(resp)).get_json()['message']['body'] == '<b>x</b>')

    print('\n== Appel du surveillant vers le personnel (sens inverse) ==')
    sent.clear()
    call = lambda staff, who: c.post(f'/api/proctor_call/{staff.id}/request', headers=H(who))
    r = call(resp, sv); chk("le surveillant appelle le responsable d'examen", r.status_code == 200 and r.get_json().get('room') == f'supcall-{sv.id}', r.get_json())
    ev = [x for x in sent if x[1] == 'proctor_call_request']
    chk("le responsable reçoit « Appel entrant » avec le nom du surveillant et son identifiant", ev and ev[-1][0] == resp.id and ev[-1][4]['proctor_id'] == sv.id and 'ZZ sv' in ev[-1][3], ev)
    chk("le surveillant appelle l'enseignant de SON examen", call(prof, sv).status_code == 200)
    chk("le surveillant appelle l'administrateur", call(adm, sv).status_code == 200)
    chk("un surveillant ne peut pas appeler un enseignant étranger à ses examens", call(prof2, sv).status_code == 403)
    chk("ni un autre surveillant, ni un étudiant", call(sv2, sv).status_code == 403 and call(stu, sv).status_code == 403)
    chk("seuls les surveillants peuvent utiliser cette route", call(sv, resp).status_code == 403 and call(sv, prof).status_code == 403)
    tok = lambda staff, as_who: c.get(f'/api/superviseur/proctor_call/{sv.id}/token', headers=H(as_who))
    r1, r2 = tok(sv, resp), tok(sv, sv)
    chk("le responsable rejoint la salle du surveillant (jeton) ; le surveillant aussi ; même salle", r1.status_code == 200 and r2.status_code == 200 and r1.get_json()['room'] == r2.get_json()['room'] == f'supcall-{sv.id}')
    chk("l'enseignant de l'examen rejoint la salle (jeton)", tok(sv, prof).status_code == 200)
    chk("l'enseignant étranger n'y entre pas", tok(sv, prof2).status_code == 403)
finally:
    PR.get_livekit_config, notif_bus.notify_user = real_cfg, real_notify
    try:
        s.rollback()
        s.query(ExamStaffMessage).filter(ExamStaffMessage.exam_id.in_(ids['ex'])).delete(synchronize_session=False)
        s.query(ExamProctor).filter(ExamProctor.exam_id.in_(ids['ex'])).delete(synchronize_session=False)
        s.query(OnlineExam).filter(OnlineExam.id.in_(ids['ex'])).delete(synchronize_session=False)
        s.query(Subject).filter(Subject.id.in_(ids['sub'])).delete(synchronize_session=False)
        s.query(User).filter(User.id.in_(ids['users'])).delete(synchronize_session=False); s.commit()
    except Exception as e:
        print('nettoyage :', e)
    s.close()
print(f"\n{sum(RES)}/{len(RES)} vérifications réussies"); sys.exit(0 if all(RES) else 1)
