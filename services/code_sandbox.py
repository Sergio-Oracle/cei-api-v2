"""Exécution du code des étudiants dans un bac à sable ISOLÉ — questions « Maths / Programmation » avec tests automatiques (24/11/2026).

Outil open source retenu : Piston (engineer-man/piston, licence MIT) — le code est compilé / exécuté dans une enceinte `isolate` (cgroups v2, espaces de noms) :
temps et mémoire bornés, nombre de processus borné, taille de sortie bornée, AUCUN accès réseau (réglages du conteneur Piston : voir docs/EXPLOITATION_EXAMEN.md).
Même principe que CodeRunner / Jobe de Moodle : l'API envoie le code et l'entrée, reçoit la sortie, et la compare à celle attendue.

Configuration (variables d'environnement) :
  CODE_SANDBOX_URL        adresse de Piston (ex. http://127.0.0.1:2000). VIDE = fonction désactivée : les questions de code restent corrigées par l'IA / le professeur, comme avant.
  CODE_SANDBOX_MAX_CONCURRENT   exécutions simultanées par processus de l'API (défaut 2) : protège les threads de l'API (heartbeats, sauvegardes) d'une rafale de tests.
Ce module ne lève jamais d'exception vers l'appelant : tout incident devient un résultat « indisponible ».
"""
import json
import os
import re
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

# Langage (nom dans les barèmes) → (langage Piston, nom du fichier source)
LANGS = {
    'python': ('python', 'main.py'),
    'javascript': ('javascript', 'main.js'),
    'java': ('java', 'Main.java'),
    'c': ('c', 'main.c'),
    'cpp': ('c++', 'main.cpp'),
    'php': ('php', 'main.php'),
}
ALIASES = {'py': 'python', 'python3': 'python', 'js': 'javascript', 'node': 'javascript', 'nodejs': 'javascript', 'c++': 'cpp', 'cplusplus': 'cpp'}

MAX_CODE_CHARS = 65536          # taille maximale d'un programme (protège l'API et le bac à sable)
RUN_TIMEOUT_MS = 3000           # temps d'exécution maximal d'UN test
COMPILE_TIMEOUT_MS = 10000
_SEM = None
_SEM_LOCK = threading.Lock()


def norm_lang(name) -> str | None:
    n = re.sub(r'[^a-z+#]', '', str(name or '').lower())
    n = ALIASES.get(n, n)
    return n if n in LANGS else None


def base_url() -> str:
    return (os.environ.get('CODE_SANDBOX_URL') or '').strip().rstrip('/')


def enabled() -> bool:
    return bool(base_url())


def _sem() -> threading.BoundedSemaphore:
    global _SEM
    with _SEM_LOCK:
        if _SEM is None:
            try:
                n = max(1, int(os.environ.get('CODE_SANDBOX_MAX_CONCURRENT') or 2))
            except ValueError:
                n = 2
            _SEM = threading.BoundedSemaphore(n)
        return _SEM


def normalize_output(s: str) -> str:
    """Comparaison tolérante de CodeRunner : fins de ligne unifiées, espaces de fin de ligne et lignes vides finales ignorés."""
    lines = [ln.rstrip() for ln in str(s or '').replace('\r\n', '\n').replace('\r', '\n').split('\n')]
    while lines and not lines[-1]:
        lines.pop()
    return '\n'.join(lines)


def _java_name(source: str) -> str:
    m = re.search(r'public\s+(?:final\s+|abstract\s+)*class\s+([A-Za-z_]\w*)', source or '')
    return f'{m.group(1)}.java' if m else 'Main.java'


