"""Garde-fou (SUR le serveur) : affiche « REEL … » s'il existe un examen ou une tentative autres que ceux du test de charge, « OK » sinon.  Usage : guard.py <id examen de charge>"""
import os, sys
sys.path.insert(0, '/home/serge/projet-cei/cei-api-v2'); os.chdir('/home/serge/projet-cei/cei-api-v2')
from app import app
from models import get_session
from sqlalchemy import text
ex = int(sys.argv[1]); s = get_session()
a = s.execute(text("select count(*) from online_exams where status='ACTIVE' and id<>:e and end_time > (now() at time zone 'utc')"), {'e': ex}).scalar()
b = s.execute(text("select count(*) from exam_attempts where status='IN_PROGRESS' and exam_id<>:e"), {'e': ex}).scalar()
print('REEL examens=%s tentatives=%s' % (a, b) if (a or b) else 'OK')
