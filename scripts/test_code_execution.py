#!/usr/bin/env python
"""Questions de code avec tests automatiques (bac à sable Piston) — 24/11/2026.
    cd cei-api-v2 && .venv-base/bin/python scripts/test_code_execution.py     (crée puis supprime ses propres données)
Trois parties : lecture du barème et notation (faux bac à sable), routes de l'étudiant (code_info / code_run : jamais de test caché), et — si
CODE_SANDBOX_URL répond — exécution RÉELLE (Python, C, Java, JavaScript, boucle infinie, erreur de compilation…)."""
import sys, os, uuid, json
sys.path.insert(0, '.')
from app import app
from models import *
from routes.exams import get_session
import routes.exams as R
from auth_paseto import create_access_token
from services import code_sandbox as SB, question_types as QT
from datetime import datetime, timedelta

RES = []
def chk(n, ok, d=''):
    RES.append(bool(ok)); print(('  ✔ ' if ok else '  ✘ ') + n + (f'  — {str(d)[:240]}' if d and not ok else ''))
REAL_URL = os.environ.get('CODE_SANDBOX_URL', 'http://127.0.0.1:2000')
real_run = SB.run

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
chk("alias de langage : py, js, c++ reconnus", [SB.norm_lang(x) for x in ('py', 'JS', 'C++', 'Python3', 'cobol')] == ['python', 'javascript', 'cpp', 'python', None])
chk("sans tests ou sans langage : aucune clé (la question reste à l'IA)", QT.extract_keys_from_block("Langage : python") == {} and 'code' not in QT.extract_keys_from_block("Test : entrée 1 ; sortie 2"))
chk("comparaison tolérante : espaces de fin de ligne et lignes vides finales ignorés", SB.normalize_output("7  \r\n\n\n") == '7' and SB.normalize_output('a\nb') != SB.normalize_output('a b'))

print('\n== Notation (faux bac à sable) ==')
os.environ['CODE_SANDBOX_URL'] = 'http://sandbox.invalid'
calls = []
def fake(lang, source, stdin='', timeout=None):
    calls.append((lang, stdin))
    if 'BOUCLE' in source: return {'status': 'timeout', 'stdout': '', 'stderr': '', 'code': None}
    if 'PANNE' in source: return {'status': 'unavailable', 'stdout': '', 'stderr': '', 'code': None}
    if 'ERREURC' in source: return {'status': 'compile_error', 'stdout': '', 'stderr': 'syntax', 'code': 1}
    a, b = [int(x) for x in stdin.split()]
    return {'status': 'ok', 'stdout': str(a + b + (1 if 'FAUX' in source else 0)) + '\n', 'stdout_': '', 'stderr': '', 'code': 0}
SB.run = fake
g = SB.grade('python', 'print(a+b)', key['tests'])
chk("4 tests sur 4 → 1,0 (visibles ET cachés exécutés)", g and g[0] == 1.0 and len(calls) == 4, (g and g[1], len(calls)))
g = SB.grade('python', 'FAUX', key['tests']); chk("code faux → 0/4", g and g[0] == 0.0 and '0/4' in g[1], g and g[1])
g = SB.grade('python', 'BOUCLE', key['tests']); chk("boucle infinie (délai dépassé) → 0, jamais de blocage", g and g[0] == 0.0, g and g[1])
g = SB.grade('python', 'ERREURC', key['tests']); chk("erreur de compilation → 0 avec le résumé « erreur de compilation »", g and g[0] == 0.0 and 'compilation' in g[1], g and g[1])
calls.clear(); g = SB.grade('python', '   \n', key['tests']); chk("réponse vide → 0 SANS appeler le bac à sable", g and g[0] == 0.0 and not calls, (g, calls))
g = SB.grade('python', 'PANNE', key['tests']); chk("bac à sable en panne → None : on ne note JAMAIS un étudiant sur une panne de notre côté", g is None)
os.environ['CODE_SANDBOX_URL'] = ''
chk("fonction désactivée (URL vide) → None : question laissée à l'IA / au professeur", SB.grade('python', 'x', key['tests']) is None and not SB.enabled())
os.environ['CODE_SANDBOX_URL'] = 'http://sandbox.invalid'
half = dict(key); half['tests'] = [dict(t) for t in key['tests']]
def fake_half(lang, source, stdin='', timeout=None):
    a, b = [int(x) for x in stdin.split()]; return {'status': 'ok', 'stdout': str(a + b if a < 10 else 999), 'stderr': '', 'code': 0}
SB.run = fake_half
g = SB.grade('python', 'x', key['tests']); chk("2 tests sur 4 → fraction 0,5 (proportionnelle)", g and g[0] == 0.5 and '2/4' in g[1], g and g[1])
g = SB.grade('python', 'x', key['tests'], all_or_nothing=True); chk("même résultat en « tout ou rien » → 0", g and g[0] == 0.0)
SB.run = fake

