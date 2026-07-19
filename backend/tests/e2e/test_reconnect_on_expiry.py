"""E2E reproduction + regression for delegated-OAuth re-auth on token expiry.

Reported symptom: a per-user OAuth connection (Gmail / Power BI / Fabric / …)
fails mid-use with a raw provider 401 (e.g. "Gmail API 401: Request had invalid
authentication credentials"). The user is never told to reconnect, and the
connection still reports itself as connected.

Root cause exercised here (pre-fix, these assertions FAIL):
  1. `build_token_identity_status` reports `has_user_credentials=True` for an
     expired row with no usable refresh token and exposes no `needs_reconnect`
     signal, so the UI never offers a reconnect affordance.
  2. `resolve_credentials` returns the stale (expired) creds instead of raising
     a typed "reconnect required" error, so the failure surfaces downstream as
     an opaque provider 401.

Post-fix, the same assertions PASS: an expired-and-unrefreshable row is flagged
`needs_reconnect=True` (while still `has_user_credentials=True`), and
`resolve_credentials` raises a 403 carrying a machine-readable
`code="reconnect_required"`. A still-valid row is unaffected.
"""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.dependencies import async_session_maker
from app.models.connection import Connection
from app.models.user import User
from app.models.user_connection_credentials import UserConnectionCredentials


async def _insert_cred_row(
    connection_id: str,
    user_id: str,
    org_id: str,
    *,
    creds: dict,
    expires_at: datetime,
):
    """Insert a primary active OAuth credential row with the given creds/expiry."""
    async with async_session_maker() as db:
        row = UserConnectionCredentials(
            connection_id=connection_id,
            user_id=user_id,
            organization_id=org_id,
            auth_mode="oauth",
            is_active=True,
            is_primary=True,
            expires_at=expires_at.replace(tzinfo=None) if expires_at.tzinfo else expires_at,
        )
        row.encrypt_credentials(creds)
        db.add(row)
        await db.commit()


def _make_powerbi_connection(create_connection, token, org_id, name):
    return create_connection(
        name=name,
        type="powerbi",
        config={},
        credentials={"tenant_id": "t1", "client_id": "c1", "client_secret": "s1"},
        auth_policy="user_required",
        allowed_user_auth_modes=["oauth"],
        user_token=token,
        org_id=org_id,
    )


async def _load(connection_id: str, user_id: str):
    from sqlalchemy.orm import selectinload
    async with async_session_maker() as db:
        conn = (await db.execute(
            select(Connection)
            .options(selectinload(Connection.organization), selectinload(Connection.data_sources))
            .where(Connection.id == connection_id)
        )).scalars().first()
        user = (await db.execute(select(User).where(User.id == user_id))).scalars().first()
        return conn, user


class TestExpiredTokenNeedsReconnect:
    """An expired OAuth token with no usable refresh token must surface as
    needs_reconnect and block query execution with a typed reconnect error."""

    @pytest.mark.asyncio
    async def test_status_flags_needs_reconnect(
        self, test_client, login_user, create_connection, create_user, whoami
    ):
        user = create_user()
        token = login_user(user["email"], user["password"])
        me = whoami(token)
        org_id = me["organizations"][0]["id"]
        user_id = me["id"]

        conn = _make_powerbi_connection(create_connection, token, org_id, "PBI Expired")
        # Expired access token, NO refresh token — nothing to renew with.
        await _insert_cred_row(
            conn["id"], user_id, org_id,
            creds={"access_token": "stale", "token_type": "Bearer",
                   "expires_at": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()},
            expires_at=datetime.now(timezone.utc) - timedelta(hours=1),
        )

        from app.services.connection_identity import build_token_identity_status
        conn_obj, user_obj = await _load(conn["id"], user_id)
        async with async_session_maker() as db:
            status = await build_token_identity_status(db, conn_obj, user_obj)

        # The row still exists, so the user is "connected"...
        assert status.has_user_credentials is True
        # ...but the token is dead and cannot self-heal → needs reconnect.
        assert status.needs_reconnect is True

    @pytest.mark.asyncio
    async def test_resolve_credentials_raises_typed_reconnect(
        self, test_client, login_user, create_connection, create_user, whoami
    ):
        user = create_user()
        token = login_user(user["email"], user["password"])
        me = whoami(token)
        org_id = me["organizations"][0]["id"]
        user_id = me["id"]

        conn = _make_powerbi_connection(create_connection, token, org_id, "PBI Expired 2")
        await _insert_cred_row(
            conn["id"], user_id, org_id,
            creds={"access_token": "stale", "token_type": "Bearer",
                   "expires_at": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()},
            expires_at=datetime.now(timezone.utc) - timedelta(hours=1),
        )

        from app.services.connection_service import ConnectionService
        conn_obj, user_obj = await _load(conn["id"], user_id)
        async with async_session_maker() as db:
            with pytest.raises(HTTPException) as exc:
                await ConnectionService().resolve_credentials(db, conn_obj, user_obj)
        assert exc.value.status_code == 403
        detail = exc.value.detail
        code = detail.get("code") if isinstance(detail, dict) else None
        assert code == "reconnect_required", f"expected reconnect_required, got {detail!r}"


