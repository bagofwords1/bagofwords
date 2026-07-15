"""Complete the per-user OBO sign-in server-side, replicating what
GET /connections/oauth/callback does after Microsoft redirects back.

Cloud-sandbox companion to e2e_obo_zero_tables_repro.mjs: the sandbox egress
proxy rejects Chromium's TLS handshake, so the hosted Microsoft login page
can't render in the headless browser. This script obtains the SAME delegated
token via the ROPC grant (the Loop B pattern from
tests/integrations/test_oauth_delegated.py) and then follows the exact
callback code path: upsert UserConnectionCredentials -> verify with
test_user_connection -> overlay sync via get_user_data_source_schema.

Run with the backend venv and the same env the server uses (BOW_CONFIG_PATH,
BOW_ENCRYPTION_KEY, TESTING/TEST_DATABASE_URL), plus:
    BOW_OAUTH_TEST_DEMO1_EMAIL / BOW_OAUTH_TEST_DEMO1_PASSWORD

    cd backend && uv run python ../tools/agent/e2e_obo_signin_ropc.py
"""
import asyncio
import os
import sys
import time
from datetime import datetime, timezone

import httpx

_BACKEND = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "backend")
sys.path.insert(0, _BACKEND)

from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

ADMIN_EMAIL = os.environ.get("BOW_E2E_ADMIN_EMAIL", "admin@example.com")
DB_PATH = os.path.join(_BACKEND, "db", "agent.db")


async def main():
    # Import the app exactly like the server does so every live mapper is
    # registered (importing model modules blindly pulls in dead ones).
    import main as _appmain  # noqa: F401
    from app.models.user import User
    from app.models.connection import Connection
    from app.models.user_connection_credentials import UserConnectionCredentials
    from app.services.connection_oauth_service import _OBO_SCOPES, parse_expires_at
    from sqlalchemy.orm import selectinload

    engine = create_async_engine(f"sqlite+aiosqlite:///{DB_PATH}")
    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with Session() as db:
        user = (await db.execute(select(User).where(User.email == ADMIN_EMAIL))).scalar_one()
        conn = (await db.execute(
            select(Connection).options(selectinload(Connection.organization), selectinload(Connection.data_sources))
            .where(Connection.type == "powerbi")
        )).scalars().first()
        print(f"user={user.id} connection={conn.id} auth_policy={conn.auth_policy}")

        creds = conn.decrypt_credentials() or {}
        client_id = creds.get("oauth_client_id") or creds.get("client_id")
        client_secret = creds.get("oauth_client_secret") or creds.get("client_secret")
        tenant = creds.get("tenant_id")

        # ROPC — same delegated audience/scopes the authorization-code flow yields
        scope = _OBO_SCOPES["powerbi"]
        async with httpx.AsyncClient() as http:
            r = await http.post(
                f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
                data={
                    "grant_type": "password",
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "username": os.environ["BOW_OAUTH_TEST_DEMO1_EMAIL"],
                    "password": os.environ["BOW_OAUTH_TEST_DEMO1_PASSWORD"],
                    "scope": scope,
                },
                timeout=30,
            )
        r.raise_for_status()
        td = r.json()
        tokens = {
            "access_token": td["access_token"],
            "refresh_token": td.get("refresh_token"),
            "expires_at": datetime.fromtimestamp(time.time() + int(td.get("expires_in", 3600)), tz=timezone.utc).isoformat(),
            "token_type": td.get("token_type", "Bearer"),
        }
        print(f"delegated token acquired for {os.environ['BOW_OAUTH_TEST_DEMO1_EMAIL']} (scope: powerbi)")

        # Upsert UserConnectionCredentials — mirrors routes/connection_oauth.py callback
        existing = (await db.execute(select(UserConnectionCredentials).where(
            UserConnectionCredentials.connection_id == str(conn.id),
            UserConnectionCredentials.user_id == str(user.id),
            UserConnectionCredentials.is_active == True,  # noqa: E712
        ))).scalars().first()
        if existing:
            row = existing
            row.auth_mode = "oauth"
            row.encrypt_credentials(tokens)
            row.expires_at = parse_expires_at(tokens.get("expires_at"))
            db.add(row)
        else:
            row = UserConnectionCredentials(
                connection_id=str(conn.id),
                user_id=str(user.id),
                organization_id=str(conn.organization_id),
                auth_mode="oauth",
                is_active=True,
                is_primary=True,
                expires_at=parse_expires_at(tokens.get("expires_at")),
            )
            row.encrypt_credentials(tokens)
            db.add(row)
        await db.commit()
        print("UserConnectionCredentials upserted")

        # Verify — same as the callback
        from app.services.connection_service import ConnectionService
        test = await ConnectionService().test_user_connection(
            db=db, connection_id=str(conn.id), organization=conn.organization, current_user=user,
        )
        print(f"test_user_connection: success={test.get('success')} message={test.get('message')}")

        # Overlay sync — same as the callback
        from app.services.data_source_service import DataSourceService
        ds_service = DataSourceService()
        for ds in (conn.data_sources or []):
            schemas = await ds_service.get_user_data_source_schema(db=db, data_source=ds, user=user)
            print(f"overlay sync for data source {ds.id}: {len(schemas or [])} tables")

    await engine.dispose()


asyncio.run(main())
