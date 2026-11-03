#!/bin/bash
# Remet en marche l'ENREGISTREMENT des étudiants (bouton REC du tableau de bord de surveillance).
#
# Contexte (03/11/2026) : depuis le déplacement de LiveKit sur srv-sfu-cei (29/10), le service
# d'enregistrement (livekit-egress) est resté sur la production, branché sur le Redis local de la
# production et sur un LiveKit local qui n'existe plus. LiveKit (ici) n'a ni Redis ni egress :
# toute demande d'enregistrement expirait après 8 s -> « Le service d'enregistrement est
# momentanément indisponible » (constaté sur l'examen 149, 10 h 41).
#
# À lancer EN ROOT sur srv-sfu-cei (102.36.138.58), en deux temps :
#   sudo bash /home/serge/sfu-install/sfu_egress_install_root.sh plan
#   sudo bash /home/serge/sfu-install/sfu_egress_install_root.sh apply
#
# Ce que fait « apply » (idempotent, sauvegardes dans /root/backup_egress_<date>) :
#   1. installe redis-server (écoute 127.0.0.1 uniquement) et docker.io ;
#   2. ajoute « redis: 127.0.0.1:6379 » à /etc/livekit/livekit.yaml et REDÉMARRE livekit
#      (coupe toutes les connexions en cours : refusé s'il y a des participants, sauf FORCE=1) ;
#   3. écrit /etc/livekit/egress.yaml (mêmes clés que livekit.yaml, ws://127.0.0.1:7880, Redis local) ;
#   4. lance le conteneur livekit-egress, MÊME image (digest) que celle qui fonctionnait en
#      production, réseau hôte, redémarrage automatique, journaux limités.
# Les fichiers MP4 continuent d'être envoyés vers MinIO (stockage de la production) par l'adresse
# publique https://storage-cei.unchk.sn, fournie par l'API dans chaque demande : rien à changer côté API.
# Retour arrière : docker rm -f livekit-egress ; retirer le bloc « redis: » de livekit.yaml
# (sauvegarde) ; systemctl restart livekit.
set -euo pipefail

MODE="${1:-plan}"
LK_YAML="${LK_YAML:-/etc/livekit/livekit.yaml}"
EGRESS_YAML="${EGRESS_YAML:-/etc/livekit/egress.yaml}"
IMAGE="${IMAGE:-livekit/egress@sha256:14c1222a0d4a1e440e2cb7e3d53429a5c1a2c9277abae7ac79be336ea32fea25}"
METRICS="${METRICS:-http://127.0.0.1:6789/metrics}"
DRY="${DRY:-0}"                     # DRY=1 : ni apt, ni docker, ni systemctl (tests)
BACKUP="/root/backup_egress_$(date +%Y%m%d_%H%M%S)"

[ "$MODE" = "plan" ] || [ "$MODE" = "apply" ] || { echo "usage : $0 plan|apply"; exit 2; }
[ "$DRY" = "1" ] || [ "$EUID" -eq 0 ] || { echo "À lancer avec sudo/root."; exit 1; }

say() { echo; echo "=== $* ==="; }
run() { if [ "$MODE" = "apply" ] && [ "$DRY" != "1" ]; then "$@"; else echo "  [$([ "$MODE" = plan ] && echo plan || echo dry)] $*"; fi; }

say "État actuel"
[ -f "$LK_YAML" ] || { echo "ERREUR : $LK_YAML introuvable"; exit 1; }
HAS_REDIS_CONF=0; grep -qE '^redis:' "$LK_YAML" && HAS_REDIS_CONF=1
echo "  livekit.yaml : section redis $([ $HAS_REDIS_CONF = 1 ] && echo présente || echo ABSENTE)"
echo "  redis-server : $(command -v redis-server >/dev/null && echo installé || echo absent)"
echo "  docker       : $(command -v docker >/dev/null && echo installé || echo absent)"
echo "  egress.yaml  : $([ -f "$EGRESS_YAML" ] && echo présent || echo absent)"
PARTICIPANTS="$( (curl -s -m 5 "$METRICS" 2>/dev/null || true) | awk '/^livekit_participant_total/{s+=$2} END{print s+0}')"
echo "  participants connectés à LiveKit : $PARTICIPANTS"

# clé/secret : première entrée du bloc « keys: » de livekit.yaml (jamais affichés)
read -r KEY_NAME KEY_SECRET < <(awk '/^keys:/{f=1;next} f && /^[[:space:]]+[^#[:space:]][^:]*:/{k=$0; sub(/^[[:space:]]+/,"",k); n=k; sub(/:.*/,"",n); v=k; sub(/^[^:]*:[[:space:]]*/,"",v); gsub(/"/,"",v); print n, v; exit}' "$LK_YAML") || true
[ -n "${KEY_NAME:-}" ] && [ -n "${KEY_SECRET:-}" ] || { echo "ERREUR : bloc keys: introuvable dans $LK_YAML"; exit 1; }
echo "  clé LiveKit utilisée : $KEY_NAME (secret masqué)"

