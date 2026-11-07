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
                "overall", "failed_stage", "manual_checks", "calls_made", "calls_unknown", "duration_ms", "checks"}
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
def test_a_healthy_tenant_answers_every_checklist_item(kms, monkeypatch):
    _clean_tenant(kms, monkeypatch)
    report = _clean_client(kms).diagnostics()
    _assert_plain_report(report)
    assert report["overall"] == "pass"
    assert report["failed_stage"] is None
    assert report["host"] == kms["base_url"].split("//")[1]
    assert report["api_prefix"] == kms["mock"].PREFIX
    assert report["auth_method"] == "userpass" and report["root_folder_set"] is True
    assert report["bow_version"] and report["generated_at"].endswith("+00:00")
    assert report["calls_made"] == len(kms["mock"].REQUEST_LOG) and report["duration_ms"] >= 0
    assert {item for row in report["checks"] for item in row["checklist"]} == _CHECKLIST
    assert not [row for row in report["checks"] if row["status"] == "fail"]


_CLEAN_ROOT = 78     # the Accounts folder: item 279 and nothing else


def _clean_tenant(kms, monkeypatch):
    """Every automatic check has something to find and finds nothing wrong."""
    mock = kms["mock"]
    monkeypatch.setattr(mock, "SEARCH_RESULTS", [
        {"id": "279", "subType": "T_PROCEDURE", "title": "t", "description": "", "statusCode": "2"}])
    mock.STATE["extra_results"] = [_scenario_hit("21302")]
    mock.STATE["no_hit_text"] = kd._NO_HIT_QUERY


def _clean_client(kms):
    return client(kms, "svc", root_folder_id=_CLEAN_ROOT)


def test_a_clean_automatic_run_passes_with_one_manual_check_left(kms, monkeypatch):
    _clean_tenant(kms, monkeypatch)
    report = _clean_client(kms).diagnostics()
    rows = _rows(report)
    assert report["overall"] == "pass", [(row["id"], row["status"]) for row in report["checks"]
                                         if row["status"] not in ("pass", "info")]
    assert report["failed_stage"] is None and report["manual_checks"] == ["ai_elements"]
    assert rows["ai_elements"]["status"] == "warn", "item 12 still cannot pass from one account"
    assert rows["tls"]["status"] == "skipped" and rows["concurrent_sessions"]["status"] == "skipped"


def test_a_warning_other_than_the_manual_check_lowers_overall(kms, monkeypatch):
    _clean_tenant(kms, monkeypatch)
    kms["mock"].STATE["search_max_rows"] = 1
    report = _clean_client(kms).diagnostics()
    assert _rows(report)["page_rows"]["status"] == "warn" and report["overall"] == "warn"


@pytest.mark.parametrize("variant,skipped", [
    ({"extra_results": []}, "scenario_text"),
    ({"ai_status": "error"}, None),
])
def test_a_skipped_automatic_check_lowers_overall(kms, monkeypatch, variant, skipped):
    _clean_tenant(kms, monkeypatch)
    kms["mock"].STATE.update(variant)
    report = _clean_client(kms).diagnostics()
    assert report["overall"] == "warn"
    if skipped:
        assert _rows(report)[skipped]["status"] == "skipped"


def test_ai_elements_skipped_lowers_overall(kms, monkeypatch):
    _clean_tenant(kms, monkeypatch)
    unversioned = {key: value for key, value in kms["mock"].ITEMS["279"].items() if key != "version"}
    monkeypatch.setitem(kms["mock"].ITEMS, "279", unversioned)
    report = _clean_client(kms).diagnostics()
    assert _rows(report)["ai_elements"]["status"] == "skipped"
    assert report["overall"] == "warn" and report["manual_checks"] == []


# ------------------------------------------------------------------ the connector's own proof
_PROOF_ROWS = ("connector_browse", "connector_search", "connector_read")


def _failure_kind(call, *args):
    with pytest.raises(kc.KmsHTTPError) as caught:
        call(*args)
    return caught.value.kind


def _offline_item(kms, monkeypatch):
    monkeypatch.setitem(kms["mock"].ITEMS, "279", {**kms["mock"].ITEMS["279"], "status": 1})


def _scenario_with_no_id(kms, monkeypatch):
    monkeypatch.setitem(kms["mock"].SCENARIOS, "21302", {**kms["mock"].SCENARIOS["21302"], "id": None})


def _tabs_with_no_elements(kms, monkeypatch):
    monkeypatch.setitem(kms["mock"].ITEMS, "279", {**kms["mock"].ITEMS["279"], "elements": {}})


def _every_hit_hidden(kms, monkeypatch):
    mock = kms["mock"]
    monkeypatch.setattr(mock, "SEARCH_RESULTS", [{**mock.SEARCH_RESULTS[0], "statusCode": "1"}])
    mock.STATE["extra_results"] = [{**_scenario_hit("21302"), "statusCode": "1"}]


def _hidden_top_folder(kms, monkeypatch):
    mock = kms["mock"]
    monkeypatch.setattr(mock, "TREE", {"78": {**mock.TREE["78"], "status": "1"}, "279": mock.TREE["279"]})


def _nothing(_kms, _monkeypatch):
    """A tenant fault that is only the root folder setting."""


@pytest.mark.parametrize("fault,root,row_id,status,connector_says", [
    (_offline_item, _CLEAN_ROOT, "connector_read", "warn",
     lambda probe: _failure_kind(probe.read_file, "item:279") == "not_found"),
    (_scenario_with_no_id, _CLEAN_ROOT, "connector_read", "warn",
     lambda probe: _failure_kind(probe.read_file, "scenario:21302") == "server"),
    (_tabs_with_no_elements, _CLEAN_ROOT, "connector_read", "warn",
     lambda probe: "\n\n" not in probe.read_file("item:279")),
    (_every_hit_hidden, _CLEAN_ROOT, "connector_search", "warn", lambda probe: probe.search_files("a") == []),
    (_hidden_top_folder, None, "connector_browse", "warn", lambda probe: probe.list_files() == []),
    (_nothing, 95, "connector_browse", "warn", lambda probe: probe.list_files() == []),
])
def test_a_tenant_the_connector_cannot_use_never_passes(kms, monkeypatch, fault, root, row_id, status, connector_says):
    """Each report sits beside what the same connector's public method does with that tenant."""
    _clean_tenant(kms, monkeypatch)
    fault(kms, monkeypatch)
    report = client(kms, "svc", root_folder_id=root).diagnostics()
    assert report["overall"] != "pass", [(row["id"], row["status"]) for row in report["checks"]]
    assert _rows(report)[row_id]["status"] == status
    assert connector_says(client(kms, "svc", root_folder_id=root))


def test_a_clean_tenant_is_proved_by_the_connector_itself(kms, monkeypatch):
    _clean_tenant(kms, monkeypatch)
    probe = _clean_client(kms)
    report = probe.diagnostics()
    rows = _rows(report)
    assert report["overall"] == "pass"
    assert [rows[row_id]["status"] for row_id in _PROOF_ROWS] == ["pass", "pass", "pass"]
    assert [rows[row_id]["stage"] for row_id in _PROOF_ROWS] == ["browse", "search", "read"]
    assert rows["connector_browse"]["observed"]["entries"] == len(_clean_client(kms).list_files()) == 1
    assert rows["connector_search"]["observed"]["entries"] == len(_clean_client(kms).search_files("a")) == 2
    assert rows["connector_read"]["observed"]["reads"] == rows["connector_read"]["observed"]["usable"] == 2


def test_overall_needs_all_three_proof_rows_to_pass():
    rows = [{"id": row_id, "status": "pass", "stage": "read", "observed": {}} for row_id in _PROOF_ROWS]
    assert kd._overall(rows, None) == ("pass", None)
    assert kd._overall([{**rows[0], "status": "info"}, *rows[1:]], None) == ("warn", None)
    assert kd._overall(rows[1:], None) == ("warn", None)


def test_the_connector_rows_come_right_after_the_session_check(kms):
    ids = [row["id"] for row in client(kms, "alice").diagnostics()["checks"]]
    assert ids[ids.index("session") + 1:ids.index("session") + 4] == list(_PROOF_ROWS)


def test_a_row_that_failed_in_the_connector_names_its_kind_only(kms, monkeypatch):
    _clean_tenant(kms, monkeypatch)
    _offline_item(kms, monkeypatch)
    row = _rows(_clean_client(kms).diagnostics())["connector_read"]
    assert row["observed"]["kinds"] == {"not_found": 1} and row["observed"]["failed_reads"] == 1
    assert row["observed"]["reads"] == 2 and row["observed"]["usable"] == 1


def test_a_read_that_is_only_its_header_is_counted(kms, monkeypatch):
    _clean_tenant(kms, monkeypatch)
    _tabs_with_no_elements(kms, monkeypatch)
    observed = _rows(_clean_client(kms).diagnostics())["connector_read"]["observed"]
    assert observed["header_only"] == 1 and observed["usable"] == 1


def test_nothing_listed_means_nothing_to_read_and_warns(kms, monkeypatch):
    _clean_tenant(kms, monkeypatch)
    _every_hit_hidden(kms, monkeypatch)
    row = _rows(client(kms, "svc", root_folder_id=95).diagnostics())["connector_read"]
    assert row["status"] == "warn" and row["observed"]["reads"] == 0


def test_a_later_sample_query_can_prove_search(kms, monkeypatch):
    _clean_tenant(kms, monkeypatch)
    kms["mock"].STATE["no_hit_text"] = "a"      # the first sample query, and the zero-hit word holds it too
    report = _clean_client(kms).diagnostics()
    row = _rows(report)["connector_search"]
    assert row["status"] == "pass" and row["observed"]["queries_tried"] == 2


def test_a_search_cut_at_its_limit_still_counts_as_results(kms, monkeypatch):
    """The limit is small, so a tenant with more hits ends in the connector's "max_results" stop."""
    _clean_tenant(kms, monkeypatch)
    kms["mock"].STATE["extra_results"] = [
        {"id": str(7000 + n), "subType": "T_PROCEDURE", "title": "x", "description": "", "statusCode": "2"} for n in range(9)]
    row = _rows(_clean_client(kms).diagnostics())["connector_search"]
    assert row["status"] == "pass" and row["observed"]["entries"] == kd._PROOF_HITS


