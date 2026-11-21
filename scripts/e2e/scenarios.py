"""Répétition générale EN NAVIGATEUR RÉEL — rejoue les défauts du 17/11/2026 et vérifie qu'ils ne reviennent pas.

Lancée par scripts/repetition_navigateur.sh (qui crée puis supprime les comptes). Serveur LOCAL uniquement (jamais la production).
Variables : CEI_E2E_BASE (défaut https://dev-cei.ddns.net), CEI_E2E_CHROME (chemin de Chromium), CEI_E2E_RESOLVE (défaut : dev-cei.ddns.net → 127.0.0.1).
"""
import json, os, sys, time, glob, subprocess, urllib.request
from playwright.sync_api import sync_playwright

ctx = json.load(open(sys.argv[1])); BASE = os.environ.get('CEI_E2E_BASE', 'https://dev-cei.ddns.net'); PW = ctx['pw']
HOST = BASE.split('//')[1].split('/')[0]
CHROME = os.environ.get('CEI_E2E_CHROME') or (sorted(glob.glob('/root/.cache/ms-playwright/chromium-*/chrome-linux*/chrome')) or [None])[-1]
ROOM = f"exam-{ctx['exam']}"
UID = dict(zip(['adm'] + list(ctx['students']), ctx['uids']))
RES = []
PWH = []                      # objet Playwright (pour lancer un second navigateur dans un scénario)
def check(nom, ok, detail=''):
    RES.append((nom, bool(ok))); print(('  ✔ ' if ok else '  ✘ ') + nom + (f'  — {str(detail)[:200]}' if detail and not ok else ''), flush=True)

def lk_tracks(room=ROOM):
    """Pistes réellement publiées, vues par le serveur vidéo (API d'administration) : {identité: [TYPE/SOURCE]}."""
    import jwt as J
    from dotenv import dotenv_values
    v = dotenv_values(os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), '.env'))
    api = v['LIVEKIT_URL'].replace('wss://', 'https://').replace('ws://', 'http://').rstrip('/')
    tok = J.encode({'iss': v['LIVEKIT_API_KEY'], 'exp': int(time.time()) + 60, 'video': {'roomAdmin': True, 'room': room}}, v['LIVEKIT_API_SECRET'], algorithm='HS256')
    req = urllib.request.Request(api + '/twirp/livekit.RoomService/ListParticipants', data=json.dumps({'room': room}).encode(), headers={'Authorization': 'Bearer ' + tok, 'Content-Type': 'application/json'})
    ps = json.load(urllib.request.urlopen(req, timeout=15)).get('participants', [])
    return {p['identity']: sorted(t.get('type', '?') + ('/' + t.get('source', '') if t.get('source') else '') for t in p.get('tracks', [])) for p in ps}

INIT_BASE = "try{sessionStorage.setItem('cei_vision_mbps',JSON.stringify({v:5,t:Date.now()}))}catch(e){}"
INIT_RTC = """(() => { const O = window.RTCPeerConnection; window.__pcs = [];
  window.RTCPeerConnection = function(...a) { const pc = new O(...a); window.__pcs.push(pc); return pc }; window.RTCPeerConnection.prototype = O.prototype })();
window.__inb = async () => { const o = {audio: 0, video: 0, aStreams: 0, vStreams: 0};
  for (const pc of window.__pcs) { try { (await pc.getStats()).forEach(r => { if (r.type === 'inbound-rtp') { o[r.kind] += r.bytesReceived || 0; o[r.kind === 'audio' ? 'aStreams' : 'vStreams']++ } }) } catch (e) {} } return o }"""
# la PREMIÈRE publication de caméra échoue (cas réel du 17/11 : écran reçu, caméra jamais publiée)
INIT_FAIL_CAM = """(() => { let v, patched = false; window.__failed = 0;
  Object.defineProperty(window, 'LivekitClient', { configurable: true, set(x){ v = x }, get(){
    if (v && !patched && v.LocalParticipant) { patched = true; const P = v.LocalParticipant.prototype, o = P.publishTrack;
      P.publishTrack = function(t, opts){ if (t && t.kind === 'video' && !(opts && opts.name === 'screen') && window.__failed < 1) { window.__failed++; return Promise.reject(new Error('échec simulé')) } return o.call(this, t, opts) } }
    return v } })})();"""

