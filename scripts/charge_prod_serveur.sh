#!/bin/bash
# TEST DE CHARGE EN PRODUCTION, générateur lancé SUR le serveur (sans le réseau extérieur) : sépare ce qui vient de la plateforme de ce qui vient du chemin réseau.
#   bash scripts/charge_prod_serveur.sh <étudiants> <surveillants> [ramp s] [régime s] [remise s]     (SHARDS=6 par défaut)
N=${1:?étudiants}; P=${2:?surveillants}; RAMP=${3:-20}; STEADY=${4:-240}; SUBMIT=${5:-30}; SH=${SHARDS:-6}
cd "$(dirname "$0")/.." || exit 9
SSH="-i /root/.ssh/id_ed25519_unchk -p 3120 serge@196.207.246.205"; SCP="-i /root/.ssh/id_ed25519_unchk -P 3120"; D=/home/serge/projet-cei/cei-api-v2; R=serge@196.207.246.205
ssh $SSH "mkdir -p /tmp/charge_prod" && scp $SCP -q scripts/charge_prod/prepare.py scripts/charge_prod/cleanup.py scripts/charge_prod/guard.py scripts/charge_prod/run.py scripts/charge_prod/pool_check.py $R:/tmp/charge_prod/ || exit 9
cleanup() { echo "== Contrôle du pool de connexions (aucune connexion ne doit être en lecture seule) =="; ssh $SSH "cd $D && .venv-base/bin/python /tmp/charge_prod/pool_check.py repair 2 2>&1 | tail -2"; echo "== Nettoyage =="; ssh $SSH "cd $D && .venv-base/bin/python /tmp/charge_prod/cleanup.py 2>&1 | tail -1; rm -f /tmp/charge_ctx.json /tmp/charge_dump_*.json"; }
FULL=$(ssh $SSH "cd $D && .venv-base/bin/python /tmp/charge_prod/prepare.py $N $P 2>&1 | grep -v 'Deprecat\|rc.setex'")
OUT=$(echo "$FULL" | tail -1)
echo "préparation : $OUT"; echo "$OUT" | grep -q '"ok": true' || { echo "préparation refusée ou en échec — dernières lignes :"; echo "$FULL" | tail -15 | cut -c1-300; echo "== Nettoyage des données partielles =="; ssh $SSH "cd $D && .venv-base/bin/python /tmp/charge_prod/cleanup.py 2>&1 | tail -1"; exit 3; }
trap cleanup EXIT
ssh $SSH "ulimit -n 65535 2>/dev/null; cd $D; rm -f /tmp/charge_dump_*.json
for k in \$(seq 0 $((SH-1))); do PYTHONPATH=/tmp/aio .venv-base/bin/python /tmp/charge_prod/run.py /tmp/charge_ctx.json --ramp $RAMP --steady $STEADY --submit $SUBMIT --shard \$k/$SH --dump /tmp/charge_dump_\$k.json --resolve 127.0.0.1 --local-server > /tmp/charge_shard_\$k.log 2>&1 & done; wait"
rm -f /tmp/charge_dump_*.json; scp $SCP -q $R:/tmp/charge_dump_*.json /tmp/ || echo "dumps introuvables"
/tmp/loadenv/bin/python scripts/charge_prod/run.py --aggregate /tmp/charge_dump_*.json
rc=$?; rm -f /tmp/charge_dump_*.json; exit $rc
