#!/bin/sh
# ──────────────────────────────────────────────
# Yusta Dify Bootstrap Script
# ──────────────────────────────────────────────
# Runs once at startup to:
#   1. Install docker CLI in this container
#   2. Wait for Dify API to be healthy
#   3. Run init.py inside the API container via docker exec
#
# The init.py script (mounted into the API container) handles:
#   - Flask DB migrations check
#   - Admin account + workspace creation
#   - openai_api_compatible model provider configuration
#   - DSL workflow file import with model name replacement
# ──────────────────────────────────────────────
set -eu

# ── Configuration ────────────────────────────
API_BASE="${CONSOLE_API_URL:-http://api:5001}"
MAX_RETRIES=${MAX_RETRIES:-90}
RETRY_INTERVAL=${RETRY_INTERVAL:-3}

# ── Helpers ──────────────────────────────────
log()  { echo "[yusta-init] $(date '+%H:%M:%S') | $*"; }
warn() { echo "[yusta-init] $(date '+%H:%M:%S') | ⚠  $*" >&2; }
fail() { echo "[yusta-init] $(date '+%H:%M:%S') | ❌ $*" >&2; exit 1; }
ok()   { echo "[yusta-init] $(date '+%H:%M:%S') | ✅ $*"; }

# ── Install docker CLI ───────────────────────
log "Installing docker CLI..."
apk add --no-cache docker-cli curl >/dev/null 2>&1
ok "Docker CLI installed"

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

# ── Run bootstrap inside API container ───────
log "Running bootstrap inside API container..."
docker exec yusta-dify-api python /init.py 2>&1 | while IFS= read -r line; do
  echo "[yusta-init] $line"
done

ok "Bootstrap finished!"
