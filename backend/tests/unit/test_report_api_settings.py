"""The public tools switch list (app.schemas.report_api_settings)."""
import pytest

from app.ai.registry import ToolRegistry
from app.schemas.report_api_settings import (
    NON_REGISTRY_TOOL_NAMES, TOOL_SWITCHES, disabled_tool_names, validate_tools,
)


def test_every_switch_maps_to_a_registered_tool():
    # Renaming an internal tool must fail here, in CI, rather than silently
    # turning a caller's switch into a no-op.
    registered = {m.name for m in ToolRegistry().list_tools()}
    for key, names in TOOL_SWITCHES.items():
        missing = [n for n in names if n not in registered and n not in NON_REGISTRY_TOOL_NAMES]
        assert not missing, f"switch {key!r} points at unregistered tools {missing}"


def test_only_false_switches_off():
    assert disabled_tool_names({"clarify": False, "mcp": True}) == {"clarify"}
    assert disabled_tool_names({"delegate": False}) == {"delegate", "read_sub_agent", "stop_sub_agent"}
    assert disabled_tool_names(None) == frozenset()


def test_null_entries_are_dropped_and_unknown_keys_rejected():
    assert validate_tools({"clarify": None}) is None
    assert validate_tools({"clarify": False, "mcp": None}) == {"clarify": False}
    with pytest.raises(ValueError):
        validate_tools({"clarfy": False})
