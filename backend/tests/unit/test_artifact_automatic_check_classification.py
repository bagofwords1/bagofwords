"""Classification contract of the automatic artifact browser check.

The evidence fed to the classifier is produced by a real
``ArtifactPreviewService`` (events recorded through ``record`` and a viewer
query answered through the broker route), wrapped in the ``BrowserOutput``
shape the guarded browser tools return. Only the HTTP response of the
forwarded query is replaced (the true boundary).
"""
from __future__ import annotations

import asyncio
import inspect
import re
from types import SimpleNamespace

import pytest

from app.ai.tools import artifact_verification as av
from app.ai.tools.artifact_verification import (
    classify_preview_output,
    classify_smoke_snapshot,
    summary_sentence,
)
from app.services.artifact_preview_service import ArtifactPreviewService

ROOT_WAIT_MESSAGE = (
    'Frame.wait_for_selector: Timeout 20000ms exceeded.\nCall log:\n'
    '  - waiting for locator("#root > *") to be visible\n'
)
EVIDENCE_KEYS = ("errors", "failed_queries")


class _Response:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code

    def json(self):
        return self.payload


class _QueryClient:
    """Replace only the forwarded viewer-query HTTP response."""

    def __init__(self, payload):
        self.payload = payload

    async def post(self, *args, **kwargs):
        return _Response(self.payload)


class _QueryRoute:
    def __init__(self, query_id="query-a"):
        self.request = SimpleNamespace(
            url=f"https://preview.test/api/queries/{query_id}/run",
            method="POST",
            post_data_json={"mode": "viewer", "params": {}},
        )

    async def fulfill(self, *, status, json):
        return {"status": status, "json": json}


def _service():
    service = ArtifactPreviewService({
        "report": SimpleNamespace(id="report-1"),
        "organization": SimpleNamespace(id="org-1"),
        "user": SimpleNamespace(id="user-1"),
    })
    service.artifact = {"id": "artifact-1", "version": 3, "content": {"code": ""}}
    service.query_ids = {"query-a"}
    service.origin = "https://preview.test"
    return service


def _output(service, *, success=True, readiness="ready", error_code=None):
    output = {"success": success, "readiness": readiness, "evidence": service.evidence(),
              "session_id": "session-1", "snapshot": "- heading \"Sales\""}
    if error_code:
        output["error_code"] = error_code
    return output


def _result(**overrides):
    base = {"status": "passed", "reason": None, "artifact_id": "a-1", "version": 2,
            "checked": ["load"], "readiness": "ready", "errors": [], "failed_queries": [],
            "blocked_requests": 0, "informational_errors": 0, "conversation_smoke": None,
            "duration_ms": 1200}
    base.update(overrides)
    return base


@pytest.mark.asyncio
async def test_a_failed_viewer_query_is_an_app_problem():
    service = _service()
    service.client = _QueryClient({"status": "error", "error": "column \"revenu\" does not exist"})
    await service.route(_QueryRoute())
    result = classify_preview_output(_output(service))
    assert result["status"] == "problems_found"
    assert result["failed_queries"] and result["failed_queries"][0]["query_id"] == "query-a"
    assert result["errors"] == []


def test_navigation_only_failures_mean_the_preview_was_not_ready():
    service = _service()
    service.record("error", source="navigation",
                   message="Page.goto: net::ERR_CONNECTION_REFUSED at http://localhost:3000/artifact-preview/x")
    result = classify_preview_output(_output(service, success=False, readiness="failed"))
    assert (result["status"], result["reason"]) == ("unavailable", "preview_not_ready")
    assert result["errors"] == []


def test_host_noise_and_blocked_requests_are_informational():
    service = _service()
    service.record("error", source="console", message="Failed to load resource: the server responded with 404")
    service.record("error", source="console", message="[vite] connecting...")
    service.record("blocked", path="/fonts.css", message="External resource is outside preview scope")
    result = classify_preview_output(_output(service, readiness="errors_observed"))
    assert result["status"] == "passed"
    assert result["blocked_requests"] >= 1
    assert result["informational_errors"] >= 1
    assert result["errors"] == []


