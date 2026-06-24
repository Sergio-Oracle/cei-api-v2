#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# CEI API v2 — Suite de tests complète (Senior Developer checklist)
# Usage: bash tests/run_all_tests.sh [--no-load] [--no-stress]
# ─────────────────────────────────────────────────────────────────────────────
set -e
cd /root/cei-api-v2

VENV=".venv-base/bin"
BASE_URL="http://62.171.190.6:8100"
REPORTS_DIR="tests/reports"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
NO_LOAD=false
NO_STRESS=false

for arg in "$@"; do
  [[ "$arg" == "--no-load" ]]   && NO_LOAD=true
  [[ "$arg" == "--no-stress" ]] && NO_STRESS=true
done

mkdir -p "$REPORTS_DIR"

PASS=0; FAIL=0; SKIP=0

section() { echo ""; echo "══════════════════════════════════════════════════════"; echo "  $1"; echo "══════════════════════════════════════════════════════"; }
ok()   { echo "  ✓ $1"; PASS=$((PASS+1)); }
fail() { echo "  ✗ $1"; FAIL=$((FAIL+1)); }
skip() { echo "  ⊘ $1 (ignoré)"; SKIP=$((SKIP+1)); }

# ─── 0. Vérifications préliminaires ──────────────────────────────────────────
section "0. Checks préliminaires"

if curl -s -o /dev/null -w "%{http_code}" "$BASE_URL/api/auth/public-key" | grep -q "200"; then
  ok "Serveur accessible sur $BASE_URL"
else
  fail "Serveur inaccessible — vérifier systemctl status cei-api-v2"
  exit 1
fi

SERVICE_STATUS=$(systemctl is-active cei-api-v2 2>/dev/null || echo "unknown")
if [ "$SERVICE_STATUS" = "active" ]; then
  ok "Service cei-api-v2 actif"
else
  fail "Service cei-api-v2 non actif (status: $SERVICE_STATUS)"
fi

WORKERS=$(ss -tlnp | grep 8100 | grep -c gunicorn || echo 0)
if [ "$WORKERS" -ge 1 ]; then
  ok "Gunicorn en écoute sur port 8100 ($WORKERS workers)"
else
  fail "Aucun worker Gunicorn détecté sur port 8100"
fi

# ─── 1. Tests unitaires + intégration ────────────────────────────────────────
section "1. Tests unitaires & intégration (pytest)"

REPORT_HTML="$REPORTS_DIR/unit_tests_$TIMESTAMP.html"
REPORT_COV="$REPORTS_DIR/coverage_$TIMESTAMP"

cd tests
if $VENV/../bin/python -m pytest \
    test_01_auth.py test_02_admin.py test_03_exams.py \
    test_04_surveillant.py test_05_security.py test_06_proctoring.py \
    -v --tb=short --timeout=30 \
    --html="../$REPORT_HTML" --self-contained-html \
    2>&1 | tee "/tmp/pytest_output_$TIMESTAMP.txt"; then
  ok "Tests unitaires et intégration passés"
else
  fail "Des tests unitaires ont échoué — voir $REPORT_HTML"
fi
cd ..

# Compter les résultats depuis la sortie pytest
PASSED=$(grep -c "PASSED" "/tmp/pytest_output_$TIMESTAMP.txt" 2>/dev/null || echo 0)
FAILED=$(grep -c "FAILED" "/tmp/pytest_output_$TIMESTAMP.txt" 2>/dev/null || echo 0)
SKIPPED=$(grep -c "SKIPPED" "/tmp/pytest_output_$TIMESTAMP.txt" 2>/dev/null || echo 0)
echo "  Détail pytest: $PASSED passés, $FAILED échoués, $SKIPPED ignorés"

# ─── 2. Tests de sécurité avancés ────────────────────────────────────────────
section "2. Tests de sécurité avancés"

# OWASP A01 - Broken Access Control
TOKEN=$(curl -s -X POST "$BASE_URL/api/auth/login" \
  -H "Content-Type: application/json" \
  -d '{"email":"laprincesseawa99@gmail.com","password":"passer"}' | \
  python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('access_token',''))" 2>/dev/null)

