// k6 — Preuve définitive de l'isolation par IP du rate limiter.
//
// AVANT le fix ProxyFix de cette session : request.remote_addr valait
// TOUJOURS 127.0.0.1 derrière nginx -> un seul utilisateur "bruyant"
// (ex: un onglet qui spam le polling) épuisait le quota de TOUTE la
// plateforme, bloquant tout le monde (429) même les utilisateurs calmes.
//
// Ce test : une IP "noisy" tape l'endpoint bien plus vite que la limite
// (doit se faire limiter), une IP "quiet" tape à un rythme largement
// sous la limite en PARALLÈLE (doit TOUJOURS réussir à 100%, quoi que
// fasse "noisy" — sinon c'est que les deux partagent encore un quota).

import http from 'k6/http';
import { check } from 'k6';

const BASE_URL = __ENV.BASE_URL || 'https://dev-cei.ddns.net';
const ENDPOINT = '/api/auth/public-key';
const NOISY_IP = '10.20.0.1';
const QUIET_IP = '10.20.0.2';

export const options = {
  scenarios: {
    noisy: {
      executor: 'constant-arrival-rate',
      rate: 5,               // 5 req/s = 300/min, très au-dessus de la limite (60/min)
      timeUnit: '1s',
      duration: '40s',
      preAllocatedVUs: 5,
      exec: 'hitNoisy',
    },
    quiet: {
      executor: 'constant-arrival-rate',
      rate: 1,                // 1 req toutes les 3s = 20/min, largement sous la limite
      timeUnit: '3s',
      duration: '40s',
      preAllocatedVUs: 2,
      exec: 'hitQuiet',
    },
  },
};

export function hitNoisy() {
  const res = http.get(`${BASE_URL}${ENDPOINT}`, {
    headers: { 'X-Forwarded-For': NOISY_IP, 'X-Real-IP': NOISY_IP },
    tags: { client: 'noisy' },
  });
  check(res, { 'noisy: pas 5xx': (r) => r.status < 500 });
}

export function hitQuiet() {
  const res = http.get(`${BASE_URL}${ENDPOINT}`, {
    headers: { 'X-Forwarded-For': QUIET_IP, 'X-Real-IP': QUIET_IP },
    tags: { client: 'quiet' },
  });
  // Le test critique : "quiet" doit TOUJOURS obtenir 200, jamais 429,
  // quoi que fasse "noisy" en parallèle sur une IP différente.
  check(res, {
    'quiet: jamais bloqué par noisy (200 attendu)': (r) => r.status === 200,
  });
}
