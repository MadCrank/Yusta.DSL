# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## ⚠️  Mandatory workflow

**For ANY task involving DSL workflows — editing, testing, deploying, debugging — use ONLY these tools, in this order. Never bypass them. Never touch the database, Dify API, or container internals directly unless a tool is broken and needs fixing.**

```
edit DSL  →  validate  →  deploy  →  test
```

## Development workflow

### Quick cycle (edit → validate → deploy → test)

```bash
# 1. Validate DSL before deploying
./venv/bin/dify-workflow validate dsls/Yusta.yml
./venv/bin/dify-workflow checklist dsls/Yusta.yml

# 2. Inspect workflow structure (tree / JSON / Mermaid)
./venv/bin/dify-workflow inspect dsls/Yusta.yml
./venv/bin/dify-workflow inspect dsls/Yusta.yml --mermaid

# 3. Deploy DSL to running Dify (no restart needed)
docker exec yusta-dify-api python /deploy.py
# Deploy a single file:
docker exec yusta-dify-api python /deploy.py dsls/Yusta.yml
# JSON output (for scripts):
docker exec yusta-dify-api python /deploy.py --json dsls/Yusta.yml

# 4. Test via API
./scripts/test.sh "Привет, расскажи о себе"
./scripts/test.sh --stream "What is Yusta?"
```

### dify-workflow CLI (local validation & editing)

```bash
# Validate (auto-detects mode: workflow/chatflow/chat/agent/completion)
./venv/bin/dify-workflow validate dsls/Yusta.yml
./venv/bin/dify-workflow validate dsls/Yusta.yml --strict   # warnings = errors

# Pre-publish checklist (mirrors Dify UI pre-publish checks)
./venv/bin/dify-workflow checklist dsls/Yusta.yml

# Inspect structure
./venv/bin/dify-workflow inspect dsls/Yusta.yml             # Rich tree
./venv/bin/dify-workflow inspect dsls/Yusta.yml -j          # JSON
./venv/bin/dify-workflow inspect dsls/Yusta.yml --mermaid   # Flowchart

# Auto-layout nodes (Dify-style left-to-right)
./venv/bin/dify-workflow layout -f dsls/Yusta.yml -o dsls/Yusta.yml

# Diff two versions
./venv/bin/dify-workflow diff dsls/Yusta.yml dsls/Yusta_v2.yml

# Create a new workflow from template
./venv/bin/dify-workflow create --mode chatflow --template llm -o dsls/new_flow.yml

# Guide
./venv/bin/dify-workflow guide
```

### How the tools work

1. **Edit** — use `dify-workflow edit` CLI for structural changes (nodes, edges, connections). For prompt/model text changes, direct YAML editing in IDE is acceptable. Prefer `dify-workflow edit update-node --data-file` for programmatic node updates where the CLI can maintain YAML integrity better than raw text editing
2. **Validate** — `dify-workflow validate` catches cycles, missing nodes, frontend crashes, and variable ref errors before Dify sees them
3. **Deploy** — `deploy.py` replaces placeholder model names (`pro`/`lite`) from `.env`, finds existing app by name, deletes and reimports (idempotent). **Automatically publishes the workflow and creates a new API token** — copy it from JSON output to `.env` as `DIFY_API_KEY` for `test.sh`.
4. **Test** — `test.sh` sends a question to the Dify Service API and prints the answer. Supports blocking and streaming modes

## Commands

```bash
# Start the Dify stack (first run: auto-initializes everything)
docker compose up -d

# Full teardown including volumes (fresh start — wipes all data)
docker compose down -v

# View bootstrap logs
docker logs yusta-dify-init

# Check Dify API health
curl http://localhost:5001/health

# Web UI
open http://localhost:3000
```

## Architecture (reference)

This repo provides a Docker Compose development environment for [Dify](https://github.com/langgenius/dify), pre-configured with the **Yusta** AI assistant DSL workflow.

### Service topology

| Service | Image | Purpose |
|---------|-------|---------|
| `db` | `postgres:15-alpine` | Main app DB (`dify`) + plugin DB (`dify_plugin`) |
| `redis` | `redis:7-alpine` | Celery broker + cache |
| `sandbox` | `langgenius/dify-sandbox` | Code executor for Jinja2 templates and Code nodes |
| `api` | `langgenius/dify-api:${DIFY_VERSION}` | Flask API server (gunicorn) |
| `worker` | `langgenius/dify-api:${DIFY_VERSION}` | Celery worker for async tasks |
| `plugin_daemon` | `langgenius/dify-plugin-daemon:0.6.3-local` | Plugin lifecycle manager. Required for model providers in Dify ≥1.0 |
| `web` | `langgenius/dify-web:${DIFY_VERSION}` | Next.js frontend on `:3000` |
| `init` | `alpine:3.21` | One-shot bootstrap: creates admin, configures models, imports DSLs |

### Key env vars

```env
OPENAI_API_KEY=sk-xxx
OPENAI_API_BASE_URL=https://api.deepseek.com
DIFY_MODEL_PRO=deepseek-v4-flash        # replaces "pro" / "pro-2026-03-01" in DSL
DIFY_MODEL_LITE=deepseek-v4-flash       # replaces "lite" in DSL
DIFY_API_KEY=app-xxx                    # for test.sh (get from deploy.py output)
```

### File layout

```
dsls/              # Dify DSL workflow YAML files
scripts/
  init.sh          # Bootstrap entrypoint (runs init.py inside API container)
  init.py          # Account, provider, DSL import
  deploy.py        # Manual DSL re-import without restart
  test.sh          # Quick chatflow test via curl
plugins/
  openai_api_compatible.difypkg   # Plugin package for offline install
```

### Persistence

- `./volumes/` is a bind mount — survives `docker compose down -v`. Delete manually for clean slate.
- `docker compose down` (without `-v`) preserves all state.