def new_ctx(br, init=(), perms=('camera', 'microphone')):
    c = br.new_context(viewport={'width': 1366, 'height': 900}, ignore_https_errors=True, permissions=list(perms))
    for i in (INIT_BASE, *init): c.add_init_script(i)
    return c.new_page()
def login(pg, email):
    pg.goto(BASE + '/login'); pg.wait_for_timeout(4500)
    pg.fill('input[type=email]', email); pg.fill('input[type=password]', PW); pg.click('button[type=submit]')
    pg.wait_for_url(lambda u: '/login' not in u, timeout=25000)
def start_exam(pg, wait=4000, full=True):
    """Ouvre l'examen et clique « Démarrer ». full=True : franchit aussi l'étape d'autorisations (copie affichée) ; full=False : s'arrête avant l'affichage de la copie."""
    pg.goto(f"{BASE}/exam/{ctx['exam']}"); pg.wait_for_timeout(6000)
    labels = ("J'accepte et je démarre", "Autoriser et commencer", "Commencer l'examen", "Continuer", "Commencer")
    for step in range(5 if full else 1):
        clicked = False
        for label in labels[(0 if step == 0 else 1):] if full else labels[:1]:
            b = pg.locator(f'button:has-text("{label}")')
            if b.count() and b.first.is_enabled():
                b.first.click(); clicked = True; pg.wait_for_timeout(wait); break
        if not clicked: break
    return True
text = lambda pg: pg.inner_text('body').replace('\n', ' | ')

def s1_entree_et_reprise(br):
    print('\n== 1. Entrée en examen : rechargements avant l\'affichage de la copie (attente de 7 à 45 min du 17/11) ==')
    pg = new_ctx(br); login(pg, ctx['students']['entree']); start_exam(pg, full=False)
    for k in range(3):
        pg.goto(f"{BASE}/exam/{ctx['exam']}"); pg.wait_for_timeout(5000); t = text(pg)
        check(f"rechargement {k + 1} : aucun code de reprise demandé", 'Reprise de l' not in t and 'code personnel' not in t, t[:120])
        b = pg.locator('button:has-text("J\'accepte et je démarre")')
        if b.count() and b.first.is_enabled(): b.first.click()
        pg.wait_for_timeout(2000)
    pg.goto(f"{BASE}/dashboard/student"); pg.wait_for_timeout(5000); t = text(pg)
    check("tableau de bord : « Votre examen vous attend » avec un seul bouton Reprendre", 'Votre examen vous attend' in t and 'Reprendre' in t, t[:160])
    check("… plus de « Vous avez quitté l'examen » ni de bouton d'appel au surveillant", 'quitté cet examen' not in t and 'Besoin d' not in t)
    pg.context.close()

def s2_camera_et_abonnements(br):
    print('\n== 2. Caméra jamais publiée + abonnements (vignettes noires, surveillants éjectés du 17/11) ==')
    pa = new_ctx(br, (INIT_FAIL_CAM,)); pb = new_ctx(br, (INIT_RTC,)); pc = new_ctx(br, (INIT_RTC,)); pm = new_ctx(br, (INIT_RTC,))
    login(pa, ctx['students']['cam']); start_exam(pa)
    login(pb, ctx['students']['sub1']); start_exam(pb); login(pc, ctx['students']['sub2']); start_exam(pc)
    login(pm, ctx['adm']); pm.goto(f"{BASE}/proctor/monitor/{ctx['exam']}"); pm.wait_for_timeout(12000)
    check("l'échec de publication de la caméra a bien été simulé", pa.evaluate('window.__failed') >= 1)
    ida, ok = f"student-{UID['cam']}", False
    for _ in range(12):
        tr = lk_tracks().get(ida, [])
        if any(t.startswith('VIDEO/CAMERA') for t in tr): ok = True; break
        pa.wait_for_timeout(3000)
    check("la caméra manquante est REPUBLIÉE automatiquement (moins de 36 s)", ok, lk_tracks().get(ida))
    pm.wait_for_timeout(8000)
    w = pm.evaluate("() => [...document.querySelectorAll('video[id^=video-student-]')].map(v => v.videoWidth)")
    check("la tuile du surveillant reçoit l'image de cet étudiant", any(x > 0 for x in w), w)
    ib, ic, im = pb.evaluate('window.__inb()'), pc.evaluate('window.__inb()'), pm.evaluate('window.__inb()')
    check("un étudiant ne reçoit JAMAIS l'audio ni la vidéo d'un autre étudiant (cause de la saturation du serveur vidéo)", ib['aStreams'] == 0 and ib['vStreams'] == 0 and ic['aStreams'] == 0 and ic['vStreams'] == 0, (ib, ic))
    check("le surveillant reçoit les vidéos des tuiles et AUCUN audio d'étudiant", im['vStreams'] >= 1 and im['aStreams'] == 0, im)
    for x in (pa, pb, pc, pm): x.context.close()

