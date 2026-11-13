#!/usr/bin/env python
"""Import des 30 auditeurs UFC du Master 1 SRIV (test de Hassimiou Diallo, examen du 14/11/2026), mot de passe « passer ».

    python scripts/import_auditeurs_ufc_msriv.py            # simulation
    python scripts/import_auditeurs_ufc_msriv.py --apply    # écrit (une seule transaction)

Comptes marqués created_via='import_ufc' (retour arrière : supprimer ces comptes et leurs inscriptions). Inscrits aux 10 UE de M1-SRIV,
comme les 68 étudiants déjà présents. Un compte déjà existant (même e-mail) n'est ni modifié ni inscrit : il est seulement signalé."""
import os, sys, json, datetime
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.env'))
import bcrypt, psycopg2

APPLY = '--apply' in sys.argv
FORMATION_ID = 17          # M1-SRIV
PASSWORD = 'passer'
ROWS = [  # (prénoms, NOM, e-mail institutionnel)
 ('Ahmadou','GUEYE','ahmadou.gueye1@unchk.edu.sn'),('Khadim','CISSE','khadim.cisse4@unchk.edu.sn'),
 ('Serigne Mbacke Madina','THIOUNE','serignembackemadina.thioune@unchk.edu.sn'),('Babacar','LY','babacar.ly4@unchk.edu.sn'),
 ('Abdiel','ZINSOU','abdiel.zinsou1@unchk.edu.sn'),('Seynabou','GUEYE','seynabougueye.samb@unchk.edu.sn'),
 ('Emmanuel Sedar','TOUPANE','emmanuelsedar.toupane1@unchk.edu.sn'),('Penda Bigue','NDIAYE','pendabigue.ndiaye1@unchk.edu.sn'),
 ('Essodong','BABAKE','essodong.babake@unchk.edu.sn'),('Mouhamadou Moustapha','TALL','mouhamadoumoustapha.tall3@unchk.edu.sn'),
 ('Serigne Cheikh','NIANG','serignecheikh.niang@unchk.edu.sn'),('Haby','SANGHARE','haby.sanghare@unchk.edu.sn'),
 ('Ibrahima','SALL','ibrahima.sall23@unchk.edu.sn'),('Seydina Malick','OUMAROU','seydinamalick.oumarou@unchk.edu.sn'),
 ('Oumy Kairy','GAYE','oumykairy.gaye@unchk.edu.sn'),('Gloire-Geodet','LOUFOUMA-BAKEBILA','gloiregeodet.loufoumabakebila@unchk.edu.sn'),
 ('Alioune Badara','KHOULE','aliounebadara.khoule1@unchk.edu.sn'),('Hassanatou','BALDE','hassanatou.balde1@unchk.edu.sn'),
 ('Parfait','ALLAGUERBAYE','parfait.allaguerbaye@unchk.edu.sn'),('Fatou','SAMB','fatou.samb29@unchk.edu.sn'),
 ('Gabriela May Simone','KPANOU','gabrielamaysimone.kpanou@unchk.edu.sn'),('Massama-Esso Samuel','BLAKIME','massamaessosamuel.blakime@unchk.edu.sn'),
 ('Ayite Daniel Emmanuel Kévin',"D'ALMEIDA",'ayitedanielemmanuelkevin.dalmeida@unchk.edu.sn'),('Oumar','DIAO','oumar.diao9@unchk.edu.sn'),
 ('Mahamat Zeni','ABDELKERIM','mahamatzeni.abdelkerim@unchk.edu.sn'),('Moussa','NASSIROU','moussa.nassirou@unchk.edu.sn'),
 ('Nafissatou','KONE','nafissatou.kone@unchk.edu.sn'),('Bouh','DIALLO','bouh.diallo@unchk.edu.sn'),
 ('El Hadji Abdoul Aziz','FAYE','elhadjiabdoulaziz.faye1@unchk.edu.sn'),('Demba','NGOM','demba.ngom2@unchk.edu.sn'),
]
assert len(ROWS) == 30 and len({r[2] for r in ROWS}) == 30
conn = psycopg2.connect(os.environ['DATABASE_URL'].replace('postgresql+psycopg2', 'postgresql'))
cur = conn.cursor()
cur.execute("select id,code from formations where id=%s", (FORMATION_ID,)); f = cur.fetchone()
if not f or f[1] != 'M1-SRIV':
    sys.exit(f"ARRÊT : la formation {FORMATION_ID} n'est pas M1-SRIV ({f})")
cur.execute("select u.id,u.code from ues u join semesters s on s.id=u.semester_id where s.formation_id=%s order by u.id", (FORMATION_ID,))
ues = cur.fetchall()
if len(ues) != 10:
    sys.exit(f"ARRÊT : {len(ues)} UE trouvées pour M1-SRIV (10 attendues)")
cur.execute("select lower(email) from users where lower(email) = any(%s)", ([r[2].lower() for r in ROWS],))
exist = {r[0] for r in cur.fetchall()}
new = [r for r in ROWS if r[2].lower() not in exist]
print(f"M1-SRIV (id {FORMATION_ID}) : {len(ues)} UE ; {len(new)} comptes à créer, {len(exist)} déjà existants {sorted(exist)}")
if not APPLY:
    for r in new[:3]: print("  ex.", f"{r[0]} {r[1]}", r[2])
    sys.exit("Simulation : rien n'a été écrit.")
h = bcrypt.hashpw(PASSWORD.encode(), bcrypt.gensalt(rounds=10)).decode()
assert bcrypt.checkpw(PASSWORD.encode(), h.encode())
ids = []
try:
    for pre, nom, mail in new:
        cur.execute("""insert into users (email,password_hash,full_name,role,niveau,is_active,email_verified,has_email,formation_id,created_via,created_at)
                       values (%s,%s,%s,'STUDENT','M1',true,true,true,%s,'import_ufc',now() at time zone 'utc') returning id""",
                    (mail, h, f"{pre} {nom}", FORMATION_ID))
        uid = cur.fetchone()[0]; ids.append(uid)
        for ue_id, _ in ues:
            cur.execute("insert into student_ue_enrollments (student_id,ue_id,enrolled_at) values (%s,%s,now() at time zone 'utc')", (uid, ue_id))
    cur.execute("select count(*) from users where created_via='import_ufc'"); assert cur.fetchone()[0] == len(new)
    cur.execute("select count(*) from student_ue_enrollments where student_id = any(%s)", (ids,)); assert cur.fetchone()[0] == len(new) * len(ues)
    conn.commit()
except Exception as ex:
    conn.rollback(); sys.exit(f"ANNULÉ : {ex}")
os.makedirs('backups', exist_ok=True)
p = f"backups/import_ufc_msriv_{datetime.datetime.utcnow():%Y%m%d_%H%M%S}.json"
json.dump({'user_ids': ids, 'created_via': 'import_ufc'}, open(p, 'w'))
print(f"OK : {len(ids)} comptes créés, {len(ids)*len(ues)} inscriptions. Liste des identifiants : {p}")
