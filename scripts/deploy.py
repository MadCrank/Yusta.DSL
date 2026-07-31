#!/usr/bin/env python3
"""
Yusta DSL Deploy — manual DSL import without restart
=====================================================
Runs inside the Dify API container to import/update DSL workflow files.

Usage:
  docker exec yusta-dify-api python /deploy.py              # deploy all DSLs
  docker exec yusta-dify-api python /deploy.py dsls/Yusta.yml  # deploy single file
  docker exec yusta-dify-api python /deploy.py --json       # JSON output
"""
import os
import sys
import re
import time
import json

sys.path.insert(0, "/app/api")

import yaml
from pathlib import Path

# ── Configuration ──────────────────────────
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "admin@yusta.local")
MODEL_PRO = os.getenv("DIFY_MODEL_PRO", "")
MODEL_LITE = os.getenv("DIFY_MODEL_LITE", "")
DSL_DIR = Path(os.getenv("DSL_DIR", "/dsls"))

# ── Logging ────────────────────────────────
def log(msg: str) -> None:
    print(f"[yusta-deploy] {time.strftime('%H:%M:%S')} | {msg}", flush=True)

def ok(msg: str) -> None:
    print(f"[yusta-deploy] {time.strftime('%H:%M:%S')} | ✅ {msg}", flush=True)

def warn(msg: str) -> None:
    print(f"[yusta-deploy] {time.strftime('%H:%M:%S')} | ⚠  {msg}", file=sys.stderr, flush=True)

def fail(msg: str, code: int = 1) -> None:
    print(f"[yusta-deploy] {time.strftime('%H:%M:%S')} | ❌ {msg}", file=sys.stderr, flush=True)
    sys.exit(code)

# ── Helpers ─────────────────────────────────
def wait_for_db(max_retries: int = 30) -> None:
    """Wait for the database to be ready (accounts table exists)."""
    from extensions.ext_database import db

    for i in range(max_retries):
        try:
            db.session.execute(db.text("SELECT 1 FROM accounts LIMIT 0"))
            return
        except Exception:
            if i >= max_retries - 1:
                fail("Database not ready after {}s".format(max_retries * 2))
            time.sleep(2)


def replace_model_names(content: str) -> str:
    """Replace placeholder model names with configured values from env."""
    if MODEL_PRO:
        # Order matters: pro-2026-03-01 is more specific, replace first
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
    if MODEL_LITE:
        content = re.sub(
            r"name: lite[ \t]*$",
            f"name: {MODEL_LITE}",
            content,
            flags=re.MULTILINE,
        )
    return content


def find_admin_account():
    """Find the admin account. Fail if it doesn't exist (init.py must run first)."""
    from models.account import Account, Tenant, TenantAccountJoin

    account = Account.query.filter(Account.email == ADMIN_EMAIL).first()
    if not account:
        fail(f"Admin account '{ADMIN_EMAIL}' not found. Run init.py first (docker compose up init).")

    join = (
        TenantAccountJoin.query
        .filter(TenantAccountJoin.account_id == account.id)
        .order_by(TenantAccountJoin.id.asc())
        .first()
    )
    if not join:
        fail("No workspace found for admin account.")

    tenant = Tenant.query.filter(Tenant.id == join.tenant_id).first()
    if not tenant:
        fail("Workspace not found.")

    return account, tenant


def find_existing_app(tenant_id: int, app_name: str):
    """Find an existing app by name in the given workspace."""
    from models.model import App
    return App.query.filter(
        App.tenant_id == tenant_id,
        App.name == app_name,
    ).first()


def delete_app(app_id: str) -> None:
    """Delete an existing app by ID."""
    from models.model import App
    from extensions.ext_database import db

    app = App.query.filter(App.id == app_id).first()
    if app:
        db.session.delete(app)
        db.session.commit()