def s3_test_technique(br):
    print('\n== 3. Test technique avant l\'examen (caméra qui ne parvient pas au surveillant) ==')
    BAN = ('serveur', 'pare-feu', 'mbit', 'livekit', 'latence')
    p1 = new_ctx(br, perms=('camera', 'microphone', 'notifications')); login(p1, ctx['students']['tt1'])
    p1.goto(BASE + '/test-poste'); p1.wait_for_timeout(3000); p1.click('button:has-text("Lancer le test")')
    p1.wait_for_selector('button:has-text("Tester le partage")', timeout=60000); p1.click('button:has-text("Tester le partage")')
    for _ in range(30):
        p1.wait_for_timeout(2000)
        if p1.locator('text=Résultat enregistré').count(): break
    t = p1.inner_text('main')
    check("poste en règle : « Le résultat est enregistré : votre poste est PRÊT »", 'poste est PRÊT' in t, t[-300:])
    p2 = new_ctx(br, (INIT_FAIL_CAM.replace('window.__failed < 1', 'true'),), perms=('camera', 'microphone', 'notifications')); login(p2, ctx['students']['tt2'])
    p2.goto(BASE + '/test-poste'); p2.wait_for_timeout(3000); p2.click('button:has-text("Lancer le test")')
    p2.wait_for_selector('button:has-text("Tester le partage")', timeout=60000); p2.click('button:has-text("Tester le partage")')
    for _ in range(30):
        p2.wait_for_timeout(2000)
        if p2.locator('text=Résultat enregistré').count(): break
    t2 = p2.inner_text('main')
    check("caméra jamais reçue : « votre poste n'est PAS prêt »", "PAS prêt" in t2, t2[-300:])
    check("le message de l'étudiant ne contient AUCUN terme technique", not any(b in t2.lower() for b in BAN), [b for b in BAN if b in t2.lower()])
    pm = new_ctx(br); login(pm, ctx['adm']); pm.goto(f"{BASE}/proctor/monitor/{ctx['exam']}"); pm.wait_for_timeout(9000)
    try: pm.click('button:has-text("à contacter")'); pm.wait_for_timeout(800)
    except Exception: pass
    tm = pm.inner_text('body')
    check("le surveillant voit « Non prêt » avec une explication en langage simple", 'Non prêt' in tm and "n'arrive pas jusqu'à vous" in tm, tm[:200])
    check("… sans terme technique non plus", not any(b in tm.lower().split('non prêt', 1)[-1][:400] for b in BAN))
    for x in (p1, p2, pm): x.context.close()

