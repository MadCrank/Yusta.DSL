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
    from services.account_service import AccountService

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
            account = AccountService.create_account_and_tenant(
                email=ADMIN_EMAIL,
                name=ADMIN_NAME,
                interface_language=LANGUAGE,
                password=ADMIN_PASSWORD,
            )
            db.session.commit()
            ok(f"Created admin: {ADMIN_EMAIL}")
        else:
            ok(f"Admin account exists: {ADMIN_EMAIL}")

        # Get the current tenant via join table (most reliable method)
        join = (
            db.session.query(TenantAccountJoin)
            .filter(
                TenantAccountJoin.account_id == account.id,
                TenantAccountJoin.current == True,
            )
            .first()
        )
        if not join:
            fail("No workspace found for admin account")
        tenant = db.session.query(Tenant).filter(Tenant.id == join.tenant_id).first()
        if not tenant:
            fail("Workspace not found in database")

        # Set the current tenant on the account (required for DSL import)
        account.current_tenant = tenant
        db.session.commit()

        log(f"Using workspace: {tenant.name} (id={tenant.id})")

        # ── Step 2: Model provider via env vars ──
        # Dify auto-configures the openai_api_compatible provider via
        # HOSTED_OPENAI_API_KEY and HOSTED_OPENAI_API_BASE env vars at startup.
        # No additional API call needed.
        if OPENAI_KEY and OPENAI_KEY != "sk-your-key-here":
            ok(f"Model provider will use: {OPENAI_BASE} (via HOSTED_OPENAI_API_KEY)")
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
        failed = 0

        for dsl_file in dsl_files:
            dsl_name = dsl_file.name
            log(f"Importing DSL: {dsl_name} ...")

            try:
                content = dsl_file.read_text()

                # Replace model names
                log(f"  Model mapping: pro/pro-2026-03-01 → {MODEL_PRO}, lite → {MODEL_LITE}")
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
                else:
                    warn(f"Failed to import {dsl_name}: {error_msg}")
                    failed += 1

        log(f"DSL import complete: {imported} imported, {failed} failed")

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