_SW_NOISE = ("Failed to read the 'serviceWorker' property from 'Navigator': Service worker is disabled because "
             "the context is sandboxed and lacks the 'allow-same-origin' flag.")


@pytest.mark.parametrize("source,message", [
    ("browser", _SW_NOISE),
    ("browser", "SecurityError: " + _SW_NOISE),
    ("console", "Uncaught SecurityError: " + _SW_NOISE),
])
def test_the_service_worker_block_in_the_sandboxed_frame_is_informational(source, message):
    # Playwright's service_workers="block" init script reads navigator.serviceWorker
    # in every frame, which throws inside the sandboxed artifact iframe. Seen on
    # every live load; it is not app code.
    service = _service()
    service.record("error", source=source, message=message)
    result = classify_preview_output(_output(service, readiness="errors_observed"))
    assert result["status"] == "passed"
    assert result["errors"] == []
    assert result["informational_errors"] == 1


def test_an_app_security_error_is_still_a_problem():
    service = _service()
    service.record("error", source="browser", message="SecurityError: Failed to read the 'localStorage' property "
                                                     "from 'Window': The document is sandboxed.")
    result = classify_preview_output(_output(service, readiness="errors_observed"))
    assert result["status"] == "problems_found"


def test_an_app_console_error_is_a_problem():
    service = _service()
    service.record("error", source="console", message="Uncaught (in promise) Error: boom")
    result = classify_preview_output(_output(service, readiness="errors_observed"))
    assert result["status"] == "problems_found"
    assert result["errors"][0]["source"] == "console"


@pytest.mark.parametrize("code", ["preview_restricted", "session_scope_mismatch"])
def test_restricted_outputs_withhold_every_piece_of_evidence(code):
    service = _service()
    service.record("error", source="browser", message="TypeError: secret-marker")
    service.record("blocked", path="/x", message="blocked")
    result = classify_preview_output(_output(service, success=False, error_code=code))
    assert (result["status"], result["reason"]) == ("unavailable", "restricted")
    assert result["errors"] == [] and result["failed_queries"] == []
    assert result["blocked_requests"] == 0 and result["informational_errors"] == 0
    assert result["readiness"] is None
    assert "secret-marker" not in str(result)


def test_problem_lists_are_bounded():
    service = _service()
    for index in range(9):
        service.record("error", source="browser", message=f"Error {index}: " + "x" * 2000)
    result = classify_preview_output(_output(service))
    assert result["status"] == "problems_found"
    assert 1 <= len(result["errors"]) <= 5
    assert all(len(error["message"]) <= 300 for error in result["errors"])


def test_transport_errors_on_a_failed_load_mean_the_preview_was_unavailable():
    service = _service()
    service.record("error", source="transport", message="ReadTimeout forwarding /api/queries")
    result = classify_preview_output(_output(service, success=False, readiness="failed"))
    assert (result["status"], result["reason"]) == ("unavailable", "preview_unavailable")
    assert result["errors"] == []


def test_a_transport_error_on_a_successful_load_is_informational_only():
    # The load succeeded (the app rendered and was snapshotted), so one failed
    # forward is neither an app problem nor a reason to call the preview
    # unavailable and trip the run breaker.
    service = _service()
    service.record("error", source="transport", message="ReadTimeout forwarding /api/queries")
    result = classify_preview_output(_output(service, readiness="errors_observed"))
    assert (result["status"], result["reason"]) == ("passed", None)
    assert result["errors"] == []
    assert result["informational_errors"] == 1
    assert result["reason"] not in av._BREAKER_REASONS


def test_a_lone_root_render_timeout_is_a_blank_app_problem():
    service = _service()
    service.record("error", source="navigation", message=ROOT_WAIT_MESSAGE)
    result = classify_preview_output(_output(service, success=False, readiness="failed"))
    assert result["status"] == "problems_found"
    assert [error["source"] for error in result["errors"]] == ["render"]
    assert result["informational_errors"] == 0


