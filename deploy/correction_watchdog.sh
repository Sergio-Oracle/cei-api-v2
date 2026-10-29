#!/bin/bash
# Relance le service de correction s'il ne tourne pas (alternative à cei-correction.service quand on n'a pas
# les droits root). Tâche cron de l'utilisateur serge, chaque minute :
#   * * * * * /home/serge/projet-cei/cei-api-v2/deploy/correction_watchdog.sh
pgrep -f "[c]orrection_worker.py" > /dev/null && exit 0
cd /home/serge/projet-cei/cei-api-v2 || exit 1
nohup setsid .venv-base/bin/python correction_worker.py >> /home/serge/correction_worker.log 2>&1 < /dev/null &
