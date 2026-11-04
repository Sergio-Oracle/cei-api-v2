#!/bin/bash
# Installe un serveur vidéo (LiveKit + enregistrement) DÉDIÉ À LA PRÉPRODUCTION sur srv-preprod-sfu-cei
# (102.36.139.35, preprod-sfu.unchk.sn), pour que la préproduction cesse d'utiliser le LiveKit de production.
#
# À lancer EN ROOT sur 102.36.139.35, en deux temps :
#   sudo bash /home/serge/sfu-install/sfu_preprod_install_root.sh plan
#   sudo bash /home/serge/sfu-install/sfu_preprod_install_root.sh apply
#
# Avant « apply », déposer dans /home/serge/sfu-install/ :
#   - cert.pem et key.pem : le certificat joker *.unchk.sn (le même que sur sfu-cei) — « plan » vérifie qu'il couvre
#     preprod-sfu.unchk.sn et que la clé correspond ;
#   - livekit-server : le binaire (même version que sfu-cei, 1.13.5) — sinon téléchargé depuis GitHub ;
#   - sfu_egress_install_root.sh : le script d'enregistrement déjà utilisé sur sfu-cei.
#
# Ce que fait « apply » (idempotent, sauvegardes dans /root/backup_sfu_preprod_<date>) :
#   1. installe nginx, redis-server (127.0.0.1 seulement) et docker.io ;
#   2. installe livekit-server dans /usr/local/bin, crée l'utilisateur « livekit » et le service systemd ;
#   3. génère une NOUVELLE paire clé/secret LiveKit (jamais affichée) dans /etc/livekit/livekit.yaml, et en dépose une copie
#      lisible par serge seul dans /home/serge/sfu-install/preprod_livekit_keys.env (pour brancher l'API de préproduction) ;
#   4. installe le certificat, écrit le site nginx (443 → LiveKit 127.0.0.1:7880, /metrics réservé à la supervision) ;
#   5. ouvre les ports dans ufw SI ufw est actif ;
#   6. lance sfu_egress_install_root.sh (service d'enregistrement : même image que la production).
# Ports à ouvrir par le réseau (DITSI) depuis Internet vers 102.36.139.35 :
#   TCP 443 (HTTPS/WSS), TCP 7881 (média sur TCP), UDP 7882 (média sur UDP), UDP 3478 + TCP 5349 (TURN).
# Retour arrière : systemctl disable --now livekit ; docker rm -f livekit-egress ; rm /etc/nginx/sites-enabled/sfu-preprod.
set -euo pipefail

MODE="${1:-plan}"
HOST="${HOST:-preprod-sfu.unchk.sn}"
LK_VERSION="${LK_VERSION:-1.13.5}"
SRC_DIR="${SRC_DIR:-/home/serge/sfu-install}"
LK_DIR="${LK_DIR:-/etc/livekit}"
LK_YAML="$LK_DIR/livekit.yaml"
CERT_DIR="$LK_DIR/certs"
BIN="${BIN:-/usr/local/bin/livekit-server}"
UNIT="${UNIT:-/etc/systemd/system/livekit.service}"
NGX_SITE="${NGX_SITE:-/etc/nginx/sites-available/sfu-preprod}"
KEY_NAME="${KEY_NAME:-cei-preprod}"
MONITOR_IP="${MONITOR_IP:-102.36.139.23}"       # srv-sendt (Prometheus)
DRY="${DRY:-0}"                                  # DRY=1 : rien n'est écrit ni exécuté (tests)
BACKUP="/root/backup_sfu_preprod_$(date +%Y%m%d_%H%M%S)"

[ "$MODE" = "plan" ] || [ "$MODE" = "apply" ] || { echo "usage : $0 plan|apply"; exit 2; }
[ "$DRY" = "1" ] || [ "$EUID" -eq 0 ] || { echo "À lancer avec sudo/root."; exit 1; }
LIVE=0; [ "$MODE" = "apply" ] && [ "$DRY" != "1" ] && LIVE=1

