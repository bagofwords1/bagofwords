"""Phase 1 transport tests.

These exercise the message handling without a broker: a real NATS server would
verify connect/subscribe, but everything Phase 1 actually decides — parsing,
logging, redaction, and answering rather than hanging — happens in the handler.
"""

from __future__ import annotations

import json

import pytest

from ..config import AgentConfig
from ..tunnel import EdgeAgentTunnel


class StubMsg:
    """Enough of nats.aio.msg.Msg for the handler paths."""

    def __init__(self, data: bytes, subject: str = "s", reply: str = "_INBOX.x"):
        self.data = data
        self.subject = subject
        self.reply = reply
        self.responses: list[bytes] = []

    async def respond(self, payload: bytes) -> None:
        self.responses.append(payload)


@pytest.fixture
def config() -> AgentConfig:
    return AgentConfig(
        org_id="cust-b",
        edge_agent_id="nyc-01",
        connections=[{"name": "prod-pg", "type": "postgresql"}],
    )


@pytest.fixture
def tunnel(config: AgentConfig) -> EdgeAgentTunnel:
    return EdgeAgentTunnel(config)


def _request(operation: str = "execute_query", **params) -> bytes:
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "id": "q_1",
            "method": "invoke",
            "params": {"connection_name": "prod-pg", "operation": operation, **params},
        }
    ).encode()


@pytest.mark.asyncio
async def test_request_gets_a_reply_not_silence(tunnel):
    """A caller using nc.request() must get an error, not a timeout.

    Silence here is the worst outcome: the worker waits out its full budget and
    reports a transport failure for what is really "not built yet".
    """
    msg = StubMsg(_request())
    await tunnel._handle_request(msg, "prod-pg")

    assert len(msg.responses) == 1
    body = json.loads(msg.responses[0])
    assert body["id"] == "q_1"
    assert body["edge_agent_id"] == "nyc-01"
    assert body["error"]["code"] == -32601
    assert body["error"]["data"]["operation"] == "execute_query"


@pytest.mark.asyncio
async def test_malformed_request_still_answers(tunnel):
    """A bad envelope must not escape the callback.

    An exception inside a NATS callback is swallowed by the client library, so
    the caller would learn nothing until its own timeout fired.
    """
    msg = StubMsg(b"{not json")
    await tunnel._handle_request(msg, "prod-pg")

    assert len(msg.responses) == 1
    body = json.loads(msg.responses[0])
    assert body["id"] is None
    assert body["error"]["code"] == -32601


@pytest.mark.asyncio
async def test_fire_and_forget_request_is_not_answered(tunnel):
    """No reply subject means nothing to answer."""
    msg = StubMsg(_request(), reply="")
    await tunnel._handle_request(msg, "prod-pg")
    assert msg.responses == []


@pytest.mark.asyncio
async def test_user_credentials_never_reach_a_log(tunnel, caplog):
    """Per-user credentials cross the tunnel; they must not cross into logs.

    Assert against the LogRecord attribute, not caplog.text. `extra` fields do
    not appear in the rendered message under the default format, so a text
    assertion passes whether or not redaction happened — and the secret would
    still be sitting on the record for any handler that formats `extra`, which
    is exactly what a JSON log shipper does.
    """
    import logging

    caplog.set_level(logging.DEBUG)
    msg = StubMsg(_request(user_credentials={"access_token": "eyJ-SECRET"}))
    await tunnel._handle_request(msg, "prod-pg")

    bodies = [r for r in caplog.records if r.getMessage() == "edge_agent.request.body"]
    assert bodies, "the request body was never logged, so nothing was verified"
    params = bodies[-1].params

    assert params["user_credentials"] == "<redacted>"
    assert "eyJ-SECRET" not in repr(params)


@pytest.mark.asyncio
async def test_control_message_is_logged(tunnel, caplog):
    import logging

    caplog.set_level(logging.INFO)
    msg = StubMsg(json.dumps({"method": "cancel", "params": {"ref_id": "q_1"}}).encode())
    await tunnel._handle_control(msg)

    assert "edge_agent.control.received" in caplog.text


def test_subjects_match_the_design(config):
    assert config.connection_subject("prod-pg") == "tunnel.cust-b.nyc-01.conn.prod-pg"
    assert config.control_subject == "tunnel.cust-b.nyc-01.control"
    assert config.advertisement_subject == "tunnel.cust-b.advertisements"


def test_subscribe_before_connect_is_an_error(tunnel):
    import asyncio

    with pytest.raises(RuntimeError):
        asyncio.run(tunnel.subscribe())


class StubNC:
    """Enough of a NATS client to observe what advertise() publishes."""

    def __init__(self):
        self.published: list[tuple[str, bytes]] = []
        self.is_connected = True

    async def publish(self, subject: str, payload: bytes) -> None:
        self.published.append((subject, payload))

    async def flush(self) -> None:
        pass


def test_advertisement_payload_omits_org_id_and_credentials(config):
    # The payload must never carry org_id (tenancy is the subject, A10) nor any
    # credential — Bow builds Connection rows from this, and leaking either is
    # the whole failure this agent exists to prevent.
    config.connections[0].credentials = {"user": "bow", "password": "secret"}
    tunnel = EdgeAgentTunnel(config)

    ad = tunnel.build_advertisement()

    assert "org_id" not in ad
    assert ad["edge_agent_id"] == "nyc-01"
    assert [c["name"] for c in ad["connections"]] == ["prod-pg"]
    blob = json.dumps(ad)
    assert "secret" not in blob and "password" not in blob


@pytest.mark.asyncio
async def test_advertise_publishes_to_the_advertisement_subject(tunnel):
    tunnel._nc = StubNC()

    await tunnel.advertise()

    assert len(tunnel._nc.published) == 1
    subject, payload = tunnel._nc.published[0]
    assert subject == "tunnel.cust-b.advertisements"
    assert json.loads(payload)["edge_agent_id"] == "nyc-01"


@pytest.mark.asyncio
async def test_advertise_skips_when_not_connected(tunnel, caplog):
    import logging

    caplog.set_level(logging.WARNING)
    tunnel._nc = None  # never connected

    await tunnel.advertise()  # must not raise

    assert "edge_agent.advertise.skipped" in caplog.text


@pytest.mark.asyncio
async def test_reconnect_re_advertises(tunnel):
    # A10: on reconnect the agent re-advertises immediately rather than waiting
    # for the next timer tick.
    tunnel._nc = StubNC()

    await tunnel._on_reconnected()

    assert len(tunnel._nc.published) == 1
    assert tunnel._nc.published[0][0] == "tunnel.cust-b.advertisements"
