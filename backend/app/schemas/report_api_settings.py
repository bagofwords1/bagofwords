"""Per-conversation opt-outs for API integrations, stored on the report.

``follow_ups`` / ``suggest_instructions`` (bool) and ``tools`` ({key: bool}).
One meaning everywhere: missing or null = the org setting decides, false =
off, true = on only if the org allows it. A report can never turn on what the
org turned off. Every turn on the report honors them, including the turns the
agent continues on its own, because they all run on the same report.

``tools`` keys are a short public list, not internal tool names, so renaming
an internal tool never breaks a caller. Group keys switch off several tools.
"""
from typing import Dict, Optional, Tuple

# Public key -> internal tools it switches off.
TOOL_SWITCHES: Dict[str, Tuple[str, ...]] = {
    "clarify": ("clarify",),
    "create_artifact": ("create_artifact",),
    "edit_artifact": ("edit_artifact",),
    "create_doc": ("create_doc",),
    "edit_doc": ("edit_doc",),
    # Also turns off the sub-agents gate, which hides read/stop and the
    # delegation prompt rules (same as the org setting being off).
    "delegate": ("delegate",),
    "notify": ("notify",),
    "send_email": ("send_email",),
    # Stops new work only: existing scheduled tasks / waits can still be
    # edited or cancelled.
    "create_scheduled_task": ("create_scheduled_task",),
    "wait": ("wait",),
    # Also removes the sandbox `http` client from generated code.
    "web_fetch": ("web_fetch",),
    # Provider-native search: not a registered tool; AgentV2._web_search_enabled
    # checks this name.
    "web_search": ("web_search",),
    "generate_image": ("generate_image",),
    "inspect_data": ("inspect_data",),
    # Also drops the native mcp__ tools and the <mcp_tools> schema block.
    "mcp": ("execute_mcp", "search_mcps", "list_mcp_resources", "read_mcp_resource"),
    # Also drops the native submit_<list> tools.
    "lists": ("submit_list",),
}

# Internal names that are not registered tools (see "web_search" above).
NON_REGISTRY_TOOL_NAMES = frozenset({"web_search"})


def validate_tools(value: Optional[dict]) -> Optional[dict]:
    """Keys must be public switch keys; values bool or null. Null entries mean
    "not set" and are dropped, so they never shadow the org setting."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("tools must be an object of {tool: false}")
    unknown = [k for k in value if k not in TOOL_SWITCHES]
    if unknown:
        raise ValueError(
            f"unknown tool key(s): {', '.join(map(str, unknown))}. Valid keys: {', '.join(TOOL_SWITCHES)}"
        )
    bad = [k for k, v in value.items() if v is not None and not isinstance(v, bool)]
    if bad:
        raise ValueError(f"tools values must be true, false or null: {', '.join(bad)}")
    cleaned = {k: v for k, v in value.items() if v is not None}
    return cleaned or None


def disabled_tool_names(tools: Optional[dict]) -> frozenset:
    """Internal tool names a report's ``tools`` setting switches off."""
    if not isinstance(tools, dict):
        return frozenset()
    names = set()
    for key, value in tools.items():
        if value is False:
            names.update(TOOL_SWITCHES.get(key, ()))
    return frozenset(names)


def report_disabled_tools(report) -> frozenset:
    return disabled_tool_names(getattr(report, "tools", None))


def mcp_tools_hidden(report) -> bool:
    """The report switched off "mcp": schemas must not advertise MCP tools."""
    return "execute_mcp" in report_disabled_tools(report)
