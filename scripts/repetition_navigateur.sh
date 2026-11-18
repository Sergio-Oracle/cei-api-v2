#!/bin/bash
# Répétition générale EN NAVIGATEUR RÉEL (serveur LOCAL, jamais la production) : rejoue les défauts du 17/11/2026 et dit s'ils sont revenus.
#   cd cei-api-v2 && bash scripts/repetition_navigateur.sh            (tous les scénarios, ≈ 8 min)
#   bash scripts/repetition_navigateur.sh 1 3                          (seulement les scénarios 1 et 3)
# Prérequis : le site local à jour (cd cei-next && ./build-local.sh) et l'API locale redémarrée ; Playwright : /tmp/pwenv (python3 -m venv /tmp/pwenv && /tmp/pwenv/bin/pip install playwright python-dotenv pyjwt && /tmp/pwenv/bin/playwright install chromium).
cd "$(dirname "$0")/.." || exit 9
PY=.venv-base/bin/python; PWPY=${CEI_E2E_PY:-/tmp/pwenv/bin/python}
CTX=$($PY scripts/e2e/setup.py 2>/dev/null | tail -1) || { echo "préparation impossible"; exit 9; }
echo "$CTX" > /tmp/e2e_ctx.json
trap '$PY scripts/e2e/teardown.py "$(cat /tmp/e2e_ctx.json)" 2>/dev/null | tail -1' EXIT
env -u ELECTRON_RUN_AS_NODE $PWPY scripts/e2e/scenarios.py /tmp/e2e_ctx.json "$@"
