"""Shrink the arguments of past tool calls before they are replayed.

A ``submit_list`` call carries every extracted record — values, evidence
quotes, notes — which can run to tens of KB. Almost all of that weight is the
quotes and notes. Once the call succeeded the full text is stored with the
rows, so the replay keeps every record with its row id, values and statuses
and only shortens long quotes and notes.

The model reads a replayed call as exactly what it sent, so the replay must
stay a truthful, schema-valid call:

* BOW-103: a string in place of the ``records`` array read as a malformed
  call, and the model re-submitted to "fix" it.
* Replaying only the sent row ids turned every save of new rows (row_id null)
  into ``{"records": []}`` next to "3 inserted". Live, the model either copied
  it (an empty save, then a 30-minute loop on "records: [] should be
  non-empty") or decided its own save was malformed and stopped to ask.
* One ``{"row_id": ...}`` stub per record read as "my save carried no field
  data": the model re-saved and stopped. An extra ``_replay_note`` key, which
  the schema forbids, also read as a malformed call.

Failed calls keep their full arguments: the agent needs what it sent to diff
against the validation errors and fix it.

Deterministic by construction (same input → same output), so compacting never
breaks the prompt-cache prefix across steps. Stored ``arguments_json`` is never
touched — the UI renders from it.
"""
from typing import Any

COMPACT_TOOLS = frozenset({"submit_list"})
# Long enough to recognise the passage, short enough that quotes stop
# dominating the replay.
MAX_REPLAYED_TEXT = 60


def _short(text: Any) -> Any:
    if isinstance(text, str) and len(text) > MAX_REPLAYED_TEXT:
        return text[: MAX_REPLAYED_TEXT - 1].rstrip() + "…"
    return text


def _compact_envelope(env: Any) -> Any:
    if not isinstance(env, dict):
        return env
    out = dict(env)
    if "note" in out:
        out["note"] = _short(out["note"])
    if isinstance(out.get("evidence"), list):
        out["evidence"] = [
            {**e, "quote": _short(e.get("quote"))} if isinstance(e, dict) else e for e in out["evidence"]
        ]
    return out


def _compact_record(rec: Any) -> Any:
    if not isinstance(rec, dict) or not isinstance(rec.get("fields"), dict):
        return rec
    return {**rec, "fields": {k: _compact_envelope(v) for k, v in rec["fields"].items()}}


def compact_replayed_args(tool_name: str, args: Any, *, succeeded: bool) -> Any:
    if tool_name not in COMPACT_TOOLS or not succeeded or not isinstance(args, dict):
        return args
    records = args.get("records")
    if not isinstance(records, list):
        return args
    return {**args, "records": [_compact_record(r) for r in records]}