# ------------------------------------------------------------------ the connector rows keep safety rules 2, 3, 4, 6
@pytest.mark.parametrize("expired", ["401", "302"])
def test_a_session_lost_during_the_connector_rows_sends_no_second_login(kms, expired):
    """Folder 12 is opened by list_files only, below the root: the session dies there."""
    kms["mock"].STATE.update({"expire_sessions_at": "12", "expired_response": expired})
    report = client(kms, "alice", root_folder_id=10).diagnostics()
    row = _rows(report)["connector_browse"]
    assert _logins(kms) == 1, "the connector would log in again here; a diagnostics run must not"
    assert row["status"] == "fail" and row["stage"] == "login" and row["observed"]["reason"] == "session_refused"
    assert report["failed_stage"] == "login"
    assert _rows(report)["connector_search"]["status"] == "skipped"
    assert _rows(report)["prefix"]["status"] == "skipped"


def test_a_token_refused_during_the_connector_rows_reports_the_login_stage(kms):
    probe = token_client(kms, "alice", root_folder_id=10)
    real = requests.Session.request

    def refused_below_the_root(self, method, url, **kwargs):
        if (kwargs.get("params") or {}).get("parentId") == "12":
            response = requests.Response()
            response.status_code, response.raw = 401, io.BytesIO(b"")
            return response
        return real(self, method, url, **kwargs)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(requests.Session, "request", refused_below_the_root)
        report = probe.diagnostics()
    row = _rows(report)["connector_browse"]
    assert row["status"] == "fail" and row["stage"] == "login" and row["observed"]["reason"] == "rejected"
    assert report["failed_stage"] == "login"


def test_a_login_works_again_once_the_connector_rows_are_over(kms):
    """The guard ends with the rows: the same client logs in again as it always does."""
    probe = client(kms, "alice", root_folder_id=10)
    probe.diagnostics()
    kms["mock"].expire_sessions()
    assert probe.read_file("item:301")
    assert _logins(kms) == 2


def test_a_login_works_again_after_a_connector_row_raised(kms):
    kms["mock"].STATE["expire_sessions_at"] = "12"
    probe = client(kms, "alice", root_folder_id=10)
    probe.diagnostics()
    kms["mock"].STATE["expire_sessions_at"] = None
    assert probe.read_file("item:301")
    assert _logins(kms) == 2


def test_the_login_guard_is_only_on_the_thread_that_set_it(kms):
    results = []

    def another_tool_call():
        results.append(client(kms, "bob").list_files(limit=1))

    with kc.proof_calls(lambda: kc._clock() + 5):
        with pytest.raises(kc.KmsHTTPError) as caught:
            client(kms, "alice").list_files(limit=1)
        worker = threading.Thread(target=another_tool_call)
        worker.start()
        worker.join(10)
    assert caught.value.kind == "auth" and results and results[0]
    assert _logins(kms) == 1, "bob logged in on his own thread; alice could not on this one"


@pytest.mark.parametrize("calls_before_proof", [3, 4])
def test_the_call_budget_stops_the_connector_rows_as_skipped(kms, monkeypatch, calls_before_proof):
    """The reach, the login and the session check take three calls."""
    monkeypatch.setattr(kd, "_MAX_CALLS", calls_before_proof)
    report = client(kms, "alice", root_folder_id=10).diagnostics()
    rows = _rows(report)
    assert report["calls_made"] == calls_before_proof == len(kms["mock"].REQUEST_LOG)
    assert rows["connector_browse"]["status"] == "skipped" and "budget" in rows["connector_browse"]["summary"].lower()
    assert report["overall"] == "warn" and report["failed_stage"] is None


def test_the_time_budget_stops_the_connector_rows_as_skipped(kms, monkeypatch):
    probe = client(kms, "alice")
    now = [kc._clock()]
    calls = []
    real = requests.Session.request

    def time_runs_out_at_the_session_check(self, method, url, **kwargs):
        calls.append(url)
        if len(calls) == 3:
            now[0] += kd._MAX_SECONDS
        return real(self, method, url, **kwargs)

    monkeypatch.setattr(kc, "_clock", lambda: now[0])
    monkeypatch.setattr(requests.Session, "request", time_runs_out_at_the_session_check)
    report = probe.diagnostics()
    assert _rows(report)["connector_browse"]["status"] == "skipped" and report["overall"] == "warn"
    assert report["calls_made"] == len(kms["mock"].REQUEST_LOG)


def test_every_connector_call_counts_against_the_budget(kms, monkeypatch):
    _clean_tenant(kms, monkeypatch)
    report = _clean_client(kms).diagnostics()
    assert report["calls_made"] == len(kms["mock"].REQUEST_LOG)
    item_calls = [row for row in kms["mock"].REQUEST_LOG if row[2].endswith("/public/item/279")]
    assert len(item_calls) == 2, "one read_file and one field_types call"


def _chain_of_folders(kms, monkeypatch, length):
    """One folder under the other: the shape that costs list_files the most calls per entry."""
    mock = kms["mock"]
    tree = {str(9000 + n): {"parent": None if n == 0 else str(8999 + n), "title": "f", "type": "FOLDER", "status": "2"}
            for n in range(length)}
    monkeypatch.setattr(mock, "TREE", tree)
    monkeypatch.setitem(mock.USERS, "alice", {**mock.USERS["alice"], "visible": set(tree)})


def test_a_deep_tree_cannot_spend_the_budget_in_the_browse_row(kms, monkeypatch):
    _chain_of_folders(kms, monkeypatch, 60)
    row = _rows(client(kms, "alice").diagnostics())["connector_browse"]
    assert row["status"] == "warn" and row["observed"]["calls"] == kd._PROOF_ENTRIES
    assert row["observed"]["capped"] is True and "part of the tree" in row["summary"]
    assert kd._PROOF_ENTRIES + 1 <= kd._PROOF_CALLS


def test_a_wide_scenario_uses_up_the_connector_rows_share_and_the_rest_still_runs(kms, monkeypatch):
    """Scenario 21501 asks the connector for forty step calls."""
    mock = kms["mock"]
    _clean_tenant(kms, monkeypatch)
    mock.STATE["extra_results"] = [_scenario_hit("21501")]
    report = _clean_client(kms).diagnostics()
    rows = _rows(report)
    assert rows["connector_read"]["status"] == "skipped" and report["overall"] == "warn"
    assert report["calls_made"] <= kd._MAX_CALLS
    assert rows["prefix"]["status"] == "info", "the other checks keep their share of the budget"


def test_the_worst_tenant_stays_inside_the_call_budget(kms, monkeypatch):
    """Everything the checks can sample, at once: no check may be skipped for want of calls."""
    mock = kms["mock"]
    mock.STATE["extra_results"] = [_scenario_hit("21501"), _scenario_hit("21302")]
    report = client(kms, "alice", layout_id=7, root_folder_id=mock.WIDE_FOLDER).diagnostics()
    assert report["calls_made"] <= kd._MAX_CALLS
    assert not [row["id"] for row in report["checks"] if "budget" in row["summary"].lower()]


def test_the_connector_rows_read_text_and_hold_none_of_it(kms, monkeypatch):
    mock = kms["mock"]
    _clean_tenant(kms, monkeypatch)
    mock.STATE["plant_sentinels"] = True
    report = _clean_client(kms).diagnostics()
    assert _rows(report)["connector_read"]["observed"]["body_chars"] > 0, "the planted text was read"
    _assert_no_content(report, mock)
    for row_id in _PROOF_ROWS:
        assert all(isinstance(value, (int, dict)) for value in _rows(report)[row_id]["observed"].values()), row_id


def test_the_connector_rows_do_not_widen_the_sample(kms, monkeypatch):
    """Everything they read counts as sampled, so rule 6 holds across the whole run.

    Ten top-level folders fill the browse limit, so the rows read search hits, and the tree walk finds four other items.
    """
    mock = kms["mock"]
    monkeypatch.setattr(mock, "SEARCH_RESULTS", [
        {"id": str(7000 + n), "subType": "T_PROCEDURE", "title": "x", "description": "", "statusCode": "2"} for n in range(9)])
    mock.STATE["extra_results"] = [_scenario_hit(str(6000 + n)) for n in range(4)]
    client(kms, "alice").diagnostics()
    items = {row[2] for row in mock.REQUEST_LOG if row[2].startswith(mock.PREFIX + "/public/item/")}
    scenarios = {req["path"] for req in mock.STATE["scenario_requests"] if "/step/" not in req["path"]}
    assert 0 < len(items) <= 5 and 0 < len(scenarios) <= 2


# ------------------------------------------------------------------ one way out of a run on a dead line
_READ_TIMEOUT = requests.exceptions.ReadTimeout()
_LINE_GONE = requests.exceptions.ConnectionError(ConnectionRefusedError())
_CERT_LOST = requests.exceptions.SSLError(ssl.SSLError())


def _fail_calls(monkeypatch, marker, error):
    """Every call to ``marker`` fails at the HTTP client; returns the calls that were sent after the first failure."""
    real = requests.Session.request
    after = []

    def failing(self, method, url, **kwargs):
        if after or marker in url:
            after.append(url)
            if marker in url:
                raise error
        return real(self, method, url, **kwargs)

    monkeypatch.setattr(requests.Session, "request", failing)
    return after


@pytest.mark.parametrize("error,stage,word", [(_LINE_GONE, "network", "refused"), (_CERT_LOST, "tls", "tls_failed")])
def test_a_search_call_that_gets_no_answer_ends_the_run_at_its_transport_stage(kms, monkeypatch, error, stage, word):
    after = _fail_calls(monkeypatch, "/search/full", error)
    report = client(kms, "alice", layout_id=7).diagnostics()
    rows = _rows(report)
    assert report["failed_stage"] == stage and report["overall"] == "fail"
    assert rows["connector_search"]["stage"] == stage and rows["connector_search"]["observed"]["failure"] == word
    assert len(after) == 1, "no further calls are sent"
    assert rows["search_layout"]["status"] == "skipped" and rows["prefix"]["status"] == "skipped"


def test_a_search_that_hits_the_deadline_never_reads_as_no_hits(kms, monkeypatch):
    """The connector turns a timed-out page into a stop with nothing found, so the row must not read as "no hits"."""
    _fail_calls(monkeypatch, "/search/full", _READ_TIMEOUT)
    report = client(kms, "alice").diagnostics(deadline=kc._clock() + 1.5)
    row = _rows(report)["connector_search"]
    assert row["status"] == "warn" and row["observed"]["failure"] == "slower_than_limit"
    assert report["overall"] == "warn"


@pytest.mark.parametrize("error,stage", [(_LINE_GONE, "network"), (_CERT_LOST, "tls")])
def test_item_calls_that_all_get_no_answer_end_the_run(kms, monkeypatch, error, stage):
    """Search and the tree work; every item read fails on the wire."""
    _clean_tenant(kms, monkeypatch)
    kms["mock"].STATE["extra_results"] = []
    after = _fail_calls(monkeypatch, "/public/item/", error)
    report = _clean_client(kms).diagnostics()
    assert report["failed_stage"] == stage and _rows(report)["connector_read"]["stage"] == stage
    assert len(after) == 1 and _rows(report)["field_types"]["status"] == "skipped"