say() { echo; echo "=== $* ==="; }
run() { if [ "$LIVE" = 1 ]; then "$@"; else echo "  [$([ "$MODE" = plan ] && echo plan || echo dry)] $*"; fi; }

say "Contrôles préalables"
echo "  serveur : $(hostname) — $(. /etc/os-release; echo "$PRETTY_NAME"), $(nproc) cœurs, $(free -g | awk '/^Mem/{print $2}') Go RAM"
echo "  IP publique vue : $(curl -s -m 8 https://api.ipify.org || echo inconnue)"
echo "  nom $HOST -> $(getent hosts "$HOST" | awk '{print $1}' || true)"
PROBLEMS=0
SRC_CERT=""; SRC_KEY=""
for d in "$CERT_DIR" "$SRC_DIR"; do
  if [ -z "$SRC_CERT" ] && [ -f "$d/cert.pem" ] && [ -f "$d/key.pem" ]; then SRC_CERT="$d/cert.pem"; SRC_KEY="$d/key.pem"; fi
done
if [ -z "$SRC_CERT" ]; then
  echo "  ✘ certificat : cert.pem + key.pem introuvables ni dans $CERT_DIR ni dans $SRC_DIR"; PROBLEMS=1
else
  if openssl x509 -in "$SRC_CERT" -noout -checkhost "$HOST" 2>/dev/null | grep -q "does match"; then echo "  ✔ certificat $SRC_CERT couvre $HOST ($(openssl x509 -in "$SRC_CERT" -noout -enddate))"
  else echo "  ✘ le certificat $SRC_CERT ne couvre PAS $HOST"; PROBLEMS=1; fi
  A="$(openssl x509 -in "$SRC_CERT" -noout -pubkey 2>/dev/null | sha256sum | cut -c1-16)"; B="$(openssl pkey -in "$SRC_KEY" -pubout 2>/dev/null | sha256sum | cut -c1-16)"
  if [ "$A" = "$B" ]; then echo "  ✔ la clé privée correspond au certificat"; else echo "  ✘ la clé privée NE correspond PAS au certificat"; PROBLEMS=1; fi
fi
if [ -x "$BIN" ]; then echo "  livekit-server déjà installé : $("$BIN" --version 2>&1 | head -1)"
elif [ -f "$SRC_DIR/livekit-server" ]; then echo "  ✔ binaire fourni : $SRC_DIR/livekit-server (sha256 $(sha256sum "$SRC_DIR/livekit-server" | cut -c1-16)…)"
else echo "  binaire absent : il sera téléchargé (livekit $LK_VERSION, GitHub)"; fi
[ -f "$SRC_DIR/sfu_egress_install_root.sh" ] && echo "  ✔ script d'enregistrement présent" || { echo "  ✘ $SRC_DIR/sfu_egress_install_root.sh manquant"; PROBLEMS=1; }
ss -ltn 2>/dev/null | awk '{print $4}' | grep -E ':(80|443|7880|7881|6379)$' | sed 's/^/  ⚠ port déjà utilisé : /' || true
if [ "$PROBLEMS" = 1 ] && [ "$MODE" = "apply" ] && [ "$DRY" != "1" ]; then echo; echo "ERREUR : corriger les ✘ ci-dessus puis relancer."; exit 1; fi

say "1/6 Paquets (nginx, redis-server, docker.io)"
NEED=""; for p in nginx redis-server docker.io; do dpkg -s "$p" >/dev/null 2>&1 || NEED="$NEED $p"; done
if [ -n "$NEED" ]; then
  run env DEBIAN_FRONTEND=noninteractive apt-get update -qq
  run env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq $NEED
else echo "  déjà installés"; fi
if [ "$LIVE" = 1 ]; then
  grep -qE '^bind 127\.0\.0\.1' /etc/redis/redis.conf || sed -i -E 's/^bind .*/bind 127.0.0.1 -::1/' /etc/redis/redis.conf
  systemctl enable --now redis-server docker
  [ "$(redis-cli ping)" = "PONG" ] || { echo "ERREUR : Redis ne répond pas"; exit 1; }
  echo "  redis : PONG"
