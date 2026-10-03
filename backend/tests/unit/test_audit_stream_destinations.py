"""Audit stream destination contracts (docs/design/audit-log-streams.md, Loop A2).

Every destination is exercised against the mock SIEM consumer, which checks
each vendor intake's wire format and auth. The external boundary is the only
thing mocked; the senders run for real (httpx, boto3, asyncio TLS sockets).
"""
from __future__ import annotations

import asyncio
import time
import uuid
from datetime import datetime, timedelta

import pytest

from app.ee.audit.streams.destinations import REGISTRY, build_destination
from app.ee.audit.streams.destinations.base import FATAL, INVALID, OK, RETRYABLE
from app.ee.audit.streams.destinations.http import HttpsDestination
from app.ee.audit.streams.envelope import iso_utc
from tests.mocks.siem_consumer import MockSiem


@pytest.fixture(scope="module")
def mock():
    m = MockSiem(port=0, syslog_port=0).start()
    yield m
    m.stop()


@pytest.fixture(autouse=True)
def _clean(mock):
    mock.state.faults.clear()
    mock.state.reset()
    mock.state.config.update({"external_id": None})
    yield


def _events(n: int, action: str = "report.updated") -> list[dict]:
    t0 = datetime(2026, 10, 3, 12, 0, 0)
    return [
        {
            "id": str(uuid.uuid4()), "version": 1, "action": action,
            "occurred_at": iso_utc(t0 + timedelta(milliseconds=i)),
            "organization": {"id": "org-1", "name": "Acme"},
            "actor": {"type": "user", "id": "u1", "email": "a@acme.com"},
            "targets": [{"type": "report", "id": f"r{i}"}],
            "context": {"ip_address": "203.0.113.4", "user_agent": "UA"},
            "metadata": {"i": i},
        }
        for i in range(n)
    ]


def _cfg(mock, dest: str):
    u = mock.url
    return {
        "datadog": ({"site": "datadoghq.com", "intake_url": f"{u}/dd/api/v2/logs"}, {"api_key": "demo-dd-key"}),
        "splunk": ({"hec_url": f"{u}/splunk", "index": "audit"}, {"token": "demo-hec-token"}),
        "sentinel": ({"tenant_id": "t1", "client_id": "demo-client", "dce_url": f"{u}/sentinel",
                      "dcr_immutable_id": "dcr-1", "stream_name": "Custom-BagOfWordsAudit_CL",
                      "authority_url": f"{u}/entra"}, {"client_secret": "demo-secret"}),
        "s3": ({"bucket": "audit", "region": "us-east-1", "prefix": "bow/audit", "endpoint_url": f"{u}/s3"},
               {"access_key_id": "demo-access-key", "secret_access_key": "x"}),
        "gcs": ({"bucket": "gcs-audit", "prefix": "bow", "endpoint_url": f"{u}/s3", "gzip": True},
                {"access_key_id": "demo-access-key", "secret_access_key": "x"}),
        "https": ({"url": f"{u}/https/hook"}, {"hmac_secret": "demo-hmac-secret"}),
        "syslog": ({"host": "127.0.0.1", "port": mock.syslog_port, "tls": True, "ca_cert": mock.ca_pem}, {}),
    }[dest]


_BAD_SECRETS = {
    "datadog": {"api_key": "wrong"},
    "splunk": {"token": "wrong"},
    "sentinel": {"client_secret": "wrong"},
    "s3": {"access_key_id": "AKIAWRONG", "secret_access_key": "x"},
    "gcs": {"access_key_id": "GOOGWRONG", "secret_access_key": "x"},
    "https": {"hmac_secret": "wrong"},
}


