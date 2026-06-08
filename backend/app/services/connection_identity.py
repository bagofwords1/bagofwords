"""
Admin query-identity toggle for delegated (OBO/OAuth) connections.

For connections that support per-user OAuth tokens (``"oauth"`` in
``allowed_user_auth_modes``), an admin/owner can choose to run interactive queries
either as the **service account** (the connection's stored system/principal creds) or
as **themselves** (their own delegated token). The default is "self": the service
principal is never used silently for an admin's interactive queries — if the admin has
no personal token yet, the query is blocked and the UI prompts them to Connect.

The preference is stored per ``(user, connection)`` on
``UserConnectionCredentials.metadata_json`` as ``{"query_identity": "self"|"service_account"}``.
When an admin chooses "service account" before ever connecting, a lightweight marker row
(``auth_mode == "service_account"``, empty encrypted payload) holds the preference.
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.connection import Connection
from app.models.user_connection_credentials import UserConnectionCredentials

QUERY_IDENTITY_SELF = "self"
QUERY_IDENTITY_SERVICE = "service_account"
# auth_mode value used for a preference-only marker row that carries no real token.
SERVICE_ACCOUNT_MARKER_MODE = "service_account"

VALID_IDENTITIES = {QUERY_IDENTITY_SELF, QUERY_IDENTITY_SERVICE}


def supports_user_token(connection: Connection) -> bool:
    """True if this connection authenticates users with a per-user OAuth/OBO token."""
    modes = connection.allowed_user_auth_modes or []
    return "oauth" in modes


def identity_pref_from_row(row: Optional[UserConnectionCredentials]) -> str:
    """Read the stored query-identity preference; defaults to 'self'."""
    if row is not None:
        md = getattr(row, "metadata_json", None)
        if isinstance(md, dict):
            v = md.get("query_identity")
            if v in VALID_IDENTITIES:
                return v
    return QUERY_IDENTITY_SELF


def row_has_token(row: Optional[UserConnectionCredentials]) -> bool:
    """True if the row carries a real delegated credential (not just a pref marker)."""
    return row is not None and row.auth_mode != SERVICE_ACCOUNT_MARKER_MODE


async def get_user_conn_cred_row(
    db: AsyncSession, connection: Connection, user
) -> Optional[UserConnectionCredentials]:
    """Fetch the user's primary active connection-level credential/preference row."""
    res = await db.execute(
        select(UserConnectionCredentials)
        .where(
            UserConnectionCredentials.connection_id == str(connection.id),
            UserConnectionCredentials.user_id == str(user.id),
            UserConnectionCredentials.is_active == True,  # noqa: E712
        )
        .order_by(
            UserConnectionCredentials.is_primary.desc(),
            UserConnectionCredentials.updated_at.desc(),
        )
    )
    return res.scalars().first()


async def build_token_identity_status(
    db: AsyncSession,
    connection: Connection,
    user,
    cached_status: str = "unknown",
    last_checked=None,
):
    """Build the per-user status for a token-supporting connection, honoring the
    admin query-identity toggle. Returns a DataSourceUserStatus."""
    from app.schemas.data_source_schema import DataSourceUserStatus

    row = await get_user_conn_cred_row(db, connection, user)
    admin_or_owner = await is_admin_or_owner(db, connection, user)
    pref = identity_pref_from_row(row)
    has_token = row_has_token(row)

    # Admin/owner explicitly chose the service account → run via system creds.
    if pref == QUERY_IDENTITY_SERVICE and admin_or_owner:
        return DataSourceUserStatus(
            has_user_credentials=False,
            connection=cached_status,
            effective_auth="system",
            uses_fallback=True,
            query_identity=QUERY_IDENTITY_SERVICE,
            can_switch_identity=True,
            last_checked_at=last_checked,
        )

    # Default "self": use the user's own delegated token when present.
    if has_token:
        user_conn_status = "success" if row.last_used_at else "unknown"
        return DataSourceUserStatus(
            has_user_credentials=True,
            auth_mode=row.auth_mode,
            is_primary=row.is_primary,
            last_used_at=row.last_used_at,
            expires_at=row.expires_at,
            connection=user_conn_status,
            effective_auth="user",
            uses_fallback=False,
            query_identity=QUERY_IDENTITY_SELF,
            can_switch_identity=admin_or_owner,
            credentials_id=str(getattr(row, "id", "")) if getattr(row, "id", None) else None,
            last_checked_at=row.last_used_at,
        )

    # "self" but not connected yet → no proven access; UI prompts Connect.
    return DataSourceUserStatus(
        has_user_credentials=False,
        connection="offline",
        effective_auth="none",
        query_identity=QUERY_IDENTITY_SELF,
        can_switch_identity=admin_or_owner,
    )


async def is_admin_or_owner(db: AsyncSession, connection: Connection, user) -> bool:
    """True if the user may switch identity: org admin/manage_connections, or owner of
    any data source linked to the connection."""
    try:
        for ds in (connection.data_sources or []):
            if str(getattr(ds, "owner_user_id", "")) == str(getattr(user, "id", "")):
                return True
    except Exception:
        pass
    try:
        from app.core.permission_resolver import resolve_permissions, FULL_ADMIN
        resolved = await resolve_permissions(
            db, str(user.id), str(connection.organization_id)
        )
        return (
            FULL_ADMIN in resolved.org_permissions
            or resolved.has_org_permission("manage_connections")
        )
    except Exception:
        return False