if [ -n "$TOKEN" ]; then
  CODE=$(curl -s -o /dev/null -w "%{http_code}" "$BASE_URL/api/admin/dashboard" \
    -H "Authorization: Bearer $TOKEN")
  if [ "$CODE" = "403" ] || [ "$CODE" = "401" ]; then
    ok "A01 - Broken Access Control: étudiant bloqué sur /admin/dashboard (HTTP $CODE)"
  else
    fail "A01 - Broken Access Control: étudiant accède à /admin/dashboard (HTTP $CODE)"
  fi
fi

# OWASP A02 - Cryptographic Failures (vérifier HTTPS optionnel, algo fixe)
PUB_KEY=$(curl -s "$BASE_URL/api/auth/public-key" | python3 -c "
import sys,json; d=json.load(sys.stdin); print(d.get('algorithm','?'))" 2>/dev/null)
if [ "$PUB_KEY" = "Ed25519" ]; then
  ok "A02 - Cryptographic: Ed25519 (algorithme fixe, résistant algorithm confusion)"
else
  fail "A02 - Cryptographic: algorithme inattendu: $PUB_KEY"
fi

# OWASP A03 - Injection SQL (input malveillant)
CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$BASE_URL/api/auth/login" \
  -H "Content-Type: application/json" \
  -d '{"email":"'"'"' OR 1=1 --","password":"x"}')
if [ "$CODE" = "401" ] || [ "$CODE" = "400" ]; then
  ok "A03 - Injection SQL: payload rejeté (HTTP $CODE)"
else
  fail "A03 - Injection SQL: code inattendu $CODE"
fi

# OWASP A07 - Auth: vérifier token invalide
CODE=$(curl -s -o /dev/null -w "%{http_code}" "$BASE_URL/api/auth/me" \
  -H "Authorization: Bearer INVALIDE")
if [ "$CODE" = "401" ]; then
  ok "A07 - Auth: token invalide rejeté (HTTP 401)"
else
  fail "A07 - Auth: token invalide accepté (HTTP $CODE)"
fi

# Cookies httpOnly
LOGIN_HEADERS=$(curl -v -s -o /dev/null -X POST "$BASE_URL/api/auth/login" \
  -H "Content-Type: application/json" \
  -d '{"email":"serge@rtn.sn","password":"passer"}' 2>&1 | grep -i "set-cookie")
if echo "$LOGIN_HEADERS" | grep -qi "httponly"; then
  ok "Cookie refresh httpOnly correct"
else
  fail "Cookie refresh ne contient pas HttpOnly"
fi

# ─── 3. Tests de performance rapides ─────────────────────────────────────────
section "3. Tests de performance (baseline)"

