#!/bin/bash
# À lancer UNE FOIS en root sur srv-sfu-cei : bash /home/serge/sfu-install/sfu_root_metrics.sh
# Mesures de la machine (processeur, mémoire, carte réseau) pour la supervision, via nginx 443.
set -euo pipefail
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq prometheus-node-exporter > /dev/null
systemctl enable --now prometheus-node-exporter
F=/etc/nginx/sites-available/sfu-cei
cp -a "$F" "$F.bak_metrics"
python3 - "$F" <<'PY'
import sys
p = sys.argv[1]
s = open(p).read()
if '/node-metrics' in s:
    print('déjà présent'); sys.exit(0)
i = s.index('    location / {')
block = '''    location = /node-metrics {
        allow 102.36.139.23;
        allow 10.149.0.0/16;
        allow 127.0.0.1;
        deny all;
        proxy_pass http://127.0.0.1:9100/metrics;
    }

'''
open(p, 'w').write(s[:i] + block + s[i:])
PY
if nginx -t; then systemctl reload nginx; else cp -a "$F.bak_metrics" "$F"; echo "ÉCHEC : ancienne configuration remise"; exit 1; fi
curl -sk -o /dev/null -w "/node-metrics en local : %{http_code}\n" --resolve sfu-cei.unchk.sn:443:127.0.0.1 https://sfu-cei.unchk.sn/node-metrics
echo "TERMINÉ"
