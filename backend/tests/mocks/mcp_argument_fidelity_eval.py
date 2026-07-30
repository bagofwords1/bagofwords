"""MCP argument-fidelity eval — drives the real agent against the
complex-schema mock MCP server and scores what it put on the wire.

Ten prompts, one per mock tool, each phrased the way a user actually would —
in natural language, never naming argument names or types. The mock server is
the oracle: it validates each call against the tool's declared JSON Schema and
records every attempt. The headline metric is **first-call validity**: did the
agent's FIRST attempt at a given tool satisfy the schema?

Usage (from backend/, with the mock server running on :3334)::

    BOW_DATABASE_URL='sqlite:///db/app.db' \
    MOCK_MCP_CAPTURE_FILE=/tmp/bow-agent/mcp_calls.jsonl \
    BOW_PLANNER_DUMP_FILE=/tmp/bow-agent/ctx_before.jsonl \
        uv run python tests/mocks/mcp_argument_fidelity_eval.py --label before

Writes a scored JSON report to /tmp/bow-agent/report_<label>.json.
"""

from __future__ import annotations

import argparse
import json
import os
import time

import httpx

BASE = "http://localhost:8000/api"
SEED = "/tmp/bow-agent/seed.json"

# One prompt per tool. Deliberately natural: no argument names, no type hints —
# the agent must get the shape from the schema, which is exactly what is under
# test. Each names the tool's *purpose* so tool selection isn't the variable.
CASES: list[dict[str, str]] = [
    {
        "tool": "board_change_column_values",
        "prompt": "On board 7788 in MockOps, set item 4455's status column (id 'status3') to Done and its text column (id 'text5') to 'Reviewed'. Use the board column update tool.",
    },
    {
        "tool": "issue_create",
        "prompt": "Create an issue in MockOps for team 'team_abc' titled 'Fix login redirect', with urgent priority, estimated at 3 points.",
    },
    {
        "tool": "search_workspace",
        "prompt": "Search the MockOps workspace for 'quarterly planning', restricted to pages only, and give me 10 results.",
    },
    {
        "tool": "find_errors",
        "prompt": "In MockOps, find errors for organization 'acme' project 'checkout' over the last 24 hours, sorted by how many users were affected.",
    },
    {
        "tool": "jira_edit_issue",
        "prompt": "In MockOps, edit Jira issue PROJ-142: change the summary to 'Checkout timeout on mobile' and add the label 'urgent'. Don't notify anyone.",
    },
    {
        "tool": "metrics_query",
        "prompt": "In MockOps, query the metric 'avg:system.cpu.user{*}' for the period from midnight on 1 March 2026 to midnight on 2 March 2026 UTC.",
    },
    {
        "tool": "contact_upsert",
        "prompt": "In MockOps, upsert a contact: email dana@acme.io, display name 'Dana Reed', address 12 King St, London, United Kingdom.",
    },
    {
        "tool": "analytics_run_report",
        "prompt": "In MockOps, run an analytics report on property '9911' for the metrics 'activeUsers' and 'sessions', broken down by the 'country' dimension, for March 2026.",
    },
    {
        "tool": "records_batch_update",
        "prompt": "In MockOps, on table 'tbl_99', set field 'owner' to 'dana' on record 'rec_1', and delete record 'rec_2'. Do it as one batch.",
    },
    {
        "tool": "workflow_trigger",
        "prompt": "In MockOps, trigger workflow 'wf_nightly' with a payload saying region is 'emea' and dryRun is true. Use idempotency key 'nightly2026a'.",
    },
]


