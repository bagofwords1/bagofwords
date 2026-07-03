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

from datetime import datetime

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


def identity_pref_from_row(row: UserConnectionCredentials | None) -> str:
    """Read the stored query-identity preference; defaults to 'self'."""
    if row is not None:
        md = getattr(row, "metadata_json", None)
        if isinstance(md, dict):
            v = md.get("query_identity")
            if v in VALID_IDENTITIES:
                return v
    return QUERY_IDENTITY_SELF


def row_has_token(row: UserConnectionCredentials | None) -> bool:
    """True if the row carries a real delegated credential (not just a pref marker)."""
    return row is not None and row.auth_mode != SERVICE_ACCOUNT_MARKER_MODE


async def get_user_conn_cred_row(
    db: AsyncSession, connection: Connection, user
) -> UserConnectionCredentials | None:
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
        from app.core.permission_resolver import FULL_ADMIN, resolve_permissions
        resolved = await resolve_permissions(
            db, str(user.id), str(connection.organization_id)
        )
        return (
            FULL_ADMIN in resolved.org_permissions
            or resolved.has_org_permission("manage_connections")
        )
    except Exception:
        return False


# ── Kerberos per-user SSO (constrained delegation) ──────────────────────────
#
# Unlike OBO, Kerberos SSO stores no per-user secret: a member's access comes
# from the app service account's delegation rights + the member's AD principal
# (UPN), derived from their login identity. So "has access" is a property of the
# identity, not of a stored credential — the per-user status/overlay machinery
# (built for stored tokens) needs to treat a resolvable UPN as first-class user
# access, otherwise a Kerberos member falls through to "no access" and gets an
# empty catalog even though they can query fine.
#
# A lightweight marker row (auth_mode == KERBEROS_SSO_MODE, EMPTY encrypted
# payload apart from the resolved principal) records verification status only —
# never a credential — so the admin gets a per-member roster and the badge can
# show "verified" vs "not checked".

KERBEROS_SSO_MODE = "kerberos_delegated"


def supports_user_kerberos_sso(connection: Connection) -> bool:
    """True if members authenticate to this connection via Kerberos delegation."""
    return KERBEROS_SSO_MODE in (connection.allowed_user_auth_modes or [])


def resolve_kerberos_principal(user, row: UserConnectionCredentials | None = None) -> str | None:
    """The AD principal (UPN) a member's queries impersonate.

    Precedence: an explicit principal the member saved > their login identity.
    Returns None when neither yields a UPN-shaped value (login isn't an AD UPN
    and no override saved) — the member must set their principal explicitly.
    """
    if row is not None and row.auth_mode == KERBEROS_SSO_MODE:
        try:
            explicit = (row.decrypt_credentials() or {}).get("kerberos_impersonate")
        except Exception:
            explicit = None
        if explicit and "@" in explicit:
            return explicit.strip()
    email = (getattr(user, "email", None) or "").strip()
    return email if "@" in email else None


async def build_kerberos_sso_status(db: AsyncSession, connection: Connection, user, cached_status, last_checked):
    """Per-user status for a Kerberos-SSO connection (no stored secret).

    A resolvable UPN → the member has delegated access (effective_auth="user"),
    so their overlay builds by impersonating them. Connection health is "success"
    once a verify/query has recorded the marker row, else "unknown". No UPN →
    effective_auth="none" and connection="not_connected" (prompt for principal).
    """
    from app.schemas.data_source_schema import DataSourceUserStatus

    row = await get_user_conn_cred_row(db, connection, user)
    marker = row if (row is not None and row.auth_mode == KERBEROS_SSO_MODE) else None
    principal = resolve_kerberos_principal(user, marker)

    if not principal:
        return DataSourceUserStatus(
            has_user_credentials=False,
            auth_mode=KERBEROS_SSO_MODE,
            connection="not_connected",
            effective_auth="none",
            last_checked_at=last_checked,
        )

    verified = bool(marker and marker.last_used_at)
    return DataSourceUserStatus(
        has_user_credentials=True,
        auth_mode=KERBEROS_SSO_MODE,
        is_primary=bool(getattr(marker, "is_primary", True)) if marker else True,
        last_used_at=getattr(marker, "last_used_at", None),
        connection="success" if verified else "unknown",
        effective_auth="user",
        credentials_id=str(marker.id) if marker and getattr(marker, "id", None) else None,
        last_checked_at=getattr(marker, "last_used_at", None) or last_checked,
    )


async def record_kerberos_verification(db: AsyncSession, connection: Connection, user, success: bool, error: str | None = None) -> None:
    """Upsert the member's Kerberos-SSO marker row (status only — no secret).

    Drives the "verified" badge and the admin roster. Stores the resolved
    principal + last error in metadata_json; stamps last_used_at on success.
    """
    row = await get_user_conn_cred_row(db, connection, user)
    principal = resolve_kerberos_principal(
        user, row if (row is not None and row.auth_mode == KERBEROS_SSO_MODE) else None
    )
    if row is None or row.auth_mode != KERBEROS_SSO_MODE:
        row = UserConnectionCredentials(
            connection_id=str(connection.id),
            user_id=str(user.id),
            organization_id=str(connection.organization_id),
            auth_mode=KERBEROS_SSO_MODE,
            is_active=True,
            is_primary=True,
        )
        row.encrypt_credentials({"kerberos_impersonate": principal} if principal else {})
    md = dict(getattr(row, "metadata_json", None) or {})
    md["principal"] = principal
    md["last_error"] = None if success else (error or "verification failed")
    row.metadata_json = md
    if success:
        row.last_used_at = datetime.utcnow()
    db.add(row)
    await db.commit()
