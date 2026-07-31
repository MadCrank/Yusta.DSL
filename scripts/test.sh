#!/usr/bin/env bash
#
# Yusta DSL Test — quick chatflow test via Dify Service API
# ============================================================
#
# Usage:
#   ./scripts/test.sh "Your question"                    # blocking mode
#   ./scripts/test.sh --stream "Your question"           # streaming (SSE)
#   ./scripts/test.sh --api-key app-xxx "Your question"  # explicit API key
#   ./scripts/test.sh --help                             # full usage
#
# Prerequisites:
#   - Dify stack running (docker compose up -d)
#   - API key for the app (get from Dify UI: App → API Access)
#   - Or set DIFY_API_KEY in .env or environment

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="${REPO_ROOT}/.env"

# ── Defaults ───────────────────────────────
API_HOST="${DIFY_API_HOST:-localhost:5001}"
STREAMING=false
API_KEY=""
QUESTION=""

# ── Help ────────────────────────────────────
usage() {
    cat <<EOF
Usage: $(basename "$0") [OPTIONS] QUESTION

Test a Dify chatflow by sending a question via the Service API.

Arguments:
  QUESTION              The question to send (required). Use quotes for spaces.

Options:
  --stream              Stream the response (SSE mode, shows tokens as they arrive)
  --api-key KEY         API key for the app (default: \$DIFY_API_KEY from .env)
  --api-host HOST:PORT  Dify API host (default: localhost:5001)
  --user USER           User identifier (default: yusta-test)
  --help                Show this help and exit

Environment:
  DIFY_API_KEY          API key (can also be set in .env file)
  DIFY_API_HOST         API host:port

Examples:
  ./scripts/test.sh "Привет, расскажи о себе"
  ./scripts/test.sh --stream "What is Yusta?"
  ./scripts/test.sh --api-key app-xxxxxxxx "Hello"
  DIFY_API_KEY=app-xxx ./scripts/test.sh "Test question"

Setup:
  1. Open Dify UI: http://localhost:3000
  2. Go to your app → API Access
  3. Create an API key (or copy existing)
  4. Add to .env: DIFY_API_KEY=app-xxxxxxxx
EOF
    exit 0
}

# ── Parse arguments ─────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        --help|-h)
            usage
            ;;
        --stream)
            STREAMING=true
            shift
            ;;
        --api-key)
            API_KEY="$2"
            shift 2
            ;;
        --api-host)
            API_HOST="$2"
            shift 2
            ;;
        --user)
            DIFY_USER="$2"
            shift 2
            ;;
        --)
            shift
            QUESTION="$*"
            break
            ;;
        -*)
            echo "❌ Unknown option: $1" >&2
            echo "   Use --help for usage." >&2
            exit 1
            ;;
        *)
            # Remaining args = question
            QUESTION="$*"
            break
            ;;
    esac
done

# ── Load API key ────────────────────────────
if [[ -z "$API_KEY" ]] && [[ -f "$ENV_FILE" ]]; then
    # shellcheck disable=SC1090
    source "$ENV_FILE" 2>/dev/null || true
    API_KEY="${DIFY_API_KEY:-}"
fi

# ── Validate ────────────────────────────────
if [[ -z "$QUESTION" ]]; then
    echo "❌ No question provided." >&2
    echo "   Usage: $(basename "$0") [OPTIONS] QUESTION" >&2
    echo "   Try:   $(basename "$0") --help" >&2
    exit 1
fi

if [[ -z "$API_KEY" ]]; then
    echo "❌ No API key provided." >&2
    echo "" >&2
    echo "   Set DIFY_API_KEY in .env or use --api-key:" >&2
    echo "     ./scripts/test.sh --api-key app-xxxxxxxx 'Your question'" >&2
    echo "" >&2
    echo "   To get an API key:" >&2
    echo "     1. Open http://localhost:3000" >&2
    echo "     2. Go to your app → API Access" >&2
    echo "     3. Create or copy an API key" >&2
    exit 1
fi

DIFY_USER="${DIFY_USER:-yusta-test}"

# ── Build request ───────────────────────────
API_URL="http://${API_HOST}/v1/chat-messages"

if [[ "$STREAMING" == "true" ]]; then
    RESPONSE_MODE="streaming"
else
    RESPONSE_MODE="blocking"
fi

# JSON payload (using jq if available, otherwise manual)
if command -v jq &>/dev/null; then
    PAYLOAD=$(jq -n \
        --arg q "$QUESTION" \
        --arg u "$DIFY_USER" \
        --arg rm "$RESPONSE_MODE" \
        '{
            inputs: {},
            query: $q,
            response_mode: $rm,
            user: $u
        }')