def s5_test_poste_sans_connexion(br):
    print("\n== 5. Test du poste SANS connexion (la transmission réelle se vérifie sans compte ; rien n'est enregistré) ==")
    BAN = ('serveur vidéo', 'pare-feu', 'mbit', 'livekit', 'latence', 'techtest')
    pg = new_ctx(br, perms=('camera', 'microphone', 'notifications'))            # AUCUNE connexion : contexte neuf, aucun cookie
    pg.goto(BASE + '/test-poste'); pg.wait_for_timeout(3000)
    check("la page reste accessible sans connexion (aucun renvoi vers /login)", '/login' not in pg.url and '/test-poste' in pg.url, pg.url)
    pg.click('button:has-text("Lancer le test")')
    pg.wait_for_selector('button:has-text("Tester le partage")', timeout=60000); pg.click('button:has-text("Tester le partage")')
    for _ in range(30):
        pg.wait_for_timeout(2000)
        if pg.locator('text=bien transmis').count() or pg.locator('text=pas transmis').count() or pg.locator('text=pas pu démarrer').count(): break
    t = pg.inner_text('main')
    check("partage d'écran accepté sans connexion", "Écran entier partagé" in t, t[-260:])
    check("la transmission réelle est vérifiée SANS compte : « bien transmis »", 'bien transmis' in t and 'Non testé' not in t, t[-300:])
    check("l'étudiant est prévenu que le résultat n'est pas enregistré pour son surveillant", "n'est pas enregistré" in t and 'Se connecter' in t, t[-300:])
    check("aucun terme technique à l'écran", not any(b in t.lower() for b in BAN), [b for b in BAN if b in t.lower()])
    check("on est toujours sur la page (aucun renvoi vers la connexion en cours de test)", '/login' not in pg.url, pg.url)
    pg.context.close()

# Faux pont de l'application CEI Examen : enregistre chaque appel dans window.__ceiCalls ; WhatsApp (logiciel interdit) et le Bloc-notes (autre application) sont « ouverts » tant qu'on ne les ferme pas.
def fake_cei(stubborn=False):
    return """(() => { window.__ceiCalls = []; let wa = true, np = true; const rec = n => window.__ceiCalls.push(n);
  window.ceiClient = { isCEIClient: true,
    info: async () => ({ client: 'CEI', version: '1.9.0', platform: 'win32', vm: false }),
    status: async () => ({ examMode: false, displays: 1, focused: true, kiosk: false, shortcuts: [], vm: false }),
    sign: async () => ({ sig: 'x', version: '1.9.0', platform: 'win32', vm: false }),
    listApps: async () => { rec('listApps'); return { apps: np ? [{ name: 'Notepad' }] : [] } },
    closeApps: async () => { rec('closeApps'); if (%s) return { closed: 0, remaining: [{ name: 'Notepad' }] }; np = false; return { closed: 1, remaining: [] } },
    forbidden: async () => { rec('forbidden'); return { names: wa ? ['WhatsApp'] : [] } },
    stopForbidden: async () => { rec('stopForbidden'); wa = false; return { remaining: [], tried: ['WhatsApp'], helper: true } },
    enterExamMode: async () => { rec('enterExamMode'); return { ok: true } }, leaveExamMode: async () => ({ ok: true }),
    configure: async () => ({ ok: true }), ports: async () => ({ displays: 1, displayNames: [], usb: [] }) }; })();""" % ('true' if stubborn else 'false')

def s6_fermeture_au_clic(br):
    print("\n== 6. L'application CEI Examen ne ferme RIEN avant que l'étudiant veuille composer ==")
    pg = new_ctx(br, (fake_cei(),)); login(pg, ctx['students']['apps1'])
    pg.goto(f"{BASE}/exam/{ctx['exam']}"); pg.wait_for_timeout(11000)          # largement plus que l'ancien intervalle de 3 s : l'ancien code aurait déjà tout fermé
    calls = pg.evaluate('window.__ceiCalls')
    check("écran d'instructions : AUCUN arrêt de logiciel interdit ni fermeture d'application (WhatsApp reste utilisable)", not any(c in calls for c in ('stopForbidden', 'closeApps')), calls)
    check("… et aucune interrogation en boucle de l'application", not any(c in calls for c in ('forbidden', 'listApps', 'enterExamMode')), calls)
    b = pg.locator('button:has-text("J\'accepte et je démarre")')
    check("le bouton « Démarrer » n'est pas bloqué par les applications ouvertes", b.count() and b.first.is_enabled(), text(pg)[:200])
    b.first.click(); pg.wait_for_timeout(9000)
    calls = pg.evaluate('window.__ceiCalls')
    check("AU CLIC sur « Démarrer » : le logiciel interdit est arrêté puis les autres applications sont fermées", 'stopForbidden' in calls and 'closeApps' in calls and calls.index('stopForbidden') < calls.index('closeApps'), calls)
    check("… avant d'entrer en mode examen", 'enterExamMode' in calls and calls.index('closeApps') < calls.index('enterExamMode'), calls)
    pg.context.close()
    pg2 = new_ctx(br, (fake_cei(stubborn=True),)); login(pg2, ctx['students']['apps2'])
    pg2.goto(f"{BASE}/exam/{ctx['exam']}"); pg2.wait_for_timeout(6000)
    pg2.locator('button:has-text("J\'accepte et je démarre")').first.click(); pg2.wait_for_timeout(8000)
    t = text(pg2); calls = pg2.evaluate('window.__ceiCalls')
    check("une application qui refuse de se fermer : l'étudiant le voit et décide (l'examen n'entre pas en mode examen tout seul)", 'Notepad' in t and 'enterExamMode' not in calls, (t[:200], calls))
    pg2.context.close()

