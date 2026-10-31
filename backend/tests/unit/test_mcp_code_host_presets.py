"""GitLab / Bitbucket MCP presets, the Atlassian endpoint move, and Basic auth.

Contract:
  * the Atlassian preset uses the Streamable HTTP endpoint (/v1/mcp/authv2),
    not the retired /v1/sse;
  * GitLab connects with per-user OAuth (DCR) or a token;
  * Bitbucket connects to Atlassian's API-token endpoint with email + API
    token (HTTP Basic) or a service-account key (Bearer);
  * an MCP connection with Basic credentials sends `Authorization: Basic …`;
  * existing Atlassian connections are migrated off /v1/sse, nothing else is;
  * a DCR connection's token audience follows the server it now points at.
"""
import asyncio
import base64
import importlib.util
import json
from pathlib import Path

import pytest
import sqlalchemy as sa

from app.data_sources.clients.mcp_client import McpClient
from app.schemas.data_source_registry import (
    ATLASSIAN_LEGACY_SSE_URL, ATLASSIAN_MCP_TOKEN_URL, ATLASSIAN_MCP_URL, mcp_preset, mcp_presets,
)

BACKEND = Path(__file__).resolve().parents[2]


def test_atlassian_preset_uses_the_streamable_http_endpoint():
    p = mcp_preset("atlassian")
    assert p.server_url == ATLASSIAN_MCP_URL == "https://mcp.atlassian.com/v1/mcp/authv2"
    assert p.transport == "streamable_http"
    assert p.auth == "oauth"
    assert all(x["server_url"] != ATLASSIAN_LEGACY_SSE_URL for x in mcp_presets())


def test_gitlab_preset_is_per_user_oauth_with_token_fallback():
    p = mcp_preset("gitlab")
    assert p.server_url == "https://gitlab.com/api/v4/mcp"
    assert p.auth == "oauth"
    assert p.allowed_auth == ["dcr", "bearer"]
    assert p.sample_tools


def test_bitbucket_preset_uses_atlassian_api_token_endpoint():
    p = mcp_preset("bitbucket")
    assert p.server_url == ATLASSIAN_MCP_TOKEN_URL == "https://mcp.atlassian.com/v2/mcp"
    assert p.auth == "basic"
    assert set(p.allowed_auth) == {"basic", "bearer"}
    # Different URL from the Atlassian (OAuth) tile, so edit mode resolves each
    # connection back to its own tile.
    assert p.server_url != mcp_preset("atlassian").server_url


def test_new_presets_are_in_the_catalog():
    keys = {p["key"] for p in mcp_presets()}
    assert {"gitlab", "bitbucket", "atlassian"} <= keys


def test_basic_credentials_become_a_basic_auth_header():
    c = McpClient(server_url=ATLASSIAN_MCP_TOKEN_URL, transport="streamable_http",
                  username="dev@example.com", password="tok-123")
    expected = "Basic " + base64.b64encode(b"dev@example.com:tok-123").decode()
    assert c._build_headers()["Authorization"] == expected


def test_bearer_still_wins_over_basic():
    c = McpClient(server_url="https://x", token="t", username="u", password="p")
    assert c._build_headers()["Authorization"] == "Bearer t"


def test_basic_auth_variant_is_registered_for_mcp():
    from app.schemas.data_source_registry import REGISTRY
    variants = REGISTRY["mcp"].credentials_auth.by_auth
    assert "basic" in variants
    fields = variants["basic"].schema.model_fields
    assert {"username", "password"} <= set(fields)


# ── migration ─────────────────────────────────────────────────────────────

def _load_migration():
    path = BACKEND / "alembic/versions/atlv201_atlassian_mcp_v2_endpoint.py"
    spec = importlib.util.spec_from_file_location("atlv201", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_migration_moves_only_legacy_atlassian_connections():
    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext

    engine = sa.create_engine("sqlite://")
    meta = sa.MetaData()
    t = sa.Table("connections", meta, sa.Column("id", sa.String, primary_key=True),
                 sa.Column("type", sa.String), sa.Column("config", sa.JSON))
    meta.create_all(engine)
    rows = [
        {"id": "a", "type": "mcp", "config": {"server_url": ATLASSIAN_LEGACY_SSE_URL, "transport": "sse", "auth_type": "dcr"}},
        {"id": "b", "type": "mcp", "config": {"server_url": ATLASSIAN_LEGACY_SSE_URL + "/", "transport": "sse"}},
        {"id": "c", "type": "mcp", "config": {"server_url": "https://mcp.linear.app/mcp", "transport": "streamable_http"}},
        {"id": "d", "type": "postgresql", "config": {"host": "db"}},
    ]
    with engine.begin() as conn:
        conn.execute(t.insert(), rows)
        with Operations.context(MigrationContext.configure(conn)):
            _load_migration().upgrade()
        got = {r.id: r.config for r in conn.execute(sa.select(t.c.id, t.c.config))}
    for cid in ("a", "b"):
        cfg = got[cid] if isinstance(got[cid], dict) else json.loads(got[cid])
        assert cfg["server_url"] == ATLASSIAN_MCP_URL
        assert cfg["transport"] == "streamable_http"
    assert (got["a"] if isinstance(got["a"], dict) else json.loads(got["a"]))["auth_type"] == "dcr"
    cfg_c = got["c"] if isinstance(got["c"], dict) else json.loads(got["c"])
    assert cfg_c["server_url"] == "https://mcp.linear.app/mcp"
    assert (got["d"] if isinstance(got["d"], dict) else json.loads(got["d"])) == {"host": "db"}


# ── audience follows the server ───────────────────────────────────────────

class _Conn:
    def __init__(self, creds):
        self.id, self.type = "c1", "mcp"
        self.config = {"server_url": ATLASSIAN_MCP_URL}
        self._creds = dict(creds)

    def decrypt_credentials(self):
        return dict(self._creds)

    def encrypt_credentials(self, creds):
        self._creds = dict(creds)


class _DB:
    async def commit(self):
        pass

    async def refresh(self, obj):
        pass


def test_dcr_audience_is_updated_after_an_endpoint_move(monkeypatch):
    import app.services.mcp_dcr_service as dcr

    async def fake_discover(url):
        return {"scopes": "read:jira-work", "scopes_source": "resource_metadata", "resource": url,
                "registration_endpoint": "https://auth.example/reg"}

    monkeypatch.setattr(dcr, "discover_mcp_oauth", fake_discover)
    conn = _Conn({
        "authorize_url": "https://auth.atlassian.com/authorize",
        "token_url": "https://auth.atlassian.com/oauth/token",
        "client_id": "dyn", "dcr_registered": True,
        "audience": ATLASSIAN_LEGACY_SSE_URL,
        "discovered_scopes": "read:jira-work", "scopes_source": "resource_metadata",
    })
    registered = asyncio.run(dcr.ensure_mcp_oauth_config(_DB(), conn))
    assert registered is False  # the existing client is reused, not re-registered
    assert conn._creds["audience"] == ATLASSIAN_MCP_URL
    assert conn._creds["client_id"] == "dyn"