else
    PAYLOAD=$(cat <<EOJSON
{
    "inputs": {},
    "query": $(echo "$QUESTION" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))'),
    "response_mode": "$RESPONSE_MODE",
    "user": "$DIFY_USER"
}
EOJSON
)
fi

# ── Send request ────────────────────────────
if [[ "$STREAMING" == "true" ]]; then
    # Streaming mode: read SSE events line by line
    echo "🚀 Sending to Yusta (streaming)..."
    echo ""

    HTTP_CODE=$(curl -s -w "%{http_code}" -o /tmp/yusta-test-response.txt \
        --no-buffer \
        -X POST "$API_URL" \
        -H "Authorization: Bearer $API_KEY" \
        -H "Content-Type: application/json" \
        -d "$PAYLOAD" 2>/dev/null || echo "000")

    if [[ "$HTTP_CODE" == "200" ]]; then
        # Parse SSE events: extract answer tokens
        while IFS= read -r line; do
            if [[ "$line" =~ ^data:\ *(.*) ]]; then
                data="${BASH_REMATCH[1]}"
                # Skip [DONE]
                [[ "$data" == "[DONE]" ]] && continue
                # Try to extract 'answer' field
                if command -v jq &>/dev/null; then
                    answer_part=$(echo "$data" | jq -r '.answer // empty' 2>/dev/null || true)
                    if [[ -n "$answer_part" ]]; then
                        printf "%s" "$answer_part"
                    fi
                else
                    # Fallback: crude extraction
                    answer_part=$(echo "$data" | python3 -c "
import json, sys
try:
    d = json.loads(sys.stdin.read())
    print(d.get('answer', ''), end='')
except: pass
" 2>/dev/null || true)
                    if [[ -n "$answer_part" ]]; then
                        printf "%s" "$answer_part"
                    fi
                fi
            fi
        done < /tmp/yusta-test-response.txt
        echo ""
        echo ""
        echo "✅ Done (streaming)"
    else
        echo "❌ HTTP $HTTP_CODE" >&2
        echo "" >&2
        cat /tmp/yusta-test-response.txt >&2
        exit 1
    fi
else
    # Blocking mode: get full response
    echo "🚀 Sending to Yusta (blocking)..."
    echo ""

    HTTP_CODE=$(curl -s -w "%{http_code}" -o /tmp/yusta-test-response.txt \
        -X POST "$API_URL" \
        -H "Authorization: Bearer $API_KEY" \
        -H "Content-Type: application/json" \
        -d "$PAYLOAD" 2>/dev/null || echo "000")

    if [[ "$HTTP_CODE" == "200" ]]; then
        if command -v jq &>/dev/null; then
            ANSWER=$(jq -r '.answer // .message // empty' /tmp/yusta-test-response.txt 2>/dev/null || true)
            echo "📝 Answer:"
            echo "──────────────────────────────────────────────────"
            echo "$ANSWER"
            echo "──────────────────────────────────────────────────"
            echo ""
            # Show metadata if available
            CONV_ID=$(jq -r '.conversation_id // empty' /tmp/yusta-test-response.txt 2>/dev/null || true)
            MSG_ID=$(jq -r '.message_id // empty' /tmp/yusta-test-response.txt 2>/dev/null || true)
            if [[ -n "$CONV_ID" ]]; then
                echo "📋 conversation_id: $CONV_ID"
            fi
            if [[ -n "$MSG_ID" ]]; then
                echo "📋 message_id: $MSG_ID"
            fi
        else
            echo "📝 Raw response:"
            echo "──────────────────────────────────────────────────"
            cat /tmp/yusta-test-response.txt
            echo ""
            echo "──────────────────────────────────────────────────"
            echo "💡 Install 'jq' for formatted output: brew install jq"
        fi
        echo ""
        echo "✅ Done"
    elif [[ "$HTTP_CODE" == "401" ]]; then
        echo "❌ HTTP 401 Unauthorized — invalid API key" >&2
        echo "   Check your DIFY_API_KEY in .env" >&2
        cat /tmp/yusta-test-response.txt >&2
        exit 1
    elif [[ "$HTTP_CODE" == "404" ]]; then
        echo "❌ HTTP 404 Not Found — app or endpoint not found" >&2
        echo "   Make sure the app is imported and published in Dify" >&2
        cat /tmp/yusta-test-response.txt >&2
        exit 1
    elif [[ "$HTTP_CODE" == "000" ]]; then
        echo "❌ Connection refused — is Dify running?" >&2
        echo "   Try: docker compose up -d" >&2
        echo "   Check: curl http://localhost:5001/health" >&2
        exit 1
    else
        echo "❌ HTTP $HTTP_CODE" >&2
        cat /tmp/yusta-test-response.txt >&2
        exit 1
    fi
fi

# Cleanup
rm -f /tmp/yusta-test-response.txt
