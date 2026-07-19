"""Live end-to-end proof of the reconnect / OBO-hybrid fix against a REAL Entra
tenant. Secrets come from env (never hardcoded). Runs against the same agent.db
the backend uses, exercising the real service code paths.

Flow:
  0. Create a powerbi user_required+oauth connection (real Entra creds).
  A. OBO simulation: store an expired cred row WITH NO refresh token.
     -> build_token_identity_status.needs_reconnect == True
     -> resolve_credentials raises 403 code=reconnect_required
  B. Interactive reconnect (ROPC stands in for the browser OAuth): acquire a
     REAL delegated token (which DOES carry a refresh token) and store it.
     -> needs_reconnect == False; resolve_credentials returns the real token
  C. Durability: expire the access token but keep the refresh token.
     -> auto_provision (next login) SKIPS it as refreshable_credentials_exist
        and preserves the refresh token (no OBO clobber)
     -> resolve_credentials performs a REAL Entra refresh and returns a NEW
        access token; needs_reconnect stays False
"""
import asyncio
import os
import sys
import time
import json
from datetime import datetime, timedelta, timezone

import httpx

_BACKEND = "/home/user/bagofwords/backend"
sys.path.insert(0, _BACKEND)

from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import selectinload

DB_PATH = os.path.join(_BACKEND, "db", "agent.db")
ADMIN_EMAIL = "admin@example.com"

TENANT = os.environ["BOW_ENTRA_TENANT_ID"]
CLIENT_ID = os.environ["BOW_ENTRA_CLIENT_ID"]
CLIENT_SECRET = os.environ["BOW_ENTRA_CLIENT_SECRET"]
DEMO_EMAIL = os.environ["BOW_OAUTH_TEST_DEMO1_EMAIL"]
DEMO_PW = os.environ["BOW_OAUTH_TEST_DEMO1_PASSWORD"]

PASS = "\033[92mPASS\033[0m"
FAIL = "\033[91mFAIL\033[0m"
results = []
def check(name, cond, extra=""):
    results.append(cond)
    print(f"  [{PASS if cond else FAIL}] {name}" + (f"  ({extra})" if extra else ""))


async def ropc_token(scope):
    async with httpx.AsyncClient() as http:
        r = await http.post(
            f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/token",
            data={"grant_type": "password", "client_id": CLIENT_ID,
                  "client_secret": CLIENT_SECRET, "username": DEMO_EMAIL,
                  "password": DEMO_PW, "scope": scope},
            timeout=30,
        )
    r.raise_for_status()
    return r.json()