def run(lang: str, source: str, stdin: str = '', timeout=None) -> dict:
    """Exécute `source`. → {'status': ok | compile_error | runtime_error | timeout | memory | output | unavailable | refused, 'stdout', 'stderr', 'code'}.
    Ne lève jamais d'exception."""
    key = norm_lang(lang)
    out = {'status': 'unavailable', 'stdout': '', 'stderr': '', 'code': None}
    if not enabled() or not key:
        return out
    if len(source or '') > MAX_CODE_CHARS:
        return {**out, 'status': 'refused', 'stderr': 'programme trop long'}
    piston_lang, fname = LANGS[key]
    if key == 'java':
        fname = _java_name(source)
    body = {'language': piston_lang, 'version': '*', 'files': [{'name': fname, 'content': source}], 'stdin': stdin or '',
            'run_timeout': int(timeout or RUN_TIMEOUT_MS), 'compile_timeout': COMPILE_TIMEOUT_MS}
    sem = _sem()
    if not sem.acquire(timeout=8):
        return {**out, 'status': 'unavailable', 'stderr': 'occupé'}
    try:
        req = urllib.request.Request(base_url() + '/api/v2/execute', data=json.dumps(body).encode(), headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=(int(timeout or RUN_TIMEOUT_MS) + COMPILE_TIMEOUT_MS) / 1000 + 8) as r:
            d = json.load(r)
    except Exception:
        return out
    finally:
        sem.release()
    comp, rn = d.get('compile') or {}, d.get('run') or {}
    if comp and comp.get('code') not in (0, None):
        return {'status': 'compile_error', 'stdout': '', 'stderr': str(comp.get('stderr') or comp.get('output') or '')[:2000], 'code': comp.get('code')}
    st = rn.get('status')
    status = {'TO': 'timeout', 'OL': 'output', 'EL': 'output'}.get(st)
    if status is None and st == 'RE' and ('MemoryError' in str(rn.get('stderr')) or rn.get('code') == 137):
        status = 'memory'
    if status is None:
        status = 'ok' if rn.get('code') in (0, None) and not rn.get('signal') else 'runtime_error'
    return {'status': status, 'stdout': str(rn.get('stdout') or '')[:65536], 'stderr': str(rn.get('stderr') or '')[:2000], 'code': rn.get('code')}


def _program(lang: str, source: str, test: dict) -> tuple[str, str]:
    """(programme, entrée standard) d'un test. « appel » : l'expression est ajoutée après le code de l'étudiant (Python, JavaScript, PHP) — test d'une fonction, comme CodeRunner."""
    if test.get('kind') == 'call':
        return (source.rstrip() + '\n' + (test.get('input') or '') + '\n'), ''
    stdin = test.get('input') or ''
    return source, (stdin if not stdin or stdin.endswith('\n') else stdin + '\n')


def run_test(lang: str, source: str, test: dict) -> dict:
    """Un test → {'n', 'ok', 'status', 'got', 'expected', 'stderr'}. `status` = unavailable si le bac à sable ne répond pas (le test n'est alors NI réussi NI échoué)."""
    prog, stdin = _program(lang, source, test)
    r = run(lang, prog, stdin)
    expected = normalize_output(test.get('expected'))
    got = normalize_output(r['stdout'])
    return {'n': test.get('n'), 'ok': r['status'] == 'ok' and got == expected, 'status': r['status'], 'got': got, 'expected': expected, 'stderr': r['stderr']}


def run_tests(lang: str, source: str, tests: list, workers: int = 4) -> list:
    if not tests:
        return []
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(tests)))) as ex:
        return list(ex.map(lambda t: run_test(lang, source, t), tests))


def healthy(timeout: float = 3.0) -> bool:
    """Le bac à sable répond-il (et connaît-il au moins un langage) ?"""
    if not enabled():
        return False
    try:
        with urllib.request.urlopen(base_url() + '/api/v2/runtimes', timeout=timeout) as r:
            return bool(json.load(r))
    except Exception:
        return False


def grade(lang: str, source: str, tests: list, all_or_nothing: bool = False):
    """Note les tests (visibles ET cachés) d'une réponse. → (fraction, résumé lisible, détail) ou None si le bac à sable est indisponible
    (la question reste alors confiée à l'IA / au professeur, comme avant). Une réponse vide vaut 0 sans appel."""
    if not tests:
        return None
    if not (source or '').strip():
        return 0.0, 'aucune réponse', []
    if not enabled():
        return None
    res = run_tests(lang, source, tests)
    if any(r['status'] in ('unavailable',) for r in res):
        return None                      # incident d'infrastructure : on ne note jamais un étudiant sur une panne de notre côté
    passed = sum(1 for r in res if r['ok'])
    fraction = (1.0 if passed == len(res) else 0.0) if all_or_nothing else passed / len(res)
    fails = [str(r['n']) for r in res if not r['ok']]
    if any(r['status'] == 'compile_error' for r in res):
        summary = f"erreur de compilation — 0/{len(res)} test(s)"
    else:
        summary = f"{passed}/{len(res)} test(s) réussi(s)" + (f" (échec : test {', '.join(fails)})" if fails else '')
    return fraction, summary, res
