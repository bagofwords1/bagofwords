"""Request dispatch: budgets, cancellation, and error codes (A5/A9/C3).

Drives `_handle_request` with a stub NATS message and a fake client installed
in the tunnel's cache, so the contract the control plane relies on — a cancel
naming the envelope id stops the operation, the per-connection budget is what
is enforced, an unknown connection is its own error — is checked without a
database or a broker.
"""

from __future__ import annotations

import asyncio
import json

import pandas as pd
import pytest

from ..config import AgentConfig
from ..tunnel import EdgeAgentTunnel, _CANCELLED, _NOT_IMPLEMENTED, _UNKNOWN_CONNECTION


class StubMsg:
    def __init__(self, data: bytes, subject: str = "s", reply: str = "_INBOX.x"):
        self.data = data
        self.subject = subject
        self.reply = reply
        self.responses: list[bytes] = []

    async def respond(self, payload: bytes) -> None:
        self.responses.append(payload)


class FakeClient:
    """A client whose execute_query blocks for `delay` seconds.

    `_on_connect` is accepted so the source-side cancel hook is exercised:
    `cancelled` records whether the statement cancel was invoked.
    """

    catalog_identity_available = True

    def __init__(self, delay: float = 0.0):
        self.delay = delay
        self.cancelled = False
        self.calls: list[str] = []

    def execute_query(self, sql, _on_connect=None, **kwargs):
        self.calls.append(sql)
        if _on_connect is not None:
            _on_connect(self)
        # Blocking sleep: runs in asyncio.to_thread exactly like a real driver.
        import time
        deadline = time.monotonic() + self.delay
        while time.monotonic() < deadline:
            if self.cancelled:
                raise RuntimeError("statement cancelled")
            time.sleep(0.01)
        return pd.DataFrame({"n": [1]})

    def cancel(self):
        self.cancelled = True

    async def atest_connection(self):
        return {"success": True}


def _tunnel(client: FakeClient, **conn_overrides) -> EdgeAgentTunnel:
    conn = {"name": "pg", "type": "postgresql", "config": {"host": "h"}, **conn_overrides}
    cfg = AgentConfig(org_id="cust-b", edge_agent_id="nyc-01",
                      default_query_timeout_seconds=300, connections=[conn])
    t = EdgeAgentTunnel(cfg)
    t._clients["pg"] = client  # bypass construct_client; the cache is the seam
    return t


def _req(operation, request_id="req-1", **kwargs):
    return json.dumps({
        "jsonrpc": "2.0", "id": request_id,
        "params": {"operation": operation, "kwargs": kwargs},
    }).encode()


def _cancel(ref_id):
    return json.dumps({"method": "cancel", "params": {"ref_id": ref_id}}).encode()


def _reply(msg: StubMsg) -> dict:
    assert msg.responses, "handler did not reply"
    return json.loads(msg.responses[-1])


@pytest.mark.asyncio
async def test_in_flight_is_keyed_by_the_envelope_id_while_the_operation_runs():
    client = FakeClient(delay=5)
    t = _tunnel(client)
    msg = StubMsg(_req("execute_query", request_id="abc-123", sql="select 1"))

    task = asyncio.create_task(t._handle_request(msg, "pg"))
    await asyncio.sleep(0.1)
    assert "abc-123" in t._in_flight
    assert t.status_snapshot()["in_flight"] == 1

    await t._handle_control(StubMsg(_cancel("abc-123")))
    await asyncio.wait_for(task, 2)

    assert client.cancelled                      # statement stopped on the source
    assert "abc-123" not in t._in_flight         # registry cleaned up
    assert _reply(msg)["error"]["code"] == _CANCELLED


@pytest.mark.asyncio
async def test_cancel_for_an_unknown_id_does_not_touch_other_requests():
    client = FakeClient(delay=0.3)
    t = _tunnel(client)
    msg = StubMsg(_req("execute_query", request_id="keep", sql="select 1"))

    task = asyncio.create_task(t._handle_request(msg, "pg"))
    await asyncio.sleep(0.05)
    await t._handle_control(StubMsg(_cancel("someone-else")))
    # Generous: the reply path serializes to parquet, and the first pyarrow
    # import in a fresh environment can take seconds.
    await asyncio.wait_for(task, 15)

    assert not client.cancelled
    assert "result" in _reply(msg)


@pytest.mark.asyncio
async def test_per_connection_query_timeout_is_enforced_not_just_advertised():
    # The connection's own budget is well under the agent default; a query
    # that would fit the default must still be cut off at the connection's.
    client = FakeClient(delay=5)
    t = _tunnel(client, query_timeout_seconds=1)
    assert t.build_advertisement()["connections"][0]["query_timeout_seconds"] == 1

    msg = StubMsg(_req("execute_query", sql="select pg_sleep(5)"))
    started = asyncio.get_running_loop().time()
    await asyncio.wait_for(t._handle_request(msg, "pg"), 4)
    elapsed = asyncio.get_running_loop().time() - started

    reply = _reply(msg)
    assert reply["error"]["data"]["kind"] == "query_timeout"
    assert reply["error"]["data"]["timeout_s"] == 1
    assert elapsed < 3
    assert client.cancelled


@pytest.mark.asyncio
async def test_unknown_connection_is_distinct_from_method_not_found():
    t = _tunnel(FakeClient())

    unknown = StubMsg(_req("execute_query", sql="select 1"))
    await t._handle_request(unknown, "ghost")
    unimplemented = StubMsg(_req("no_such_operation"))
    await t._handle_request(unimplemented, "pg")

    assert _reply(unknown)["error"]["code"] == _UNKNOWN_CONNECTION
    assert _reply(unknown)["error"]["data"]["connection"] == "ghost"
    assert _reply(unimplemented)["error"]["code"] == _NOT_IMPLEMENTED
    assert _reply(unknown)["error"]["code"] != _reply(unimplemented)["error"]["code"]


def test_advertisement_carries_the_clients_catalog_identity_flag():
    client = FakeClient()
    client.catalog_identity_available = False
    t = _tunnel(client)

    conn = t.build_advertisement()["connections"][0]
    assert conn["catalog_identity_available"] is False
