"""Run every case in backend/tests/evals/suites/*.yaml through the real
agent, across every LLM in LLM_MATRIX, and assert expectations pass.

Gated behind ``@pytest.mark.evals`` and the per-provider env var — see
tests/evals/conftest.py. Skips cleanly when a provider's key is absent,
so you can run with whichever subset you have credentials for.

Per-case failure produces a human-readable rule-by-rule report. Every
case also appends one JSON line to ``BOW_EVAL_REPORT`` (default
``/tmp/bow_eval_report.jsonl``) so you can post-process a multi-LLM matrix:

    Case                                 anthropic/claude-sonnet-4-6  openai/gpt-5.4
    Sanity · Smoke/count_tracks          pass                          pass
    Sanity · Clarify/vague_show_data     fail                          pass
"""

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from tests.evals.conftest import ALL_EVAL_CASES, LLM_MATRIX, _display_for


def _fmt_rule(rule_spec: Dict[str, Any], rule_result: Dict[str, Any]) -> str:
    """One line per rule, like::

        rule 2: tool.calls create_instruction min=1 [phase=knowledge]  →  FAIL
                actual=0
                create_instruction calls=0, expected min=1, max=None [phase=knowledge]
    """
    rule_type = rule_spec.get("type")
    phase = rule_spec.get("phase")
    turn = rule_spec.get("turn")

    is_judge = False
    if rule_type == "tool.calls":
        desc = f"tool.calls {rule_spec.get('tool')} min={rule_spec.get('min_calls', 0)}"
        if rule_spec.get("max_calls") is not None:
            desc += f" max={rule_spec['max_calls']}"
    elif rule_type == "ordering":
        mode = rule_spec.get("mode", "flexible")
        seq = [step.get("tool_or_bind") for step in (rule_spec.get("sequence") or [])]
        desc = f"ordering mode={mode} sequence={seq}"
    elif rule_type == "phase":
        desc = f"phase {rule_spec.get('phase')} occurred={rule_spec.get('occurred', True)}"
    elif rule_type == "judge":
        is_judge = True
        desc = "judge"
    elif rule_type == "field":
        tgt = rule_spec.get("target") or {}
        matcher = rule_spec.get("matcher") or {}
        is_judge = tgt.get("category") == "judge"
        if is_judge:
            desc = "judge (legacy field shape)"
        else:
            desc = (
                f"field {tgt.get('category')}.{tgt.get('field')} "
                f"matcher={matcher.get('type')}"
            )
    else:
        desc = f"{rule_type}"

    scope = []
    if phase:
        scope.append(f"phase={phase}")
    if turn is not None:
        scope.append(f"turn={turn}")
    if scope:
        desc += f" [{', '.join(scope)}]"

    status = (rule_result.get("status") or "").upper()
    actual = rule_result.get("actual")
    msg = rule_result.get("message")

    lines = [f"  • {desc}  →  {status}"]

    if is_judge:
        # Surface the judge's prompt and its verdict/reasoning. New shape
        # keeps the prompt at rule.prompt; legacy shape nested it inside
        # target/matcher.
        prompt_text = rule_spec.get("prompt") or (
            (rule_spec.get("matcher") or {}).get("value")
        )
        if prompt_text:
            lines.append(f"      prompt: {str(prompt_text).strip()}")
        evidence = rule_result.get("evidence") or {}
        reasoning = evidence.get("reasoning") if isinstance(evidence, dict) else None
        if reasoning:
            lines.append(f"      judge:  {str(reasoning).strip()}")
        elif msg:
            lines.append(f"      judge:  {msg}")
    else:
        if actual is not None:
            lines.append(f"      actual={actual!r}")
        if msg:
            lines.append(f"      {msg}")
    return "\n".join(lines)


def _format_result_report(
    result: Dict[str, Any], *, case_label: str, llm_display: str,
) -> str:
    rj = result.get("result_json") or {}
    totals = rj.get("totals") or {}
    rule_specs = ((rj.get("spec") or {}).get("rules") or [])
    rule_results = rj.get("rule_results") or []

    header = [
        "",
        f"eval FAILED  [{llm_display}]  {case_label}",
        f"  status={result.get('status')}  "
        f"passed={totals.get('passed', 0)}  "
        f"failed={totals.get('failed', 0)}  "
        f"skipped={totals.get('skipped', 0)}  "
        f"duration_ms={totals.get('duration_ms')}",
    ]
    meta_bits = []
    for key in ("input_tokens", "output_tokens", "total_iterations"):
        v = totals.get(key)
        if v is not None:
            meta_bits.append(f"{key}={v}")
    if meta_bits:
        header.append(f"  {'  '.join(meta_bits)}")
    if result.get("failure_reason"):
        header.append(f"  failure_reason={result['failure_reason']}")

    rule_lines: List[str] = []
    for i, (spec, rr) in enumerate(zip(rule_specs, rule_results), 1):
        rule_lines.append(f"rule {i}:")
        rule_lines.append(_fmt_rule(spec, rr))
    return "\n".join(header + rule_lines)


