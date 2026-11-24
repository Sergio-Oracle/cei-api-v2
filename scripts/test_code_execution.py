#!/usr/bin/env python
"""Questions de code avec tests automatiques, EXÉCUTÉS DANS LE NAVIGATEUR (24/11/2026) — partie serveur.
    cd cei-api-v2 && .venv-base/bin/python scripts/test_code_execution.py     (crée puis supprime ses propres données)
Le serveur n'exécute jamais le code d'un étudiant : il lit le barème, donne à l'étudiant les seuls tests VISIBLES, reçoit de l'enseignant les SORTIES obtenues
dans son navigateur, RECALCULE le verdict de chaque test, enregistre le résultat et met la note à jour. L'exécution réelle (Python / JavaScript dans une enceinte
isolée) est vérifiée en navigateur : scénario e2e 11 et cei-next/scripts/test_code_runner.py."""
import sys, os, uuid, json
sys.path.insert(0, '.')
from app import app
from models import *
from routes.exams import get_session
import routes.exams as R
from auth_paseto import create_access_token
from services import question_types as QT, code_results as CR
from datetime import datetime, timedelta

RES = []
def chk(n, ok, d=''):
    RES.append(bool(ok)); print(('  ✔ ' if ok else '  ✘ ') + n + (f'  — {str(d)[:240]}' if d and not ok else ''))

BLOCK = """Question 1 (4 pts) : Programme qui additionne deux entiers.
Langage : Python
Test : entrée « 3 4 » ; sortie « 7 »
Test : entrée « 10 20 » ; sortie « 30 »
Test caché : entrée « -5 5 » ; sortie « 0 »
Test caché : entrée « 100 1 » ; sortie « 101 »
"""
key = QT.extract_keys_from_block(BLOCK)['code']

print('== Lecture du barème ==')
chk("langage et 4 tests lus (2 visibles, 2 cachés)", key['lang'] == 'python' and len(key['tests']) == 4 and [t['hidden'] for t in key['tests']] == [False, False, True, True], key)
chk("entrées et sorties sans les guillemets français ni leurs espaces", key['tests'][0]['input'] == '3 4' and key['tests'][0]['expected'] == '7')
chk("notation proportionnelle par défaut ; « Notation : tout ou rien » honorée", key['all_or_nothing'] is False and QT.extract_keys_from_block(BLOCK + 'Notation : tout ou rien\n')['code']['all_or_nothing'] is True)
chk("test d'appel (fonction) et séquences \\n lues", QT.extract_keys_from_block("Langage : js\nTest : appel « console.log(f(2)) » ; sortie « 4 »\nTest : entrée « a\\nb » ; sortie « x »")['code']['tests'][1]['input'] == 'a\nb')
chk("alias de langage : py, js, c++ reconnus", [QT.norm_lang(x) for x in ('py', 'JS', 'C++', 'Python3', 'cobol')] == ['python', 'javascript', 'cpp', 'python', None])
chk("sans tests ou sans langage : aucune clé (la question reste à l'IA)", QT.extract_keys_from_block("Langage : python") == {} and 'code' not in QT.extract_keys_from_block("Test : entrée 1 ; sortie 2"))
chk("comparaison tolérante : espaces de fin de ligne et lignes vides finales ignorés", QT.normalize_output("7  \r\n\n\n") == '7' and QT.normalize_output('a\nb') != QT.normalize_output('a b'))
chk("seuls Python et JavaScript sont exécutables dans le navigateur", QT.CODE_RUNNABLE == ('python', 'javascript'))

print('\n== Jugement des sorties (verdict recalculé par le serveur) ==')
def outs(vals, status='ok'):
    return [{'n': t['n'], 'status': status, 'got': v} for t, v in zip(key['tests'], vals)]
