"""TunnelClient — control-plane NATS transport (design B3/B4).

No broker: these pin what the advertisement handler decides — reading org from
the subject not the payload, tolerating malformed input, and logging every
advertised data source — plus the module accessor's contract. A real NATS server
would additionally verify connect/subscribe, which is exercised in-cluster.
"""
import json
import logging
from contextlib import contextmanager

import pytest

from app.services.tunnel_client import (
    TunnelClient,
    get_tunnel_client,
    set_tunnel_client,
)


@contextmanager
def capture_logs():
    """Collect records straight off the module logger.

    The app disables propagation on its loggers, so the built-in `caplog`
    fixture (which listens on the root logger) sees nothing. Attaching a handler
    to `app.services.tunnel_client` directly captures its records regardless of
    the global logging config.
    """
    logger = logging.getLogger("app.services.tunnel_client")
    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record):
            records.append(record)

    handler = _Capture()
    prev_level = logger.level
    # The app configures logging with disable_existing_loggers, which flips
    # logger.disabled True on any logger that already existed — so records never
    # reach a handler. Clear it (and the global logging.disable threshold) for
    # the capture, then restore both.
    prev_disabled = logger.disabled
    prev_disable = logging.root.manager.disable
    logger.disabled = False
    logging.disable(logging.NOTSET)
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        yield records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(prev_level)
        logger.disabled = prev_disabled
        logging.disable(prev_disable)


def _ad(**overrides) -> bytes:
    payload = {
        "edge_agent_id": "nyc-01",
        "edge_agent_name": "NYC Office",
        "version": "1.0.0",
        "connections": [
            {"name": "lego-pg", "type": "postgresql", "label": "LEGO sample"},
        ],
    }
    payload.update(overrides)
    return json.dumps(payload).encode()


class StubMsg:
    """Enough of nats.aio.msg.Msg for the advertisement handler."""

    def __init__(self, data: bytes, subject: str = "tunnel.cust-b.advertisements"):
        self.data = data
        self.subject = subject


@pytest.mark.asyncio
async def test_advertisement_logs_org_from_subject_and_each_data_source():
    tunnel = TunnelClient()

    with capture_logs() as records:
        await tunnel._on_advertisement(StubMsg(_ad()))

    received = [r for r in records if r.msg == "tunnel.advertisement.received"]
    sources = [r for r in records if r.msg == "tunnel.advertisement.data_source"]
    assert len(received) == 1
    # org comes from the subject token, never the payload (A10).
    assert received[0].org_id == "cust-b"
    assert received[0].edge_agent_id == "nyc-01"
    assert received[0].connection_count == 1
    assert len(sources) == 1
    assert sources[0].connection_name == "lego-pg"
    assert sources[0].connection_type == "postgresql"


@pytest.mark.asyncio
async def test_org_is_read_from_subject_not_payload():
    # A forged org_id field in the payload must be ignored: tenancy is the
    # subject the broker attested, not a JSON key anyone could set.
    tunnel = TunnelClient()

    msg = StubMsg(_ad(org_id="cust-EVIL"), subject="tunnel.cust-b.advertisements")
    with capture_logs() as records:
        await tunnel._on_advertisement(msg)

    received = [r for r in records if r.msg == "tunnel.advertisement.received"]
    assert received[0].org_id == "cust-b"


@pytest.mark.asyncio
async def test_malformed_payload_is_logged_not_raised():
    tunnel = TunnelClient()

    with capture_logs() as records:
        await tunnel._on_advertisement(StubMsg(b"not json"))  # must not raise

    assert any(r.msg == "tunnel.advertisement.unparseable" for r in records)


@pytest.mark.asyncio
async def test_advertisement_with_no_connections_logs_the_header_only():
    tunnel = TunnelClient()

    with capture_logs() as records:
        await tunnel._on_advertisement(StubMsg(_ad(connections=[])))

    received = [r for r in records if r.msg == "tunnel.advertisement.received"]
    sources = [r for r in records if r.msg == "tunnel.advertisement.data_source"]
    assert received[0].connection_count == 0
    assert sources == []


@pytest.mark.asyncio
async def test_data_source_log_uses_non_reserved_keys():
    # Regression: extra={"name": ...} shadows a reserved LogRecord attribute and
    # raises inside the NATS callback. The keys must be the prefixed variants.
    tunnel = TunnelClient()
    # Would raise KeyError if the handler passed a reserved key like "name".
    await tunnel._on_advertisement(StubMsg(_ad()))


@pytest.mark.asyncio
async def test_drain_is_safe_when_never_connected():
    # Shutdown may run before connect() ever succeeded; drain must be a no-op.
    tunnel = TunnelClient()
    assert tunnel.is_connected is False
    await tunnel.drain()  # must not raise


@pytest.mark.asyncio
async def test_start_listener_before_connect_is_an_error():
    tunnel = TunnelClient()
    with pytest.raises(RuntimeError):
        await tunnel.start_advertisement_listener()


def test_module_accessor_roundtrips():
    # Construction sites reach the per-worker client through the accessor; it is
    # None before startup wires one in (design B4).
    set_tunnel_client(None)
    assert get_tunnel_client() is None
    client = TunnelClient()
    set_tunnel_client(client)
    try:
        assert get_tunnel_client() is client
    finally:
        set_tunnel_client(None)