class TestValidTokenUnaffected:
    """A still-valid token must NOT be flagged for reconnect (no over-triggering)."""

    @pytest.mark.asyncio
    async def test_valid_token_no_reconnect(
        self, test_client, login_user, create_connection, create_user, whoami
    ):
        user = create_user()
        token = login_user(user["email"], user["password"])
        me = whoami(token)
        org_id = me["organizations"][0]["id"]
        user_id = me["id"]

        conn = _make_powerbi_connection(create_connection, token, org_id, "PBI Valid")
        future = datetime.now(timezone.utc) + timedelta(hours=1)
        await _insert_cred_row(
            conn["id"], user_id, org_id,
            creds={"access_token": "fresh", "refresh_token": "r", "token_type": "Bearer",
                   "expires_at": future.isoformat()},
            expires_at=future,
        )

        from app.services.connection_identity import build_token_identity_status
        from app.services.connection_service import ConnectionService
        conn_obj, user_obj = await _load(conn["id"], user_id)
        async with async_session_maker() as db:
            status = await build_token_identity_status(db, conn_obj, user_obj)
            creds = await ConnectionService().resolve_credentials(db, conn_obj, user_obj)

        assert status.has_user_credentials is True
        assert status.needs_reconnect is False
        assert creds.get("access_token") == "fresh"


class TestOBOHybridUpgrade:
    """Entra OBO rows carry no refresh token, so they expire un-renewable and
    surface needs_reconnect. An interactive reconnect stores a refresh-token-
    bearing credential; a later login must NOT re-OBO over that durable row."""

    @pytest.mark.asyncio
    async def test_obo_expired_needs_reconnect(
        self, test_client, login_user, create_connection, create_user, whoami
    ):
        user = create_user()
        token = login_user(user["email"], user["password"])
        me = whoami(token)
        org_id = me["organizations"][0]["id"]
        user_id = me["id"]

        conn = _make_powerbi_connection(create_connection, token, org_id, "PBI OBO")
        # OBO grant returns NO refresh token; simulate an expired OBO row.
        await _insert_cred_row(
            conn["id"], user_id, org_id,
            creds={"access_token": "obo-stale", "token_type": "Bearer",
                   "expires_at": (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()},
            expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        )
        from app.services.connection_identity import build_token_identity_status
        conn_obj, user_obj = await _load(conn["id"], user_id)
        async with async_session_maker() as db:
            status = await build_token_identity_status(db, conn_obj, user_obj)
        assert status.has_user_credentials is True
        assert status.needs_reconnect is True

    @pytest.mark.asyncio
    async def test_login_does_not_clobber_durable_reconnect(
        self, test_client, login_user, create_connection, create_user, whoami
    ):
        from tests.mocks.mock_oauth_provider import patch_oauth_for_tests

        user = create_user()
        token = login_user(user["email"], user["password"])
        me = whoami(token)
        org_id = me["organizations"][0]["id"]
        user_id = me["id"]

        conn = _make_powerbi_connection(create_connection, token, org_id, "PBI OBO Upgrade")
        # An interactive reconnect already upgraded this row: it carries a refresh
        # token but the access token has since expired. auto_provision must NOT
        # re-OBO over it (that would drop the refresh token).
        await _insert_cred_row(
            conn["id"], user_id, org_id,
            creds={"access_token": "expired-but-refreshable", "refresh_token": "durable-rt",
                   "token_type": "Bearer",
                   "expires_at": (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()},
            expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        )

        from app.services.connection_oauth_service import auto_provision_connection_credentials
        conn_obj, user_obj = await _load(conn["id"], user_id)
        with patch_oauth_for_tests() as mock:
            async with async_session_maker() as db:
                # reload the user in this session
                user_row = (await db.execute(select(User).where(User.id == user_id))).scalars().first()
                summary = await auto_provision_connection_credentials(db, user_row, "login_token")

        # Skipped as refreshable — no OBO exchange, refresh token preserved.
        assert len(mock.obo_log) == 0, "must not re-OBO over a refreshable row"
        skipped = {s["connection_id"]: s.get("reason") for s in summary["skipped"]}
        assert skipped.get(conn["id"]) == "refreshable_credentials_exist"

        async with async_session_maker() as db:
            from app.models.user_connection_credentials import UserConnectionCredentials
            row = (await db.execute(select(UserConnectionCredentials).where(
                UserConnectionCredentials.connection_id == conn["id"],
                UserConnectionCredentials.user_id == user_id,
                UserConnectionCredentials.is_active == True,
            ))).scalars().first()
            assert row.decrypt_credentials().get("refresh_token") == "durable-rt"
