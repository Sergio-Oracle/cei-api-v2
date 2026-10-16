#!/usr/bin/env python
"""Journal d'activité des comptes à privilèges : traces fidèles, secrets masqués, consultation réservée au propriétaire
(les autres reçoivent un 404 identique à une route inexistante). Comptes et lignes de test supprimés en fin de test."""
import os, sys, uuid, json, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as A
from models import get_session, User, UserRole, Subject, EC, StaffAuditLog
from auth_paseto import create_access_token
from extensions import bcrypt

RESULTS = []
def check(nom, ok, detail=''):
    RESULTS.append((nom, bool(ok)))
    print(('  ✔ ' if ok else '  ✘ ') + nom + (f'  — {detail}' if detail and not ok else ''))

c = A.app.test_client(); s = get_session(); tag = uuid.uuid4().hex[:6]; ids = []
def mk(role, n, pwd='MotDePasse-Test-1'):
    u = User(email=f'journal.{tag}.{n}@test.invalid', full_name=f'Journal Test {n}', role=role, is_active=True, email_verified=True,
             has_email=True, password_hash=bcrypt.generate_password_hash(pwd).decode('utf-8')); s.add(u); s.commit(); ids.append(u.id); return u
def H(u): return {'Authorization': 'Bearer ' + create_access_token(u.id, u.role.value, u.email)}
def rows(**kw): return s.query(StaffAuditLog).filter_by(**kw).order_by(StaffAuditLog.id).all()

