#!/bin/bash
# Compression gzip des fichiers de surveillance par vision (/mediapipe/, /models/, /vendor/ : 48,4 Mo -> ~22,5 Mo par appareil).
# À lancer EN ROOT sur le serveur du site (thieboudiene = production, puis préprod) :
#     sudo bash nginx_compression_vision_root.sh plan     # montre ce qui changerait, ne modifie rien
#     sudo bash nginx_compression_vision_root.sh apply    # sauvegarde, modifie, teste (nginx -t), recharge ; restaure tout seul si le test échoue
# Ne touche QUE les trois blocs « location » des fichiers de vision (pas de compression globale : le serveur sert aussi du stockage
# vidéo déjà compressé). Idempotent : sans effet si déjà fait.
set -euo pipefail
MODE="${1:-plan}"
CONF="${NGINX_SITE:-/etc/nginx/sites-available/cei}"
[ -f "$CONF" ] || { echo "ERREUR : $CONF introuvable"; exit 1; }
[ "$(id -u)" -eq 0 ] || { echo "ERREUR : à lancer en root (sudo)"; exit 1; }
TMP="$(mktemp)"; trap 'rm -f "$TMP"' EXIT

python3 - "$CONF" "$TMP" <<'PY'
import re, sys
src, dst = sys.argv[1], sys.argv[2]
s = open(src, encoding='utf-8').read()
GZ = ("        gzip on;\n        gzip_vary on;\n        gzip_comp_level 5;\n"
      "        gzip_types application/javascript application/wasm application/octet-stream;\n")
n = 0
def fix(m):
    global n
    block = m.group(0)
    if re.search(r'^\s*gzip_types\b', block, re.M):
        return block                                        # déjà fait
    n += 1
    return re.sub(r'(\n    \})$', '\n' + GZ.rstrip('\n') + r'\1', block)
out = re.sub(r'    location /(?:mediapipe|models|vendor)/ \{.*?\n    \}', fix, s, flags=re.S)
open(dst, 'w', encoding='utf-8').write(out)
print(f"blocs à modifier : {n} sur 3")
PY

echo "--- changements prévus :"
diff -u "$CONF" "$TMP" || true
if [ "$MODE" != "apply" ]; then echo; echo "(plan : rien n'a été modifié. Pour appliquer : sudo bash $0 apply)"; exit 0; fi
if diff -q "$CONF" "$TMP" >/dev/null; then echo "Déjà appliqué : rien à faire."; exit 0; fi

BAK="/home/serge/cei.nginx.bak_$(date +%Y%m%d_%H%M%S)_compression"
cp -p "$CONF" "$BAK"; echo "sauvegarde : $BAK"
cp "$TMP" "$CONF"
if nginx -t 2>&1; then
  systemctl reload nginx && echo "nginx rechargé."
else
  echo "ERREUR : nginx -t a échoué, RESTAURATION de la configuration d'origine."; cp -p "$BAK" "$CONF"; nginx -t; exit 1
fi
HOST="${CHECK_HOST:-$(awk '/^[[:space:]]*server_name/ {gsub(";", "", $2); print $2; exit}' "$CONF")}"
echo "--- contrôle sur https://$HOST : taille reçue du moteur WASM (attendu ≈ 3,4 Mo au lieu de 11,5 Mo)"
curl -s -o /dev/null -H 'Accept-Encoding: gzip' -w 'reçu : %{size_download} octets (HTTP %{http_code})\n' --resolve "$HOST:443:127.0.0.1" -k "https://$HOST/mediapipe/wasm/vision_wasm_internal.wasm" || true
curl -sI -H 'Accept-Encoding: gzip' --resolve "$HOST:443:127.0.0.1" -k "https://$HOST/mediapipe/wasm/vision_wasm_internal.wasm" | grep -i '^content-encoding' || echo "(pas de content-encoding : vérifier depuis un poste externe)"
