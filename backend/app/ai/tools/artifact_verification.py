"""Artifact verification guidance and live main-loop tool availability."""
from __future__ import annotations

import asyncio
import contextlib
import functools
import logging
import os
import re
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


async def refresh_browser_tool_catalog(catalog, candidates, runtime_ctx):
    """Refresh only browser tools, preserving other run-specific catalog filters."""
    import logging
    from app.services.artifact_verification_policy import browser_policy
    from app.ai.tools.implementations._browser_common import session_manager

    available = False
    preview_allowed = False
    try:
        policy = await browser_policy(runtime_ctx)
        available, preview_allowed = policy.available, policy.artifact_preview
    except Exception:
        logging.getLogger(__name__).warning(
            "Browser eligibility could not be checked; hiding browser tools", exc_info=True)
    if not preview_allowed:
        await session_manager.close_preview(runtime_ctx)
    result = [t for t in (catalog or []) if not t.name.startswith("browser_")]
    if available:
        result.extend(candidates)
    return result


def _interaction_signature(code: str) -> list[str]:
    # Advisory: identify state and handlers, not text, CSS, chart configuration,
    # or the mere number of datasets. This is not a JavaScript correctness gate.
    hooks = re.findall(r"(?:useState|useReducer|useParams|setParam|setParams)\s*\([^\n;]{0,240}?\)", code)
    handlers = re.findall(r"(?:onClick|onChange|onSubmit|onKeyDown)\s*=\s*\{[^\n]*?\}(?=\s|>)", code)
    return hooks + handlers


def _functional_code(code: str) -> str:
    # Conservative advisory diff: ignore obvious presentation-only changes,
    # but retain handler bodies, query bindings, transforms and parameter values.
    code = re.sub(r"/\*.*?\*/|(?m:^\s*//[^\n]*)", "", code, flags=re.S)
    code = re.sub(r"""\bclassName\s*=\s*(?:"[^"\n]*"|'[^'\n]*')""", "", code)
    code = re.sub(r"\bstyle\s*=\s*\{\{[^{}]*\}\}", "", code)
    code = re.sub(r">[^<>{}]*<", "><", code)
    return re.sub(r"\s+", "", code)


def build_artifact_verification_hint(*, artifact_id: str, version: int,
                                     mode: str = "page", code: str = "",
                                     parameters: list[dict] | None = None,
                                     previous_code: str | None = None,
                                     available: bool = False) -> dict:
    adjustable = sorted({p.get("name") for p in parameters or []
                         if p.get("name") and p.get("source") != "identity"})
    signature = _interaction_signature(code)
    changed = previous_code is None or _functional_code(code) != _functional_code(previous_code)
    reasons = []
    if mode == "page" and changed:
        if adjustable:
            reasons.append("backend_parameters")
        if signature:
            reasons.append("interaction_changed" if previous_code is not None else "interactive_app")
    return {
        "recommended": available and bool(reasons), "reason_codes": reasons,
        "focus": adjustable or (["primary interaction", "close or reset"] if reasons else []),
        "artifact_id": artifact_id, "version": version,
        "next_tool": "browser_navigate" if available and reasons else None,
        "next_tool_input": {"artifact_id": artifact_id} if available and reasons else None,
        "availability": "available" if available else "unavailable",
    }


