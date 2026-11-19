"""NETTOIE le test de charge en production (SUR le serveur) :  .venv-base/bin/python scripts/charge_prod/cleanup.py   (lit /tmp/charge_ctx.json)
Supprime l'examen, les copies, journaux, comptes de test et leurs clés Redis. Ne touche à rien d'autre (comptes zz_charge_<tag>_ seulement)."""
import os, sys, json
API = os.environ.get('CEI_API_DIR', '/home/serge/projet-cei/cei-api-v2'); sys.path.insert(0, API); os.chdir(API)
from app import app
from models import get_session
from cache import _get_client
from sqlalchemy import text
ctx = json.load(open('/tmp/charge_ctx.json')); tag = ctx['tag']; ex = ctx['exam']
s = get_session()
FK = s.execute(text("""select c.confrelid::regclass::text, c.conrelid::regclass::text, a.attname, pa.attname
  from pg_constraint c join pg_attribute a on a.attrelid=c.conrelid and a.attnum=c.conkey[1]
  join pg_attribute pa on pa.attrelid=c.confrelid and pa.attnum=c.confkey[1] where c.contype='f'""")).fetchall()
def purge(table, col, ids, depth=0):
    ids = list(ids)
    if not ids or depth > 6: return
    for b in range(0, len(ids), 2000):
        chunk = ids[b:b + 2000]
        rows = [r[0] for r in s.execute(text(f'select id from {table} where {col} = any(:i)'), {'i': chunk}).fetchall()]
        for parent, child, ccol, pcol in FK:
            if parent == table and pcol == 'id' and rows: purge(child, ccol, rows, depth + 1)
        s.execute(text(f'delete from {table} where {col} = any(:i)'), {'i': chunk}); s.commit()
att_ids = [r[0] for r in s.execute(text('select id from exam_attempts where exam_id=:e'), {'e': ex})]
purge('exam_attempts', 'id', att_ids)
purge('online_exams', 'id', [ex])
purge('subjects', 'id', [ctx['sub']])
uids = [r[0] for r in s.execute(text("select id from users where email like :p"), {'p': f'zz_charge_{tag}_%'})]
for b in range(0, len(uids), 2000):
    s.execute(text('delete from student_ue_enrollments where student_id = any(:i)'), {'i': uids[b:b + 2000]}); s.commit()
purge('users', 'id', uids)
rc = _get_client()
if rc is not None:
    for u in uids: rc.delete(f'cei:notif:user:{u}', f'cei:session:active:{u}')
    for a in att_ids: rc.delete(f'cei:risk:{a}', f'cei:riskalert:{a}')
    for code in ('entree_bloquee', 'entree_lente', 'hors_ligne', 'liaison_video', 'trop_d_alertes', 'application_instable', 'appels_attente'): rc.delete(f'cei:vitals:alert:{ex}:{code}')
    for k in (f'cei:vitals:cache:{ex}', f'cei:readiness:{ex}:full', f'cei:exam_seen_count:{ex}', f'cei:ap_full:{ex}'): rc.delete(k)
# Captures caméra du test (stockage objet + repli disque) : supprimées elles aussi, rien ne reste en production
nb_snap = 0
try:
    import s3_client as S3, shutil
    cl = S3._client(); pref = f'snapshots/{ex}/'
    for page in cl.get_paginator('list_objects_v2').paginate(Bucket=S3._SNAP_BUCKET, Prefix=pref):
        objs = [{'Key': o['Key']} for o in page.get('Contents', [])]
        if objs: cl.delete_objects(Bucket=S3._SNAP_BUCKET, Delete={'Objects': objs}); nb_snap += len(objs)
    shutil.rmtree(os.path.join(S3._UPLOAD_FOLDER, S3._FALLBACK_DIR, str(ex)), ignore_errors=True)
except Exception as e:
    print('captures : nettoyage incomplet :', e)
left = s.execute(text("select count(*) from users where email like :p"), {'p': f'zz_charge_{tag}_%'}).scalar() + s.execute(text('select count(*) from online_exams where id=:e'), {'e': ex}).scalar()
print(json.dumps({'nettoyé': True, 'restant': left, 'captures_supprimées': nb_snap}))
