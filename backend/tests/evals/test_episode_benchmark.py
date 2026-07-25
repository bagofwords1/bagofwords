"""Objective benchmark for episode-shaped instruction authoring.

Complements ``test_overfit_benchmark.py``. That benchmark scores whether
the agent leaks *record-level literals* (a person's name, a row id, an
observed count). This one scores a different failure: whether the agent
writes an instruction as a **log of the conversation** — referencing what
changed, what was removed, what the text used to say, or what was just
asked — instead of as a statement of current state.

The motivating trace:

    user:  "we removed col1 from the tables, remove it from the instruction"
    agent: edit_instruction -> "Do not use col1, it was removed"

The user issued a maintenance directive; the agent transcribed the
directive into the artifact instead of executing it. The result is a
tombstone: an instruction whose whole content is about something that no
longer exists. It can never change a query and it never expires.

Methodology (fixed before any run; identical for baseline and post-fix):

- Cases live in ``benchmarks/episode_suite.yaml``.
    * ``maint_*``   run in **training mode** against a PRE-SEEDED
      instruction and issue a removal directive. Correct behaviour is to
      excise the clause; the failure is appending a tombstone.
    * ``capture_*`` run in chat mode; the correction teaches a genuine
      reusable rule and the knowledge harness captures it.
- The metric is computed from the DATABASE, not from LLM judges: the
  scorer reads the text the agent actually AUTHORED — the ``text``
  argument of every ``create_instruction`` / ``edit_instruction`` call in
  ``tool_executions`` — plus the final persisted rows. Authored text is
  the primary signal because it is unaffected by whether the draft build
  was promoted.
- Scoring is pure regex, defined in ``SCORING`` / ``META_PATTERNS``:
    * ``meta_leak``  — authored text references the episode, the change
      history, or the conversation. Applies to every case.
    * ``stale_ref``  — (maint only) authored text still mentions the thing
      the user said was removed.
    * ``missing``    — authored text lost the surviving rule. Guards
      against the degenerate "fix" of deleting everything or writing
      nothing.
    * ``chars``      — length of authored text, for token sensitivity.
- Each (case, trial) runs in a fresh org so trials are independent.

Run (opt-in; requires ANTHROPIC_API_KEY_TEST):

    BENCH_EPISODE=1 BENCH_TRIALS=3 \
    BENCH_REPORT=/tmp/episode_baseline.jsonl \
    TESTING=true uv run pytest tests/evals/test_episode_benchmark.py -q -p no:randomly

Aggregate:

    uv run python -m tests.evals.test_episode_benchmark /tmp/episode_baseline.jsonl
"""

import json
import os
import re
import sqlite3
import statistics
import time
from pathlib import Path
from typing import Any, Dict, List

import pytest

BENCH_ENABLED = os.getenv("BENCH_EPISODE") == "1"
BENCH_TRIALS = int(os.getenv("BENCH_TRIALS", "3"))
BENCH_MODEL_ID = os.getenv("BENCH_MODEL_ID", "claude-haiku-4-5-20251001")

SUITE_PATH = Path(__file__).resolve().parent / "benchmarks" / "episode_suite.yaml"


# ---------------------------------------------------------------------------
# Instructions seeded BEFORE the run. maint_* cases need something to edit;
# capture_conflict_edit needs an existing definition to contradict so the
# harness takes the edit path rather than the create path.
# ---------------------------------------------------------------------------
SEEDED_INSTRUCTIONS: Dict[str, Dict[str, str]] = {
    "maint_column_removed": {
        "title": "Customer region grouping",
        "text": (
            "Group customers by region using `customers.legacy_region_code`. "
            "When `legacy_region_code` is null, fall back to `customers.Country`. "
            "Region labels are uppercase two-letter codes."
        ),
    },
    "maint_clause_dropped": {
        "title": "Invoice totals",
        "text": (
            "Invoice totals are stored in `invoices.Total` and are inclusive of tax. "
            "Before reporting monthly totals, apply the nightly reconciliation "
            "adjustments from the reconciliation job so figures match finance."
        ),
    },
    "capture_conflict_edit": {
        "title": "Revenue definition",
        "text": "Revenue is calculated as the sum of the `invoices.Total` column.",
    },
}