j = QT.judge_code(key, outs(['7', '30', '0', '101']))
chk("4 sorties justes → 1,0", j and j[0] == 1.0 and '4/4' in j[1], j and j[1])
j = QT.judge_code(key, outs(['7\n', '30  ', '0\n\n', '101\r\n']))
chk("espaces et lignes vides de fin ignorés → 1,0", j and j[0] == 1.0)
j = QT.judge_code(key, outs(['7', '30', 'X', 'Y']))
chk("2 sur 4 → 0,5 (proportionnelle) et tests en échec nommés", j and j[0] == 0.5 and 'test 3, 4' in j[1], j and j[1])
j = QT.judge_code({**key, 'all_or_nothing': True}, outs(['7', '30', 'X', '101']))
chk("« tout ou rien » : un seul échec → 0", j and j[0] == 0.0)
j = QT.judge_code(key, [{'n': t['n'], 'status': 'timeout', 'got': t['expected']} for t in key['tests']])
chk("délai dépassé → échec MÊME si la sortie annoncée est la bonne (le statut compte)", j and j[0] == 0.0)
j = QT.judge_code(key, [{'n': t['n'], 'status': 'ok', 'ok': True, 'got': 'faux'} for t in key['tests']])
chk("un navigateur qui annonce « ok: true » sans la bonne sortie n'est pas cru : 0", j and j[0] == 0.0)
chk("un test manquant → None (jamais de note sur une exécution incomplète)", QT.judge_code(key, outs(['7', '30', '0', '101'])[:3]) is None)
chk("un test « indisponible » (exécution impossible) → None, ni réussi ni échoué", QT.judge_code(key, outs(['7', '30', '0', '101'])[:3] + [{'n': 4, 'status': 'unavailable', 'got': ''}]) is None)
chk("réponse vide → 0 sans résultat nécessaire", QT.grade_code({'code': key}, '  \n') == (0.0, 'aucune réponse'))
chk("sans résultat enregistré → None (reste à l'IA / au professeur)", QT.grade_code({'code': key}, 'print(1)') is None)
sha = QT.code_sha('print(1)')
chk("résultat enregistré pour CETTE réponse → utilisé", QT.grade_code({'code': key}, 'print(1)', {'sha': sha, 'fraction': 0.5, 'summary': '2/4'}) == (0.5, '2/4'))
chk("résultat d'une AUTRE réponse (empreinte différente) → ignoré", QT.grade_code({'code': key}, 'print(2)', {'sha': sha, 'fraction': 0.5, 'summary': '2/4'}) is None)

print('\n== Note de la copie (_deterministic_grade) ==')
CONTENT = "1. Écrire un programme qui additionne deux entiers. (4 pts) [CODE]\nLangage : Python\n2. Expliquez votre démarche. (4 pts) [OUVERT]\n"
RUBRIC = BLOCK + "Question 2 (4 pts) : démarche claire\n"
sc, mx, tot, bd, nums, st = R._deterministic_grade(CONTENT, RUBRIC, {'pq_1': 'print(1)'}, seed=1)
chk("sans résultat de tests : question PAS notée ici (IA / professeur), comme avant", '1' not in nums, nums)
sc, mx, tot, bd, nums, st = R._deterministic_grade(CONTENT, RUBRIC, {'pq_1': 'print(1)'}, seed=1, code_results={'1': {'sha': sha, 'fraction': 0.75, 'summary': '3/4 test(s) réussi(s)'}})
chk("avec résultats : 3/4 des 4 pts = 3,0, question notée sans l'IA", sc == 3.0 and '1' in nums and st[0]['type'] == 'code', (sc, nums, st))
sc, mx, tot, bd, nums, st = R._deterministic_grade(CONTENT, RUBRIC, {'pq_1': ''}, seed=1)
chk("réponse vide : 0/4, notée", sc == 0.0 and '1' in nums, (sc, nums))
sc, mx, tot, bd, nums, st = R._deterministic_grade(CONTENT, "Question 1 (4 pts) : preuve attendue", {'pq_1': 'x'}, seed=1, code_results={'1': {'sha': QT.code_sha('x'), 'fraction': 1.0, 'summary': ''}})
chk("question de code SANS tests dans le barème : comportement d'avant (à l'IA)", '1' not in nums, nums)

print("\n== Routes ==")
s = get_session(); sfx = uuid.uuid4().hex[:6]; now = datetime.utcnow(); c = app.test_client(use_cookies=False); ids = {'users': [], 'ex': [], 'sub': [], 'att': []}
def mk(role, name):
    u = User(email=f'zz_{name}_{sfx}@test.local', full_name=f'ZZ {name}', role=role, is_active=True, password_hash='x'); s.add(u); s.commit(); ids['users'].append(u.id); return u
