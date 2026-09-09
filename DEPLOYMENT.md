# Topologie d'exécution CEI

Référence rapide : quel processus écoute où, quel fichier nginx le sert, sur
chaque environnement. À tenir à jour dès qu'un bind / port / service change.

Le **dev et la prod sont volontairement alignés** : mêmes noms d'`upstream`,
même transport (TCP loopback), mêmes ports. Seuls diffèrent `server_name`,
les chemins de certificat, le nombre d'instances front et le pooler DB.

## Backends (gunicorn, `app:app`, `gunicorn.conf.py` + surcharges `GUNICORN_*`)

| Service systemd | Bind | Rôle |
|---|---|---|
| `cei-api-v2` | `127.0.0.1:8091` | API principale (tout `/api/` sauf le poll) |
| `cei-api-v2-notif` | `127.0.0.1:8092` | `/api/notifications/poll` uniquement — pool isolé (6 workers × 14 threads, I/O-bound). Voir `routes/notifications.py`. |

## Front (Next.js standalone, `node .next/standalone/server.js`)

6 instances **des deux côtés**, une par port, servant le même
`.next/standalone/` :

| Service systemd | Port |
|---|---|
| `cei-next` | `127.0.0.1:5175` |
| `cei-next-2` … `cei-next-6` | `127.0.0.1:5176` … `5180` |

- Config par `Environment=PORT=` + `HOSTNAME=127.0.0.1` dans chaque unit.
- dev : `User=root`, `WorkingDirectory=/root/cei-next`, node via nvm, `.env.local`
  fournit `NEXT_PUBLIC_API_URL` ; prod : `User=serge`,
  `/home/serge/projet-cei/cei-next`, `/usr/bin/node`, URL API figée au build.
- Rebuild + restart des 6 : dev `cei-next/build-local.sh`,
  prod `cei-next/deploy-to-prod.sh`.

- Le bind vient de `Environment=GUNICORN_BIND=` dans l'unit (défaut du fichier
  `gunicorn.conf.py` = `unix:/run/cei-api-v2.sock`, **surchargé** dans les deux
  units pour du TCP).
- `preload_app = True` → un changement de code Python exige un **`restart`**
  complet, pas un `reload` (le master ne réimporte rien sur SIGHUP).

## nginx

| Env | Fichier vhost | `server_name` |
|---|---|---|
| dev | `/etc/nginx/sites-available/dev-cei.conf` | `dev-cei.ddns.net` |
| prod | `/etc/nginx/sites-available/cei` | `cei.unchk.sn` |

`upstream` (mêmes noms des deux côtés), avec `keepalive` → nécessite
`proxy_http_version 1.1` + `proxy_set_header Connection ""` dans chaque
`location` (déjà en place) :

```nginx
upstream cei_api_upstream   { server 127.0.0.1:8091; keepalive 64; }
upstream cei_notif_upstream { server 127.0.0.1:8092; keepalive 32; }
# prod a aussi cei_next_upstream (6 instances Next) ; dev sert Next en direct.
```

Front : `upstream cei_next_upstream { least_conn; server 127.0.0.1:5175 … :5180;
keepalive 64; }` **des deux côtés** (6 instances). Nécessite le `map
$http_upgrade $connection_upgrade` (défini en tête du vhost) pour que le
keepalive coexiste avec l'upgrade WebSocket.

| `location` | `proxy_pass` |
|---|---|
| `= /api/notifications/poll` | `http://cei_notif_upstream` |
| `/api/` | `http://cei_api_upstream` |
| `/` (front) | `http://cei_next_upstream` |

`/api/notifications/poll` est un `location =` (exact) → matché avant `/api/`
quel que soit l'ordre dans le fichier.

## Redis

Un seul Redis local (`127.0.0.1:6379`). Clés notifications : `cei:notif:user:{id}`
= **liste** (RPUSH côté `notif_bus`, drain LRANGE+LTRIM côté endpoint), 50 max,
TTL 1 h. (Historique : c'était un canal Pub/Sub jusqu'au 09/09.)

## Postgres

| Env | Accès |
|---|---|
| dev | direct `127.0.0.1:5432` |
| prod | via PgBouncer `127.0.0.1:6432` (`pool_mode=transaction`), backe sur `5432` |

## Divergences dev / prod à connaître

- **Accès** : dev = ce serveur en direct (Bash root). prod = SSH `serge@…:3120`,
  sudo NOPASSWD **restreint** (`systemctl restart/reload cei-api-v2`,
  `restart cei-next*` seulement — **pas** `cei-api-v2-notif`, pas d'écriture
  `/etc/`). Détails d'accès + rituel de déploiement : hors de ce fichier.
- **Assets vision** (`/mediapipe/ /models/ /vendor/`) : prod = `alias` nginx
  direct ; dev = proxifiés via Next (perf moindre, sans impact fonctionnel).