# ---------------------------------------------------------------------------
# Fixed scoring criteria.
#
# META_PATTERNS are the episode/changelog tells — text that talks ABOUT the
# conversation or the change history rather than stating the current rule.
# They apply to every case. Contrastive definitions ("revenue means X, not
# the Total column") are deliberately NOT banned: naming the wrong-but-
# tempting alternative is legitimate, durable rule content. Only temporal
# and meta references are.
# ---------------------------------------------------------------------------
META_PATTERNS: List[str] = [
    r"(?i)\bno longer\b",
    r"(?i)\b(was|were) (removed|dropped|deleted)\b",
    r"(?i)\b(has|have) been (removed|dropped|deleted)\b",
    r"(?i)\bdoes not exist (anymore|any more|now)\b",
    r"(?i)\bnot exist anymore\b",
    r"(?i)\bpreviously\b",
    r"(?i)\bused to\b",
    r"(?i)\bformerly\b",
    r"(?i)\bdeprecated\b",
    r"(?i)\bobsolete\b",
    r"(?i)\bmigration\b",
    r"(?i)\bupdated to reflect\b",
    r"(?i)\bnow correctly\b",
    r"(?i)\bchanged from\b",
    r"(?i)\bthis session\b",
    r"(?i)\bthe user (said|asked|clarified|corrected|meant|wanted|requested)\b",
    r"(?i)\bper the user'?s?\b",
    r"(?i)\b(this|the previous) (report|query|answer|question)\b",
    r"(?i)\bas (originally|initially)\b",
]

SCORING: Dict[str, Dict[str, Any]] = {
    "maint_column_removed": {
        "kind": "maint",
        # The dropped column must be gone from the text entirely — neither
        # mentioned nor negated.
        "stale": [r"(?i)legacy_region_code"],
        # The surviving half of the rule must remain.
        "required": [r"(?i)\bcountry\b"],
    },
    "maint_clause_dropped": {
        "kind": "maint",
        "stale": [r"(?i)reconcil"],
        "required": [r"(?i)(invoice|total)"],
    },
    "capture_top_selling": {
        "kind": "capture",
        "stale": [],
        "required": [r"(?i)(revenue|quantity|unit\s*_?price)"],
    },
    "capture_conflict_edit": {
        "kind": "capture",
        "stale": [],
        "required": [r"(?i)(quantity|unit\s*_?price|invoice\s*_?line)"],
    },
}

CASE_NAMES = list(SCORING.keys())


def _report_path() -> str:
    return os.getenv("BENCH_REPORT") or "/tmp/episode_benchmark.jsonl"


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


def _seed_instruction(test_client, case_name, *, token, org_id, data_source_id):
    """Create the pre-existing instruction a case edits. Returns its id."""
    spec = SEEDED_INSTRUCTIONS.get(case_name)
    if not spec:
        return None
    resp = test_client.post(
        "/api/instructions/global",
        json={
            "text": spec["text"],
            "title": spec["title"],
            "category": "general",
            "status": "published",
            "load_mode": "always",
            "data_source_ids": [str(data_source_id)],
        },
        headers={
            "Authorization": f"Bearer {token}",
            "X-Organization-Id": str(org_id),
        },
    )
    assert resp.status_code == 200, f"seeding failed: {resp.status_code} {resp.text}"
    return resp.json()["id"]


