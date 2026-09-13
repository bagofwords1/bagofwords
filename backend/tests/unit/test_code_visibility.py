"""Unit coverage for generated-code redaction.

The behavior under test is "a caller without ``view_code`` receives no
generated code, on any surface that carries it". These tests are organised by
SURFACE rather than by permission, because the failure mode this feature has is
a forgotten serializer — a test that loops over permissions would pass while a
whole surface leaks.
"""
import datetime

import pytest

from app.core.code_visibility import (
    CODE_PRODUCING_TOOLS,
    code_visibility,
    code_visible_now,
    redact_progress_payload,
    redact_tool_arguments,
    redact_tool_result,
    redact_tool_timings,
)
from app.schemas.step_schema import PublicStepSchema, StepSchema


def _step(code="SELECT order_id FROM orders"):
    return StepSchema(
        id="s1", title="t", slug="s", status="success", prompt="p", code=code,
        created_at=datetime.datetime(2025, 1, 1), type="table", query_id="q1",
    )


# ── Surface 1: step payloads ─────────────────────────────────────────────

def test_step_code_is_withheld_without_permission():
    assert _step().model_dump()["code"] is None


def test_step_code_is_served_with_permission():
    with code_visibility(True):
        assert _step().model_dump()["code"] == "SELECT order_id FROM orders"


def test_redaction_uses_none_not_empty_string():
    """`None` and `""` are not interchangeable: an empty string is what a step
    with no code looks like, so using it would make "withheld" indistinguishable
    from "there was none" — for the UI and for these tests alike."""
    dumped = _step().model_dump()
    assert dumped["code"] is None
    assert dumped["code"] != ""


def test_redaction_does_not_mutate_the_underlying_object():
    """Execution, exports and the agent itself read `.code` directly. Redaction
    is display-only; corrupting the attribute would break running the query."""
    step = _step()
    step.model_dump()
    assert step.code == "SELECT order_id FROM orders"


def test_public_step_schema_is_gated_too():
    pub = PublicStepSchema(id="s1", title="t", type="table", code="SELECT 1")
    assert pub.model_dump()["code"] is None
    with code_visibility(True):
        assert pub.model_dump()["code"] == "SELECT 1"


def test_default_is_deny_when_nothing_sets_the_flag():
    """An unset flag must deny. If a surface forgets to resolve the permission,
    the resulting bug should be an authorized user missing code (loud) rather
    than an unauthorized user seeing it (silent)."""
    assert code_visible_now() is False


def test_visibility_does_not_leak_out_of_its_scope():
    with code_visibility(True):
        assert code_visible_now() is True
    assert code_visible_now() is False


# ── Surface 2: tool results ──────────────────────────────────────────────

@pytest.mark.parametrize("key", ["code", "errors", "executed_queries"])
def test_every_code_bearing_result_key_is_withheld(key):
    result = {"code": "SELECT 1", "errors": [["SELECT 1", "boom"]],
              "executed_queries": ["SELECT 1"]}
    assert redact_tool_result(result, False, "create_data")[key] is None


def test_non_code_result_fields_survive_redaction():
    """Redaction must cost the caller only the code. Row data, stats and the
    execution log are what a view-only user is still entitled to see."""
    result = {"code": "SELECT 1", "data": {"rows": [{"a": 1}]},
              "stats": {"n": 1}, "execution_log": "ok", "success": True}
    out = redact_tool_result(result, False, "create_data")
    assert out["data"] == {"rows": [{"a": 1}]}
    assert out["stats"] == {"n": 1}
    assert out["execution_log"] == "ok"
    assert out["success"] is True


def test_error_identifier_named_code_is_not_clobbered():
    """Tool ERROR payloads use `code` as an error identifier ("FORBIDDEN"), not
    as source. A blanket sweep for keys named "code" would corrupt every error
    the UI renders, so redaction is scoped to code-producing tools."""
    payload = {"error": "not allowed", "code": "FORBIDDEN"}
    assert redact_tool_result(payload, False, "notify")["code"] == "FORBIDDEN"


def test_artifact_source_is_not_redacted():
    """Artifact tools key their payload on `code` too, but there the value IS
    the component the iframe renders. Redacting it would break artifacts for
    exactly the users this feature is supposed to keep working."""
    out = redact_tool_result({"code": "<div/>"}, False, "read_artifact")
    assert out["code"] == "<div/>"


def test_unknown_tool_is_redacted_conservatively():
    """An unrecognised tool gets the safe treatment, so a newly added
    code-producing tool leaks nothing before anyone remembers this list."""
    assert redact_tool_result({"code": "SELECT 1"}, False, None)["code"] is None


def test_nested_sub_results_are_redacted():
    nested = {"results": [{"code": "SELECT 1"}, {"code": "SELECT 2"}]}
    out = redact_tool_result(nested, False, "create_data")
    assert [r["code"] for r in out["results"]] == [None, None]


