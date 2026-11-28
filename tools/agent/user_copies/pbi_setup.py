#!/usr/bin/env python3
"""Set up the Power BI per-user-copies loop on a running stack.

Creates a `user_required` Power BI agent ("Sign in with Microsoft"), signs
BOW members in to it as real tenant users, and creates custom tables (DAX)
over a row-level-security model. Prints the ids as JSON.

The hosted Microsoft login page cannot render in the sandbox's headless
Chromium, so the interactive hop is replaced by a password grant that yields
the SAME delegated token (as tools/agent/e2e_powerbi_obo_rls.py does).
Everything after that is the product's own code path: the stored per-user
credential row, the connect gate, the per-user table discovery.

    cd backend && uv run python ../tools/agent/user_copies/pbi_setup.py

Env (values never printed):
    PBI_TENANT, PBI_CLIENT, PBI_SECRET   the app registration the connection uses
    UC_PBI_USERS    "bow_email=upn,..."  which tenant user each BOW member signs in as
    UC_PBI_PASSWORD the tenant users' password
    UC_PBI_WORKSPACES  comma-separated workspace names to index (optional)
    UC_PBI_TABLES   JSON list of {name, dax, datasetId, workspaceId, datasetName, description}
    UC_DATA_SOURCE_ID  reuse this agent instead of creating one (re-runs)
    BOW_BASE_URL, BOW_ADMIN_EMAIL, BOW_ADMIN_PASSWORD

Needs an enterprise license (per-user sign-in on a warehouse connection).
"""
import asyncio
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone

import httpx

BASE = os.environ.get("BOW_BASE_URL", "http://localhost:8000")
ADMIN = os.environ.get("BOW_ADMIN_EMAIL", "admin@example.com")
PASSWORD = os.environ.get("BOW_ADMIN_PASSWORD", "Password123!")
TENANT, CLIENT, SECRET = os.environ["PBI_TENANT"], os.environ["PBI_CLIENT"], os.environ["PBI_SECRET"]
USERS = dict(pair.split("=", 1) for pair in os.environ["UC_PBI_USERS"].split(","))
USER_PASSWORD = os.environ["UC_PBI_PASSWORD"]
TABLES = json.loads(os.environ["UC_PBI_TABLES"])
SCOPE = "https://analysis.windows.net/powerbi/api/.default offline_access"

c = httpx.Client(base_url=BASE, timeout=600)


def ok(r: httpx.Response, what: str) -> httpx.Response:
    if r.status_code >= 300:
        sys.exit(f"{what} failed: {r.status_code} {r.text[:600]}")
    return r


def login(email: str) -> str:
    return ok(c.post("/api/auth/jwt/login", data={"username": email, "password": PASSWORD}),
              f"login {email}").json()["access_token"]


def delegated_tokens(upn: str) -> dict:
    r = httpx.post(
        f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/token",
        data={"grant_type": "password", "client_id": CLIENT, "client_secret": SECRET,
              "username": upn, "password": USER_PASSWORD, "scope": SCOPE}, timeout=60)
    if r.status_code != 200:
        sys.exit(f"sign-in for {upn} failed: {r.status_code} {r.json().get('error_description', '')[:300]}")
    td = r.json()
    return {
        "access_token": td["access_token"],
        "refresh_token": td.get("refresh_token"),
        "expires_at": datetime.fromtimestamp(
            time.time() + int(td.get("expires_in", 3600)), tz=timezone.utc).isoformat(),
        "token_type": td.get("token_type", "Bearer"),
    }