async def _wait_for(pred, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        await asyncio.sleep(0.02)
    return pred()


def test_registry_covers_every_planned_destination():
    assert set(REGISTRY) == {"datadog", "splunk", "sentinel", "s3", "gcs", "https", "syslog"}


@pytest.mark.asyncio
@pytest.mark.parametrize("dest", ["datadog", "splunk", "sentinel", "s3", "gcs", "https", "syslog"])
@pytest.mark.parametrize("n", [1, 7])
async def test_batch_is_delivered_in_the_intakes_format(mock, dest, n):
    config, secrets = _cfg(mock, dest)
    events = _events(n)
    res = await build_destination(dest, config, secrets).send(events)
    assert res.kind == OK, res.error
    want = [e["id"] for e in events]
    assert await _wait_for(lambda: len(mock.state.envelopes(dest)) >= n)
    stats = mock.state.stats(dest, want)
    assert stats["missing"] == [] and stats["unexpected"] == []
    assert stats["format_errors"] == 0 and stats["auth_failures"] == 0
    assert stats["duplicates"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("dest", sorted(_BAD_SECRETS))
async def test_rejected_credentials_are_invalid_not_retried(mock, dest):
    config, _ = _cfg(mock, dest)
    res = await build_destination(dest, config, _BAD_SECRETS[dest]).send(_events(2))
    assert res.kind == INVALID, (res.kind, res.error)
    assert mock.state.envelopes(dest) == []


_HTTP_DESTS = ["datadog", "splunk", "sentinel", "s3", "https"]


@pytest.mark.asyncio
@pytest.mark.parametrize("dest", _HTTP_DESTS)
@pytest.mark.parametrize("mode,kind", [
    ("503", RETRYABLE), ("429", RETRYABLE), ("500", RETRYABLE), ("reset", RETRYABLE),
    ("401", INVALID), ("403", INVALID), ("400", FATAL),
])
async def test_failure_classification(mock, dest, mode, kind):
    config, secrets = _cfg(mock, dest)
    d = build_destination(dest, config, secrets)
    mock.state.faults[dest] = {"mode": mode, "count": 1, "sleep": None}
    res = await d.send(_events(2))
    assert res.kind == kind, (res.kind, res.error)
    # The fault was one-shot: the same destination recovers on the next send.
    assert (await d.send(_events(2))).kind == OK


@pytest.mark.asyncio
async def test_s3_retry_of_the_same_batch_overwrites_one_object(mock):
    config, secrets = _cfg(mock, "s3")
    events = _events(5)
    d = build_destination("s3", config, secrets)
    assert (await d.send(events)).ok
    assert (await d.send(events)).ok
    assert len([k for k in mock.state.s3_objects if k[0] == "s3"]) == 1
    assert mock.state.stats("s3")["duplicates"] == 0


@pytest.mark.asyncio
async def test_s3_assume_role_requires_the_external_id(mock):
    config, secrets = _cfg(mock, "s3")
    mock.state.config["external_id"] = "ext-123"
    role = {**config, "role_arn": "arn:aws:iam::123456789012:role/bow-audit", "sts_endpoint_url": f"{mock.url}/sts"}
    ok = await build_destination("s3", {**role, "external_id": "ext-123"}, secrets).send(_events(1))
    assert ok.kind == OK, ok.error
    bad = await build_destination("s3", {**role, "external_id": "ext-999"}, secrets).send(_events(1))
    assert bad.kind == INVALID


@pytest.mark.asyncio
async def test_syslog_cef_format_round_trips_ids(mock):
    config, secrets = _cfg(mock, "syslog")
    events = _events(3, action="tool.data_queried")
    res = await build_destination("syslog", {**config, "format": "cef"}, secrets).send(events)
    assert res.ok, res.error
    assert await _wait_for(lambda: len(mock.state.envelopes("syslog")) >= 3)
    got = mock.state.envelopes("syslog")
    assert [e["id"] for e in got] == [e["id"] for e in events]
    assert all(e["action"] == "tool.data_queried" for e in got)


@pytest.mark.asyncio
async def test_syslog_rejects_an_untrusted_server_certificate(mock):
    config, secrets = _cfg(mock, "syslog")
    res = await build_destination("syslog", {**config, "ca_cert": None}, secrets).send(_events(1))
    assert res.kind == INVALID


@pytest.mark.asyncio
async def test_syslog_unreachable_collector_is_retryable():
    res = await build_destination("syslog", {"host": "127.0.0.1", "port": 1, "tls": False}, {}).send(_events(1))
    assert res.kind == RETRYABLE


def test_https_signature_is_hmac_sha256_over_timestamp_and_body():
    import hashlib
    import hmac

    body = b'[{"id":"x"}]'
    sig = HttpsDestination.signature("s3cret", "1700000000", body)
    assert sig == "v1=" + hmac.new(b"s3cret", b"1700000000." + body, hashlib.sha256).hexdigest()