fi

say "2/6 LiveKit $LK_VERSION"
if [ -x "$BIN" ]; then echo "  binaire déjà en place"; else
  if [ -f "$SRC_DIR/livekit-server" ]; then run install -m 0755 "$SRC_DIR/livekit-server" "$BIN"
  else
    run bash -c "cd /tmp && curl -fsSL -o lk.tgz https://github.com/livekit/livekit/releases/download/v${LK_VERSION}/livekit_${LK_VERSION}_linux_amd64.tar.gz && tar -xzf lk.tgz livekit-server && install -m 0755 livekit-server '$BIN' && rm -f lk.tgz livekit-server"
  fi
fi
id livekit >/dev/null 2>&1 || run useradd --system --home /var/lib/livekit --shell /usr/sbin/nologin livekit
run install -d -m 0750 -o root -g livekit "$LK_DIR" "$CERT_DIR"
if [ "$LIVE" = 1 ] && [ ! -f "$UNIT" ]; then
cat > "$UNIT" <<EOF
[Unit]
Description=LiveKit server (préproduction CEI)
After=network-online.target redis-server.service
Wants=network-online.target

[Service]
User=livekit
ExecStart=$BIN --config $LK_YAML
Restart=always
RestartSec=3
LimitNOFILE=1048576

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload; echo "  service $UNIT créé"
else echo "  service : $([ -f "$UNIT" ] && echo déjà présent || echo '[plan] créé')"; fi

say "3/6 Certificat et configuration LiveKit (nouvelle clé générée, jamais affichée)"
if [ "$LIVE" = 1 ]; then
  mkdir -p "$BACKUP"
  if [ "$SRC_CERT" != "$CERT_DIR/cert.pem" ]; then
    install -m 0644 -o root -g livekit "$SRC_CERT" "$CERT_DIR/cert.pem"; install -m 0640 -o root -g livekit "$SRC_KEY" "$CERT_DIR/key.pem"
    echo "  certificat installé dans $CERT_DIR"
  fi
  if [ -f "$LK_YAML" ] && grep -q '^keys:' "$LK_YAML"; then echo "  $LK_YAML existe déjà avec des clés : conservé (sauvegarde $BACKUP)"; cp -a "$LK_YAML" "$BACKUP/livekit.yaml"
  else
    SECRET="$(openssl rand -hex 32)"
    umask 027
    cat > "$LK_YAML" <<EOF
# LiveKit — préproduction CEI (généré par sfu_preprod_install_root.sh)
port: 7880
bind_addresses:
  - 127.0.0.1
rtc:
  tcp_port: 7881
  udp_port: 7882
  use_external_ip: true
turn:
  enabled: true
  domain: $HOST
  tls_port: 5349
  udp_port: 3478
  cert_file: $CERT_DIR/cert.pem
  key_file: $CERT_DIR/key.pem
redis:
  address: 127.0.0.1:6379
prometheus:
  port: 6789
keys:
  $KEY_NAME: $SECRET
logging:
  level: info
EOF
    chown root:livekit "$LK_YAML"; chmod 0640 "$LK_YAML"
    # copie pour brancher l'API de préproduction : lisible par serge seul
    ( umask 077; printf 'LIVEKIT_API_KEY=%s\nLIVEKIT_API_SECRET=%s\nLIVEKIT_URL=wss://%s\n' "$KEY_NAME" "$SECRET" "$HOST" > "$SRC_DIR/preprod_livekit_keys.env" )
    chown serge:serge "$SRC_DIR/preprod_livekit_keys.env"; chmod 0600 "$SRC_DIR/preprod_livekit_keys.env"
    echo "  $LK_YAML écrit (clé « $KEY_NAME ») ; copie pour l'API : $SRC_DIR/preprod_livekit_keys.env (0600, serge)"
  fi
