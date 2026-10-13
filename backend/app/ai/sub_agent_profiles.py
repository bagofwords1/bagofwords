"""Sub-agent profiles — an allow-list of tools, a model preference and a prompt
addendum for a delegated child run (``AgentV2`` mode ``subagent``).

A child never holds a tool the parent lacks: the profile is intersected with
the parent's catalog at spawn time, and the ``delegate`` tool itself is never
in any profile (depth is one). Two profiles ship hardcoded; org-authored
profiles via the skills catalog are a later phase (docs/design/sub-agents.md A6).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, FrozenSet, Optional

# Tools every child loses, whatever the profile: anything that asks the user,
# builds a user-facing deliverable, writes outside the report, or delegates.
BLOCKED_IN_SUB_AGENTS: FrozenSet[str] = frozenset({
    "delegate", "read_sub_agent", "stop_sub_agent",
    "clarify", "wait", "cancel_wait",
    "create_artifact", "edit_artifact", "read_artifact", "create_dashboard", "create_doc", "edit_doc",
    "manage_artifact_resources", "create_widget",
    "create_instruction", "edit_instruction", "create_prompt", "edit_prompt",
    "create_memory", "edit_memory", "search_memory",
    "create_scheduled_task", "edit_scheduled_task", "cancel_scheduled_task",
    "send_email", "notify", "attach_file",
    "create_eval", "edit_eval", "run_eval", "stop_eval_run",
    "create_agent", "set_report_agents", "create_demo_dataset",
    "route_model", "generate_image", "write_officejs_code", "write_to_excel",
    "submit_list",
})

_RESEARCH = (
    "describe_tables", "inspect_data", "read_query", "run_query", "describe_entity",
    "read_resources", "read_file", "grep_files", "search_files", "list_files",
    "read_excel_as_csv", "read_excel_range",
    "list_mcp_resources", "read_mcp_resource", "execute_mcp", "search_mcps",
    "search_instructions", "read_instruction", "search_agents", "read_report", "search_reports",
    "web_fetch", "web_search",
    "create_note", "edit_note",
    "finish_subtask",
)


@dataclass(frozen=True)
class SubAgentProfile:
    key: str
    title: str
    tools: FrozenSet[str]
    # "small" | "default": which org model the child plans on under
    # sub_agent_model=auto.
    model_pref: str
    addendum: str
    # Tools added when the org allows sub-agents to create tracked queries.
    with_create_data: FrozenSet[str] = field(default_factory=frozenset)


PROFILES: Dict[str, SubAgentProfile] = {
    "investigate": SubAgentProfile(
        key="investigate",
        title="Investigate",
        tools=frozenset(_RESEARCH),
        model_pref="small",
        addendum=(
            "PROFILE: investigate. You test one hypothesis or answer one bounded question. "
            "Verify names against the schema before querying; peek with inspect_data; prefer a few "
            "targeted queries over one big one. Keep ONE note: create it once with create_note, then "
            "edit_note as you learn more; never create a second note with the same title. Decide, then finish."
        ),
        with_create_data=frozenset({"create_data", "write_csv"}),
    ),
    "validate": SubAgentProfile(
        key="validate",
        title="Validate",
        tools=frozenset(_RESEARCH) | frozenset({"create_data", "write_csv"}),
        model_pref="default",
        addendum=(
            "PROFILE: validate. You produce tracked evidence: build the query or table that settles the "
            "goal with create_data, check the numbers with inspect_data, and return the step/query/viz "
            "ids as evidence. A validation without a tracked query is incomplete."
        ),
    ),
}


def get_profile(key: Optional[str]) -> SubAgentProfile:
    return PROFILES.get((key or "investigate").strip().lower(), PROFILES["investigate"])


def allowed_tools(profile: SubAgentProfile, *, allow_create_data: bool) -> FrozenSet[str]:
    tools = set(profile.tools)
    if allow_create_data:
        tools |= set(profile.with_create_data)
    return frozenset(t for t in tools if t not in BLOCKED_IN_SUB_AGENTS)
