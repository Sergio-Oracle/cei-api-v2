"""Générateur de charge RÉALISTE contre la production (depuis une machine distante, par le vrai réseau/nginx/TLS).

    /tmp/loadenv/bin/python scripts/charge_prod/run.py /tmp/charge_ctx.json [--base https://cei.unchk.sn] [--ramp 20] [--steady 240] [--submit 30]

Phases : (1) RUÉE : tous les étudiants démarrent dans la fenêtre « ramp » (démarrage, 2 rechargements, affichage de la copie, lecture du sujet) ;
(2) RÉGIME : chacun fait ce que fait une vraie page d'examen (heartbeat 20 s, sauvegarde 30 s, messages 30 s, sujet 30 s, événements) pendant que les surveillants
et l'administrateur rafraîchissent leurs pages comme en vrai ; (3) REMISE : tous remettent leur copie dans la fenêtre « submit ».
Arrêt automatique si la santé de la plateforme se dégrade (sonde de santé lente ou en erreur) ou si un vrai examen apparaît (vérifié par ssh).
"""
import asyncio, aiohttp, json, random, sys, time, statistics, argparse, subprocess, collections

ap = argparse.ArgumentParser(); ap.add_argument('ctx', nargs='?', default=''); ap.add_argument('--base', default='https://cei.unchk.sn'); ap.add_argument('--ramp', type=float, default=20)
ap.add_argument('--steady', type=float, default=240); ap.add_argument('--submit', type=float, default=30); ap.add_argument('--n', type=int, default=0)
ap.add_argument('--ssh', default='-i /root/.ssh/id_ed25519_unchk -p 3120 serge@196.207.246.205'); ap.add_argument('--no-guard', action='store_true'); ap.add_argument('--shard', default='0/1'); ap.add_argument('--dump', default=''); ap.add_argument('--aggregate', nargs='*'); ap.add_argument('--resolve', default=''); ap.add_argument('--local-server', action='store_true')
A = ap.parse_args()
SK, SM = (int(x) for x in A.shard.split('/'))
ctx = json.load(open(A.ctx)) if not A.aggregate else {'exam': 0, 'stu': [], 'sv': []}; BASE = A.base.rstrip('/'); EX = ctx['exam']
_all = ctx['stu'][:A.n] if A.n else ctx['stu']; STU = _all[SK::SM]; N = len(STU)      # la charge est répartie entre plusieurs PROCESSUS : un seul processus Python peut saturer sa propre machine et fausser les mesures
ctx['sv'] = ctx['sv'][SK::SM]
M = collections.defaultdict(list)            # étiquette → [(ms, statut)]
PH = {'phase': 'prépa'}
abort = asyncio.Event(); abort_reason = []
T0 = time.perf_counter()

async def call(sess, label, method, path, tok, body=None, timeout=30):
    t = time.perf_counter()
    try:
        async with sess.request(method, BASE + path, json=body if method != 'GET' else None, headers={'Authorization': 'Bearer ' + tok}, timeout=aiohttp.ClientTimeout(total=timeout)) as r:
            data = await r.read(); st = r.status
        M[(PH['phase'], label)].append(((time.perf_counter() - t) * 1000, st))
        try: return st, json.loads(data)
        except Exception: return st, {}
    except Exception as e:
        M[(PH['phase'], label)].append(((time.perf_counter() - t) * 1000, 'ERR'))
        return 0, {}

async def student(sess, i, t_submit_at):
    tok = STU[i]
    await asyncio.sleep(random.random() * A.ramp)
    st, d = await call(sess, 'start', 'POST', f'/api/online_exams/{EX}/start', tok, {})
    if st not in (200, 201): return
    aid = d['attempt']['id']
    for _ in range(2): await call(sess, 'reprise', 'POST', f'/api/online_exams/{EX}/start', tok, {})
    await call(sess, 'seen', 'POST', f'/api/exam_attempts/{aid}/seen', tok, {})
    await call(sess, 'subject', 'GET', f'/api/exam_attempts/{aid}/subject', tok)
    PH['phase'] = PH['phase'] if PH['phase'] != 'ruée' else PH['phase']
    nxt = {'hb': random.random() * 20, 'save': random.random() * 30, 'msg': random.random() * 30, 'subj': random.random() * 30, 'log': random.random() * 60, 'ev': random.random() * 120}
    every = {'hb': 20, 'save': 30, 'msg': 30, 'subj': 30, 'log': 60, 'ev': 120}
    base = time.perf_counter()
    while not abort.is_set():
        now = time.perf_counter() - base
        if time.perf_counter() - T0 >= t_submit_at: break
        due = min(nxt, key=nxt.get)
        if nxt[due] > now: await asyncio.sleep(min(nxt[due] - now, 1.0)); continue
        nxt[due] = now + every[due] * (0.9 + random.random() * 0.2)
        if due == 'hb': await call(sess, 'heartbeat', 'POST', f'/api/exam_attempts/{aid}/heartbeat', tok, {})
        elif due == 'save': await call(sess, 'save', 'POST', f'/api/exam_attempts/{aid}/save', tok, {'answers': json.dumps({'1': random.choice('ABC')})})
        elif due == 'msg': await call(sess, 'pending_messages', 'GET', f'/api/exam_attempts/{aid}/pending_messages', tok)
        elif due == 'subj': await call(sess, 'subject (relève)', 'GET', f'/api/exam_attempts/{aid}/subject', tok)
        elif due == 'log': await call(sess, 'log_activity', 'POST', f'/api/exam_attempts/{aid}/log_activity', tok, {'event_type': 'window_blur', 'event_data': 'charge'})
        else: await call(sess, 'proctoring_event', 'POST', f'/api/exam_attempts/{aid}/proctoring_event', tok, {'event_type': 'gaze_away', 'event_data': 'charge'})
    if abort.is_set(): return
    await asyncio.sleep(max(0, t_submit_at - (time.perf_counter() - T0)) + random.random() * A.submit)
    PH['phase'] = 'remise'
    await call(sess, 'submit', 'POST', f'/api/exam_attempts/{aid}/submit', tok, {'answers': json.dumps({'1': 'A'})}, timeout=60)