def _append_report_line(entry: Dict[str, Any]) -> None:
    path = os.getenv("BOW_EVAL_REPORT") or "/tmp/bow_eval_report.jsonl"
    try:
        with open(path, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass


@pytest.mark.parametrize(
    "llm_model",
    LLM_MATRIX,
    ids=[_display_for(m) for m in LLM_MATRIX] or None,
)
@pytest.mark.parametrize(
    "yaml_path,suite_name,case_name,tags",
    ALL_EVAL_CASES,
    ids=[f"{s}/{c}" for _p, s, c, _t in ALL_EVAL_CASES] or None,
)
def test_eval_case(
    yaml_path, suite_name, case_name, tags, llm_model,
    eval_env, import_suite_yaml, run_case_and_wait,
):
    env = eval_env(llm_model)
    token = env["token"]
    org_id = env["org_id"]
    llm_display = env["llm_display"]

    case_label = f"{suite_name}/{case_name}"
    print(f"\n[eval] case={case_label}  llm={llm_display}", flush=True)

    yaml_text = Path(yaml_path).read_text()
    imported = import_suite_yaml(yaml_text, user_token=token, org_id=org_id)
    assert imported.status_code == 200, imported.json()
    case_id = imported.json()["cases_by_name"][case_name]
    print(f"[eval] imported case_id={case_id[:8]}", flush=True)

    t0 = time.time()
    run_data = run_case_and_wait(
        [case_id], user_token=token, org_id=org_id, timeout_s=300,
    )
    harness_duration_ms = int((time.time() - t0) * 1000)

    results = run_data["results"]
    tool_traces = run_data.get("tool_traces") or {}
    completions_by_result = run_data.get("completions") or {}
    assert len(results) == 1
    result = results[0]
    status = result.get("status")

    rj = result.get("result_json") or {}
    rule_specs = ((rj.get("spec") or {}).get("rules") or [])
    rule_results = rj.get("rule_results") or []
    # Compact per-rule summary; keeps judge reasoning for side-by-side
    # comparison across providers.
    rules_summary = []
    for spec, rr in zip(rule_specs, rule_results):
        entry = {
            "type": spec.get("type"),
            "status": rr.get("status"),
            "actual": rr.get("actual"),
        }
        if spec.get("phase"):
            entry["phase"] = spec["phase"]
        if spec.get("turn") is not None:
            entry["turn"] = spec["turn"]
        if spec.get("type") == "judge":
            entry["judge_prompt"] = spec.get("prompt")
            evidence = rr.get("evidence") or {}
            if isinstance(evidence, dict):
                entry["judge_reasoning"] = evidence.get("reasoning")
        else:
            tgt = spec.get("target") or {}
            if tgt.get("category") == "judge":  # legacy shape
                entry["judge_prompt"] = (spec.get("matcher") or {}).get("value")
                evidence = rr.get("evidence") or {}
                if isinstance(evidence, dict):
                    entry["judge_reasoning"] = evidence.get("reasoning")
            elif spec.get("type") == "tool.calls":
                entry["tool"] = spec.get("tool")
        if rr.get("message"):
            entry["message"] = rr["message"]
        rules_summary.append(entry)

    tool_trace: List[Dict[str, Any]] = tool_traces.get(result.get("id")) or []
    completions_trace: List[Dict[str, Any]] = completions_by_result.get(result.get("id")) or []

    _append_report_line({
        "llm": llm_display,
        "suite": suite_name,
        "case": case_name,
        "tags": list(tags),
        "status": status,
        "failure_reason": result.get("failure_reason"),
        "harness_duration_ms": harness_duration_ms,
        "totals": rj.get("totals"),
        "rules": rules_summary,
        "tools": tool_trace,
        # Per-turn agent breakdown: one system-completion per turn, each
        # with its ordered blocks (planner reasoning, tool calls with
        # durations, final answer content). Strings are truncated.
        "completions": completions_trace,
    })

    if status != "pass":
        pytest.fail(
            _format_result_report(
                result, case_label=case_label, llm_display=llm_display,
            )
        )
