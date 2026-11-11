#!/usr/bin/env python
"""Protection contre la perte de notifications lors de la publication d'une promotion.

Ne contacte ni Redis ni la base : un faux pipeline vérifie que 1 499 messages
personnalisés sont découpés et que chaque étudiant reçoit exactement sa file
et son historique.
"""
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import notif_bus as N


RESULTS = []


def check(label, ok, detail=''):
    RESULTS.append(bool(ok))
    print(('  ✔ ' if ok else '  ✘ ') + label + (f' — {detail}' if detail else ''))


class Pipeline:
    def __init__(self, calls):
        self.calls = calls

    def rpush(self, *args): self.calls.append(('rpush', args)); return self
    def ltrim(self, *args): self.calls.append(('ltrim', args)); return self
    def expire(self, *args): self.calls.append(('expire', args)); return self
    def execute(self): self.calls.append(('execute', ())); return []


class Redis:
    def __init__(self): self.calls = []
    def pipeline(self, **_kwargs): return Pipeline(self.calls)


class InlineExecutor:
    def submit(self, fn, *args, **kwargs):
        fn(*args, **kwargs)


fake = Redis()
events = [(i, 'correction_done', 'Copie corrigée', f'Note : {i % 21}/20') for i in range(1, 1500)]
with mock.patch.object(N, '_get_redis', return_value=fake), \
     mock.patch.object(N, '_executor', InlineExecutor()):
    N.notify_users(events)

rpush = [args for name, args in fake.calls if name == 'rpush']
executes = [1 for name, _args in fake.calls if name == 'execute']
student_keys = [args[0] for args in rpush if args[0].startswith('cei:notif:user:')]
history_keys = [args[0] for args in rpush if args[0].startswith('cei:notif:history:')]
check('1 499 notifications personnalisées sont toutes préparées', len(student_keys) == 1499, len(student_keys))
check('chaque étudiant reçoit une seule notification', len(set(student_keys)) == 1499, len(set(student_keys)))
check('chaque notification a un historique personnel', len(history_keys) == 1499, len(history_keys))
check('la rafale est découpée en lots bornés', executes == [1] * 8, len(executes))

print(f'\n{sum(RESULTS)}/{len(RESULTS)} vérifications réussies')
sys.exit(0 if all(RESULTS) else 1)
