#!/bin/sh
# ──────────────────────────────────────────────
# Yusta Dify Bootstrap Script
# ──────────────────────────────────────────────
# Runs once at startup to:
#   1. Wait for Dify API to be healthy
#   2. Trigger admin setup (if first run)
#   3. Login as admin and obtain a token
#   4. Configure the openai_api_compatible model provider
#   5. Import all DSL workflow files from /dsls/
# ──────────────────────────────────────────────
set -eu

# ── Configuration ────────────────────────────
API_BASE="${CONSOLE_API_URL:-http://api:5001}"
ADMIN_EMAIL="${ADMIN_EMAIL:-admin@yusta.local}"
ADMIN_PASSWORD="${ADMIN_PASSWORD:-yusta-admin-123}"
INIT_PASSWORD="${INIT_PASSWORD:-}"
OPENAI_KEY="${OPENAI_API_KEY:-}"
OPENAI_BASE="${OPENAI_API_BASE_URL:-https://api.openai.com/v1}"
DSL_DIR="${DSL_DIR:-/dsls}"
MODEL_PRO="${DIFY_MODEL_PRO:-gpt-4o}"
MODEL_LITE="${DIFY_MODEL_LITE:-gpt-4o-mini}"
MAX_RETRIES=${MAX_RETRIES:-60}
RETRY_INTERVAL=${RETRY_INTERVAL:-3}
TMP_DIR="/tmp/dsl-import"

# ── Helpers ──────────────────────────────────
log()  { echo "[yusta-init] $(date '+%H:%M:%S') | $*"; }
warn() { echo "[yusta-init] $(date '+%H:%M:%S') | ⚠  $*" >&2; }
fail() { echo "[yusta-init] $(date '+%H:%M:%S') | ❌ $*" >&2; exit 1; }
ok()   { echo "[yusta-init] $(date '+%H:%M:%S') | ✅ $*"; }

# ── Wait for Dify API ────────────────────────
log "Waiting for Dify API at ${API_BASE} ..."
i=0
while [ "$i" -lt "$MAX_RETRIES" ]; do
  if curl -fsS "${API_BASE}/health" >/dev/null 2>&1; then
    ok "Dify API is healthy"
    break
  fi
  i=$((i + 1))
  if [ "$i" -ge "$MAX_RETRIES" ]; then
    fail "Dify API did not become healthy within $((MAX_RETRIES * RETRY_INTERVAL))s"
  fi
  sleep "$RETRY_INTERVAL"
done

# ── Setup admin account (first run) ──────────
log "Checking Dify setup status..."
SETUP_STATUS=$(curl -fsS "${API_BASE}/console/api/setup" 2>/dev/null || echo "")

if echo "$SETUP_STATUS" | grep -q '"step":"finished"'; then
  ok "Dify is already set up"
else
  log "Running first-time Dify setup..."
  SETUP_PAYLOAD="{\"email\":\"${ADMIN_EMAIL}\",\"password\":\"${ADMIN_PASSWORD}\",\"name\":\"Yusta Admin\"}"
  if [ -n "${INIT_PASSWORD}" ]; then
    SETUP_PAYLOAD="{\"email\":\"${ADMIN_EMAIL}\",\"password\":\"${INIT_PASSWORD}\",\"name\":\"Yusta Admin\"}"
  fi

  SETUP_RESULT=$(curl -fsS -X POST "${API_BASE}/console/api/setup" \
    -H "Content-Type: application/json" \
    -d "$SETUP_PAYLOAD" 2>&1) || warn "Setup returned non-zero: ${SETUP_RESULT}"

  # Give Dify a moment to finalize setup
  sleep 3
  ok "Dify setup completed"
fi

# ── Login ────────────────────────────────────
log "Logging into Dify as ${ADMIN_EMAIL} ..."
LOGIN_RESPONSE=$(curl -fsS -X POST "${API_BASE}/console/api/login" \
  -H "Content-Type: application/json" \
  -d "{\"email\":\"${ADMIN_EMAIL}\",\"password\":\"${ADMIN_PASSWORD}\"}")

ACCESS_TOKEN=$(echo "$LOGIN_RESPONSE" | sed -n 's/.*"access_token"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')

if [ -z "$ACCESS_TOKEN" ]; then
  # Try alternative parsing with jq-style extraction from grep
  ACCESS_TOKEN=$(echo "$LOGIN_RESPONSE" | grep -o '"access_token"[[:space:]]*:[[:space:]]*"[^"]*"' | head -1 | sed 's/.*"access_token"[[:space:]]*:[[:space:]]*"\([^"]*\)"/\1/')