else echo "  [plan] certificat copié dans $CERT_DIR, $LK_YAML généré (ports 7880 local, 7881/tcp, 7882/udp, TURN 3478/udp + 5349/tls, Redis local, métriques 6789), clé aléatoire"; fi

say "4/6 nginx"
if [ "$LIVE" = 1 ]; then
  [ -f "$NGX_SITE" ] && cp -a "$NGX_SITE" "$BACKUP/sfu-preprod.nginx"
  cat > "$NGX_SITE" <<EOF
map \$http_upgrade \$connection_upgrade { default upgrade; '' close; }
server {
    listen 80;
    server_name $HOST;
    location /.well-known/acme-challenge/ { root /var/www/html; }
    location / { return 301 https://\$host\$request_uri; }
}
server {
    listen 443 ssl http2;
    server_name $HOST;
    ssl_certificate     $CERT_DIR/cert.pem;
    ssl_certificate_key $CERT_DIR/key.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    location /metrics {
        allow 127.0.0.1; allow $MONITOR_IP; deny all;
        proxy_pass http://127.0.0.1:6789;
    }
    location / {
        proxy_pass http://127.0.0.1:7880;
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection \$connection_upgrade;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
    }
}
EOF
  ln -sf "$NGX_SITE" /etc/nginx/sites-enabled/sfu-preprod; rm -f /etc/nginx/sites-enabled/default
  nginx -t && systemctl enable --now nginx && systemctl reload nginx && echo "  nginx rechargé"
else echo "  [plan] site $NGX_SITE : 80 → 443, 443 → 127.0.0.1:7880 (WebSocket), /metrics réservé à 127.0.0.1 et $MONITOR_IP"; fi

say "5/6 Démarrage de LiveKit et pare-feu local"
if [ "$LIVE" = 1 ]; then
  # LiveKit recommande un tampon UDP de 5 Mo (sinon « UDP receive buffer is too small » et paquets perdus sous charge)
  printf 'net.core.rmem_max=5000000\nnet.core.wmem_max=5000000\n' > /etc/sysctl.d/99-livekit.conf; sysctl -q -p /etc/sysctl.d/99-livekit.conf
  systemctl enable --now livekit; sleep 3
  systemctl is-active livekit || { echo "ERREUR : livekit ne démarre pas — journalctl -u livekit -n 30"; exit 1; }
  C="$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:7880/ || true)"; echo "  LiveKit local (7880) -> $C"
  C="$(curl -sk -o /dev/null -w '%{http_code}' --resolve "$HOST:443:127.0.0.1" "https://$HOST/" || true)"; echo "  HTTPS via nginx -> $C"
  if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
    for r in 3120/tcp 80/tcp 443/tcp 7881/tcp 7882/udp 3478/udp 5349/tcp; do ufw allow "$r" >/dev/null; done; echo "  ufw : ports ouverts (3120, 80, 443, 7881, 7882/udp, 3478/udp, 5349)"
  else echo "  ufw inactif : rien à ouvrir localement (le réseau est géré par la DITSI)"; fi
else echo "  [plan] livekit démarré ; ufw : ouvre 80, 443, 7881, 7882/udp, 3478/udp, 5349 seulement s'il est actif"; fi

say "6/6 Enregistrement (egress)"
if [ -f "$SRC_DIR/sfu_egress_install_root.sh" ]; then
  if [ "$LIVE" = 1 ]; then bash "$SRC_DIR/sfu_egress_install_root.sh" apply
  else echo "  [plan] sfu_egress_install_root.sh apply : Redis déjà configuré, egress.yaml (mêmes clés que livekit.yaml), conteneur livekit-egress (même image que la production)"; fi
fi

say "Terminé ($MODE)"
[ "$MODE" = "apply" ] && echo "Dites-le à Claude : il branchera l'API de préproduction sur ce serveur et testera une salle + un enregistrement de bout en bout."
true