async def sign_members_in(connection_id: str) -> dict:
    """What the OAuth callback does, per member: store the delegated token on
    the connection, run the connect gate, discover their tables."""
    import main  # noqa: F401  registers every mapper
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from app.dependencies import async_session_maker
    from app.models.connection import Connection
    from app.models.user import User
    from app.models.user_connection_credentials import UserConnectionCredentials
    from app.services.connection_oauth_service import parse_expires_at
    from app.services.connection_service import ConnectionService
    from app.services.data_source_service import DataSourceService

    out = {}
    async with async_session_maker() as db:
        conn = (await db.execute(
            select(Connection)
            .options(selectinload(Connection.organization), selectinload(Connection.data_sources))
            .where(Connection.id == connection_id))).scalar_one()
        for email, upn in USERS.items():
            user = (await db.execute(select(User).where(User.email == email))).scalar_one()
            tokens = delegated_tokens(upn)
            row = (await db.execute(select(UserConnectionCredentials).where(
                UserConnectionCredentials.connection_id == str(conn.id),
                UserConnectionCredentials.user_id == str(user.id)))).scalars().first()
            row = row or UserConnectionCredentials(
                connection_id=str(conn.id), user_id=str(user.id),
                organization_id=str(conn.organization_id), auth_mode="oauth")
            row.auth_mode, row.is_active, row.is_primary = "oauth", True, True
            row.metadata_json = None
            row.encrypt_credentials(tokens)
            row.expires_at = parse_expires_at(tokens.get("expires_at"))
            db.add(row)
            await db.commit()
            test = await ConnectionService().test_user_connection(
                db=db, connection_id=str(conn.id), organization=conn.organization, current_user=user)
            tables, discovery_error = 0, None
            for ds in (conn.data_sources or []):
                # Discovery crawls the tenant and is rate-limited; the custom
                # tables below pin their semantic model, so a failed crawl
                # does not stop the loop. It is reported, not hidden.
                try:
                    schemas = await DataSourceService().get_user_data_source_schema(
                        db=db, data_source=ds, user=user)
                    tables += len(schemas or [])
                except Exception as e:
                    await db.rollback()
                    discovery_error = f"{type(e).__name__}: {e}"[:200]
            out[email] = {"signed_in_as": upn, "connect_ok": bool(test.get("success")),
                          "tables_visible": tables, "discovery_error": discovery_error}
    return out


def main_() -> None:
    admin_tok = login(ADMIN)
    org = c.get("/api/organizations", headers={"Authorization": f"Bearer {admin_tok}"}).json()[0]["id"]
    ah = {"Authorization": f"Bearer {admin_tok}", "X-Organization-Id": org}
    ok(c.put("/api/organization/settings",
             json={"config": {"enable_custom_queries": {"value": True}}}, headers=ah), "enable custom tables")

    config = {}
    if os.environ.get("UC_PBI_WORKSPACES"):
        config["workspaces"] = os.environ["UC_PBI_WORKSPACES"]
    existing = os.environ.get("UC_DATA_SOURCE_ID")
    ds = ok(c.get(f"/api/data_sources/{existing}", headers=ah), "load data source").json() if existing else \
        ok(c.post("/api/data_sources", json={
        "name": f"Retail BI {uuid.uuid4().hex[:4]}",
        "type": "powerbi",
        "config": config,
        "credentials": {"tenant_id": TENANT, "client_id": CLIENT, "client_secret": SECRET},
        "auth_policy": "user_required",
        "allowed_user_auth_modes": ["oauth"],
        "is_public": True,
    }, headers=ah), "create data source").json()
    ds_id = ds["id"]
    conn = ok(c.get(f"/api/data_sources/{ds_id}/connections", headers=ah), "list connections").json()[0]
    conn_id = conn["id"]

    members = asyncio.run(sign_members_in(conn_id))

    created = []
    for t in TABLES:
        r = c.post(f"/api/connections/{conn_id}/custom-queries", json={
            "name": t["name"],
            "definition_sql": t["dax"],
            "description": t.get("description"),
            "target": {"datasetId": t["datasetId"], "workspaceId": t.get("workspaceId"),
                       "datasetName": t.get("datasetName")},
            "refresh_schedule_mode": "interval",
            "refresh_interval_minutes": 60,
            "activate_for_datasource_id": ds_id,
        }, headers=ah)
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        created.append({
            "name": t["name"], "status": r.status_code,
            "id": body.get("id"), "per_user": body.get("per_user"),
            "columns": [col.get("name") for col in body.get("columns") or []],
            "error": None if r.status_code < 300 else r.text[:400],
        })

    print(json.dumps({
        "organization_id": org, "data_source_id": ds_id, "data_source_name": ds["name"],
        "connection_id": conn_id, "members": members, "custom_tables": created,
    }, indent=2))


main_()
