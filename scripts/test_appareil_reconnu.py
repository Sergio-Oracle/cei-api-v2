#!/usr/bin/env python
"""Bouton « CEI » de Moodle : un appareil déjà connu rouvre sa session sans repasser par Keycloak.

    cd cei-api-v2 && .venv-base/bin/python scripts/test_appareil_reconnu.py

Cas couverts : cookie d'appareil posé à la connexion étudiante (pas pour les autres rôles) ; session CEI
remplacée ailleurs → l'appareil connu rentre directement (ou confirme si une autre session est active, sans
mot de passe) ; appareil inconnu / navigateur différent / jeton falsifié → connexion UNCHK comme avant ;
déconnexion et changement de mot de passe révoquent l'appareil. Données temporaires, supprimées en fin de test.
"""
import os, sys, uuid
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as A
from extensions import bcrypt
from models import get_session, User, UserRole, TokenBlocklist
from cache import cache_delete
from auth_paseto import session_key, _device_key

RESULTS = []
def check(nom, ok, detail=''):
    RESULTS.append((nom, bool(ok)))
    print(('  ✔ ' if ok else '  ✘ ') + nom + (f'  — {detail}' if detail and not ok else ''))

s = get_session(); tag = uuid.uuid4().hex[:6]; ids = []
PW = 'MotDePasse-Test-1'
UA = {'User-Agent': 'TestBrowser/1.0'}
def mk(role, n):
    u = User(email=f'appareil.{tag}.{n}@test.invalid', full_name=f'Appareil Test {n}', role=role, is_active=True,
             email_verified=True, has_email=True, password_hash=bcrypt.generate_password_hash(PW).decode())
    s.add(u); s.commit(); ids.append(u.id); return u
def client():
    return A.app.test_client()
def get_cookie(c, name):
    for ck in c._cookies.values() if hasattr(c, '_cookies') else []:
        if ck.key == name: return ck
    return None
def ck(c, name):
    return c.get_cookie(name, domain='localhost', path='/api/auth')
def has_cookie(c, name):
    return ck(c, name) is not None
def login(c, email, force=False):
    return c.post('/api/auth/login', json={'email': email, 'password': PW, 'force': force}, headers=UA, base_url='https://localhost')
def moodle(c, to=None, headers=UA):
    return c.get('/api/auth/from-moodle' + (f'?to={to}' if to else ''), headers=headers, base_url='https://localhost')

