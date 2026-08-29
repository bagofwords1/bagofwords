"""Architecture-neutral scorer for the artifact-iteration evals.

Usage (from backend/, against the sandbox sqlite DB):

    uv run python tests/evals/artifact_iteration/scorer.py <db_path> <report_id> <scenario_id>

Scores a finished scenario run purely from persisted state — works identically
for the coder-based (legacy) and planner-authored (mechanical) pipelines:

- integrity: head artifact completed; every viz reference in the code resolves
  (vizById uuid in payload / positional index in range) and every payload viz
  is referenced (map/forEach counts as coverage); no fatal render_errors.
- retention: required substrings survive to the head version; forbidden ones
  are gone (memory of earlier decisions across later edits).
- structure: KPI section ordering when the scenario demands it.
- filters: expected server-side parameter names declared on the report's
  queries (substring match on param names). The interactive filter check
  (select an option, assert the KPI changes) runs in the Playwright harness,
  not here — record its result via --filter-interaction pass|fail.
- effort: planner iterations (completions), tool calls, failed tool calls,
  wall time from first user message to last activity.

Prints a JSON result; exit code 0 always (scores, doesn't gate CI).
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys

VIZ_BY_ID_RE = re.compile(r"vizById\(\s*['\"]([0-9a-fA-F-]{10,40})['\"]\s*\)")
VIZ_POS_RE = re.compile(r"\bviz\[(\d+)\]")
ITER_RE = re.compile(r"\b(?:viz|visualizations)\s*\.\s*(?:map|forEach)\b")


def score(db_path: str, report_id: str, scenario_id: str, filter_interaction: str | None) -> dict:
    scenarios = json.load(open(__file__.replace("scorer.py", "scenarios.json")))
    scenario = next(s for s in scenarios["scenarios"] if s["id"] == scenario_id)
    checks = scenario.get("checks", {})
    con = sqlite3.connect(db_path)
    out: dict = {"scenario": scenario_id, "report_id": report_id, "passes": {}, "failures": []}

    def _check(name: str, ok: bool, detail: str = ""):
        out["passes"][name] = bool(ok)
        if not ok:
            out["failures"].append(f"{name}: {detail}")

    art = con.execute(
        "select id, title, status, version, json_extract(content,'$.code'),"
        " json_extract(content,'$.visualization_ids'), render_errors"
        " from artifacts where report_id=? and mode='page' order by created_at desc limit 1",
        (report_id,),
    ).fetchone()
    if not art:
        _check("artifact_exists", False, "no page artifact")
        out["score"] = 0.0
        return out
    aid, title, status, version, code, viz_ids_json, render_errors = art
    code = code or ""
    viz_ids = [str(v) for v in json.loads(viz_ids_json or "[]")]
    _check("artifact_completed", status == "completed", f"status={status}")

    fatal = [e for e in json.loads(render_errors or "[]") if not str(e).startswith("[console")]
    _check("no_fatal_render_errors", not fatal, str(fatal[:2]))

    # Reference integrity (both binding styles)
    id_refs = set(VIZ_BY_ID_RE.findall(code))
    pos_refs = {int(m) for m in VIZ_POS_RE.findall(code)}
    unknown = sorted(id_refs - set(viz_ids))
    out_of_range = sorted(i for i in pos_refs if i >= len(viz_ids))
    _check("no_unknown_viz_refs", not unknown, f"unknown ids {unknown[:3]}")
    _check("no_out_of_range_refs", not out_of_range, f"indices {out_of_range[:3]}")
    referenced = set(id_refs & set(viz_ids)) | {viz_ids[i] for i in pos_refs if i < len(viz_ids)}
    covered = bool(ITER_RE.search(code)) or (set(viz_ids) <= referenced if viz_ids else True)
    _check("all_payload_vizs_rendered", covered,
           f"unreferenced {sorted(set(viz_ids) - referenced)[:3]}")
    _check("min_visualizations", len(viz_ids) >= checks.get("min_visualizations", 1),
           f"{len(viz_ids)} vizs")

    # Retention / absence
    for pat in checks.get("retention_all", []):
        ok = any(alt.lower() in code.lower() or alt.lower() in (title or "").lower()
                 for alt in pat.split("|"))
        _check(f"retained:{pat.split('|')[0]}", ok, "not found in head code/title")
    for pat in checks.get("absent_all", []):
        _check(f"absent:{pat}", pat.lower() not in code.lower(), "still present")
    for pat in checks.get("expected_sections", []):
        ok = any(alt.lower() in code.lower() for alt in pat.split("|"))
        _check(f"section:{pat.split('|')[0]}", ok, "missing")

    if checks.get("kpi_first_section"):
        kpi_pos = min([code.find("KPICard"), code.lower().find("kpi")] +
                      [10**9]) if ("KPICard" in code or "kpi" in code.lower()) else -1
        chart_pos = code.find("EChart")
        _check("kpi_row_first", 0 <= kpi_pos < (chart_pos if chart_pos >= 0 else 10**9),
               f"kpi@{kpi_pos} chart@{chart_pos}")

    # Server-side filter declarations across the report's queries
    param_names: set[str] = set()
    for (pjson,) in con.execute("select parameters from queries where report_id=?", (report_id,)):
        for p in json.loads(pjson or "[]"):
            if (p.get("source") or "input") != "identity":
                param_names.add(str(p.get("name", "")).lower())
    for want in checks.get("filter_params_expected", []):
        _check(f"server_param:{want}", any(want in n for n in param_names),
               f"declared={sorted(param_names)}")

    if filter_interaction is not None:
        _check("filter_interaction", filter_interaction == "pass", filter_interaction)

    # Effort metrics over the run
    row = con.execute(
        "select count(*), min(created_at), max(updated_at) from completions where report_id=?",
        (report_id,),
    ).fetchone()
    out["metrics"] = {"completions": row[0], "started": row[1], "ended": row[2]}
    tools = con.execute(
        "select te.status, count(*) from tool_executions te"
        " join agent_executions ae on te.agent_execution_id=ae.id"
        " where ae.report_id=? group by te.status", (report_id,),
    ).fetchall()
    out["metrics"]["tool_calls"] = {s: n for s, n in tools}
    out["metrics"]["artifact_versions"] = con.execute(
        "select count(*) from artifacts where report_id=?", (report_id,)).fetchone()[0]
    out["metrics"]["code_chars"] = len(code)

    total = len(out["passes"])
    out["score"] = round(sum(out["passes"].values()) / total, 3) if total else 0.0
    return out


if __name__ == "__main__":
    db_path, report_id, scenario_id = sys.argv[1:4]
    fi = None
    for a in sys.argv[4:]:
        if a.startswith("--filter-interaction="):
            fi = a.split("=", 1)[1]
    print(json.dumps(score(db_path, report_id, scenario_id, fi), indent=1))