@pytest.mark.parametrize("message", [
    "Page.goto: net::ERR_CONNECTION_REFUSED at http://localhost:3000/artifact-preview/x",
    'Page.wait_for_selector: Timeout 30000ms exceeded.\nCall log:\n'
    '  - waiting for locator("iframe[data-artifact-frame]") to be attached\n',
])
def test_goto_and_iframe_failures_are_not_blank_renders(message):
    service = _service()
    service.record("error", source="navigation", message=message)
    result = classify_preview_output(_output(service, success=False, readiness="failed"))
    assert (result["status"], result["reason"]) == ("unavailable", "preview_not_ready")


def test_app_errors_come_before_the_blank_render_error():
    service = _service()
    service.record("error", source="browser", message="ReferenceError: x is not defined")
    service.record("error", source="navigation", message=ROOT_WAIT_MESSAGE)
    result = classify_preview_output(_output(service, success=False, readiness="failed"))
    assert [error["source"] for error in result["errors"]] == ["browser", "render"]


BEFORE = '- heading "Team chat" [ref=e1]\n- heading "Errors" [ref=e2]\n- textbox "Ask a question" [ref=e3]\n- button "Send" [ref=e4]'
REPLY = "- text: Preview answer. No agent was called."


def test_reply_with_a_new_type_error_line_is_a_strong_indicator():
    after = BEFORE + "\n" + REPLY + "\n- text: \"TypeError: Cannot read properties of undefined (reading 'text')\""
    result = classify_smoke_snapshot(BEFORE, after)
    assert result["reply_seen"] is True
    assert result["indicator"] == "strong"


def test_a_new_alert_saying_failed_is_strong():
    after = BEFORE + '\n- alert [ref=e9]: "Request failed"'
    assert classify_smoke_snapshot(BEFORE, after)["indicator"] == "strong"


def test_generic_failed_text_is_weak():
    after = BEFORE + "\n" + REPLY + "\n- text: Sending failed, please retry"
    result = classify_smoke_snapshot(BEFORE, after)
    assert result["reply_seen"] is True
    assert result["indicator"] == "weak"


def test_static_error_words_present_before_the_send_do_not_count():
    after = BEFORE + "\n" + REPLY
    result = classify_smoke_snapshot(BEFORE, after)
    assert result == {"reply_seen": True, "indicator": None, "message": None}


def test_changed_refs_do_not_create_new_lines():
    after = re.sub(r"ref=e(\d)", lambda m: f"ref=e{int(m[1]) + 40}", BEFORE)
    result = classify_smoke_snapshot(BEFORE, after + "\n- alert [ref=e77]: Saved")
    assert result["indicator"] == "weak"  # only the alert is new; the renamed refs are not
    assert classify_smoke_snapshot(BEFORE, after)["indicator"] is None
    assert classify_smoke_snapshot(BEFORE, after)["reply_seen"] is False


def test_focus_moving_onto_an_unnamed_container_does_not_create_new_lines():
    # Before the send the log div is dropped by the filter (its children sit one
    # level up); after the send the app focuses it, so it is kept ([active]) and
    # its old children move one level deeper. They are not new.
    before = ("- textbox [ref=e3]\n"
              "- generic [ref=e9]: \"Earlier: export failed\"\n"
              "- generic [ref=e10]: TypeError in checkout\n")
    after = ("- textbox [ref=e3]\n"
             "- generic [active] [ref=e8]:\n"
             "  - generic [ref=e9]: \"Earlier: export failed\"\n"
             "  - generic [ref=e10]: TypeError in checkout\n")
    assert classify_smoke_snapshot(before, after)["indicator"] is None
    assert classify_smoke_snapshot(before, after + "- generic [ref=e11]: TypeError: x is undefined")["indicator"] == "strong"


def test_failed_query_summary_points_to_query_repair_not_code_edits():
    text = summary_sentence(_result(status="problems_found", checked=["load"],
                                    failed_queries=[{"query_id": "q", "status": "error", "error": "bad column"}]))
    assert "read_query" in text and "create_data" in text
    assert "edit_artifact" not in text


def test_runtime_error_summary_points_to_edit_artifact():
    text = summary_sentence(_result(status="problems_found",
                                    errors=[{"source": "browser", "message": "TypeError: boom"}]))
    assert "edit_artifact" in text


