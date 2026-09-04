"""Runtime connection lifecycle on the real tunnel (admin UI, design C4).

The admin-server tests use a fake tunnel; these drive the *actual*
EdgeAgentTunnel methods against a stub NATS client, so subscription tracking,
client-cache invalidation, and re-advertisement are covered directly rather
than only live in the rig.
"""

from __future__ import annotations

import json

import pytest

from ..config import AgentConfig, ConnectionConfig
from ..tunnel import EdgeAgentTunnel


class StubSub:
    def __init__(self, subject: str):
        self.subject = subject
        self.unsubscribed = False

    async def unsubscribe(self) -> None:
        self.unsubscribed = True


class StubNC:
    """Enough of a NATS client for subscribe / publish / flush."""

    def __init__(self):
        self.is_connected = True
        self.subs: list[StubSub] = []
        self.published: list[tuple[str, bytes]] = []

    async def subscribe(self, subject, cb=None):
        s = StubSub(subject)
        self.subs.append(s)
        return s

    async def publish(self, subject, payload):
        self.published.append((subject, payload))

    async def flush(self):
        pass


def _cfg(connections=None) -> AgentConfig:
    return AgentConfig(
        org_id="cust-b", edge_agent_id="nyc-01",
        connections=connections or [],
    )


def _last_advertised(nc: StubNC) -> list[str]:
    subj, payload = nc.published[-1]
    return [c["name"] for c in json.loads(payload)["connections"]]


@pytest.mark.asyncio
async def test_apply_new_connection_subscribes_and_advertises():
    t = EdgeAgentTunnel(_cfg())
    t._nc = StubNC()
    conn = ConnectionConfig(name="new-pg", type="postgresql", config={"host": "h"})

    await t.apply_connection(conn)

    assert t.get_connection("new-pg") is conn
    assert "new-pg" in t._conn_subs                 # subscribed
    assert t._conn_subs["new-pg"].subject.endswith(".conn.new-pg")
    assert _last_advertised(t._nc) == ["new-pg"]    # re-advertised


@pytest.mark.asyncio
async def test_apply_existing_replaces_without_new_sub_and_drops_client_cache():
    t = EdgeAgentTunnel(_cfg([{"name": "prod-pg", "type": "postgresql",
                               "config": {"host": "old"}}]))
    t._nc = StubNC()
    await t.subscribe()
    t._clients["prod-pg"] = object()                # pretend a pooled client
    subs_before = len(t._nc.subs)

    updated = ConnectionConfig(name="prod-pg", type="postgresql", config={"host": "new"})
    await t.apply_connection(updated)

    assert t.get_connection("prod-pg").config["host"] == "new"
    assert "prod-pg" not in t._clients              # stale client dropped
    assert len(t._nc.subs) == subs_before           # subject reused, no new sub
    assert _last_advertised(t._nc) == ["prod-pg"]


@pytest.mark.asyncio
async def test_remove_connection_unsubscribes_and_readvertises_without_it():
    t = EdgeAgentTunnel(_cfg([{"name": "prod-pg", "type": "postgresql"}]))
    t._nc = StubNC()
    await t.subscribe()
    sub = t._conn_subs["prod-pg"]
    t._clients["prod-pg"] = object()

    removed = await t.remove_connection("prod-pg")

    assert removed is True
    assert sub.unsubscribed is True
    assert t.get_connection("prod-pg") is None
    assert "prod-pg" not in t._conn_subs
    assert "prod-pg" not in t._clients
    assert _last_advertised(t._nc) == []            # withdrawn


@pytest.mark.asyncio
async def test_remove_missing_connection_returns_false():
    t = EdgeAgentTunnel(_cfg())
    t._nc = StubNC()
    assert await t.remove_connection("ghost") is False


@pytest.mark.asyncio
async def test_status_snapshot_reports_identity_and_nats():
    t = EdgeAgentTunnel(_cfg([{"name": "a", "type": "postgresql"}]))
    t._nc = StubNC()
    s = t.status_snapshot()
    assert s["edge_agent_id"] == "nyc-01"
    assert s["nats"] == {"connected": True, "url": t._config.tunnel_endpoint_url}
    assert s["connections_served"] == 1
    assert s["in_flight"] == 0


@pytest.mark.asyncio
async def test_status_snapshot_nats_disconnected_when_no_client():
    t = EdgeAgentTunnel(_cfg())
    assert t.status_snapshot()["nats"]["connected"] is False


@pytest.mark.asyncio
async def test_test_connection_config_swallows_construct_errors():
    # An unresolvable type must come back as a failure result, not raise — the
    # admin UI's Test button relies on always getting {success, message}.
    t = EdgeAgentTunnel(_cfg())
    conn = ConnectionConfig(name="x", type="totally_unknown_type", config={})
    r = await t.test_connection_config(conn)
    assert r["success"] is False and isinstance(r["message"], str)


@pytest.mark.asyncio
async def test_apply_without_nats_persists_but_does_not_subscribe():
    # Edits made while the broker is down still update the served set; the
    # subscription is deferred (guarded on self._nc), advertise is a no-op.
    t = EdgeAgentTunnel(_cfg())
    conn = ConnectionConfig(name="offline-pg", type="postgresql")
    await t.apply_connection(conn)
    assert t.get_connection("offline-pg") is conn
    assert "offline-pg" not in t._conn_subs
