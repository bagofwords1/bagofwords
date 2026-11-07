"""POST /api/connections/{id}/kms-diagnostics through the real API and DB (P12).

Only the KMS Lighthouse API is simulated (tools/kms_lighthouse mock, in process).
"""
import os
import threading
import uuid

import pytest
from sqlalchemy import create_engine, text

from app.core import egress
from app.data_sources.clients import kms_lighthouse_client as kc
from app.data_sources.clients import kms_lighthouse_diagnostics as kd
from tests.e2e.test_kms_lighthouse import _admin, _cfg, _member, _svc_userpass, _user_required_source
from tests.unit.test_kms_lighthouse_client import kms, kms_server  # noqa: F401


def _url(connection_id):
    return f"/api/connections/{connection_id}/kms-diagnostics"


def _rows(body):
    return {row["id"]: row for row in body["checks"]}


def _readable_search(kms, monkeypatch):
    """The tree and search hold only ids with a body, so the connector can read everything the run samples."""
    mock = kms["mock"]
    monkeypatch.setattr(mock, "TREE", {node_id: node for node_id, node in mock.TREE.items()
                                       if node["type"] == "FOLDER" or node_id in mock.ITEMS})
    monkeypatch.setattr(mock, "SEARCH_RESULTS", [
        {"id": "279", "subType": "T_PROCEDURE", "title": "t", "description": "", "statusCode": "2"}])
    mock.STATE["extra_results"] = [{"id": "21302", "subType": "T_SCENARIO_FLOW", "title": "t", "description": "",
                                    "statusCode": "2"}]


def _forget_saved_credentials(connection_id):
    """A raw write: the API refuses to save a KMS connection without credentials, so only this reaches the state."""
    url = os.environ["TEST_DATABASE_URL"]
    engine = create_engine(url.replace("postgresql+asyncpg://", "postgresql://").replace("sqlite+aiosqlite://", "sqlite://"))
    with engine.begin() as conn:
        conn.execute(text("UPDATE connections SET credentials = NULL WHERE id = :id"), {"id": connection_id})
    engine.dispose()


def _kms_connection(kms, create_connection, token, org, credentials=None, **config):
    return create_connection(name=f"KMS {uuid.uuid4().hex[:6]}", type="kms_lighthouse", config=_cfg(kms, **config),
                             credentials=credentials or _svc_userpass(kms), user_token=token, org_id=org)