def test_passed_summary_admits_unconfirmed_data_loading():
    text = summary_sentence(_result(readiness="errors_observed"))
    assert "not confirmed" in text


def test_unavailable_summary_names_its_reason():
    text = summary_sentence(_result(status="unavailable", reason="preview_not_ready", checked=[], readiness=None))
    assert "preview_not_ready" in text


def test_budget_constants_respect_the_runner_timeouts():
    from app.ai.agent_v2 import AgentV2

    match = re.search(r"idle_timeout_s=(\d+), hard_timeout_s=(\d+)", inspect.getsource(AgentV2))
    assert match, "runner timeout policy not found"
    idle, hard = int(match[1]), int(match[2])
    browser_work = av.AUTOMATIC_CHECK_TIMEOUT_S + av.AUTOMATIC_CHECK_SMOKE_TIMEOUT_S + av.AUTOMATIC_CHECK_CLEANUP_TIMEOUT_S
    assert idle > av.AUTOMATIC_CHECK_DB_RELEASE_WAIT_S + av.AUTOMATIC_CHECK_LOCK_WAIT_S
    assert browser_work < idle
    assert av.AUTOMATIC_CHECK_LATEST_START_S + browser_work < hard


class _BrokenQueryClient:
    """The forwarded viewer query fails with a non-JSON 500 page."""

    async def post(self, *args, **kwargs):
        import httpx
        return httpx.Response(500, text="Internal Server Error",
                              request=httpx.Request("POST", "https://preview.test/api/queries/query-a/run"))


@pytest.mark.asyncio
async def test_a_non_json_500_viewer_query_is_a_failed_query_not_transport():
    service = _service()
    service.client = _BrokenQueryClient()
    fulfilled = await service.route(_QueryRoute())
    assert fulfilled["status"] == 500
    result = classify_preview_output(_output(service))
    assert result["status"] == "problems_found"
    assert [q["query_id"] for q in result["failed_queries"]] == ["query-a"]
    assert result["failed_queries"][0]["status"] == "error"
    assert "500" in (result["failed_queries"][0]["error"] or "")
    assert result["informational_errors"] == 0 and result["errors"] == []
    assert "read_query" in summary_sentence({**_result(status="problems_found"), **result})


def test_one_crash_echoed_three_ways_counts_once():
    service = _service()
    service.record("error", source="runtime", message="Cannot read properties of undefined (reading 'map')")
    service.record("error", source="console",
                   message="[ArtifactFrame] Iframe render error: Cannot read properties of undefined (reading 'map')")
    service.record("error", source="browser", message="TypeError: Cannot read properties of undefined (reading 'map')")
    service.record("error", source="browser", message="ReferenceError: rows is not defined")
    result = classify_preview_output(_output(service, readiness="errors_observed"))
    assert result["status"] == "problems_found"
    assert len(result["errors"]) == 2
    assert result["errors"][0]["message"] == "Cannot read properties of undefined (reading 'map')"
    assert result["errors"][1]["message"] == "ReferenceError: rows is not defined"


def test_an_error_rendered_in_the_load_snapshot_is_a_ui_problem():
    # A crash the app shows on screen at load sits in an alert (an error
    # boundary, a toast); the JS-error token inside that alert is the problem.
    service = _service()
    output = _output(service)
    output["snapshot"] = ('- heading "Sales" [ref=e1]\n'
                          '- alert [ref=e2]:\n'
                          '  - text: "TypeError: Cannot read properties of undefined (reading \'map\')"')
    result = av.classify_load_output(output)
    assert result["status"] == "problems_found"
    assert result["errors"] == [{"source": "ui", "message":
                                 "- text: \"TypeError: Cannot read properties of undefined (reading 'map')\""}]


def test_plain_text_js_error_outside_an_alert_is_data_at_load():
    service = _service()
    output = _output(service)
    output["snapshot"] = ('- heading "Sales" [ref=e1]\n'
                          '- text: "TypeError: Cannot read properties of undefined (reading \'map\')"')
    result = av.classify_load_output(output)
    assert (result["status"], result["errors"]) == ("passed", [])


