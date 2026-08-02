"""A per-user OAuth connector (MCP/Custom API connected via DCR or an admin
OAuth app) stores an OAuth *client*, never a token. Anything that runs without
a user in context therefore has nothing to authenticate with, and the provider
answers 401 — which is how a Monday connector ended up reporting "Indexing
failed: 401 Unauthorized" on every scheduled sweep, with a Retry button that
could only reproduce it.

These tests pin the predicate that tells those connections apart from ones with
real system credentials, and the three call sites that must honour it.
"""
import json
import types

import pytest

from app.services.connection_identity import catalog_requires_user_sign_in
from app.services.connection_service import ConnectionService


def _conn(*, auth_policy="user_required", auth_type="dcr", credentials=True, type="mcp",
          config_as_str=False):
    """A stand-in for a Connection row — only the fields the predicate reads."""
    config = {"server_url": "https://mcp.monday.com/mcp", "transport": "streamable_http"}
    if auth_type is not None:
        config["auth_type"] = auth_type
    return types.SimpleNamespace(
        id="conn-1",
        type=type,
        auth_policy=auth_policy,
        config=json.dumps(config) if config_as_str else config,
        # The DCR blob: an OAuth client and endpoints, and no token anywhere.
        credentials="<encrypted>" if credentials else None,
        allowed_user_auth_modes=["oauth"] if auth_policy == "user_required" else None,
    )


# ── The predicate ──────────────────────────────────────────────────────────

def test_dcr_connection_needs_a_signed_in_user():
    # The bug in one assertion: `bool(connection.credentials)` is True here, but
    # there is no token in that blob, so a user-less call goes out unauthenticated.
    conn = _conn(auth_type="dcr")
    assert bool(conn.credentials) is True
    assert catalog_requires_user_sign_in(conn) is True


def test_oauth_app_connection_needs_a_signed_in_user():
    assert catalog_requires_user_sign_in(_conn(auth_type="oauth_app")) is True


def test_config_stored_as_json_string_is_parsed():
    assert catalog_requires_user_sign_in(_conn(auth_type="dcr", config_as_str=True)) is True


def test_system_only_connection_indexes_admin_side():
    # An admin-supplied bearer/api key lives in the connection credentials and
    # authenticates fine with no user in context.
    assert catalog_requires_user_sign_in(
        _conn(auth_policy="system_only", auth_type="bearer")
    ) is False


def test_user_required_with_shared_secret_still_indexes_admin_side():
    # user_required + bearer: each user MAY supply their own token, but the
    # admin's stored one still works for indexing. Not the broken case.
    assert catalog_requires_user_sign_in(
        _conn(auth_policy="user_required", auth_type="bearer")
    ) is False


def test_credential_less_but_indexable_source_is_not_gated():
    """SQLite/DuckDB/QVD index from `config` alone, and an MCP server with no
    auth needs no token — a user_required one of those must still index
    admin-side. Gating it here would strand every such catalog at zero items."""
    assert catalog_requires_user_sign_in(
        _conn(type="sqlite", auth_policy="user_required", auth_type=None, credentials=False)
    ) is False
    assert catalog_requires_user_sign_in(
        _conn(auth_policy="user_required", auth_type="none", credentials=False)
    ) is False


# ── refresh_tools: skip instead of 401 ─────────────────────────────────────

@pytest.mark.asyncio
async def test_refresh_tools_without_user_skips_per_user_oauth_connection(monkeypatch):
    """No user + no system credentials → don't even build a client.

    Before the fix this constructed an McpClient with no Authorization header
    and let the provider's 401 bubble up as "Failed to refresh tools".
    """
    svc = ConnectionService()

    async def _boom(*a, **kw):  # pragma: no cover - must not be reached
        raise AssertionError("construct_client must not run without credentials")

    monkeypatch.setattr(svc, "construct_client", _boom)

    tools = await svc.refresh_tools(db=None, connection=_conn(), current_user=None)

    assert tools == []
    assert svc.last_tools_awaiting_sign_in is True


@pytest.mark.asyncio
async def test_refresh_tools_with_a_user_still_runs(monkeypatch):
    """The same connection IS indexable as a signed-in user — that's the path
    the OAuth callback and the admin's Retry take."""
    svc = ConnectionService()
    built = {}

    class _Client:
        async def alist_tools(self):
            return []

    async def _construct(db, connection, current_user, **kw):
        built["user"] = current_user
        return _Client()

    monkeypatch.setattr(svc, "construct_client", _construct)
    # refresh_tools writes ConnectionTool rows; stop at the client call.
    with pytest.raises(Exception):
        await svc.refresh_tools(
            db=None, connection=_conn(), current_user=types.SimpleNamespace(id="u1"),
        )
    assert built.get("user") is not None
    assert svc.last_tools_awaiting_sign_in is False
