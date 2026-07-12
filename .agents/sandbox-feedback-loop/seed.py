#!/usr/bin/env python
"""Seed the sandbox: Anthropic LLM provider + three file connections
(network_dir content-indexed, network_dir live logs, S3 content-indexed),
one agent linking them, indexed, plus a report to drive completions.

Reads creds from env (see secrets.env). No secrets are written to disk here.
Run from backend/ with the venv + secrets sourced.
"""
from __future__ import annotations
import asyncio, json, os, sys
import httpx

BASE = "http://localhost:8000"
NETDIR = "/home/user/bagofwords/.agents/sandbox-feedback-loop/netdir"
S3_BUCKET = os.environ["S3_TEST_BUCKET"]
ANTHROPIC_KEY = os.environ["ANTHROPIC_API_KEY"]
AWS_KEY = os.environ["AWS_ACCESS_KEY_ID"]
AWS_SECRET = os.environ["AWS_SECRET_ACCESS_KEY"]
REGION = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
EMAIL, PW = "admin@example.com", "Password123!"


def api():
    c = httpx.Client(base_url=BASE, timeout=120)
    # register (idempotent) then login
    c.post("/api/auth/register", json={"email": EMAIL, "password": PW, "name": "Admin"})
    tok = c.post("/api/auth/jwt/login", data={"username": EMAIL, "password": PW}).json()["access_token"]
    org = c.get("/api/organizations", headers={"Authorization": f"Bearer {tok}"}).json()[0]["id"]
    c.headers.update({"Authorization": f"Bearer {tok}", "X-Organization-Id": org})
    return c, org


def setup_llm(c):
    # Skip if an anthropic provider already exists
    existing = c.get("/api/llm/providers").json()
    for p in existing:
        if p.get("provider_type") == "anthropic":
            print("  anthropic provider already present")
            return
    body = {
        "name": "Anthropic",
        "provider_type": "anthropic",
        "credentials": {"api_key": ANTHROPIC_KEY},
        "models": [
            {"name": "Claude Haiku 4.5", "model_id": "claude-haiku-4-5-20251001",
             "is_default": True, "is_small_default": True,
             "context_window_tokens": 200000, "max_output_tokens": 8192},
        ],
    }
    r = c.post("/api/llm/providers", json=body)
    print("  create provider:", r.status_code, r.text[:200])
    r.raise_for_status()


async def seed_connections(org_id: str):
    import importlib, pkgutil
    import app.models as _m
    for mod in pkgutil.iter_modules(_m.__path__):
        if mod.name != "application":
            importlib.import_module(f"app.models.{mod.name}")

    from sqlalchemy import select
    from app.dependencies import async_session_maker
    from app.models.connection import Connection
    from app.models.data_source import DataSource
    from app.models.report import Report
    from app.models.user import User
    from app.models.organization import Organization
    from app.models.domain_connection import domain_connection
    from app.models.report_data_source_association import report_data_source_association
    from app.services.data_source_service import DataSourceService

    CONNS = [
        dict(name="Finance Share (content)", type="network_dir",
             config={"root_path": NETDIR,
                     "include_globs": "reports/**/*.csv, docs/**, files/**/*.ppt",
                     "recursive": True, "writable": False, "max_file_mb": 50,
                     "index_mode": "content"},
             creds={}),
        dict(name="Ops Logs (live)", type="network_dir",
             config={"root_path": NETDIR,
                     "include_globs": "logs/*.log, data/*.ndjson",
                     "recursive": True, "writable": False, "max_file_mb": 200,
                     "index_mode": "none"},
             creds={}),
        dict(name="S3 bowathena docs (content)", type="s3",
             config={"bucket": S3_BUCKET, "prefix": "",
                     "include_globs": "docs/**",
                     "region": REGION, "recursive": True, "max_file_mb": 50,
                     "index_mode": "content"},
             creds={"access_key": AWS_KEY, "secret_key": AWS_SECRET}),
    ]

    async with async_session_maker() as db:
        user = (await db.execute(select(User).where(User.email == EMAIL))).scalar_one()
        org = (await db.execute(select(Organization).where(Organization.id == org_id))).scalar_one()

        ds = DataSource(name="Sandbox Files Agent", organization_id=org_id,
                        is_active=True, owner_user_id=user.id)
        db.add(ds); await db.flush()

        conn_ids = []
        for spec in CONNS:
            conn = Connection(organization_id=org_id, name=spec["name"], type=spec["type"],
                              config=spec["config"], auth_policy="system_only")
            conn.encrypt_credentials(spec["creds"])
            db.add(conn); await db.flush()
            await db.execute(domain_connection.insert().values(data_source_id=ds.id, connection_id=conn.id))
            conn_ids.append((spec["name"], str(conn.id)))

        report = Report(title="Sandbox file-tools verification", slug="sandbox-file-tools",
                        organization_id=org_id, user_id=user.id)
        db.add(report); await db.flush()
        await db.execute(report_data_source_association.insert().values(report_id=report.id, data_source_id=ds.id))
        await db.commit()

        # Index (real refresh pipeline) — network_dir 'none' returns [] fast.
        await DataSourceService().refresh_data_source_schema(db, str(ds.id), org, user)
        return str(ds.id), str(report.id), conn_ids


def main():
    c, org = api()
    print("org:", org)
    setup_llm(c)
    ds_id, report_id, conn_ids = asyncio.run(seed_connections(org))
    out = {
        "org_id": org, "ds_id": ds_id, "report_id": report_id,
        "report_url": f"http://localhost:3000/reports/{report_id}",
        "connections": conn_ids,
    }
    print(json.dumps(out, indent=2))
    with open("/home/user/bagofwords/.agents/sandbox-feedback-loop/runtime/seed.json", "w") as f:
        json.dump(out, f, indent=2)


if __name__ == "__main__":
    os.makedirs("/home/user/bagofwords/.agents/sandbox-feedback-loop/runtime", exist_ok=True)
    main()