# A single prompt that forces MANY sequential MCP calls in one agent run.
#
# This is the scenario the per-case suite above cannot reach. With one task per
# report the agent calls search_mcps and then execute_mcp one turn later, so the
# schema is sitting verbatim in <last_observation> and argument fidelity is easy.
# Real sessions are not like that. Here the schemas are fetched ONCE at the top
# and then have to survive nine more tool observations — past the
# _RECENT_OBS_FULL=5 window, after which _compact_past_observations strips
# observation["tools"] (not in _OBS_KEEP_KEYS) down to a bare summary line.
#
# The tools are ordered so the hardest shapes (JSON-encoded strings, unix
# epochs, $ref arrays) land LAST, i.e. furthest from the schema.
PRESSURE_PROMPT = (
    "Using MockOps, work through this checklist in order, one tool call at a time. "
    "Do not stop until all nine are done.\n"
    "1. Search the workspace for 'quarterly planning', pages only, 10 results.\n"
    "2. Find errors for organization 'acme' project 'checkout' over the last 24 hours, sorted by user count.\n"
    "3. Create an issue for team 'team_abc' titled 'Fix login redirect', urgent priority, estimated 3 points.\n"
    "4. Upsert a contact: email dana@acme.io, display name 'Dana Reed', address 12 King St, London, United Kingdom.\n"
    "5. Run an analytics report on property '9911' for metrics 'activeUsers' and 'sessions', by 'country', for March 2026.\n"
    "6. On board 7788, set item 4455's status column (id 'status3') to Done and text column (id 'text5') to 'Reviewed'.\n"
    "7. Edit Jira issue PROJ-142: summary to 'Checkout timeout on mobile', add label 'urgent', don't notify.\n"
    "8. Query metric 'avg:system.cpu.user{*}' from midnight 1 March 2026 to midnight 2 March 2026 UTC.\n"
    "9. On table 'tbl_99', set field 'owner' to 'dana' on record 'rec_1' and delete record 'rec_2', as one batch.\n"
)

PRESSURE_EXPECTED = [
    "search_workspace", "find_errors", "issue_create", "contact_upsert",
    "analytics_run_report", "board_change_column_values", "jira_edit_issue",
    "metrics_query", "records_batch_update",
]


def load_seed() -> dict:
    """Load seeded ids and mint a FRESH token.

    The dev JWT secret is regenerated per backend process, so a token stored at
    seed time is dead after any restart — re-authenticate instead of reusing it.
    """
    with open(SEED) as f:
        seed = json.load(f)
    r = httpx.post(
        f"{BASE}/auth/jwt/login",
        data={"username": seed["email"], "password": "TestPassword123!"},
        timeout=60.0,
    )
    r.raise_for_status()
    seed["token"] = r.json()["access_token"]
    return seed


def headers(seed: dict) -> dict:
    return {
        "Authorization": f"Bearer {seed['token']}",
        "X-Organization-Id": seed["org_id"],
    }


def run_case(c: httpx.Client, seed: dict, case: dict, timeout_s: int = 240) -> dict:
    """Create a fresh report per case (isolated context) and run one turn."""
    H = headers(seed)
    r = c.post("/reports", headers=H, json={
        "title": f"eval-{case['tool']}",
        "data_sources": [seed["ds_id"]],
    })
    r.raise_for_status()
    report_id = r.json()["id"]

    r = c.post(f"/reports/{report_id}/completions", headers=H, json={
        "prompt": {"content": case["prompt"]},
        "stream": False,
    })
    r.raise_for_status()

    deadline = time.time() + timeout_s
    status = "unknown"
    while time.time() < deadline:
        time.sleep(4)
        rr = c.get(f"/reports/{report_id}/completions", headers=H)
        if rr.status_code != 200:
            continue
        body = rr.json()
        # The endpoint returns {"completions": [...]} on some builds and a bare
        # list on others; a bare dict iterates as its KEYS (strings), which is
        # how this silently crashed rather than just polling.
        comps = body.get("completions", []) if isinstance(body, dict) else body
        if not isinstance(comps, list):
            continue
        sys_comps = [x for x in comps if isinstance(x, dict) and x.get("role") == "system"]
        if sys_comps and sys_comps[-1].get("status") in ("success", "error"):
            status = sys_comps[-1]["status"]
            break
    return {"tool": case["tool"], "report_id": report_id, "status": status}


