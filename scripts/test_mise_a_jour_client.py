#!/usr/bin/env python
"""Application CEI Examen : liste des versions avec les fichiers de MISE À JOUR AUTOMATIQUE (signés), et leur téléchargement.
    cd cei-api-v2 && .venv-base/bin/python scripts/test_mise_a_jour_client.py"""
import sys, os, json, tempfile, hashlib, hmac
sys.path.insert(0, '.')
D = tempfile.mkdtemp(prefix='cei-dist-')
os.environ['CLIENT_DIST_DIR'] = D
from app import app
RES = []
def chk(n, ok, d=''):
    RES.append(bool(ok)); print(('  ✔ ' if ok else '  ✘ ') + n + (f'  — {str(d)[:200]}' if d and not ok else ''))
def put(name, data=b'x' * 100):
    open(os.path.join(D, name), 'wb').write(data); return hashlib.sha256(data).hexdigest()
setup, appimg = 'CEI-Examen-1.3.0-win-setup.exe', 'CEI-Examen-1.3.0-linux-x86_64.AppImage'
h1, h2, h3 = put(setup), put(appimg), put('CEI-Examen-1.3.0-universal-mac.zip')
sig = lambda n, h: hmac.new(b'k', f'update|1.3.0|{n}|{h}'.encode(), hashlib.sha256).hexdigest()
json.dump({'version': '1.3.0', 'files': [{'name': setup, 'platform': 'win32', 'arch': 'x64', 'kind': 'installer', 'label': 'Windows', 'sha256': h1}],
           'updates': [{'name': setup, 'platform': 'win32', 'arch': 'x64', 'mode': 'nsis', 'sha256': h1, 'sig': sig(setup, h1)},
                       {'name': appimg, 'platform': 'linux', 'arch': 'x64', 'mode': 'appimage', 'sha256': h2, 'sig': sig(appimg, h2)},
                       {'name': 'CEI-Examen-1.3.0-universal-mac.zip', 'platform': 'darwin', 'arch': 'universal', 'mode': 'mac-zip', 'sha256': h3, 'sig': sig('CEI-Examen-1.3.0-universal-mac.zip', h3)},
                       {'name': 'absent.zip', 'platform': 'darwin', 'arch': 'universal', 'mode': 'mac-zip', 'sha256': h3, 'sig': 'ab'},
                       {'name': appimg, 'platform': 'linux', 'arch': 'x64', 'mode': 'appimage', 'sha256': h2}]}, open(os.path.join(D, 'latest.json'), 'w'))
c = app.test_client()
j = c.get('/api/exam-client/releases').get_json()
chk('la liste des versions annonce la 1.3.0', j['version'] == '1.3.0')
ups = {u['name']: u for u in j['updates']}
chk("les 3 fichiers de mise à jour signés sont annoncés (Windows, AppImage, macOS)", set(ups) == {setup, appimg, 'CEI-Examen-1.3.0-universal-mac.zip'}, list(ups))
chk("chaque fichier annonce taille, somme et signature", all(u['size'] == 100 and u['sha256'] and u['sig'] and u['mode'] for u in ups.values()))
chk("le téléchargement des mises à jour passe par nginx (/static/downloads/…), pas par l'API", all(u['url'] == '/static/downloads/' + u['name'] for u in ups.values()))
chk("un fichier absent du disque ou sans signature n'est jamais annoncé", 'absent.zip' not in ups and len(j['updates']) == 3)
chk("la page de téléchargement ne voit que les vraies versions (pas l'archive macOS de mise à jour)", [f['name'] for f in j['files']] == [setup])
r = c.get(f'/api/exam-client/download/{appimg}')
chk("un fichier de mise à jour (hors liste des versions à télécharger) se télécharge", r.status_code == 200 and len(r.data) == 100, r.status_code)
chk("un fichier non listé est refusé", c.get('/api/exam-client/download/secret.txt').status_code == 404)
chk("une tentative de sortie du dossier est refusée", c.get('/api/exam-client/download/..%2Fapp.py').status_code == 404)
ok = sum(RES); print(f'\n{ok}/{len(RES)} vérifications réussies'); sys.exit(0 if ok == len(RES) else 1)