ARTIFACT_VERIFICATION_POLICY = """
verification_hint.automatic_check, when present, is a system-run check of the
saved version in the real viewer (no model call). problems_found with errors:
repair the app code with edit_artifact before finishing; the repaired version is
checked automatically. problems_found with failed_queries only: the app code may
be fine; inspect the query with read_query, repair it with create_data, then
reconnect with edit_artifact if needed. Never call the app working while problems
are listed. passed: no runtime errors were observed while it loaded (and, if
checked includes conversation_send, a preview message showed the preview reply
with no visible error). It does not test filters or every control: when
reason_codes include backend_parameters, still exercise them as described below.
A conversation_smoke that is inconclusive or not_run leaves the send path
unverified. not_verified (reason no_reply): the app loaded, but a test message
got no reply although the preview always replies at once, so the send path is
probably broken: inspect it and repair it with edit_artifact; never call the
chat working. unavailable: follow the guidance below or state that
interactions are unverified.
After successful page create/edit, use verification_hint to decide whether an
interactive check is useful. A simple static dashboard must finish normally:
do not call browser tools merely to verify a static dashboard or a copy/CSS edit.
When verification_hint.availability is available and verification is recommended,
call browser_navigate with the exact returned artifact_id (no browser connector
required), then exercise a few relevant
controls using browser_act. Browser results already include fresh snapshots and
automatic query/runtime evidence: do not mechanically call snapshot after each act.
For backend filters use expect_query_update={"params": {"parameter_name": value}}
using declared names. The tool derives all affected query IDs from the authorized
manifest, so a control that sends nothing is detected without retyping UUIDs.
Only provide explicit query_ids when checking a deliberate subset. Use the exact snapshot refs,
including frame prefixes. Read applied parameters and fresh visible results;
HTTP success and data_received prove transport, not business correctness. Read
result_checks: inconclusive checks require investigation or an explicitly unverified
final outcome. Never call a parameter_mismatch, pending result, or unexplained
empty result a passed check.
For date controls, first identify a known record/date from the unfiltered data;
choose a range containing it and confirm it remains present with returned dates
inside the intended range; where available, confirm a known outside record is
excluded. Clear other filters or account for their intersection.
Test both endpoints, reset, and an open bound if supported. Empty output is not
proof of a correct date filter. On zero rows inspect the existing query (read_query)
and its from/to keys, boundary comparisons, source date format and timezone;
repair a broken query via create_data and reconnect with edit_artifact. Calendar
ranges use YYYY-MM-DD from/to; exact timestamps retain their precision/offset.
Legacy queries may accept mixed or reversed bounds for compatibility. Treat
mixed_date_range/reversed_date_range result checks as inconclusive: confirm the
source semantics and final picker state rather than assuming acceptance proves
correctness or silently converting/swapping bounds.
Do not guess a timezone for naive timestamps or text columns. Inspect datasets
and query evidence: result_truncated means displayed rows are only part of the
result. Do not certify full totals from partial rows; use a backend aggregate
query with the same parameters or clearly label the limitation INSIDE the app;
a chat caveat alone does not fix a misleading KPI. Never disable
an organization's row limit to make a check pass. For reset, compare the initial
and restored visible totals; also open and close the primary detail/modal. Use browser_vision
only when appearance needs inspection. A pending result is not a failure or pass.
Supply friendly titles in the user's language, e.g. 'Verifying music album filters',
'Checking track details', 'Rechecking the genre filter after the fix'. They appear
in one group with a spinner and shimmer. No extra call is needed to update a title.
If an observed bug needs repair, edit_artifact then open the NEW artifact_id and
repeat that flow. At most two functional repair passes; keep checks focused.
Unavailable/restricted verification: explain what was not checked and finish.
Never claim interactions were tested from a static screenshot, or claim a live
write/MCP operation was tested in this read-only preview. Report actual checked
scope and unresolved findings; do not perform redundant create_data queries.
""".strip()



# ---------------------------------------------------------------------------
# Automatic browser check after an interactive page create/edit.
#
# A bounded, system-run check of the saved version through the same guarded
# browser tools the model uses (guard, policy, preview broker, session
# manager). It observes and reports; repair stays with the agent.
# Budgets derive from the runner's TimeoutPolicy(idle=180, hard=300) and its
# retry of create/edit: the check must never trip either timeout.
# ---------------------------------------------------------------------------

AUTOMATIC_CHECK_TIMEOUT_S: float = 45          # load (navigate) budget
AUTOMATIC_CHECK_SMOKE_TIMEOUT_S: float = 20    # conversation smoke budget
AUTOMATIC_CHECK_CLEANUP_TIMEOUT_S: float = 10  # close_preview + final access check
AUTOMATIC_CHECK_DB_RELEASE_WAIT_S: float = 10  # wait for tool_db_lock before the pre-check commit
AUTOMATIC_CHECK_LOCK_WAIT_S: float = 100       # wait for another check in the same run
AUTOMATIC_CHECK_LATEST_START_S: float = 210    # no browser work after this much tool time