def _collect_outcomes(org_id: str, seeded_id: str | None) -> Dict[str, Any]:
    """Read authored instruction text + final rows for ``org_id``.

    ``authored`` — the ``text`` argument of every create/edit_instruction
    call, i.e. exactly what the model wrote. This is the primary signal:
    it is independent of whether the draft build was ever promoted.
    """
    con = sqlite3.connect(str(_db_file_from_env()))
    try:
        cur = con.cursor()
        cur.execute(
            "SELECT te.tool_name, te.arguments_json, te.success, te.status "
            "FROM tool_executions te "
            "JOIN agent_executions ae ON ae.id = te.agent_execution_id "
            "JOIN reports r ON r.id = ae.report_id "
            "WHERE r.organization_id = ? "
            "AND te.tool_name IN ('create_instruction', 'edit_instruction') "
            "ORDER BY te.created_at",
            (str(org_id),),
        )
        authored = []
        for tool_name, args_json, success, status in cur.fetchall():
            try:
                args = json.loads(args_json) if isinstance(args_json, str) else (args_json or {})
            except Exception:
                args = {}
            authored.append({
                "tool": tool_name,
                "text": (args or {}).get("text"),
                "evidence": (args or {}).get("evidence"),
                "instruction_id": (args or {}).get("instruction_id"),
                "success": bool(success),
                "status": status,
            })

        # Final text of the seeded instruction: latest version wins (an AI
        # edit stages a new instruction_versions row while the build is a
        # draft, so instructions.text can lag).
        seeded_final = None
        if seeded_id:
            cur.execute(
                "SELECT text FROM instruction_versions WHERE instruction_id = ? "
                "ORDER BY version_number DESC LIMIT 1",
                (str(seeded_id),),
            )
            row = cur.fetchone()
            if row:
                seeded_final = row[0]
            else:
                cur.execute("SELECT text FROM instructions WHERE id = ?", (str(seeded_id),))
                row = cur.fetchone()
                seeded_final = row[0] if row else None

        cur.execute(
            "SELECT pd.action_name, pd.analysis_complete "
            "FROM plan_decisions pd "
            "JOIN agent_executions ae ON ae.id = pd.agent_execution_id "
            "JOIN reports r ON r.id = ae.report_id "
            "WHERE r.organization_id = ? AND pd.phase = 'knowledge_harness' "
            "ORDER BY pd.created_at",
            (str(org_id),),
        )
        harness_steps = [{"action": a, "analysis_complete": bool(ac)} for a, ac in cur.fetchall()]

        return {
            "authored": authored,
            "seeded_final": seeded_final,
            "harness_steps": harness_steps,
        }
    finally:
        con.close()


def _score(case_name: str, outcomes: Dict[str, Any]) -> Dict[str, Any]:
    spec = SCORING[case_name]

    # Score the text the model authored. For maint cases also score the
    # seeded instruction's final text, so a rewrite performed some other
    # way is still caught.
    texts = [a["text"] for a in outcomes["authored"] if (a.get("text") or "").strip()]
    if spec["kind"] == "maint" and (outcomes.get("seeded_final") or "").strip():
        texts.append(outcomes["seeded_final"])

    def _hits(patterns: List[str]) -> List[str]:
        out = []
        for pat in patterns:
            for t in texts:
                if re.search(pat, t):
                    out.append(pat)
                    break
        return out

    meta_hits = _hits(META_PATTERNS)
    stale_hits = _hits(spec.get("stale", []))
    required_hits = _hits(spec.get("required", []))
    lengths = [len(t) for t in texts]

    return {
        "kind": spec["kind"],
        "n_authored": len(texts),
        "wrote_anything": bool(texts),
        "meta_hits": meta_hits,
        "meta_leak": bool(meta_hits),
        "stale_hits": stale_hits,
        "stale_ref": bool(stale_hits),
        "required_hits": required_hits,
        # "missing" only means something if the agent wrote at all.
        "missing": bool(texts) and not required_hits,
        "max_chars": max(lengths) if lengths else 0,
        "total_chars": sum(lengths),
    }


def _haiku_model_detail() -> Dict[str, Any]:
    from app.models.llm_model import LLM_MODEL_DETAILS

    match = next(
        (d for d in LLM_MODEL_DETAILS
         if d.get("provider_type") == "anthropic" and d.get("model_id") == BENCH_MODEL_ID),
        None,
    )
    assert match, f"model {BENCH_MODEL_ID} not found in LLM_MODEL_DETAILS"
    return match