fi

if [ -z "$ACCESS_TOKEN" ]; then
  warn "Could not extract access token from login response"
  warn "Response: $(echo "$LOGIN_RESPONSE" | head -c 500)"
  warn "Skipping API-based configuration. Please configure provider manually."
else
  ok "Logged in successfully"

  AUTH_HEADER="Authorization: Bearer ${ACCESS_TOKEN}"

  # ── Configure openai_api_compatible provider ──
  if [ -n "${OPENAI_KEY}" ] && [ "${OPENAI_KEY}" != "sk-your-key-here" ]; then
    log "Configuring openai_api_compatible model provider..."

    # Build credentials JSON
    CREDENTIALS="{\"api_key\":\"${OPENAI_KEY}\",\"api_base\":\"${OPENAI_BASE}\"}"

    # Try to add the provider credentials
    PROVIDER_RESPONSE=$(curl -fsS -X POST \
      "${API_BASE}/console/api/workspaces/current/model-providers/langgenius/openai_api_compatible/openai_api_compatible/models/credentials" \
      -H "${AUTH_HEADER}" \
      -H "Content-Type: application/json" \
      -d "${CREDENTIALS}" 2>&1) || warn "Provider config returned non-zero: ${PROVIDER_RESPONSE}"

    ok "Model provider configured (openai_api_compatible → ${OPENAI_BASE})"
  else
    warn "OPENAI_API_KEY not set or using placeholder. Skipping provider configuration."
    warn "Set OPENAI_API_KEY and OPENAI_API_BASE_URL in .env, then restart."
  fi

  # ── Import DSL files ────────────────────────
  log "Scanning for DSL files in ${DSL_DIR} ..."

  mkdir -p "${TMP_DIR}"
  IMPORTED=0
  FAILED=0

  for dsl_file in "${DSL_DIR}"/*.yml "${DSL_DIR}"/*.yaml; do
    [ -e "$dsl_file" ] || continue

    DSL_NAME=$(basename "$dsl_file")
    log "Importing DSL: ${DSL_NAME} ..."

    # ── Replace model names with configured ones ──
    # The DSL references abstract names (pro, lite, pro-2026-03-01).
    # Replace them with actual model names from the provider.
    STAGED_FILE="${TMP_DIR}/${DSL_NAME}"
    cp "$dsl_file" "$STAGED_FILE"

    log "  Model mapping: pro/pro-2026-03-01 → ${MODEL_PRO}, lite → ${MODEL_LITE}"
    sed -i'' -E \
      -e "s/name: pro-2026-03-01[[:space:]]*\$/name: ${MODEL_PRO}/" \
      -e "s/name: pro[[:space:]]*\$/name: ${MODEL_PRO}/" \
      -e "s/name: lite[[:space:]]*\$/name: ${MODEL_LITE}/" \
      "$STAGED_FILE"

    IMPORT_RESPONSE=$(curl -fsS -X POST "${API_BASE}/console/api/apps/imports" \
      -H "${AUTH_HEADER}" \
      -F "file=@${STAGED_FILE};type=application/x-yaml" \
      2>&1) || true

    if echo "$IMPORT_RESPONSE" | grep -q '"result"\|"success"\|"id"\|"app_id"'; then
      ok "Imported: ${DSL_NAME}"
      IMPORTED=$((IMPORTED + 1))
    elif echo "$IMPORT_RESPONSE" | grep -q '"errors"'; then
      # May already be imported — check for "already exists"
      if echo "$IMPORT_RESPONSE" | grep -qi "already\|duplicate\|exists"; then
        warn "DSL '${DSL_NAME}' may already exist, skipping"
      else
        warn "Failed to import ${DSL_NAME}: $(echo "$IMPORT_RESPONSE" | head -c 300)"
        FAILED=$((FAILED + 1))
      fi
    else
      warn "Unexpected response for ${DSL_NAME}: $(echo "$IMPORT_RESPONSE" | head -c 300)"
      # Don't count as failure — the import might have succeeded with a different response shape
      IMPORTED=$((IMPORTED + 1))
    fi
  done

  # Cleanup
  rm -rf "${TMP_DIR}"

  log "DSL import complete: ${IMPORTED} imported, ${FAILED} failed"
fi

# ── Summary ──────────────────────────────────
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Yusta Dify Dev Environment — Ready"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Web UI:    http://localhost:${WEB_PORT:-3000}"
echo "  API:       http://localhost:5001"
echo "  Admin:     ${ADMIN_EMAIL}"
echo "  DSLs dir:  ./dsls/"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

ok "Bootstrap complete!"