# Must match fixtureConversation() in frontend/public/libs/artifact-sdk.js
FIXTURE_CONVERSATION_REPLY: str = "No agent was called."

# Must match the root wait in _artifact_browser.py (navigate_artifact). Only
# that wait, which runs after goto and the iframe wait succeeded, can record
# a navigation error containing this selector.
_ROOT_RENDER_MARKER = "#root > *"
_RENDER_ERROR = {"source": "render",
                 "message": "The saved app rendered nothing: the #root element stayed empty for 20 s."}
_FROZEN_ERROR = {"source": "render",
                 "message": "The saved app stopped responding after it started rendering: the load did not "
                            "finish within the check budget."}
_RESTRICTED_CODES = {"preview_restricted", "session_scope_mismatch"}
_HOST_NOISE_PREFIXES = ("Failed to load resource", "[vite]", "[nuxt]", "[Vue warn]", "[HMR]")
# Playwright's service_workers="block" (preview contexts, _browser_common.py)
# installs an init script that reads navigator.serviceWorker in every frame; in
# the sandboxed artifact iframe that read throws. Not app code.
_SANDBOX_SW_NOISE = "Failed to read the 'serviceWorker' property from 'Navigator'"
_BREAKER_REASONS = {"preview_unavailable", "preview_not_ready", "timeout"}
_MAX_ITEMS = 5
_MAX_MESSAGE = 300

_REF_TOKEN = re.compile(r"\s*\[ref=[^\]]*\]")
_STRONG_TOKENS = ("TypeError", "ReferenceError", "SyntaxError", "RangeError", "Cannot read properties",
                  "is not a function", "undefined is not", "is not defined", "Unexpected token",
                  "Maximum update depth")
_ALERT_LINE = re.compile(r"^\s*-\s*(?:alert|alertdialog)\b")
_ALERT_STRONG = re.compile(r"error|failed|went wrong", re.I)
_WEAK_TEXT = re.compile(r"\b(?:error|failed|went wrong|could not|couldn['\u2019]t|cannot|can['\u2019]t|unable to|"
                        r"broke|broken|oops|sorry|try again)\b", re.I)
# Prefixes under which one crash is echoed (runtime event, the host's console
# echo, the pageerror): stripped to count the crash once.
_ECHO_PREFIX = re.compile(r"^\s*(?:\[ArtifactFrame\] Iframe render error:\s*)?(?:Uncaught\s+)?"
                          r"(?:(?:[A-Z]\w*)?Error:\s*)?")
_SEND_WORD = re.compile(r"\bsend\b")
_TEXTBOX_LINE = re.compile(r"^\s*-\s*textbox\b[^\n]*?\[ref=([^\]]+)\]", re.M)
_BUTTON_LINE = re.compile(r'^\s*-\s*button\s+"([^"]*)"[^\n]*?\[ref=([^\]]+)\]', re.M)
_SEND_NAME = re.compile(r"\b(?:send|submit|ask|enviar|preguntar|שלח|שאל)\b", re.I)


@dataclass
class ArtifactCheckRun:
    """Run-scoped state shared by every automatic check of one execution."""
    execution_id: str | None
    smoke_allowed: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    unavailable_reason: str | None = None  # breaker; set once, never cleared within the execution


def _clip(text, limit=_MAX_MESSAGE):
    return str(text or "")[:limit]


def _evidence_counts():
    return {"errors": [], "failed_queries": [], "blocked_requests": 0, "informational_errors": 0}