def test_failed_sql_inside_an_error_object_is_withheld():
    out = redact_tool_result(
        {"error": {"message": "syntax error", "failed_sql": "SELEC 1"}},
        False, "create_data",
    )
    assert out["error"]["failed_sql"] is None
    assert out["error"]["message"] == "syntax error"


def test_permitted_caller_gets_the_payload_untouched():
    result = {"code": "SELECT 1", "executed_queries": ["SELECT 1"]}
    assert redact_tool_result(result, True, "create_data") == result


# ── Surface 3: tool call arguments ───────────────────────────────────────

def test_code_in_tool_arguments_is_withheld():
    """The tool CALL carries the code as well; the UI reads it as a fallback."""
    out = redact_tool_arguments({"code": "SELECT 1", "title": "x"}, False, "write_csv")
    assert out["code"] is None
    assert out["title"] == "x"


# ── Surface 4: per-query timing telemetry ────────────────────────────────
#
# Found by diffing an admin's API payload against a restricted viewer's against
# a running stack. `code` was already redacted; the executed statement was still
# readable one field over, in the timing record. Nothing in the allowlist-based
# unit tests could have caught it, because the leak was in a key nobody had
# thought to list.


def _timings():
    return {"queries": [
        {"index": 0, "query_ms": 19.2, "rows": 5, "result_bytes": 226,
         "sql": "SELECT a.Name FROM Artist a LEFT JOIN Album al"},
    ]}


def test_executed_statement_in_result_query_timings_is_withheld():
    """The exact leak: `code` was redacted while `result_json.query_timings[].sql`
    still carried the statement. Two containers, same shape — both must be
    covered, and a test for only one of them is how this survived the first
    implementation."""
    result = {"code": "SELECT 1", "query_timings": [
        {"index": 0, "query_ms": 19.2, "rows": 5,
         "sql": "SELECT a.Name FROM Artist a"},
    ]}
    out = redact_tool_result(result, False, "create_data")
    assert out["query_timings"][0]["sql"] is None
    assert out["query_timings"][0]["query_ms"] == 19.2


def test_no_code_string_survives_anywhere_in_a_redacted_result():
    """Whole-payload assertion, not a per-key one: it fails for a code-bearing
    key nobody has thought of yet, which is exactly how the timings leak got
    through a suite that only checked the keys on the list."""
    import json
    result = {
        "code": "SELECT secret FROM t",
        "errors": [["SELECT secret FROM t", "boom"]],
        "executed_queries": ["SELECT secret FROM t"],
        "query_timings": [{"query_ms": 1, "sql": "SELECT secret FROM t"}],
        "error": {"failed_sql": "SELECT secret FROM t", "message": "bad"},
        "data": {"rows": [{"a": 1}]},
    }
    out = json.dumps(redact_tool_result(result, False, "create_data"))
    assert "secret" not in out, f"code leaked through: {out}"


def test_executed_statement_in_timings_is_withheld():
    out = redact_tool_timings(_timings(), False, "create_data")
    assert out["queries"][0]["sql"] is None


def test_timing_measurements_survive_redaction():
    """The numbers are not code. A viewer who may not read the query is still
    entitled to know it ran and how long it took."""
    out = redact_tool_timings(_timings(), False, "create_data")
    q = out["queries"][0]
    assert q["query_ms"] == 19.2
    assert q["rows"] == 5
    assert q["result_bytes"] == 226


def test_timings_are_served_intact_with_permission():
    assert redact_tool_timings(_timings(), True, "create_data") == _timings()


def test_timings_without_queries_are_left_alone():
    assert redact_tool_timings({"total_ms": 5}, False, "create_data") == {"total_ms": 5}


# ── Surface 5: the live agent stream ─────────────────────────────────────

@pytest.mark.parametrize("stage", ["generating_code", "generated_code", "executing_code"])
def test_streamed_code_stages_are_withheld(stage):
    """The stream reaches the client before any REST serializer runs — a
    UI-only approach misses it entirely."""
    out = redact_progress_payload({"stage": stage, "code": "SELECT 1"}, False)
    assert out["code"] is None


def test_non_code_stages_stream_normally():
    payload = {"stage": "reading_artifact", "detail": "x"}
    assert redact_progress_payload(payload, False) == payload


def test_stream_error_payload_keeps_its_error_code():
    payload = {"error": "nope", "code": "FORBIDDEN"}
    assert redact_progress_payload(payload, False) == payload


# ── The tool allowlist itself ────────────────────────────────────────────

def test_analysis_tools_are_covered_and_artifact_tools_are_not():
    assert {"create_data", "inspect_data"} <= CODE_PRODUCING_TOOLS
    assert not CODE_PRODUCING_TOOLS & {"create_artifact", "read_artifact", "edit_artifact"}
