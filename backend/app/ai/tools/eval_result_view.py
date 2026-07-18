"""Shared view helpers that turn a TestResult's ``result_json`` into the
compact, agent-readable shape the eval-read tools return.

``result_json`` (persisted by ``TestEvaluationService``) has the shape::

    {
      "spec":  {"spec_version": 1, "rules": [ {type, ...}, ... ], "order_mode": ...},
      "totals": {...},
      "rule_results": [ {ok, status, message, actual, evidence}, ... ],  # index-aligned with spec.rules
    }

The tools previously surfaced only ``TestResult.status`` + the (usually null)
``failure_reason`` column, so the agent that woke on a failed run couldn't see
*why* it failed. These helpers expose the rules, the per-rule verdicts (judge
messages, expected/actual), and a derived failure reason.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

_MSG_MAX = 400
_ACTUAL_MAX = 200
_PROMPT_MAX = 400
_MAX_RULE_RESULTS = 10


def _truncate(text: Any, limit: int) -> Optional[str]:
    if text is None:
        return None
    s = str(text)
    return s if len(s) <= limit else s[: limit - 1] + "…"


def rule_summary(rule: Dict[str, Any]) -> Optional[str]:
    """One-line human description of an expectation rule."""
    if not isinstance(rule, dict):
        return None
    t = rule.get("type") or "rule"
    if t == "tool.calls":
        tool = rule.get("tool") or "?"
        parts = [f"tool.calls: {tool}"]
        if rule.get("min_calls") is not None:
            parts.append(f"min {rule['min_calls']}")
        if rule.get("max_calls") is not None:
            parts.append(f"max {rule['max_calls']}")
        return " ".join(parts)
    if t == "judge":
        return "judge: " + (_truncate(rule.get("prompt"), 200) or "")
    if t == "field":
        return f"field: {rule.get('target') or rule.get('field') or ''}".strip()
    if t == "ordering":
        return "ordering"
    if t == "phase":
        return f"phase: {rule.get('phase') or ''}".strip()
    # Fallback: type + any short scalar params
    extras = [f"{k}={v}" for k, v in rule.items() if k != "type" and isinstance(v, (str, int, float, bool))]
    return t + ((": " + ", ".join(extras[:3])) if extras else "")


def _rr_status(rr: Dict[str, Any]) -> str:
    st = rr.get("status")
    if st in ("pass", "fail", "skipped"):
        return st
    return "pass" if rr.get("ok") else "fail"


def rules_view(result_json: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Compact list of the case's expectation rules."""
    spec = ((result_json or {}).get("spec") or {})
    out: List[Dict[str, Any]] = []
    for r in (spec.get("rules") or []):
        if isinstance(r, dict):
            out.append({"type": r.get("type"), "summary": rule_summary(r)})
    return out


def rule_results_view(result_json: Optional[Dict[str, Any]], limit: int = _MAX_RULE_RESULTS) -> List[Dict[str, Any]]:
    """Per-rule verdicts (judge message, expected/actual), failing rules first."""
    rr_list = (result_json or {}).get("rule_results") or []
    spec_rules = ((result_json or {}).get("spec") or {}).get("rules") or []
    items: List[Dict[str, Any]] = []
    for i, rr in enumerate(rr_list):
        if not isinstance(rr, dict):
            continue
        rule = spec_rules[i] if i < len(spec_rules) else {}
        # Judge reasoning may live on evidence.reasoning rather than message.
        message = rr.get("message")
        if not message:
            ev = rr.get("evidence")
            if isinstance(ev, dict):
                message = ev.get("reasoning")
        items.append({
            "rule": rule_summary(rule) if isinstance(rule, dict) else None,
            "status": _rr_status(rr),
            "message": _truncate(message, _MSG_MAX),
            "actual": _truncate(rr.get("actual"), _ACTUAL_MAX),
        })
    # Failing first, then skipped, then passing — the agent cares about failures.
    order = {"fail": 0, "skipped": 1, "pass": 2}
    items.sort(key=lambda x: order.get(x["status"], 3))
    return items[:limit]


def derive_failure_reason(
    status: Optional[str],
    failure_reason_col: Optional[str],
    result_json: Optional[Dict[str, Any]],
) -> Optional[str]:
    """The persisted ``failure_reason`` column when set, else a reason
    synthesized from the failing rules' messages. Normal ``fail`` results
    leave the column null and put everything in ``rule_results``."""
    if failure_reason_col:
        return failure_reason_col
    if status not in ("fail", "error"):
        return None
    msgs: List[str] = []
    for rr in ((result_json or {}).get("rule_results") or []):
        if not isinstance(rr, dict) or _rr_status(rr) != "fail":
            continue
        msg = rr.get("message")
        if not msg:
            ev = rr.get("evidence")
            if isinstance(ev, dict):
                msg = ev.get("reasoning")
        if msg:
            msgs.append(str(msg))
    if msgs:
        return _truncate(" | ".join(msgs), 500)
    return None


def prompt_text(prompt_json: Optional[Dict[str, Any]]) -> Optional[str]:
    """The case's replay prompt content, truncated."""
    if not isinstance(prompt_json, dict):
        return None
    return _truncate(prompt_json.get("content"), _PROMPT_MAX)
