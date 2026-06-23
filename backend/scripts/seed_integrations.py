"""Seed an Integrations scenario directly against the DB (no live OAuth).

Creates, for the first organization + a target user:
  - a Gmail integration Connection (auth_policy=user_required, oauth)
  - one ConnectionTool per declared Gmail tool
  - a UserConnectionCredentials row (so the user counts as "connected")
  - UserConnectionTool overlays enabling each tool for that user

Idempotent: re-running updates the same rows. Use to exercise the catalog /
mention / runtime paths without standing up Google OAuth.

    cd backend && uv run python scripts/seed_integrations.py [user_email]
"""
import asyncio
import importlib
import pathlib
import sys
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

# Register the ORM mapper graph before running queries (standalone script — the
# app's normal import chain isn't loaded). We import the same model set Alembic's
# env.py uses, which is the known-good, fully-resolvable graph (globbing every
# module pulls in optional models with unresolved relationships).
def _load_models() -> None:
    models_dir = pathlib.Path(__file__).resolve().parent.parent / "app" / "models"
    # `application.py` carries a dangling relationship ('DataSourceApplicationAssociation'
    # is never defined); the running app tolerates it by never configuring that
    # mapper. Importing it here would break global mapper configuration, so skip it.
    skip = {"__init__", "application"}
    for p in sorted(models_dir.glob("*.py")):
        if p.stem in skip:
            continue
        importlib.import_module(f"app.models.{p.stem}")


_load_models()

from app.dependencies import async_session_maker
from app.models.organization import Organization
from app.models.user import User
from app.models.connection import Connection
from app.models.connection_tool import ConnectionTool
from app.models.user_connection_credentials import UserConnectionCredentials
from app.models.user_connection_tool import UserConnectionTool
from app.data_sources.clients.gmail_client import TOOLS as GMAIL_TOOLS


async def seed(user_email: str = "admin@bow.dev") -> None:
    async with async_session_maker() as db:
        org = (await db.execute(select(Organization).limit(1))).scalar_one_or_none()
        if not org:
            raise SystemExit("No organization found — register an admin + create an org first.")
        user = (await db.execute(select(User).where(User.email == user_email))).scalar_one_or_none()
        if not user:
            raise SystemExit(f"No user {user_email!r} found.")

        # 1) Gmail connection (admin-configured app, user-required OAuth)
        conn = (await db.execute(
            select(Connection).where(
                Connection.organization_id == str(org.id),
                Connection.type == "gmail",
            )
        )).scalar_one_or_none()
        if not conn:
            conn = Connection(
                name="Gmail",
                type="gmail",
                config={},
                organization_id=str(org.id),
                auth_policy="user_required",
                allowed_user_auth_modes=["oauth"],
                is_active=True,
            )
            conn.encrypt_credentials({
                "oauth_client_id": "dev-google-client-id.apps.googleusercontent.com",
                "oauth_client_secret": "dev-google-client-secret",
            })
            db.add(conn)
            await db.flush()
            print(f"  + connection gmail {conn.id}")
        else:
            print(f"  = connection gmail {conn.id} (exists)")

        # 2) ConnectionTool rows from the declared catalog
        existing_tools = {
            t.name: t for t in (await db.execute(
                select(ConnectionTool).where(ConnectionTool.connection_id == conn.id)
            )).scalars().all()
        }
        tool_rows = {}
        for spec in GMAIL_TOOLS:
            ct = existing_tools.get(spec["name"])
            if not ct:
                ct = ConnectionTool(
                    name=spec["name"],
                    connection_id=str(conn.id),
                    description=spec["description"],
                    input_schema=spec.get("input_schema"),
                    is_enabled=True,
                    policy="allow",
                )
                db.add(ct)
                await db.flush()
                print(f"  + tool {spec['name']} {ct.id}")
            tool_rows[spec["name"]] = ct

        # 3) Per-user credential (marks the user as "connected")
        cred = (await db.execute(
            select(UserConnectionCredentials).where(
                UserConnectionCredentials.connection_id == str(conn.id),
                UserConnectionCredentials.user_id == str(user.id),
            )
        )).scalar_one_or_none()
        if not cred:
            cred = UserConnectionCredentials(
                connection_id=str(conn.id),
                user_id=str(user.id),
                organization_id=str(org.id),
                auth_mode="oauth",
                is_active=True,
                is_primary=True,
                expires_at=datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=1),
            )
            cred.encrypt_credentials({
                "access_token": "dev-fake-access-token",
                "refresh_token": "dev-fake-refresh-token",
                "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            })
            db.add(cred)
            print(f"  + user_connection_credentials {user_email}")
        else:
            print(f"  = user_connection_credentials {user_email} (exists)")

        # 4) UserConnectionTool overlays — enable every tool for this user
        for name, ct in tool_rows.items():
            uct = (await db.execute(
                select(UserConnectionTool).where(
                    UserConnectionTool.connection_id == str(conn.id),
                    UserConnectionTool.user_id == str(user.id),
                    UserConnectionTool.tool_name == name,
                )
            )).scalar_one_or_none()
            if not uct:
                db.add(UserConnectionTool(
                    connection_id=str(conn.id),
                    user_id=str(user.id),
                    tool_name=name,
                    connection_tool_id=str(ct.id),
                    is_accessible=True,
                    status="accessible",
                ))
                print(f"  + user_connection_tool {name}")

        await db.commit()
        print(f"\nSeeded Gmail integration for org={org.id} user={user_email}")


if __name__ == "__main__":
    email = sys.argv[1] if len(sys.argv) > 1 else "admin@bow.dev"
    asyncio.run(seed(email))
