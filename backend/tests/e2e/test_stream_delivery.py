"""E2E contract tests for keeping live chat streams unbuffered, and for the
browser's delivery report when they are buffered anyway.

Contract:
  - Live completion streams tell every hop not to transform or compress them
    (Cache-Control: no-transform, Content-Encoding: identity) and nginx not to
    buffer them (X-Accel-Buffering: no). A compressed event stream is what
    makes content-inspecting proxies hold the whole answer back.
  - POST .../stream-diagnostics accepts the browser's measurement, answers
    whether it shows buffering, and logs buffered deliveries as a warning.
    It is gated like reading the conversation (owner-only) and rejects
    header values that could forge log lines.
"""
import logging
import uuid

import pytest

from tests.e2e.test_completion_watch_stream import _seed_terminal_completion


def _headers(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _owner_report(create_user, login_user, whoami, create_report):
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    report = create_report(title="Stream delivery", user_token=token, org_id=org_id, data_sources=[])
    return token, org_id, report


def _capture_delivery_log(monkeypatch, caplog):
    """The app's logging setup disables pre-existing loggers; attach caplog
    directly (same approach as test_artifact_resources)."""
    delivery_logger = logging.getLogger("app.streaming.delivery")
    monkeypatch.setattr(delivery_logger, "disabled", False)
    monkeypatch.setattr(delivery_logger, "handlers", [caplog.handler])
    monkeypatch.setattr(delivery_logger, "propagate", False)
    caplog.set_level(logging.INFO, logger=delivery_logger.name)
    delivery_logger.warning("capture is active")
    assert "capture is active" in caplog.text
    caplog.clear()


def _delivery(**overrides):
    body = {
        "kind": "kickoff",
        "holdback_ms": 9_800,
        "server_span_ms": 10_000,
        "token_events": 600,
        "network_reads": 1,
        "content_encoding": "gzip",
        "via": "1.1 corp-gateway",
    }
    body.update(overrides)
    return body


@pytest.mark.e2e
def test_live_completion_stream_opts_out_of_buffering_and_compression(
    test_client, create_user, login_user, whoami, create_report
):
    token, org_id, report = _owner_report(create_user, login_user, whoami, create_report)
    completion_id, _ = _seed_terminal_completion(report["id"])

    with test_client.stream(
        "GET",
        f"/api/reports/{report['id']}/completions/{completion_id}/stream",
        headers={**_headers(token, org_id), "Accept": "text/event-stream", "Accept-Encoding": "gzip, br, zstd"},
    ) as resp:
        assert resp.status_code == 200
        cache_control = [d.strip().lower() for d in resp.headers.get("cache-control", "").split(",")]
        assert "no-transform" in cache_control
        assert "no-cache" in cache_control
        assert resp.headers.get("content-encoding", "").lower() == "identity"
        assert resp.headers.get("x-accel-buffering", "").lower() == "no"
        body = resp.read().decode()
    assert "[DONE]" in body, "the identity-encoded stream must still be readable to the end"


@pytest.mark.e2e
@pytest.mark.parametrize("holdback_ms,span_ms", [(9_800, 10_000), (3_200, 9_600), (30_000, 31_000)])
def test_buffered_delivery_is_flagged_and_logged(
    holdback_ms, span_ms, test_client, create_user, login_user, whoami, create_report, caplog, monkeypatch
):
    token, org_id, report = _owner_report(create_user, login_user, whoami, create_report)
    completion_id = str(uuid.uuid4())
    _capture_delivery_log(monkeypatch, caplog)

    resp = test_client.post(
        f"/api/reports/{report['id']}/completions/{completion_id}/stream-diagnostics",
        json=_delivery(holdback_ms=holdback_ms, server_span_ms=span_ms),
        headers=_headers(token, org_id),
    )
    assert resp.status_code == 200, resp.json()
    assert resp.json()["buffered"] is True
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING and "sse.delivery_buffered" in r.getMessage()]
    assert len(warnings) == 1
    assert completion_id in warnings[0].getMessage()


@pytest.mark.e2e
@pytest.mark.parametrize("holdback_ms,span_ms", [(20, 10_000), (1_400, 3_000), (2_000, 30_000)])
def test_live_delivery_is_not_flagged(
    holdback_ms, span_ms, test_client, create_user, login_user, whoami, create_report, caplog, monkeypatch
):
    token, org_id, report = _owner_report(create_user, login_user, whoami, create_report)
    _capture_delivery_log(monkeypatch, caplog)

    resp = test_client.post(
        f"/api/reports/{report['id']}/completions/{uuid.uuid4()}/stream-diagnostics",
        json=_delivery(holdback_ms=holdback_ms, server_span_ms=span_ms, network_reads=500, content_encoding=None),
        headers=_headers(token, org_id),
    )
    assert resp.status_code == 200
    assert resp.json()["buffered"] is False
    assert not [r for r in caplog.records if "sse.delivery_buffered" in r.getMessage()]


@pytest.mark.e2e
@pytest.mark.parametrize("field,value", [
    ("via", "1.1 gw\r\nsse.delivery_buffered forged"),
    ("content_encoding", "gzip\n"),
    ("holdback_ms", -1),
    ("kind", "websocket"),
])
def test_delivery_report_rejects_invalid_values(
    field, value, test_client, create_user, login_user, whoami, create_report
):
    token, org_id, report = _owner_report(create_user, login_user, whoami, create_report)
    resp = test_client.post(
        f"/api/reports/{report['id']}/completions/{uuid.uuid4()}/stream-diagnostics",
        json=_delivery(**{field: value}),
        headers=_headers(token, org_id),
    )
    assert resp.status_code == 422


@pytest.mark.e2e
def test_delivery_report_is_gated_like_the_conversation(
    test_client, create_user, login_user, whoami, create_report
):
    admin_email = f"sd_admin_{uuid.uuid4().hex[:6]}@test.com"
    create_user(email=admin_email, password="test123")
    admin_token = login_user(email=admin_email, password="test123")
    org_id = whoami(admin_token)["organizations"][0]["id"]
    member_email = f"sd_member_{uuid.uuid4().hex[:6]}@test.com"
    test_client.post(
        f"/api/organizations/{org_id}/members",
        json={"organization_id": org_id, "email": member_email, "role": "member"},
        headers=_headers(admin_token, org_id),
    )
    create_user(email=member_email, password="test123")
    member_token = login_user(email=member_email, password="test123")
    report = create_report(title="Admin's report", user_token=admin_token, org_id=org_id, data_sources=[])
    url = f"/api/reports/{report['id']}/completions/{uuid.uuid4()}/stream-diagnostics"

    assert test_client.post(url, json=_delivery()).status_code == 401
    assert test_client.post(url, json=_delivery(), headers=_headers(member_token, org_id)).status_code == 403
    assert test_client.post(url, json=_delivery(), headers=_headers(admin_token, org_id)).status_code == 200
