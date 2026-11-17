#!/bin/bash
# Montée en charge du SFU (srv-sfu-cei) : supprime les PLAFONDS SYSTÈME qui empêchent de nouveaux participants de se connecter (17/11/2026 : limite de 1 024 fichiers
# ouverts pendant l'examen 192, puis tempête de reconnexions). À lancer en ROOT sur le serveur SFU.
#
#   bash sfu_scale_root.sh            # AUDIT : affiche les plafonds actuels et ce qui serait changé ; ne modifie RIEN
#   bash sfu_scale_root.sh apply      # applique (sysctl, systemd, nginx), recharge nginx ; ne redémarre PAS LiveKit
#   RESTART=1 bash sfu_scale_root.sh apply   # redémarre aussi LiveKit si aucun examen n'est en cours (refuse sinon ; FORCE=1 pour passer outre)
#
# Idempotent : relancé, il ne change plus rien. Sauvegardes dans /root/sfu-scale-backup-<date> (JAMAIS dans /etc/nginx/sites-enabled ou conf.d : nginx les relit).
# Tests : ROOT=/tmp/bac DRY=1 (fichiers factices, aucune commande système).
set -u
MODE="${1:-plan}"
ROOT="${ROOT:-}"
DRY="${DRY:-0}"
NOFILE=1048576
TS="$(date +%Y%m%d_%H%M%S)"
BK="${ROOT}/root/sfu-scale-backup-$TS"
CHANGED=0
say()  { printf '%s\n' "$*"; }
doit() { if [ "$DRY" = 1 ] || [ "$MODE" != apply ]; then :; else eval "$@"; fi; }       # audit : les lignes « écrire / fixer » ci-dessus disent ce qui SERAIT fait
backup() { [ -f "$1" ] && { mkdir -p "$BK"; cp -p "$1" "$BK/$(echo "$1" | tr / _)"; }; }

if [ "$DRY" != 1 ] && [ "$(id -u)" != 0 ]; then say "ERREUR : à lancer en root."; exit 1; fi
say "== SFU : plafonds système ($MODE) — $(date -u '+%F %T UTC')"

# ── 1. État actuel ────────────────────────────────────────────────────────────
LKPID="$(pgrep -f livekit-server 2>/dev/null | head -1 || true)"
say "-- LiveKit"
if [ -n "$LKPID" ] && [ -r "/proc/$LKPID/limits" ]; then
  say "   fichiers ouverts autorisés : $(awk '/Max open files/{print $4}' /proc/$LKPID/limits) | utilisés : $(ls /proc/$LKPID/fd 2>/dev/null | wc -l)"
else say "   (processus livekit-server non trouvé)"; fi
CONN="$(ss -Htn state established '( sport = :7880 or sport = :7881 or sport = :443 )' 2>/dev/null | wc -l)"
say "   connexions établies (443/7880/7881) : $CONN"
LKYAML="${ROOT}/etc/livekit/livekit.yaml"
if [ -f "$LKYAML" ]; then
  say "   livekit.yaml : $(grep -E '^\s*(port|tcp_port|udp_port|port_range_start|port_range_end|use_external_ip|max_participants|num_tracks|bytes_per_sec)\s*:' "$LKYAML" | tr -s ' ' | tr '\n' ';')"
fi

# ── 2. Limites systemd (LiveKit, nginx) : LimitNOFILE / TasksMax ────────────────────────────
for unit in livekit nginx; do
  say "-- unité $unit"
  d="${ROOT}/etc/systemd/system/${unit}.service.d"; f="$d/zz-cei-scale.conf"
  want=$'[Service]\nLimitNOFILE='"$NOFILE"$'\nLimitNPROC=infinity\nTasksMax=infinity\n'
  if [ -f "$f" ] && [ "$(cat "$f")" = "${want%$'\n'}" ]; then say "   déjà en place : $f"
  else
    say "   écrire $f (LimitNOFILE=$NOFILE, LimitNPROC=infinity, TasksMax=infinity)"; CHANGED=1
    doit "mkdir -p '$d' && backup '$f'; printf '%s' '$want' > '$f'"
  fi
