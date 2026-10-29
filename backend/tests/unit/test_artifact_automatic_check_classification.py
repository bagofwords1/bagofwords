"""Classification contract of the automatic artifact browser check.

The evidence fed to the classifier is produced by a real
``ArtifactPreviewService`` (events recorded through ``record`` and a viewer
query answered through the broker route), wrapped in the ``BrowserOutput``
shape the guarded browser tools return. Only the HTTP response of the
forwarded query is replaced (the true boundary).
"""
from __future__ import annotations

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
    service = _service()
    output = _output(service)
    output["snapshot"] = ('- heading "Sales" [ref=e1]\n'
                          '- text: "TypeError: Cannot read properties of undefined (reading \'map\')"')
    result = av.classify_load_output(output)
    assert result["status"] == "problems_found"
    assert result["errors"] == [{"source": "ui", "message":
                                 "- text: \"TypeError: Cannot read properties of undefined (reading 'map')\""}]


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
