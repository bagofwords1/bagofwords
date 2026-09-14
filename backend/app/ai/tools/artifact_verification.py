"""Shared, advisory verification policy. No browser or secondary LLM loop."""
from __future__ import annotations

import os
import re


def artifact_verification_enabled() -> bool:
    return os.getenv("BOW_ARTIFACT_VERIFICATION_ENABLED", "true").lower() not in {"0", "false", "off"}


def _interaction_signature(code: str) -> list[str]:
    # Advisory: identify state and handlers, not text, CSS, chart configuration,
    # or the mere number of datasets. This is not a JavaScript correctness gate.
    hooks = re.findall(r"(?:useState|useReducer|useParams|setParam|setParams)\s*\([^\n;]{0,240}?\)", code)
    handlers = re.findall(r"(?:onClick|onChange|onSubmit|onKeyDown)\s*=\s*\{[^\n]*?\}(?=\s|>)", code)
    return hooks + handlers


def _functional_code(code: str) -> str:
    # Conservative advisory diff: ignore obvious presentation-only changes,
    # but retain handler bodies, query bindings, transforms and parameter values.
    code = re.sub(r"/\*.*?\*/|(?m:^\s*//[^\n]*)", "", code, flags=re.S)
    code = re.sub(r"""\bclassName\s*=\s*(?:"[^"\n]*"|'[^'\n]*')""", "", code)
    code = re.sub(r"\bstyle\s*=\s*\{\{[^{}]*\}\}", "", code)
    code = re.sub(r">[^<>{}]*<", "><", code)
    return re.sub(r"\s+", "", code)


def build_artifact_verification_hint(*, artifact_id: str, version: int,
                                     mode: str = "page", code: str = "",
                                     parameters: list[dict] | None = None,
                                     previous_code: str | None = None,
                                     available: bool = True) -> dict:
    adjustable = sorted({p.get("name") for p in parameters or []
                         if p.get("name") and p.get("source") != "identity"})
    signature = _interaction_signature(code)
    changed = previous_code is None or _functional_code(code) != _functional_code(previous_code)
    reasons = []
    if mode == "page" and changed:
        if adjustable:
            reasons.append("backend_parameters")
        if signature:
            reasons.append("interaction_changed" if previous_code is not None else "interactive_app")
    return {
        "recommended": bool(reasons), "reason_codes": reasons,
        "focus": adjustable or (["primary interaction", "close or reset"] if reasons else []),
        "artifact_id": artifact_id, "version": version,
        "next_tool": "browser_navigate" if reasons else None,
        "next_tool_input": {"artifact_id": artifact_id} if reasons else None,
        "availability": "available" if available and artifact_verification_enabled() else "unavailable",
    }


ARTIFACT_VERIFICATION_POLICY = """
After successful page create/edit, use verification_hint to decide whether an
interactive check is useful. A simple static dashboard must finish normally:
do not call browser tools merely to verify a static dashboard or a copy/CSS edit.
For a recommended complex/parameterized app, call browser_navigate with the exact
returned artifact_id (no browser connector required), then exercise a few relevant
controls using browser_act. Browser results already include fresh snapshots and
automatic query/runtime evidence: do not mechanically call snapshot after each act.
For backend filters use expect_query_update={"params": {"parameter_name": value}}
using declared names. The tool derives all affected query IDs from the authorized
manifest, so a control that sends nothing is detected without retyping UUIDs.
Only provide explicit query_ids when checking a deliberate subset. Use the exact snapshot refs,
including frame prefixes. Read applied parameters and fresh visible results;
HTTP success and data_received prove transport, not business correctness. Read
result_checks: inconclusive checks require investigation or an explicitly unverified
final outcome. Never call a parameter_mismatch, pending result, or unexplained
empty result a passed check.
For date controls, first identify a known record/date from the unfiltered data;
choose a range containing it and confirm it remains present with returned dates
inside the intended range; where available, confirm a known outside record is
excluded. Clear other filters or account for their intersection.
Test both endpoints, reset, and an open bound if supported. Empty output is not
proof of a correct date filter. On zero rows inspect the existing query (read_query)
and its from/to keys, boundary comparisons, source date format and timezone;
repair a broken query via create_data and reconnect with edit_artifact. Calendar
ranges use YYYY-MM-DD from/to; exact timestamps retain their precision/offset.
Do not guess a timezone for naive timestamps or text columns. Inspect datasets
and query evidence: result_truncated means displayed rows are only part of the
result. Do not certify full totals from partial rows; use a backend aggregate
query with the same parameters or clearly label the limitation INSIDE the app;
a chat caveat alone does not fix a misleading KPI. Never disable
an organization's row limit to make a check pass. For reset, compare the initial
and restored visible totals; also open and close the primary detail/modal. Use browser_vision
only when appearance needs inspection. A pending result is not a failure or pass.
Supply friendly titles in the user's language, e.g. 'Verifying music album filters',
'Checking track details', 'Rechecking the genre filter after the fix'. They appear
in one group with a spinner and shimmer. No extra call is needed to update a title.
If an observed bug needs repair, edit_artifact then open the NEW artifact_id and
repeat that flow. At most two functional repair passes; keep checks focused.
Unavailable/restricted verification: explain what was not checked and finish.
Never claim interactions were tested from a static screenshot, or claim a live
write/MCP operation was tested in this read-only preview. Report actual checked
scope and unresolved findings; do not perform redundant create_data queries.
""".strip()