done

# ── 3. Noyau : fichiers, files d'attente TCP, ports, tampons UDP, suivi de connexions ────────────
SYSCTL="${ROOT}/etc/sysctl.d/99-cei-sfu-scale.conf"
read -r -d '' SC <<'EOF' || true
# CEI — SFU : montée en charge (10 000+ participants). Géré par deploy/sfu_scale_root.sh
fs.file-max = 4194304
fs.nr_open = 1048576
net.core.somaxconn = 65535
net.core.netdev_max_backlog = 250000
net.ipv4.tcp_max_syn_backlog = 65535
net.ipv4.ip_local_port_range = 1024 65535
net.ipv4.tcp_fin_timeout = 15
net.ipv4.tcp_tw_reuse = 1
net.core.rmem_max = 26214400
net.core.wmem_max = 26214400
net.core.rmem_default = 1048576
net.core.wmem_default = 1048576
net.ipv4.udp_mem = 3145728 4194304 8388608
net.ipv4.udp_rmem_min = 16384
net.ipv4.udp_wmem_min = 16384
net.netfilter.nf_conntrack_max = 2097152
net.netfilter.nf_conntrack_tcp_timeout_established = 86400
net.netfilter.nf_conntrack_udp_timeout_stream = 60
EOF
say "-- noyau (sysctl)"
if [ -f "$SYSCTL" ] && [ "$(cat "$SYSCTL")" = "$SC" ]; then say "   déjà en place : $SYSCTL"
else say "   écrire $SYSCTL puis « sysctl --system »"; CHANGED=1; doit "backup '$SYSCTL'; mkdir -p '$(dirname "$SYSCTL")'; printf '%s\n' \"\$SC\" > '$SYSCTL'; sysctl -e --system >/dev/null"; fi
if [ -r /proc/sys/net/netfilter/nf_conntrack_count ]; then
  say "   suivi de connexions : $(cat /proc/sys/net/netfilter/nf_conntrack_count) utilisées / max actuel $(cat /proc/sys/net/netfilter/nf_conntrack_max)"
  hs=/sys/module/nf_conntrack/parameters/hashsize
  [ -z "$ROOT" ] && [ -w "$hs" ] && doit "echo 262144 > $hs"
fi

# ── 4. nginx devant LiveKit : fichiers par processus et connexions simultanées ───────────────────
NGX="${ROOT}/etc/nginx/nginx.conf"
say "-- nginx"
if [ -f "$NGX" ]; then
  curr_rl="$(grep -E '^\s*worker_rlimit_nofile' "$NGX" | head -1 | tr -s ' ')"; curr_wc="$(grep -E '^\s*worker_connections' "$NGX" | head -1 | tr -s ' ')"
  say "   actuel : ${curr_rl:-(worker_rlimit_nofile absent)} | ${curr_wc:-(worker_connections absent)}"
  if ! grep -qE "^\s*worker_rlimit_nofile\s+$NOFILE;" "$NGX" || ! grep -qE '^\s*worker_connections\s+65535;' "$NGX"; then
    say "   fixer worker_rlimit_nofile $NOFILE et worker_connections 65535 (sauvegarde hors de sites-enabled/conf.d)"; CHANGED=1
    doit "backup '$NGX'; if grep -qE '^\s*worker_rlimit_nofile' '$NGX'; then sed -i -E 's/^\s*worker_rlimit_nofile\s+[0-9]+;/worker_rlimit_nofile $NOFILE;/' '$NGX'; else sed -i '1i worker_rlimit_nofile $NOFILE;' '$NGX'; fi; if grep -qE '^\s*worker_connections' '$NGX'; then sed -i -E 's/^\s*worker_connections\s+[0-9]+;/\tworker_connections 65535;/' '$NGX'; else say '   ATTENTION : pas de worker_connections dans nginx.conf — à ajouter dans events {}'; fi"
  else say "   déjà en place"; fi
  # limites volontaires de débit/connexions par IP sur le SFU : à retirer pour les étudiants derrière une même IP
  if grep -rEn 'limit_req|limit_conn' "${ROOT}/etc/nginx/nginx.conf" "${ROOT}/etc/nginx/sites-enabled" "${ROOT}/etc/nginx/conf.d" 2>/dev/null | grep -v '^\s*#' | grep -q .; then
    say "   ATTENTION : limit_req / limit_conn présents (à retirer ou relever pour le SFU) :"; grep -rEn 'limit_req|limit_conn' "${ROOT}/etc/nginx/nginx.conf" "${ROOT}/etc/nginx/sites-enabled" "${ROOT}/etc/nginx/conf.d" 2>/dev/null | grep -v '#' | cut -c1-150 | sed 's/^/      /'
  fi
  if ! grep -rEq 'proxy_read_timeout\s+([3-9][0-9]{3,}|[0-9]{5,})' "${ROOT}/etc/nginx/sites-enabled" 2>/dev/null; then
    say "   NOTE : proxy_read_timeout < 1 h pour le WebSocket LiveKit ? (recommandé : proxy_read_timeout 3600s; proxy_send_timeout 3600s;)"
  fi
  NGXBK="$BK/$(echo "$NGX" | tr / _)"
  doit "if nginx -t; then systemctl reload nginx; else echo 'ERREUR : nginx -t échoue, restauration de la configuration d origine'; [ -f '$NGXBK' ] && cp -p '$NGXBK' '$NGX' && nginx -t; fi"
