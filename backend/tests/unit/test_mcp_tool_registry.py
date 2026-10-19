"""Unit tests for native MCP tool naming and schema normalization."""
import pytest

from app.ai.tools.mcp_tool_registry import (
    build_tool_name, normalize_schema, parse_native_tool_name, sanitize,
    native_tools_enabled, tools_max,
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


# ── always on, with a deployment escape hatch ───────────────────────────────

class _Setting:
    def __init__(self, value):
        self.value = value


class _FakeSettings:
    """Stand-in for OrganizationSettings.get_config."""

    def __init__(self, **values):
        self._values = values

    def get_config(self, key, default=None):
        if key not in self._values:
            return default
        return _Setting(self._values[key])


@pytest.fixture(autouse=True)
def _no_env_flag(monkeypatch):
    monkeypatch.delenv("BOW_TOOLS_MAX", raising=False)


@pytest.mark.parametrize("settings", [
    None,
    _FakeSettings(),
    # Orgs store their whole config, so old saved values must not turn it off.
    _FakeSettings(enable_mcp_native_tools=False, mcp_native_tools_threshold=12),
])
@pytest.mark.parametrize("count", [None, 0, 1, 5, 500])
def test_native_registration_is_on_for_any_org_and_any_catalog_size(settings, count):
    assert native_tools_enabled(settings, count) is True


def test_the_old_deployment_switch_no_longer_turns_tools_off(monkeypatch):
    monkeypatch.setenv("BOW_MCP_NATIVE_TOOLS", "0")
    assert native_tools_enabled(_FakeSettings(), 100) is True


def test_tools_max_defaults_and_env_override(monkeypatch):
    assert tools_max() == 50
    monkeypatch.setenv("BOW_TOOLS_MAX", "12")
    assert tools_max() == 12
    monkeypatch.setenv("BOW_TOOLS_MAX", "not-a-number")
    assert tools_max() == 50


def test_garbage_setting_falls_back_to_the_default():
    assert tools_max(_FakeSettings(tools_max="lots")) == 50


def _real_settings(stored):
    import app.models  # noqa: F401  (register every mapper before instantiating)
    from app.models.organization_settings import OrganizationSettings
    return OrganizationSettings(organization_id="org", config=stored)


def _feature(value):
    return {"name": "n", "description": "d", "value": value}


@pytest.mark.parametrize("stored, expected", [
    ({}, 50),
    ({"ai_features": {"tools_max": _feature(25)}}, 25),
    # A cap saved under the old name is kept until the org saves tools_max.
    ({"ai_features": {"mcp_native_tools_max": _feature(30)}}, 30),
    ({"ai_features": {"mcp_native_tools_max": _feature(30), "tools_max": _feature(8)}}, 8),
    # Saved before the on/off switch was removed, with it off.
    ({"ai_features": {"enable_mcp_native_tools": _feature(False), "mcp_native_tools_threshold": _feature(12)}}, 50),
])
def test_real_org_settings(stored, expected):
    s = _real_settings(stored)
    assert tools_max(s) == expected
    assert native_tools_enabled(s) is True


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


DEFERRED = {"mcp__conn__report_2012": {"connection_id": "c1", "tool_name": "report_2012"}}


def test_call_tool_for_a_found_tool_maps_to_execute_mcp():
    name, args = _FakeAgent(DEFERRED)._rewrite_native_mcp_action(
        "call_tool", {"name": "mcp__conn__report_2012", "arguments": {"year": 2012}, "title": "Pulling 2012"},
    )
    assert name == "execute_mcp"
    assert args == {"connection_id": "c1", "tool_name": "report_2012",
                    "arguments": {"year": 2012}, "title": "Pulling 2012"}


@pytest.mark.parametrize("call", [
    {"name": "mcp__conn__not_found", "arguments": {}},
    {"name": "create_data", "arguments": {}},
    {"arguments": {}},
])
def test_call_tool_for_an_unknown_name_stays_call_tool(call):
    name, args = _FakeAgent(DEFERRED)._rewrite_native_mcp_action("call_tool", call)
    assert name == "call_tool" and args == call


def test_call_tool_accepts_the_short_name_when_it_is_unambiguous():
    name, args = _FakeAgent(DEFERRED)._rewrite_native_mcp_action(
        "call_tool", {"name": "report_2012", "arguments": {"year": 2012}},
    )
    assert name == "execute_mcp" and args["tool_name"] == "report_2012"


def test_call_tool_refuses_a_short_name_shared_by_two_connections():
    routing = {
        "mcp__a__echo": {"connection_id": "a", "tool_name": "echo"},
        "mcp__b__echo": {"connection_id": "b", "tool_name": "echo"},
    }
    name, _ = _FakeAgent(routing)._rewrite_native_mcp_action("call_tool", {"name": "echo", "arguments": {}})
    assert name == "call_tool"