ADMIN_TOKEN=$(curl -s -c /tmp/test_cookies.txt -X POST "$BASE_URL/api/auth/login" \
  -H "Content-Type: application/json" \
  -d '{"email":"serge@rtn.sn","password":"passer"}' | \
  python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

# Latence moyenne sur 10 requêtes successives
LATENCIES=()
for i in $(seq 1 10); do
  MS=$(curl -s -o /dev/null -w "%{time_total}" "$BASE_URL/api/admin/dashboard" \
    -H "Authorization: Bearer $ADMIN_TOKEN" | awk '{printf "%.0f", $1*1000}')
  LATENCIES+=($MS)
done

AVG_MS=$(echo "${LATENCIES[@]}" | tr ' ' '\n' | awk '{s+=$1}END{printf "%.0f",s/NR}')
MAX_MS=$(echo "${LATENCIES[@]}" | tr ' ' '\n' | sort -n | tail -1)

if [ "$AVG_MS" -lt 500 ]; then
  ok "Latence moyenne /api/admin/dashboard : ${AVG_MS}ms (SLA<500ms)"
else
  fail "Latence trop élevée /api/admin/dashboard : ${AVG_MS}ms (SLA<500ms)"
fi
echo "  Latences: ${LATENCIES[*]} | Max: ${MAX_MS}ms"

# Taux d'erreur sur 20 requêtes simultanées
ERROR_COUNT=0
for i in $(seq 1 20); do
  CODE=$(curl -s -o /dev/null -w "%{http_code}" "$BASE_URL/api/online_exams" \
    -H "Authorization: Bearer $ADMIN_TOKEN" &)
  [ "$CODE" = "200" ] || ERROR_COUNT=$((ERROR_COUNT+1))
done
wait
echo "  20 requêtes simultanées /api/online_exams — erreurs: $ERROR_COUNT/20"

# ─── 4. Tests de charge (Locust) ─────────────────────────────────────────────
if [ "$NO_LOAD" = false ]; then
  section "4. Tests de charge (Locust — 50 users, 60s)"
  LOCUST_REPORT="$REPORTS_DIR/load_report_$TIMESTAMP.html"
  if $VENV/locust -f tests/locustfile.py \
      --host="$BASE_URL" \
      --headless \
      -u 50 -r 5 \
      --run-time 60s \
      --html="$LOCUST_REPORT" \
      --csv="$REPORTS_DIR/locust_$TIMESTAMP" \
      --exit-code-on-error 1 \
      2>&1 | tail -20; then
    ok "Locust — 50 users, 60s terminé"
    echo "  Rapport: $LOCUST_REPORT"
  else
    fail "Locust — test de charge échoué"
  fi
else
  skip "Tests de charge Locust (--no-load)"
fi

# ─── 5. Tests de stress et scalabilité ───────────────────────────────────────
if [ "$NO_STRESS" = false ]; then
  section "5. Tests de stress et scalabilité (proctoring)"
  if TEST_BASE_URL="$BASE_URL" $VENV/python tests/stress_scalability.py; then
    ok "Tests de stress et scalabilité passés"
  else
    fail "Tests de stress — certains SLA dépassés"
  fi
else
  skip "Tests de stress (--no-stress)"
fi

# ─── 6. Tests de débogage (endpoints debug) ──────────────────────────────────
section "6. Tests de débogage"

# Vérifier les logs d'erreur récents
ERROR_LINES=$(tail -100 /var/log/cei-api-v2/error.log 2>/dev/null | \
  grep -c "ERROR\|CRITICAL\|Traceback" || echo 0)
if [ "$ERROR_LINES" -lt 5 ]; then
  ok "Logs d'erreur propres ($ERROR_LINES erreurs dans les 100 dernières lignes)"
else
  fail "Logs d'erreur anormaux: $ERROR_LINES erreurs récentes"
  tail -20 /var/log/cei-api-v2/error.log 2>/dev/null | head -10
fi

# Vérifier la mémoire des workers Gunicorn
MEM_MB=$(ps aux | grep gunicorn | grep -v grep | \
  awk '{sum+=$6}END{printf "%.0f", sum/1024}')
if [ -n "$MEM_MB" ] && [ "$MEM_MB" -lt 2048 ]; then
  ok "Mémoire Gunicorn: ${MEM_MB}MB (SLA<2048MB)"
else
  fail "Mémoire Gunicorn trop élevée: ${MEM_MB}MB"
fi

# Vérifier le nombre de workers actifs
WORKER_COUNT=$(ps aux | grep "[g]unicorn" | grep -v master | wc -l)
EXPECTED_WORKERS=9
if [ "$WORKER_COUNT" -ge "$((EXPECTED_WORKERS - 1))" ]; then
  ok "Workers Gunicorn: $WORKER_COUNT/$EXPECTED_WORKERS actifs"
else
  fail "Nombre de workers insuffisant: $WORKER_COUNT/$EXPECTED_WORKERS"
fi

# Vérifier le PID file
if [ -f /run/cei-api-v2.pid ]; then
  ok "PID file présent"
else
  fail "PID file manquant (/run/cei-api-v2.pid)"
fi

# ─── Récapitulatif final ──────────────────────────────────────────────────────
section "RÉCAPITULATIF"
echo "  Tests passés  : $PASS"
echo "  Tests échoués : $FAIL"
echo "  Ignorés       : $SKIP"
echo ""
echo "  Rapport HTML  : $REPORTS_DIR/unit_tests_$TIMESTAMP.html"
[ -f "$REPORTS_DIR/load_report_$TIMESTAMP.html" ] && \
  echo "  Rapport Locust: $REPORTS_DIR/load_report_$TIMESTAMP.html"
echo ""

if [ "$FAIL" -eq 0 ]; then
  echo "  ✓ DÉPLOIEMENT VALIDÉ — Prêt pour la production"
  exit 0
else
  echo "  ✗ $FAIL PROBLÈME(S) DÉTECTÉ(S) — Ne pas déployer en production"
  exit 1
fi
