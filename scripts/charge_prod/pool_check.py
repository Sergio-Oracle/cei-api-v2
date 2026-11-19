"""CONTRÔLE (check) ou RÉPARATION (repair) du pool PgBouncer — usage : pool_check.py check|repair <tours>   (SUR le serveur)
Répare TOUTES les connexions serveur du pool PgBouncer : 200 clients simultanés gardent chacun une transaction ouverte 0,6 s, ce qui oblige PgBouncer à mobiliser chaque connexion serveur."""
import os, sys, threading, time
from urllib.parse import urlparse
import psycopg2
API = '/home/serge/projet-cei/cei-api-v2'
url = next(l.strip().split('=', 1)[1] for l in open(os.path.join(API, '.env')) if l.startswith('DATABASE_URL='))
u = urlparse(url); mode = sys.argv[1]; rounds = int(sys.argv[2]); st = {}; lock = threading.Lock()
def worker():
    try:
        c = psycopg2.connect(host=u.hostname, port=u.port, user=u.username, password=u.password, dbname=u.path.lstrip('/'), connect_timeout=10)
        c.autocommit = False; cur = c.cursor()
        cur.execute('SHOW transaction_read_only'); ro = cur.fetchone()[0] == 'on'
        cur.execute('select pg_backend_pid(), pg_sleep(0.6)'); pid = cur.fetchone()[0]
        if ro and mode == 'repair':
            cur.execute('SET SESSION CHARACTERISTICS AS TRANSACTION READ WRITE')
        c.commit(); c.close()
        with lock: st[pid] = ro
    except Exception as e:
        with lock: st.setdefault('err', 0); st['err'] = st.get('err', 0) + 1
for r in range(rounds):
    st.clear(); ts = [threading.Thread(target=worker) for _ in range(200)]
    [t.start() for t in ts]; [t.join() for t in ts]
    pids = {k: v for k, v in st.items() if k != 'err'}
    print(f"tour {r+1}: connexions serveur distinctes atteintes : {len(pids)} ; en LECTURE SEULE : {sum(1 for v in pids.values() if v)} ; erreurs : {st.get('err', 0)}")

sys.exit(0)
