#!/usr/bin/env python
"""Test réel CEI <-> Moodle (PRÉPRODUCTION SEULEMENT) : calendrier d'examen et retour des notes.

    cd cei-api-v2 && .venv-base/bin/python scripts/test_moodle_notes_calendrier.py

Utilise le cours de test « TEST JOKKO » de Moodle préprod (EC du même code) et les comptes de test
testuseretudiant@ / testuserpersonnel@unchk.edu.sn déjà inscrits. Écrit réellement dans Moodle (événement du
calendrier, colonne de notes) puis nettoie : l'événement est retiré avec l'examen, la colonne de notes est supprimée
par l'API Moodle (AGS). Refuse de s'exécuter ailleurs que sur la plateforme de test.
"""
import json, os, sys, time
from datetime import datetime, timedelta, timezone
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app
import cache
from models import (get_session, User, EC, MoodleInstance, MoodleExamEvent, LtiLineItem, OnlineExam, Subject,
                    ExamAttempt, ExamActivityLog, ProctorAssignment, StaffAuditLog)
from services import moodle_sync, lti
from auth_paseto import create_access_token

BASE = os.getenv('CEI_LOCAL_API', 'http://127.0.0.1:8091')
COURSE_CODE, COURSE_ID = 'TEST JOKKO', 95
STUDENT, TEACHER, STUDENT_MOODLE_ID = 'testuseretudiant@unchk.edu.sn', 'testuserpersonnel@unchk.edu.sn', 8113

RESULTS = []
def check(nom, ok, detail=''):
    RESULTS.append((nom, bool(ok)))
    print(('  ✔ ' if ok else '  ✘ ') + nom + (f'  — {detail}' if detail and not ok else ''))

s = get_session()
inst = s.query(MoodleInstance).first()
if not inst or 'dev-promo13seja' not in inst.base_url:
    sys.exit("Refus : ce test écrit dans Moodle et ne doit tourner que sur la plateforme de test de la préproduction.")
cl = moodle_sync.client_for(inst)
prof = s.query(User).filter_by(email=TEACHER).first(); stud = s.query(User).filter_by(email=STUDENT).first()
ec = s.query(EC).filter_by(code=COURSE_CODE).first()
assert prof and stud and ec, "comptes ou EC de test introuvables"
H = lambda u, role: {'Authorization': 'Bearer ' + create_access_token(u.id, role, u.email), 'Content-Type': 'application/json'}
hp, hs = H(prof, 'professor'), H(stud, 'student')
Z = lambda dt: dt.strftime('%Y-%m-%dT%H:%M:%S.000Z')

def call(m, path, h, body=None):
    r = requests.request(m, BASE + path, headers=h, json=body if body is not None else {}, timeout=60)
    try: return r.status_code, r.json()
    except Exception: return r.status_code, r.text[:200]

def events(title):
    now = int(time.time())
    r = cl.call('core_calendar_get_calendar_events', {'events': {'courseids': [COURSE_ID]}, 'options': {
        'userevents': 0, 'siteevents': 0, 'timestart': now - 86400, 'timeend': now + 86400 * 30}})
    return [e for e in r.get('events', []) if title in e['name']]

def moodle_grade(title):
    g = cl.call('gradereport_user_get_grade_items', {'courseid': COURSE_ID, 'userid': STUDENT_MOODLE_ID})
    return [i.get('graderaw') for ug in g.get('usergrades', []) for i in ug.get('gradeitems', []) if i.get('itemname') and title in i['itemname']]

def wait(cond, secs=40):
    t = time.time()
    while time.time() - t < secs:
        v = cond()
        if v: return v
        time.sleep(2)
    return cond()

