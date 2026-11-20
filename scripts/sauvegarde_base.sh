#!/bin/bash
# Sauvegarde quotidienne de la base PostgreSQL (20/11/2026 : il n'existait AUCUNE sauvegarde automatique, seulement un dump manuel du 16/11).
#   Cron (utilisateur serge) :  30 2 * * * /home/serge/projet-cei/cei-api-v2/scripts/sauvegarde_base.sh >> /home/serge/backups/db/sauvegarde.log 2>&1
# - connexion DIRECTE à PostgreSQL (port 5432), jamais via PgBouncer ; format personnalisé compressé ;
# - vérification d'intégrité (lecture complète par pg_restore) AVANT de déclarer la sauvegarde bonne ; fichier LAST_OK = preuve de la dernière sauvegarde valide
#   (l'API prévient les administrateurs si elle a plus de 36 h : services/exam_closer.check_backup_fresh) ;
# - conserve les 14 dernières ; dossier et fichiers lisibles par le seul propriétaire.
# Restauration :  pg_restore -h 127.0.0.1 -U <utilisateur> -d <base_vide> --no-owner <fichier.dump>
set -u
API="${CEI_API_DIR:-/home/serge/projet-cei/cei-api-v2}"
DEST="${BACKUP_DIR:-/home/serge/backups/db}"
KEEP="${BACKUP_KEEP:-14}"
umask 077
mkdir -p "$DEST" && chmod 700 "$DEST"
eval "$(python3 - "$API/.env" <<'PY'
import sys, shlex
from urllib.parse import urlparse
url = next(l.strip().split('=', 1)[1] for l in open(sys.argv[1]) if l.startswith('DATABASE_URL='))
u = urlparse(url)
for k, v in (('PGHOST', u.hostname or '127.0.0.1'), ('PGPORT', '5432'), ('PGUSER', u.username or ''), ('PGPASSWORD', u.password or ''), ('PGDATABASE', u.path.lstrip('/'))):
    print(f'export {k}={shlex.quote(v)}')
PY
)"
FILE="$DEST/cei_$(date -u +%Y%m%d_%H%M)_$PGDATABASE.dump"
echo "$(date -u +%FT%TZ) début → $FILE"
if nice -n 19 ionice -c3 pg_dump -Fc --no-owner "$PGDATABASE" > "$FILE.partiel" 2> "$FILE.err"; then
  if pg_restore -f /dev/null "$FILE.partiel" > /dev/null 2>&1 && [ -s "$FILE.partiel" ]; then      # lit et décompresse TOUT le contenu (pas seulement le sommaire)
    mv "$FILE.partiel" "$FILE"; rm -f "$FILE.err"; touch "$DEST/LAST_OK"
    echo "$(date -u +%FT%TZ) OK $(du -h "$FILE" | cut -f1)"
    ls -1t "$DEST"/cei_*.dump 2>/dev/null | tail -n +$((KEEP + 1)) | xargs -r rm -f
    exit 0
  fi
fi
echo "$(date -u +%FT%TZ) ÉCHEC de la sauvegarde — voir $FILE.err"; rm -f "$FILE.partiel"; exit 1