H = lambda u: {'Authorization': 'Bearer ' + create_access_token(u.id, u.role.value, u.email)}
try:
    prof, prof2, stu, other = mk(UserRole.PROFESSOR, 'prof'), mk(UserRole.PROFESSOR, 'prof2'), mk(UserRole.STUDENT, 'stu'), mk(UserRole.STUDENT, 'other')
    sub = Subject(title='ZZ code', content=CONTENT, rubric=RUBRIC, creator_id=prof.id); s.add(sub); s.commit(); ids['sub'].append(sub.id)
    ex = OnlineExam(subject_id=sub.id, title='ZZ code', duration_minutes=60, start_time=now, end_time=now + timedelta(hours=1), status=ExamStatus.ACTIVE, created_by_id=prof.id); s.add(ex); s.commit(); ids['ex'].append(ex.id)
    CODE = 'a,b=map(int,input().split())\nprint(a+b)'
    at = ExamAttempt(exam_id=ex.id, student_id=stu.id, status=AttemptStatus.IN_PROGRESS, started_at=now, answers=json.dumps({'pq_1': CODE, 'pq_2': 'je calcule'}), exam_seen_at=now); s.add(at); s.commit(); ids['att'].append(at.id)
    A = at.id
    r = c.get(f'/api/exam_attempts/{A}/code_info', headers=H(stu)); j = r.get_json() or {}
    q1 = (j.get('questions') or {}).get('1', {})
    chk("code_info : langage, exécutable, 2 tests visibles, 2 cachés annoncés", r.status_code == 200 and q1.get('lang') == 'python' and q1.get('runnable') is True and len(q1['tests']) == 2 and q1['hidden'] == 2, j)
    raw = json.dumps(j)
    chk("… AUCUN test caché ne fuit vers l'étudiant (ni « -5 5 » ni « 101 »)", '-5 5' not in raw and '"101"' not in raw and '100 1' not in raw, raw)
    chk("code_info d'un autre étudiant : refusé (404)", c.get(f'/api/exam_attempts/{A}/code_info', headers=H(other)).status_code == 404)
    chk("l'étudiant ne peut PAS lire les tests cachés (code_tests réservé au personnel : 403)", c.get(f'/api/exam_attempts/{A}/code_tests', headers=H(stu)).status_code == 403)
    chk("l'étudiant ne peut PAS poster de résultats (403)", c.post(f'/api/exam_attempts/{A}/code_results', headers=H(stu), json={'num': '1', 'code_sha': QT.code_sha(CODE), 'results': []}).status_code == 403)
    r = c.get(f'/api/exam_attempts/{A}/code_tests', headers=H(prof)); j = r.get_json() or {}
    q = (j.get('questions') or [{}])[0]
    chk("code_tests (enseignant) : 4 tests dont 2 cachés, réponse de l'étudiant et empreinte", r.status_code == 200 and len(q['tests']) == 4 and sum(t['hidden'] for t in q['tests']) == 2 and q['code'] == CODE and q['code_sha'] == QT.code_sha(CODE) and q['saved'] is None, j)
    chk("code_tests d'un autre professeur sans lien avec l'examen : 403", c.get(f'/api/exam_attempts/{A}/code_tests', headers=H(prof2)).status_code == 403)
    post = lambda body, who=prof: c.post(f'/api/exam_attempts/{A}/code_results', headers=H(who), json=body)
    good = [{'n': t['n'], 'status': 'ok', 'got': t['expected']} for t in key['tests']]
    r = post({'num': '1', 'code_sha': 'x' * 64, 'results': good}); chk("réponse changée depuis l'exécution (empreinte différente) : 409", r.status_code == 409, r.get_json())
    r = post({'num': '1', 'code_sha': q['code_sha'], 'results': good[:3]}); chk("exécution incomplète (3 tests sur 4) : refusée, rien d'enregistré", r.status_code == 400 and CR.load(s, A) == {}, r.get_json())
    r = post({'num': '9', 'code_sha': q['code_sha'], 'results': good}); chk("question sans tests : 404", r.status_code == 404)
    r = post({'num': '1', 'code_sha': q['code_sha'], 'results': good}, who=prof2); chk("autre professeur : 403", r.status_code == 403)
    # copie pas encore corrigée : le résultat est enregistré, la note sera lue à la correction
    r = post({'num': '1', 'code_sha': q['code_sha'], 'results': good[:2] + [{'n': 3, 'status': 'ok', 'got': 'X'}, {'n': 4, 'status': 'timeout', 'got': ''}]}); j = r.get_json() or {}
    chk("résultats valides : 2/4 → fraction 0,5 enregistrée, verdict recalculé par le serveur", r.status_code == 200 and j['fraction'] == 0.5 and '2/4' in j['summary'] and CR.load(s, A).get('1', {}).get('fraction') == 0.5, j)
    chk("… copie non corrigée : la note n'est pas touchée (lue à la correction)", at.score is None and j['question_scores'] == [])
    s.expire_all(); at = s.query(ExamAttempt).get(A)
    # copie corrigée par l'IA : la note de CETTE question est remplacée, le total recalculé
    at.status = AttemptStatus.SUBMITTED; at.score = 10.0
    at.question_scores = json.dumps([{'num': '1', 'type': 'open', 'max': 4, 'score': 1.0, 'feedback': 'IA'}, {'num': '2', 'type': 'open', 'max': 4, 'score': 4.0, 'feedback': 'ok'}]); s.commit()
    r = post({'num': '1', 'code_sha': q['code_sha'], 'results': good}); j = r.get_json() or {}
    s.expire_all(); at = s.query(ExamAttempt).get(A)
    qs = {x['num']: x for x in json.loads(at.question_scores)}
    chk("copie corrigée : question 1 = 4/4 (tests), question 2 inchangée, total recalculé sur 20 (8/8 → 20)", r.status_code == 200 and qs['1']['score'] == 4.0 and qs['1']['type'] == 'code' and qs['2']['score'] == 4.0 and at.score == 20.0 and j['score'] == 20.0, (j.get('score'), qs))
    r = post({'num': '1', 'code_sha': q['code_sha'], 'results': [{'n': t['n'], 'status': 'ok', 'got': 'faux'} for t in key['tests']]})
    s.expire_all(); at = s.query(ExamAttempt).get(A); qs = {x['num']: x for x in json.loads(at.question_scores)}
    chk("nouvelle exécution : la note de la question suit (0/4), total 10/20", r.status_code == 200 and qs['1']['score'] == 0.0 and at.score == 10.0, (at.score, qs))
    r = c.get(f'/api/exam_attempts/{A}/code_tests', headers=H(prof)); q = r.get_json()['questions'][0]
    chk("code_tests rend le dernier résultat enregistré pour cette réponse", q['saved'] and q['saved']['fraction'] == 0.0 and len(q['saved']['detail']) == 4, q['saved'])
    # liste des copies à tester (exécution groupée)
    at.status = AttemptStatus.SUBMITTED; s.commit()
    CR.save(s, A, '1', 'z' * 64, 1.0, 'ancien', [], prof.id)
    r = c.get(f'/api/online_exams/{ex.id}/code_pending', headers=H(prof)); j = r.get_json() or {}
    chk("code_pending : la copie dont le résultat est périmé est listée", r.status_code == 200 and j.get('has_code') and j['total'] == 1 and j['attempts'][0]['questions'] == ['1'], j)
    post({'num': '1', 'code_sha': q['code_sha'], 'results': good})
    j = c.get(f'/api/online_exams/{ex.id}/code_pending', headers=H(prof)).get_json() or {}
    chk("… et disparaît une fois testée", j.get('total') == 0, j)
    chk("code_pending interdit à un étudiant (403) et à un professeur étranger à l'examen (403)", c.get(f'/api/online_exams/{ex.id}/code_pending', headers=H(stu)).status_code == 403 and c.get(f'/api/online_exams/{ex.id}/code_pending', headers=H(prof2)).status_code == 403)
    # la correction automatique reprend le résultat enregistré
    s.expire_all(); at = s.query(ExamAttempt).get(A)
    stored = R._stored_code_results(s, A)
    sc, mx, tot, bd, nums, st = R._deterministic_grade(CONTENT, RUBRIC, json.loads(at.answers), seed=1, code_results=stored)
    chk("la correction automatique relit les résultats enregistrés (4/4)", '1' in nums and sc == 4.0, (sc, nums))
finally:
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