def test_one_call_of_several_with_no_answer_does_not_end_the_run(kms, monkeypatch):
    _clean_tenant(kms, monkeypatch)
    _fail_calls(monkeypatch, "/public/item/", _LINE_GONE)
    report = _clean_client(kms).diagnostics()
    rows = _rows(report)
    assert rows["connector_read"]["status"] == "warn" and rows["connector_read"]["stage"] == "read"
    assert rows["connector_read"]["observed"]["kinds"] == {"unreachable": 1}
    assert rows["field_types"]["status"] != "skipped", "the scenario was read, so the line is up"


def test_folders_that_all_get_no_answer_end_the_run(kms, monkeypatch):
    """The session check reads the root; the walk opens its folders; every one of those times out."""
    real = requests.Session.request
    sent = []

    def only_folders_fail(self, method, url, **kwargs):
        parent = (kwargs.get("params") or {}).get("parentId")
        if sent:
            sent.append(url)
        if parent in ("12", "13", "14"):
            sent.append(url)
            raise _LINE_GONE
        return real(self, method, url, **kwargs)

    monkeypatch.setattr(requests.Session, "request", only_folders_fail)
    report = client(kms, "alice", root_folder_id=10).diagnostics()
    row = _rows(report)["connector_browse"]
    assert row["stage"] == "network" and row["observed"]["failure"] == "refused"
    assert report["failed_stage"] == "network" and len(sent) == 1


def test_a_walk_that_gets_no_answer_reads_as_network_even_after_the_root_answered(kms, monkeypatch):
    """The third read of the root is the top_level check's own; every folder it opens after that loses the line."""
    real = requests.Session.request
    root_reads = []

    def walk_fails(self, method, url, **kwargs):
        parent = (kwargs.get("params") or {}).get("parentId")
        root_reads.extend([parent] if parent == "10" else [])
        if len(root_reads) == 3 and parent in ("12", "13", "14"):
            raise _LINE_GONE
        return real(self, method, url, **kwargs)

    monkeypatch.setattr(requests.Session, "request", walk_fails)
    _clean_tenant(kms, monkeypatch)
    report = client(kms, "alice", root_folder_id=10).diagnostics()
    rows = _rows(report)
    assert rows["top_level"]["status"] == "fail" and rows["top_level"]["stage"] == "network"
    assert rows["top_level"]["observed"]["failure"] == "refused" and rows["search_layout"]["status"] == "skipped"


def test_a_session_lost_at_a_later_check_reports_the_login_stage(kms):
    kms["mock"].STATE.update({"expire_sessions_at": "ai_answers_item"})
    kms["mock"].STATE["ai_status"] = None
    report = client(kms, "alice", root_folder_id=10).diagnostics()
    assert report["failed_stage"] == "login" and _logins(kms) == 1
    lost = [row for row in report["checks"] if row["status"] == "fail"]
    assert len(lost) == 1 and lost[0]["stage"] == "login" and lost[0]["observed"]["reason"] == "session_refused"


def test_an_alternative_prefix_that_redirects_says_nothing_about_the_session(kms):
    real = requests.Session.request

    def api_prefix_redirects(self, method, url, **kwargs):
        if "/api/" in url and "/kms/lh/api/" not in url:
            response = requests.Response()
            response.status_code, response.raw = 302, io.BytesIO(b"")
            return response
        return real(self, method, url, **kwargs)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(requests.Session, "request", api_prefix_redirects)
        report = client(kms, "alice").diagnostics()
    assert _rows(report)["prefix"]["status"] == "info"
    assert _rows(report)["prefix"]["observed"]["alternatives"]["api"]["http_status"] == 302


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
    assert _rows(report)["login"]["status"] == "pass"
    assert "login did not succeed" not in json.dumps(report), "the login row says it did"


def test_the_session_check_is_one_authenticated_get(kms):
    report = client(kms, "alice").diagnostics()
    assert _rows(report)["session"]["status"] == "pass"
    assert kms["mock"].REQUEST_LOG[2][:2] == ("alice", "GET"), "the first call after the login"


def test_an_api_token_is_proved_by_one_tree_read(kms):
    row = _rows(token_client(kms, "alice").diagnostics())["session"]
    assert row["status"] == "pass"
    assert kms["mock"].REQUEST_LOG[2] == ("alice", "GET", kms["mock"].PREFIX + "/cm/tree/branch/view")


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
    """The ``nth`` tree call raises ``error``: 1 is the reach, 2 the session check, 3 connector_browse, 4 top_level."""
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


@pytest.mark.parametrize("nth,row_id", [(3, "connector_browse"), (4, "top_level")])
@pytest.mark.parametrize("error,stage", [(_LOST_LINE, "network"), (_TLS_LOST, "tls")])
def test_a_transport_failure_at_the_tree_is_not_a_browse_failure(kms, monkeypatch, error, stage, nth, row_id):
    _clean_tenant(kms, monkeypatch)
    _fail_tree_call(monkeypatch, nth, error)
    report = client(kms, "alice").diagnostics()
    assert report["failed_stage"] == stage and _rows(report)[row_id]["stage"] == stage
    assert _logins(kms) == 1


@pytest.mark.parametrize("expired", ["401", "302"])
def test_a_refused_token_ends_the_run(kms, expired):
    kms["mock"].STATE["expired_response"] = expired
    report = kc.KmsLighthouseClient(base_url=kms["base_url"], allow_http=True, api_token="not-a-known-token").diagnostics()
    assert report["failed_stage"] == "login"
    assert [row["id"] for row in report["checks"] if row["status"] == "fail"] == [{"401": "login", "302": "session"}[expired]]
    _assert_rest_skipped(report, keep=("session",))


@pytest.mark.parametrize("answer", [301, 302, 400, 403, 404, 500, 503, "error", "not_json", "empty"])
def test_a_token_that_only_session_info_refuses_works_for_the_connector(kms, answer):
    """The connector takes a display name from /sessionInfo and ignores any answer but a refused token."""
    kms["mock"].STATE["session_info_status"] = answer
    assert token_client(kms, "alice").test_connection()["success"] is True
    assert token_client(kms, "alice").list_files(limit=1)
    report = token_client(kms, "alice", root_folder_id=10).diagnostics()
    rows = _rows(report)
    assert rows["login"]["status"] == "warn" and rows["session"]["status"] == "pass"
    assert rows["connector_browse"]["status"] == "pass"
    assert rows["top_level"]["status"] == "pass"


def test_a_top_level_that_refuses_with_a_400_is_not_blamed_on_the_token(kms):
    """With no root the connector reads a 400 as "set a root folder", so the token is not what failed."""
    kms["mock"].STATE["top_level_status"] = 400
    assert "Root folder ID" in token_client(kms, "alice").test_connection()["message"]
    report = token_client(kms, "alice").diagnostics()
    assert _rows(report)["session"]["status"] == "warn" and report["failed_stage"] != "login"


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
                                       (errno.EACCES, "blocked_route"), (errno.EPERM, "blocked_route"),
                                       (errno.ETIMEDOUT, "timeout"),
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
    assert rows["search_layout"]["observed"]["without_layout_status"] == word
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


def test_a_ca_bundle_that_holds_no_certificate_is_a_tls_failure(monkeypatch, tmp_path):
    bundle = tmp_path / "not-a-bundle.pem"
    bundle.write_text("not a certificate", encoding="utf-8")
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(bundle))
    sent = []
    monkeypatch.setattr(requests.Session, "request", lambda self, *args, **kwargs: sent.append(args))
    report = kc.KmsLighthouseClient(base_url="https://kms.example.com", username="u", password="p").diagnostics()
    assert _rows(report)["tls"]["observed"]["failure"] == "ca_bundle_unreadable"
    assert sent == [] and report["failed_stage"] == "tls"


def test_a_ca_directory_is_a_bundle_the_connector_accepts(monkeypatch, tmp_path):
    """requests takes REQUESTS_CA_BUNDLE as a directory too, so the pre-check must not refuse one."""
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(tmp_path))
    sent = []

    def refused(self, method, url, **kwargs):
        sent.append(url)
        raise _LOST_LINE

    monkeypatch.setattr(requests.Session, "request", refused)
    report = kc.KmsLighthouseClient(base_url="https://kms.example.com", username="u", password="p").diagnostics()
    assert sent and _rows(report)["network"]["observed"]["failure"] == "refused"


def test_a_ca_bundle_is_not_consulted_over_plain_http(kms, monkeypatch, tmp_path):
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(tmp_path / "no-such-bundle.pem"))
    assert _rows(client(kms, "alice").diagnostics())["login"]["status"] == "pass"


def test_a_silent_server_in_a_short_run_is_slower_than_this_runs_limit():
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
    assert report["overall"] == "warn" and rows["network"]["status"] == "warn"
    assert rows["network"]["observed"]["failure"] == "slower_than_limit"


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
        assert connect + read <= max(2 * kc._MIN_TIMEOUT_S, left), (connect, read, left)


def test_the_first_call_fits_connect_plus_read_into_a_short_run(kms, monkeypatch):
    probe = client(kms, "alice")
    real = requests.Session.request
    timeouts = []

    def recording(self, method, url, **kwargs):
        timeouts.append(kwargs["timeout"])
        return real(self, method, url, **kwargs)

    monkeypatch.setattr(requests.Session, "request", recording)
    probe.diagnostics(deadline=kc._clock() + 30)
    connect, read = timeouts[0]
    assert connect + read <= 30 and read > 15, "the connector's read timeout, less its connect timeout"


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


@pytest.mark.parametrize("no_parent,status", [({"top_level_status": 500}, "http_500"), ({"top_level_status": 403}, "http_403"),
                                              ({"top_level_requires_parent": True}, "http_200")])
def test_with_a_root_set_the_call_with_no_parent_is_only_information(kms, monkeypatch, no_parent, status):
    """The connector never makes that call with a root set, so it cannot stop a clean run from passing."""
    _clean_tenant(kms, monkeypatch)
    kms["mock"].STATE.update(no_parent)
    assert _clean_client(kms).test_connection()["success"] is True
    report = _clean_client(kms).diagnostics()
    rows = _rows(report)
    assert report["overall"] == "pass", [(row["id"], row["status"]) for row in report["checks"]]
    assert rows["top_level"]["status"] == "info" and rows["session"]["status"] == "pass"
    assert rows["top_level"]["observed"]["no_parent_status"] == status
    assert "Root folder ID" not in json.dumps(report)


