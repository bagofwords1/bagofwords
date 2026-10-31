"""Profile-granted data tools must survive the registry's mode filter."""

import pytest

from app.ai.registry import ToolRegistry
from app.ai.sub_agent_profiles import PROFILES, allowed_tools


@pytest.mark.parametrize("profile", PROFILES.values(), ids=PROFILES.keys())
@pytest.mark.parametrize("allow_create_data", [False, True])
def test_subagent_catalog_preserves_profile_data_access(profile, allow_create_data):
    registry = ToolRegistry()
    granted = allowed_tools(profile, allow_create_data=allow_create_data)
    catalog = {
        tool["name"]
        for plan_type in ("action", "research")
        for tool in registry.get_catalog_for_plan_type(plan_type, mode="subagent")
    }
    effective = catalog & granted

    assert ("create_data" in effective) == ("create_data" in granted)
    assert "create_artifact" not in effective
    assert "delegate" not in effective