@pytest.mark.skipif(not BENCH_ENABLED, reason="opt-in: set BENCH_EPISODE=1")
@pytest.mark.parametrize("trial", list(range(BENCH_TRIALS)))
@pytest.mark.parametrize("case_name", CASE_NAMES)
def test_episode_benchmark_case(
    case_name, trial,
    eval_env, import_suite_yaml, run_case_and_wait, test_client,
):
    model_detail = _haiku_model_detail()
    env = eval_env(model_detail)
    token, org_id = env["token"], env["org_id"]

    print(f"\n[bench] case={case_name} trial={trial} llm={env['llm_display']}", flush=True)

    seeded_id = _seed_instruction(
        test_client, case_name,
        token=token, org_id=org_id, data_source_id=env["data_source_id"],
    )
    if seeded_id:
        print(f"[bench] seeded instruction {seeded_id[:8]}", flush=True)

    yaml_text = SUITE_PATH.read_text()
    imported = import_suite_yaml(yaml_text, user_token=token, org_id=org_id)
    assert imported.status_code == 200, imported.text
    case_id = imported.json()["cases_by_name"][case_name]

    t0 = time.time()
    run_data = run_case_and_wait([case_id], user_token=token, org_id=org_id, timeout_s=420)
    duration_ms = int((time.time() - t0) * 1000)

    result = run_data["results"][0]
    outcomes = _collect_outcomes(org_id, seeded_id)
    score = _score(case_name, outcomes)
    score["knowledge_phase_ran"] = bool(outcomes["harness_steps"])

    entry = {
        "case": case_name,
        "trial": trial,
        "llm": env["llm_display"],
        "duration_ms": duration_ms,
        "run_status": result.get("status"),
        "score": score,
        "authored": outcomes["authored"],
        "seeded_final": outcomes["seeded_final"],
        "harness_steps": outcomes["harness_steps"],
    }
    _append(entry)
    print(f"[bench] score={json.dumps(score, ensure_ascii=False)}", flush=True)
    for a in outcomes["authored"]:
        print(f"[bench] {a['tool']}: {(a.get('text') or '')[:400]}", flush=True)
    if outcomes.get("seeded_final"):
        print(f"[bench] seeded_final: {outcomes['seeded_final'][:400]}", flush=True)

    # The benchmark records outcomes; it never fails on model behavior.
    # Only infrastructure errors fail the test.
    assert result.get("status") in {"pass", "fail", "success"}, (
        f"run did not complete cleanly: {result.get('status')} "
        f"{result.get('failure_reason')}"
    )
    comps = (run_data.get("completions") or {}).get(result.get("id")) or []
    errored = [c for c in comps if c.get("role") != "user" and c.get("status") == "error"]
    assert not errored, (
        "agent completions errored — trial is invalid, not a behavioral outcome: "
        + "; ".join((c.get("content") or "unknown error")[:200] for c in errored)
    )


# ---------------------------------------------------------------------------
# Aggregator: python -m tests.evals.test_episode_benchmark report.jsonl [...]
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
    tot_trials = tot_meta = tot_stale = tot_missing = tot_wrote = 0
    maint_trials = maint_stale = 0
    all_chars: List[int] = []

    for case, rs in sorted(by_case.items()):
        kind = rs[0]["score"]["kind"]
        wrote = sum(1 for r in rs if r["score"].get("wrote_anything"))
        meta = sum(1 for r in rs if r["score"].get("meta_leak"))
        stale = sum(1 for r in rs if r["score"].get("stale_ref"))
        missing = sum(1 for r in rs if r["score"].get("missing"))
        chars = [r["score"].get("max_chars", 0) for r in rs if r["score"].get("wrote_anything")]
        all_chars.extend(chars)
        summary["cases"][case] = {
            "kind": kind,
            "trials": len(rs),
            "wrote_anything": wrote,
            "meta_leaks": meta,
            **({"stale_refs": stale} if kind == "maint" else {}),
            "missing_rule": missing,
            "median_chars": int(statistics.median(chars)) if chars else None,
        }
        tot_trials += len(rs)
        tot_meta += meta
        tot_stale += stale
        tot_missing += missing
        tot_wrote += wrote
        if kind == "maint":
            maint_trials += len(rs)
            maint_stale += stale

    summary["totals"] = {
        "trials": tot_trials,
        "wrote_anything_rate": round(tot_wrote / tot_trials, 3) if tot_trials else None,
        "meta_leak_rate": round(tot_meta / tot_trials, 3) if tot_trials else None,
        "maint_trials": maint_trials,
        "stale_ref_rate": round(maint_stale / maint_trials, 3) if maint_trials else None,
        "missing_rule_rate": round(tot_missing / tot_trials, 3) if tot_trials else None,
        "median_max_chars": int(statistics.median(all_chars)) if all_chars else None,
    }
    return summary


if __name__ == "__main__":
    import sys
    print(json.dumps(aggregate(sys.argv[1:]), indent=2, ensure_ascii=False))