def score(capture_file: str, cases: list[dict]) -> dict:
    """First-call validity per tool, from the oracle's capture file."""
    calls: list[dict] = []
    if os.path.exists(capture_file):
        with open(capture_file) as f:
            for line in f:
                line = line.strip()
                if line:
                    calls.append(json.loads(line))

    per_tool: dict[str, dict] = {}
    for call in calls:
        t = call["tool"]
        entry = per_tool.setdefault(t, {"attempts": 0, "first_valid": None, "violations": []})
        entry["attempts"] += 1
        if entry["first_valid"] is None:
            entry["first_valid"] = bool(call["valid"])
            if not call["valid"]:
                entry["violations"] = call["violations"]
        if call["valid"]:
            entry["eventually_valid"] = True

    expected = [c["tool"] for c in cases]
    attempted = [t for t in expected if t in per_tool]
    first_valid = [t for t in attempted if per_tool[t]["first_valid"]]
    ever_valid = [t for t in attempted if per_tool[t].get("eventually_valid")]

    return {
        "tools_expected": len(expected),
        "tools_attempted": len(attempted),
        "first_call_valid": len(first_valid),
        "eventually_valid": len(ever_valid),
        "never_attempted": [t for t in expected if t not in per_tool],
        "total_calls": len(calls),
        "per_tool": per_tool,
    }


def schema_in_context(dump_file: str) -> dict:
    """How often was a real argument schema present in the prompt at generation
    time? Looks for schema-shaped markers inside the rendered <mcp_tools> block
    and in past observations."""
    if not os.path.exists(dump_file):
        return {"error": "no dump file"}
    total = 0
    with_schema = 0
    with open(dump_file) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            total += 1
            blob = (rec.get("messages") or [{}])[0].get("content", "")
            # A schema is "present" if the prompt contains an actual properties
            # map for a mock tool, not merely the tool's name.
            if '"properties"' in blob or "<arg " in blob:
                with_schema += 1
    return {"planner_turns": total, "turns_with_schema": with_schema}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", required=True)
    ap.add_argument("--only", default=None, help="comma-separated tool names")
    ap.add_argument("--mode", default="cases", choices=["cases", "pressure"],
                    help="'cases' = one fresh report per tool (schema always fresh); "
                         "'pressure' = one report, nine sequential calls (schema ages out)")
    args = ap.parse_args()

    seed = load_seed()

    if args.mode == "pressure":
        capture = os.environ.get("MOCK_MCP_CAPTURE_FILE", "/tmp/bow-agent/mcp_calls.jsonl")
        dump = os.environ.get("BOW_PLANNER_DUMP_FILE", "")
        with httpx.Client(base_url=BASE, timeout=900.0) as c:
            res = run_case(
                c, seed,
                {"tool": "pressure", "prompt": PRESSURE_PROMPT},
                timeout_s=900,
            )
        print("   ", res)
        cases = [{"tool": t} for t in PRESSURE_EXPECTED]
        report = {
            "label": args.label,
            "mode": "pressure",
            "runs": [res],
            "score": score(capture, cases),
            "context": schema_in_context(dump) if dump else {},
        }
        out = f"/tmp/bow-agent/report_{args.label}.json"
        with open(out, "w") as f:
            json.dump(report, f, indent=2)
        print(json.dumps(report["score"], indent=2))
        print("wrote", out)
        return

    cases = CASES
    if args.only:
        want = set(args.only.split(","))
        cases = [c for c in CASES if c["tool"] in want]

    capture = os.environ.get("MOCK_MCP_CAPTURE_FILE", "/tmp/bow-agent/mcp_calls.jsonl")
    dump = os.environ.get("BOW_PLANNER_DUMP_FILE", "")

    results = []
    with httpx.Client(base_url=BASE, timeout=300.0) as c:
        for case in cases:
            print(f"--- {case['tool']}")
            try:
                res = run_case(c, seed, case)
            except Exception as e:
                res = {"tool": case["tool"], "status": f"harness_error: {e}"}
            print("   ", res)
            results.append(res)

    report = {
        "label": args.label,
        "runs": results,
        "score": score(capture, cases),
        "context": schema_in_context(dump) if dump else {},
    }
    out = f"/tmp/bow-agent/report_{args.label}.json"
    with open(out, "w") as f:
        json.dump(report, f, indent=2)
    print(json.dumps(report["score"], indent=2))
    print(json.dumps(report["context"], indent=2))
    print("wrote", out)


if __name__ == "__main__":
    main()
