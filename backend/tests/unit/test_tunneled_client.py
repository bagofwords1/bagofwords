"""TunnelClient.invoke + TunneledClient — the query transport (design B1/B3).

No broker: a stub NATS client / stub tunnel exercise payload shaping, the
ownership guard, error translation, parquet round-trips, and the proxy surface.
"""
import base64
import io
import json

import pandas as pd
import pytest

from app.services.tunnel_client import TunnelClient, _ENVELOPE_HEADROOM
from app.services.tunnel_errors import (
    RemoteError,
    TunnelOwnershipError,
    TunnelPayloadTooLarge,
)


class _Conn:
    """Minimal Connection stand-in."""

    def __init__(self, tunnel_mode=True, config=None):
        self.name = "lego-pg"
        self.type = "postgresql"
        self.organization_id = "org-1"
        self.edge_agent_id = "nyc-01"
        self.tunnel_mode = tunnel_mode
        self.config = config or {}
        self.auth_policy = "system_only"


class _Msg:
    def __init__(self, data: bytes):
        self.data = data


class _StubNC:
    """Enough of a NATS client for TunnelClient.invoke."""

    def __init__(self, reply: dict, max_payload=1 << 20):
        self.max_payload = max_payload
        self.requests = []
        self._reply = reply

    async def request(self, subject, payload, timeout=None):
        self.requests.append((subject, payload, timeout))
        return _Msg(json.dumps(self._reply).encode())


def _tunnel_with(reply, max_payload=1 << 20):
    tc = TunnelClient()
    tc._nc = _StubNC(reply, max_payload)
    return tc


# ── TunnelClient.invoke ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_invoke_builds_subject_and_payload():
    reply = {"edge_agent_id": "nyc-01", "result": {"ok": True}}
    tc = _tunnel_with(reply)
    conn = _Conn()

    result = await tc.invoke(conn, "test_connection", {}, timeout=30)

    assert result == {"ok": True}
    subject, payload, timeout = tc._nc.requests[0]
    assert subject == "tunnel.org-1.nyc-01.conn.lego-pg"
    assert timeout == 35  # timeout + 5 backstop
    body = json.loads(payload)
    assert body["params"]["operation"] == "test_connection"
    assert body["params"]["connection_name"] == "lego-pg"
    assert body["params"]["timeout_ms"] == 30000


@pytest.mark.asyncio
async def test_invoke_ownership_guard():
    reply = {"edge_agent_id": "tokyo-02", "result": {}}  # wrong agent answered
    tc = _tunnel_with(reply)
    with pytest.raises(TunnelOwnershipError):
        await tc.invoke(_Conn(), "test_connection", {}, timeout=30)


@pytest.mark.asyncio
async def test_invoke_translates_remote_error():
    reply = {"edge_agent_id": "nyc-01",
             "error": {"code": -32000, "message": "boom"}}
    tc = _tunnel_with(reply)
    with pytest.raises(RemoteError) as ei:
        await tc.invoke(_Conn(), "execute_query", {"sql": "x"}, timeout=30)
    assert "boom" in str(ei.value)
    assert ei.value.operation == "execute_query"


@pytest.mark.asyncio
async def test_invoke_unwraps_dataframe():
    df = pd.DataFrame({"n": [1, 2, 3]})
    buf = io.BytesIO()
    df.to_parquet(buf, index=False)
    reply = {"edge_agent_id": "nyc-01",
             "result": {"dataframe_b64": base64.b64encode(buf.getvalue()).decode()}}
    tc = _tunnel_with(reply)

    out = await tc.invoke(_Conn(), "execute_query", {"sql": "x"}, timeout=30)
    assert list(out["n"]) == [1, 2, 3]


@pytest.mark.asyncio
async def test_invoke_rejects_oversized_request():
    reply = {"edge_agent_id": "nyc-01", "result": {}}
    # tiny ceiling so any real payload exceeds it
    tc = _tunnel_with(reply, max_payload=_ENVELOPE_HEADROOM + 10)
    with pytest.raises(TunnelPayloadTooLarge):
        await tc.invoke(_Conn(), "execute_query", {"sql": "select 1"}, timeout=30)


# ── TunneledClient proxy ─────────────────────────────────────────────────────


class _StubTunnel:
    """Stub with the invoke / invoke_streaming surface TunneledClient uses,
    plus a _loop so the loop-bridge takes the direct-await path."""

    def __init__(self, result):
        import asyncio
        self._loop = asyncio.get_event_loop()
        self._result = result
        self.calls = []

    async def invoke(self, connection, operation, kwargs, *, timeout=60.0,
                     user_credentials=None):
        self.calls.append((operation, kwargs))
        return self._result

    async def invoke_streaming(self, connection, operation, kwargs, *, timeout,
                               progress_callback=None, cancel_check=None,
                               user_credentials=None):
        self.calls.append((operation, kwargs))
        return self._result


def _tunneled(result, config=None):
    from app.data_sources.clients.tunneled_client import TunneledClient
    from app.data_sources.clients.postgresql_client import PostgresqlClient
    import asyncio
    tunnel = _StubTunnel(result)
    tunnel._loop = asyncio.get_running_loop()
    return TunneledClient(_Conn(config=config), PostgresqlClient, tunnel)


@pytest.mark.asyncio
async def test_proxy_is_instantiable_and_holds_no_creds():
    c = _tunneled({"value": [], "index_stats": {}})
    assert c._user_credentials is None
    assert "lego-pg" in c.description
    assert c.catalog_identity_available is True


@pytest.mark.asyncio
async def test_proxy_aget_schemas_returns_tables_and_stats():
    reply = {
        "value": [
            {"name": "public.lego_sets",
             "columns": [{"name": "set_num", "dtype": "text"}],
             "pks": [{"name": "set_num", "dtype": "text"}], "fks": []},
        ],
        "index_stats": {"table_count": 1},
    }
    c = _tunneled(reply)
    tables = await c.aget_schemas()
    assert len(tables) == 1
    assert tables[0].name == "public.lego_sets"
    assert c.index_stats() == {"table_count": 1}


@pytest.mark.asyncio
async def test_proxy_aexecute_query_returns_dataframe():
    df = pd.DataFrame({"c": [7]})
    buf = io.BytesIO()
    df.to_parquet(buf, index=False)
    # invoke returns the already-unwrapped dataframe (TunnelClient._unwrap does
    # that); the stub tunnel returns the dataframe directly.
    c = _tunneled(df)
    out = await c.aexecute_query("select count(*) c from lego_sets")
    assert list(out["c"]) == [7]
    assert c._tunnel.calls[0][0] == "execute_query"