LOG_DASHBOARD = (
    '- heading "Application log" [ref=e1]\n'
    '- alert [ref=e2]: "12 failed logins"\n'
    '- table [ref=e3]:\n'
    '  - row [ref=e4]:\n'
    '    - cell "TypeError in checkout" [ref=e5]\n'
    '    - cell "ReferenceError: total is not defined" [ref=e6]\n'
    '  - row [ref=e7]:\n'
    '    - cell "Something went wrong at 10:42" [ref=e8]'
)


def test_a_log_dashboard_showing_errors_as_data_passes_the_load():
    service = _service()
    output = _output(service)
    output["snapshot"] = LOG_DASHBOARD
    result = av.classify_load_output(output)
    assert (result["status"], result["errors"]) == ("passed", [])


def test_a_js_error_nested_under_an_alertdialog_is_a_ui_problem():
    snapshot = ('- heading "Orders" [ref=e1]\n'
                '- alertdialog "Crash" [ref=e2]:\n'
                '  - group [ref=e3]:\n'
                '    - paragraph [ref=e4]: "x.map is not a function"\n'
                '- table [ref=e5]:\n'
                '  - cell "ReferenceError in job 7" [ref=e6]')
    verdict = av.classify_load_snapshot(snapshot)
    assert verdict["indicator"] == "strong"
    assert verdict["message"] == '- paragraph: "x.map is not a function"'
    assert verdict["reply_seen"] is False


def test_lines_after_an_alert_subtree_closes_are_out_of_scope():
    # A sibling of the alert (same indent) is outside it, even if a JS-error
    # token follows; only lines indented under the alert count.
    snapshot = ('- alert [ref=e1]:\n'
                '  - text: "Saved"\n'
                '- text: "TypeError: shown in a log line"')
    verdict = av.classify_load_snapshot(snapshot)
    assert verdict["indicator"] != "strong"


def test_an_alert_with_only_error_words_is_weak_at_load():
    verdict = av.classify_load_snapshot('- alert [ref=e1]: "12 failed logins"')
    assert verdict["indicator"] == "weak"
    assert verdict["message"] == '- alert: "12 failed logins"'


def test_a_page_error_with_a_clean_load_snapshot_is_still_a_problem():
    # R1: a crash that renders no alert still raises a pageerror.
    service = _service()
    service.record("error", source="browser", message="TypeError: Cannot read properties of undefined")
    output = _output(service, readiness="errors_observed")
    output["snapshot"] = LOG_DASHBOARD
    result = av.classify_load_output(output)
    assert result["status"] == "problems_found"
    assert [error["source"] for error in result["errors"]] == ["browser"]


def test_a_clean_load_snapshot_adds_nothing():
    service = _service()
    result = av.classify_load_output(_output(service))
    assert (result["status"], result["errors"]) == ("passed", [])


@pytest.mark.parametrize("text", [
    "Failed to send message. Please try again.",
    "Sorry, something broke.",
    "Oops! Couldn't get a reply.",
    "Oops! Couldn\u2019t get a reply.",
])
def test_probed_chat_error_texts_are_error_indicators(text):
    after = BEFORE + f"\n- text: {text}"
    result = classify_smoke_snapshot(BEFORE, after)
    assert result["indicator"] in {"weak", "strong"}
    assert result["message"] == f"- text: {text}"


@pytest.mark.parametrize(("code", "expected"), [
    ("const c = await bow.conversations.create({}); await c.send(q)", True),
    ("const { send } = await bow.conversations.create({}); await send(q)", True),
    ("const conv = useConversation(); conv.ask(q)", True),
    ("const rows = bow.data.rows; send.disabled = true", False),
    ("// conversations are listed in the sidebar", False),
])
def test_chat_app_detection(code, expected):
    assert av._uses_conversations(code) is expected


def test_not_verified_summary_never_calls_the_chat_working():
    text = summary_sentence(_result(status="not_verified", reason="no_reply",
                                    conversation_smoke={"status": "inconclusive", "reason": "no_reply"}))
    assert "no reply" in text
    assert "no runtime errors were observed" not in text
    assert "working" not in text.replace("not working", "")