def classify_preview_output(output: dict) -> dict:
    """Classify one guarded browser tool output (load or act) per the D8 rules."""
    output = output or {}
    result = {"status": "passed", "reason": None, "readiness": output.get("readiness"), **_evidence_counts()}
    if output.get("error_code") in _RESTRICTED_CODES:
        return {**result, "status": "unavailable", "reason": "restricted", "readiness": None}
    evidence = output.get("evidence") or {}
    errors, render, navigation, transport, seen = [], False, 0, False, set()
    for event in evidence.get("errors") or []:
        if event.get("kind") == "blocked":
            result["blocked_requests"] += 1
            continue
        if event.get("kind") != "error":
            continue
        source = event.get("source") or "bridge"
        message = str(event.get("message") or "")
        if source == "navigation":
            if _ROOT_RENDER_MARKER in message:
                render = True
            else:
                navigation += 1
                result["informational_errors"] += 1
        elif source == "transport":
            transport = True
            result["informational_errors"] += 1
        elif source == "console" and message.startswith(_HOST_NOISE_PREFIXES):
            result["informational_errors"] += 1
        elif _SANDBOX_SW_NOISE in message:
            result["informational_errors"] += 1
        else:
            key = _ECHO_PREFIX.sub("", message).strip()
            if key in seen:
                continue
            seen.add(key)
            errors.append({"source": source, "message": _clip(message)})
    if render:
        errors.append(dict(_RENDER_ERROR))
    failed = [{"query_id": str(q.get("query_id")), "status": str(q.get("status")),
               "error": _clip(q["error"]) if q.get("error") is not None else None}
              for q in evidence.get("queries") or [] if q.get("status") != "success"]
    if render and len(errors) > _MAX_ITEMS:
        errors = errors[:_MAX_ITEMS - 1] + [errors[-1]]
    result["errors"], result["failed_queries"] = errors[:_MAX_ITEMS], failed[:_MAX_ITEMS]
    if errors or failed:
        result["status"] = "problems_found"
    elif output.get("success") is False and navigation and not transport:
        result.update(status="unavailable", reason="preview_not_ready")
    elif output.get("success") is False:
        # A transport error on a load that succeeded stays informational: the
        # app rendered, so it is neither an app problem nor an outage.
        result.update(status="unavailable", reason="preview_unavailable")
    return result


def classify_load_output(output: dict) -> dict:
    """classify_preview_output for the load, plus an error the app rendered on screen."""
    result = classify_preview_output(output)
    if result["status"] == "unavailable" or (output or {}).get("success") is False:
        return result
    verdict = classify_smoke_snapshot("", (output or {}).get("snapshot") or "")
    if verdict["indicator"] == "strong":
        result["errors"] = (result["errors"] + [{"source": "ui", "message": verdict["message"]}])[:_MAX_ITEMS]
        result["status"] = "problems_found"
    return result


def classify_smoke_snapshot(before: str, after: str) -> dict:
    """Look for error indicators among lines that appeared after the test send."""
    def lines(text):
        return [_REF_TOKEN.sub("", line).rstrip() for line in (text or "").splitlines()]

    seen = set(lines(before))
    fresh = [line for line in lines(after) if line.strip() and line not in seen]
    strong = weak = None
    for line in fresh:
        is_alert = bool(_ALERT_LINE.match(line))
        if any(token in line for token in _STRONG_TOKENS) or (is_alert and _ALERT_STRONG.search(line)):
            strong = strong or line
        elif is_alert or _WEAK_TEXT.search(line):
            weak = weak or line
    line = strong or weak
    return {"reply_seen": FIXTURE_CONVERSATION_REPLY in (after or ""),
            "indicator": "strong" if strong else "weak" if weak else None,
            "message": _clip(line.strip()) if line else None}


def summary_sentence(result: dict) -> str:
    """One model-facing sentence (two when both problem kinds exist)."""
    status = result.get("status")
    if status == "unavailable":
        return (f"Automatic browser check could not run ({result.get('reason')}); "
                "interactions remain unverified.")
    if status == "not_verified":
        return ("Automatic browser check: the saved version loaded, but a test message sent through the app's "
                "composer got no reply in the preview, which always replies at once. The send path is probably "
                "broken: inspect it and repair it with edit_artifact. Until a recheck shows the reply, the chat "
                "is unverified.")
    if status == "passed":
        text = ("Automatic browser check: no runtime errors were observed while the saved "
                "version loaded in the real viewer")
        if result.get("readiness") != "ready":
            text += f"; data loading was not confirmed (readiness: {result.get('readiness')})"
        smoke = result.get("conversation_smoke")
        if smoke and smoke.get("reason") == "not_detected":
            pass
        elif smoke and smoke.get("status") == "passed":
            text += "; a test message received the preview reply with no visible error"
        elif smoke:
            text += f"; the test message path was not verified ({smoke.get('reason') or smoke.get('status')})"
        return text + "."
    parts = []
    errors, failed = result.get("errors") or [], result.get("failed_queries") or []
    if errors:
        parts.append(f"Automatic browser check found {len(errors)} runtime problem(s): {errors[0]['message']}. "
                     "Fix the app code with edit_artifact; the new version is checked automatically.")
    if failed:
        parts.append(f"Automatic browser check: {len(failed)} viewer query(ies) failed: "
                     f"{failed[0].get('error') or failed[0].get('status')}. The app code may be fine: "
                     "inspect the query with read_query and repair it with create_data.")
    return " ".join(parts)


