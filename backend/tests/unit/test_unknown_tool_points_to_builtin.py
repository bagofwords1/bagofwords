"""An mcp__-prefixed name whose last part is a built-in tool of the run.

A connector's write actions are named mcp__<connection>__<action> while its
read tools are not; models generalise the prefix (seen live:
mcp__Outlook__search_email). The observation must name the real tool so the
next step can call it, and an ordinary unknown MCP name keeps listing the
run's MCP tools.
"""
from types import SimpleNamespace as N

from app.ai.agent_v2 import AgentV2


def _agent(catalog, native):
    a = AgentV2.__new__(AgentV2)
    a.planner = N(tool_catalog=[N(name=n) for n in catalog])
    a._native_mcp_names = set(native)
    a._deferred_mcp_tools = []
    return a


def test_a_prefixed_builtin_name_suggests_the_builtin_tool():
    a = _agent(["search_email", "list_emails", "mcp__Outlook__flag_email"], ["mcp__Outlook__flag_email"])
    obs = a._unknown_tool_observation("mcp__Outlook__search_email")
    assert obs["suggested_tool"] == "search_email"
    assert "search_email" in obs["error"]["message"]


def test_an_unknown_mcp_name_still_lists_the_runs_mcp_tools():
    a = _agent(["search_email", "mcp__Outlook__flag_email"], ["mcp__Outlook__flag_email"])
    obs = a._unknown_tool_observation("mcp__Outlook__delete_everything")
    assert "suggested_tool" not in obs
    assert obs["available_mcp_tools"] == ["mcp__Outlook__flag_email"]
