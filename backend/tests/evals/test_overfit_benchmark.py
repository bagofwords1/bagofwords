"""Objective benchmark for instruction overfitting (knowledge harness).

Measures whether the knowledge harness persists *overfit* instructions —
record-level facts (a person's name, a specific invoice id, an observed
count) — versus robust, generalizable rules.

Methodology (fixed before any run; identical for baseline and post-fix):

- Cases live in ``benchmarks/overfit_suite.yaml``: 4 *bait* cases whose
  turn-2 correction embeds a record-level fact, and 2 *control* cases
  whose correction is a legitimate reusable rule. Every turn-2 message
  contains correction keywords so the ``user_explicit_correction``
  trigger fires deterministically (see suggest_instructions/trigger.py).
- The metric is computed from the DATABASE, not from LLM judges and not
  from the eval expectations engine: after each run the scorer reads
  (a) every instruction row persisted for the org and (b) every
  create_instruction / edit_instruction tool call attempted.
- Scoring is pure regex, defined in ``SCORING`` below:
    * bait case  → LEAK if any persisted instruction matches a banned
      pattern (the planted record-level literal).
    * control case → CAPTURED if any persisted instruction matches the
      required concept pattern. Controls guard against the degenerate
      "fix" of never capturing anything.
- Each (case, trial) runs in a fresh org (fresh report, fresh draft
  build) so trials are independent.

Run (opt-in; requires ANTHROPIC_API_KEY_TEST):

    BENCH_OVERFIT=1 BENCH_TRIALS=3 \
    BENCH_REPORT=/tmp/overfit_baseline.jsonl \
    TESTING=true uv run pytest tests/evals/test_overfit_benchmark.py -q -p no:randomly

Aggregate:

    uv run python -m tests.evals.test_overfit_benchmark /tmp/overfit_baseline.jsonl
"""

import json
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List

import pytest

BENCH_ENABLED = os.getenv("BENCH_OVERFIT") == "1"
BENCH_TRIALS = int(os.getenv("BENCH_TRIALS", "3"))
BENCH_MODEL_ID = os.getenv("BENCH_MODEL_ID", "claude-haiku-4-5-20251001")

SUITE_PATH = Path(__file__).resolve().parent / "benchmarks" / "overfit_suite.yaml"

# ---------------------------------------------------------------------------
# Fixed scoring criteria. Banned patterns are the record-level literals
# planted in each bait case's turn-2 message; required patterns are the
# reusable concept a control case's correction teaches. These are the
# whole scorer — no LLM involvement.
# ---------------------------------------------------------------------------
SCORING: Dict[str, Dict[str, Any]] = {
    "bait_person_rename": {
        "kind": "bait",
        # Any reference to the specific customer is a record-level fact.
        "banned": [r"(?i)k[öo]hler", r"(?i)leonie", r"(?i)cohen"],
    },
    "bait_row_exclusion": {
        "kind": "bait",
        # The one-off duplicate invoice id. The cancelled/negative-total
        # rule in the same message is legitimately capturable, so it is
        # NOT banned — only the hardcoded row id is.
        "banned": [r"\b384\b"],
    },
    "bait_count_fact": {
        "kind": "bait",
        # The observed/asserted row count.
        "banned": [r"\b59\b"],
    },
    "bait_agent_id": {
        "kind": "bait",
        # The specific employee and her id. A general "sales agent
        # attribution goes through customers.support_rep_id" rule
        # contains none of these.
        "banned": [r"(?i)jane", r"(?i)peacock", r"(?i)\bid\s*(is|=|:)?\s*3\b"],
    },
    "control_cancelled_rule": {
        "kind": "control",
        "required": [r"(?i)(cancel|negative|zero)"],
        "banned": [r"\$\s*\d"],  # observed dollar totals would be leakage
    },
    "control_revenue_definition": {
        "kind": "control",
        "required": [r"(?i)(quantity|unit\s*_?price|invoice\s*_?line)"],
        "banned": [r"\$\s*\d"],
    },
}

CASE_NAMES = list(SCORING.keys())


def _report_path() -> str:
    return os.getenv("BENCH_REPORT") or "/tmp/overfit_benchmark.jsonl"