else say "   $NGX introuvable"; fi

# ── 5. Ce qu'il reste à régler À LA MAIN (pas automatisable sans risque) ─────────────────────────
cat <<'EOF'
-- À vérifier / décider à la main
   1. livekit.yaml › rtc : la plage UDP (port_range_start/end) donne UN port par participant : 50000-60000 = 10 001 ports, donc AU PLAFOND exactement
      pour 10 000 participants en UDP. Deux solutions : élargir la plage (ex. 20000-60000, à faire ouvrir par la DITSI) OU un seul port UDP partagé
      (rtc: udp_port: 7882, à faire ouvrir), puis redémarrer LiveKit. Le repli TCP 7881 est un seul port et n'est pas concerné.
   2. livekit.yaml : aucune section « limit: » restrictive (num_tracks, bytes_per_sec, subscription_limit_*), ni room.max_participants.
   3. Pare-feu (ufw) : actif ; si le suivi de connexions sature, envisager d'exempter 7881/tcp et la plage UDP du suivi (raw/NOTRACK).
   4. UNE salle de 10 000 participants sur UN nœud n'est pas réaliste : répartir en plusieurs salles (≈ 500 étudiants par salle) et, au-delà d'un nœud,
      un cluster LiveKit (Redis). Mesurer avec un test de charge AVANT l'examen (livekit-cli load-test).
EOF

# ── 6. Redémarrage de LiveKit (seulement si demandé et si personne n'est connecté) ─────────────────────────
if [ "$MODE" = apply ] && [ "${RESTART:-0}" = 1 ] && [ "$DRY" != 1 ]; then
  if [ "$CONN" -gt 20 ] && [ "${FORCE:-0}" != 1 ]; then
    say "!! $CONN connexions établies : un examen est probablement en cours. LiveKit N'EST PAS redémarré (FORCE=1 pour passer outre)."
  else
    systemctl daemon-reload && systemctl restart livekit && sleep 4 && systemctl is-active livekit | sed 's/^/livekit : /'
    P="$(pgrep -f livekit-server | head -1)"; [ -n "$P" ] && awk '/Max open files/{print "fichiers ouverts autorisés après redémarrage : "$4}' /proc/$P/limits
  fi
elif [ "$MODE" = apply ] && [ "$DRY" != 1 ]; then
  systemctl daemon-reload
  say "(LiveKit non redémarré : les nouvelles limites systemd ne s'appliquent à son processus qu'au prochain redémarrage — RESTART=1 hors examen)"
fi
say "== terminé ($([ "$CHANGED" = 1 ] && echo 'des changements étaient nécessaires' || echo 'rien à changer'))"
