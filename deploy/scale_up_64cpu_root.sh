#!/bin/bash
# Montée en capacité de CEI après le passage de thieboudiene à 64 vCPU / 125 Go
# (03/11/2026). À lancer EN ROOT sur la production, en deux temps :
#
#   sudo bash scale_up_64cpu_root.sh plan    # n'affiche que l'état et ce qui serait changé
#   sudo bash scale_up_64cpu_root.sh apply   # applique (sauvegardes dans /root/backup_scale_<date>)
#
# Ce que fait « apply » (chaque étape est idempotente) :
#   1. PgBouncer : relève max_client_conn (2000) et la limite de fichiers ouverts du
#      service ; en mode « transaction », assure default_pool_size >= 80.
#      => condition pour passer l'API de 24 à 48 workers (GUNICORN_WORKERS dans .env).
#   2. Site Next.js : passe de 6 à 12 instances (cei-next-7 … cei-next-12, ports
#      5181-5186), copies exactes de cei-next-6, ajoutées à l'upstream nginx
#      (nginx -t avant rechargement, restauration automatique en cas d'échec).
#
# Rien d'autre n'est touché : PostgreSQL n'a pas besoin de réglage mémoire (base de
# 144 Mo, 99,96 % de lectures servies par le cache), Redis et nginx ont déjà de la marge.
# Retour arrière : voir les fichiers sauvegardés ; pour les instances,
#   systemctl disable --now cei-next-{7..12} puis retirer leurs lignes « server » de l'upstream.
set -euo pipefail

MODE="${1:-plan}"
PGB_INI="${PGB_INI:-/etc/pgbouncer/pgbouncer.ini}"
NGINX_CONF="${NGINX_CONF:-/etc/nginx/sites-available/cei}"
UNIT_DIR="${UNIT_DIR:-/etc/systemd/system}"
TEMPLATE_UNIT="${TEMPLATE_UNIT:-$UNIT_DIR/cei-next-6.service}"
FIRST_IDX="${FIRST_IDX:-7}"
LAST_IDX="${LAST_IDX:-12}"
BASE_PORT="${BASE_PORT:-5181}"            # cei-next-7 -> 5181 … cei-next-12 -> 5186
DRY="${DRY:-0}"                          # DRY=1 : n'appelle ni systemctl ni nginx (tests)
BACKUP="/root/backup_scale_$(date +%Y%m%d_%H%M%S)"

[ "$MODE" = "plan" ] || [ "$MODE" = "apply" ] || { echo "usage : $0 plan|apply"; exit 2; }
[ "$DRY" = "1" ] || [ "$EUID" -eq 0 ] || { echo "À lancer avec sudo/root."; exit 1; }

say()  { echo; echo "=== $* ==="; }
doit() { if [ "$MODE" = "apply" ]; then "$@"; else echo "  [plan] $*"; fi; }
sc()   { if [ "$DRY" = "1" ]; then echo "  [dry] systemctl $*"; else systemctl "$@"; fi; }

# --- valeur d'une clé pgbouncer (hors commentaires), vide si absente ---------------
pgb_get() { grep -E "^[[:space:]]*$1[[:space:]]*=" "$PGB_INI" | tail -1 | sed -E 's/^[^=]*=[[:space:]]*//; s/[[:space:]]*$//' || true; }
# --- fixe une clé dans la section [pgbouncer] (remplace ou ajoute) -----------------
pgb_set() {
  local key="$1" val="$2"
  if grep -qE "^[[:space:]]*$key[[:space:]]*=" "$PGB_INI"; then
    sed -i -E "s|^[[:space:]]*$key[[:space:]]*=.*|$key = $val|" "$PGB_INI"
  else
    sed -i -E "/^\[pgbouncer\]/a $key = $val" "$PGB_INI"
  fi
}

