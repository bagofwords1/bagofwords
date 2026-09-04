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


# Auth modes whose stored connection credentials are an OAuth *client*
# (client_id/secret + endpoints), never a token: every user signs in for
# themselves. DCR registers the client dynamically; oauth_app takes an
# admin-registered one. Either way there is nothing to authenticate WITH
# outside a signed-in user's session.
PER_USER_OAUTH_AUTH_TYPES = {"dcr", "oauth_app"}


def catalog_requires_user_sign_in(connection: Connection) -> bool:
    """True when this connection's catalog can ONLY be discovered as a
    signed-in user, because nothing it stores can authenticate a call made with
    no user in context.

    `bool(connection.credentials)` does not answer this. A per-user OAuth
    connector (an MCP/Custom-API tile connected via DCR or an admin OAuth app)
    stores a full credentials blob — but it holds only the OAuth *client*, so a
    system-context crawl goes out with no Authorization header at all and the
    provider answers 401. Callers that run without a user (background indexing,
    the scheduled reindex sweep) have to ask this, or they schedule a failure
    that no amount of retrying can fix.

    Deliberately narrow — it asks only about the auth mode the connection was
    configured with. Everything else keeps indexing admin-side as before:
    system_only connections, delegated connections carrying real
    service-principal credentials, and credential-less-but-indexable sources
    (SQLite/DuckDB/QVD, whose catalog comes from `config`).
    """
    if connection.auth_policy != "user_required":
        return False

    config = connection.config
    if isinstance(config, str):
        import json

        try:
            config = json.loads(config)
        except (TypeError, ValueError):
            config = {}
    return (config or {}).get("auth_type") in PER_USER_OAUTH_AUTH_TYPES


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


class UserCredentialIndex:
    """One user's credential rows for a whole list of agents, loaded up front.

    Building a connection's ``user_status`` otherwise costs one query per
    connection (``UserConnectionCredentials``) plus one per data source
    (``UserDataSourceCredentials``). Those are serialized round-trips, so on a
    workspace with hundreds of agents they dominate the agent-list endpoints —
    hundreds of queries against two tables, all keyed by the same user. This
    loads both in two queries and answers the same lookups from memory.

    Row selection mirrors the per-connection queries exactly: active rows only,
    ranked ``is_primary`` then most-recently-updated, first one wins.
    """

    __slots__ = ("_conn_rows", "_ds_rows")

    def __init__(self, conn_rows: dict, ds_rows: dict):
        self._conn_rows = conn_rows
        self._ds_rows = ds_rows

    @classmethod
    async def build(
        cls, db: AsyncSession, user, *, connection_ids, data_source_ids
    ) -> "UserCredentialIndex":
        from app.models.user_data_source_credentials import UserDataSourceCredentials

        conn_rows: dict = {}
        ds_rows: dict = {}
        if user is None:
            return cls(conn_rows, ds_rows)

        conn_ids = [str(c) for c in (connection_ids or [])]
        if conn_ids:
            rows = (await db.execute(
                select(UserConnectionCredentials)
                .where(
                    UserConnectionCredentials.connection_id.in_(conn_ids),
                    UserConnectionCredentials.user_id == str(user.id),
                    UserConnectionCredentials.is_active == True,  # noqa: E712
                )
                .order_by(
                    UserConnectionCredentials.is_primary.desc(),
                    UserConnectionCredentials.updated_at.desc(),
                )
            )).scalars().all()
            for row in rows:
                conn_rows.setdefault(str(row.connection_id), row)

        ds_ids = [str(d) for d in (data_source_ids or [])]
        if ds_ids:
            rows = (await db.execute(
                select(UserDataSourceCredentials)
                .where(
                    UserDataSourceCredentials.data_source_id.in_(ds_ids),
                    UserDataSourceCredentials.user_id == str(user.id),
                    UserDataSourceCredentials.is_active == True,  # noqa: E712
                )
                .order_by(
                    UserDataSourceCredentials.is_primary.desc(),
                    UserDataSourceCredentials.updated_at.desc(),
                )
            )).scalars().all()
            for row in rows:
                ds_rows.setdefault(str(row.data_source_id), row)

        return cls(conn_rows, ds_rows)

    def connection_row(self, connection_id) -> UserConnectionCredentials | None:
        return self._conn_rows.get(str(connection_id))

    def data_source_row(self, data_source_id):
        return self._ds_rows.get(str(data_source_id))


async def build_token_identity_status(
    db: AsyncSession,
    connection: Connection,
    user,
    cached_status: str = "unknown",
    last_checked=None,
    *,
    cred_index: "UserCredentialIndex | None" = None,
):
    """Build the per-user status for a token-supporting connection, honoring the
    admin query-identity toggle. Returns a DataSourceUserStatus.

    ``cred_index`` (list endpoints) answers the credential lookup from a
    prefetched map instead of querying per connection.
    """
    from app.schemas.data_source_schema import DataSourceUserStatus

    row = (
        cred_index.connection_row(connection.id) if cred_index is not None
        else await get_user_conn_cred_row(db, connection, user)
    )
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


async def build_kerberos_sso_status(
    db: AsyncSession, connection: Connection, user, cached_status, last_checked,
    *, cred_index: "UserCredentialIndex | None" = None,
):
    """Per-user status for a Kerberos-SSO connection (no stored secret).

    A resolvable UPN → the member has delegated access (effective_auth="user"),
    so their overlay builds by impersonating them. Connection health is "success"
    once a verify/query has recorded the marker row, else "unknown". No UPN →
    effective_auth="none" and connection="not_connected" (prompt for principal).
    """
    from app.schemas.data_source_schema import DataSourceUserStatus

    row = (
        cred_index.connection_row(connection.id) if cred_index is not None
        else await get_user_conn_cred_row(db, connection, user)
    )
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

    last_error = (getattr(marker, "metadata_json", None) or {}).get("last_error") if marker else None
    verified = bool(marker and marker.last_used_at and not last_error)
    return DataSourceUserStatus(
        has_user_credentials=True,
        auth_mode=KERBEROS_SSO_MODE,
        is_primary=bool(getattr(marker, "is_primary", True)) if marker else True,
        last_used_at=getattr(marker, "last_used_at", None),
        connection="success" if verified else ("offline" if last_error else "unknown"),
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
