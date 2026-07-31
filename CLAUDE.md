# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

This repo provides a Docker Compose development environment for [Dify](https://github.com/langgenius/dify), pre-configured with the **Yusta** AI assistant DSL workflow. On `docker compose up -d`, a bootstrap script automatically creates the admin account, configures the OpenAI-compatible model provider, and imports DSL workflows from `dsls/`.

## Commands

```bash
# Start the Dify stack (first run: auto-initializes everything)
docker compose up -d

# Full teardown including volumes (fresh start — wipes all data)
docker compose down -v

# View bootstrap logs
docker logs yusta-dify-init

# Run bootstrap manually (e.g., after fixing init.py)
docker cp scripts/init.py yusta-dify-api:/init.py && docker exec yusta-dify-api python /init.py

# Check Dify API health
curl http://localhost:5001/health

# Web UI
open http://localhost:3000
```

## Architecture

### Service topology (7 containers)

| Service | Image | Purpose |
|---------|-------|---------|
| `db` | `postgres:15-alpine` | Main app DB (`dify`) + plugin DB (`dify_plugin`, created by `scripts/init-db.sh`) |
| `redis` | `redis:7-alpine` | Celery broker + cache |
| `api` | `langgenius/dify-api:${DIFY_VERSION}` | Flask API server (gunicorn). Runs DB migrations via `MIGRATION_ENABLED=true` before starting |
| `worker` | `langgenius/dify-api:${DIFY_VERSION}` | Celery worker for async tasks |
| `plugin_daemon` | `langgenius/dify-plugin-daemon:0.6.3-local` | Plugin lifecycle manager. Has its own versioning (separate from Dify API). **Required** for model providers in Dify ≥1.0 |
| `web` | `langgenius/dify-web:${DIFY_VERSION}` | Next.js frontend on `:3000` |
| `init` | `alpine:3.21` | One-shot bootstrap container. Installs Docker CLI, waits for API health, then runs `init.py` inside the API container via `docker exec` |

### Bootstrap flow (`scripts/init.py`)

The bootstrap script runs inside the API container to access Dify's internal Python services directly (bypassing HTTP auth, CSRF, and password encryption):

1. **Wait for DB** — polls `accounts` table until migrations complete
2. **Create admin** — `AccountService.create_account(is_setup=True)` + `TenantService.create_owner_tenant_if_not_exist(is_setup=True)`
3. **Mark setup complete** — inserts into `dify_setups` table (prevents the web UI install wizard)
4. **Configure model credentials** — waits for plugin daemon to register the `openai_api_compatible` provider, then calls `ModelProviderService.create_model_credential()` for each model in `{DIFY_MODEL_PRO, DIFY_MODEL_LITE}`
5. **Import DSLs** — reads `.yml`/`.yaml` from `dsls/`, replaces model names (`pro`/`lite`/`pro-2026-03-01` → configured model names), imports via `AppDslService.import_app()`. Skips apps already imported (idempotent by workspace app name)

### Plugin architecture (critical for Dify ≥1.0)

Starting from Dify 1.0, **all model providers are plugins** managed by `plugin_daemon`. This means:

- `HOSTED_OPENAI_API_KEY` only works for the built-in OpenAI provider — NOT for `openai_api_compatible`
- The `openai_api_compatible` plugin must be installed (one-time, persists in `volumes/plugin_daemon/`)
- Model credentials are **per-model** (not per-provider) — use `create_model_credential()`, NOT `create_provider_credential()`. The `openai_api_compatible` plugin has no `provider_credential_schema`
- Plugin daemon auth key (`PLUGIN_DAEMON_KEY` / `SERVER_KEY`) must match between `api` and `plugin_daemon` services
- `PLUGIN_DIFY_INNER_API_URL` / `PLUGIN_DIFY_INNER_API_KEY` use different variable names inside the daemon container (`DIFY_INNER_API_URL` / `DIFY_INNER_API_KEY`)

### Key env vars for model configuration

```env
# In .env — mapped to both api container (HOSTED_OPENAI_*) and init.py
OPENAI_API_KEY=sk-xxx
OPENAI_API_BASE_URL=https://api.deepseek.com
DIFY_MODEL_PRO=deepseek-v4-flash        # replaces "pro" / "pro-2026-03-01" in DSL
DIFY_MODEL_LITE=deepseek-v4-flash       # replaces "lite" in DSL
```

### File layout

```
dsls/              # Dify DSL workflow YAML files (auto-imported on boot)
  Yusta.yml
scripts/
  init.sh          # Entrypoint for init container: waits for API, runs init.py via docker exec
  init.py          # Python bootstrap: account, provider, DSL import (runs inside API container)
  init-db.sh       # Creates dify_plugin DB on PostgreSQL first boot (docker-entrypoint-initdb.d)
plugins/
  openai_api_compatible.difypkg   # Plugin package for offline install
```

### Persistence notes

- `./volumes/` is a **bind mount** (not a Docker volume) — survives `docker compose down -v`. Delete manually for a truly clean slate: `rm -rf ./volumes/`
- Plugin installation persists in `volumes/plugin_daemon/plugin/` and `volumes/plugin_daemon/plugin_packages/`
- `docker compose down` (without `-v`) preserves all state — data, accounts, plugins, DSL imports