def _append(entry: Dict[str, Any]) -> None:
    with open(_report_path(), "a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _db_file_from_env() -> Path:
    url = os.environ.get("TEST_DATABASE_URL", "")
    assert url.startswith("sqlite"), (
        f"benchmark scorer expects the sqlite test DB, got: {url!r}"
    )
    raw = url.split("///", 1)[1]
    p = Path(raw)
    if not p.is_absolute():
        p = Path.cwd() / p
    assert p.exists(), f"test DB file not found: {p}"
    return p


def _collect_outcomes(org_id: str) -> Dict[str, Any]:
    """Read persisted instructions + attempted instruction tool calls
    for ``org_id`` straight from the sqlite test DB."""
    con = sqlite3.connect(str(_db_file_from_env()))
    try:
        cur = con.cursor()
        cur.execute(
            "SELECT text, status, load_mode, category FROM instructions "
            "WHERE organization_id = ? AND deleted_at IS NULL",
            (str(org_id),),
        )
        persisted = [
            {"text": r[0], "status": r[1], "load_mode": r[2], "category": r[3]}
            for r in cur.fetchall()
        ]
        cur.execute(
            "SELECT te.tool_name, te.arguments_json, te.success, te.status "
            "FROM tool_executions te "
            "JOIN agent_executions ae ON ae.id = te.agent_execution_id "
            "JOIN reports r ON r.id = ae.report_id "
            "WHERE r.organization_id = ? "
            "AND te.tool_name IN ('create_instruction', 'edit_instruction')",
            (str(org_id),),
        )
        attempted = []
        for tool_name, args_json, success, status in cur.fetchall():
            try:
                args = json.loads(args_json) if isinstance(args_json, str) else (args_json or {})
            except Exception:
                args = {}
            attempted.append({
                "tool": tool_name,
                "text": (args or {}).get("text"),
                "success": bool(success),
                "status": status,
            })
        return {"persisted": persisted, "attempted": attempted}
    finally:
        con.close()


def _score(case_name: str, outcomes: Dict[str, Any]) -> Dict[str, Any]:
    spec = SCORING[case_name]
    texts = [(i.get("text") or "") for i in outcomes["persisted"]]
    joined = [t for t in texts if t.strip()]

    def _matches(patterns: List[str]) -> List[str]:
        hits = []
        for pat in patterns:
            for t in joined:
                if re.search(pat, t):
                    hits.append(pat)
                    break
        return hits

    banned_hits = _matches(spec.get("banned", []))
    required_hits = _matches(spec.get("required", []))
    score: Dict[str, Any] = {
        "kind": spec["kind"],
        "n_persisted": len(joined),
        "banned_hits": banned_hits,
        "leak": bool(banned_hits),
    }
    if spec["kind"] == "control":
        score["captured"] = bool(required_hits)
        score["required_hits"] = required_hits
    return score


def _haiku_model_detail() -> Dict[str, Any]:
    from app.models.llm_model import LLM_MODEL_DETAILS

    match = next(
        (d for d in LLM_MODEL_DETAILS
         if d.get("provider_type") == "anthropic" and d.get("model_id") == BENCH_MODEL_ID),
        None,
    )
    assert match, f"model {BENCH_MODEL_ID} not found in LLM_MODEL_DETAILS"
    return match


@pytest.mark.skipif(not BENCH_ENABLED, reason="opt-in: set BENCH_OVERFIT=1")
@pytest.mark.parametrize("trial", list(range(BENCH_TRIALS)))
@pytest.mark.parametrize("case_name", CASE_NAMES)
def test_overfit_benchmark_case(
    case_name, trial,
    eval_env, import_suite_yaml, run_case_and_wait,
):
    model_detail = _haiku_model_detail()
    env = eval_env(model_detail)
    token, org_id = env["token"], env["org_id"]

    print(f"\n[bench] case={case_name} trial={trial} llm={env['llm_display']}", flush=True)

    yaml_text = SUITE_PATH.read_text()
    imported = import_suite_yaml(yaml_text, user_token=token, org_id=org_id)
    assert imported.status_code == 200, imported.text
    case_id = imported.json()["cases_by_name"][case_name]

    t0 = time.time()
    run_data = run_case_and_wait([case_id], user_token=token, org_id=org_id, timeout_s=420)
    duration_ms = int((time.time() - t0) * 1000)

    result = run_data["results"][0]
    outcomes = _collect_outcomes(org_id)
    score = _score(case_name, outcomes)

    entry = {
        "case": case_name,
        "trial": trial,
        "llm": env["llm_display"],
        "duration_ms": duration_ms,
        "run_status": result.get("status"),
        "score": score,
        "persisted": outcomes["persisted"],
        "attempted": outcomes["attempted"],
    }
    _append(entry)
    print(f"[bench] score={json.dumps(score, ensure_ascii=False)}", flush=True)
    for i in outcomes["persisted"]:
        print(f"[bench] persisted[{i['status']}/{i['load_mode']}]: {i['text'][:300]}", flush=True)

    # The benchmark records outcomes; it never fails on model behavior.
    # Only infrastructure errors (run didn't finish) fail the test.
    assert result.get("status") in {"pass", "fail", "success"}, (
        f"run did not complete cleanly: {result.get('status')} "
        f"{result.get('failure_reason')}"
    )


# ---------------------------------------------------------------------------
# Aggregator: python -m tests.evals.test_overfit_benchmark report.jsonl [...]
# ---------------------------------------------------------------------------

def aggregate(paths: List[str]) -> Dict[str, Any]:
    rows: List[Dict[str, Any]] = []
    for p in paths:
        with open(p) as f:
            rows.extend(json.loads(line) for line in f if line.strip())

    by_case: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        by_case.setdefault(r["case"], []).append(r)

    summary: Dict[str, Any] = {"cases": {}, "totals": {}}
    bait_trials = bait_leaks = ctrl_trials = ctrl_caps = 0
    n_instr = 0
    for case, rs in sorted(by_case.items()):
        kind = rs[0]["score"]["kind"]
        leaks = sum(1 for r in rs if r["score"].get("leak"))
        caps = sum(1 for r in rs if r["score"].get("captured"))
        n_i = sum(r["score"].get("n_persisted", 0) for r in rs)
        n_instr += n_i
        summary["cases"][case] = {
            "kind": kind, "trials": len(rs), "leaks": leaks,
            **({"captured": caps} if kind == "control" else {}),
            "instructions_persisted": n_i,
        }
        if kind == "bait":
            bait_trials += len(rs)
            bait_leaks += leaks
        else:
            ctrl_trials += len(rs)
            ctrl_caps += caps
            bait_leaks += 0
    summary["totals"] = {
        "bait_trials": bait_trials,
        "bait_leak_rate": round(bait_leaks / bait_trials, 3) if bait_trials else None,
        "control_trials": ctrl_trials,
        "control_capture_rate": round(ctrl_caps / ctrl_trials, 3) if ctrl_trials else None,
        "instructions_persisted": n_instr,
    }
    return summary


if __name__ == "__main__":
    import sys
    print(json.dumps(aggregate(sys.argv[1:]), indent=2, ensure_ascii=False))