async def main():
    import main as _appmain  # noqa: F401 — register all mappers like the server
    from app.models.user import User
    from app.models.organization import Organization
    from app.models.connection import Connection
    from app.models.user_connection_credentials import UserConnectionCredentials
    from app.services.connection_oauth_service import (
        _OBO_SCOPES, parse_expires_at, auto_provision_connection_credentials,
    )
    from app.services.connection_identity import build_token_identity_status
    from app.services.connection_service import ConnectionService
    from fastapi import HTTPException

    engine = create_async_engine(f"sqlite+aiosqlite:///{DB_PATH}")
    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    scope = _OBO_SCOPES["powerbi"]

    async with Session() as db:
        user = (await db.execute(select(User).where(User.email == ADMIN_EMAIL))).scalar_one()
        org = (await db.execute(select(Organization))).scalars().first()

        # 0. Create the powerbi user_required + oauth connection with real creds.
        conn = Connection(
            name="Live PBI OBO",
            type="powerbi",
            organization_id=str(org.id),
            config=json.dumps({}),
            auth_policy="user_required",
            allowed_user_auth_modes=["oauth"],
        )
        conn.encrypt_credentials({
            "tenant_id": TENANT,
            "oauth_client_id": CLIENT_ID, "oauth_client_secret": CLIENT_SECRET,
            "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
        })
        db.add(conn)
        await db.commit()
        conn = (await db.execute(
            select(Connection).options(selectinload(Connection.organization),
                                       selectinload(Connection.data_sources))
            .where(Connection.id == conn.id))).scalars().first()
        print(f"\nconnection={conn.id} type={conn.type} auth_policy={conn.auth_policy}")

        async def status():
            return await build_token_identity_status(db, conn, user)

        async def put_row(creds, expires_at):
            existing = (await db.execute(select(UserConnectionCredentials).where(
                UserConnectionCredentials.connection_id == str(conn.id),
                UserConnectionCredentials.user_id == str(user.id),
                UserConnectionCredentials.is_active == True))).scalars().first()
            row = existing or UserConnectionCredentials(
                connection_id=str(conn.id), user_id=str(user.id),
                organization_id=str(org.id), auth_mode="oauth",
                is_active=True, is_primary=True)
            row.auth_mode = "oauth"
            row.encrypt_credentials(creds)
            row.expires_at = parse_expires_at(expires_at) if isinstance(expires_at, str) else expires_at
            db.add(row); await db.commit(); await db.refresh(row)
            return row

        # --- A. OBO simulation: expired, NO refresh token ---
        print("\nA. OBO row (expired, no refresh token):")
        past = (datetime.now(timezone.utc) - timedelta(minutes=1))
        await put_row({"access_token": "obo-stale", "token_type": "Bearer",
                       "expires_at": past.isoformat()}, past.replace(tzinfo=None))
        st = await status()
        check("has_user_credentials is True", st.has_user_credentials is True)
        check("needs_reconnect is True", st.needs_reconnect is True)
        try:
            await ConnectionService().resolve_credentials(db, conn, user)
            check("resolve_credentials raises 403", False, "no exception")
        except HTTPException as e:
            code = e.detail.get("code") if isinstance(e.detail, dict) else None
            check("resolve_credentials raises reconnect_required 403",
                  e.status_code == 403 and code == "reconnect_required", f"code={code}")

        # --- B. Interactive reconnect via REAL Entra (ROPC) ---
        print("\nB. Interactive reconnect (real delegated token):")
        td = await ropc_token(scope)
        has_rt = bool(td.get("refresh_token"))
        check("real delegated token carries a refresh token", has_rt)
        exp_iso = datetime.fromtimestamp(time.time() + int(td.get("expires_in", 3600)),
                                         tz=timezone.utc).isoformat()
        await put_row({"access_token": td["access_token"],
                       "refresh_token": td.get("refresh_token"),
                       "token_type": "Bearer", "expires_at": exp_iso},
                      parse_expires_at(exp_iso))
        st = await status()
        check("needs_reconnect is False after reconnect", st.needs_reconnect is False)
        creds = await ConnectionService().resolve_credentials(db, conn, user)
        check("resolve_credentials returns the real access token",
              creds.get("access_token") == td["access_token"])

        # --- C. Durability: expired access token but refresh token retained ---
        print("\nC. Durability (expired access token, refresh token retained):")
        real_rt = td.get("refresh_token")
        await put_row({"access_token": td["access_token"], "refresh_token": real_rt,
                       "token_type": "Bearer",
                       "expires_at": past.isoformat()}, past.replace(tzinfo=None))
        # next Entra login runs auto_provision — must NOT re-OBO over a refreshable row
        summary = await auto_provision_connection_credentials(db, user, td["access_token"])
        skipped = {s["connection_id"]: s.get("reason") for s in summary["skipped"]}
        check("auto_provision skips refreshable row (no OBO clobber)",
              skipped.get(str(conn.id)) == "refreshable_credentials_exist",
              f"reason={skipped.get(str(conn.id))}")
        row = (await db.execute(select(UserConnectionCredentials).where(
            UserConnectionCredentials.connection_id == str(conn.id),
            UserConnectionCredentials.user_id == str(user.id),
            UserConnectionCredentials.is_active == True))).scalars().first()
        check("refresh token preserved after login", row.decrypt_credentials().get("refresh_token") == real_rt)
        # resolve_credentials should perform a REAL Entra refresh and return a new token
        creds2 = await ConnectionService().resolve_credentials(db, conn, user)
        new_at = creds2.get("access_token")
        check("resolve_credentials performs a real Entra refresh (new access token issued)",
              bool(new_at) and new_at != td["access_token"])
        st = await status()
        check("needs_reconnect stays False (durable)", st.needs_reconnect is False)

        # cleanup the live connection so re-runs start clean
        await db.delete(row)
        await db.delete(conn)
        await db.commit()

    await engine.dispose()
    print(f"\n==== {sum(results)}/{len(results)} checks passed ====")
    sys.exit(0 if all(results) else 1)


asyncio.run(main())