async def poller(sess, label, method, path, tok, every, until):
    await asyncio.sleep(random.random() * every)
    while not abort.is_set() and time.perf_counter() - T0 < until:
        await call(sess, label, method, path, tok, {} if method == 'POST' else None)
        await asyncio.sleep(every * (0.9 + random.random() * 0.2))

canary = []
async def canary_loop(sess, until):
    bad = 0
    while not abort.is_set() and time.perf_counter() - T0 < until:
        t = time.perf_counter()
        try:
            async with sess.get(BASE + '/api/health', timeout=aiohttp.ClientTimeout(total=10)) as r: await r.read(); ok = r.status == 200
        except Exception: ok = False
        ms = (time.perf_counter() - t) * 1000; canary.append((ms, ok))
        last = canary[-8:]
        if len(last) >= 8 and (sum(1 for m, o in last if not o) >= 4 or statistics.median([m for m, _ in last]) > 2500):
            abort_reason.append(f'sonde de santé dégradée ({ms:.0f} ms) — test arrêté pour protéger la plateforme'); abort.set()
        await asyncio.sleep(2)

def ssh(cmd, t=40):
    try: return subprocess.run((['bash', '-c', cmd] if A.local_server else ['ssh', *A.ssh.split(), cmd]), capture_output=True, text=True, timeout=t).stdout.strip()
    except Exception: return ''
res = []
async def guard_loop(until):
    while not abort.is_set() and time.perf_counter() - T0 < until:
        if not A.no_guard and SK == 0:
            out = await asyncio.get_running_loop().run_in_executor(None, ssh, f"cd /home/serge/projet-cei/cei-api-v2 && .venv-base/bin/python /tmp/charge_prod/guard.py {EX} 2>/dev/null | tail -1")
            if out.startswith('REEL'):
                abort_reason.append('un vrai examen est apparu : ' + out); abort.set()
        out = await asyncio.get_running_loop().run_in_executor(None, ssh, "read l1 l5 l15 _ < /proc/loadavg; vmstat 1 2 | tail -1 | awk '{print $15}'; free -m | awk '/Mem/{print $7}'; echo $l1")
        try:
            p = out.split('\n'); res.append({'t': round(time.perf_counter() - T0), 'idle': float(p[0]), 'mem_dispo_mo': int(p[1]), 'load1': float(p[2])})
        except Exception: pass
        await asyncio.sleep(10)

def pct(v, q): v = sorted(v); return v[min(len(v) - 1, int(len(v) * q))]
def report():
    print(f"\n===== RÉSULTAT — {N} étudiants, {len(ctx['sv'])} surveillants, durée {time.perf_counter() - T0:.0f} s =====")
    if abort_reason: print('⚠ ARRÊT :', abort_reason[0])
    print(f"{'phase':8s} {'appel':20s} {'n':>7s} {'succès':>8s} {'p50':>7s} {'p90':>7s} {'p99':>8s} {'max':>8s}  statuts")
    worst_p99, errs, total = 0, 0, 0
    for (ph, lab), v in sorted(M.items(), key=lambda kv: (['ruée', 'régime', 'remise', 'prépa'].index(kv[0][0]) if kv[0][0] in ('ruée', 'régime', 'remise', 'prépa') else 9, kv[0][1])):
        ms = [x for x, _ in v]; sts = collections.Counter(str(s) for _, s in v)
        okn = sum(c for s, c in sts.items() if s.startswith('2')); total += len(v); errs += len(v) - okn
        worst_p99 = max(worst_p99, pct(ms, .99)) if lab not in ('submit',) else worst_p99
        print(f"{ph:8s} {lab:20s} {len(v):7d} {100 * okn / len(v):7.2f}% {statistics.median(ms):6.0f}ms {pct(ms, .9):6.0f}ms {pct(ms, .99):7.0f}ms {max(ms):7.0f}ms  {dict(sts.most_common(3))}")
    if canary:
        cm = [m for m, o in canary]; print(f"\nSonde de santé du site (toutes les 2 s) : {len(canary)} mesures, p50 {statistics.median(cm):.0f} ms, p99 {pct(cm, .99):.0f} ms, échecs {sum(1 for _, o in canary if not o)}")
    if res: print(f"Serveur de production : processeur libre mini {min(r['idle'] for r in res):.0f} %, charge max {max(r['load1'] for r in res):.1f}, mémoire disponible mini {min(r['mem_dispo_mo'] for r in res) / 1024:.0f} Go")
    rate = 100 * errs / max(total, 1)
    print(f"\nERREURS : {errs}/{total} ({rate:.3f} %) ; pire p99 hors remise : {worst_p99:.0f} ms")
    print('VERDICT :', 'RÉUSSI' if (not abort_reason and rate < 0.5 and worst_p99 < 3000) else 'À EXAMINER')

