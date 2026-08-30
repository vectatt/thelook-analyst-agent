#!/usr/bin/env bash
# Smoke-test the full auth chain. Runs ALL checks and reports a summary,
# rather than dying on the first failure.
# Load .env if present (GOOGLE_CLOUD_PROJECT, optional CLOUDSDK_CONFIG, GEMINI_API_KEY)
HERE="$(cd "$(dirname "$0")" && pwd)"
if [ -f "$HERE/.env" ]; then set -a; . "$HERE/.env"; set +a; fi
PROJECT="${GOOGLE_CLOUD_PROJECT:-$(gcloud config get-value project 2>/dev/null)}"
PASS=0; FAIL=0
ok(){ echo "  [ OK ] $1"; PASS=$((PASS+1)); }
no(){ echo "  [FAIL] $1"; echo "         fix: $2"; FAIL=$((FAIL+1)); }

echo "=============================================="
echo " Auth chain check"
echo " config dir : ${CLOUDSDK_CONFIG:-$HOME/.config/gcloud}"
echo " account    : $(gcloud config get-value account 2>/dev/null)"
echo " project    : $PROJECT"
echo "=============================================="

# 1. CLI credential
if gcloud auth list --filter=status:ACTIVE --format='value(account)' 2>/dev/null | grep -q '@'; then
  ok "gcloud CLI login"
else
  no "gcloud CLI login" "gcloud auth login <your-email> --no-launch-browser"
fi

# 2. ADC -- this is what bq_client.py / google-cloud-bigquery uses
if [ -f "${CLOUDSDK_CONFIG:-$HOME/.config/gcloud}/application_default_credentials.json" ]; then
  ok "ADC file present"
else
  no "ADC file present" "gcloud auth application-default login --no-launch-browser"
fi

# 3. BigQuery API enabled
if gcloud services list --enabled 2>/dev/null | grep -q '^bigquery.googleapis.com'; then
  ok "BigQuery API enabled"
else
  no "BigQuery API enabled" "gcloud services enable bigquery.googleapis.com"
fi

# 4. Dry-run query (bills 0 bytes, proves read access to the public dataset)
if gcloud auth application-default print-access-token >/dev/null 2>&1; then
  if bq query --use_legacy_sql=false --dry_run \
      'SELECT COUNT(*) FROM `bigquery-public-data.thelook_ecommerce.orders`' >/dev/null 2>&1; then
    ok "dry-run query on thelook_ecommerce"
  else
    no "dry-run query on thelook_ecommerce" "see error: bq query --dry_run ... (run manually)"
  fi
else
  no "ADC usable by client libraries" "gcloud auth application-default login --no-launch-browser"
fi

# 5. Python client path -- the one that actually matters for the prototype
PY="$HERE/.venv/bin/python"; [ -x "$PY" ] || PY=python3
if $PY -c "
from google.cloud import bigquery
c = bigquery.Client(project='$PROJECT')
r = list(c.query('SELECT COUNT(*) AS n FROM \`bigquery-public-data.thelook_ecommerce.orders\`').result())
print('rows_in_orders=%d' % r[0].n)
" 2>/dev/null; then
  ok "python google-cloud-bigquery end-to-end"
else
  no "python google-cloud-bigquery end-to-end" "pip install google-cloud-bigquery, and finish ADC login"
fi

echo "----------------------------------------------"
echo " passed: $PASS   failed: $FAIL"
[ "$FAIL" -eq 0 ] && echo " ALL GREEN - ready to build." || echo " Fix the [FAIL] lines above, then re-run."
echo "=============================================="