def deploy_one(dsl_path: Path, account, tenant, dsl_service, json_mode: bool) -> dict:
    """
    Deploy a single DSL file. Returns result dict with keys:
      file, app_name, app_id, status (created/updated/skipped/failed), error
    """
    from extensions.ext_database import db

    result = {
        "file": str(dsl_path.name),
        "app_name": None,
        "app_id": None,
        "status": "failed",
        "error": None,
    }

    try:
        content = dsl_path.read_text()
        dsl_data = yaml.safe_load(content)
        app_name = dsl_data.get("app", {}).get("name", dsl_path.name)
        result["app_name"] = app_name

        # Replace model names
        content = replace_model_names(content)

        if not json_mode:
            log(f"Processing: {dsl_path.name} → '{app_name}'")
            if MODEL_PRO or MODEL_LITE:
                log(f"  Model mapping: pro → {MODEL_PRO or '(unchanged)'}, lite → {MODEL_LITE or '(unchanged)'}")

        # Check if app already exists → delete to allow reimport
        existing = find_existing_app(tenant.id, app_name)
        if existing:
            old_id = existing.id
            delete_app(old_id)
            if not json_mode:
                log(f"  Deleting existing app (id={old_id}) before reimport...")

        # Import
        import_result = dsl_service.import_app(
            account=account,
            import_mode="yaml-content",
            yaml_content=content,
        )

        result["app_id"] = import_result.app_id

        if import_result.status in ("completed", "success"):
            tag = "updated" if existing else "created"
            result["status"] = tag
            if not json_mode:
                ok(f"  {tag}: {app_name} (app_id={import_result.app_id})")
        elif import_result.status == "pending":
            tag = "updated" if existing else "created"
            result["status"] = tag
            if not json_mode:
                ok(f"  {tag} (pending confirm): {app_name} (app_id={import_result.app_id})")
        else:
            result["status"] = "failed"
            result["error"] = f"import status={import_result.status}: {getattr(import_result, 'error', 'unknown')}"
            warn(f"  Failed: {result['error']}")
            db.session.rollback()

    except Exception as e:
        error_msg = str(e)
        result["error"] = error_msg
        if any(w in error_msg.lower() for w in ("already exist", "duplicate", "unique")):
            result["status"] = "skipped"
            if not json_mode:
                warn(f"  Skipped (already exists): {app_name}")
        else:
            result["status"] = "failed"
            if not json_mode:
                warn(f"  Failed: {app_name} — {error_msg}")
        try:
            db.session.rollback()
        except Exception:
            pass

    return result


def main() -> None:
    json_mode = "--json" in sys.argv
    args = [a for a in sys.argv[1:] if a != "--json"]

    from app import create_app
    from extensions.ext_database import db
    from services.app_dsl_service import AppDslService

    app = create_app()
    ctx = app.app_context()
    ctx.push()

    try:
        # Wait for DB
        wait_for_db()

        # Find admin account (must exist from init.py)
        account, tenant = find_admin_account()
        if not json_mode:
            log(f"Using workspace: {tenant.name} (id={tenant.id})")

        dsl_service = AppDslService(db.session)

        # Determine which files to deploy
        if args:
            # Specific file(s) passed
            dsl_files = [Path(p) for p in args]
            for p in dsl_files:
                if not p.exists():
                    # Try relative to DSL_DIR
                    alt = DSL_DIR / p.name
                    if alt.exists():
                        dsl_files[dsl_files.index(p)] = alt
                    else:
                        fail(f"File not found: {p} (also tried {alt})")
        else:
            if not DSL_DIR.exists():
                fail(f"DSL directory not found: {DSL_DIR}")
            dsl_files = sorted(DSL_DIR.glob("*.yml")) + sorted(DSL_DIR.glob("*.yaml"))
            if not dsl_files:
                warn(f"No DSL files found in {DSL_DIR}")
                sys.exit(0)

        # Deploy each file
        results = []
        for dsl_file in dsl_files:
            r = deploy_one(dsl_file, account, tenant, dsl_service, json_mode)
            results.append(r)

        # Summary
        counts = {"created": 0, "updated": 0, "skipped": 0, "failed": 0}
        for r in results:
            counts[r["status"]] = counts.get(r["status"], 0) + 1

        if json_mode:
            print(json.dumps({
                "results": results,
                "summary": counts,
            }, indent=2))
        else:
            log(f"Done: {counts['created']} created, {counts['updated']} updated, "
                f"{counts['skipped']} skipped, {counts['failed']} failed")

        # Exit code
        if counts["failed"] > 0:
            sys.exit(1)

    finally:
        ctx.pop()


if __name__ == "__main__":
    main()