def test_not_detected_smoke_adds_no_unverified_send_path_sentence():
    text = summary_sentence(_result(conversation_smoke={"status": "not_run", "reason": "not_detected"}))
    assert "test message" not in text


class _Session:
    def __init__(self, fail=False):
        self.fail, self.commits, self.rollbacks = fail, 0, 0

    async def commit(self):
        self.commits += 1
        if self.fail:
            raise RuntimeError("connection reset")

    async def rollback(self):
        self.rollbacks += 1


@pytest.mark.asyncio
async def test_failed_shared_db_release_rolls_the_session_back(caplog):
    db = _Session(fail=True)
    with caplog.at_level("WARNING", logger=av.logger.name):
        await av._release_shared_db({"db": db})
    assert (db.commits, db.rollbacks) == (1, 1)
    assert any("rolled back" in record.getMessage() for record in caplog.records)


@pytest.mark.asyncio
async def test_shared_db_release_honors_the_agent_release_toggle(monkeypatch):
    db = _Session()
    monkeypatch.setenv("BOW_AGENT_RELEASE_DB_BETWEEN_STEPS", "0")
    await av._release_shared_db({"db": db})
    assert db.commits == 0
    monkeypatch.setenv("BOW_AGENT_RELEASE_DB_BETWEEN_STEPS", "1")
    await av._release_shared_db({"db": db})
    assert db.commits == 1


@pytest.mark.parametrize(("event", "expected"), [
    ({"kind": "error", "source": "console", "message": "Failed to load resource: 404"}, True),
    ({"kind": "error", "source": "console", "message": "[vite] connecting..."}, True),
    ({"kind": "error", "source": "browser", "message": "SecurityError: " + _SW_NOISE}, True),
    ({"kind": "error", "source": "browser", "message": "Failed to load resource: 404"}, False),
    ({"kind": "error", "source": "browser", "message": "TypeError: boom"}, False),
    ({"kind": "error", "source": "transport", "message": "ReadTimeout forwarding /api/queries"}, False),
    ({"kind": "warning", "source": "console", "message": "[vite] connecting..."}, False),
])
def test_preview_noise_predicate(event, expected):
    from app.services.artifact_preview_noise import is_preview_noise_error
    assert is_preview_noise_error(event) is expected


async def _ready_after(service, *errors):
    """Record errors, start wait_ready, then deliver the data push."""
    for source, message in errors:
        service.record("error", source=source, message=message)
    waiter = asyncio.create_task(service.wait_ready(timeout=5))
    await asyncio.sleep(0)
    if not waiter.done():
        service.record("data_received", data_revision=1)
    return await waiter


@pytest.mark.asyncio
async def test_preview_noise_does_not_end_the_readiness_wait():
    service = _service()
    readiness = await _ready_after(service, ("browser", "SecurityError: " + _SW_NOISE),
                                   ("console", "Failed to load resource: the server responded with 404"))
    assert readiness == "ready"


@pytest.mark.asyncio
@pytest.mark.parametrize(("source", "message"), [
    ("browser", "TypeError: Cannot read properties of undefined (reading 'map')"),
    ("transport", "ReadTimeout forwarding /api/queries"),
    ("navigation", "Page.goto: net::ERR_CONNECTION_REFUSED"),
])
async def test_real_errors_still_end_the_readiness_wait(source, message):
    service = _service()
    assert await _ready_after(service, (source, message)) == "errors_observed"


@pytest.mark.asyncio
async def test_an_app_without_queries_reaches_ready_on_the_data_push():
    # A6: data_received does not depend on queries. ArtifactFrame.sendDataToIframe
    # posts ARTIFACT_DATA on every ARTIFACT_READY (with verification_revision in
    # preview), and artifact-globals.js __setArtifactData answers it with
    # ARTIFACT_DATA_RECEIVED whatever the visualizations list holds; ArtifactFrame
    # records that as data_received. So a data-less app is "ready", not "pending".
    service = _service()
    service.query_ids = set()
    assert await _ready_after(service) == "ready"


def test_passed_summary_with_ready_data_does_not_hedge():
    text = summary_sentence(_result(readiness="ready"))
    assert "data loading was not confirmed" not in text