title = f"Test notes et calendrier {int(time.time()) % 100000}"
ids = {}
try:
    print('\n== Calendrier : création ==')
    st, j = call('POST', '/api/subjects', hp, {'title': title, 'content': 'Question 1 — Capitale du Sénégal ?\nA) Dakar\nB) Thiès',
                                                'rubric': '=== BARÈME DE NOTATION ===\nQuestion 1 (20 pts) :\n  Bonne réponse : A)\n\nTOTAL : 20 pts', 'ec_id': ec.id})
    check("sujet créé par l'enseignant de test", st == 201); ids['sub'] = j['subject']['id']
    now = datetime.utcnow()
    st, j = call('POST', '/api/online_exams', hp, {'subject_id': ids['sub'], 'title': title, 'start_time': Z(now - timedelta(minutes=2)), 'end_time': Z(now + timedelta(minutes=90))})
    check("examen créé", st in (200, 201)); eid = ids['exam'] = (j.get('exam') or j)['id']
    ev = wait(lambda: events(title))
    check("l'événement apparaît dans le calendrier du cours Moodle", len(ev) == 1, ev)
    first_ts = ev[0]['timestart'] if ev else None

    print('\n== Calendrier : reprogrammation ==')
    call('PUT', f'/api/admin/online_exams/{eid}', hp, {'start_time': Z(now + timedelta(minutes=28)), 'end_time': Z(now + timedelta(minutes=118))})
    ev = wait(lambda: [e for e in events(title) if first_ts and e['timestart'] > first_ts + 1500])
    check("l'événement est déplacé (un seul, à la nouvelle heure)", len(ev) == 1 and len(events(title)) == 1, events(title))
    call('PUT', f'/api/admin/online_exams/{eid}', hp, {'start_time': Z(now - timedelta(minutes=2)), 'end_time': Z(now + timedelta(minutes=90))})
    wait(lambda: [e for e in events(title) if first_ts and abs(e['timestart'] - first_ts) < 60])
    check("remis à l'heure d'origine, toujours un seul événement", len(events(title)) == 1)

    print('\n== Notes : passage, correction, publication ==')
    check("activation", call('POST', f'/api/online_exams/{eid}/activate', hp)[0] == 200)
    cache.cache_set(f'cei:biometric:verify:{stud.id}', '1', ttl=600)
    st, j = call('POST', f'/api/online_exams/{eid}/start', hs, {}); aid = ids['att'] = (j.get('attempt') or {}).get('id')
    check("l'étudiant de test commence l'examen", st in (200, 201) and aid)
    call('POST', f'/api/exam_attempts/{aid}/submit', hs, {'answers': json.dumps({'pq_1': 'A'})})
    check("note manuelle 16/20", call('PUT', f'/api/exam_attempts/{aid}/manual-grade', hp, {'score': 16, 'feedback': 'test'})[0] == 200)
    check("avant publication : rien dans le carnet Moodle", moodle_grade(title) == [])
    check("publication des résultats", call('PUT', f'/api/online_exams/{eid}/publish-results', hp, {'published': True})[0] == 200)
    g = wait(lambda: moodle_grade(title)); check("la note 16 arrive dans le carnet Moodle de l'étudiant", g == [16], g)

    print('\n== Notes : correction APRÈS publication (réclamation, correction manuelle) ==')
    call('PUT', f'/api/exam_attempts/{aid}/manual-grade', hp, {'score': 18, 'feedback': 'corrigée après publication'})
    g = wait(lambda: moodle_grade(title) == [18] and [18], 60); check("la note corrigée (18) suit dans Moodle", g == [18], moodle_grade(title))
    call('PUT', f'/api/exam_attempts/{aid}/manual-grade', hp, {'score': 12, 'feedback': 'a'}); call('PUT', f'/api/exam_attempts/{aid}/manual-grade', hp, {'score': 13, 'feedback': 'b'})
    g = wait(lambda: moodle_grade(title) == [13] and [13], 60); check("deux corrections rapprochées : la dernière gagne (un seul dépôt)", g == [13], moodle_grade(title))
finally:
    print('\n== Nettoyage ==')
    try:
        s.expire_all()
        li = s.query(LtiLineItem).filter_by(exam_id=ids.get('exam')).first() if ids.get('exam') else None
        if li:
            try:
                lti._ags(inst, 'DELETE', li.lineitem_url, 'application/vnd.ims.lis.v2.lineitem+json')
                print('  colonne de notes supprimée dans Moodle')
            except Exception as e:
                print('  colonne de notes non supprimée :', str(e)[:120])
            s.delete(li); s.commit()
        if ids.get('exam'):
            st, _ = call('DELETE', f"/api/online_exams/{ids['exam']}", hp); print('  suppression de l\'examen :', st)
            gone = wait(lambda: not events(title), 40)
            check("l'événement disparaît de Moodle avec l'examen", gone, events(title))
            time.sleep(2)
            s.query(MoodleExamEvent).filter_by(exam_id=ids['exam']).delete(synchronize_session=False)
        if ids.get('sub'):
            s.query(Subject).filter_by(id=ids['sub']).delete(synchronize_session=False)
        s.query(StaffAuditLog).filter(StaffAuditLog.user_email.like('%@test.invalid')).delete(synchronize_session=False)
        s.commit()
        print('  moodle grade résiduel :', moodle_grade(title), '| événements résiduels :', len(events(title)))
    except Exception as e:
        s.rollback(); print('Nettoyage incomplet :', e)

ok = sum(1 for _, r in RESULTS if r)
print(f'\n{ok}/{len(RESULTS)} points réussis')
for n, r in RESULTS:
    if not r: print('  ÉCHEC :', n)
sys.exit(0 if ok == len(RESULTS) else 1)