say "1/4 Paquets (redis-server, docker.io)"
if ! command -v redis-server >/dev/null || ! command -v docker >/dev/null; then
  run env DEBIAN_FRONTEND=noninteractive apt-get update -qq
  run env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq redis-server docker.io
else echo "  déjà installés"; fi
if [ "$MODE" = "apply" ] && [ "$DRY" != "1" ]; then
  # Redis : local seulement (aucun accès réseau), démarré au boot
  grep -qE '^bind 127\.0\.0\.1' /etc/redis/redis.conf || sed -i -E 's/^bind .*/bind 127.0.0.1 -::1/' /etc/redis/redis.conf
  systemctl enable --now redis-server docker
  [ "$(redis-cli ping)" = "PONG" ] || { echo "ERREUR : Redis ne répond pas"; exit 1; }
  ss -ltn | awk '{print $4}' | grep -E ':6379$' | grep -vE '^(127\.0\.0\.1|\[::1\]):6379$' && { echo "ERREUR : Redis écoute hors localhost"; exit 1; } || true
  echo "  redis : PONG, écoute locale uniquement"
fi

say "2/4 LiveKit -> Redis"
if [ "$HAS_REDIS_CONF" = "1" ]; then echo "  déjà configuré"; else
  if [ "$PARTICIPANTS" -gt 0 ] && [ "${FORCE:-0}" != "1" ]; then
    echo "  REFUS : $PARTICIPANTS participant(s) connecté(s) — le redémarrage de LiveKit les couperait."
    echo "  Relancer hors examen, ou FORCE=1 pour passer outre."
    [ "$MODE" = "apply" ] && exit 1
  fi
  if [ "$MODE" = "apply" ]; then
    mkdir -p "$BACKUP"; cp -a "$LK_YAML" "$BACKUP/livekit.yaml"
    printf '\n# Requis pour l'"'"'enregistrement (egress) : file de demandes partagée avec le service livekit-egress.\nredis:\n  address: 127.0.0.1:6379\n' >> "$LK_YAML"
    echo "  bloc redis ajouté (sauvegarde : $BACKUP/livekit.yaml)"
  else echo "  [plan] bloc « redis: 127.0.0.1:6379 » ajouté, puis livekit redémarré (coupure des connexions en cours)"; fi
  run systemctl restart livekit
  if [ "$MODE" = "apply" ] && [ "$DRY" != "1" ]; then
    sleep 3; systemctl is-active livekit || { echo "ERREUR : livekit ne démarre pas — restaurer $BACKUP/livekit.yaml"; exit 1; }
  fi
fi

say "3/4 egress.yaml"
EGRESS_BODY="$(cat <<EOF
# Service d'enregistrement LiveKit (egress) sur srv-sfu-cei — généré par sfu_egress_install_root.sh
api_key: $KEY_NAME
api_secret: $KEY_SECRET
ws_url: ws://127.0.0.1:7880
redis:
  address: 127.0.0.1:6379
logging:
  level: info
EOF
)"
if [ "$MODE" = "apply" ]; then
  mkdir -p "$(dirname "$EGRESS_YAML")"
  [ -f "$EGRESS_YAML" ] && { mkdir -p "$BACKUP"; cp -a "$EGRESS_YAML" "$BACKUP/egress.yaml"; }
  umask 077; printf '%s\n' "$EGRESS_BODY" > "$EGRESS_YAML"; chmod 0600 "$EGRESS_YAML"
  echo "  $EGRESS_YAML écrit (0600)"
else echo "  [plan] $EGRESS_YAML serait écrit (clé $KEY_NAME, ws://127.0.0.1:7880, Redis local)"; fi

say "4/4 Conteneur livekit-egress"
if [ "$DRY" != "1" ] && command -v docker >/dev/null && docker ps --format '{{.Names}}' | grep -qx livekit-egress; then
  echo "  déjà en marche"
else
  run docker pull "$IMAGE"
  run docker rm -f livekit-egress
  run docker run -d --name livekit-egress --restart unless-stopped --network host \
      --shm-size=1g --cap-add SYS_ADMIN \
      --log-driver json-file --log-opt max-size=50m --log-opt max-file=3 \
      -e EGRESS_CONFIG_FILE=/egress.yaml -v "$EGRESS_YAML":/egress.yaml:ro "$IMAGE"
fi
if [ "$MODE" = "apply" ] && [ "$DRY" != "1" ]; then
  sleep 6
  docker ps --filter name=livekit-egress --format '  conteneur : {{.Names}} {{.Status}}'
  docker logs livekit-egress 2>&1 | grep -E "service ready|cpu available" | tail -2 | cut -c1-160 | sed 's/^/  /'
  redis-cli pubsub channels '*' | grep -c Egress | sed 's/^/  canaux Egress dans Redis : /'
fi

say "Terminé ($MODE)"
[ "$MODE" = "apply" ] && echo "Dites-le à Claude : il testera un enregistrement complet (salle de test, fichier MP4 dans MinIO) puis arrêtera l'ancien egress de la production."
true
