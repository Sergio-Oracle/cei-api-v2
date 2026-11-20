#!/usr/bin/env python
"""Pages PUBLIQUES, ouvertes par un visiteur NON connecté (navigateur vierge) : aucune ne doit renvoyer vers la connexion (20/11/2026).

    /tmp/pwenv/bin/python scripts/e2e/pages_publiques.py [URL_DU_SITE]          (défaut : https://cei.unchk.sn ; lecture seule, rien n'est créé)
    CEI_E2E_BASE=https://preprod-cei.unchk.sn /tmp/pwenv/bin/python scripts/e2e/pages_publiques.py
    CEI_E2E_LOCAL=1 /tmp/pwenv/bin/python scripts/e2e/pages_publiques.py https://dev-cei.ddns.net          (site de développement de ce serveur)

Défaut rattrapé : la page publique « Tester mon poste » (/test-poste) appelait /api/tech_test/me à son ouverture ; pour un visiteur non connecté la réponse 401 faisait renvoyer vers /login
par le client d'API (renvoi valable pour une page privée, faux pour une page publique). La répétition en navigateur ne testait que des utilisateurs connectés. Ce test rend ce défaut impossible à
laisser passer : il s'exécute APRÈS chaque livraison (deploy-to-preprod.sh / deploy-to-prod.sh) et tout renvoi vers la connexion échoue la livraison.
Les pages sont lues dans middleware.ts (une seule source de vérité) : une page publique ajoutée plus tard est testée sans rien modifier ici.
"""
import glob, os, re, sys
from playwright.sync_api import sync_playwright

CHROME = os.environ.get('CEI_E2E_CHROME') or (sorted(glob.glob('/root/.cache/ms-playwright/chromium-*/chrome-linux*/chrome')) or [None])[-1]     # même recherche que scenarios.py

BASE = (sys.argv[1] if len(sys.argv) > 1 else os.environ.get('CEI_E2E_BASE', 'https://cei.unchk.sn')).rstrip('/')
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MW = os.path.join(os.path.dirname(ROOT), 'cei-next', 'lib', 'publicPaths.ts')
if not os.path.exists(MW): MW = os.path.join(os.path.dirname(ROOT), 'cei-next', 'middleware.ts')
src = open(MW, encoding='utf-8').read()
block = re.search(r'PUBLIC_PATHS\s*=\s*new Set\(\[(.*?)\]\)', src, re.S).group(1)
PATHS = [p for p in re.findall(r"'(/[^']*)'", re.sub(r'//[^\n]*', '', block))]
# pages publiques par préfixe (guides) : une page réelle par préfixe de page
EXTRA = ['/guide-etudiant', '/guide-surveillant']
PATHS = sorted(set(PATHS + EXTRA))
SKIP = {'/robots.txt', '/security.txt'}          # fichiers, pas des pages
# /phone-camera et /reset-password sans paramètre affichent un message d'erreur de la page : elles ne doivent PAS pour autant quitter la page