try:
    owner = mk(UserRole.ADMIN, 'proprio'); other = mk(UserRole.ADMIN, 'autre'); mgr = mk(UserRole.RESPONSABLE_EXAMEN, 'resp'); prof = mk(UserRole.PROFESSOR, 'prof')
    os.environ['AUDIT_VIEWER_EMAILS'] = owner.email
    ho, hx, hm, hp = H(owner), H(other), H(mgr), H(prof)
    ec = s.query(EC).filter(EC.ue_id.isnot(None)).order_by(EC.id).first()

    print('\n== Les actions du responsable d\'examen sont tracées ==')
    r = c.post('/api/auth/login', json={'email': mgr.email, 'password': 'MotDePasse-Test-1'})
    check("connexion réussie tracée", r.status_code == 200 and len(rows(user_id=mgr.id, kind='login')) == 1)
    c.post('/api/auth/login', json={'email': mgr.email, 'password': 'mauvais'})
    check("connexion refusée tracée", len(rows(user_id=mgr.id, kind='login_failed')) == 1)
    c.post('/api/auth/login', json={'email': prof.email, 'password': 'mauvais'})
    check("un professeur n'est pas tracé", len(rows(user_id=prof.id)) == 0)
    r = c.post('/api/subjects', headers=hm, json={'title': f'Sujet journal {tag}', 'content': 'Question 1\nA) x\nB) y', 'rubric': 'Question 1 (4 pts): B)', 'ec_id': ec.id})
    sid = r.get_json()['subject']['id'] if r.status_code == 201 else None
    cr = rows(user_id=mgr.id, kind='create')
    check("création d'un sujet tracée avec son titre", r.status_code == 201 and cr and cr[-1].target_label == f'Sujet journal {tag}' and cr[-1].target_id == sid,
          cr and (cr[-1].label, cr[-1].target_label))
    check("l'adresse IP et le navigateur sont conservés", cr and cr[-1].ip and cr[-1].user_agent is not None)
    r = c.put(f'/api/subjects/{sid}', headers=hm, json={'title': f'Sujet journal {tag} (v2)', 'content': 'Question 1\nA) x\nB) y', 'rubric': 'Question 1 (4 pts): B)'})
    check("modification tracée", any(x.kind == 'update' and x.target_id == sid for x in rows(user_id=mgr.id)))
    r = c.delete(f'/api/subjects/{sid}', headers=hm)
    dl = rows(user_id=mgr.id, kind='delete')
    check("suppression tracée avec le NOM de ce qui a disparu", r.status_code in (200, 204) and dl and 'Sujet journal' in (dl[-1].target_label or ''), dl and dl[-1].target_label)
    r = c.post('/api/admin/users', headers=hm, json={'email': f'x.{tag}@test.invalid', 'full_name': 'X', 'password': 'SecretTresLong-9', 'role': 'student'})
    ref = [x for x in rows(user_id=mgr.id) if x.rule == '/api/admin/users' and x.method == 'POST']
    check("une tentative REFUSÉE est tracée (403)", r.status_code == 403 and ref and ref[-1].status_code == 403)
    check("le mot de passe envoyé est masqué", ref and 'SecretTresLong-9' not in (ref[-1].details or '') and 'masqué' in (ref[-1].details or ''), ref and ref[-1].details)
    c.get('/api/admin/users?role=professor', headers=hm); c.get('/api/admin/users?role=professor', headers=hm)
    rd = [x for x in rows(user_id=mgr.id, kind='read') if x.rule == '/api/admin/users']
    check("consultation de la liste des comptes tracée une seule fois par minute", len(rd) == 1, len(rd))
    c.get('/api/online_exams', headers=hm)
    check("un simple affichage n'est pas tracé", not [x for x in rows(user_id=mgr.id) if x.rule == '/api/online_exams'])
    r = c.post('/api/admin/users', headers=ho, json={'email': f'y.{tag}@test.invalid', 'full_name': 'Y', 'password': 'AutreSecret-77', 'role': 'student'})
    if r.status_code in (200, 201):
        ids.append(r.get_json().get('user', {}).get('id') or 0)
    ad = [x for x in rows(user_id=owner.id) if x.rule == '/api/admin/users']
    check("les actions des administrateurs sont tracées aussi", ad and ad[-1].kind == 'create')
    check("… sans mot de passe en clair", ad and 'AutreSecret-77' not in (ad[-1].details or ''))

    print('\n== Seul le propriétaire peut consulter ==')
    for name, h in (('propriétaire', ho),):
        check("accès accordé au propriétaire", c.get('/api/audit/access', headers=h).status_code == 200)
    for name, h in (('autre administrateur', hx), ('responsable d\'examen', hm), ('professeur', hp)):
        codes = [c.get(u, headers=h).status_code for u in ('/api/audit/access', '/api/audit/accounts', '/api/audit/activity', '/api/audit/export')]
        check(f"{name} : 404 partout (la fonction paraît inexistante)", codes == [404] * 4, codes)
    same = c.get('/api/audit/access', headers=hm).get_json() == c.get('/api/no-such-route-xyz', headers=hm).get_json()
    check("la réponse est identique à celle d'une route inexistante", same)

    print('\n== Ce que voit le propriétaire ==')
    acc = c.get('/api/audit/accounts', headers=ho).get_json()
    m = next((a for a in acc['accounts'] if a['id'] == mgr.id), None)
    check("le responsable d'examen apparaît, en ligne", m is not None and m['online'] is True, m)
    check("avec le nombre d'actions et la dernière", m and m['actions_total'] >= 3 and m['last_action'] and m['failed_logins_24h'] == 1, m)
    check("l'autre administrateur figure aussi dans la liste", any(a['id'] == other.id for a in acc['accounts']))
    check("les professeurs n'y figurent pas", not any(a['id'] == prof.id for a in acc['accounts']))
    a = c.get(f'/api/audit/activity?user_id={mgr.id}', headers=ho).get_json()
    check("journal du responsable : actions dans l'ordre récent", a['total'] >= 6 and a['rows'][0]['id'] > a['rows'][-1]['id'], a['total'])
    check("filtre par nature (suppression)", {r['kind'] for r in c.get(f'/api/audit/activity?user_id={mgr.id}&kind=delete', headers=ho).get_json()['rows']} == {'delete'})
    fl = c.get(f'/api/audit/activity?user_id={mgr.id}&failures=1', headers=ho).get_json()['rows']
    check("filtre « refus et échecs »", fl and all(r['status_code'] >= 400 or r['kind'] == 'login_failed' for r in fl) and len(fl) >= 2, len(fl))
    check("recherche par texte", c.get(f'/api/audit/activity?user_id={mgr.id}&q=Sujet+journal', headers=ho).get_json()['total'] >= 2)
    check("filtre par date", c.get(f'/api/audit/activity?user_id={mgr.id}&date_from=2000-01-01&date_to=2000-01-02', headers=ho).get_json()['total'] == 0)
    ex = c.get(f'/api/audit/export?user_id={mgr.id}', headers=ho)
    body = ex.get_data(as_text=True)
    check("export CSV", ex.status_code == 200 and 'text/csv' in ex.content_type and 'Création' in body and 'SecretTresLong-9' not in body)
    check("aucune route ne permet de modifier ou supprimer une trace", c.delete('/api/audit/activity', headers=ho).status_code in (404, 405)
          and c.put('/api/audit/activity', headers=ho).status_code in (404, 405))
    n_before = s.query(StaffAuditLog).count()
    c.get('/api/audit/accounts', headers=ho); c.get('/api/audit/activity', headers=ho)
    check("consulter le journal ne l'alourdit pas", s.query(StaffAuditLog).count() == n_before)
finally:
    s.rollback()
    s.query(StaffAuditLog).filter(StaffAuditLog.user_email.like(f'journal.{tag}.%')).delete(synchronize_session=False)
    s.query(StaffAuditLog).filter(StaffAuditLog.user_id.in_([i for i in ids if i])).delete(synchronize_session=False)
    s.query(Subject).filter(Subject.title.like(f'Sujet journal {tag}%')).delete(synchronize_session=False)
    s.query(User).filter(User.email.like(f'%.{tag}@test.invalid')).delete(synchronize_session=False)
    s.query(User).filter(User.email.like(f'journal.{tag}.%')).delete(synchronize_session=False)
    s.commit(); print('\n(nettoyage terminé)')

ok = sum(1 for _, r in RESULTS if r)
print(f'\n{ok}/{len(RESULTS)} points réussis')
for n, r in RESULTS:
    if not r: print('  ÉCHEC :', n)
sys.exit(0 if ok == len(RESULTS) else 1)
