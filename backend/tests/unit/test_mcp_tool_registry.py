"""Unit tests for native MCP tool naming and schema normalization."""
import pytest

from app.ai.tools.mcp_tool_registry import (
    build_tool_name, normalize_schema, parse_native_tool_name, sanitize,
    native_tools_enabled, native_tools_budget,
)

MAX = 64


def test_sanitize_strips_unsupported_characters():
    assert sanitize("Intercom (Prod)!") == "Intercom__Prod__"
    assert sanitize("search-contacts_v2") == "search-contacts_v2"


def test_basic_name_shape():
    name = build_tool_name("Intercom", "search_contacts", set())
    assert name == "mcp__Intercom__search_contacts"
    assert parse_native_tool_name(name) == name


def test_non_native_names_are_not_claimed():
    assert parse_native_tool_name("create_data") is None
    assert parse_native_tool_name(None) is None


def test_same_tool_name_on_two_connections_does_not_collide():
    taken = set()
    a = build_tool_name("Intercom", "search", taken)
    b = build_tool_name("Notion", "search", taken)
    assert a != b
    assert a.lower() in taken and b.lower() in taken


def test_names_are_capped_at_provider_limit():
    taken = set()
    name = build_tool_name("a-very-long-connection-name-indeed", "b" * 120, taken)
    assert len(name) <= MAX
    assert name.startswith("mcp__")


def test_long_names_from_different_connections_stay_distinct():
    """Truncation must not merge two tools into one name — the connection half
    is preserved and a stable hash disambiguates the trimmed tool half."""
    taken = set()
    a = build_tool_name("connection-alpha-long", "z" * 120, taken)
    b = build_tool_name("connection-bravo-long", "z" * 120, taken)
    assert a != b
    assert len(a) <= MAX and len(b) <= MAX


def test_name_is_stable_across_runs():
    """Prompt caching depends on tool names not moving between runs."""
    first = build_tool_name("Intercom", "x" * 120, set())
    second = build_tool_name("Intercom", "x" * 120, set())
    assert first == second


def test_duplicate_registration_gets_distinct_names():
    taken = set()
    a = build_tool_name("Intercom", "search", taken)
    b = build_tool_name("Intercom", "search", taken)
    assert a != b


# ── schema normalization ───────────────────────────────────────────────────

def test_empty_schema_becomes_valid_object():
    assert normalize_schema(None) == {"type": "object", "properties": {}}
    assert normalize_schema({}) == {"type": "object", "properties": {}}


def test_missing_type_and_properties_are_filled():
    out = normalize_schema({"required": ["a"]})
    assert out["type"] == "object"
    assert out["properties"] == {}


def test_draft_marker_is_stripped():
    out = normalize_schema({
        "$schema": "http://json-schema.org/draft-07/schema#",
        "type": "object",
        "properties": {"q": {"type": "string"}},
    })
    assert "$schema" not in out
    assert out["properties"]["q"]["type"] == "string"


def test_refs_are_inlined():
    out = normalize_schema({
        "type": "object",
        "properties": {"estimate": {"$ref": "#/$defs/Estimate"}},
        "$defs": {"Estimate": {
            "type": "object",
            "properties": {"value": {"type": "number"}},
            "required": ["value"],
        }},
    })
    est = out["properties"]["estimate"]
    assert est["type"] == "object"
    assert est["properties"]["value"]["type"] == "number"
    assert "$ref" not in str(out)


def test_malformed_required_is_dropped():
    out = normalize_schema({"type": "object", "properties": {}, "required": "a"})
    assert "required" not in out


def test_intercom_shaped_schema_survives_normalization():
    """The real search_contacts schema: no `required` at all. Normalization must
    not invent one — the whole point is that the server under-declares, and
    fabricating requiredness here would make valid calls fail locally."""
    out = normalize_schema({
        "type": "object",
        "properties": {"per_page": {"type": "number"}, "email": {"type": "string"}},
        "additionalProperties": False,
        "$schema": "http://json-schema.org/draft-07/schema#",
    })
    assert "required" not in out
    assert out["additionalProperties"] is False


# ── flag plumbing ──────────────────────────────────────────────────────────

def test_flag_defaults_off(monkeypatch):
    monkeypatch.delenv("BOW_MCP_NATIVE_TOOLS", raising=False)
    assert native_tools_enabled() is False


@pytest.mark.parametrize("val", ["1", "true", "TRUE", "yes", "on"])
def test_flag_accepts_truthy_values(monkeypatch, val):
    monkeypatch.setenv("BOW_MCP_NATIVE_TOOLS", val)
    assert native_tools_enabled() is True


def test_budget_defaults_and_overrides(monkeypatch):
    monkeypatch.delenv("BOW_MCP_NATIVE_TOOLS_MAX", raising=False)
    assert native_tools_budget() == 60
    monkeypatch.setenv("BOW_MCP_NATIVE_TOOLS_MAX", "12")
    assert native_tools_budget() == 12
    monkeypatch.setenv("BOW_MCP_NATIVE_TOOLS_MAX", "not-a-number")
    assert native_tools_budget() == 60


# ── dispatch rewrite ───────────────────────────────────────────────────────

class _FakeAgent:
    """Minimal stand-in exposing just the rewrite helper."""
    from app.ai.agent_v2 import AgentV2
    _rewrite_native_mcp_action = AgentV2._rewrite_native_mcp_action

    def __init__(self, routing):
        self._native_mcp_routing = routing


ROUTING = {"mcp__conn__issue_create": {"connection_id": "c1", "tool_name": "issue_create"}}


def test_rewrite_maps_to_execute_mcp():
    name, args = _FakeAgent(ROUTING)._rewrite_native_mcp_action(
        "mcp__conn__issue_create", {"teamId": "t", "title": "Fix login", "priority": 1}
    )
    assert name == "execute_mcp"
    assert args["connection_id"] == "c1"
    assert args["tool_name"] == "issue_create"


def test_rewrite_preserves_a_tool_argument_named_title():
    """Regression: `title` is execute_mcp's cosmetic label on the gateway path,
    but on a native call every key belongs to the server — and issue_create
    REQUIRES a `title`. Stripping it made every such call fail validation for a
    missing required field, three retries deep, without reaching the server."""
    _, args = _FakeAgent(ROUTING)._rewrite_native_mcp_action(
        "mcp__conn__issue_create", {"teamId": "t", "title": "Fix login", "priority": 1}
    )
    assert args["arguments"]["title"] == "Fix login"


def test_unknown_native_name_passes_through():
    name, args = _FakeAgent(ROUTING)._rewrite_native_mcp_action("mcp__conn__nope", {"a": 1})
    assert name == "mcp__conn__nope" and args == {"a": 1}


def test_non_native_tool_untouched():
    name, args = _FakeAgent(ROUTING)._rewrite_native_mcp_action("create_data", {"a": 1})
    assert name == "create_data" and args == {"a": 1}


def test_rewrite_noop_when_routing_absent():
    name, args = _FakeAgent({})._rewrite_native_mcp_action("mcp__conn__issue_create", {"a": 1})
    assert name == "mcp__conn__issue_create"
