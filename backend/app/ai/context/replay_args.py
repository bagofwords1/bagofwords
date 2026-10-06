"""Shrink the arguments of past tool calls before they are replayed.

A ``submit_list`` call carries every extracted record — values, evidence
quotes, notes — which can run to tens of KB. Once the call succeeded, all of
that is already in the list (and the result names every row), so replaying it
verbatim on every later step only burns context. The model still sees that the
call happened, which list it targeted and which rows it sent.

The stub keeps the schema's shape — ``records`` stays an array (of the sent
``row_id``s) — and a note says the fields were omitted. A string in place of
the array looked like a malformed call the model had made with placeholder
data, and it re-submitted to "fix" it (BOW-103).

Failed calls keep their full arguments: the agent needs what it sent to diff
against the validation errors and fix it.

Deterministic by construction (same input → same stub), so compacting never
breaks the prompt-cache prefix across steps. Stored ``arguments_json`` is never
touched — the UI renders from it.
"""
from typing import Any

COMPACT_TOOLS = frozenset({"submit_list"})


def compact_replayed_args(tool_name: str, args: Any, *, succeeded: bool) -> Any:
    if tool_name not in COMPACT_TOOLS or not succeeded or not isinstance(args, dict):
        return args
    records = args.get("records")
    if not isinstance(records, list):
        return args
    out = {k: v for k, v in args.items() if k != "records"}
    out["records"] = [
        {"row_id": r["row_id"]} for r in records if isinstance(r, dict) and r.get("row_id") is not None
    ]
    out["_replay_note"] = (
        f"{len(records)} record(s) were sent in this call and saved; their fields are "
        "omitted from this replay to save context. This call already succeeded — see its result."
    )
    return out
