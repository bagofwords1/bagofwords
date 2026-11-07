"""On-site diagnostics contract against the in-process mock KMS API (P12).

The mock is the remote boundary: a real HTTP server on a real socket. Only the
HTTP client (requests) and the clock are ever replaced, and only to reach a
failure the socket cannot produce. Each Safety rule of the brief has a test.
"""
import errno
import io
import json
import re
import socket
import ssl
import subprocess
import threading
from pathlib import Path

import pytest
import requests

from app.data_sources.clients import kms_lighthouse_client as kc
from app.data_sources.clients import kms_lighthouse_diagnostics as kd
from tests.unit.test_kms_lighthouse_client import client, kms, kms_server, raw_kms, token_client  # noqa: F401

_REPORT_KEYS = {"report_version", "generated_at", "bow_version", "host", "api_prefix", "auth_method", "root_folder_set",
                "overall", "failed_stage", "manual_checks", "calls_made", "duration_ms", "checks"}
_STATUSES = {"pass", "warn", "fail", "info", "skipped"}
_STAGES = {"network", "tls", "login", "browse", "search", "read"}
# Every Live-probe checklist item has a row.
_CHECKLIST = set(range(1, 19))
_REPO = Path(__file__).resolve().parents[3]
_PANEL = _REPO / "frontend" / "components" / "datasources" / "KmsDiagnosticsPanel.vue"
_PAGE = _REPO / "frontend" / "pages" / "integrations" / "[id]" / "connection.vue"
_GUIDE = _REPO / "docs" / "kms-lighthouse.md"
_LOOP = _REPO / "docs" / "feedback-loops" / "kms-lighthouse-connector.md"
_LOCALES = sorted((_REPO / "locales").glob("*.json"))


def _rows(report):
    return {row["id"]: row for row in report["checks"]}


def _logins(kms):
    return kms["mock"].STATE["login_count"]


def _after_login(report):
    ids = [row["id"] for row in report["checks"]]
    return report["checks"][ids.index("login") + 1:]


def _assert_rest_skipped(report, keep=()):
    """Every check after login was skipped; none ran."""
    rows = [row for row in _after_login(report) if row["id"] not in keep]
    assert rows and all(row["status"] == "skipped" for row in rows), rows


def _assert_plain_report(report):
    """The report is plain JSON data of the stable shape."""
    assert set(report) == _REPORT_KEYS
    assert report["report_version"] == 1
    assert report["overall"] in ("pass", "warn", "fail")
    assert report["failed_stage"] is None or report["failed_stage"] in _STAGES
    assert json.loads(json.dumps(report)) == report
    for row in report["checks"]:
        assert set(row) == {"id", "checklist", "stage", "status", "summary", "observed"}
        assert row["status"] in _STATUSES and row["stage"] in _STAGES and row["summary"], row


def _scenario_hit(scenario_id):
    return {"id": scenario_id, "subType": "T_SCENARIO_FLOW", "title": "A flow", "description": "", "statusCode": "2"}


# ------------------------------------------------------------------ the report
def test_a_healthy_tenant_answers_every_checklist_item(kms):
    report = client(kms, "alice", root_folder_id=10).diagnostics()
    _assert_plain_report(report)
    assert report["overall"] == "warn", "the mock's search matches every query and holds ids with no body"
    assert report["failed_stage"] is None
    assert report["host"] == kms["base_url"].split("//")[1]
    assert report["api_prefix"] == kms["mock"].PREFIX
    assert report["auth_method"] == "userpass" and report["root_folder_set"] is True
    assert report["bow_version"] and report["generated_at"].endswith("+00:00")
    assert report["calls_made"] == len(kms["mock"].REQUEST_LOG) and report["duration_ms"] >= 0
    assert {item for row in report["checks"] for item in row["checklist"]} == _CHECKLIST
    assert not [row for row in report["checks"] if row["status"] == "fail"]


def _clean_tenant(kms, monkeypatch):
    """Every automatic check has something to find and finds nothing wrong."""
    mock = kms["mock"]
    monkeypatch.setattr(mock, "SEARCH_RESULTS", [
        {"id": "279", "subType": "T_PROCEDURE", "title": "t", "description": "", "statusCode": "2"}])
    mock.STATE["extra_results"] = [_scenario_hit("21302")]
    mock.STATE["no_hit_text"] = kd._NO_HIT_QUERY


def test_a_clean_automatic_run_passes_with_one_manual_check_left(kms, monkeypatch):
    _clean_tenant(kms, monkeypatch)
    report = client(kms, "svc", root_folder_id=95).diagnostics()
    rows = _rows(report)
    assert report["overall"] == "pass", [(row["id"], row["status"]) for row in report["checks"]
                                         if row["status"] not in ("pass", "info")]
    assert report["failed_stage"] is None and report["manual_checks"] == ["ai_elements"]
    assert rows["ai_elements"]["status"] == "warn", "item 12 still cannot pass from one account"
    assert rows["tls"]["status"] == "skipped" and rows["concurrent_sessions"]["status"] == "skipped"


def test_a_warning_other_than_the_manual_check_lowers_overall(kms, monkeypatch):
    _clean_tenant(kms, monkeypatch)
    kms["mock"].STATE["search_max_rows"] = 1
    report = client(kms, "svc", root_folder_id=95).diagnostics()
    assert _rows(report)["page_rows"]["status"] == "warn" and report["overall"] == "warn"


@pytest.mark.parametrize("variant,skipped", [
    ({"extra_results": []}, "scenario_text"),
    ({"ai_status": "error"}, None),
])
def test_a_skipped_automatic_check_lowers_overall(kms, monkeypatch, variant, skipped):
    _clean_tenant(kms, monkeypatch)
    kms["mock"].STATE.update(variant)
    report = client(kms, "svc", root_folder_id=95).diagnostics()
    assert report["overall"] == "warn"
    if skipped:
        assert _rows(report)[skipped]["status"] == "skipped"


def test_ai_elements_skipped_lowers_overall(kms, monkeypatch):
    _clean_tenant(kms, monkeypatch)
    unversioned = {key: value for key, value in kms["mock"].ITEMS["279"].items() if key != "version"}
    monkeypatch.setitem(kms["mock"].ITEMS, "279", unversioned)
    report = client(kms, "svc", root_folder_id=95).diagnostics()
    assert _rows(report)["ai_elements"]["status"] == "skipped"
    assert report["overall"] == "warn" and report["manual_checks"] == []


def test_ten_and_thirteen_are_skipped_with_a_reason_to_ask_the_admin(kms):
    rows = _rows(client(kms, "alice").diagnostics())
    assert rows["concurrent_sessions"]["status"] == "skipped" and rows["logout"]["status"] == "skipped"
    assert "failed login" in rows["concurrent_sessions"]["summary"].lower()
    assert "admin" in rows["concurrent_sessions"]["summary"].lower() and "admin" in rows["logout"]["summary"].lower()


def test_an_api_token_never_logs_in(kms):
    report = token_client(kms, "alice").diagnostics()
    assert report["auth_method"] == "token"
    assert _logins(kms) == 0
    assert _rows(report)["login"]["status"] == "pass"


@pytest.mark.parametrize("mode", ["cookie", "token", "both"])
def test_login_reports_the_session_shape_never_its_value(kms, mode):
    kms["mock"].STATE["login_mode"] = mode
    observed = _rows(client(kms, "alice").diagnostics())["login"]["observed"]
    assert observed["http_status"] == 200
    assert observed["cookie_count"] == (0 if mode == "token" else 1)
    assert observed["cookie_names"] == ([] if mode == "token" else [kms["mock"].SESSION_COOKIE])
    assert observed["data_token"] is (mode != "cookie")
    assert set(observed["response_keys"]) == {"status", "data"}


# ------------------------------------------------------------------ safety rule 2: one login at most
def test_a_rejected_login_is_sent_once_and_ends_the_run(kms):
    report = client(kms, "alice", password="wrong-pass").diagnostics()
    _assert_plain_report(report)
    assert _logins(kms) == 1
    assert report["overall"] == "fail" and report["failed_stage"] == "login"
    assert _rows(report)["login"]["status"] == "fail"
    _assert_rest_skipped(report)
    # Only the one sessionless reach probe and the login went out.
    assert report["calls_made"] == 2 and len(kms["mock"].REQUEST_LOG) == 2


def test_a_login_hold_sends_no_login_and_ends_the_run(kms):
    client(kms, "alice", password="wrong-pass").diagnostics()
    report = client(kms, "alice", password="wrong-pass").diagnostics()
    assert _logins(kms) == 1, "the hold from the first run must stop the second login"
    assert report["failed_stage"] == "login"
    assert _rows(report)["login"]["observed"]["reason"] == "held"
    _assert_rest_skipped(report)