def test_the_session_read_asks_for_the_root_when_one_is_set(kms, monkeypatch):
    real = requests.Session.request
    tree_params = []

    def recording(self, method, url, **kwargs):
        if "/cm/tree/" in url:
            tree_params.append(kwargs.get("params"))
        return real(self, method, url, **kwargs)

    monkeypatch.setattr(requests.Session, "request", recording)
    client(kms, "alice", root_folder_id=10).diagnostics()
    assert tree_params[1] == {"parentId": "10"}, "the first read after the reach"


@pytest.mark.parametrize("answer,hint", [(400, True), (500, False)])
def test_with_no_root_a_failed_top_level_uses_the_connectors_hint_where_the_connector_does(kms, answer, hint):
    kms["mock"].STATE["top_level_status"] = answer
    said = client(kms, "alice").test_connection()["message"]
    report = client(kms, "alice").diagnostics()
    row = _rows(report)["top_level"]
    assert row["status"] == "fail" and report["failed_stage"] == "browse"
    assert (row["summary"] == kc._TOP_LEVEL_HINT) is hint
    assert (said == kc._TOP_LEVEL_HINT) is hint


def test_an_empty_top_level_with_no_root_uses_the_connectors_hint(kms):
    kms["mock"].STATE["top_level_requires_parent"] = True
    assert _rows(client(kms, "alice").diagnostics())["top_level"]["summary"] == kc._TOP_LEVEL_HINT


def test_a_root_that_returns_nothing_warns_and_never_asks_to_set_a_root(kms, monkeypatch):
    _clean_tenant(kms, monkeypatch)
    report = client(kms, "svc", root_folder_id=95).diagnostics()
    rows = _rows(report)
    assert rows["top_level"]["status"] == "warn" and rows["connector_browse"]["status"] == "warn"
    assert report["overall"] == "warn" and "Root folder ID" not in json.dumps(report)


def test_a_root_that_cannot_be_read_fails_browse(kms, monkeypatch):
    _clean_tenant(kms, monkeypatch)
    kms["mock"].STATE["tree_status"] = {str(_CLEAN_ROOT): 500}
    report = _clean_client(kms).diagnostics()
    assert _rows(report)["top_level"]["status"] == "fail" and report["failed_stage"] == "browse"
    assert "Root folder ID" not in json.dumps(report)


def test_the_walk_opens_no_folder_the_connector_hides(kms):
    """Folder 15 is offline: the connector never lists it, so nothing may be judged from it."""
    kms["mock"].STATE["tree_status"] = {kms["mock"].OFFLINE_FOLDER: 500}
    row = _rows(client(kms, "alice", root_folder_id=10).diagnostics())["top_level"]
    assert kms["mock"].STATE["tree_status_sent"] == [] and row["observed"]["folders_failed"] == 0


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
    ([{"id": "x-1", "title": "t"}, {"title": "no id"}], "non_numeric_id"),
    ([{"id": "279", "title": "t"}, "a string"], "not_records"),
])
def test_a_search_answer_the_connector_refuses_fails_search_layout(kms, results, reason):
    kms["mock"].STATE["search_body"] = {"regularSearch": {"results": results}}
    with pytest.raises(kc.KmsHTTPError):
        client(kms, "alice").search_files("anything")
    row = _rows(client(kms, "alice").diagnostics())["search_layout"]
    assert row["status"] == "fail" and row["observed"]["reason"] == reason


@pytest.mark.parametrize("results,usable,unusable", [
    ([{"id": "279", "title": "t"}, {"id": "x-1", "title": "t"}], 1, 1),
    ([{"id": "x-1", "title": "t"}, {"id": "279", "title": "t"}, {"id": "301", "title": "t"}, {"title": "no id"}], 2, 2),
    # An offline record is hidden, not unusable, so the connector does not refuse this answer either.
    ([{"id": "x-1", "title": "t"}, {"id": "4410", "title": "t", "statusCode": "1"}], 0, 1),
])
def test_a_search_answer_the_connector_can_use_warns_and_counts_the_unusable_records(kms, results, usable, unusable):
    kms["mock"].STATE["search_body"] = {"regularSearch": {"results": results}}
    assert len(client(kms, "alice").search_files("anything")) == usable, "the connector skips them and goes on"
    row = _rows(client(kms, "alice").diagnostics())["search_layout"]
    assert row["status"] == "warn" and row["observed"]["unusable_records"] == unusable
    assert "reason" not in row["observed"]


def test_a_search_with_every_record_usable_counts_none(kms):
    row = _rows(client(kms, "alice").diagnostics())["search_layout"]
    assert row["status"] == "pass" and row["observed"]["unusable_records"] == 0


def test_a_folder_that_cannot_be_opened_warns(kms):
    kms["mock"].STATE["tree_status"] = {"78": 500}
    row = _rows(client(kms, "alice").diagnostics())["top_level"]
    assert row["status"] == "warn"
    assert row["observed"]["folder_statuses"]["http_500"] == 1 and row["observed"]["folder_statuses"]["http_200"] > 0


def test_no_folder_that_can_be_opened_fails(kms, monkeypatch):
    mock = kms["mock"]
    _clean_tenant(kms, monkeypatch)
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
    assert row["observed"]["with_layout_status"] == "http_200" and row["observed"]["without_layout_status"] == "http_400"
    assert row["observed"]["layout_required"] is True


def test_search_without_a_layout_runs_once(kms):
    row = _rows(client(kms, "alice").diagnostics())["search_layout"]
    assert row["status"] == "pass" and row["observed"]["without_layout_status"] == "http_200"
    assert "with_layout_status" not in row["observed"]


def test_a_rejected_search_is_a_search_failure(kms):
    kms["mock"].STATE["search_400"] = True
    report = client(kms, "alice").diagnostics()
    assert _rows(report)["search_layout"]["status"] == "fail" and report["failed_stage"] == "search"


def test_a_search_with_no_hits_warns_after_a_few_fixed_queries(kms):
    kms["mock"].STATE["no_hit_text"] = ""
    row = _rows(client(kms, "alice").diagnostics())["search_layout"]
    assert row["status"] == "warn" and row["observed"]["without_layout_rows"] == 0
    assert row["observed"]["queries_tried"] == len(kd._SAMPLE_QUERIES) + 1 <= 4, "the fixed ones and a listed title's word"


@pytest.mark.parametrize("total_key", ["total", "totalResults", "numFound"])
def test_a_stated_zero_total_is_no_hits_not_a_failed_search(kms, total_key):
    kms["mock"].STATE["search_body"] = {"regularSearch": {total_key: 0}}
    row = _rows(client(kms, "alice").diagnostics())["search_layout"]
    assert row["status"] == "warn" and row["observed"]["without_layout_rows"] == 0
    assert row["observed"]["queries_tried"] == len(kd._SAMPLE_QUERIES) + 1, "every sample query is still tried"


def test_a_search_answer_with_no_results_and_no_total_is_a_failure(kms):
    kms["mock"].STATE["search_body"] = {"regularSearch": {"query": "x"}}
    assert _rows(client(kms, "alice").diagnostics())["search_layout"]["status"] == "fail"


def test_layout_required_needs_a_usable_answer_with_the_layout_and_a_refusal_without_it(kms):
    kms["mock"].STATE["search_requires_layout"] = "error"
    row = _rows(client(kms, "alice", layout_id=7).diagnostics())["search_layout"]
    assert row["observed"]["without_layout_status"] == "kms_error", "a 200 ERROR is a refusal too"
    assert row["observed"]["layout_required"] is True


def _bare_search_answers(mock, monkeypatch, status):
    real = mock.Handler._search

    def answer(self, user, query, odd):
        return real(self, user, query, odd) if "layoutId=" in self.path else self._send_empty(status)

    monkeypatch.setattr(mock.Handler, "_search", answer)


@pytest.mark.parametrize("status,required", [(400, True), (413, True), (414, True), (500, None), (403, None), (404, None)])
def test_layout_required_is_true_only_on_a_refusal_and_unknown_otherwise(kms, monkeypatch, status, required):
    _bare_search_answers(kms["mock"], monkeypatch, status)
    observed = _rows(client(kms, "alice", layout_id=7).diagnostics())["search_layout"]["observed"]
    assert observed["layout_required"] is required
    assert observed["layout_statuses"][f"http_{status}"] == 1 and observed["layout_statuses"]["http_200"] == 1


def test_a_bare_search_that_also_works_says_nothing_about_the_layout(kms):
    observed = _rows(client(kms, "alice", layout_id=7).diagnostics())["search_layout"]["observed"]
    assert observed["layout_required"] is None and observed["layout_statuses"] == {"http_200": 2}


def test_a_timed_out_bare_search_has_a_word_and_the_layout_stays_unknown(kms, monkeypatch):
    real = requests.Session.request

    def bare_times_out(self, method, url, **kwargs):
        if "/search/full" in url and "layoutId" not in (kwargs.get("params") or {}):
            raise _READ_TIMEOUT
        return real(self, method, url, **kwargs)

    monkeypatch.setattr(requests.Session, "request", bare_times_out)
    row = _rows(client(kms, "alice", layout_id=7).diagnostics())["search_layout"]
    assert row["status"] == "info" and row["observed"]["without_layout_status"] == "slower_than_limit"
    assert row["observed"]["layout_required"] is None


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
    assert row["observed"]["with_layout_status"] == "kms_error" and row["observed"]["without_layout_status"] == "http_500"
    assert row["observed"]["layout_required"] is None


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
                                               ("error", "kms_error")])
@pytest.mark.parametrize("over", [100, 300])
def test_any_other_answer_to_a_long_query_is_inconclusive_not_accepted_or_rejected(kms, over, answer, status_key):
    kms["mock"].STATE.update({"search_max_query": over, "search_too_long": answer})
    row = _rows(client(kms, "alice").diagnostics())["query_length"]
    assert row["observed"]["shortest_rejected"] is None
    assert row["status"] == ("warn" if over < kc._MAX_QUERY_CHARS else "info"), "past the limit it is an observation"
    assert row["observed"]["inconclusive"] is True
    assert row["observed"]["statuses"][status_key] >= 1
    assert row["observed"]["longest_accepted"] == (None if over < kc._MAX_QUERY_CHARS else kc._MAX_QUERY_CHARS)


