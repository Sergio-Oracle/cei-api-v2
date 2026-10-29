#!/bin/bash
# À lancer UNE FOIS en root sur thieboudiene (production) :
#   bash /home/serge/projet-cei/cei-api-v2/deploy/prod_root_cleanup.sh
# 1. arrête l'ancien LiveKit (la vidéo passe désormais par srv-sfu-cei) ; 2. expose les mesures machine/PostgreSQL
# sur le vhost cei.unchk.sn (443) pour la supervision, avec retour arrière automatique si nginx refuse la config.
set -euo pipefail
echo "== 1. ancien LiveKit"
systemctl disable --now livekit
echo "   livekit : $(systemctl is-active livekit || true)"
echo "== 2. mesures pour la supervision (cei.unchk.sn/node-metrics et /pg-metrics)"
# sites-enabled contient des liens vers sites-available : on édite le vrai fichier et la sauvegarde va hors de nginx.
rm -f /etc/nginx/sites-enabled/cei.bak_supervision_29oct
F=$(readlink -f /etc/nginx/sites-enabled/cei)
BAK=/root/cei.nginx.avant_supervision_$(date +%Y%m%d_%H%M)
cp "$F" "$BAK"
python3 - "$F" <<'PY'
import sys
p = sys.argv[1]
s = open(p).read()
if '/node-metrics' in s:
    print('   déjà présent'); sys.exit(0)
anchor = 'ssl_certificate_key /opt/star_unchk.sn.key;'
i = s.index(anchor, s.index('listen 443')) + len(anchor)
block = '''

    # Mesures de la machine et de PostgreSQL pour la supervision (srv-sendt, DITSI) : ports 9100/9187 coupés par le réseau.
    location = /node-metrics {
        allow 102.36.139.23;
        allow 10.149.0.0/16;
        allow 127.0.0.1;
        deny all;
        proxy_pass http://127.0.0.1:9100/metrics;
    }

    location = /pg-metrics {
        allow 102.36.139.23;
        allow 10.149.0.0/16;
        allow 127.0.0.1;
        deny all;
        proxy_pass http://127.0.0.1:9187/metrics;
    }
'''
open(p, 'w').write(s[:i] + block + s[i:])
PY
if nginx -t; then systemctl reload nginx; echo "   nginx rechargé"; else cp "$BAK" "$F"; nginx -t; echo "   ÉCHEC : ancienne configuration remise"; exit 1; fi
curl -sk -o /dev/null -w "   /node-metrics en local : %{http_code}\n" --resolve cei.unchk.sn:443:127.0.0.1 https://cei.unchk.sn/node-metrics
echo "TERMINÉ"