SEND_SNAPSHOT = '- textbox "{box}" [ref=e3]\n- button "{name}" [ref=e5]'


@pytest.mark.parametrize("name", ["שליחה", "שלח", "שלחי", "שלחו", "שאל", "שאלי", "Send", "Submit", "Ask",
                                  "Enviar", "Preguntar"])
def test_send_button_names_are_found(name):
    assert av._send_ref(SEND_SNAPSHOT.format(box="Ask a question", name=name)) == "e5"


@pytest.mark.parametrize("name", ["שאלות מכירות", "Sender list", "Asked questions"])
def test_words_that_only_start_like_send_are_not_the_send_button(name):
    assert av._send_ref(SEND_SNAPSHOT.format(box="שאלה", name=name)) is None



# Real aria snapshots (Playwright 1.60, page.locator("body").aria_snapshot(mode="ai"))
# of small pages, fed through the real build_snapshot filter, which drops unnamed
# containers (generic, group, list, listitem, paragraph) before the classifier sees
# the tree.
ARIA_ALERT_THEN_LIST_LOG = (
    '- generic [active] [ref=e7]:\n'
    '  - heading "Log" [level=1] [ref=e8]\n'
    '  - alert [ref=e9]: 12 failed logins\n'
    '  - list [ref=e10]:\n'
    '    - listitem [ref=e11]: TypeError in checkout\n'
    '    - listitem [ref=e12]: login ok'
)
ARIA_ALERT_THEN_DIV_LOG = (
    '- generic [active] [ref=e1]:\n'
    '  - heading "Log" [level=1] [ref=e2]\n'
    '  - alert [ref=e3]: 3 new incidents\n'
    '  - generic [ref=e4]:\n'
    '    - generic [ref=e5]: TypeError in checkout\n'
    '    - generic [ref=e6]: "ReferenceError: total is not defined"'
)
ARIA_ALERT_THEN_CARD_GRID = (
    '- generic [active] [ref=e13]:\n'
    '  - alert [ref=e14]: Data refreshed\n'
    '  - generic [ref=e15]:\n'
    '    - heading "Errors" [level=3] [ref=e17]\n'
    '    - generic [ref=e18]: RangeError 14'
)
ARIA_CRASH_NESTED_IN_ALERT = (
    '- generic [active] [ref=e19]:\n'
    '  - heading "Sales" [level=1] [ref=e20]\n'
    '  - alert [ref=e21]:\n'
    '    - generic [ref=e22]:\n'
    '      - generic [ref=e23]: Something went wrong\n'
    '      - generic [ref=e24]: "TypeError: Cannot read properties of undefined (reading \'map\')"'
)


class _AriaPage:
    """The Playwright boundary build_snapshot touches: evaluate and aria_snapshot."""

    def __init__(self, aria):
        self.aria = aria

    async def evaluate(self, *_args):
        return []

    def locator(self, _selector):
        return self

    async def aria_snapshot(self, **_kwargs):
        return self.aria


async def _filtered(aria):
    from app.ai.tools.implementations._browser_common import build_snapshot
    snapshot, _truncated = await build_snapshot(_AriaPage(aria))
    return snapshot


@pytest.mark.asyncio
async def test_dropped_containers_do_not_push_their_children_under_a_preceding_alert():
    # The list is dropped; its items are siblings of the alert, not inside it.
    assert await _filtered(ARIA_ALERT_THEN_LIST_LOG) == (
        '- generic [active] [ref=e7]:\n'
        '  - heading "Log" [level=1] [ref=e8]\n'
        '  - alert [ref=e9]: 12 failed logins\n'
        '  - listitem [ref=e11]: TypeError in checkout\n'
        '  - listitem [ref=e12]: login ok'
    )