def test_a_long_query_the_connector_could_not_read_is_not_accepted(kms, monkeypatch):
    """A 200 with no results is an answer the connector refuses, so it proves nothing about the length."""
    mock = kms["mock"]
    real_send_json = mock.Handler._send_json

    def long_queries_get_an_odd_body(self, status, body, headers=None):
        if "/search/full" in self.path and "a" * kc._MAX_QUERY_CHARS in self.path:
            body = {"regularSearch": {"note": 1}}
        return real_send_json(self, status, body, headers)

    monkeypatch.setattr(mock.Handler, "_send_json", long_queries_get_an_odd_body)
    row = _rows(client(kms, "alice").diagnostics())["query_length"]
    assert row["status"] == "warn" and row["observed"]["inconclusive"] is True
    assert row["observed"]["longest_accepted"] is None


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
    assert row["observed"]["next_page_status"] == "http_502" and row["observed"]["server_cap"] is None


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


def test_ai_text_that_shares_no_element_with_the_item_never_passes(kms, monkeypatch):
    """The connector uses only the elements /public/item shows, so this text never reaches an agent."""
    _clean_tenant(kms, monkeypatch)
    _ai_elements(kms["mock"], monkeypatch, {"element_name": "E_ELSEWHERE", "element_text": "plain words"})
    row = _rows(_clean_client(kms).diagnostics())["ai_text"]
    assert row["status"] == "warn" and row["observed"]["plain"] == 1 and row["observed"]["common"] == 0


def test_ai_text_from_only_some_of_the_items_warns(kms, monkeypatch):
    mock = kms["mock"]
    _clean_tenant(kms, monkeypatch)
    monkeypatch.setattr(mock, "SEARCH_RESULTS", [{**mock.SEARCH_RESULTS[0]}, {**mock.SEARCH_RESULTS[0], "id": "301"}])
    mock.STATE["ai_missing_items"] = ["301"]
    row = _rows(_clean_client(kms).diagnostics())["ai_text"]
    assert row["status"] == "warn" and row["observed"]["plain"] > 0
    assert row["observed"]["statuses"] == {"http_200": 1, "http_404": 1}


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


def test_some_scenarios_that_could_not_be_read_warn(kms, monkeypatch):
    mock = kms["mock"]
    monkeypatch.setattr(mock, "SEARCH_RESULTS", [])
    mock.STATE["extra_results"] = [_scenario_hit("21302"), _scenario_hit("99999")]
    row = _rows(client(kms, "svc", root_folder_id=78).diagnostics())["scenario_text"]
    assert row["status"] == "warn" and row["observed"]["strings"] > 0
    assert row["observed"]["scenarios_read"] == 1 and row["observed"]["scenarios_tried"] == 2


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


@pytest.mark.parametrize("answer", ["wrong_step", "error"])
def test_a_step_answer_the_connector_would_drop_warns(kms, monkeypatch, answer):
    """A 200 is not enough: the connector shows such a step as not available."""
    monkeypatch.setattr(kms["mock"], "SEARCH_RESULTS", [])
    probe = _only_scenario(kms)
    kms["mock"].STATE["step_status"] = {"23506": answer}
    assert "(step not available)" in _only_scenario(kms).read_file("scenario:21302")
    row = _rows(probe.diagnostics())["scenario_steps"]
    assert row["status"] == "warn" and row["observed"]["unusable"] == 1


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


def test_a_run_past_the_route_limit_does_not_claim_nothing_was_sent():
    """Its thread was working when the route gave up, so the report cannot know what went out."""
    report = kd.timed_out_report({"base_url": "https://kms.example.com"})
    assert report["calls_made"] is None and report["calls_unknown"] is True
    assert report["duration_ms"] == kd.route_seconds() * 1000
    skipped = [row["summary"] for row in report["checks"] if row["status"] == "skipped" and row["id"] != "logout"
               and row["id"] != "concurrent_sessions"]
    assert skipped and set(skipped) == {"Skipped: the run was cut off; calls may have been sent."}


@pytest.mark.parametrize("make", [lambda kms: kd.failure_report({"base_url": kms["base_url"]}, stage="login",
                                                                reason="credentials_unavailable", error_type="E"),
                                  lambda kms: client(kms, "alice").diagnostics()])
def test_a_finished_run_knows_its_calls(kms, make):
    report = make(kms)
    assert report["calls_unknown"] is False and isinstance(report["calls_made"], int)


def test_both_kinds_of_report_pass_the_host_through_one_gate(monkeypatch):
    """A client keeps the URL's netloc as typed, and a percent sign in it passes the client's own check."""
    def refused(self, method, url, **kwargs):
        raise _LOST_LINE

    monkeypatch.setattr(requests.Session, "request", refused)
    base_url = "https://kms%20secret.example.com"
    report = kc.KmsLighthouseClient(base_url=base_url, username="u", password="p").diagnostics()
    assert report["host"] == kd.failure_report({"base_url": base_url}, stage="login", reason="r", error_type="E")["host"]
    assert report["host"] == "" and "secret" not in json.dumps(report)


@pytest.mark.parametrize("settings", [{"base_url": "ftp://kms.example.com"}, {"base_url": "http://kms.example.com"},
                                      {"base_url": "https://kms.example.com", "api_prefix": "not a path"}])
def test_settings_the_client_refuses_have_their_own_reason(settings):
    with pytest.raises(kc.KmsError) as caught:
        kc.KmsLighthouseClient(username="u", password="p", **settings)
    report = kd.build_failure_report(settings, caught.value)
    row = _rows(report)["network"]
    assert row["status"] == "fail" and row["observed"] == {"reason": "bad_settings", "error_type": "KmsError"}
    assert "settings" in row["summary"].lower() and str(caught.value) not in json.dumps(report)
    assert report["failed_stage"] == "network" and report["calls_made"] == 0


@pytest.mark.parametrize("credentials", [{"api_token": "not a token"}, {}])
def test_a_credential_the_client_refuses_stays_a_login_failure(credentials):
    with pytest.raises(kc.KmsError) as caught:
        kc.KmsLighthouseClient(base_url="https://kms.example.com", **credentials)
    report = kd.build_failure_report({"base_url": "https://kms.example.com"}, caught.value)
    assert report["failed_stage"] == "login"
    assert _rows(report)["login"]["observed"]["reason"] == "credentials_unavailable"


def test_the_route_limit_leaves_the_run_its_own_budget():
    assert kd.route_seconds() == kd._MAX_SECONDS + 5


def test_a_deadline_set_before_the_run_starts_is_kept(kms):
    """The route sets it when the request arrives, so a run that starts late cannot outlive the route."""
    deadline = kd.run_deadline()
    assert kd._MAX_SECONDS - 1 <= deadline - kc._clock() <= kd._MAX_SECONDS
    report = client(kms, "alice").diagnostics(deadline=kc._clock() - 1)
    assert report["calls_made"] == 0 and kms["mock"].REQUEST_LOG == [] and _logins(kms) == 0


def test_a_blocked_destination_is_a_network_failure_report():
    report = kd.failure_report({"base_url": "https://169.254.169.254"}, stage="network", reason="egress_blocked",
                               error_type="UnsafeURLError")
    assert report["failed_stage"] == "network"
    assert _rows(report)["network"]["observed"]["reason"] == "egress_blocked"


# ------------------------------------------------------------------ only the connector's own calls can fail or end a run
_REDIRECT = 302


def _answer_with(status):
    response = requests.Response()
    response.status_code, response.raw = status, io.BytesIO(b"")
    return response


def _inject(monkeypatch, is_the_call, what):
    """``what`` (a status, or an error to raise) answers every call ``is_the_call`` picks; the rest reach the mock."""
    real = requests.Session.request
    picked = []

    def choosing(self, method, url, **kwargs):
        if is_the_call(url, kwargs.get("params") or {}, kwargs.get("headers") or {}, self):
            picked.append(url)
            if isinstance(what, int):
                return _answer_with(what)
            raise what
        return real(self, method, url, **kwargs)

    monkeypatch.setattr(requests.Session, "request", choosing)
    return picked


def _other_prefix(url, _params, _headers, _session):
    return "/api/" in url and "/kms/lh/api/" not in url


def _long_query(url, params, _headers, _session):
    return len(params.get("query", "")) > kc._MAX_QUERY_CHARS


def _bare_search(url, params, _headers, _session):
    return "/search/full" in url and "layoutId" not in params


def _no_parent_tree(url, params, _headers, _session):
    return "/cm/tree/" in url and "parentId" not in params


def _no_session(url, _params, _headers, session):
    return "/cm/tree/" in url and not session.cookies and "Authorization" not in session.headers


def _layout_client(kms):
    return client(kms, "svc", root_folder_id=_CLEAN_ROOT, layout_id=7)


@pytest.mark.parametrize("what", [_REDIRECT, _LOST_LINE, _READ_TIMEOUT, _TLS_LOST],
                         ids=["redirect", "refused", "timeout", "tls"])
@pytest.mark.parametrize("site,row_id", [(_other_prefix, "prefix"), (_long_query, "query_length"), (_bare_search, "search_layout"),
                                         (_no_parent_tree, "top_level"), (_no_session, "network")],
                         ids=["prefix", "long_query", "bare_search", "no_parent_tree", "no_session"])
def test_an_observation_that_fails_never_changes_a_clean_tenants_verdict(kms, monkeypatch, site, row_id, what):
    _clean_tenant(kms, monkeypatch)
    picked = _inject(monkeypatch, site, what)
    report = _layout_client(kms).diagnostics()
    rows = _rows(report)
    assert picked, "the fault was injected on a call that went out"
    assert report["overall"] == "pass" and report["failed_stage"] is None, [
        (row["id"], row["status"]) for row in report["checks"] if row["status"] not in ("pass", "info")]
    assert rows["connector_read"]["status"] == "pass"
    assert rows[row_id]["status"] in ("pass", "info", "warn"), rows[row_id]
    assert _logins(kms) == 1
    assert rows["prefix"]["status"] == "info", "the run went on to its last check"


@pytest.mark.parametrize("site,row_id,word_of", [
    (_other_prefix, "prefix", lambda row: row["observed"]["alternatives"]["api"]["failure"]),
    (_long_query, "query_length", lambda row: next(iter(set(row["observed"]["statuses"]) - {"http_200"}))),
    (_bare_search, "search_layout", lambda row: row["observed"]["without_layout_status"]),
    (_no_parent_tree, "top_level", lambda row: row["observed"]["no_parent_status"]),
    (_no_session, "network", lambda row: row["observed"]["failure"]),
])
def test_a_failed_observation_keeps_its_status_word(kms, monkeypatch, site, row_id, word_of):
    _clean_tenant(kms, monkeypatch)
    _inject(monkeypatch, site, _LOST_LINE)
    row = _rows(_layout_client(kms).diagnostics())[row_id]
    assert word_of(row) == "refused"
    assert row["status"] in ("info", "warn"), "a failed observation is never a clean pass"