# clique, dans la page de surveillance, le bouton `title` de la carte de l'étudiant `name`
CLICK_IN_CARD = """([name, title]) => { const els = [...document.querySelectorAll('*')].filter(e => e.children.length === 0 && e.textContent.includes(name));
  for (const el of els) { let p = el; for (let i = 0; i < 10 && p; i++) { const b = p.querySelector('button[title="' + title + '"]'); if (b) { b.click(); return true } p = p.parentElement } } return false }"""

def s7_evenements_une_fois(br):
    print("\n== 7. Message et appel du surveillant : chaque événement n'arrive qu'UNE fois chez l'étudiant (relève de 30 s comprise) ==")
    name = 'ZZ RG dup1'; MSG = 'Merci de rester face à la caméra (essai automatique)'
    st = new_ctx(br, perms=('camera', 'microphone', 'notifications')); login(st, ctx['students']['dup1']); start_exam(st); st.wait_for_timeout(5000)
    sv = new_ctx(br); errs7 = []
    for pg_ in (st, sv): pg_.on('pageerror', lambda e: errs7.append(str(e)[:160]))        # une exception de page (ex. erreur React 310 : page blanche) doit faire échouer le scénario
    login(sv, ctx['adm']); sv.goto(f"{BASE}/proctor/monitor/{ctx['exam']}"); sv.wait_for_timeout(12000)
    check("la page de surveillance et la copie de l'étudiant s'ouvrent sans exception", not errs7, errs7[:2])
    # DEUX messages décalés : la relève du serveur a lieu toutes les 30 s à une phase inconnue ; l'ancienne protection ne tenait que 10 s, donc au moins l'un des deux tombait hors de sa portée
    for k, txt in enumerate(('Merci de rester face à la caméra (essai 1)', 'Gardez votre écran visible (essai 2)'), 1):
        ok = sv.evaluate(CLICK_IN_CARD, [name, 'Message'])
        check(f"message {k} : le surveillant trouve l'étudiant dans sa liste et ouvre « Message »", ok)
        if not ok: continue
        sv.fill('textarea', txt); sv.locator('button:has-text("Envoyer")').last.click()
        try: st.wait_for_selector("text=Message de l'Enseignant", timeout=20000); recu = True
        except Exception: recu = False
        check(f"message {k} : l'étudiant le reçoit", recu)
        if recu:
            st.locator('button:has-text("J\'ai compris")').first.click(); st.wait_for_timeout(500)
            st.wait_for_timeout(42000)                       # plus que la relève du serveur (30 s)
            check(f"message {k} : l'étudiant ne le revoit PAS après la relève du serveur", st.locator("text=Message de l'Enseignant").count() == 0)
    ok = sv.evaluate(CLICK_IN_CARD, [name, 'Appel privé'])
    check("le surveillant lance un appel privé", ok)
    if ok:
        try: st.wait_for_selector('text=Appel du Surveillant', timeout=20000); recu = True
        except Exception: recu = False
        check("l'étudiant voit la fenêtre d'appel", recu)
        if recu:
            st.locator('button:has-text("Refuser")').first.click(); st.wait_for_timeout(500)
            st.wait_for_timeout(42000)
            check("… et elle NE réapparaît PAS après la relève du serveur (30 s)", st.locator('text=Appel du Surveillant').count() == 0)
    check("aucune exception de page pendant tout le scénario", not errs7, errs7[:2])
    for x in (st, sv): x.context.close()

