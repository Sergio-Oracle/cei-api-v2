"""Supprime tout ce que setup.py a créé.  Usage : .venv-base/bin/python scripts/e2e/teardown.py '<json de setup>'"""
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from app import app
from models import get_session
from cache import cache_delete
from sqlalchemy import text
s = get_session()
FK = s.execute(text("""select c.confrelid::regclass::text, c.conrelid::regclass::text, a.attname, pa.attname
  from pg_constraint c join pg_attribute a on a.attrelid=c.conrelid and a.attnum=c.conkey[1]
  join pg_attribute pa on pa.attrelid=c.confrelid and pa.attnum=c.confkey[1] where c.contype='f'""")).fetchall()
def purge(table, col, ids, depth=0):
    if not ids or depth > 6: return
    rows = [r[0] for r in s.execute(text(f'select id from {table} where {col} = any(:i)'), {'i': list(ids)}).fetchall()]
    for parent, child, ccol, pcol in FK:
        if parent == table and pcol == 'id' and rows: purge(child, ccol, rows, depth + 1)
    s.execute(text(f'delete from {table} where {col} = any(:i)'), {'i': list(ids)}); s.commit()
d = json.loads(sys.argv[1])
for aid in [r[0] for r in s.execute(text('select id from exam_attempts where exam_id=:e'), {'e': d['exam']}).fetchall()]: purge('exam_attempts', 'id', [aid])
if d.get('exam2'):
    for aid in [r[0] for r in s.execute(text('select id from exam_attempts where exam_id=:e'), {'e': d['exam2']}).fetchall()]: purge('exam_attempts', 'id', [aid])
    purge('online_exams', 'id', [d['exam2']]); purge('subjects', 'id', [d['sub2']])
for t in ('online_exams',): purge(t, 'id', [d['exam']])
purge('subjects', 'id', [d['sub']])
for u in d['uids']:
    s.execute(text('delete from student_ue_enrollments where student_id=:u'), {'u': u}); s.commit()
    for k in (f'cei:techtest:{u}', f'cei:techtest:verify:{u}'): cache_delete(k)
purge('users', 'id', d['uids'])
print('nettoyé')
