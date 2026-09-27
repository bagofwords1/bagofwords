"""Keep user-memory content out of anything persisted for other viewers.

Memory is private to its owner. The report timeline and trace payloads are
readable by others (shared reports, org admins), so memory tool calls may only
leave their status there: the tool name, success, the handle — never
entry text, aliases, tags, search results or refusal fragments. The owner
reads full details in their profile and in the owner-only memory section of
the trace.
"""
from __future__ import annotations

from typing import Any, Dict

MEMORY_TOOL_NAMES = frozenset({"create_memory", "edit_memory", "search_memory", "update_user_memory"})

_SAFE_OBS_KEYS = ("handle", "previous_handle", "action", "deduped_into", "excluded_injected")


def scrub_tool_observation(item: Dict[str, Any]) -> Dict[str, Any]:
    """Redacted copy of one ``{tool_name, tool_input, observation}`` record
    for a memory tool; any other tool is returned unchanged."""
    if not isinstance(item, dict) or item.get("tool_name") not in MEMORY_TOOL_NAMES:
        return item
    out = dict(item)
    ti = item.get("tool_input") if isinstance(item.get("tool_input"), dict) else {}
    out["tool_input"] = {"title": ti.get("title")} if ti.get("title") else {}
    obs = item.get("observation") if isinstance(item.get("observation"), dict) else {}
    safe: Dict[str, Any] = {k: obs[k] for k in _SAFE_OBS_KEYS if k in obs}
    err = obs.get("error")
    if err:
        safe["error"] = {"type": err.get("type") if isinstance(err, dict) else "error"}
        safe["summary"] = f"{item.get('tool_name')} failed"
    else:
        safe["summary"] = f"{item.get('tool_name')} ok"
        if isinstance(obs.get("entries"), list):
            safe["count"] = len(obs["entries"])
    out["observation"] = safe
    return out


def scrub_context_snapshot(data: Any) -> Any:
    """Redact memory tool records inside a context-snapshot dict (in place
    safe: returns a new top-level structure for the observations branch)."""
    if not isinstance(data, dict):
        return data
    warm = data.get("warm")
    if not isinstance(warm, dict):
        return data
    obs = warm.get("observations")
    if not isinstance(obs, dict):
        return data
    new_obs = dict(obs)
    for key in ("tool_observations", "observations", "history"):
        seq = obs.get(key)
        if isinstance(seq, list):
            new_obs[key] = [scrub_tool_observation(i) for i in seq]
    last = obs.get("last_observation")
    if isinstance(last, dict) and last.get("tool_name") in MEMORY_TOOL_NAMES:
        new_obs["last_observation"] = scrub_tool_observation(last)
    data = dict(data)
    data["warm"] = dict(warm, observations=new_obs)
    return data
