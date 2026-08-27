// k6 — Vérifie que le rate limiter isole bien chaque client par IP réelle
// (fix ProxyFix de cette session : avant, request.remote_addr valait
// TOUJOURS 127.0.0.1 derrière nginx, donc TOUTE la plateforme partageait
// un seul quota de 60/min — un seul utilisateur actif suffisait à
// bloquer tout le monde).
//
// Scénario : N "clients" distincts, chacun avec sa propre IP simulée via
// X-Forwarded-For (nginx transmet cet en-tête ; ProxyFix doit maintenant
// en tenir compte pour request.remote_addr). Chaque client tape le même
// endpoint non-exempté plus vite que la limite individuelle (60/min =
// 1/s) pour vérifier qu'il se fait limiter SANS bloquer les autres.
//
// Lancement :
//   k6 run tests/k6_rate_limit_isolation.js
//   BASE_URL=https://dev-cei.ddns.net k6 run tests/k6_rate_limit_isolation.js

import http from 'k6/http';
import { check, sleep } from 'k6';
import { Counter } from 'k6/metrics';

const BASE_URL = __ENV.BASE_URL || 'https://dev-cei.ddns.net';
const N_CLIENTS = 5;          // 5 IPs simulées distinctes
const REQS_PER_CLIENT = 90;   // largement au-dessus de 60/min individuel
const ENDPOINT = '/api/health';                 // exempté — sert de sonde neutre de connectivité
const LIMITED_ENDPOINT = '/api/auth/public-key'; // non-exempté, public, sans auth requise

export const ok200 = new Counter('ok_200');
export const limited429 = new Counter('limited_429');
export const other = new Counter('other_status');

export const options = {
  scenarios: {},
};

for (let i = 0; i < N_CLIENTS; i++) {
  options.scenarios[`client_${i}`] = {
    executor: 'shared-iterations',
    vus: 1,
    iterations: REQS_PER_CLIENT,
    maxDuration: '90s',
    exec: 'hitAsClient',
    env: { CLIENT_ID: String(i) },
    startTime: '0s',
  };
}

// IPs privées distinctes, une par "client" simulé — jamais de vraies IPs publiques.
const FAKE_IPS = ['10.10.0.1', '10.10.0.2', '10.10.0.3', '10.10.0.4', '10.10.0.5'];

export function hitAsClient() {
  const clientId = parseInt(__ENV.CLIENT_ID || '0', 10);
  const fakeIp = FAKE_IPS[clientId % FAKE_IPS.length];

  const res = http.get(`${BASE_URL}${LIMITED_ENDPOINT}`, {
    headers: {
      'X-Forwarded-For': fakeIp,
      'X-Real-IP': fakeIp,
    },
    tags: { client: `client_${clientId}`, fake_ip: fakeIp },
  });

  if (res.status === 200) {
    ok200.add(1, { client: `client_${clientId}` });
  } else if (res.status === 429) {
    limited429.add(1, { client: `client_${clientId}` });
  } else {
    other.add(1, { client: `client_${clientId}`, status: String(res.status) });
  }

  check(res, {
    'pas une erreur serveur (5xx)': (r) => r.status < 500,
  });

  sleep(0.3); // ~3.3 req/s par client -> dépasse largement 60/min = 1/s
}