def _new_result(artifact_id, version):
    return {"status": "unavailable", "reason": None, "artifact_id": artifact_id, "version": version,
            "checked": [], "readiness": None, **_evidence_counts(), "conversation_smoke": None,
            "duration_ms": 0}


def _emptied(result, reason):
    return {**result, "status": "unavailable", "reason": reason, "checked": [], "readiness": None,
            **_evidence_counts(), "conversation_smoke": None}


def _uses_conversations(code):
    code = code or ""
    return "useConversation(" in code or ("conversations" in code and _SEND_WORD.search(code) is not None)


def _composer_ref(snapshot):
    match = _TEXTBOX_LINE.search(snapshot or "")
    return match[1] if match else None


def _send_ref(snapshot):
    for name, ref in _BUTTON_LINE.findall(snapshot or ""):
        if _SEND_NAME.search(name):
            return ref
    return None


def _lock_wait_s(t0, now):
    """Bounded lock wait, or None when the latest start time already passed."""
    elapsed = now - t0
    if elapsed > AUTOMATIC_CHECK_LATEST_START_S:
        return None
    return min(AUTOMATIC_CHECK_LOCK_WAIT_S, AUTOMATIC_CHECK_LATEST_START_S - elapsed)


def _event_parts(event):
    if isinstance(event, dict):
        return event.get("type"), event.get("payload") or {}
    return getattr(event, "type", None), getattr(event, "payload", None) or {}


async def _last_output(tool, tool_input, runtime_ctx):
    """Drive a guarded browser tool; inner events are consumed, never forwarded."""
    output = None
    async with contextlib.aclosing(tool.run_stream(tool_input, runtime_ctx)) as stream:
        async for event in stream:
            event_type, payload = _event_parts(event)
            if event_type == "tool.end":
                output = payload.get("output") or {}
    return output if output is not None else {"success": False, "error_code": "preview_unavailable"}


def _log_ids(runtime_ctx, result):
    return (f"execution_id={runtime_ctx.get('agent_execution_id')} tool_call_id={runtime_ctx.get('tool_call_id')} "
            f"artifact_id={result.get('artifact_id')} version={result.get('version')}")


def _preview_session(runtime_ctx):
    """The internal preview session this execution has open, if any."""
    from app.ai.tools.implementations._browser_common import session_manager

    scope = session_manager.scope_for(runtime_ctx)
    return next((s for s in list(session_manager._sessions.values()) if s.preview and s.scope == scope), None)


def _classify_frozen_load(runtime_ctx):
    """On a load timeout: when the app frame was already reached, the page froze
    while rendering (an app problem); classify the evidence it produced so far.
    None when the preview never got that far (infrastructure)."""
    session = _preview_session(runtime_ctx)
    if session is None or session.frame is None:
        return None
    load = classify_preview_output({"success": True, "readiness": None,
                                    "evidence": session.preview.evidence(since=0)})
    load["errors"] = load["errors"][:_MAX_ITEMS - 1] + [dict(_FROZEN_ERROR)]
    load.update(status="problems_found", reason=None)
    return load


def _merge(result, classified):
    result["errors"] = (result["errors"] + classified["errors"])[:_MAX_ITEMS]
    result["failed_queries"] = (result["failed_queries"] + classified["failed_queries"])[:_MAX_ITEMS]
    result["blocked_requests"] += classified["blocked_requests"]
    result["informational_errors"] += classified["informational_errors"]