say "État actuel"
echo "PgBouncer ($PGB_INI) — paramètres utiles (identifiants masqués) :"
grep -E "^[[:space:]]*(pool_mode|max_client_conn|default_pool_size|reserve_pool_size|min_pool_size|max_db_connections|listen_port|server_idle_timeout)" "$PGB_INI" | sed 's/^/  /' || echo "  (aucun — valeurs par défaut de PgBouncer)"
echo "Instances Next.js existantes : $(ls "$UNIT_DIR" | grep -c '^cei-next.*\.service$')"
grep -n "server 127.0.0.1:51" "$NGINX_CONF" | sed 's/^/  nginx /'
[ "$DRY" = "1" ] || echo "Charge : $(cut -d' ' -f1-3 /proc/loadavg) ; mémoire libre : $(free -g | awk '/^Mem/{print $7}') Go"

# =====================================================================================
say "1/2 PgBouncer"
MODE_POOL="$(pgb_get pool_mode)"; MODE_POOL="${MODE_POOL:-session}"   # défaut PgBouncer = session
CUR_MAX="$(pgb_get max_client_conn)"; CUR_MAX="${CUR_MAX:-100}"       # défaut PgBouncer = 100
CUR_POOL="$(pgb_get default_pool_size)"; CUR_POOL="${CUR_POOL:-20}"   # défaut PgBouncer = 20
echo "  pool_mode=$MODE_POOL  max_client_conn=$CUR_MAX  default_pool_size=$CUR_POOL"
WANT_MAX=2000
if [ "$MODE_POOL" = "transaction" ]; then WANT_POOL=80; else WANT_POOL="$CUR_POOL"; fi
if [ "$MODE_POOL" != "transaction" ]; then
  echo "  ATTENTION : pool_mode=$MODE_POOL. En mode session, chaque connexion cliente occupe une connexion"
  echo "  PostgreSQL (max_connections=300) : ne PAS relever GUNICORN_WORKERS au-delà de 24 tant que ce"
  echo "  mode n'est pas passé à « transaction » (changement à valider avec l'équipe : il demande de"
  echo "  vérifier l'usage de SET / LISTEN / verrous consultatifs par l'application)."
fi
CHANGED_PGB=0
if [ "$CUR_MAX" -lt "$WANT_MAX" ]; then echo "  -> max_client_conn $CUR_MAX => $WANT_MAX"; CHANGED_PGB=1; fi
if [ "$WANT_POOL" -gt "$CUR_POOL" ]; then echo "  -> default_pool_size $CUR_POOL => $WANT_POOL"; CHANGED_PGB=1; fi
DROPIN_DIR="$UNIT_DIR/pgbouncer.service.d"
NEED_DROPIN=0; [ -f "$DROPIN_DIR/nofile.conf" ] || NEED_DROPIN=1
[ "$NEED_DROPIN" = "1" ] && echo "  -> LimitNOFILE=65536 pour le service pgbouncer (2000 clients > 1024 descripteurs par défaut)"
if [ "$CHANGED_PGB" = "1" ] || [ "$NEED_DROPIN" = "1" ]; then
  if [ "$MODE" = "apply" ]; then
    mkdir -p "$BACKUP"; cp -a "$PGB_INI" "$BACKUP/pgbouncer.ini"
    [ "$CUR_MAX" -lt "$WANT_MAX" ] && pgb_set max_client_conn "$WANT_MAX"
    [ "$WANT_POOL" -gt "$CUR_POOL" ] && pgb_set default_pool_size "$WANT_POOL"
    if [ "$NEED_DROPIN" = "1" ]; then
      mkdir -p "$DROPIN_DIR"; printf '[Service]\nLimitNOFILE=65536\n' > "$DROPIN_DIR/nofile.conf"
      sc daemon-reload
    fi
    echo "  Redémarrage de pgbouncer (les connexions de l'API sont rétablies automatiquement, ~1 s)…"
    sc restart pgbouncer
    [ "$DRY" = "1" ] || { sleep 2; systemctl is-active pgbouncer; }
  else
    echo "  [plan] pgbouncer serait redémarré (~1 s de coupure des connexions DB, reprises automatiquement)"
  fi
else
  echo "  Rien à changer."
fi

