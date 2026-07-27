"""Unit tests for the MCP failed-then-fixed instruction trigger.

The negative controls matter as much as the positive one here: a trigger that
fires on a rate limit would mint a permanent instruction describing a temporary
outage, and that rule would outlive the outage.
"""
import pytest

from app.ai.agents.suggest_instructions.trigger import InstructionTriggerEvaluator


class _Row(tuple):
    """Mimics a SQLAlchemy row of (arguments_json, success, status, error_message, started_at)."""


def _row(args, success, status, error=None, ts=0):
    return _Row((args, success, status, error, ts))


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeDB:
    def __init__(self, rows):
        self._rows = rows

    async def execute(self, _stmt):
        return _FakeResult(self._rows)


def _evaluator(rows):
    ev = InstructionTriggerEvaluator.__new__(InstructionTriggerEvaluator)
    ev.db = _FakeDB(rows)
    ev.current_execution_id = "exec-1"
    ev.report_id = "report-1"
    return ev


CONN = "conn-intercom"


# ── transient-error classifier ─────────────────────────────────────────────

@pytest.mark.parametrize("msg", [
    "429 Too Many Requests",
    "Rate limit exceeded, try again in 30s",
    "503 Service Unavailable",
    "upstream request timed out",
    "401 Unauthorized",
    "token has expired",
    "connection refused",
    None,
    "",
])
def test_transient_errors_are_excluded(msg):
    assert InstructionTriggerEvaluator._is_transient_error(msg) is True


@pytest.mark.parametrize("msg", [
    "At least one search parameter must be provided",
    "invalid value for 'columnValues'",
    "field 'from' must be a unix timestamp",
    "unknown column id 'status9'",
])
def test_deterministic_errors_are_capturable(msg):
    assert InstructionTriggerEvaluator._is_transient_error(msg) is False


# ── the trigger ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_fires_on_intercom_shaped_failure_then_recovery():
    """The real production case: schema-valid call rejected at runtime, then a
    sibling tool succeeds — all inside one execution, no user turn."""
    rows = [
        _row({"connection_id": CONN, "tool_name": "search_contacts",
              "arguments": {"per_page": 50}},
             False, "error", "At least one search parameter must be provided", 1),
        _row({"connection_id": CONN, "tool_name": "search",
              "arguments": {"query": "object_type:contacts"}},
             True, "success", None, 2),
    ]
    cond = await _evaluator(rows)._check_mcp_failed_then_fixed()
    assert cond.met is True
    assert "search_contacts" in cond.hint
    assert "At least one search parameter" in cond.hint
    assert "search_instructions" in cond.hint       # dedupe is mandated
    assert "GENERAL RULE" in cond.hint              # pushes away from record-level facts


@pytest.mark.asyncio
async def test_fires_when_same_tool_retried_with_fixed_arguments():
    rows = [
        _row({"connection_id": CONN, "tool_name": "board_change_column_values",
              "arguments": {"columnValues": {"status": "Done"}}},
             False, "error", "invalid value for 'columnValues'", 1),
        _row({"connection_id": CONN, "tool_name": "board_change_column_values",
              "arguments": {"columnValues": "{\"status\": \"Done\"}"}},
             True, "success", None, 2),
    ]
    cond = await _evaluator(rows)._check_mcp_failed_then_fixed()
    assert cond.met is True
    assert "same tool with different arguments" in cond.hint


@pytest.mark.asyncio
async def test_does_not_fire_on_rate_limit():
    rows = [
        _row({"connection_id": CONN, "tool_name": "search_contacts", "arguments": {}},
             False, "error", "429 Too Many Requests", 1),
        _row({"connection_id": CONN, "tool_name": "search_contacts", "arguments": {}},
             True, "success", None, 2),
    ]
    cond = await _evaluator(rows)._check_mcp_failed_then_fixed()
    assert cond.met is False


@pytest.mark.asyncio
async def test_does_not_fire_on_expired_token():
    rows = [
        _row({"connection_id": CONN, "tool_name": "get_contact", "arguments": {}},
             False, "error", "401 Unauthorized: token has expired", 1),
        _row({"connection_id": CONN, "tool_name": "get_contact", "arguments": {}},
             True, "success", None, 2),
    ]
    cond = await _evaluator(rows)._check_mcp_failed_then_fixed()
    assert cond.met is False


@pytest.mark.asyncio
async def test_does_not_fire_without_a_recovery():
    rows = [
        _row({"connection_id": CONN, "tool_name": "search_contacts", "arguments": {}},
             False, "error", "At least one search parameter must be provided", 1),
    ]
    cond = await _evaluator(rows)._check_mcp_failed_then_fixed()
    assert cond.met is False


@pytest.mark.asyncio
async def test_does_not_pair_across_different_connections():
    """A failure on Intercom is not explained by a success on monday."""
    rows = [
        _row({"connection_id": CONN, "tool_name": "search_contacts", "arguments": {}},
             False, "error", "At least one search parameter must be provided", 1),
        _row({"connection_id": "conn-monday", "tool_name": "get_board", "arguments": {}},
             True, "success", None, 2),
    ]
    cond = await _evaluator(rows)._check_mcp_failed_then_fixed()
    assert cond.met is False


@pytest.mark.asyncio
async def test_success_before_failure_does_not_count():
    """Ordering matters: the fix must come after the failure."""
    rows = [
        _row({"connection_id": CONN, "tool_name": "search", "arguments": {}},
             True, "success", None, 1),
        _row({"connection_id": CONN, "tool_name": "search_contacts", "arguments": {}},
             False, "error", "At least one search parameter must be provided", 2),
    ]
    cond = await _evaluator(rows)._check_mcp_failed_then_fixed()
    assert cond.met is False