async def _conversation_smoke(runtime_ctx, session_id, snapshot, state):
    """Send one fixture-mode message (at most 3 acts) and record the outcome in state."""
    from app.ai.tools.implementations.browser_act import BrowserActTool

    composer = _composer_ref(snapshot)
    if composer is None:
        state["smoke"] = {"status": "not_run", "reason": "no_composer"}
        return

    async def act(action, ref, text=None):
        output = await _last_output(BrowserActTool(), {
            "session_id": session_id, "ref": ref, "action": action, "text": text,
            "title": "Checking the saved app"}, runtime_ctx)
        if output.get("error_code") in _RESTRICTED_CODES:
            state["restricted"] = True
            return None
        classified = classify_preview_output({**output, "success": True})
        _merge(state["evidence"], classified)
        if classified["errors"] or classified["failed_queries"]:
            state["smoke"] = {"status": "problems_found", "reason": None}
            return None
        if output.get("success") is False:
            state["smoke"] = {"status": "inconclusive", "reason": "action_failed"}
            return None
        return output

    def judge(output):
        verdict = classify_smoke_snapshot(snapshot, output.get("snapshot") or "")
        if verdict["indicator"]:
            # The fixture SDK always replies at once: error text after the
            # send, even weakly worded, is the app's own send path failing.
            state["evidence"]["errors"] = (state["evidence"]["errors"]
                                           + [{"source": "ui", "message": verdict["message"]}])[:_MAX_ITEMS]
            state["smoke"] = {"status": "problems_found",
                              "reason": None if verdict["indicator"] == "strong" else "possible_error_text"}
        elif verdict["reply_seen"]:
            state["smoke"] = {"status": "passed", "reason": None}
        else:
            return False
        return True

    if await act("type", composer, "Preview check") is None:
        return
    output = await act("press", composer, "Enter")
    if output is None or judge(output):
        return
    send = _send_ref(output.get("snapshot") or "") or _send_ref(snapshot)
    if send is not None:
        output = await act("click", send)
        if output is None or judge(output):
            return
    state["smoke"] = {"status": "inconclusive", "reason": "no_reply"}