def s4_remise_et_autorisations(br):
    print('\n== 4. Remise de la copie sur réseau instable, et autorisations refusées ==')
    pg = new_ctx(br); login(pg, ctx['students']['remise']); start_exam(pg); pg.wait_for_timeout(6000)
    pg.route('**/api/exam_attempts/*/submit', lambda r: r.abort()); pg.route('**/api/exam_attempts/*/save', lambda r: r.abort())
    btn = pg.locator('button:has-text("Soumettre"), button:has-text("Terminer"), button:has-text("Rendre")')
    if btn.count():
        btn.first.click(); pg.wait_for_timeout(1500)
        for lab in ('Oui', 'Confirmer', 'Soumettre'):
            b = pg.locator(f'button:has-text("{lab}")')
            if b.count() > 1 or (b.count() and lab != 'Soumettre'):
                try: b.last.click(); break
                except Exception: pass
        pg.wait_for_timeout(5000); t = text(pg)
        check("réseau coupé : écran « Connexion instable… Ne fermez pas cette page » (exercices masqués)", 'Connexion instable' in t and 'Ne fermez pas' in t, t[:160])
        pg.unroute('**/api/exam_attempts/*/submit'); pg.unroute('**/api/exam_attempts/*/save')
        pg.locator('button:has-text("Réessayer maintenant")').click(); pg.wait_for_timeout(5000); t = text(pg)
        check("réseau rétabli : « Copie soumise ! » avec la date et l'heure de remise", 'Copie soumise' in t and ' à ' in t, t[:160])
    else:
        check("bouton de remise trouvé", False)
    pg.context.close()
    # Navigateur SANS acceptation automatique des autorisations : l'étudiant refuse réellement l'accès à la caméra
    br2 = PWH[0].chromium.launch(executable_path=CHROME, headless=True, args=['--use-fake-device-for-media-stream', f'--host-resolver-rules=MAP {HOST} 127.0.0.1', '--ignore-certificate-errors'])
    pp = new_ctx(br2, perms=()); login(pp, ctx['students']['perm']); start_exam(pp, 4000, full=False)
    try: pp.click('button:has-text("Autoriser et commencer")'); pp.wait_for_timeout(3500)
    except Exception: pass
    t = text(pp)
    check("autorisation refusée : message précis (cadenas / réglages) au lieu d'un message générique", 'cadenas' in t or 'Réglages' in t or 'Windows' in t, t[:200])
    check("… et le bouton devient « Réessayer »", pp.locator('button:has-text("Réessayer")').count() > 0)
    pp.context.close(); br2.close()

if __name__ == '__main__':
    only = sys.argv[2:] 
    with sync_playwright() as p:
        PWH.append(p)
        br = p.chromium.launch(executable_path=CHROME, headless=True, args=['--use-fake-device-for-media-stream', '--use-fake-ui-for-media-stream', f'--host-resolver-rules=MAP {HOST} 127.0.0.1', '--ignore-certificate-errors'])
        for name, fn in (('1', s1_entree_et_reprise), ('2', s2_camera_et_abonnements), ('3', s3_test_technique), ('5', s5_test_poste_sans_connexion), ('6', s6_fermeture_au_clic), ('7', s7_evenements_une_fois), ('4', s4_remise_et_autorisations)):
            if only and name not in only: continue
            try: fn(br)
            except Exception as e: check(f'scénario {name} : exécution sans erreur', False, repr(e))
        br.close()
    bad = [n for n, ok in RES if not ok]
    print(f"\n{len(RES) - len(bad)}/{len(RES)} vérifications réussies" + (f" — ÉCHECS : {bad}" if bad else ''))
    sys.exit(1 if bad else 0)