@pytest.mark.parametrize("expired", ["401", "302"])
def test_a_session_refused_after_login_ends_the_run_with_no_second_login(kms, expired):
    """The session dies on arrival: the first read is refused, and the run must not log in again."""
    kms["mock"].STATE.update({"session_expiry": "immediate", "expired_response": expired})
    report = client(kms, "alice", root_folder_id=10).diagnostics()
    assert _logins(kms) == 1
    row = _rows(report)["session"]
    assert row["status"] == "fail" and row["observed"]["reason"] == "session_refused"
    assert report["failed_stage"] == "login" and report["overall"] == "fail"
    _assert_rest_skipped(report, keep=("session",))
    assert report["calls_made"] == len(kms["mock"].REQUEST_LOG)


def test_the_session_check_is_one_authenticated_get(kms):
    report = client(kms, "alice").diagnostics()
    assert _rows(report)["session"]["status"] == "pass"
    assert kms["mock"].REQUEST_LOG[2][:2] == ("alice", "GET"), "the first call after the login"


def test_an_api_token_has_no_session_check_to_skip(kms):
    row = _rows(token_client(kms, "alice").diagnostics())["session"]
    assert row["status"] == "info"


def test_a_session_read_with_an_odd_answer_warns_and_the_run_goes_on(kms, monkeypatch):
    real = requests.Session.request
    calls = []

    def second_tree_call_fails(self, method, url, **kwargs):
        calls.append(url)
        if len([seen for seen in calls if "/cm/tree/" in seen]) == 2:
            response = requests.Response()
            response.status_code, response.raw = 503, io.BytesIO(b"")
            return response
        return real(self, method, url, **kwargs)

    monkeypatch.setattr(requests.Session, "request", second_tree_call_fails)
    rows = _rows(client(kms, "alice").diagnostics())
    assert rows["session"]["status"] == "warn" and rows["session"]["observed"]["http_status"] == 503
    assert rows["top_level"]["status"] == "pass"


def _fail_tree_call(monkeypatch, nth, error):
    """The ``nth`` tree call raises ``error`` at the HTTP client: 1 is the reach, 2 the session check, 3 top_level."""
    real = requests.Session.request
    seen = []

    def failing(self, method, url, **kwargs):
        if "/cm/tree/" in url:
            seen.append(url)
            if len(seen) == nth:
                raise error
        return real(self, method, url, **kwargs)

    monkeypatch.setattr(requests.Session, "request", failing)


_LOST_LINE = requests.exceptions.ConnectionError(ConnectionRefusedError())
_TLS_LOST = requests.exceptions.SSLError(ssl.SSLError())


@pytest.mark.parametrize("error,stage", [(_LOST_LINE, "network"), (_TLS_LOST, "tls")])
def test_a_transport_failure_at_the_session_check_reports_its_stage_and_ends_the_run(kms, monkeypatch, error, stage):
    _fail_tree_call(monkeypatch, 2, error)
    report = client(kms, "alice").diagnostics()
    assert report["failed_stage"] == stage and _rows(report)["session"]["stage"] == stage
    assert _logins(kms) == 1
    _assert_rest_skipped(report, keep=("session",))


@pytest.mark.parametrize("error,stage", [(_LOST_LINE, "network"), (_TLS_LOST, "tls")])
def test_a_transport_failure_at_the_tree_is_not_a_browse_failure(kms, monkeypatch, error, stage):
    _fail_tree_call(monkeypatch, 3, error)
    report = client(kms, "alice").diagnostics()
    assert report["failed_stage"] == stage and _rows(report)["top_level"]["stage"] == stage
    assert _logins(kms) == 1


@pytest.mark.parametrize("expired", ["401", "302"])
def test_a_refused_token_ends_the_run(kms, expired):
    kms["mock"].STATE["expired_response"] = expired
    report = kc.KmsLighthouseClient(base_url=kms["base_url"], allow_http=True, api_token="not-a-known-token").diagnostics()
    assert report["failed_stage"] == "login" and _rows(report)["login"]["status"] == "fail"
    _assert_rest_skipped(report)


@pytest.mark.parametrize("answer", [301, 302, 400, 401, 403])
def test_a_token_check_refused_in_any_way_ends_the_run(kms, answer):
    kms["mock"].STATE["session_info_status"] = answer
    report = token_client(kms, "alice").diagnostics()
    row = _rows(report)["login"]
    assert row["status"] == "fail" and row["observed"]["reason"] == "rejected"
    assert report["failed_stage"] == "login"
    _assert_rest_skipped(report)


@pytest.mark.parametrize("answer", [500, 503, "error", "not_json", "empty"])
def test_a_token_check_answered_with_a_server_failure_ends_the_run(kms, answer):
    kms["mock"].STATE["session_info_status"] = answer
    report = token_client(kms, "alice").diagnostics()
    row = _rows(report)["login"]
    assert row["status"] == "fail" and row["observed"]["reason"] == "server"
    assert report["failed_stage"] == "login"
    _assert_rest_skipped(report)


def test_a_missing_session_info_route_warns_and_the_run_goes_on(kms):
    kms["mock"].STATE["session_info_status"] = 404
    rows = _rows(token_client(kms, "alice").diagnostics())
    assert rows["login"]["status"] == "warn" and rows["login"]["observed"]["http_status"] == 404
    assert rows["top_level"]["status"] == "pass"


def test_a_login_rejection_names_the_other_causes_of_a_rejection(kms):
    summary = _rows(client(kms, "alice", password="wrong-pass").diagnostics())["login"]["summary"].lower()
    assert "password" in summary and "device" in summary and "shape" in summary


def test_a_login_server_error_ends_the_run(kms):
    kms["mock"].STATE["login_status"] = 503
    report = client(kms, "alice").diagnostics()
    assert report["failed_stage"] == "login" and _logins(kms) == 1
    assert _rows(report)["login"]["observed"]["http_status"] == 503
    _assert_rest_skipped(report)


def test_a_login_with_no_session_says_so(kms):
    kms["mock"].STATE["login_mode"] = "none"
    report = client(kms, "alice").diagnostics()
    assert report["failed_stage"] == "login"
    assert _rows(report)["login"]["observed"]["reason"] == "no_session"
    _assert_rest_skipped(report)


# ------------------------------------------------------------------ item 3 and network classification
@pytest.mark.parametrize("expired,status,redirected", [("401", 401, False), ("302", 302, True)])
def test_expiry_answer_is_probed_with_no_session(kms, expired, status, redirected):
    kms["mock"].STATE["expired_response"] = expired
    rows = _rows(client(kms, "alice").diagnostics())
    assert rows["expiry"]["observed"] == {"http_status": status, "redirected": redirected}
    assert rows["expiry"]["status"] == "pass"
    anonymous = kms["mock"].REQUEST_LOG[0]
    assert anonymous == (None, "GET", kms["mock"].PREFIX + "/cm/tree/branch/view")


@pytest.mark.parametrize("answer,failure", [("html", "not_json"), ("empty", "empty_body")])
def test_a_200_with_no_session_shows_what_came_back(kms, answer, failure):
    """A login page answered as HTTP 200 is not a session expiry the connector can see."""
    kms["mock"].STATE["no_session_answer"] = answer
    row = _rows(client(kms, "alice").diagnostics())["expiry"]
    assert row["status"] == "warn"
    assert row["observed"] == {"http_status": 200, "redirected": False, "failure": failure}


def test_no_reply_at_all_never_reads_as_a_trusted_certificate(monkeypatch):
    def nothing_comes_back(self, method, url, **kwargs):
        raise RuntimeError("kms-exception-text-e4")

    monkeypatch.setattr(requests.Session, "request", nothing_comes_back)
    report = kc.KmsLighthouseClient(base_url="https://kms.example.com", username="u", password="p").diagnostics()
    rows = _rows(report)
    assert rows["network"]["observed"] == {"error_type": "RuntimeError"}
    assert rows["tls"]["status"] == "skipped" and rows["expiry"]["status"] == "skipped"
    assert report["overall"] == "fail"


def test_the_first_call_and_the_login_keep_the_connectors_read_timeout(kms, monkeypatch):
    real = requests.Session.request
    timeouts = []

    def recording(self, method, url, **kwargs):
        timeouts.append(kwargs["timeout"])
        return real(self, method, url, **kwargs)

    monkeypatch.setattr(requests.Session, "request", recording)
    client(kms, "alice").diagnostics()
    reach, login, *later = timeouts
    assert kd._CALL_SECONDS < reach[1] <= kc._DEFAULT_TIMEOUT[1] and kd._CALL_SECONDS < login[1] <= kc._DEFAULT_TIMEOUT[1]
    assert later and all(read <= kd._CALL_SECONDS for _, read in later)


def _closed_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def test_a_refused_port_is_a_network_failure_with_no_exception_text():
    report = kc.KmsLighthouseClient(base_url=f"http://127.0.0.1:{_closed_port()}", allow_http=True,
                                    username="u", password="p").diagnostics()
    assert report["overall"] == "fail" and report["failed_stage"] == "network"
    rows = _rows(report)
    assert rows["network"]["status"] == "fail" and rows["network"]["observed"]["failure"] == "refused"
    assert rows["tls"]["status"] == "skipped"
    assert rows["login"]["observed"]["failure"] == "refused"
    _assert_rest_skipped(report)
    assert "Errno" not in json.dumps(report) and "HTTPConnectionPool" not in json.dumps(report)