async def run_automatic_artifact_check(runtime_ctx: dict, *, artifact_id: str, version: int, code: str) -> dict:
    """Load the saved version (plus a fixture smoke for chat apps) and classify it.

    Never raises except to honor cancellation. Evidence is returned only when
    the final access check, run inside the cleanup budget, returned normally.
    """
    from app.ai.tools.implementations._browser_common import session_manager
    from app.ai.tools.implementations.browser_navigate import BrowserNavigateTool
    from app.services.artifact_verification_policy import require_browser_access

    started = time.monotonic()
    run = runtime_ctx.get("artifact_check_run")
    result = _new_result(artifact_id, version)
    restricted = failed = cancelled = final_ok = False
    try:
        navigation = None
        try:
            async with asyncio.timeout(AUTOMATIC_CHECK_TIMEOUT_S):
                navigation = await _last_output(BrowserNavigateTool(), {
                    "artifact_id": artifact_id, "title": "Checking the saved app"}, runtime_ctx)
        except TimeoutError:
            frozen = _classify_frozen_load(runtime_ctx)
            if frozen is None:
                result["reason"] = "timeout"
            else:
                result.update(frozen)
                result["checked"] = ["load"]
        if navigation is not None:
            load = classify_load_output(navigation)
            restricted = load["reason"] == "restricted"
            result.update(load)
            if load["status"] in ("passed", "problems_found"):
                result["checked"] = ["load"]
            if load["status"] == "passed" and not _uses_conversations(code):
                result["conversation_smoke"] = {"status": "not_run", "reason": "not_detected"}
            elif load["status"] == "passed":
                if not getattr(run, "smoke_allowed", False):
                    result["conversation_smoke"] = {"status": "not_run", "reason": "act_not_allowed"}
                elif navigation.get("session_id") and navigation.get("snapshot"):
                    state = {"smoke": None, "restricted": False, "evidence": _evidence_counts()}
                    try:
                        async with asyncio.timeout(AUTOMATIC_CHECK_SMOKE_TIMEOUT_S):
                            await _conversation_smoke(runtime_ctx, navigation["session_id"],
                                                      navigation["snapshot"], state)
                    except TimeoutError:
                        state["smoke"] = {"status": "inconclusive", "reason": "timeout"}
                    restricted = restricted or state["restricted"]
                    smoke = state["smoke"] or {"status": "inconclusive", "reason": None}
                    result["conversation_smoke"] = smoke
                    _merge(result, state["evidence"])
                    if smoke["status"] in ("passed", "problems_found"):
                        result["checked"] = ["load", "conversation_send"]
                    if result["errors"] or result["failed_queries"]:
                        result["status"] = "problems_found"
                    elif smoke["reason"] == "no_reply":
                        # The fixture SDK always replies at once: no reply means
                        # the send path is broken (or was not found). Never a pass.
                        result.update(status="not_verified", reason="no_reply")
                else:
                    result["conversation_smoke"] = {"status": "inconclusive", "reason": None}
    except asyncio.CancelledError:
        cancelled = True
        raise
    except Exception:
        logger.warning("Automatic artifact check failed: %s", _log_ids(runtime_ctx, result), exc_info=True)
        failed = True
    finally:
        # One shared cleanup budget: closing the preview and the final access
        # check. Access revoked after the last inner tool.end is caught here.
        try:
            async with asyncio.timeout(AUTOMATIC_CHECK_CLEANUP_TIMEOUT_S):
                try:
                    await session_manager.close_preview(runtime_ctx)
                except Exception:
                    logger.warning("Automatic artifact check could not close its preview: %s", _log_ids(runtime_ctx, result),
                                   exc_info=True)
                if not cancelled:
                    await require_browser_access(runtime_ctx, artifact_id)
                    final_ok = True
        except PermissionError:
            restricted = True
        except Exception:
            if not cancelled:
                logger.warning("Automatic artifact check cleanup failed: %s", _log_ids(runtime_ctx, result), exc_info=True)
            failed = True
    result["duration_ms"] = int((time.monotonic() - started) * 1000)
    # Privacy precedence: applied last, it overrides everything above.
    if restricted or failed or not final_ok:
        reason = "restricted" if restricted else "check_failed"
        # The status only: the evidence is exactly what is being withheld.
        logger.info("automatic_artifact_check withheld its result: reason=%s status=%s %s",
                    reason, result["status"], _log_ids(runtime_ctx, result))
        return _emptied(result, reason)
    return result


async def _release_shared_db(runtime_ctx):
    """Commit the agent's shared session so its connection is not held idle in
    a transaction during the browser work (mirrors _release_db_between_steps)."""
    db = runtime_ctx.get("db")
    if db is None or os.getenv("BOW_AGENT_RELEASE_DB_BETWEEN_STEPS", "1") != "1":
        return
    lock = runtime_ctx.get("tool_db_lock")
    if lock is not None:
        try:
            async with asyncio.timeout(AUTOMATIC_CHECK_DB_RELEASE_WAIT_S):
                await lock.acquire()
        except TimeoutError:
            logger.warning("Automatic artifact check skipped the shared DB release: tool_db_lock is busy")
            return
    try:
        await db.commit()  # never inside a timeout: a cancelled commit leaves the connection unknown
    except Exception:
        # A failed commit leaves the shared session in a failed transaction:
        # roll it back so the agent's next statement does not fail on it.
        try:
            await db.rollback()
            logger.warning("Automatic artifact check could not release the shared DB session; rolled back",
                           exc_info=True)
        except Exception:
            logger.warning("Automatic artifact check could not release or roll back the shared DB session",
                           exc_info=True)
    finally:
        if lock is not None:
            lock.release()


