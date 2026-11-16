#!/usr/bin/env python
"""Enrôlement de 12 surveillants (liste du Dr Abdou Diouf DIALLO, 16/11/2026) : comptes SURVEILLANT, mot de passe « passer ».
Aucun e-mail ni notification, aucun groupe ni rattachement à un examen (fait ensuite à la main par l'utilisateur).

    python scripts/creer_surveillants_16nov.py            # simulation (rien n'est écrit)
    python scripts/creer_surveillants_16nov.py --apply    # écrit"""
import os, sys, json, datetime
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as A  # noqa: F401
from extensions import bcrypt
from models import get_session, User, UserRole

APPLY = '--apply' in sys.argv
PEOPLE = [
    ('Fatou Mbacké NDIAYE', 'fatoumbacke.ndiaye@unchk.edu.sn'),
    ('Ababacar NDIAYE', 'ababacar1.ndiaye@unchk.edu.sn'),
    ('Tatiana MANGA', 'tatiana.manga@unchk.edu.sn'),
    ('Cheikh Tidiane SAMB', 'cheikhtidiane.samb@unchk.edu.sn'),
    ('Seynabou FAYE', 'seynabou.faye3@unchk.edu.sn'),
    ('Magueye GASSAMA', 'magueye.gassama@unchk.edu.sn'),
    ('Oumar DOUMBOUYA', 'oumar.doumbouya@unchk.edu.sn'),
    ('Johana Binta Vitale FAYE', 'johanabintavitale.faye@unchk.edu.sn'),
    ('Khadidiatou SANE', 'khadidiatou1.sane@unchk.edu.sn'),
    ('Kora SENE', 'kora.sene@unchk.edu.sn'),
    ('Younouss SANE', 'younouss.sane@unchk.edu.sn'),
    ('Eliane Pauline Ngoné MBAYE', 'elianepaulinengone.mbaye@unchk.edu.sn'),
]
s = get_session()
mails = [e.lower() for _, e in PEOPLE]
assert len(set(mails)) == len(mails), 'e-mails en double dans la liste'
exist = s.query(User).filter(User.email.in_(mails)).all()
for u in exist:
    print(f"  DÉJÀ PRÉSENT : {u.email} → id {u.id}, {u.full_name}, rôle {u.role.value}, actif={u.is_active}")
print('Homonymes EXACTS (même nom complet, autre e-mail) — information seulement, ce sont d\'autres personnes :')
hom = 0
for name, mail in PEOPLE:
    n = s.query(User).filter(User.full_name.ilike(name), User.email != mail.lower()).count()
    if n: hom += 1; print(f"  {name} : {n} compte(s) de même nom (étudiants)")
if not hom: print('  aucun')
tpl = s.query(User).filter(User.role == UserRole.SURVEILLANT).order_by(User.id.desc()).first()
print('modèle d\'un surveillant existant :', tpl.email, '| email_verified', tpl.email_verified, '| has_email', tpl.has_email, '| created_via', tpl.created_via)
present = {u.email for u in exist}
PEOPLE = [(n, e) for n, e in PEOPLE if e.lower() not in present]       # un compte existant n'est JAMAIS modifié (son rôle reste celui qu'il a)
print(f"À créer : {len(PEOPLE)} comptes SURVEILLANT (mot de passe « passer »), aucun e-mail envoyé. Ignorés car déjà présents : {len(present)}.")
if not APPLY:
    sys.exit('Simulation : rien n\'a été écrit.')
h = bcrypt.generate_password_hash('passer').decode()
ids = []
try:
    for name, mail in PEOPLE:
        u = User(email=mail.lower(), password_hash=h, full_name=name, role=UserRole.SURVEILLANT, is_active=True, email_verified=True, has_email=True, created_via='import_surveillants_16nov')
        s.add(u); s.flush(); ids.append(u.id)
    s.commit()
    print('OK : comptes créés', ids)
except Exception as ex:
    s.rollback(); sys.exit(f'ANNULÉ : {ex}')
os.makedirs('backups', exist_ok=True)
json.dump({'user_ids': ids, 'emails': [e.lower() for _, e in PEOPLE]}, open(f"backups/surveillants_16nov_{datetime.datetime.utcnow():%Y%m%d_%H%M%S}.json", 'w'))