print('\n== Note de la copie (_deterministic_grade) ==')
CONTENT = "1. Écrire un programme qui additionne deux entiers. (4 pts) [CODE]\nLangage : Python\n"
RUBRIC = BLOCK
sc, mx, tot, bd, nums, st = R._deterministic_grade(CONTENT, RUBRIC, {'pq_1': 'print(a+b)'}, seed=1)
chk("copie juste : 4/4, question notée sans l'IA", sc == 4.0 and '1' in nums and st and st[0]['type'] == 'code', (sc, nums, st))
sc, mx, tot, bd, nums, st = R._deterministic_grade(CONTENT, RUBRIC, {'pq_1': 'FAUX'}, seed=1)
chk("copie fausse : 0/4, question tout de même notée", sc == 0.0 and '1' in nums, (sc, nums))
os.environ['CODE_SANDBOX_URL'] = ''
sc, mx, tot, bd, nums, st = R._deterministic_grade(CONTENT, RUBRIC, {'pq_1': 'print(a+b)'}, seed=1)
chk("bac à sable désactivé : la question n'est PAS notée ici (reste à l'IA / au professeur)", '1' not in nums, nums)
os.environ['CODE_SANDBOX_URL'] = 'http://sandbox.invalid'
sc, mx, tot, bd, nums, st = R._deterministic_grade(CONTENT, "Question 1 (4 pts) : preuve attendue", {'pq_1': 'x'}, seed=1)
chk("question de code SANS tests dans le barème : comportement d'avant (à l'IA)", '1' not in nums, nums)

print('\n== Routes de l\'étudiant ==')
s = get_session(); sfx = uuid.uuid4().hex[:6]; now = datetime.utcnow(); c = app.test_client(use_cookies=False); ids = {'users': [], 'ex': [], 'sub': [], 'att': []}
def mk(role, name):
    u = User(email=f'zz_{name}_{sfx}@test.local', full_name=f'ZZ {name}', role=role, is_active=True, password_hash='x'); s.add(u); s.commit(); ids['users'].append(u.id); return u