async def main():
    import socket
    class _Fixed(aiohttp.abc.AbstractResolver):          # --resolve IP : joindre le serveur par l'adresse indiquée (ex. 127.0.0.1 quand le générateur tourne SUR le serveur), certificat et nom inchangés
        async def resolve(self, host, port=0, family=socket.AF_INET): return [{'hostname': host, 'host': A.resolve, 'port': port, 'family': family, 'proto': 0, 'flags': 0}]
        async def close(self): pass
    conn = aiohttp.TCPConnector(limit=0, limit_per_host=0, ttl_dns_cache=300, enable_cleanup_closed=True, **({'resolver': _Fixed()} if A.resolve else {}))
    async with aiohttp.ClientSession(connector=conn) as sess:
        t_submit_at = A.ramp + A.steady
        PH['phase'] = 'ruée'
        tasks = [asyncio.create_task(student(sess, i, t_submit_at)) for i in range(N)]
        until = t_submit_at + A.submit + 20
        tasks.append(asyncio.create_task(canary_loop(sess, until))); tasks.append(asyncio.create_task(guard_loop(until)))
        for k, tok in enumerate(ctx['sv']):
            for label, path, ev, m in (('sv active_proctoring', f'/api/online_exams/{EX}/active_proctoring', 8, 'GET'), ('sv student_messages', f'/api/online_exams/{EX}/student_messages', 20, 'GET'),
                                       ('sv agent/alerts', f'/api/agent/alerts?exam_id={EX}', 15, 'GET'), ('sv readiness', f'/api/online_exams/{EX}/readiness', 60, 'GET'),
                                       ('sv proctor_heartbeat', f'/api/online_exams/{EX}/proctor_heartbeat', 30, 'POST')):
                tasks.append(asyncio.create_task(poller(sess, label, m, path, tok, ev, until)))
        for k in range(3 if SK == 0 else 0):                                         # 3 administrateurs/enseignants qui regardent l'examen
            for label, path, ev in (('adm active_proctoring', f'/api/online_exams/{EX}/active_proctoring', 8), ('adm vitals', f'/api/online_exams/{EX}/vitals', 60), ('adm readiness', f'/api/online_exams/{EX}/readiness', 60), ('adm agent/alerts', f'/api/agent/alerts?exam_id={EX}', 15)):
                tasks.append(asyncio.create_task(poller(sess, label, 'GET', path, ctx['adm'], ev, until)))
        async def phases():
            await asyncio.sleep(A.ramp); PH['phase'] = 'régime'
            await asyncio.sleep(A.steady)
        await phases()
        await asyncio.gather(*tasks, return_exceptions=True)
def dump(path):
    json.dump({'M': {f'{k[0]}|{k[1]}': v for k, v in M.items()}, 'canary': canary, 'res': res, 'abort': abort_reason, 'N': N, 'sv': len(ctx['sv']), 'dur': time.perf_counter() - T0}, open(path, 'w'))

def aggregate(files):
    global N, canary, res, abort_reason
    tot_n = tot_sv = 0; dur = 0
    for f in files:
        d = json.load(open(f)); tot_n += d['N']; tot_sv += d['sv']; dur = max(dur, d['dur']); canary += [tuple(x) for x in d['canary']]; res += d['res']; abort_reason += d['abort']
        for k, v in d['M'].items(): ph, lab = k.split('|', 1); M[(ph, lab)] += [tuple(x) for x in v]
    ctx['sv'] = [0] * tot_sv; N = tot_n
    global T0; T0 = time.perf_counter() - dur
    report()

if A.aggregate: aggregate(A.aggregate)
else:
    asyncio.run(main())
    if A.dump: dump(A.dump)
    else: report()
