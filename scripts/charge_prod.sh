#!/bin/bash
# TEST DE CHARGE EN PRODUCTION (hors examen réel uniquement) — prépare, charge, nettoie (nettoyage GARANTI, même si le test est interrompu).
#   bash scripts/charge_prod.sh <étudiants> <surveillants> [ramp s] [régime s] [remise s]
#   ex. bash scripts/charge_prod.sh 1500 40 30 240 30
# Refuse de démarrer si un vrai examen ou une vraie tentative est en cours ; s'arrête seul si la sonde de santé du site se dégrade ou si un vrai examen apparaît.
N=${1:?étudiants}; P=${2:?surveillants}; RAMP=${3:-20}; STEADY=${4:-240}; SUBMIT=${5:-30}
cd "$(dirname "$0")/.." || exit 9
SSH="-i /root/.ssh/id_ed25519_unchk -p 3120 serge@196.207.246.205"; SCP="-i /root/.ssh/id_ed25519_unchk -P 3120"; D=/home/serge/projet-cei/cei-api-v2
ssh $SSH "mkdir -p /tmp/charge_prod" && scp $SCP -q scripts/charge_prod/prepare.py scripts/charge_prod/cleanup.py scripts/charge_prod/guard.py serge@196.207.246.205:/tmp/charge_prod/ || exit 9
cleanup() { echo "== Nettoyage =="; ssh $SSH "cd $D && .venv-base/bin/python /tmp/charge_prod/cleanup.py 2>&1 | tail -1; rm -f /tmp/charge_ctx.json"; }
OUT=$(ssh $SSH "cd $D && .venv-base/bin/python /tmp/charge_prod/prepare.py $N $P 2>&1 | tail -1")
echo "préparation : $OUT"
echo "$OUT" | grep -q '"ok": true' || { echo "préparation refusée ou en échec"; exit 3; }
trap cleanup EXIT
scp $SCP -q serge@196.207.246.205:/tmp/charge_ctx.json /tmp/charge_ctx.json || exit 4
# Sonde LOCALE au serveur : mesure le temps de réponse de nginx + application depuis le serveur lui-même (sans le réseau ni le générateur), pour séparer ce qui vient de la plateforme de ce qui vient du chemin réseau.
PROBE_S=$((RAMP+STEADY+SUBMIT+60))
ssh $SSH "rm -f /tmp/charge_probe.txt; nohup bash -c 'end=\$((\$(date +%s)+$PROBE_S)); while [ \$(date +%s) -lt \$end ]; do curl -sk -o /dev/null -w \"%{time_total}\\n\" --resolve cei.unchk.sn:443:127.0.0.1 https://cei.unchk.sn/api/health >> /tmp/charge_probe.txt; sleep 0.5; done' > /dev/null 2>&1 &"
SH=${SHARDS:-4}; rm -f /tmp/charge_dump_*.json
for k in $(seq 0 $((SH-1))); do
  /tmp/loadenv/bin/python scripts/charge_prod/run.py /tmp/charge_ctx.json --ramp "$RAMP" --steady "$STEADY" --submit "$SUBMIT" --shard $k/$SH --dump /tmp/charge_dump_$k.json > /dev/null 2>&1 &
done
wait
/tmp/loadenv/bin/python scripts/charge_prod/run.py --aggregate /tmp/charge_dump_*.json
rc=$?
ssh $SSH "cat /tmp/charge_probe.txt" 2>/dev/null | python3 -c "
import sys
v=sorted(float(x)*1000 for x in sys.stdin.read().split() if x.replace('.','',1).isdigit())
p=lambda q: v[min(len(v)-1,int(len(v)*q))]
print(f\"Sonde DEPUIS le serveur (nginx + application, sans réseau extérieur) : {len(v)} mesures, p50 {p(.5):.0f} ms, p90 {p(.9):.0f} ms, p99 {p(.99):.0f} ms, max {v[-1]:.0f} ms\") if v else print('sonde locale : aucune mesure')"
rm -f /tmp/charge_ctx.json /tmp/charge_dump_*.json
exit $rc