H = lambda u: {'Authorization': 'Bearer ' + create_access_token(u.id, u.role.value, u.email)}
try:
    prof, stu, other = mk(UserRole.PROFESSOR, 'prof'), mk(UserRole.STUDENT, 'stu'), mk(UserRole.STUDENT, 'other')
    sub = Subject(title='ZZ code', content=CONTENT, rubric=RUBRIC, creator_id=prof.id); s.add(sub); s.commit(); ids['sub'].append(sub.id)
    ex = OnlineExam(subject_id=sub.id, title='ZZ code', duration_minutes=60, start_time=now, end_time=now + timedelta(hours=1), status=ExamStatus.ACTIVE, created_by_id=prof.id); s.add(ex); s.commit(); ids['ex'].append(ex.id)
    at = ExamAttempt(exam_id=ex.id, student_id=stu.id, status=AttemptStatus.IN_PROGRESS, started_at=now); s.add(at); s.commit(); ids['att'].append(at.id)
    info = lambda who=stu, a=at: c.get(f'/api/exam_attempts/{a.id}/code_info', headers=H(who))
    run_ = lambda code, num='1', who=stu: c.post(f'/api/exam_attempts/{at.id}/code_run', headers=H(who), json={'num': num, 'code': code})
    r = info(); j = r.get_json() or {}
    chk("code_info : langage et 2 tests visibles, 2 cachés annoncés", r.status_code == 200 and j['questions']['1']['lang'] == 'python' and len(j['questions']['1']['tests']) == 2 and j['questions']['1']['hidden'] == 2, j)
    raw = json.dumps(j)
    chk("… AUCUN test caché ne fuit (ni « -5 5 » ni « 101 »)", '-5 5' not in raw and '"101"' not in raw and '100 1' not in raw, raw)
    chk("code_info d'un autre étudiant : refusé (404)", info(other).status_code == 404)
    calls.clear(); r = run_('print(a+b)'); j = r.get_json() or {}
    chk("code_run : les 2 tests VISIBLES seulement sont exécutés, tous réussis", r.status_code == 200 and j['passed'] == 2 and j['total'] == 2 and j['hidden'] == 2 and len(calls) == 2, (j, len(calls)))
    chk("… le détail de chaque test (entrée, attendu, obtenu) est rendu", j['results'][0]['input'] == '3 4' and j['results'][0]['expected'] == '7' and j['results'][0]['got'] == '7')
    chk("… sans le contenu des tests cachés", '-5 5' not in json.dumps(j) and '101' not in json.dumps(j))
    j = run_('FAUX').get_json(); chk("code faux : 0/2 visibles, détail de l'écart", j['passed'] == 0 and j['results'][0]['got'] != j['results'][0]['expected'], j)
    j = run_('BOUCLE').get_json(); chk("boucle infinie : résultat « délai dépassé », réponse rendue", j['success'] and j['results'][0]['status'] == 'timeout', j)
    chk("code vide : réponse immédiate, aucun appel", (run_('  ').get_json() or {}).get('empty') is True)
    chk("code trop long (> 64 Ko) : refusé (413)", run_('x' * (SB.MAX_CODE_CHARS + 1)).status_code == 413)
    chk("question sans tests : 404", run_('x', num='9').status_code == 404)
    chk("tentative d'un autre étudiant : refusée (404)", run_('x', who=other).status_code == 404)
    from cache import cache_set_nx_value, cache_delete
    cache_set_nx_value(f'cei:coderun:{at.id}', 1, 40); r = run_('print(a+b)'); chk("une exécution à la fois par étudiant : la 2e simultanée → 429 « busy »", r.status_code == 429 and (r.get_json() or {}).get('error') == 'busy'); cache_delete(f'cei:coderun:{at.id}')
    chk("le verrou est libéré après une exécution (la suivante passe)", run_('print(a+b)').status_code == 200)
    j = run_('PANNE').get_json(); chk("panne du bac à sable : 503 « unavailable », jamais un faux « échoué »", run_('PANNE').status_code == 503)
    os.environ['CODE_SANDBOX_URL'] = ''; chk("fonction désactivée : 503 et code_info annonce enabled=false", run_('x').status_code == 503 and info().get_json()['enabled'] is False)
    os.environ['CODE_SANDBOX_URL'] = 'http://sandbox.invalid'
    at.status = AttemptStatus.SUBMITTED; s.commit(); chk("copie rendue : exécution refusée (400)", run_('x').status_code == 400)
    at.status = AttemptStatus.IN_PROGRESS; s.commit()

    print('\n== Exécution RÉELLE (bac à sable Piston) ==')
    SB.run = real_run; os.environ['CODE_SANDBOX_URL'] = REAL_URL
    if not SB.healthy(4):
        print(f'  (bac à sable non joignable à {REAL_URL} : contrôles réels ignorés)')
    else:
        tests = key['tests']
        g = SB.grade('python', 'a,b=map(int,input().split())\nprint(a+b)', tests); chk("Python juste : 4/4", g and g[0] == 1.0, g and g[1])
        g = SB.grade('python', 'a,b=map(int,input().split())\nprint(a-b)', tests); chk("Python faux : 0 ou partiel selon les tests (ici 1/4 : -5 5 donne -10 ≠ 0…)", g and g[0] < 1.0, g and g[1])
        g = SB.grade('python', 'while True: pass', tests); chk("boucle infinie : tuée au délai, 0/4, résultat rendu en quelques secondes", g and g[0] == 0.0, g and g[1])
        t0 = __import__('time').time(); g = SB.grade('python', 'import os\nwhile True:\n  os.fork()', tests); chk("bombe de processus : contenue (jamais de panne de l'hôte)", g is not None and g[0] == 0.0 and __import__('time').time() - t0 < 60, g and g[1])
        g = SB.grade('python', 'x=[0]*(10**10)\nprint(7)', tests[:1]); chk("bombe mémoire : stoppée par la limite, 0", g and g[0] == 0.0, g and g[1])
        g = SB.grade('python', 'import urllib.request\nurllib.request.urlopen("http://1.1.1.1",timeout=3)\nprint(7)', tests[:1]); chk("accès réseau : impossible depuis le code de l'étudiant", g and g[0] == 0.0, g and g[1])
        c_ok = '#include <stdio.h>\nint main(){int a,b;scanf("%d %d",&a,&b);printf("%d\\n",a+b);return 0;}'
        g = SB.grade('c', c_ok, tests); chk("C juste (scanf) : 4/4", g and g[0] == 1.0, g and g[1])
        g = SB.grade('c', 'int main(){ return }', tests); chk("C : erreur de compilation → 0 avec « erreur de compilation »", g and g[0] == 0.0 and 'compilation' in g[1], g and g[1])
        cpp = '#include <iostream>\nint main(){int a,b;std::cin>>a>>b;std::cout<<a+b<<"\\n";}'
        g = SB.grade('cpp', cpp, tests); chk("C++ juste : 4/4", g and g[0] == 1.0, g and g[1])
        jv = 'import java.util.*;\npublic class Somme { public static void main(String[] x){ Scanner s=new Scanner(System.in); System.out.println(s.nextInt()+s.nextInt()); } }'
        g = SB.grade('java', jv, tests); chk("Java juste (classe Somme, pas Main) : 4/4", g and g[0] == 1.0, g and g[1])
        js = 'const [a,b]=require("fs").readFileSync(0,"utf8").trim().split(/\\s+/).map(Number);console.log(a+b)'
        g = SB.grade('javascript', js, tests); chk("JavaScript juste : 4/4", g and g[0] == 1.0, g and g[1])
        callk = QT.extract_keys_from_block("Langage : python\nTest : appel « print(somme(3, 4)) » ; sortie « 7 »\nTest caché : appel « print(somme(-1, 1)) » ; sortie « 0 »")['code']
        g = SB.grade('python', 'def somme(a, b):\n    return a + b\n', callk['tests']); chk("test d'appel de fonction (comme CodeRunner) : 2/2", g and g[0] == 1.0, g and g[1])
        g = SB.grade('python', 'a,b=map(int,input().split())\nprint(a+b)   \n', tests[:1]); chk("espaces de fin de ligne ignorés dans la comparaison", g and g[0] == 1.0)
finally:
    SB.run = real_run
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