def test_a_redirected_observation_keeps_its_status(kms, monkeypatch):
    _clean_tenant(kms, monkeypatch)
    _inject(monkeypatch, _bare_search, _REDIRECT)
    row = _rows(_layout_client(kms).diagnostics())["search_layout"]
    assert row["observed"]["without_layout_status"] == "http_302" and row["status"] == "info"


def test_a_no_session_call_that_fails_with_nothing_else_reaching_kms_is_still_a_failure(monkeypatch):
    calls = []

    def refused(self, method, url, **kwargs):
        calls.append(url)
        raise _LOST_LINE

    monkeypatch.setattr(requests.Session, "request", refused)
    report = kc.KmsLighthouseClient(base_url="https://kms.example.com", username="u", password="p").diagnostics()
    assert _rows(report)["network"]["status"] == "fail" and report["failed_stage"] == "network"
    assert len(calls) == 2, "the no-session call and the login"


def test_a_tls_failure_on_the_no_session_call_alone_is_not_a_tls_failure(monkeypatch):
    calls = []

    def only_the_first_call_fails(self, method, url, **kwargs):
        calls.append(url)
        if len(calls) == 1:
            raise _CERT_LOST
        response = requests.Response()
        response.status_code, response.raw = 200, io.BytesIO(b'{"status": "SUCCESS", "data": []}')
        return response

    monkeypatch.setattr(requests.Session, "request", only_the_first_call_fails)
    report = kc.KmsLighthouseClient(base_url="https://kms.example.com", api_token="t").diagnostics()
    assert _rows(report)["tls"]["status"] == "warn" and _rows(report)["tls"]["observed"]["failure"] == "tls_failed"
    assert report["failed_stage"] != "tls"


def test_the_no_parent_call_feeds_no_sample_with_a_root_set(kms, monkeypatch):
    """The connector never reads what that call lists, so nothing may be judged from it."""
    _clean_tenant(kms, monkeypatch)
    probe = _clean_client(kms)
    report = probe.diagnostics()
    assert _rows(report)["types"]["observed"]["tree_types"].get("FOLDER", 0) == 0, "root 78 holds item 279 alone"


def test_a_query_past_the_connectors_limit_that_fails_leaves_the_row_at_info(kms, monkeypatch):
    _clean_tenant(kms, monkeypatch)
    kms["mock"].STATE.update({"search_max_query": 300, "search_too_long": 503})
    report = _clean_client(kms).diagnostics()
    row = _rows(report)["query_length"]
    assert row["status"] == "info" and row["observed"]["longest_accepted"] == kc._MAX_QUERY_CHARS
    assert report["overall"] == "pass"


def test_a_query_at_the_connectors_limit_is_a_real_call(kms, monkeypatch):
    _clean_tenant(kms, monkeypatch)
    _inject(monkeypatch, lambda url, params, _h, _s: len(params.get("query", "")) == kc._MAX_QUERY_CHARS, _LOST_LINE)
    report = _clean_client(kms).diagnostics()
    assert report["failed_stage"] == "network" and _rows(report)["query_length"]["stage"] == "network"
    assert _rows(report)["zero_hit"]["status"] == "skipped"


def test_an_observation_counts_against_the_call_budget(kms, monkeypatch):
    _clean_tenant(kms, monkeypatch)
    report = _layout_client(kms).diagnostics()
    assert report["calls_made"] == len(kms["mock"].REQUEST_LOG), "prefix, bare search and long queries included"


# ------------------------------------------------------------------ what ends a run at the session and token checks
def test_session_info_that_gets_no_answer_alone_warns_with_its_word(kms, monkeypatch):
    _inject(monkeypatch, lambda url, *_rest: url.endswith("/sessionInfo"), _LOST_LINE)
    report = token_client(kms, "alice", root_folder_id=10).diagnostics()
    rows = _rows(report)
    assert rows["login"]["status"] == "warn" and rows["login"]["observed"]["failure"] == "refused"
    assert rows["connector_browse"]["status"] == "pass" and report["failed_stage"] is None


def test_a_401_on_session_info_ends_the_run(kms):
    kms["mock"].STATE["session_info_status"] = 401
    report = token_client(kms, "alice").diagnostics()
    row = _rows(report)["login"]
    assert row["status"] == "fail" and row["observed"]["reason"] == "rejected" and report["failed_stage"] == "login"
    _assert_rest_skipped(report)


def _tree_after_the_reach_answers(monkeypatch, answer):
    seen = []
    real = requests.Session.request

    def tree_answers(self, method, url, **kwargs):
        if "/cm/tree/" in url:
            seen.append(url)
            if len(seen) > 1:           # the first is the reach, with no token
                return _answer_with(answer)
        return real(self, method, url, **kwargs)

    monkeypatch.setattr(requests.Session, "request", tree_answers)


@pytest.mark.parametrize("answer", [301, 302, 401])
def test_a_token_lost_on_the_tree_read_ends_the_run_at_login(kms, monkeypatch, answer):
    _tree_after_the_reach_answers(monkeypatch, answer)
    report = token_client(kms, "alice").diagnostics()
    row = _rows(report)["session"]
    assert row["status"] == "fail" and row["stage"] == "login" and row["observed"]["reason"] == "rejected"
    assert report["failed_stage"] == "login" and report["calls_made"] == 3, "the reach, /sessionInfo, the tree read"
    _assert_rest_skipped(report, keep=("session",))


@pytest.mark.parametrize("root,answer,kind", [(None, 403, "forbidden"), (10, 403, "forbidden"), (10, 400, "bad_request")])
def test_a_403_or_400_on_the_tree_read_warns_and_the_connector_row_names_the_kind(kms, monkeypatch, root, answer, kind):
    """The token may be fine: only the connector's own listing can say what this answer means."""
    _tree_after_the_reach_answers(monkeypatch, answer)
    report = token_client(kms, "alice", root_folder_id=root).diagnostics()
    rows = _rows(report)
    assert rows["session"]["status"] == "warn" and rows["session"]["observed"]["http_status"] == answer
    assert rows["connector_browse"]["status"] == "fail" and rows["connector_browse"]["stage"] == "browse"
    assert rows["connector_browse"]["observed"]["kind"] == kind
    assert report["failed_stage"] == "browse"
    assert rows["prefix"]["status"] != "skipped", "the run went on"


def _some_hits(kms, monkeypatch, count):
    mock = kms["mock"]
    monkeypatch.setattr(mock, "SEARCH_RESULTS", [
        {"id": "279", "subType": "T_PROCEDURE", "title": "t", "description": "", "statusCode": "2"},
        {"id": "301", "subType": "T_PROCEDURE", "title": "t", "description": "", "statusCode": "2"}][:count])


def test_one_item_that_redirects_while_others_read_does_not_end_the_run(kms, monkeypatch):
    _clean_tenant(kms, monkeypatch)
    _some_hits(kms, monkeypatch, 2)
    calls = []
    real = requests.Session.request

    def second_item_read_redirects(self, method, url, **kwargs):
        if url.endswith("/public/item/301"):
            calls.append(url)
            if len(calls) == 1:
                return real(self, method, url, **kwargs)
            return _answer_with(302)
        return real(self, method, url, **kwargs)

    monkeypatch.setattr(requests.Session, "request", second_item_read_redirects)
    report = _clean_client(kms).diagnostics()
    rows = _rows(report)
    assert rows["field_types"]["status"] == "warn" and rows["field_types"]["observed"]["statuses"]["http_302"] == 1
    assert report["failed_stage"] is None and rows["prefix"]["status"] == "info"


def test_a_row_whose_every_call_lost_the_session_ends_the_run(kms, monkeypatch):
    _clean_tenant(kms, monkeypatch)
    _some_hits(kms, monkeypatch, 2)
    reads = []
    real = requests.Session.request

    def field_types_all_redirect(self, method, url, **kwargs):
        if "/public/item/" in url:
            reads.append(url)
            if len(reads) > 2:      # the two connector reads came first
                return _answer_with(302)
        return real(self, method, url, **kwargs)

    monkeypatch.setattr(requests.Session, "request", field_types_all_redirect)
    report = _clean_client(kms).diagnostics()
    row = _rows(report)["field_types"]
    assert row["stage"] == "login" and row["observed"]["reason"] == "session_refused" and report["failed_stage"] == "login"


# ------------------------------------------------------------------ no answer, and an answer that came too late for this run
def _stall_after_the_headers(monkeypatch, is_the_call):
    """The status arrives, then the body stalls, so the failure keeps a status of 200."""
    class Stalling(requests.Response):
        def iter_content(self, *_args, **_kwargs):
            raise requests.exceptions.ReadTimeout()
            yield b""

    real = requests.Session.request

    def stalling(self, method, url, **kwargs):
        if is_the_call(method, url, self):
            response = Stalling()
            response.status_code, response.raw = 200, io.BytesIO(b"")
            return response
        return real(self, method, url, **kwargs)

    monkeypatch.setattr(requests.Session, "request", stalling)


def test_a_stall_after_the_headers_of_the_first_call_is_no_answer(kms, monkeypatch):
    _stall_after_the_headers(monkeypatch, lambda _method, url, session: _no_session(url, {}, {}, session))
    rows = _rows(client(kms, "alice").diagnostics())
    assert rows["network"]["observed"]["failure"] == "timeout", "a status alone does not make it an answer"
    assert rows["expiry"]["status"] == "skipped"


def test_a_stall_after_the_headers_of_the_login_is_a_network_failure(kms, monkeypatch):
    _stall_after_the_headers(monkeypatch, lambda method, _url, _session: method == "POST")
    report = client(kms, "alice").diagnostics()
    row = _rows(report)["login"]
    assert row["stage"] == "network" and row["observed"]["failure"] == "timeout" and report["failed_stage"] == "network"


def test_a_stall_after_the_headers_of_a_cut_call_is_slower_than_limit(kms, monkeypatch):
    _clean_tenant(kms, monkeypatch)
    _stall_after_the_headers(monkeypatch, lambda _method, url, _session: "/search/full" in url)
    report = _clean_client(kms).diagnostics()
    row = _rows(report)["connector_search"]
    assert row["status"] == "warn" and row["observed"]["failure"] == "slower_than_limit"


def test_a_row_whose_last_call_got_no_answer_ends_the_run_at_that_stage(kms, monkeypatch):
    """The first listed item reads, the second has no line left: the line is what the row last saw."""
    _clean_tenant(kms, monkeypatch)
    _some_hits(kms, monkeypatch, 2)
    reads = []
    real = requests.Session.request

    def second_item_of_field_types_is_cut(self, method, url, **kwargs):
        if "/public/item/" in url:
            reads.append(url)
            if len(reads) == 4:
                raise _TLS_LOST
        return real(self, method, url, **kwargs)

    monkeypatch.setattr(requests.Session, "request", second_item_of_field_types_is_cut)
    report = _clean_client(kms).diagnostics()
    row = _rows(report)["field_types"]
    assert row["status"] == "fail" and row["stage"] == "tls" and report["failed_stage"] == "tls"
    assert _rows(report)["ai_elements"]["status"] == "skipped", "the run ended there"