@pytest.mark.e2e
def test_an_admin_gets_a_report_and_no_secret(kms, test_client, create_user, login_user, whoami, create_connection, monkeypatch):
    token, org, headers = _admin(create_user, login_user, whoami)
    conn = _kms_connection(kms, create_connection, token, org)
    _readable_search(kms, monkeypatch)
    mock = kms["mock"]
    mock.REQUEST_LOG.clear()
    mock.STATE["login_count"] = 0
    r = test_client.post(_url(conn["id"]), headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["report_version"] == 1 and body["failed_stage"] is None
    assert body["host"] == kms["base_url"].split("//")[1]
    assert mock.STATE["login_count"] == 1
    assert body["calls_made"] == len(mock.REQUEST_LOG)
    assert mock.USERS["svc"]["password"] not in r.text and "svc" not in r.text.replace("service", "")


@pytest.mark.e2e
def test_a_member_without_manage_connection_is_refused_and_nothing_is_sent(
    kms, test_client, create_user, login_user, whoami, create_connection,
):
    token, org, headers = _admin(create_user, login_user, whoami)
    conn = _kms_connection(kms, create_connection, token, org)
    member_headers = _member(test_client, create_user, login_user, org, headers, "plain")
    kms["mock"].REQUEST_LOG.clear()
    r = test_client.post(_url(conn["id"]), headers=member_headers)
    assert r.status_code == 403
    assert kms["mock"].REQUEST_LOG == []


@pytest.mark.e2e
def test_another_connection_type_gets_a_typed_error(tmp_path, test_client, create_user, login_user, whoami, create_connection):
    token, org, headers = _admin(create_user, login_user, whoami)
    conn = create_connection(name="Shared drive", type="network_dir", config={"root_path": str(tmp_path)},
                             credentials={}, user_token=token, org_id=org)
    r = test_client.post(_url(conn["id"]), headers=headers)
    assert r.status_code == 400, r.text
    assert r.json()["error_code"] == "connection.kms_only"


@pytest.mark.e2e
def test_an_unknown_connection_is_not_found(test_client, create_user, login_user, whoami):
    _, _, headers = _admin(create_user, login_user, whoami)
    assert test_client.post(_url(uuid.uuid4()), headers=headers).status_code in (403, 404)


@pytest.mark.e2e
def test_a_wrong_password_is_a_report_not_an_error(kms, test_client, create_user, login_user, whoami, create_connection):
    token, org, headers = _admin(create_user, login_user, whoami)
    conn = _kms_connection(kms, create_connection, token, org)
    kms["mock"].STATE["login_status"] = 400
    kms["mock"].STATE["login_count"] = 0
    r = test_client.post(_url(conn["id"]), headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["failed_stage"] == "login" and r.json()["overall"] == "fail"
    assert kms["mock"].STATE["login_count"] == 1


@pytest.mark.e2e
def test_the_admin_fallback_runs_on_a_per_user_connection(
    kms, test_client, create_user, login_user, whoami, create_data_source, monkeypatch,
):
    token, org, headers = _admin(create_user, login_user, whoami)
    _, conn_id = _user_required_source(kms, test_client, create_data_source, token, org, headers)
    _readable_search(kms, monkeypatch)
    kms["mock"].REQUEST_LOG.clear()
    r = test_client.post(_url(conn_id), headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["failed_stage"] is None, [row for row in r.json()["checks"] if row["status"] == "fail"]
    assert "svc" in {user for user, _, _ in kms["mock"].REQUEST_LOG}


@pytest.mark.e2e
def test_credentials_that_cannot_be_resolved_are_a_report_not_a_500(
    kms, test_client, create_user, login_user, whoami, create_data_source,
):
    token, org, headers = _admin(create_user, login_user, whoami)
    _, conn_id = _user_required_source(kms, test_client, create_data_source, token, org, headers)
    _forget_saved_credentials(conn_id)
    kms["mock"].REQUEST_LOG.clear()
    r = test_client.post(_url(conn_id), headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["failed_stage"] == "login" and body["overall"] == "fail" and body["calls_made"] == 0
    assert _rows(body)["login"]["observed"]["reason"] == "credentials_unavailable"
    assert kms["mock"].REQUEST_LOG == []


@pytest.mark.e2e
def test_a_blocked_destination_is_a_network_report_and_nothing_is_sent(
    kms, test_client, create_user, login_user, whoami, create_connection, monkeypatch,
):
    token, org, headers = _admin(create_user, login_user, whoami)
    conn = _kms_connection(kms, create_connection, token, org)
    monkeypatch.setattr(egress, "block_private_outbound", lambda: True)
    kms["mock"].REQUEST_LOG.clear()
    r = test_client.post(_url(conn["id"]), headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["failed_stage"] == "network" and body["calls_made"] == 0
    assert _rows(body)["network"]["observed"]["reason"] == "egress_blocked"
    assert kms["mock"].REQUEST_LOG == [], "the egress check runs before any KMS call"


@pytest.mark.e2e
def test_a_run_past_the_route_limit_is_a_report_and_the_request_ends(
    kms, test_client, create_user, login_user, whoami, create_connection, monkeypatch,
):
    token, org, headers = _admin(create_user, login_user, whoami)
    conn = _kms_connection(kms, create_connection, token, org)
    # KMS takes the first call and says nothing. The client's own floor is 1 s, so the route's limit expires first.
    monkeypatch.setattr(kd, "_MAX_SECONDS", 0.2)
    monkeypatch.setattr(kd, "_ROUTE_GRACE_SECONDS", 0)
    mock = kms["mock"]
    mock.STATE["hold_no_session"] = True
    try:
        r = test_client.post(_url(conn["id"]), headers=headers)
    finally:
        mock.HELD["release"].set()
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["failed_stage"] == "network" and body["overall"] == "fail"
    assert _rows(body)["network"]["observed"]["reason"] == "run_timed_out"
    assert body["calls_made"] is None and body["calls_unknown"] is True


@pytest.mark.e2e
def test_a_run_that_starts_after_the_route_gave_up_sends_nothing(
    kms, test_client, create_user, login_user, whoami, create_connection, monkeypatch,
):
    """A worker thread that frees up late must not start a login nobody waits for."""
    token, org, headers = _admin(create_user, login_user, whoami)
    conn = _kms_connection(kms, create_connection, token, org)
    monkeypatch.setattr(kd, "_MAX_SECONDS", 0.2)
    monkeypatch.setattr(kd, "_ROUTE_GRACE_SECONDS", 0)
    gate, finished = threading.Event(), threading.Event()
    real = kc.KmsLighthouseClient.diagnostics

    def starts_late(self, *args, **kwargs):
        gate.wait(5)
        try:
            return real(self, *args, **kwargs)
        finally:
            finished.set()

    monkeypatch.setattr(kc.KmsLighthouseClient, "diagnostics", starts_late)
    mock = kms["mock"]
    mock.REQUEST_LOG.clear()
    mock.STATE["login_count"] = 0
    r = test_client.post(_url(conn["id"]), headers=headers)
    assert r.status_code == 200, r.text
    assert _rows(r.json())["network"]["observed"]["reason"] == "run_timed_out"
    gate.set()
    assert finished.wait(5)
    assert mock.STATE["login_count"] == 0 and mock.REQUEST_LOG == []


@pytest.mark.e2e
def test_the_run_changes_no_connection_state(
    kms, test_client, create_user, login_user, whoami, create_connection, get_connection,
):
    token, org, headers = _admin(create_user, login_user, whoami)
    conn = _kms_connection(kms, create_connection, token, org)
    before = get_connection(connection_id=conn["id"], user_token=token, org_id=org)
    kms["mock"].STATE["login_status"] = 400
    assert test_client.post(_url(conn["id"]), headers=headers).status_code == 200
    after = get_connection(connection_id=conn["id"], user_token=token, org_id=org)
    assert (after.get("is_active"), after.get("last_connection_status")) == (
        before.get("is_active"), before.get("last_connection_status"))
