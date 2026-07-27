"""Unit tests for the eval result-view helpers that turn a TestResult's
result_json into the agent-readable detail get_eval_run returns.

The tool integration is exercised in the sandbox loop; here we cover the
pure transforms deterministically.
"""
from app.ai.tools.eval_result_view import (
    derive_failure_reason,
    prompt_text,
    rule_results_view,
    rule_summary,
    rules_view,
)

RJ_FAIL = {
    "spec": {
        "rules": [
            {"type": "tool.calls", "tool": "create_data", "min_calls": 1},
            {"type": "judge", "prompt": "The answer must be a monthly breakdown; reject a total count."},
        ]
    },
    "rule_results": [
        {"ok": True, "status": "pass"},
        {"ok": False, "status": "fail", "message": "Returned a single total, not a monthly breakdown."},
    ],
}


def test_rule_summary_shapes():
    assert rule_summary({"type": "tool.calls", "tool": "create_data", "min_calls": 1}) == "tool.calls: create_data min 1"
    assert rule_summary({"type": "judge", "prompt": "abc"}).startswith("judge: abc")
    assert rule_summary({"type": "phase", "phase": "explore"}) == "phase: explore"


def test_rules_view_lists_all_rules():
    v = rules_view(RJ_FAIL)
    assert [r["type"] for r in v] == ["tool.calls", "judge"]
    assert "create_data" in v[0]["summary"]


def test_rule_results_view_failing_first():
    v = rule_results_view(RJ_FAIL)
    # Failing rule sorts ahead of the passing one.
    assert v[0]["status"] == "fail"
    assert "monthly breakdown" in v[0]["message"]
    assert v[0]["rule"].startswith("judge:")
    assert v[1]["status"] == "pass"


def test_rule_results_view_reads_evidence_reasoning_when_no_message():
    rj = {
        "spec": {"rules": [{"type": "judge", "prompt": "x"}]},
        "rule_results": [{"ok": False, "status": "fail", "evidence": {"type": "judge", "reasoning": "wrong shape"}}],
    }
    v = rule_results_view(rj)
    assert v[0]["message"] == "wrong shape"


def test_derive_failure_reason_prefers_explicit_column():
    assert derive_failure_reason("error", "Stopped by user", RJ_FAIL) == "Stopped by user"


def test_derive_failure_reason_synthesizes_from_rules():
    reason = derive_failure_reason("fail", None, RJ_FAIL)
    assert reason == "Returned a single total, not a monthly breakdown."


def test_derive_failure_reason_none_for_pass():
    assert derive_failure_reason("pass", None, RJ_FAIL) is None


def test_derive_failure_reason_none_when_no_messages():
    rj = {"spec": {"rules": []}, "rule_results": []}
    assert derive_failure_reason("fail", None, rj) is None


def test_prompt_text_truncates():
    assert prompt_text({"content": "hi"}) == "hi"
    long = "x" * 600
    out = prompt_text({"content": long})
    assert out is not None and len(out) <= 400 and out.endswith("…")
    assert prompt_text(None) is None