_CONNECT_TIMEOUT = requests.exceptions.ConnectTimeout()


@pytest.mark.parametrize("marker,row_id", [("/search/full", "connector_search"), ("/public/item/", "connector_read")])
def test_a_timeout_the_diagnostic_cut_short_is_slower_than_limit_and_the_run_goes_on(kms, monkeypatch, marker, row_id):
    _clean_tenant(kms, monkeypatch)
    kms["mock"].STATE["extra_results"] = []
    _fail_calls(monkeypatch, marker, _READ_TIMEOUT)
    report = _clean_client(kms).diagnostics()
    row = _rows(report)[row_id]
    assert row["status"] == "warn" and row["observed"]["failure"] == "slower_than_limit"
    assert "may still work" in row["summary"]
    assert report["failed_stage"] is None and report["overall"] == "warn"
    assert _rows(report)["prefix"]["status"] == "info", "the run went on"


def test_the_full_read_timeout_on_the_first_call_is_a_real_timeout(monkeypatch):
    """The first call keeps the connector's own read timeout, so nothing here is the diagnostic's doing."""
    timeouts = []

    def silent(self, method, url, **kwargs):
        timeouts.append(kwargs["timeout"])
        raise requests.exceptions.ReadTimeout()

    monkeypatch.setattr(requests.Session, "request", silent)
    report = kc.KmsLighthouseClient(base_url="https://kms.example.com", username="u", password="p").diagnostics()
    assert timeouts[0][1] == pytest.approx(kc._DEFAULT_TIMEOUT[1], abs=1)
    assert _rows(report)["network"]["status"] == "fail" and _rows(report)["network"]["observed"]["failure"] == "timeout"
    assert report["failed_stage"] == "network"


def test_a_first_call_cut_short_by_a_short_run_is_slower_than_limit(monkeypatch):
    def silent(self, method, url, **kwargs):
        raise requests.exceptions.ReadTimeout()

    monkeypatch.setattr(requests.Session, "request", silent)
    report = kc.KmsLighthouseClient(base_url="https://kms.example.com", username="u", password="p").diagnostics(
        deadline=kc._clock() + 30)
    rows = _rows(report)
    assert rows["network"]["status"] == "warn" and rows["network"]["observed"]["failure"] == "slower_than_limit"
    assert rows["tls"]["status"] == "skipped", "no answer means nothing about the certificate"
    assert rows["expiry"]["status"] == "skipped"


def test_a_login_cut_short_is_slower_than_limit_and_ends_the_run_with_no_second_login(kms, monkeypatch):
    real = requests.Session.request

    def login_silent(self, method, url, **kwargs):
        if method == "POST":
            raise requests.exceptions.ReadTimeout()
        return real(self, method, url, **kwargs)

    monkeypatch.setattr(requests.Session, "request", login_silent)
    report = client(kms, "alice").diagnostics(deadline=kc._clock() + 30)
    row = _rows(report)["login"]
    assert row["status"] == "warn" and row["observed"]["failure"] == "slower_than_limit"
    assert report["overall"] == "warn" and _rows(report)["session"]["status"] == "skipped"


def test_a_connect_timeout_the_diagnostic_did_not_cut_is_a_real_timeout(kms, monkeypatch):
    """Later calls keep the connector's connect timeout, so this one proves the line is gone."""
    _clean_tenant(kms, monkeypatch)
    _fail_calls(monkeypatch, "/search/full", _CONNECT_TIMEOUT)
    report = _clean_client(kms).diagnostics()
    row = _rows(report)["connector_search"]
    assert row["status"] == "fail" and row["stage"] == "network" and row["observed"]["failure"] == "timeout"
    assert report["failed_stage"] == "network"


def test_a_row_with_real_failures_stays_failed_beside_one_slow_call(kms, monkeypatch):
    mock = kms["mock"]
    _clean_tenant(kms, monkeypatch)
    monkeypatch.setattr(mock, "SEARCH_RESULTS", [
        {"id": str(n), "subType": "T_PROCEDURE", "title": "t", "description": "", "statusCode": "2"} for n in (7001, 7002)])
    calls = []
    real = requests.Session.request

    def first_timed_read_stalls(self, method, url, **kwargs):
        if url.endswith("/public/item/7001"):
            calls.append(url)
            if len(calls) == 2:
                raise _READ_TIMEOUT
        return real(self, method, url, **kwargs)

    monkeypatch.setattr(requests.Session, "request", first_timed_read_stalls)
    row = _rows(client(kms, "svc", root_folder_id=95).diagnostics())["field_types"]
    assert row["status"] == "fail", "the mock has neither item, so one 404 is a real failure beside the slow call"


# ------------------------------------------------------------------ proof rows that match real use
def _item_read_order(mock):
    return [row[2].rsplit("/", 1)[1] for row in mock.REQUEST_LOG if "/public/item/" in row[2]]


def _hit(entry_id):
    return {"id": entry_id, "subType": "T_PROCEDURE", "title": "t", "description": "", "statusCode": "2"}


def _folder_of_items(kms, monkeypatch, count):
    mock = kms["mock"]
    tree = {"78": mock.TREE["78"], **{str(7100 + n): {"parent": "78", "title": "i", "type": "ITEM", "status": "2"}
                                      for n in range(count)}}
    monkeypatch.setattr(mock, "TREE", tree)
    monkeypatch.setitem(mock.USERS, "svc", {**mock.USERS["svc"], "visible": set(tree)})


def test_connector_read_reads_a_search_hit_first_then_a_listed_entry(kms, monkeypatch):
    mock = kms["mock"]
    _clean_tenant(kms, monkeypatch)
    monkeypatch.setattr(mock, "SEARCH_RESULTS", [_hit("301")])
    mock.STATE["extra_results"] = []
    _clean_client(kms).diagnostics()     # the root lists item 279; search finds item 301
    assert _item_read_order(mock)[:2] == ["301", "279"]


def test_a_listed_entry_is_read_even_when_search_fills_the_sample(kms, monkeypatch):
    mock = kms["mock"]
    _clean_tenant(kms, monkeypatch)
    monkeypatch.setattr(mock, "SEARCH_RESULTS", [_hit("301"), _hit("302"), _hit("305")])
    mock.STATE["extra_results"] = []
    report = _clean_client(kms).diagnostics()
    assert _rows(report)["connector_read"]["observed"]["reads"] == 2 and _item_read_order(mock)[:2] == ["301", "279"]


def test_a_listed_scenario_and_a_found_scenario_are_both_read(kms, monkeypatch):
    mock = kms["mock"]
    monkeypatch.setattr(mock, "TREE", {"78": mock.TREE["78"], "21302": {"parent": "78", "title": "s", "type": "ITEM",
                                                                        "subType": "T_SCENARIO_FLOW", "status": "2"}})
    monkeypatch.setitem(mock.USERS, "svc", {**mock.USERS["svc"], "visible": {"78", "21302"}})
    monkeypatch.setattr(mock, "SEARCH_RESULTS", [_scenario_hit("21400")])
    mock.STATE["extra_results"] = []
    report = _clean_client(kms).diagnostics()
    assert _rows(report)["connector_read"]["observed"]["reads"] == 2
    asked = [request["path"].rsplit("/", 1)[1] for request in mock.STATE["scenario_requests"] if "/step/" not in request["path"]]
    assert asked[:2] == ["21400", "21302"]


def test_the_item_sample_holds_a_search_hit_and_a_tree_entry(kms, monkeypatch):
    """With the connector rows out of calls, the sample comes from the records alone, and from both of them."""
    mock = kms["mock"]
    _folder_of_items(kms, monkeypatch, 8)
    monkeypatch.setattr(mock, "SEARCH_RESULTS", [_hit("301")])
    monkeypatch.setattr(kd, "_PROOF_CALLS", 0)
    _clean_client(kms).diagnostics()
    read = _item_read_order(mock)
    assert read[0] == "301" and "7100" in read and len(read) <= kd._MAX_ITEMS


def test_what_the_connector_read_counts_towards_the_sample(kms, monkeypatch):
    """The listed item sits behind five folders, so the records alone would fill the sample with five search hits."""
    mock = kms["mock"]
    folders = {str(8100 + n): {"parent": "78", "title": "f", "type": "FOLDER", "status": "2"} for n in range(5)}
    tree = {"78": mock.TREE["78"], **folders, "7100": {"parent": "78", "title": "i", "type": "ITEM", "status": "2"}}
    monkeypatch.setattr(mock, "TREE", tree)
    monkeypatch.setitem(mock.USERS, "svc", {**mock.USERS["svc"], "visible": set(tree)})
    monkeypatch.setattr(mock, "SEARCH_RESULTS", [_hit(str(7000 + n)) for n in range(9)])
    _clean_client(kms).diagnostics()
    read = set(_item_read_order(mock))
    assert "7100" in read and len(read) <= kd._MAX_ITEMS


def test_a_browse_with_folders_only_does_not_pass(kms, monkeypatch):
    _chain_of_folders(kms, monkeypatch, 3)
    row = _rows(client(kms, "alice").diagnostics())["connector_browse"]
    assert row["status"] == "warn" and row["observed"]["entries"] == 3 and row["observed"]["content_entries"] == 0
    assert row["observed"]["capped"] is False


def test_a_listing_cut_by_the_cap_passes_and_says_only_part_was_proved(kms, monkeypatch):
    _clean_tenant(kms, monkeypatch)
    _folder_of_items(kms, monkeypatch, kd._PROOF_ENTRIES + 3)
    report = _clean_client(kms).diagnostics()
    row = _rows(report)["connector_browse"]
    assert row["status"] == "pass" and row["observed"]["capped"] is True
    assert "part of the tree" in row["summary"]
    assert row["observed"]["calls"] == 1, "no descent to find more"


def test_a_listing_inside_the_cap_is_not_capped(kms, monkeypatch):
    _clean_tenant(kms, monkeypatch)
    row = _rows(_clean_client(kms).diagnostics())["connector_browse"]
    assert row["observed"]["capped"] is False and "part of the tree" not in row["summary"]


def test_page_rows_asks_for_the_next_page_after_a_full_page(kms, monkeypatch):
    mock = kms["mock"]
    mock.STATE["extra_results"] = [_hit(str(7000 + n)) for n in range(60)]
    report = client(kms, "alice").diagnostics()
    observed = _rows(report)["page_rows"]["observed"]
    assert observed["next_page_rows"] > 0 and observed["server_cap"] is False