def test_a_name_that_does_not_resolve_is_a_dns_failure(monkeypatch):
    def no_such_name(*_args, **_kwargs):
        raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")

    monkeypatch.setattr(socket, "getaddrinfo", no_such_name)
    report = kc.KmsLighthouseClient(base_url="https://kms.invalid", username="u", password="p").diagnostics()
    assert report["failed_stage"] == "network"
    assert _rows(report)["network"]["observed"]["failure"] == "dns"


@pytest.mark.parametrize("code,word", [(errno.EHOSTUNREACH, "blocked_route"), (errno.ENETUNREACH, "blocked_route"),
                                       (errno.EACCES, "blocked_route"), (errno.ETIMEDOUT, "timeout"),
                                       (errno.ECONNREFUSED, "refused"), (errno.ECONNRESET, "connection_failed")])
def test_a_failed_connect_is_named_by_its_errno(monkeypatch, code, word):
    def no_route(self, address):
        raise OSError(code, "kms-exception-text-e5")

    monkeypatch.setattr(socket.socket, "connect", no_route)
    report = kc.KmsLighthouseClient(base_url="http://10.0.0.1", allow_http=True, username="u",
                                    password="p").diagnostics()
    assert _rows(report)["network"]["observed"]["failure"] == word
    assert report["failed_stage"] == "network" and "kms-exception-text-e5" not in json.dumps(report)


def test_a_connector_message_is_the_same_for_a_blocked_route(monkeypatch):
    def no_route(self, address):
        raise OSError(errno.EHOSTUNREACH, "kms-exception-text-e6")

    monkeypatch.setattr(socket.socket, "connect", no_route)
    outcome = kc.KmsLighthouseClient(base_url="http://10.0.0.1", allow_http=True, api_token="t").test_connection()
    assert outcome == {"success": False, "message": kc._MESSAGES["unreachable"]}


def _bad_tls(code):
    failure = ssl.SSLCertVerificationError("kms-exception-text-e7")
    failure.verify_code = code
    return requests.exceptions.SSLError(failure)


@pytest.mark.parametrize("code,word", [(10, "expired"), (62, "host name"), (19, "not trusted"), (20, "not trusted")])
def test_a_certificate_failure_carries_its_verify_code_and_says_what_is_wrong(monkeypatch, code, word):
    def refused(self, method, url, **kwargs):
        raise _bad_tls(code)

    monkeypatch.setattr(requests.Session, "request", refused)
    report = kc.KmsLighthouseClient(base_url="https://kms.example.com", username="u", password="p").diagnostics()
    row = _rows(report)["tls"]
    assert row["status"] == "fail" and row["observed"]["failure"] == "untrusted_certificate"
    assert row["observed"]["verify_code"] == code
    assert word in row["summary"].lower()
    assert _rows(report)["login"]["observed"]["verify_code"] == code
    assert "kms-exception-text-e7" not in json.dumps(report)


def test_a_certificate_failure_at_the_login_says_what_is_wrong(kms, monkeypatch):
    real = requests.Session.request

    def login_refused(self, method, url, **kwargs):
        if method == "POST":
            raise _bad_tls(10)
        return real(self, method, url, **kwargs)

    monkeypatch.setattr(requests.Session, "request", login_refused)
    report = client(kms, "alice").diagnostics()
    row = _rows(report)["login"]
    assert row["stage"] == "tls" and row["observed"]["verify_code"] == 10 and "expired" in row["summary"].lower()


@pytest.mark.parametrize("fault,word", [("truncated", "bad_body"), ("bad_gzip", "bad_encoding")])
def test_a_body_that_breaks_after_the_status_keeps_the_status_and_the_run_goes_on(kms, fault, word):
    kms["mock"].STATE["body_fault"] = {"route": "/search/full", "fault": fault}
    report = client(kms, "alice").diagnostics()
    rows = _rows(report)
    assert rows["search_layout"]["status"] == "fail" and rows["search_layout"]["observed"]["failure"] == word
    assert rows["search_layout"]["observed"]["without_layout_status"] == 200
    assert report["failed_stage"] == "search"
    assert rows["field_types"]["status"] != "skipped", "KMS answered, so the run is not over"


@pytest.mark.parametrize("fault,word", [("truncated", "bad_body"), ("bad_gzip", "bad_encoding")])
def test_a_login_answer_that_breaks_is_a_login_failure_with_its_status(kms, fault, word):
    kms["mock"].STATE["body_fault"] = {"route": "/login", "fault": fault}
    report = client(kms, "alice").diagnostics()
    row = _rows(report)["login"]
    assert report["failed_stage"] == "login" and row["stage"] == "login"
    assert row["observed"]["reason"] == word and row["observed"]["http_status"] == 200
    assert _logins(kms) == 1
    _assert_rest_skipped(report)


@pytest.mark.parametrize("fault", ["truncated", "bad_gzip"])
def test_a_connector_message_is_the_same_for_a_broken_body(kms, fault):
    kms["mock"].STATE["body_fault"] = {"route": "/search/full", "fault": fault}
    with pytest.raises(kc.KmsHTTPError) as caught:
        client(kms, "alice").search_files("anything")
    assert (caught.value.status, caught.value.kind, str(caught.value)) == (0, "unreachable", kc._MESSAGES["unreachable"])


def test_an_unreadable_ca_bundle_is_a_tls_failure_and_nothing_is_sent(monkeypatch, tmp_path):
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(tmp_path / "secret-name-no-such-bundle.pem"))
    sent = []
    monkeypatch.setattr(requests.Session, "request", lambda self, *args, **kwargs: sent.append(args))
    report = kc.KmsLighthouseClient(base_url="https://kms.example.com", username="u", password="p").diagnostics()
    rows = _rows(report)
    assert rows["tls"]["status"] == "fail" and rows["tls"]["observed"]["failure"] == "ca_bundle_unreadable"
    assert report["failed_stage"] == "tls" and report["overall"] == "fail"
    assert sent == [] and report["calls_made"] == 0
    assert "secret-name-no-such-bundle" not in json.dumps(report)


def test_a_ca_bundle_is_not_consulted_over_plain_http(kms, monkeypatch, tmp_path):
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(tmp_path / "no-such-bundle.pem"))
    assert _rows(client(kms, "alice").diagnostics())["login"]["status"] == "pass"


def test_a_silent_server_is_a_timeout():
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    port = listener.getsockname()[1]
    try:
        report = kc.KmsLighthouseClient(base_url=f"http://127.0.0.1:{port}", allow_http=True, username="u",
                                        password="p").diagnostics(deadline=kc._clock() + 1.0)
    finally:
        listener.close()
    rows = _rows(report)
    assert report["failed_stage"] == "network"
    assert rows["network"]["observed"]["failure"] == "timeout"


@pytest.fixture
def self_signed_kms(tmp_path):
    """A real TLS listener whose certificate no CA signed."""
    key, cert = tmp_path / "key.pem", tmp_path / "cert.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key), "-out", str(cert),
                    "-days", "1", "-subj", "/CN=localhost"], check=True, capture_output=True)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    listener.settimeout(0.1)
    received = []
    stop = threading.Event()

    def serve():
        while not stop.is_set():
            try:
                conn, _ = listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            try:
                with context.wrap_socket(conn, server_side=True) as tls:
                    received.append(tls.recv(4096))
            except (ssl.SSLError, OSError):
                pass

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    yield {"port": listener.getsockname()[1], "received": received}
    stop.set()
    thread.join(5)
    listener.close()


def test_an_untrusted_certificate_is_a_tls_failure_and_no_password_leaves(self_signed_kms):
    secret = "tls-test-password-91c"
    report = kc.KmsLighthouseClient(base_url=f"https://localhost:{self_signed_kms['port']}", username="u",
                                    password=secret).diagnostics()
    assert report["overall"] == "fail" and report["failed_stage"] == "tls"
    rows = _rows(report)
    assert rows["network"]["status"] == "pass"
    assert rows["tls"]["status"] == "fail" and rows["tls"]["observed"]["failure"] == "untrusted_certificate"
    assert type(rows["tls"]["observed"]["verify_code"]) is int and rows["tls"]["observed"]["verify_code"] > 0
    assert rows["login"]["stage"] == "tls"
    _assert_rest_skipped(report)
    assert self_signed_kms["received"] == [], "no request may finish a handshake with an untrusted server"


# ------------------------------------------------------------------ safety rule 3: the budget
def test_the_budget_constants_are_the_contract():
    assert kd._MAX_CALLS == 60 and kd._MAX_SECONDS == 90 and kd._MAX_ITEMS == 5 and kd._MAX_SCENARIOS == 2