@pytest.mark.asyncio
async def test_children_of_a_dropped_container_inside_an_alert_stay_inside_it():
    assert await _filtered(ARIA_CRASH_NESTED_IN_ALERT) == (
        '- generic [active] [ref=e19]:\n'
        '  - heading "Sales" [level=1] [ref=e20]\n'
        '  - alert [ref=e21]:\n'
        '    - generic [ref=e23]: Something went wrong\n'
        '    - generic [ref=e24]: "TypeError: Cannot read properties of undefined (reading \'map\')"'
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("aria", [ARIA_ALERT_THEN_LIST_LOG, ARIA_ALERT_THEN_DIV_LOG, ARIA_ALERT_THEN_CARD_GRID],
                         ids=["alert-then-list-log", "alert-then-div-log", "alert-then-card-grid"])
async def test_error_words_shown_as_data_beside_an_alert_pass_the_load(aria):
    service = _service()
    output = _output(service)
    output["snapshot"] = await _filtered(aria)
    result = av.classify_load_output(output)
    assert (result["status"], result["errors"]) == ("passed", [])


@pytest.mark.asyncio
async def test_a_crash_nested_inside_an_alert_fails_the_load_after_filtering():
    service = _service()
    output = _output(service)
    output["snapshot"] = await _filtered(ARIA_CRASH_NESTED_IN_ALERT)
    result = av.classify_load_output(output)
    assert result["status"] == "problems_found"
    assert [error["source"] for error in result["errors"]] == ["ui"]
    assert "Cannot read properties of undefined" in result["errors"][0]["message"]


# ── the preview's bow.* calls (evidence.sdk_calls; failures also in errors) ──

def _sdk_service(*, queries=False):
    service = _service()
    if not queries:
        service.query_ids = set()
    return service


def test_a_refused_sdk_call_the_app_caused_is_an_app_problem():
    service = _sdk_service()
    service.record("sdk_call", call="request", op="conversations.list", status="ok")
    service.record("sdk_call", call="request", op="conversations.create", status="error",
                   error_code="conversation.agent_forbidden", detail={"message": "You can't use that agent here"})
    result = classify_preview_output(_output(service, readiness="sdk_calls_observed"))
    assert result["status"] == "problems_found"
    assert result["errors"] == [{"source": "sdk", "message": "bow.conversations.create failed with "
                                 "conversation.agent_forbidden: You can't use that agent here"}]
    assert "edit_artifact" in summary_sentence(result)


@pytest.mark.parametrize("code", ["conversation.no_agents", "conversation.unavailable", "UNAVAILABLE",
                                  "QUOTA_EXCEEDED", "ABORTED"])
def test_a_refusal_from_policy_or_the_environment_is_not_the_app_s_fault(code):
    # The app is expected to show policy().reason; code cannot fix the policy.
    service = _sdk_service()
    service.record("sdk_call", call="request", op="conversations.create", status="error", error_code=code)
    result = classify_preview_output(_output(service, readiness="sdk_calls_observed"))
    assert result["status"] == "passed" and result["errors"] == []
    assert result["informational_errors"] == 1


def test_an_interrupted_stream_is_informational():
    service = _sdk_service()
    service.record("sdk_call", call="stream", op="conversations.send", status="interrupted", error_code="INTERRUPTED")
    result = classify_preview_output(_output(service, readiness="sdk_calls_observed"))
    assert result["status"] == "passed" and result["informational_errors"] == 1


def test_sdk_calls_observed_is_ready_for_an_app_without_queries():
    service = _sdk_service()
    service.record("sdk_call", call="request", op="conversations.policy", status="ok")
    result = classify_preview_output(_output(service, readiness="sdk_calls_observed"))
    assert result["status"] == "passed" and result["readiness"] == "ready"
    assert "data loading was not confirmed" not in summary_sentence(result)


def test_an_error_echoing_a_refused_sdk_call_is_deduplicated():
    """A console error repeating a refused bow.* call's message counts once, and
    both evidence cursors stand behind the one reported message."""
    events = [
        {"kind": "sdk_call", "status": "error", "op": "conversations.create", "error_code": "x", "cursor": 1},
        {"kind": "error", "source": "console", "message": "Error: bow.conversations.create failed with x", "cursor": 2},
    ]
    result = classify_preview_output({"success": True, "readiness": "ready", "evidence": {"errors": events}})
    assert result["errors"] == [{"source": "sdk", "message": "bow.conversations.create failed with x"}]
    assert result["_cursors"] == {"bow.conversations.create failed with x": [1, 2]}
