"""Garde-fou (SUR le serveur) : affiche « REEL … » s'il existe un examen ou une tentative autres que ceux du test de charge, « OK » sinon.  Usage : guard.py <id examen de charge>

N'IMPORTE PAS l'application (19/11/2026) : `import app` exécute init_db() et ses migrations (ALTER TABLE → verrou exclusif sur proctor_assignments). Lancé toutes les 10 s pendant un test
de charge, ce garde-fou bloquait lui-même la plateforme (72 requêtes en attente derrière lui) et faussait les mesures. Il se connecte donc directement à la base, en lecture seule."""
import os, sys
from urllib.parse import urlparse
API = os.environ.get('CEI_API_DIR', '/home/serge/projet-cei/cei-api-v2')
url = next(l.strip().split('=', 1)[1] for l in open(os.path.join(API, '.env')) if l.startswith('DATABASE_URL='))
import psycopg2
u = urlparse(url)
c = psycopg2.connect(host=u.hostname, port=u.port, user=u.username, password=u.password, dbname=u.path.lstrip('/'), connect_timeout=5)
c.set_session(readonly=True, autocommit=True)
cur = c.cursor(); ex = int(sys.argv[1])
cur.execute("select count(*) from online_exams where status='ACTIVE' and id<>%s and end_time > (now() at time zone 'utc')", (ex,)); a = cur.fetchone()[0]
cur.execute("select count(*) from exam_attempts where status='IN_PROGRESS' and exam_id<>%s", (ex,)); b = cur.fetchone()[0]
print('REEL examens=%s tentatives=%s' % (a, b) if (a or b) else 'OK')
