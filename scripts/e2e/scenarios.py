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
        for name, fn in (('1', s1_entree_et_reprise), ('2', s2_camera_et_abonnements), ('3', s3_test_technique), ('4', s4_remise_et_autorisations)):
            if only and name not in only: continue
            try: fn(br)
            except Exception as e: check(f'scénario {name} : exécution sans erreur', False, repr(e))
        br.close()
    bad = [n for n, ok in RES if not ok]
    print(f"\n{len(RES) - len(bad)}/{len(RES)} vérifications réussies" + (f" — ÉCHECS : {bad}" if bad else ''))
    sys.exit(1 if bad else 0)
