"""Unit tests for native MCP tool naming and schema normalization."""
import pytest

from app.ai.tools.mcp_tool_registry import (
    build_tool_name, normalize_schema, parse_native_tool_name, sanitize,
    native_tools_enabled, native_tools_budget, native_tools_threshold,
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


# ── org settings + adaptive threshold ──────────────────────────────────────

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
    """These tests are about settings, so keep the deployment override out."""
    monkeypatch.delenv("BOW_MCP_NATIVE_TOOLS", raising=False)
    monkeypatch.delenv("BOW_MCP_NATIVE_TOOLS_MAX", raising=False)


def test_adaptive_threshold_gates_on_effective_tool_count():
    s = _FakeSettings(enable_mcp_native_tools=True, mcp_native_tools_threshold=12)
    # Below the threshold the two paths measure as a wash, so stay on gateway.
    assert native_tools_enabled(s, 11) is False
    # At and above it native registration wins.
    assert native_tools_enabled(s, 12) is True
    assert native_tools_enabled(s, 60) is True


def test_master_switch_off_beats_any_count():
    s = _FakeSettings(enable_mcp_native_tools=False, mcp_native_tools_threshold=0)
    assert native_tools_enabled(s, 500) is False


def test_unknown_count_asks_only_the_master_switch():
    """The cheap pre-check before doing DB work to count."""
    assert native_tools_enabled(_FakeSettings(enable_mcp_native_tools=True)) is True
    assert native_tools_enabled(_FakeSettings(enable_mcp_native_tools=False)) is False


def test_zero_threshold_always_registers_natively():
    s = _FakeSettings(enable_mcp_native_tools=True, mcp_native_tools_threshold=0)
    assert native_tools_enabled(s, 0) is True
    assert native_tools_enabled(s, 1) is True


def test_no_settings_falls_back_to_gateway():
    """No settings object (e.g. the external MCP context path) must not flip on."""
    assert native_tools_enabled(None, 100) is False


def test_env_overrides_settings_in_both_directions(monkeypatch):
    on = _FakeSettings(enable_mcp_native_tools=True, mcp_native_tools_threshold=12)
    off = _FakeSettings(enable_mcp_native_tools=False)
    monkeypatch.setenv("BOW_MCP_NATIVE_TOOLS", "0")
    assert native_tools_enabled(on, 100) is False  # forced off despite settings
    monkeypatch.setenv("BOW_MCP_NATIVE_TOOLS", "1")
    assert native_tools_enabled(off, 0) is True    # forced on, threshold ignored


def test_budget_and_threshold_read_from_settings():
    s = _FakeSettings(mcp_native_tools_max=25, mcp_native_tools_threshold=5)
    assert native_tools_budget(s) == 25
    assert native_tools_threshold(s) == 5


def test_settings_garbage_values_fall_back_to_defaults():
    s = _FakeSettings(mcp_native_tools_max="lots", mcp_native_tools_threshold=None)
    assert native_tools_budget(s) == 60
    assert native_tools_threshold(s) == 12


def test_real_org_settings_defaults_are_adaptive():
    """The shipped defaults, read through the real settings object.

    Native registration ships as a lab feature that is OFF: an org that has
    never touched the setting stays on the generic execute_mcp path whatever
    its catalog size. Switching it on (a full FeatureConfig dict, as the
    settings UI writes) gives the adaptive behavior: the generic path for a
    small catalog, native registration once it reaches the threshold.
    """
    import app.models  # noqa: F401  (register every mapper before instantiating)
    from app.models.organization_settings import OrganizationSettings

    s = OrganizationSettings(organization_id="org", config={})
    assert native_tools_threshold(s) == 12
    assert native_tools_budget(s) == 60
    assert native_tools_enabled(s) is False
    assert native_tools_enabled(s, 12) is False

    on = OrganizationSettings(organization_id="org", config={"ai_features": {
        "enable_mcp_native_tools": {
            "name": "Native MCP tool registration", "description": "d", "value": True,
        }
    }})
    assert native_tools_threshold(on) == 12
    assert native_tools_budget(on) == 60
    assert native_tools_enabled(on, 11) is False
    assert native_tools_enabled(on, 12) is True


def test_admin_can_switch_it_off_from_settings():
    """A toggle written back by the settings UI (a full FeatureConfig dict)."""
    import app.models  # noqa: F401
    from app.models.organization_settings import OrganizationSettings

    s = OrganizationSettings(organization_id="org", config={"ai_features": {
        "enable_mcp_native_tools": {
            "name": "Native MCP tool registration", "description": "d", "value": False,
        }
    }})
    assert native_tools_enabled(s, 60) is False


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