ok_all = True; rows = []
with sync_playwright() as p:
    args = ['--use-fake-ui-for-media-stream', '--use-fake-device-for-media-stream', '--ignore-certificate-errors']
    host = BASE.split('://', 1)[-1].split('/')[0]
    if os.environ.get('CEI_E2E_LOCAL') == '1':                   # site de développement de CE serveur (comme scenarios.py)
        args.append(f'--host-resolver-rules=MAP {host} 127.0.0.1')
    b = p.chromium.launch(executable_path=CHROME, headless=True, args=args)
    for path in PATHS:
        if path in SKIP: continue
        ctx = b.new_context(ignore_https_errors=True, viewport={'width': 1280, 'height': 900})     # contexte neuf = aucun cookie, aucune session
        pg = ctx.new_page(); errs = []; apis = []
        pg.on('pageerror', lambda e: errs.append(str(e)[:120]))
        pg.on('response', lambda r: apis.append((r.status, r.url.split('/api/', 1)[-1][:40])) if '/api/' in r.url else None)
        try:
            pg.goto(BASE + path, wait_until='load', timeout=45000)
            pg.wait_for_timeout(3500)                   # laisse les appels d'API de la page se terminer (le renvoi se produisait ~1 s après l'ouverture)
            final = pg.url.replace(BASE, '').split('?')[0].split('#')[0] or '/'
            stayed = final.rstrip('/') == path.rstrip('/') or path == '/'
            redirected_login = final.startswith('/login') and path != '/login'
            body = pg.inner_text('body') if pg.query_selector('body') else ''
            rendered = len(body.strip()) > 80                  # une page publique qui s'ouvre BLANCHE est aussi un défaut
            ok = stayed and not redirected_login and rendered
            n401 = sum(1 for s, _ in apis if s == 401)
            rows.append((ok, path, final, n401, errs[:1]))
            ok_all &= ok
        except Exception as e:
            rows.append((False, path, f'ERREUR {str(e)[:60]}', 0, [])); ok_all = False
        ctx.close()
    # Parcours COMPLET de « Tester mon poste » sans connexion (caméra/micro simulés) : on clique sur « Lancer le test » et on vérifie que les contrôles s'affichent sans quitter la page
    parcours = None
    if '/test-poste' in PATHS:
        ctx = b.new_context(ignore_https_errors=True, viewport={'width': 1280, 'height': 900}, permissions=['camera', 'microphone'])
        pg = ctx.new_page()
        try:
            pg.goto(BASE + '/test-poste', wait_until='load', timeout=45000); pg.wait_for_timeout(2500)
            pg.get_by_role('button', name=re.compile('Lancer le test|Relancer le test')).click(timeout=15000)
            pg.wait_for_timeout(9000)
            final = pg.url.replace(BASE, '').split('?')[0]
            texte = pg.inner_text('body')
            nb = len(re.findall(r'Caméra|Micro|Connexion|Partage|Application|Système|Visioconférence', texte))
            parcours = [final.rstrip('/') == '/test-poste' and nb >= 3, final, nb, None]
            # Suite (20/11/2026) : le test de partage d'écran puis la transmission réelle doivent aussi marcher SANS connexion. Règle stricte : jamais « Non testé : connectez-vous ».
            # Un échec de transmission dû au RÉSEAU de la machine qui lance ce contrôle n'est qu'un avertissement (la page a bien tenté et dit pourquoi).
            try:
                pg.get_by_role('button', name=re.compile('Tester le partage')).click(timeout=15000)
                for _ in range(25):
                    pg.wait_for_timeout(2000)
                    t2 = pg.inner_text('body')
                    if 'bien transmis' in t2 or 'pas transmis' in t2 or 'pas pu démarrer' in t2 or 'Non testé' in t2: break
                live = 'ok' if 'bien transmis' in t2 else ('refus' if 'Non testé' in t2 else ('reseau' if ('pas transmis' in t2 or 'pas pu démarrer' in t2) else 'rien'))
                parcours[3] = live
                if live in ('refus', 'rien') or '/login' in pg.url: parcours[0] = False
            except Exception as e:
                parcours[3] = 'rien'; parcours[0] = False
        except Exception as e:
            parcours = [False, f'ERREUR {str(e)[:70]}', 0, None]
        ctx.close(); ok_all &= parcours[0]
    b.close()
print(f'Pages publiques sur {BASE} (visiteur non connecté)')
for ok, path, final, n401, errs in rows:
    print(f"  ✔ {path:30s} → reste sur la page" if ok else (f"  ✘ {path:30s} → RENVOYÉE VERS {final}" if final != path and path != '/' else f"  ✘ {path:30s} → page vide ou en erreur"))
    if n401 and ok: print(f"       (appels d'API répondant 401 sans conséquence : {n401})")
    if errs: print(f"       erreur de page : {errs[0]}")
if parcours:
    print(f"  {'✔' if parcours[0] else '✘'} /test-poste, parcours complet sans connexion (test, partage d'écran, transmission) → " + (f"{parcours[2]} contrôles affichés, reste sur la page" if parcours[0] else f"ÉCHEC ({parcours[1]}, {parcours[2]} contrôles, transmission : {parcours[3]})"))
    if parcours[0]: print({'ok': "       transmission réelle vérifiée sans compte : « bien transmis »", 'reseau': "       ⚠ transmission non aboutie depuis cette machine (réseau vers le serveur vidéo) : la page l'a signalé correctement, à surveiller", None: ''}.get(parcours[3], ''))
print('RÉSULTAT :', 'TOUTES LES PAGES PUBLIQUES RESTENT ACCESSIBLES' if ok_all else 'ÉCHEC — une page publique renvoie vers la connexion')
sys.exit(0 if ok_all else 1)