def test_hitting_the_call_budget_skips_the_rest(kms, monkeypatch):
    monkeypatch.setattr(kd, "_MAX_CALLS", 5)
    report = client(kms, "alice").diagnostics()
    assert report["calls_made"] == 5 == len(kms["mock"].REQUEST_LOG)
    assert report["overall"] == "warn" and report["failed_stage"] is None
    assert [row for row in report["checks"] if row["status"] == "skipped" and "budget" in row["summary"].lower()]
    assert _rows(report)["prefix"]["status"] == "skipped"


def test_hitting_the_time_budget_skips_the_rest(kms, monkeypatch):
    """Each reading of the clock moves it 10 s, so the 90 s budget runs out part of the way through."""
    now = [kc._clock()]

    def ticking():
        now[0] += 10
        return now[0]

    monkeypatch.setattr(kc, "_clock", ticking)
    report = client(kms, "alice").diagnostics()
    assert report["overall"] == "warn"
    assert 0 < report["calls_made"] < 20
    assert [row for row in report["checks"] if row["status"] == "skipped" and "budget" in row["summary"].lower()]


def test_every_call_is_bound_by_the_time_left(kms, monkeypatch):
    """A frozen clock that only the calls move: no call may get a timeout longer than what remains."""
    probe = client(kms, "alice")
    now = [1000.0]
    deadline = now[0] + 30
    real = requests.Session.request
    seen = []

    def recording(self, method, url, **kwargs):
        seen.append((kwargs["timeout"], deadline - now[0]))
        now[0] += 3
        return real(self, method, url, **kwargs)

    monkeypatch.setattr(kc.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(kc, "_clock", lambda: now[0])
    monkeypatch.setattr(requests.Session, "request", recording)
    report = probe.diagnostics(deadline=deadline)
    assert len(seen) >= 5 and report["calls_made"] == len(seen)
    for (connect, read), left in seen:
        assert max(connect, read) <= max(kc._MIN_TIMEOUT_S, left), (connect, read, left)


def test_a_caller_cannot_widen_the_time_budget(kms):
    far = kc._clock() + 10_000
    run = kd._Run(client(kms, "alice"), far)
    assert run.deadline <= run.started + kd._MAX_SECONDS


# ------------------------------------------------------------------ safety rule 5: one check does not stop the others
def test_an_unexpected_exception_is_that_checks_fail_with_the_type_name_only(kms, monkeypatch):
    real = requests.Session.request

    def boom(self, method, url, **kwargs):
        if "/search/full" in url:
            raise RuntimeError("kms-exception-text-e1 " + url)
        return real(self, method, url, **kwargs)

    monkeypatch.setattr(requests.Session, "request", boom)
    report = client(kms, "alice").diagnostics()
    rows = _rows(report)
    assert rows["search_layout"]["status"] == "fail"
    assert rows["search_layout"]["observed"] == {"error_type": "RuntimeError"}
    assert rows["field_types"]["status"] in ("pass", "warn"), "a later check still ran"
    assert report["failed_stage"] == "search"
    assert "kms-exception-text-e1" not in json.dumps(report)


def test_a_failed_read_does_not_stop_the_scenario_checks(kms, monkeypatch):
    kms["mock"].STATE["extra_results"] = [_scenario_hit("21302")]
    real = requests.Session.request

    def boom(self, method, url, **kwargs):
        if "/public/item/" in url:
            raise RuntimeError("kms-exception-text-e2")
        return real(self, method, url, **kwargs)

    monkeypatch.setattr(requests.Session, "request", boom)
    report = client(kms, "alice").diagnostics()
    rows = _rows(report)
    assert rows["field_types"]["status"] == "fail"
    assert rows["scenario_text"]["status"] != "skipped"
    assert "kms-exception-text-e2" not in json.dumps(report)


@pytest.mark.parametrize("make,marker", [(client, "/login"), (token_client, "/sessionInfo")])
def test_an_unexpected_exception_during_login_still_ends_the_run(kms, monkeypatch, make, marker):
    real = requests.Session.request

    def boom(self, method, url, **kwargs):
        if url.endswith(marker):
            raise RuntimeError("kms-exception-text-e3")
        return real(self, method, url, **kwargs)

    monkeypatch.setattr(requests.Session, "request", boom)
    report = make(kms, "alice").diagnostics()
    assert report["failed_stage"] == "login"
    assert _rows(report)["login"]["observed"] == {"error_type": "RuntimeError"}
    _assert_rest_skipped(report)
    assert "kms-exception-text-e3" not in json.dumps(report)


# ------------------------------------------------------------------ safety rule 1: read-only
def test_only_the_connectors_own_calls_go_out(kms):
    kms["mock"].STATE["extra_results"] = [_scenario_hit("21302")]
    client(kms, "alice", layout_id=7, root_folder_id=10).diagnostics()
    prefix = kms["mock"].PREFIX
    allowed = ("/cm/tree/branch/view", "/search/full", "/public/item/", "/ai_answers_item", "/public/scenario/")
    posts = [row for row in kms["mock"].REQUEST_LOG if row[1] == "POST"]
    assert posts == [("alice", "POST", prefix + "/login")]
    gets = [row[2] for row in kms["mock"].REQUEST_LOG if row[1] == "GET"]
    assert gets and all(path.startswith("/api/") or path[len(prefix):].startswith(allowed) for path in gets), gets


# ------------------------------------------------------------------ safety rule 4: no content leaves
def _everything_the_mock_could_say(mock):
    """Literal content of the mock tenant: titles, descriptions, bodies, names, passwords, tokens."""
    texts = [mock.ODD_MARKER, mock.RESTRICTED_MARKER, mock.CONTENT_JWT, mock.URL_TOKEN, mock.SENTINEL, mock.SENTINEL_KEY]
    texts += [node["title"] for node in mock.TREE.values()]
    texts += [record["description"] for record in mock.SEARCH_RESULTS if record["description"]]
    texts += [item["title"] for item in mock.ITEMS.values()] + [item["description"] for item in mock.ITEMS.values()]
    texts += list(mock.ARTICLE_SECRETS.values()) + list(mock.SCENARIO_SECRETS.values())
    for record in mock.USERS.values():
        texts += [record["password"], record["full_name"], record["session_username"]]
    texts += list(mock.TOKENS)
    return texts


def _assert_no_content(report, mock):
    dump = json.dumps(report)
    assert [text for text in _everything_the_mock_could_say(mock) if text in dump] == []
    assert '"alice"' not in dump and "identity" not in dump
    assert "sentinel" not in dump.lower()


@pytest.mark.parametrize("variant", [
    {}, {"plant_sentinels": True}, {"plant_sentinels": True, "login_mode": "both"}, {"odd_shapes": "list"},
    {"odd_shapes": "error"}, {"odd_shapes": "bad_id"}, {"odd_shapes": "bad_elements"}, {"odd_shapes": "bad_username"},
    {"ai_status": "not_json"}, {"ai_status": "error"}, {"ai_status": 500}, {"search_400": True},
])
def test_no_content_leaves_for_any_tenant_shape(kms, variant):
    mock = kms["mock"]
    mock.STATE.update(variant)
    mock.STATE["extra_results"] = [_scenario_hit("21302"), _scenario_hit("21400")]
    report = client(kms, "alice", layout_id=7, root_folder_id=10).diagnostics()
    _assert_plain_report(report)
    _assert_no_content(report, mock)


def test_sentinels_really_reached_the_checks(kms):
    """The sentinel test proves nothing unless the planted answers were read."""
    mock = kms["mock"]
    mock.STATE["plant_sentinels"] = True
    rows = _rows(client(kms, "alice").diagnostics())
    assert rows["top_level"]["observed"]["records"] > 0
    assert rows["field_types"]["observed"]["items_read"] > 0
    assert rows["login"]["observed"]["unlisted_names"] > 0, "the planted key name must be counted, never shown"
    assert rows["types"]["observed"]["search_sub_types"]["other"] > 0, "a subType with a space is no plain name"


def test_a_plain_word_type_label_is_shown_on_purpose(kms):
    """A one-word type or subType is a label, not content: it stays readable, and the guide says so."""
    kms["mock"].STATE["extra_results"] = [{"id": "7001", "subType": "T_ANY_PLAIN_WORD", "title": "x", "description": "",
                                           "statusCode": "2"}]
    observed = _rows(client(kms, "alice").diagnostics())["types"]["observed"]
    assert observed["search_sub_types"]["T_ANY_PLAIN_WORD"] == 1


def test_a_token_and_cookie_value_never_leave(kms):
    mock = kms["mock"]
    mock.STATE["login_mode"] = "both"
    dump = json.dumps(client(kms, "alice").diagnostics())
    assert mock._SESSIONS and mock._LOGIN_TOKENS
    assert not [value for value in [*mock._SESSIONS, *mock._LOGIN_TOKENS] if value in dump]


def test_observed_is_closed_by_default():
    """The one chokepoint: a string that is not a plain name never reaches a report."""
    dirty = {"name": "E_SUMMARY", "bad": "Reset a password", "nested": {"a b": 1, "ok": ["x y", "fine", True, 3]}, "n": None}
    assert kd._closed(dirty) == {"name": "E_SUMMARY", "bad": "other", "nested": {"ok": ["other", "fine", True, 3]}, "n": None}


# ------------------------------------------------------------------ safety rule 6: small, fixed sampling
def test_sampling_is_at_most_five_items_and_two_scenarios(kms):
    mock = kms["mock"]
    mock.STATE["extra_results"] = [_scenario_hit(str(6000 + n)) for n in range(4)] + [
        {"id": str(7000 + n), "subType": "T_PROCEDURE", "title": "x", "description": "", "statusCode": "2"} for n in range(9)]
    client(kms, "alice").diagnostics()
    prefix = mock.PREFIX
    items = {row[2] for row in mock.REQUEST_LOG if row[2].startswith(prefix + "/public/item/")}
    scenarios = [req["path"] for req in mock.STATE["scenario_requests"] if "/step/" not in req["path"]]
    assert 0 < len(items) <= 5
    assert 0 < len(set(scenarios)) <= 2


# ------------------------------------------------------------------ item 1: prefix
def test_prefix_is_probed_after_login_with_a_get_and_never_a_login(kms):
    mock = kms["mock"]
    mock.STATE["alt_prefix"] = "/api"
    rows = _rows(client(kms, "alice").diagnostics())
    assert rows["prefix"]["status"] == "info"
    assert rows["prefix"]["observed"]["alternatives"]["api"] == {"http_status": 200, "served": True}
    assert not [row for row in mock.REQUEST_LOG if row[1] == "POST" and row[2] != mock.PREFIX + "/login"]
    alt = [row for row in mock.REQUEST_LOG if row[2].startswith("/api/")]
    assert len(alt) == 1 and alt[0][1] == "GET" and alt[0][0] == "alice", "one authenticated GET"


def test_an_alternative_prefix_that_is_absent_says_so(kms):
    rows = _rows(client(kms, "alice").diagnostics())
    assert rows["prefix"]["observed"]["alternatives"]["api"] == {"http_status": 404, "served": False}


def test_no_prefix_probe_without_a_login(kms):
    client(kms, "alice", password="wrong-pass").diagnostics()
    assert not [row for row in kms["mock"].REQUEST_LOG if row[2].startswith("/api/")]


# ------------------------------------------------------------------ item 4: top level
def test_top_level_records_are_counted(kms):
    rows = _rows(client(kms, "alice").diagnostics())
    assert rows["top_level"]["status"] == "pass"
    assert rows["top_level"]["observed"]["records"] == sum(1 for n in kms["mock"].TREE.values() if n["parent"] is None)


def test_an_empty_top_level_warns(kms):
    kms["mock"].STATE["top_level_requires_parent"] = True
    rows = _rows(client(kms, "alice").diagnostics())
    assert rows["top_level"]["status"] == "warn" and rows["top_level"]["observed"]["records"] == 0


def test_an_empty_top_level_with_a_root_folder_still_samples_the_root(kms):
    kms["mock"].STATE["top_level_requires_parent"] = True
    rows = _rows(client(kms, "alice", root_folder_id=10).diagnostics())
    assert rows["top_level"]["observed"]["root_records"] > 0
    assert rows["field_types"]["observed"]["items_read"] > 0


@pytest.mark.parametrize("record,reason", [
    ({"id": "not-a-number", "type": "FOLDER", "title": "t"}, "non_numeric_id"),
    ({"type": "ITEM", "title": "no id"}, "non_numeric_id"),
    ("a string, not a record", "not_records"),
])
def test_a_tree_the_connector_would_refuse_fails_top_level(kms, monkeypatch, record, reason):
    mock = kms["mock"]
    good = {"id": "78", "type": "FOLDER", "title": "t"}
    real_send_json = mock.Handler._send_json

    def tree_with_a_bad_record(self, status, body, headers=None):
        if "/cm/tree/branch/view" in self.path:
            body = {"status": "SUCCESS", "data": [good, record]}
        return real_send_json(self, status, body, headers)

    monkeypatch.setattr(mock.Handler, "_send_json", tree_with_a_bad_record)
    report = client(kms, "alice").diagnostics()
    row = _rows(report)["top_level"]
    assert row["status"] == "fail" and row["observed"]["reason"] == reason
    assert report["failed_stage"] == "browse"


@pytest.mark.parametrize("results,reason", [
    ([{"id": "x-1", "title": "t"}], "non_numeric_id"),
    ([{"id": "279", "title": "t"}, "a string"], "not_records"),
])
def test_a_search_answer_the_connector_would_refuse_fails_search_layout(kms, results, reason):
    kms["mock"].STATE["search_body"] = {"regularSearch": {"results": results}}
    row = _rows(client(kms, "alice").diagnostics())["search_layout"]
    assert row["status"] == "fail" and row["observed"]["reason"] == reason


def test_a_folder_that_cannot_be_opened_warns(kms):
    kms["mock"].STATE["tree_status"] = {"78": 500}
    row = _rows(client(kms, "alice").diagnostics())["top_level"]
    assert row["status"] == "warn"
    assert row["observed"]["folder_statuses"]["http_500"] == 1 and row["observed"]["folder_statuses"]["http_200"] > 0


def test_no_folder_that_can_be_opened_fails(kms):
    mock = kms["mock"]
    mock.STATE["tree_status"] = {node_id: 503 for node_id, node in mock.TREE.items() if node["parent"] is None}
    report = client(kms, "alice").diagnostics()
    row = _rows(report)["top_level"]
    assert row["status"] == "fail" and report["failed_stage"] == "browse"
    assert set(row["observed"]["folder_statuses"]) == {"http_503"}


# ------------------------------------------------------------------ items 5, 6: types and id overlap
def test_type_and_subtype_counts_come_from_tree_and_search(kms):
    observed = _rows(client(kms, "alice").diagnostics())["types"]["observed"]
    assert observed["tree_types"]["FOLDER"] > 0
    assert observed["search_sub_types"]["T_PROCEDURE"] >= 2
    assert observed["search_sub_types"]["T_SCENARIO_FLOW"] == 1


def test_id_overlap_is_skipped_when_the_sample_holds_no_id(kms):
    mock = kms["mock"]
    mock.STATE["no_hit_text"] = ""
    mock.STATE["top_level_requires_parent"] = True
    row = _rows(client(kms, "alice").diagnostics())["id_overlap"]
    assert row["status"] == "skipped"


def test_a_shared_id_between_item_and_scenario_is_counted(kms):
    kms["mock"].STATE["extra_results"] = [_scenario_hit("279")]
    observed = _rows(client(kms, "alice").diagnostics())["id_overlap"]["observed"]
    assert observed["item_ids"] > 0 and observed["scenario_ids"] > 0
    assert observed["shared_ids"] == 1


# ------------------------------------------------------------------ item 7: date shape
@pytest.mark.parametrize("date,status,first,second", [
    ("25/04/2024", "pass", True, False),
    ("04/25/2024", "warn", False, True),
])
def test_date_shape_says_which_field_goes_over_twelve(kms, date, status, first, second):
    kms["mock"].STATE["extra_results"] = [{"id": "7001", "subType": "T_PROCEDURE", "title": "x", "description": "",
                                           "statusCode": "2", "updateDate": date}]
    row = _rows(client(kms, "alice").diagnostics())["dates"]
    assert row["status"] == status
    assert row["observed"]["first_over_12"] is first and row["observed"]["second_over_12"] is second


def test_dates_that_never_go_over_twelve_cannot_tell(kms):
    row = _rows(client(kms, "alice").diagnostics())["dates"]
    assert row["status"] == "info" and row["observed"]["day_month_dates"] > 0
    assert row["observed"]["first_over_12"] is False and row["observed"]["second_over_12"] is False


# ------------------------------------------------------------------ item 8: layoutId
def test_search_runs_with_and_without_a_layout_when_one_is_set(kms):
    kms["mock"].STATE["search_requires_layout"] = True
    row = _rows(client(kms, "alice", layout_id=7).diagnostics())["search_layout"]
    assert row["status"] == "pass"
    assert row["observed"]["with_layout_status"] == 200 and row["observed"]["without_layout_status"] == 400
    assert row["observed"]["layout_required"] is True


def test_search_without_a_layout_runs_once(kms):
    row = _rows(client(kms, "alice").diagnostics())["search_layout"]
    assert row["status"] == "pass" and row["observed"]["without_layout_status"] == 200
    assert "with_layout_status" not in row["observed"]


def test_a_rejected_search_is_a_search_failure(kms):
    kms["mock"].STATE["search_400"] = True
    report = client(kms, "alice").diagnostics()
    assert _rows(report)["search_layout"]["status"] == "fail" and report["failed_stage"] == "search"


def test_a_search_with_no_hits_warns_after_a_few_fixed_queries(kms):
    kms["mock"].STATE["no_hit_text"] = ""
    row = _rows(client(kms, "alice").diagnostics())["search_layout"]
    assert row["status"] == "warn" and row["observed"]["without_layout_rows"] == 0
    assert row["observed"]["queries_tried"] == len(kd._SAMPLE_QUERIES) <= 3


@pytest.mark.parametrize("total_key", ["total", "totalResults", "numFound"])
def test_a_stated_zero_total_is_no_hits_not_a_failed_search(kms, total_key):
    kms["mock"].STATE["search_body"] = {"regularSearch": {total_key: 0}}
    row = _rows(client(kms, "alice").diagnostics())["search_layout"]
    assert row["status"] == "warn" and row["observed"]["without_layout_rows"] == 0
    assert row["observed"]["queries_tried"] == len(kd._SAMPLE_QUERIES), "every fixed query is still tried"


def test_a_search_answer_with_no_results_and_no_total_is_a_failure(kms):
    kms["mock"].STATE["search_body"] = {"regularSearch": {"query": "x"}}
    assert _rows(client(kms, "alice").diagnostics())["search_layout"]["status"] == "fail"


def test_layout_required_needs_a_usable_answer_with_the_layout_and_a_refusal_without_it(kms):
    kms["mock"].STATE["search_requires_layout"] = "error"
    row = _rows(client(kms, "alice", layout_id=7).diagnostics())["search_layout"]
    assert row["observed"]["without_layout_status"] == 200, "a 200 ERROR is a refusal too"
    assert row["observed"]["layout_required"] is True


def test_an_error_answer_with_the_layout_is_not_proof_the_layout_is_required(kms, monkeypatch):
    mock = kms["mock"]
    real_send_json = mock.Handler._send_json

    def error_with_a_layout_and_500_without(self, status, body, headers=None):
        if "/search/full" in self.path and "layoutId=" in self.path:
            body = {"status": "ERROR"}
        elif "/search/full" in self.path:
            return self._send_empty(500)
        return real_send_json(self, status, body, headers)

    monkeypatch.setattr(mock.Handler, "_send_json", error_with_a_layout_and_500_without)
    row = _rows(client(kms, "alice", layout_id=7).diagnostics())["search_layout"]
    assert row["observed"]["with_layout_status"] == 200 and row["observed"]["without_layout_status"] == 500
    assert row["observed"]["layout_required"] is False


# ------------------------------------------------------------------ item 9: longest query
@pytest.mark.parametrize("limit,accepted,status", [(10_000, 1000, "pass"), (700, 500, "pass"), (300, 200, "pass"),
                                                   (150, 100, "warn"), (60, 50, "warn"), (20, None, "warn")])
def test_longest_query_takes_at_most_three_calls(kms, limit, accepted, status):
    kms["mock"].STATE["search_max_query"] = limit
    row = _rows(client(kms, "alice").diagnostics())["query_length"]
    assert row["status"] == status and row["observed"]["longest_accepted"] == accepted
    assert row["observed"]["connector_limit"] == kc._MAX_QUERY_CHARS
    assert row["observed"]["calls"] <= 3


@pytest.mark.parametrize("rejection", [400, 413, 414])
def test_each_rejection_status_counts_as_rejected(kms, rejection):
    kms["mock"].STATE.update({"search_max_query": 100, "search_too_long": rejection})
    row = _rows(client(kms, "alice").diagnostics())["query_length"]
    assert row["status"] == "warn" and row["observed"]["inconclusive"] is False
    assert row["observed"]["longest_accepted"] == 100 and row["observed"]["shortest_rejected"] == 200


@pytest.mark.parametrize("answer,status_key", [(500, "http_500"), (503, "http_503"), (403, "http_403"),
                                               ("error", "http_200")])
@pytest.mark.parametrize("over", [100, 300])
def test_any_other_answer_to_a_long_query_is_inconclusive_not_accepted_or_rejected(kms, over, answer, status_key):
    kms["mock"].STATE.update({"search_max_query": over, "search_too_long": answer})
    row = _rows(client(kms, "alice").diagnostics())["query_length"]
    assert row["observed"]["shortest_rejected"] is None and row["status"] == "warn"
    assert row["observed"]["inconclusive"] is True
    assert row["observed"]["statuses"][status_key] >= 1
    assert row["observed"]["longest_accepted"] == (None if over < kc._MAX_QUERY_CHARS else kc._MAX_QUERY_CHARS)


# ------------------------------------------------------------------ item 17: zero hits
def test_a_zero_hit_answer_reports_status_and_key_names_only(kms):
    mock = kms["mock"]
    mock.STATE.update({"no_hit_text": "nohit", "no_hit_body": {
        "regularSearch": {"query": "x", "numFound": 0}, "suggestion": "a tenant sentence"}})
    row = _rows(client(kms, "alice").diagnostics())["zero_hit"]
    assert row["status"] == "pass"
    assert row["observed"]["http_status"] == 200
    assert row["observed"]["top_level_keys"] == ["regularSearch", "suggestion"]
    assert row["observed"]["search_block_keys"] == ["numFound", "query"]
    assert row["observed"]["understood"] is True
    assert "a tenant sentence" not in json.dumps(row)


def test_a_zero_hit_answer_the_connector_cannot_read_warns(kms):
    mock = kms["mock"]
    mock.STATE.update({"no_hit_text": "nohit", "no_hit_body": {"regularSearch": {"query": "x", "hits": 0}}})
    row = _rows(client(kms, "alice").diagnostics())["zero_hit"]
    assert row["status"] == "warn" and row["observed"]["understood"] is False


# ------------------------------------------------------------------ item 18: rows per page
def test_a_server_side_row_cap_is_found(kms):
    kms["mock"].STATE["search_max_rows"] = 3
    row = _rows(client(kms, "alice").diagnostics())["page_rows"]
    assert row["status"] == "warn"
    assert row["observed"] == {"rows_requested": 50, "rows_returned": 3, "next_page_rows": 3, "server_cap": True}


def test_a_failed_next_page_warns_with_its_status(kms):
    kms["mock"].STATE["search_page_status"] = 502
    row = _rows(client(kms, "alice").diagnostics())["page_rows"]
    assert row["status"] == "warn"
    assert row["observed"]["next_page_status"] == 502 and row["observed"]["server_cap"] is None


def test_a_short_last_page_is_no_cap(kms):
    row = _rows(client(kms, "alice").diagnostics())["page_rows"]
    assert row["status"] == "pass" and row["observed"]["server_cap"] is False


def test_a_full_page_is_no_cap(kms):
    kms["mock"].STATE["extra_results"] = [
        {"id": str(7000 + n), "subType": "T_PROCEDURE", "title": "x", "description": "", "statusCode": "2"} for n in range(60)]
    observed = _rows(client(kms, "alice").diagnostics())["page_rows"]["observed"]
    assert observed["rows_returned"] == 50 and observed["server_cap"] is False


# ------------------------------------------------------------------ item 11: field types
def test_python_type_names_of_tab_ids(kms):
    observed = _rows(client(kms, "alice").diagnostics())["field_types"]["observed"]
    assert observed["tabs_id_types"] == {"int": 3}, "two tabs on item 279, one on 301"
    assert observed["element_tab_id_types"]["str"] > 0
    assert observed["element_tab_id_types"]["NoneType"] > 0 and observed["element_tab_id_types"]["int"] > 0


def test_an_item_that_could_not_be_read_warns(kms):
    """The mock's search holds ids with no /public/item body."""
    row = _rows(client(kms, "alice").diagnostics())["field_types"]
    assert row["observed"]["items_read"] < row["observed"]["items_tried"]
    assert row["status"] == "warn"


def _only_item(mock, monkeypatch, **fields):
    monkeypatch.setattr(mock, "SEARCH_RESULTS", [
        {"id": "279", "subType": "T_PROCEDURE", "title": "t", "description": "", "statusCode": "2"}])
    monkeypatch.setitem(mock.ITEMS, "279", {**mock.ITEMS["279"], **fields})


def test_items_with_no_tab_ids_warn(kms, monkeypatch):
    _only_item(kms["mock"], monkeypatch, tabs=[], elements={})
    row = _rows(client(kms, "svc", root_folder_id=95).diagnostics())["field_types"]
    assert row["observed"]["items_read"] == row["observed"]["items_tried"] == 1
    assert row["observed"]["tabs_id_types"] == {} and row["observed"]["element_tab_id_types"] == {}
    assert row["status"] == "warn"


def test_every_item_read_with_tab_ids_passes(kms, monkeypatch):
    _only_item(kms["mock"], monkeypatch)
    row = _rows(client(kms, "svc", root_folder_id=95).diagnostics())["field_types"]
    assert row["status"] == "pass"


def test_no_readable_item_fails_the_read_stage(kms, monkeypatch):
    monkeypatch.setattr(kms["mock"], "ITEMS", {})
    report = client(kms, "alice").diagnostics()
    row = _rows(report)["field_types"]
    assert row["status"] == "fail" and row["observed"]["items_read"] == 0 and row["observed"]["items_tried"] > 0
    assert report["failed_stage"] == "read"


# ------------------------------------------------------------------ item 12: never a pass
def test_ai_elements_warn_and_tell_to_repeat_as_a_restricted_user(kms):
    row = _rows(client(kms, "alice").diagnostics())["ai_elements"]
    assert row["status"] == "warn"
    assert "restricted" in row["summary"].lower()
    assert "E_RESTRICTED" in row["observed"]["only_in_ai"]
    assert "E_SCREEN" in row["observed"]["only_in_item"]
    assert row["observed"]["common"] > 0


@pytest.mark.parametrize("variant", [{"ai_text_available": False}, {"ai_status": 500}])
def test_ai_elements_never_pass_when_the_module_is_absent(kms, variant):
    kms["mock"].STATE.update(variant)
    assert _rows(client(kms, "alice").diagnostics())["ai_elements"]["status"] == "warn"


def test_ai_elements_never_pass_when_the_sets_are_equal(kms, monkeypatch):
    mock = kms["mock"]
    only = {"id": 301, "version": 1, "language": "en", "title": "t", "status": 2, "tabs": [],
            "elements": {"E_SUMMARY": {"value": "<p>x</p>", "tabId": "1"}}}
    monkeypatch.setattr(mock, "ITEMS", {"279": {**only, "id": 279}, "301": only})
    monkeypatch.setitem(mock.AI_ITEMS, "279", {**mock.AI_ITEMS["301"], "item_id": 279, "version": 1})
    row = _rows(client(kms, "alice").diagnostics())["ai_elements"]
    assert row["observed"]["only_in_ai"] == [] and row["observed"]["only_in_item"] == []
    assert row["status"] == "warn"


# ------------------------------------------------------------------ item 14: AI text
def test_ai_text_counts_plain_and_ambiguous_as_numbers_only(kms, monkeypatch):
    mock = kms["mock"]
    body = {**mock.AI_ITEMS["301"], "elements": [mock._ai_element("E_SUMMARY", "t", "c", "<b>marked up</b>"),
                                                 mock._ai_element("E_NOTES", "t", "c", "plain words")]}
    monkeypatch.setitem(mock.AI_ITEMS, "301", body)
    row = _rows(client(kms, "alice").diagnostics())["ai_text"]
    assert row["status"] == "warn"
    assert row["observed"]["ambiguous"] == 1 and row["observed"]["plain"] >= 1
    assert "marked up" not in json.dumps(row)


@pytest.mark.parametrize("status", [403, 404])
def test_an_absent_ai_module_reports_its_status(kms, status):
    kms["mock"].STATE["ai_status"] = status
    row = _rows(client(kms, "alice").diagnostics())["ai_text"]
    assert row["status"] == "warn" and row["observed"]["statuses"] == {f"http_{status}": 2}


def test_plain_ai_text_passes(kms):
    assert _rows(client(kms, "alice").diagnostics())["ai_text"]["status"] == "pass"


def _ai_elements(mock, monkeypatch, element):
    for item in ("301", "279"):
        monkeypatch.setitem(mock.AI_ITEMS, item, {**mock.AI_ITEMS[item], "elements": [element]})


@pytest.mark.parametrize("element", [
    {"element_name": "E_SUMMARY", "element_text": ""},
    {"element_name": "E_SUMMARY", "element_text": None},
    {"element_name": "E_SUMMARY", "body": "words"},
    {"caption": "c", "text": "words"},
])
def test_ai_text_with_nothing_plain_never_passes(kms, monkeypatch, element):
    _ai_elements(kms["mock"], monkeypatch, element)
    row = _rows(client(kms, "alice").diagnostics())["ai_text"]
    assert row["status"] == "warn" and row["observed"]["plain"] == 0


def test_ai_elements_with_no_text_key_report_their_key_names(kms, monkeypatch):
    _ai_elements(kms["mock"], monkeypatch, {"name": "E_SUMMARY", "text": "words", "a key with spaces": 1})
    row = _rows(client(kms, "alice").diagnostics())["ai_text"]
    assert row["status"] == "warn"
    assert row["observed"]["element_keys"] == ["name", "text"] and row["observed"]["unlisted_names"] == 1


def test_ai_elements_with_their_keys_report_no_key_names(kms):
    assert "element_keys" not in _rows(client(kms, "alice").diagnostics())["ai_text"]["observed"]


# ------------------------------------------------------------------ items 15, 16: scenarios
def test_scenario_text_counts_tags_and_entities(kms, monkeypatch):
    mock = kms["mock"]
    mock.STATE["extra_results"] = [_scenario_hit("21302")]
    marked = mock._scenario(21302, "t", mock._step(23505, name="<b>Have you</b>", text="fish &amp; chips",
                                                   decisions={"21302:2:23506": "Yes"}))
    monkeypatch.setitem(mock.SCENARIOS, "21302", marked)
    row = _rows(client(kms, "alice").diagnostics())["scenario_text"]
    assert row["status"] == "warn" and row["observed"]["scenarios_read"] == 1
    assert row["observed"]["with_tags"] == 1 and row["observed"]["with_entities"] == 1


def _only_scenario(kms):
    """Scenario 21302 is the first scenario in the sample (the mock's own hits have no scenario body)."""
    mock = kms["mock"]
    mock.STATE["extra_results"] = [_scenario_hit("21302")]
    return client(kms, "svc", root_folder_id=78)


def test_scenario_steps_with_no_text_warn_and_name_their_keys(kms, monkeypatch):
    mock = kms["mock"]
    monkeypatch.setattr(mock, "SEARCH_RESULTS", [])
    monkeypatch.setitem(mock.SCENARIOS, "21302", mock._scenario(21302, "t", {"caption": "words", "nextNode": 1}))
    row = _rows(_only_scenario(kms).diagnostics())["scenario_text"]
    assert row["status"] == "warn"
    assert row["observed"]["strings"] == 0 and row["observed"]["scenarios_read"] == 1
    assert row["observed"]["step_keys"] == ["caption", "nextNode"]


def test_plain_scenario_text_names_no_step_keys(kms, monkeypatch):
    monkeypatch.setattr(kms["mock"], "SEARCH_RESULTS", [])
    assert "step_keys" not in _rows(_only_scenario(kms).diagnostics())["scenario_text"]["observed"]


def test_no_scenario_read_skips_the_step_check(kms):
    """The mock's search holds a scenario id with no body, so one is sampled and none is read."""
    rows = _rows(client(kms, "alice").diagnostics())
    assert rows["scenario_text"]["observed"]["scenarios_tried"] > 0
    assert rows["scenario_text"]["observed"]["scenarios_read"] == 0
    assert rows["scenario_steps"]["status"] == "skipped"


def test_plain_scenario_text_passes(kms, monkeypatch):
    monkeypatch.setattr(kms["mock"], "SEARCH_RESULTS", [])
    row = _rows(_only_scenario(kms).diagnostics())["scenario_text"]
    assert row["status"] == "pass" and row["observed"]["strings"] > 0


def test_no_scenario_in_the_sample_is_skipped_not_passed(kms, monkeypatch):
    mock = kms["mock"]
    monkeypatch.setattr(mock, "SEARCH_RESULTS", [r for r in mock.SEARCH_RESULTS if "SCENARIO" not in r.get("subType", "")])
    rows = _rows(client(kms, "bob", root_folder_id=78).diagnostics())
    assert rows["scenario_text"]["status"] == "skipped" and rows["scenario_steps"]["status"] == "skipped"


def test_step_fetch_statuses_are_counted(kms, monkeypatch):
    monkeypatch.setattr(kms["mock"], "SEARCH_RESULTS", [])
    row = _rows(_only_scenario(kms).diagnostics())["scenario_steps"]
    assert row["status"] == "pass" and row["observed"]["statuses"] == {"http_200": 1}


def test_a_406_on_a_step_warns(kms, monkeypatch):
    monkeypatch.setattr(kms["mock"], "SEARCH_RESULTS", [])
    probe = _only_scenario(kms)
    kms["mock"].STATE["step_status"] = {"23506": 406}
    row = _rows(probe.diagnostics())["scenario_steps"]
    assert row["status"] == "warn" and row["observed"]["statuses"] == {"http_406": 1}


def test_step_fetches_are_bounded(kms, monkeypatch):
    mock = kms["mock"]
    monkeypatch.setattr(mock, "SEARCH_RESULTS", [])
    mock.STATE["extra_results"] = [_scenario_hit("21501")]
    row = _rows(client(kms, "svc", root_folder_id=78).diagnostics())["scenario_steps"]
    assert sum(row["observed"]["statuses"].values()) == kd._STEP_FETCHES


# ------------------------------------------------------------------ a report without a client
@pytest.mark.parametrize("config,host", [
    ({"base_url": "https://kms.example.com/kms/lh/api", "auth_type": "token", "root_folder_id": 4}, "kms.example.com"),
    ({"base_url": "https://kms.example.com:8443"}, "kms.example.com:8443"),
    ({"base_url": "https://[::1]:8443"}, "[::1]:8443"),
    ({"base_url": "https://user:secret@kms.example.com"}, ""),
    ({"base_url": "https://svc:secret/ss@kms.example.com"}, ""),
    ({"base_url": "https://svc:secret?cd@kms.example.com"}, ""),
    ({"base_url": "https://svc:secret"}, ""),
    ({"base_url": "https://secret@"}, ""),
    ({"base_url": 12}, ""),
    ({}, ""),
])
def test_a_failure_report_has_the_same_shape_and_no_secret(config, host):
    report = kd.failure_report(config, stage="login", reason="credentials_unavailable", error_type="CredentialsRequiredError")
    _assert_plain_report(report)
    assert report["overall"] == "fail" and report["failed_stage"] == "login" and report["calls_made"] == 0
    assert report["host"] == host and "secret" not in json.dumps(report)
    rows = _rows(report)
    assert rows["login"]["observed"] == {"reason": "credentials_unavailable", "error_type": "CredentialsRequiredError"}
    assert {item for row in report["checks"] for item in row["checklist"]} == _CHECKLIST
    assert {row["status"] for row in report["checks"]} == {"fail", "skipped"}


@pytest.mark.parametrize("prefix", ["https://svc:secret@kms.example.com/api", "/kms/secret value", "/kms?token=secret",
                                    "secret", 12])
def test_a_failure_report_never_copies_an_api_prefix_that_is_not_a_plain_path(prefix):
    report = kd.failure_report({"base_url": "https://kms.example.com", "api_prefix": prefix}, stage="login",
                               reason="credentials_unavailable", error_type="CredentialsRequiredError")
    assert report["api_prefix"] == kc._DEFAULT_PREFIX
    assert "secret" not in json.dumps(report)


@pytest.mark.parametrize("prefix", ["https://svc:secret@kms.example.com/api", "/kms/secret value", "/kms?token=secret"])
def test_a_client_is_never_built_with_an_api_prefix_that_is_not_a_plain_path(prefix):
    with pytest.raises(kc.KmsError):
        kc.KmsLighthouseClient(base_url="https://kms.example.com", api_prefix=prefix, username="u", password="p")


@pytest.mark.parametrize("config", [{"base_url": "https://kms.example.com"}, '{"base_url": "https://kms.example.com"}'])
def test_a_run_past_the_route_limit_is_a_network_failure_report(config):
    report = kd.timed_out_report(config)
    _assert_plain_report(report)
    assert report["failed_stage"] == "network" and report["overall"] == "fail"
    assert _rows(report)["network"]["observed"]["reason"] == "run_timed_out"
    assert report["host"] == "kms.example.com"


def test_the_route_limit_leaves_the_run_its_own_budget():
    assert kd.route_seconds() == kd._MAX_SECONDS + 5


def test_a_blocked_destination_is_a_network_failure_report():
    report = kd.failure_report({"base_url": "https://169.254.169.254"}, stage="network", reason="egress_blocked",
                               error_type="UnsafeURLError")
    assert report["failed_stage"] == "network"
    assert _rows(report)["network"]["observed"]["reason"] == "egress_blocked"


# ------------------------------------------------------------------ frontend wiring (read from source; no browser here)
def _flat(tree, prefix=""):
    found = {}
    for key, value in tree.items():
        found.update(_flat(value, f"{prefix}{key}.") if isinstance(value, dict) else {f"{prefix}{key}": value})
    return found


def test_all_ten_catalogs_hold_the_same_diagnostics_strings():
    assert len(_LOCALES) == 10
    english = _flat(json.loads((_REPO / "locales" / "en.json").read_text(encoding="utf-8"))["kmsDiagnostics"])
    for path in _LOCALES:
        catalog = json.loads(path.read_text(encoding="utf-8"))
        strings = _flat(catalog["kmsDiagnostics"])
        assert set(strings) == set(english), path.name
        assert all(value.strip() for value in strings.values()), path.name
        for key, value in english.items():
            assert set(re.findall(r"\{\w+\}", strings[key])) == set(re.findall(r"\{\w+\}", value)), (path.name, key)
        assert catalog["errors"]["connection.kms_only"].strip(), path.name
        if path.stem != "en":
            assert strings["run"] != english["run"], f"{path.name} is not translated"


def test_every_diagnostics_key_the_panel_uses_exists_and_every_key_is_used():
    english = _flat(json.loads((_REPO / "locales" / "en.json").read_text(encoding="utf-8"))["kmsDiagnostics"])
    panel = _PANEL.read_text(encoding="utf-8")
    used = {key for key in re.findall(r"kmsDiagnostics\.([A-Za-z.]+)", panel) if not key.endswith(".")}
    dynamic = {f"status.{status}" for status in _STATUSES} | {f"stage.{stage}" for stage in _STAGES}
    assert used - set(english) == set(), sorted(used - set(english))
    assert set(english) - used - dynamic == set(), sorted(set(english) - used)
    assert "`kmsDiagnostics.status.${" in panel and "`kmsDiagnostics.stage.${" in panel


def test_the_panel_shows_for_kms_managers_only():
    page = _PAGE.read_text(encoding="utf-8")
    mount = re.search(r"<KmsDiagnosticsPanel\b[^>]*>", page, re.S)
    assert mount, "the page must mount the panel"
    assert "canManageConnections" in mount.group(0) and "kms_lighthouse" in mount.group(0)
    assert ':connection-id="connectionId"' in mount.group(0)


def test_the_panel_calls_the_route_and_reports_errors_like_the_page():
    panel = _PANEL.read_text(encoding="utf-8")
    assert "/kms-diagnostics`" in panel and "method: 'POST'" in panel
    assert "getErrorMessage(" in panel
    assert "writeText(" in panel and "createObjectURL(" in panel
    assert 'role="alert"' in panel


def test_the_panel_refuses_an_answer_that_is_not_a_report():
    panel = _PANEL.read_text(encoding="utf-8")
    run = panel[panel.index("async function runDiagnostics"):panel.index("const reportText")]
    assert "answer?.report_version" in run and "Array.isArray(answer.checks)" in run, run
    assert re.search(r"else errorMessage\.value = t\('kmsDiagnostics\.failed'\)", run), run


def test_the_panel_states_the_manual_check_that_remains():
    panel = _PANEL.read_text(encoding="utf-8")
    template = panel[:panel.index("<script")]
    assert re.search(r'v-if="report\.manual_checks\?\.length"[^>]*>\s*\{\{ \$t\(\'kmsDiagnostics\.manualCheck\'\) \}\}',
                     template, re.S), "the panel must say one manual check remains"


@pytest.mark.parametrize("status,color", [("pass", "green"), ("fail", "red"), ("warn", "amber")])
def test_the_status_pills_keep_their_text_readable_in_dark_mode(status, color):
    panel = _PANEL.read_text(encoding="utf-8")
    pill = re.search(rf"status === '{status}'\) return '([^']+)'", panel)
    assert pill and f"text-{color}-700" in pill.group(1) and f"dark:text-{color}-400" in pill.group(1)


def test_the_panel_works_right_to_left():
    panel = _PANEL.read_text(encoding="utf-8")
    template = panel[:panel.index("<script")]
    physical = re.findall(r"\b(?:ml|mr|pl|pr|left|right|text-left|text-right|border-l|border-r)-[\w\[]+", template)
    assert physical == []
    assert 'dir="ltr"' in template, "the English report text keeps its own direction"
    assert not re.search(r"dark:text-|text-gray-", template), "semantic text classes flip with the theme"


# ------------------------------------------------------------------ docs
def test_the_connector_guide_covers_the_diagnostics():
    guide = _GUIDE.read_text(encoding="utf-8")
    section = guide[guide.index("## On-site diagnostics"):]
    section = section[:section.index("\n## ", 5)]
    for stage in _STAGES:
        assert f"`{stage}`" in section, stage
    assert "/kms-diagnostics" in section and "60 calls" in section and "90 s" in section
    assert "restricted" in section.lower()
    assert "proxy" in guide[guide.index("## Known limitations and follow-ups"):].lower()
    assert "manual_checks" in section, "what a clean run means"
    for word in ("session_refused", "blocked_route", "ca_bundle_unreadable", "bad_body", "run_timed_out"):
        assert f"`{word}`" in section, word
    assert re.search(r"gateway.*60 s.*read limit|60 s read limit.*gateway", section, re.S), "a stalled tenant behind a gateway"


def test_the_feedback_loop_maps_every_checklist_item_to_a_check():
    loop = _LOOP.read_text(encoding="utf-8")
    assert "| P12 |" in loop
    table = loop[loop.index("## Checklist items and diagnostics checks"):]
    for check_id, checklist, *_ in kd._CHECKS:
        assert f"`{check_id}`" in table, check_id
        for item in checklist:
            assert re.search(rf"^\| {item} \| `{check_id}` \|", table, re.M), (item, check_id)