try:
    st = mk(UserRole.STUDENT, 'etu'); pr = mk(UserRole.PROFESSOR, 'prof')
    cache_delete(session_key(st.id)); cache_delete(_device_key(st.id))

    print('\n== Cookie d\'appareil ==')
    a = client(); r = login(a, st.email)
    check("connexion étudiante réussie", r.status_code == 200, r.get_data(as_text=True)[:150])
    check("le cookie d'appareil est posé (étudiant)", has_cookie(a, 'cei_device'))
    p = client(); rp = login(p, pr.email)
    check("pas de cookie d'appareil pour un enseignant", rp.status_code == 200 and not has_cookie(p, 'cei_device'))

    print('\n== Session CEI valide ==')
    r = moodle(a)
    check("session ouverte : accès direct (page relais)", r.status_code == 200 and '/lti/enter' in r.get_data(as_text=True))

    print('\n== Session remplacée ailleurs (autre appareil) ==')
    b = client(); rb = login(b, st.email, force=True)
    check("l'autre appareil se connecte en remplaçant la session", rb.status_code == 200)
    r = moodle(a)
    loc = r.headers.get('Location', '')
    check("appareil connu mais autre session active : page de confirmation CEI (pas Keycloak)",
          r.status_code == 302 and '/login?' in loc and 'sso_conflict=1' in loc and 'khaamelou' not in loc and '/api/auth/oidc/login' not in loc, loc[:160])
    check("le conflit mène à la confirmation, avec le moyen de continuer", 'retry_token=' in loc)

    print('\n== Session remplacée puis libérée ==')
    cache_delete(session_key(st.id))   # l'autre appareil s'est déconnecté / sa session a expiré
    r = moodle(a)
    check("appareil connu, aucune autre session : entrée directe sans mot de passe", r.status_code == 200 and '/lti/enter' in r.get_data(as_text=True),
          f"{r.status_code} {r.headers.get('Location','')[:100]}")
    r2 = moodle(a, to='/exam/175')
    check("l'examen visé est conservé", r2.status_code == 200 and 'exam' in r2.get_data(as_text=True))

    print('\n== Cas refusés (retour à la connexion UNCHK) ==')
    cache_delete(session_key(st.id))
    r = moodle(client())
    check("appareil inconnu → connexion UNCHK", r.status_code == 302 and '/api/auth/oidc/login' in r.headers.get('Location', ''))
    tok = ck(a, 'cei_device').value if ck(a, 'cei_device') else ''
    d = client(); d.set_cookie('cei_device', tok, domain='localhost', path='/api/auth')
    check("(le jeton de test existe)", bool(tok))
    r = moodle(d, headers={'User-Agent': 'AutreNavigateur/9'})
    check("même cookie mais autre navigateur → connexion UNCHK", r.status_code == 302 and '/api/auth/oidc/login' in r.headers.get('Location', ''))
    f = client(); f.set_cookie('cei_device', tok[:-6] + 'AAAAAA', domain='localhost', path='/api/auth')
    r = moodle(f)
    check("jeton falsifié → connexion UNCHK", r.status_code == 302 and '/api/auth/oidc/login' in r.headers.get('Location', ''))

    print('\n== Révocation ==')
    cache_delete(session_key(st.id))
    cache_delete(session_key(st.id)); cache_delete(_device_key(st.id))
    a2 = client(); ra = login(a2, st.email)
    r = a2.post('/api/auth/logout', headers={**UA, 'Authorization': 'Bearer ' + ra.get_json()['access_token']}, base_url='https://localhost')
    check("la déconnexion réussit", r.status_code == 200, r.status_code)
    r = moodle(a2)
    check("déconnexion : l'appareil n'est plus reconnu", r.status_code == 302 and '/api/auth/oidc/login' in r.headers.get('Location', ''), r.headers.get('Location', '')[:100])
    cache_delete(session_key(st.id))
    e = client(); login(e, st.email)
    cache_delete(session_key(st.id))
    from auth_paseto import revoke_all_devices
    revoke_all_devices(st.id)
    r = moodle(e)
    check("changement de mot de passe (révocation) : appareil plus reconnu", r.status_code == 302 and '/api/auth/oidc/login' in r.headers.get('Location', ''))

    print('\n== Compte désactivé ==')
    cache_delete(session_key(st.id))
    g = client(); login(g, st.email); cache_delete(session_key(st.id))
    s.expire_all(); u = s.get(User, st.id); u.is_active = False; s.commit()
    r = moodle(g)
    check("compte désactivé : jamais reconnu", r.status_code == 302 and '/api/auth/oidc/login' in r.headers.get('Location', ''))
finally:
    s.rollback()
    for uid in ids:
        cache_delete(session_key(uid)); cache_delete(_device_key(uid))
    s.query(TokenBlocklist).filter(TokenBlocklist.user_id.in_(ids)).delete(synchronize_session=False)
    s.query(User).filter(User.id.in_(ids)).delete(synchronize_session=False)
    s.commit(); s.close()
    try:
        from models import StaffAuditLog
        t = get_session(); t.query(StaffAuditLog).filter(StaffAuditLog.target_name.like('%@test.invalid')).delete(synchronize_session=False); t.commit(); t.close()
    except Exception:
        pass

ok = sum(1 for _, v in RESULTS if v)
print(f'\n{ok}/{len(RESULTS)} vérifications réussies')
sys.exit(0 if ok == len(RESULTS) else 1)
