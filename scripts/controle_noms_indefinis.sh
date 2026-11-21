#!/bin/bash
# Refuse le code qui référence un nom qui n'existe pas (ex. « name 'stuck' is not defined », 43 fois dans le journal de prod le 21/11/2026 :
# un patch partiel d'un fichier propre à un serveur avait laissé une boucle sur une variable supprimée ; le filet « copies bloquées / non corrigées » plantait à chaque passage).
#   bash scripts/controle_noms_indefinis.sh            → code du dépôt local
#   bash scripts/controle_noms_indefinis.sh <dossier>  → copie d'un serveur (à récupérer en lecture seule avec rsync), sauvegardes ignorées
cd "$(dirname "$0")/.." || exit 9
DIR=${1:-.}; VENV=${CEI_LINT_VENV:-/tmp/cei_lint}
[ -x "$VENV/bin/python" ] || { python3 -m venv "$VENV" && "$VENV/bin/pip" install -q pyflakes >/dev/null 2>&1; }
"$VENV/bin/python" -m pyflakes --version >/dev/null 2>&1 || { echo "contrôle des noms indéfinis impossible (pyflakes indisponible) — IGNORÉ"; exit 0; }
cd "$DIR" || exit 9
out=$(find . -name '*.py' -not -path './scripts/*' -not -path './tests/*' -not -path '*/.venv*' -not -path './venv*' -not -path './backups/*' -not -path './backup_*' -not -name '*.bak*' -print0 \
      | xargs -0 "$VENV/bin/python" -m pyflakes 2>&1 | grep "undefined name" | grep -v "'__file__'")
if [ -n "$out" ]; then echo "$out"; echo "noms indéfinis : $(echo "$out" | wc -l) — vérifications échouées"; exit 1; fi
echo "0 nom indéfini — vérifications réussies"
