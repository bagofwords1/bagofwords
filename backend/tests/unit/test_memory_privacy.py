"""Memory content never lands in persisted trace data readable by others.

Contract: context snapshots (shown in the trace to admins) and report/trace
tool payloads keep only a memory tool's status — never entry text, aliases,
search results or refusal fragments — while every other tool is untouched.
"""
import copy

import pytest

from app.serializers.completion_v2 import redact_memory_tool_payload
from app.services.memory_privacy import scrub_context_snapshot

SECRET = "Prefers amounts in €M with one decimal"


def _snapshot(tool_name, tool_input, observation):
    return {
        "static": {"schemas": None},
        "warm": {"observations": {"execution_count": 2, "tool_observations": [
            {"execution_number": 1, "tool_name": "create_data", "tool_input": {"title": "Revenue"},
             "timestamp": "t", "observation": {"summary": "5 rows", "rows": [[1]]}},
            {"execution_number": 2, "tool_name": tool_name, "tool_input": tool_input,
             "timestamp": "t", "observation": observation},
        ]}},
    }


@pytest.mark.parametrize("tool_name,tool_input,observation", [
    ("create_memory", {"text": SECRET, "section": "style", "tags": ["currency"], "title": "Noting your format"},
     {"summary": f"Saved to memory as [m3] (style).", "handle": "m3", "section": "style", "text": SECRET}),
    ("edit_memory", {"handle": "m3", "action": "update", "text": SECRET},
     {"summary": "Updated [m3] → now [m4].", "handle": "m4", "previous_handle": "m3"}),
    ("search_memory", {"query": "currency"},
     {"summary": "Found 1 memory entry.", "entries": [{"handle": "m4", "text": SECRET}], "excluded_injected": 2}),
    ("create_memory", {"text": SECRET, "section": "vocabulary", "tags": ["x"]},
     {"summary": f"Memory not saved: This reads like a rule (matched: \"{SECRET}\")",
      "error": {"type": "memory.looks_like_rule", "message": SECRET}}),
])
def test_snapshot_scrub_removes_memory_content_but_keeps_other_tools(tool_name, tool_input, observation):
    data = _snapshot(tool_name, tool_input, observation)
    original = copy.deepcopy(data)
    out = scrub_context_snapshot(data)
    assert SECRET not in str(out)
    obs = out["warm"]["observations"]["tool_observations"]
    assert obs[0] == original["warm"]["observations"]["tool_observations"][0]  # other tools untouched
    assert obs[1]["tool_name"] == tool_name and "summary" in obs[1]["observation"]
    assert data == original  # input not mutated


def test_snapshot_scrub_tolerates_other_shapes():
    for data in (None, {}, {"warm": None}, {"warm": {"observations": None}}):
        assert scrub_context_snapshot(data) == data


def test_ui_payload_keeps_only_title_and_success():
    data = {"tool_name": "create_memory", "arguments_json": {"text": SECRET, "title": "Noting your format"},
            "result_json": {"success": True, "handle": "m3"}}
    out = redact_memory_tool_payload(dict(data))
    assert out["arguments_json"] == {"title": "Noting your format"}
    assert out["result_json"] == {"success": True}
