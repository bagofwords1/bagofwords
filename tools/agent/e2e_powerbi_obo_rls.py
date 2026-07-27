"""Drive the per-user Power BI OBO sign-in for an arbitrary member, the way the
OAuth callback does, and report every layer the customer's topology touches.

The hosted Microsoft login page cannot render in the sandbox's headless Chromium
(the egress proxy rejects its TLS handshake), so the interactive hop is replaced
by a ROPC grant that yields the SAME delegated token. Everything after that is
the real product code path:

    upsert UserConnectionCredentials
      -> ConnectionService.test_user_connection   (the connect gate)
      -> DataSourceService.get_user_data_source_schema  (the overlay sync)

Usage (from backend/, with the app venv):
    BOW_DATABASE_URL=sqlite:///db/app.db \
    BOW_RLS_USER_EMAIL=demo2@... BOW_RLS_USER_PASSWORD=... \
    BOW_RLS_LOCAL_EMAIL=demo2@local  uv run python ../tools/agent/e2e_powerbi_obo_rls.py
"""
import asyncio
import os
import sys
import time
from datetime import datetime, timezone

import httpx

_BACKEND = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "backend")
sys.path.insert(0, _BACKEND)

from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession  # noqa: E402

DB_PATH = os.path.join(_BACKEND, "db", "app.db")
LOCAL_EMAIL = os.environ.get("BOW_RLS_LOCAL_EMAIL", "admin@example.com")
UPN = os.environ["BOW_RLS_USER_EMAIL"]
PWD = os.environ["BOW_RLS_USER_PASSWORD"]


async def main():
    import main as _appmain  # noqa: F401  (registers every SQLAlchemy mapper)
    from app.models.user import User
    from app.models.connection import Connection
    from app.models.user_connection_credentials import UserConnectionCredentials
    from app.services.connection_oauth_service import _OBO_SCOPES, parse_expires_at
    from sqlalchemy.orm import selectinload

    engine = create_async_engine(f"sqlite+aiosqlite:///{DB_PATH}")
    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with Session() as db:
        user = (await db.execute(select(User).where(User.email == LOCAL_EMAIL))).scalar_one()
        conn = (await db.execute(
            select(Connection)
            .options(selectinload(Connection.organization), selectinload(Connection.data_sources))
            .where(Connection.type == "powerbi")
        )).scalars().first()
        print(f"local user={user.email}  connection={conn.id}  policy={conn.auth_policy}")

        creds = conn.decrypt_credentials() or {}
        client_id = creds.get("oauth_client_id") or creds.get("client_id")
        client_secret = creds.get("oauth_client_secret") or creds.get("client_secret")
        tenant = creds.get("tenant_id")

        async with httpx.AsyncClient() as http:
            r = await http.post(
                f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
                data={"grant_type": "password", "client_id": client_id,
                      "client_secret": client_secret, "username": UPN, "password": PWD,
                      "scope": _OBO_SCOPES["powerbi"]},
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
        print(f"delegated token acquired for {UPN}")

        existing = (await db.execute(select(UserConnectionCredentials).where(
            UserConnectionCredentials.connection_id == str(conn.id),
            UserConnectionCredentials.user_id == str(user.id),
            UserConnectionCredentials.is_active == True,  # noqa: E712
        ))).scalars().first()
        row = existing or UserConnectionCredentials(
            connection_id=str(conn.id), user_id=str(user.id),
            organization_id=str(conn.organization_id), auth_mode="oauth",
            is_active=True, is_primary=True,
        )
        row.auth_mode = "oauth"
        row.encrypt_credentials(tokens)
        row.expires_at = parse_expires_at(tokens.get("expires_at"))
        db.add(row)
        await db.commit()
        print("UserConnectionCredentials upserted")

        from app.services.connection_service import ConnectionService
        test = await ConnectionService().test_user_connection(
            db=db, connection_id=str(conn.id), organization=conn.organization, current_user=user)
        print(f"CONNECT GATE  success={test.get('success')}  msg={str(test.get('message'))[:220]}")

        from app.services.data_source_service import DataSourceService
        svc = DataSourceService()
        for ds in (conn.data_sources or []):
            schemas = await svc.get_user_data_source_schema(db=db, data_source=ds, user=user)
            names = sorted(getattr(t, "name", "?") for t in (schemas or []))
            print(f"OVERLAY  data_source={ds.name}  tables={len(names)}")
            for n in names:
                print(f"           - {n}")

    await engine.dispose()


asyncio.run(main())
