"""inherited_disable: what a continuing turn (sub-agent, fan-in, wait wake,
steer) carries over from the request's stored prompt.disable."""
from app.ai.registry import builtin_tool_names
from app.schemas.completion_v2_schema import inherited_disable


def test_nothing_to_inherit():
    assert inherited_disable(None) is None
    assert inherited_disable({"content": "x"}) is None
    assert inherited_disable({"content": "x", "disable": None}) is None


def test_flags_and_known_tools_carry_over():
    tool = sorted(builtin_tool_names() - {"finish_subtask"})[0]
    out = inherited_disable({"disable": {"follow_ups": True, "suggest_instructions": False, "tools": [tool]}})
    assert out == {"follow_ups": True, "suggest_instructions": False, "tools": [tool]}


def test_tools_no_longer_registered_are_dropped_not_fatal():
    # A deploy can rename or remove a tool between the request and its
    # continuation; the continuation must still start.
    tool = sorted(builtin_tool_names() - {"finish_subtask"})[0]
    out = inherited_disable({"disable": {"tools": [tool, "tool_removed_in_a_later_release", "finish_subtask", 7]}})
    assert out["tools"] == [tool]
