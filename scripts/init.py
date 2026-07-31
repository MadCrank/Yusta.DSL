#!/usr/bin/env python3
"""
Yusta Dify Bootstrap — Python Edition
======================================
Runs inside the Dify API container to:
  1. Create admin account + workspace (if first run)
  2. Configure openai_api_compatible model provider
  3. Import all DSL workflow files from /dsls/
"""
import os
import sys
import re
import time
import json

sys.path.insert(0, "/app/api")

import yaml
from pathlib import Path

# ── Configuration (from env vars) ────────────
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "admin@yusta.local")
ADMIN_NAME = os.getenv("ADMIN_NAME", "Yusta Admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "yusta-admin-123")
OPENAI_KEY = os.getenv("OPENAI_API_KEY", "") or os.getenv("HOSTED_OPENAI_API_KEY", "")
OPENAI_BASE = os.getenv("OPENAI_API_BASE_URL", "") or os.getenv("HOSTED_OPENAI_API_BASE", "https://api.openai.com/v1")
MODEL_PRO = os.getenv("DIFY_MODEL_PRO", "gpt-4o")
MODEL_LITE = os.getenv("DIFY_MODEL_LITE", "gpt-4o-mini")
DSL_DIR = Path(os.getenv("DSL_DIR", "/dsls"))
LANGUAGE = os.getenv("DIFY_LANGUAGE", "en-US")

PROVIDER_NAME = "langgenius/openai_api_compatible/openai_api_compatible"


def log(msg: str) -> None:
    print(f"[yusta-init] {time.strftime('%H:%M:%S')} | {msg}")


def ok(msg: str) -> None:
    print(f"[yusta-init] {time.strftime('%H:%M:%S')} | ✅ {msg}")


def warn(msg: str) -> None:
    print(f"[yusta-init] {time.strftime('%H:%M:%S')} | ⚠  {msg}", file=sys.stderr)


def fail(msg: str) -> None:
    print(f"[yusta-init] {time.strftime('%H:%M:%S')} | ❌ {msg}", file=sys.stderr)
    sys.exit(1)


def bootstrap() -> None:
    from app import create_app
    from extensions.ext_database import db
    from models.account import Account, Tenant, TenantAccountJoin
    from services.account_service import AccountService, TenantService

    app = create_app()
    ctx = app.app_context()
    ctx.push()

    try:
        # ── Step 0: Wait for DB migrations ──────
        log("Checking database state...")
        # If the accounts table doesn't exist yet, migrations haven't run
        retries = 0
        while retries < 30:
            try:
                db.session.execute(db.text("SELECT 1 FROM accounts LIMIT 0"))
                break
            except Exception:
                retries += 1
                if retries >= 30:
                    fail("Database not ready after 60s")
                time.sleep(2)
        ok("Database is ready")

        # ── Step 1: Find or create admin account ──
        log(f"Looking up account: {ADMIN_EMAIL}")
        account = (
            db.session.query(Account)
            .filter(Account.email == ADMIN_EMAIL)
            .first()
        )

        if account is None:
            log("Creating admin account + workspace...")
            account = AccountService.create_account(
                email=ADMIN_EMAIL,
                name=ADMIN_NAME,
                interface_language=LANGUAGE,
                password=ADMIN_PASSWORD,
                is_setup=True,
            )
            db.session.commit()
            # Create tenant (workspace) for the account
            TenantService.create_owner_tenant_if_not_exist(account=account, is_setup=True)
            db.session.commit()
            # Refresh from DB to get the tenant relationship
            db.session.refresh(account)
            ok(f"Created admin: {ADMIN_EMAIL}")
        else:
            ok(f"Admin account exists: {ADMIN_EMAIL}")

        # ── Complete Dify setup (marks setup as finished in dify_setups) ──
        # Without this the web UI keeps showing the install/setup wizard.
        result = db.session.execute(
            db.text("SELECT version FROM dify_setups LIMIT 1")
        ).fetchall()
        if not result:
            log("Marking Dify setup as complete...")
            db.session.execute(
                db.text(
                    "INSERT INTO dify_setups (version, setup_at) VALUES (:ver, NOW())"
                ),
                {"ver": "1.13.3"},
            )
            db.session.commit()
            ok("Dify setup marked as complete")

        # Get the tenant for this account (just created or existing)
        join = (
            db.session.query(TenantAccountJoin)
            .filter(
                TenantAccountJoin.account_id == account.id,
            )
            .order_by(TenantAccountJoin.id.asc())
            .first()
        )
        if not join:
            fail("No workspace found for admin account")
        tenant = db.session.query(Tenant).filter(Tenant.id == join.tenant_id).first()
        if not tenant:
            fail("Workspace not found in database")

        # Ensure the join is marked as current and the account references the tenant
        if not join.current:
            join.current = True
        account.current_tenant = tenant
        db.session.commit()

        log(f"Using workspace: {tenant.name} (id={tenant.id})")

        # ── Step 2: Configure model credentials ──
        # Dify 1.13 uses a plugin architecture. The openai_api_compatible
        # plugin must be installed once (persists in volumes/plugin_daemon/).
        # If missing, install via web UI: Plugins → Marketplace → search
        # "OpenAI API-compatible" → Install.
        if OPENAI_KEY and OPENAI_KEY != "sk-your-key-here":
            log("Configuring openai_api_compatible model credentials...")
            provider = "langgenius/openai_api_compatible/openai_api_compatible"
            try:
                from services.model_provider_service import ModelProviderService
                mps = ModelProviderService()

                # Wait for provider to be available (plugin may still be loading)
                provider_ready = False
                for attempt in range(10):
                    try:
                        mps._get_provider_configuration(tenant.id, provider)
                        provider_ready = True
                        break
                    except Exception:
                        time.sleep(3)

                if not provider_ready:
                    warn("openai_api_compatible plugin not installed!")
                    warn("Install it once via web UI:")
                    warn("  Plugins → Marketplace → 'OpenAI API-compatible' → Install")
                    warn("Then restart: docker compose down && docker compose up -d")
                else:
                    base_credentials = {
                        "api_key": OPENAI_KEY,
                        "endpoint_url": OPENAI_BASE,
                        "context_size": "131072",
                        "mode": "chat",
                        "compatibility_mode": "strict",
                        "function_calling_type": "no_call",
                        "max_tokens_to_sample": "8192",
                    }
                    created = 0
                    for model_name in {MODEL_PRO, MODEL_LITE}:
                        try:
                            mps.create_model_credential(
                                tenant_id=tenant.id, provider=provider,
                                model_type="llm", model=model_name,
                                credentials={**base_credentials},
                                credential_name=model_name,
                            )
                            ok(f"  Model: {model_name}")
                            created += 1
                        except Exception as e:
                            if "already" in str(e).lower():
                                ok(f"  Model already exists: {model_name}")
                            else:
                                warn(f"  {model_name}: {e}")
                    if created > 0:
                        db.session.commit()
                        ok(f"Provider configured ({created} models): {OPENAI_BASE}")
            except Exception as e:
                warn(f"Provider setup: {e} — configure manually in Settings")
                db.session.rollback()
        else:
            warn("OPENAI_API_KEY not set or placeholder. Set it in .env and restart.")

        # ── Step 3: Import DSL files ──
        log(f"Scanning for DSL files in {DSL_DIR} ...")
        if not DSL_DIR.exists():
            warn(f"DSL directory {DSL_DIR} does not exist. Skipping import.")
            return

        dsl_files = sorted(DSL_DIR.glob("*.yml")) + sorted(DSL_DIR.glob("*.yaml"))
        if not dsl_files:
            warn(f"No DSL files found in {DSL_DIR}")
            return

        from services.app_dsl_service import AppDslService

        dsl_service = AppDslService(db.session)
        imported = 0
        skipped = 0
        failed = 0

        # Get list of already-imported app names in this workspace
        from models.model import App
        existing_apps = {
            a.name: a.id
            for a in db.session.query(App).filter(App.tenant_id == tenant.id).all()
        }

        for dsl_file in dsl_files:
            dsl_name = dsl_file.name
            log(f"Processing DSL: {dsl_name} ...")

            try:
                content = dsl_file.read_text()

                # Parse YAML to extract the app name
                dsl_data = yaml.safe_load(content)
                dsl_app_name = dsl_data.get("app", {}).get("name", dsl_name)

                # Skip if already imported (idempotent)
                if dsl_app_name in existing_apps:
                    ok(f"Already imported: {dsl_app_name} (id={existing_apps[dsl_app_name]}), skipping")
                    skipped += 1
                    continue

                # Replace model names
                log(f"  Importing '{dsl_app_name}' | Model mapping: pro/pro-2026-03-01 → {MODEL_PRO}, lite → {MODEL_LITE}")
                content = re.sub(
                    r"name: pro-2026-03-01[ \t]*$",
                    f"name: {MODEL_PRO}",
                    content,
                    flags=re.MULTILINE,
                )
                content = re.sub(
                    r"name: pro[ \t]*$",
                    f"name: {MODEL_PRO}",
                    content,
                    flags=re.MULTILINE,
                )
                content = re.sub(
                    r"name: lite[ \t]*$",
                    f"name: {MODEL_LITE}",
                    content,
                    flags=re.MULTILINE,
                )

                result = dsl_service.import_app(
                    account=account,
                    import_mode="yaml-content",
                    yaml_content=content,
                )

                if result.status in ("completed", "success"):
                    ok(f"Imported: {dsl_name} (app_id={result.app_id})")
                    imported += 1
                elif result.status == "pending":
                    ok(f"Imported (pending confirm): {dsl_name}")
                    imported += 1
                else:
                    warn(f"Status={result.status} for {dsl_name}: {result.error}")
                    failed += 1

            except Exception as e:
                error_msg = str(e)
                if any(w in error_msg.lower() for w in ("already exist", "duplicate", "unique")):
                    warn(f"DSL '{dsl_name}' may already exist, skipping")
                    skipped += 1
                else:
                    warn(f"Failed to import {dsl_name}: {error_msg}")
                    failed += 1

        log(f"DSL import complete: {imported} imported, {skipped} skipped, {failed} failed")

    finally:
        ctx.pop()

    # ── Summary ──
    print()
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print("  Yusta Dify Dev Environment — Ready")
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print(f"  Web UI:    http://localhost:{os.getenv('WEB_PORT', '3000')}")
    print("  API:       http://localhost:5001")
    print(f"  Admin:     {ADMIN_EMAIL}")
    print(f"  DSLs dir:  {DSL_DIR}")
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print()
    ok("Bootstrap complete!")


if __name__ == "__main__":
    bootstrap()