# =====================================================================================
say "2/2 Instances Next.js $FIRST_IDX → $LAST_IDX"
[ -f "$TEMPLATE_UNIT" ] || { echo "Modèle $TEMPLATE_UNIT introuvable"; exit 1; }
NEWUNITS=()
for idx in $(seq "$FIRST_IDX" "$LAST_IDX"); do
  port=$((BASE_PORT + idx - FIRST_IDX)); unit="$UNIT_DIR/cei-next-$idx.service"
  if [ -f "$unit" ]; then echo "  cei-next-$idx : déjà présente"; continue; fi
  echo "  + cei-next-$idx (port $port)"
  if [ "$MODE" = "apply" ]; then
    sed -E "s/5180/$port/g; s/instance 6/instance $idx/; s/cei-next-6/cei-next-$idx/g" "$TEMPLATE_UNIT" > "$unit"
    grep -q "PORT=$port" "$unit" || { echo "ERREUR : modèle inattendu, $unit supprimé"; rm -f "$unit"; exit 1; }
  fi
  NEWUNITS+=("$idx:$port")
done
# lignes upstream nginx manquantes
NEEDNGINX=()
for idx in $(seq "$FIRST_IDX" "$LAST_IDX"); do
  port=$((BASE_PORT + idx - FIRST_IDX))
  grep -q "server 127.0.0.1:$port;" "$NGINX_CONF" || NEEDNGINX+=("$port")
done
if [ "${#NEEDNGINX[@]}" -gt 0 ]; then
  echo "  nginx : ajout de ${NEEDNGINX[*]} à l'upstream cei_next_upstream"
  if [ "$MODE" = "apply" ]; then
    mkdir -p "$BACKUP"; cp -a "$NGINX_CONF" "$BACKUP/nginx_cei.conf"
    for port in "${NEEDNGINX[@]}"; do
      # après la dernière ligne « server 127.0.0.1:51xx; » de l'upstream
      last=$(grep -n "^[[:space:]]*server 127.0.0.1:51[0-9][0-9];" "$NGINX_CONF" | tail -1 | cut -d: -f1)
      sed -i "${last}a\\    server 127.0.0.1:$port;" "$NGINX_CONF"
    done
  fi
fi
if [ "$MODE" = "apply" ]; then
  if [ "${#NEWUNITS[@]}" -gt 0 ] || [ "${#NEEDNGINX[@]}" -gt 0 ]; then
    sc daemon-reload
    for e in "${NEWUNITS[@]:-}"; do [ -n "$e" ] && sc enable --now "cei-next-${e%%:*}"; done
    if [ "$DRY" != "1" ]; then
      sleep 4
      for e in "${NEWUNITS[@]:-}"; do
        [ -n "$e" ] || continue; p="${e##*:}"
        code=$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:$p/" || true)
        echo "  cei-next-${e%%:*} (port $p) -> $code"
        [ "$code" = "200" ] || {
          echo "ERREUR : l'instance ne répond pas — configuration nginx restaurée, trafic inchangé."
          [ -f "$BACKUP/nginx_cei.conf" ] && cp -a "$BACKUP/nginx_cei.conf" "$NGINX_CONF"
          exit 1; }
      done
      if nginx -t; then systemctl reload nginx && echo "  nginx rechargé"; else
        echo "ERREUR nginx -t : restauration de la configuration"; cp -a "$BACKUP/nginx_cei.conf" "$NGINX_CONF"; exit 1; fi
    fi
  else
    echo "  Rien à changer."
  fi
else
  echo "  [plan] les nouvelles instances seraient démarrées et vérifiées (200) AVANT d'être ajoutées à nginx"
fi

say "Terminé ($MODE)"
if [ "$MODE" = "apply" ]; then
  echo "Sauvegardes : $BACKUP"
  echo "Étape suivante (non-root, je m'en charge) : GUNICORN_WORKERS=48 dans .env puis restart cei-api-v2,"
  echo "à condition que pool_mode soit « transaction » (voir ci-dessus)."
fi