def test_a_refused_next_page_after_a_full_page_fails_as_the_connector_does(kms):
    mock = kms["mock"]
    mock.STATE["extra_results"] = [_hit(str(7000 + n)) for n in range(60)]
    mock.STATE["search_page_status"] = 502
    with pytest.raises(kc.KmsHTTPError):
        client(kms, "alice").search_files("a", limit=100)
    report = client(kms, "alice").diagnostics()
    row = _rows(report)["page_rows"]
    assert row["status"] == "fail" and row["observed"]["next_page_status"] == "http_502" and report["failed_stage"] == "search"


def test_no_answer_on_the_next_page_after_a_full_page_ends_the_run(kms, monkeypatch):
    """The connector makes this call, so it counts like any other of its calls."""
    kms["mock"].STATE["extra_results"] = [_hit(str(7000 + n)) for n in range(60)]
    picked = _inject(monkeypatch, lambda url, params, _h, _s: "/search/full" in url and params.get("start") == "50", _LOST_LINE)
    report = client(kms, "alice").diagnostics()
    row = _rows(report)["page_rows"]
    assert picked and row["stage"] == "network" and report["failed_stage"] == "network"
    assert _rows(report)["field_types"]["status"] == "skipped"


def test_the_call_after_a_short_page_is_an_observation_the_connector_never_makes(kms, monkeypatch):
    _clean_tenant(kms, monkeypatch)

    def past_the_short_page(url, params, _headers, _session):
        return "/search/full" in url and params.get("start", "0") != "0"

    picked = _inject(monkeypatch, past_the_short_page, _LOST_LINE)
    assert _clean_client(kms).search_files("a", limit=100) and picked == [], "the connector stops at a short page"
    report = _clean_client(kms).diagnostics()
    row = _rows(report)["page_rows"]
    assert picked and row["status"] == "warn" and row["observed"]["next_page_status"] == "refused"
    assert report["failed_stage"] is None and _rows(report)["field_types"]["status"] != "skipped"


def _first_query_refused(monkeypatch, status=400):
    return _inject(monkeypatch, lambda url, params, _h, _s: "/search/full" in url and params.get("query") == "a", status)


def test_a_refused_fixed_query_tries_the_next_and_warns(kms, monkeypatch):
    _clean_tenant(kms, monkeypatch)
    _first_query_refused(monkeypatch)
    report = _clean_client(kms).diagnostics()
    rows = _rows(report)
    for row_id in ("connector_search", "search_layout"):
        assert rows[row_id]["status"] == "warn", row_id
        assert rows[row_id]["observed"]["queries_refused"] == 1 and rows[row_id]["observed"]["queries_tried"] == 2
    assert rows["connector_search"]["observed"]["entries"] > 0
    assert report["failed_stage"] is None


def test_every_fixed_query_refused_fails_search(kms, monkeypatch):
    _clean_tenant(kms, monkeypatch)
    kms["mock"].STATE["search_400"] = True
    report = _clean_client(kms).diagnostics()
    assert _rows(report)["connector_search"]["status"] == "fail"
    assert _rows(report)["search_layout"]["status"] == "fail" and report["failed_stage"] == "search"


def test_a_failed_query_with_no_line_left_tries_no_other(kms, monkeypatch):
    _clean_tenant(kms, monkeypatch)
    picked = _inject(monkeypatch, lambda url, _p, _h, _s: "/search/full" in url, _LOST_LINE)
    report = _clean_client(kms).diagnostics()
    assert len(picked) == 1 and report["failed_stage"] == "network"


def _only_the_title_word_finds_anything(kms, monkeypatch, word):
    mock = kms["mock"]
    real = mock.Handler._search

    def answer(self, user, query, odd):
        if word in self.path:
            return real(self, user, query, odd)
        return self._send_json(200, {"regularSearch": {"results": []}})

    monkeypatch.setattr(mock.Handler, "_search", answer)


def test_a_listed_title_gives_the_last_sample_query_and_stays_out_of_the_report(kms, monkeypatch, caplog):
    mock = kms["mock"]
    word = "Zebrawordsentinel"
    _clean_tenant(kms, monkeypatch)
    monkeypatch.setitem(mock.TREE, "279", {**mock.TREE["279"], "title": f"{word} manual"})
    _only_the_title_word_finds_anything(kms, monkeypatch, word)
    caplog.set_level("DEBUG")
    report = _clean_client(kms).diagnostics()
    rows = _rows(report)
    assert rows["connector_search"]["status"] == "pass"
    assert rows["connector_search"]["observed"]["queries_tried"] == len(kd._SAMPLE_QUERIES) + 1
    assert rows["search_layout"]["observed"]["queries_tried"] == len(kd._SAMPLE_QUERIES) + 1
    assert word not in json.dumps(report) and word not in caplog.text


def test_with_no_listed_title_only_the_fixed_queries_are_tried(kms, monkeypatch):
    _clean_tenant(kms, monkeypatch)
    kms["mock"].STATE["no_hit_text"] = ""
    row = _rows(client(kms, "svc", root_folder_id=95).diagnostics())["connector_search"]
    assert row["observed"]["queries_tried"] == len(kd._SAMPLE_QUERIES)


def test_one_unreadable_entry_warns_when_another_read_worked(kms, monkeypatch):
    _clean_tenant(kms, monkeypatch)
    _offline_item(kms, monkeypatch)
    row = _rows(_clean_client(kms).diagnostics())["connector_read"]
    assert row["status"] == "warn" and row["observed"]["failed_reads"] == 1 and row["observed"]["usable"] == 1


def test_every_read_failed_fails_the_row(kms, monkeypatch):
    mock = kms["mock"]
    _clean_tenant(kms, monkeypatch)
    mock.STATE["extra_results"] = []
    monkeypatch.setattr(mock, "SEARCH_RESULTS", [_hit("7001")])
    report = client(kms, "svc", root_folder_id=95).diagnostics()
    row = _rows(report)["connector_read"]
    assert row["status"] == "fail" and row["observed"]["failed_reads"] == row["observed"]["reads"] == 1
    assert report["failed_stage"] == "read"


@pytest.mark.parametrize("name", [["E_SUMMARY"], {"a": 1}, 7, None])
def test_an_ai_element_name_that_is_not_a_string_is_left_out(kms, monkeypatch, name):
    _ai_elements(kms["mock"], monkeypatch, {"element_name": name, "element_text": "plain words"})
    row = _rows(client(kms, "alice").diagnostics())["ai_elements"]
    assert row["status"] == "warn" and "error_type" not in row["observed"]
    assert row["observed"]["only_in_ai"] == [] and row["observed"]["unlisted_names"] == 0


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


def test_the_panel_says_when_the_calls_of_a_run_are_unknown():
    panel = _PANEL.read_text(encoding="utf-8")
    template = panel[:panel.index("<script")]
    assert re.search(r"report\.calls_unknown\s*\?\s*\$t\('kmsDiagnostics\.callsUnknown'", template), template
    english = json.loads((_REPO / "locales" / "en.json").read_text(encoding="utf-8"))["kmsDiagnostics"]
    assert "unknown" in english["callsUnknown"].lower()


def test_a_clean_run_never_says_every_check_passed():
    """One check stays manual and some are skipped, so the line claims the automatic checks only."""
    english = json.loads((_REPO / "locales" / "en.json").read_text(encoding="utf-8"))["kmsDiagnostics"]
    assert english["overallPass"] == "Automatic checks passed"
    old = ("All checks passed", "Все проверки пройдены", "Todas las comprobaciones se superaron", "نجحت جميع الفحوصات",
           "Alla kontroller godkändes", "כל הבדיקות עברו", "Toutes les vérifications ont réussi", "Alle Prüfungen bestanden",
           "Tutti i controlli superati", "Todas as verificações passaram")
    for path in _LOCALES:
        strings = json.loads(path.read_text(encoding="utf-8"))["kmsDiagnostics"]
        assert strings["overallPass"] not in old, path.name


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
    for word in ("session_refused", "blocked_route", "ca_bundle_unreadable", "bad_body", "run_timed_out", "kms_error",
                 "bad_settings", "connector_browse", "connector_search", "connector_read", "calls_unknown"):
        assert f"`{word}`" in section, word
    assert "the connector's own public methods" in section, "what the proof rows are"
    assert re.search(r"`pass` needs all three", section), "the overall rule"
    assert re.search(r"gateway.*60 s.*read limit|60 s read limit.*gateway", section, re.S), "a stalled tenant behind a gateway"


def _guide_section():
    guide = _GUIDE.read_text(encoding="utf-8")
    section = guide[guide.index("## On-site diagnostics"):]
    return section[:section.index("\n## ", 5)]


def test_the_guide_states_what_can_fail_or_end_a_run():
    section = " ".join(_guide_section().split())
    assert "Only a call the connector really makes with this configuration can fail a run or end it" in section
    for word in ("slower_than_limit", "observation_failed", "failed_reads", "capped", "queries_refused"):
        assert f"`{word}`" in section, word
    assert "may still work for users" in section
    assert "/sessionInfo" in section and not re.search(r"401 or a redirect\) ends the run there", section)
    assert not re.search(r"a\s+403 there ends the run", section), "a 403 on the tree read warns"
    assert f"{kd._PROOF_CALLS} of the 60 calls" in section, "the share in the guide is the share in the code"


def test_the_guide_sends_the_installer_to_layout_id_only_when_one_is_set():
    row = next(line for line in _guide_section().splitlines() if line.startswith("| `search` |"))
    assert "Check Layout ID" not in row and "If a Layout ID is set" in row


def test_the_guide_says_a_listed_title_can_give_one_search_word_held_in_memory_only():
    assert re.search(r"listed\s+title.*held in\s+memory.*never", _guide_section(), re.S)


def test_the_guide_lists_the_timed_out_panel_state_as_not_yet_proven():
    guide = _GUIDE.read_text(encoding="utf-8")
    assert re.search(r"timed-out\s+state.*(render|browser)", guide[guide.index("## Verification boundary"):], re.S)


def test_the_feedback_loop_maps_every_checklist_item_to_a_check():
    loop = _LOOP.read_text(encoding="utf-8")
    assert "| P12 |" in loop
    table = loop[loop.index("## Checklist items and diagnostics checks"):]
    for check_id, checklist, *_ in kd._CHECKS:
        assert f"`{check_id}`" in table, check_id
        for item in checklist:
            assert re.search(rf"^\| {item} \| `{check_id}` \|", table, re.M), (item, check_id)