def _skip_reason(event, runtime_ctx):
    """None when this event starts the check; otherwise why not ("" for events
    that are not a tool result)."""
    event_type, payload = _event_parts(event)
    output = payload.get("output") if event_type == "tool.end" else None
    if not isinstance(output, dict):
        return ""
    if output.get("success") is False:
        return "tool_failed"
    hint = output.get("verification_hint")
    if not isinstance(hint, dict) or hint.get("recommended") is not True:
        return "not_recommended"
    if hint.get("availability") != "available":
        return "verification_unavailable"
    if not isinstance(runtime_ctx.get("artifact_check_run"), ArtifactCheckRun):
        return "no_run_state"
    return None


def _log_skip(reason, event, runtime_ctx):
    _, payload = _event_parts(event)
    hint = (payload.get("output") or {}).get("verification_hint")
    hint = hint if isinstance(hint, dict) else {}
    logger.log(logging.INFO if reason == "verification_unavailable" else logging.DEBUG,
               "automatic_artifact_check skipped: reason=%s %s", reason, _log_ids(runtime_ctx, hint))


def _attach(payload, result):
    hint = payload["output"]["verification_hint"]
    hint["automatic_check"] = result
    observation = payload.get("observation")
    if isinstance(observation, dict):
        observed = observation.get("verification_hint")
        if not isinstance(observed, dict):
            observation["verification_hint"] = observed = hint
        observed["automatic_check"] = result
        observation["summary"] = f"{observation.get('summary') or ''} {summary_sentence(result)}".strip()


def with_automatic_artifact_check(run_stream):
    """Decorate CreateArtifactTool/EditArtifactTool.run_stream with the automatic check."""
    @functools.wraps(run_stream)
    async def wrapped(self, tool_input, runtime_ctx):
        # First statement, unconditional: a runner retry reuses the same ctx,
        # so elapsed time always counts from the first attempt.
        t0 = runtime_ctx.setdefault("_artifact_check_t0", time.monotonic())
        from app.ai.tools.schemas import ToolProgressEvent

        done = False
        async with contextlib.aclosing(run_stream(self, tool_input, runtime_ctx)) as stream:
            async for event in stream:
                skip = "" if done else _skip_reason(event, runtime_ctx)
                if skip is not None:
                    if skip:
                        _log_skip(skip, event, runtime_ctx)
                    yield event
                    continue
                done = True
                _, payload = _event_parts(event)
                hint = payload["output"]["verification_hint"]
                run = runtime_ctx["artifact_check_run"]
                started = time.monotonic()
                yield ToolProgressEvent(type="tool.progress",
                                        payload={"stage": "validating", "check": "automatic_browser"})
                await _release_shared_db(runtime_ctx)
                result = None
                wait = None if run.unavailable_reason else _lock_wait_s(t0, time.monotonic())
                if run.unavailable_reason:
                    reason = "skipped_after_unavailable"
                elif wait is None:
                    reason = "time_budget"
                else:
                    reason = None
                    try:
                        async with asyncio.timeout(wait):
                            await run.lock.acquire()
                    except TimeoutError:
                        reason = "time_budget"
                    if reason is None:
                        try:
                            yield ToolProgressEvent(type="tool.progress", payload={
                                "stage": "validating", "check": "automatic_browser", "timing": False})
                            if run.unavailable_reason:
                                reason = "skipped_after_unavailable"
                            elif time.monotonic() - t0 > AUTOMATIC_CHECK_LATEST_START_S:
                                reason = "time_budget"
                            else:
                                result = await run_automatic_artifact_check(
                                    runtime_ctx, artifact_id=hint["artifact_id"], version=hint["version"],
                                    code=payload["output"].get("code") or "")
                        finally:
                            run.lock.release()
                if result is None:
                    result = {**_new_result(hint["artifact_id"], hint["version"]), "reason": reason}
                result["duration_ms"] = int((time.monotonic() - started) * 1000)
                _attach(payload, result)
                logger.info("automatic_artifact_check %s status=%s reason=%s smoke=%s duration_ms=%d",
                            _log_ids(runtime_ctx, result), result["status"], result["reason"],
                            (result["conversation_smoke"] or {}).get("status"), result["duration_ms"])
                if result["status"] == "unavailable" and result["reason"] in _BREAKER_REASONS:
                    run.unavailable_reason = result["reason"]
                yield event
    return wrapped
