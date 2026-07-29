#!/usr/bin/env python3
"""
Turn a ramp's raw output into the tables the report needs.

Joins four independent sources per level so a claim can be cross-checked
rather than taken from one instrument:
  results_L*.json  — client-visible reliability + latency (harness)
  phase_L*.jsonl   — where the time went inside the server (phase_trace)
  metrics_L*.csv   — CPU / memory / Postgres connection state (sampler)
  mock_L*.jsonl    — LLM calls per completion (proves work was constant)

Usage: python analyze.py results/mock
"""
import csv
import json
import os
import statistics
import sys
from collections import defaultdict


def pct(xs, p):
    if not xs:
        return None
    xs = sorted(xs)
    k = max(0, min(len(xs) - 1, int(round((p / 100.0) * (len(xs) - 1)))))
    return xs[k]


def load_phases(path):
    """-> {cid: {phase: record}}"""
    if not os.path.exists(path):
        return {}
    by = defaultdict(dict)
    with open(path) as f:
        for line in f:
            try:
                r = json.loads(line)
            except Exception:
                continue
            by[r["cid"]][r["phase"]] = r
    return by


def phase_breakdown(by):
    """Split each completion into queue wait / pool+setup / agent execution."""
    sem, pool, exec_, total = [], [], [], []
    for cid, ph in by.items():
        s = ph.get("sem_acquired", {}).get("dt_ms")
        o = ph.get("objects_refetched", {}).get("dt_ms")
        e = ph.get("agent_exec_end", {}).get("extra", {}).get("span_ms")
        t = ph.get("released", {}).get("dt_ms")
        if s is not None:
            sem.append(s)
        if s is not None and o is not None:
            pool.append(max(0.0, o - s))
        if e is not None:
            exec_.append(e)
        if t is not None:
            total.append(t)
    def st(a):
        return {"n": len(a), "p50": pct(a, 50), "p95": pct(a, 95),
                "max": max(a) if a else None}
    return {"sem_wait_ms": st(sem), "pool_setup_ms": st(pool),
            "agent_exec_ms": st(exec_), "total_ms": st(total)}


def load_metrics(path):
    """Handles both samplers: the original host-only CSV and the per-role one."""
    if not os.path.exists(path):
        return {}
    cols = defaultdict(list)
    with open(path) as f:
        for row in csv.DictReader(f):
            for k, v in row.items():
                try:
                    cols[k].append(float(v or 0))
                except Exception:
                    pass
    if not cols:
        return {}
    host = cols.get("host_cpu_pct") or cols.get("cpu_pct") or []
    out = {
        "host_cpu_p95": pct(host, 95), "host_cpu_max": max(host) if host else None,
        "mem_mb_max": max(cols.get("mem_used_mb", [0])),
        "pg_conn_max": max(cols.get("pg_total", [0])),
        "pg_active_max": max(cols.get("pg_active", [0])),
        "pg_idle_in_txn_max": max(cols.get("pg_idle_in_txn", [0])),
        "samples": len(host),
    }
    # Only the per-role sampler has these; the first-pass CSVs do not.
    if "backend_cpu" in cols:
        # The sampler converts jiffies to a percentage assuming a 1s tick, but
        # its loop also runs a psql query that slows down under load, so the
        # real interval stretches past 1s and the raw figure over-reads (it
        # produced >400% on a 4-vCPU box, which is impossible). Rescale by the
        # actual wall time between samples, which the ts column records.
        ts = cols.get("ts", [])
        def rescale(key):
            vals = cols.get(key, [])
            if len(ts) != len(vals) or len(ts) < 2:
                return vals
            fixed = []
            for i in range(1, len(vals)):
                dt = ts[i] - ts[i - 1]
                fixed.append(vals[i] / dt if dt > 0.05 else vals[i])
            return fixed
        for k in ("backend_cpu", "mock_cpu", "harness_cpu", "pg_cpu", "front_cpu"):
            if k in cols:
                cols[k] = rescale(k)
        out.update({
            "backend_cpu_p95": pct(cols["backend_cpu"], 95),
            "backend_cpu_max": max(cols["backend_cpu"]),
            "backend_rss_max": max(cols.get("backend_rss_mb", [0])),
            "mock_cpu_max": max(cols.get("mock_cpu", [0])),
            "harness_cpu_max": max(cols.get("harness_cpu", [0])),
            "pg_cpu_max": max(cols.get("pg_cpu", [0])),
            "pg_lock_waits_max": max(cols.get("pg_lock_waits", [0])),
        })
    return out


def load_ui(path):
    if not os.path.exists(path):
        return {}
    try:
        return json.load(open(path))["summary"]
    except Exception:
        return {}


def load_mock(path):
    if not os.path.exists(path):
        return {}
    n = 0
    with open(path) as f:
        for _ in f:
            n += 1
    return {"llm_calls": n}


def main():
    d = sys.argv[1] if len(sys.argv) > 1 else "results/mock"
    levels = []
    for fn in sorted(os.listdir(d)):
        if fn.startswith("results_L") and fn.endswith(".json"):
            levels.append(int(fn[len("results_L"):-len(".json")]))
    levels.sort()

    # Quiet baseline for the interference ratio.
    base = None
    bp = os.path.join(d, "baseline.json")
    if os.path.exists(bp):
        with open(bp) as f:
            base = json.load(f)["summary"]

    rows = []
    for L in levels:
        with open(os.path.join(d, f"results_L{L}.json")) as f:
            r = json.load(f)
        s = r["summary"]
        ph = phase_breakdown(load_phases(os.path.join(d, f"phase_L{L}.jsonl")))
        me = load_metrics(os.path.join(d, f"metrics_L{L}.csv"))
        mo = load_mock(os.path.join(d, f"mock_L{L}.jsonl"))
        qp = 0
        qf = os.path.join(d, f"queuepool_L{L}.txt")
        if os.path.exists(qf):
            try:
                qp = int(open(qf).read().strip() or 0)
            except Exception:
                qp = 0
        # Cohort C's chat turns run through the same streaming path, so the
        # phase trace (and the mock's call log) cover more completions than
        # cohort A alone. Divide by what was actually traced, not by n_agent.
        n_ag = ph.get("total_ms", {}).get("n") or s["n_agent"]
        tc = 0
        tf = os.path.join(d, f"toomanyclients_L{L}.txt")
        if os.path.exists(tf):
            try:
                tc = int(open(tf).read().strip() or 0)
            except Exception:
                tc = 0
        ui = load_ui(os.path.join(d, f"ui_L{L}.json"))
        rows.append({"L": L, "s": s, "ph": ph, "me": me, "mo": mo, "qp": qp,
                     "tc": tc, "ui": ui,
                     "llm_per_completion": (mo.get("llm_calls", 0) / n_ag) if n_ag else None})

    def f(v, nd=1, scale=1.0):
        return "—" if v is None else f"{v*scale:.{nd}f}"

    print("\n## Reliability and latency (cohort A — agent flow)\n")
    print("| level | agents | browse | success | drop/err | p50 total | p95 total | p95 TTFE | wall |")
    print("|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        s, a = r["s"], r["s"]["agent"]
        bad = sum(v for k, v in a["outcomes"].items() if k != "finished")
        print(f"| L{r['L']} | {s['n_agent']} | {s['n_browse']} | {f(a['success_pct'])}% | {bad} "
              f"| {f(a['t_total_p50'])}s | {f(a['t_total_p95'])}s "
              f"| {f(a['t_first_event_p95'])}s | {f(s['wall_s'])}s |")

    print("\n## Client-visible success vs. server-side truth\n")
    print("| level | client says finished | db: success | db: error | db: in_progress | real success |")
    print("|---|---|---|---|---|---|")
    for r in rows:
        a, ss = r["s"]["agent"], (r["s"].get("server_side") or {})
        by = ss.get("by_status", {}) or {}
        n = a["n"]
        ok = by.get("success", 0)
        real = f"{100.0*ok/n:.1f}%" if n and by else "—"
        print(f"| L{r['L']} | {a['outcomes'].get('finished',0)}/{n} | {by.get('success','—')} "
              f"| {by.get('error','—')} | {by.get('in_progress','—')} | {real} |")

    print("\n## Where the time goes (server-side phase attribution)\n")
    print("| level | queue wait p50 | queue wait p95 | pool+setup p95 | agent exec p50 | agent exec p95 | LLM calls/completion |")
    print("|---|---|---|---|---|---|---|")
    for r in rows:
        p = r["ph"]
        if not p.get("total_ms", {}).get("n"):
            print(f"| L{r['L']} | — | — | — | — | — | — |")
            continue
        print(f"| L{r['L']} | {f(p['sem_wait_ms']['p50'],0)}ms | {f(p['sem_wait_ms']['p95'],0)}ms "
              f"| {f(p['pool_setup_ms']['p95'],0)}ms | {f(p['agent_exec_ms']['p50'],0,0.001)}s "
              f"| {f(p['agent_exec_ms']['p95'],0,0.001)}s | {f(r['llm_per_completion'],1)} |")

    print("\n## Interference on ordinary app use (cohort B — /agents + instructions)\n")
    if base:
        b = base["browse"]
        print(f"Quiet baseline (no agent traffic): p50 {f(b['p50'],0,1000)}ms  "
              f"p95 {f(b['p95'],0,1000)}ms  p99 {f(b['p99'],0,1000)}ms  "
              f"n={b['n']} fail={b['fail']}\n")
    print("| level | calls | fail | p50 | p95 | p99 | p95 vs baseline |")
    print("|---|---|---|---|---|---|---|")
    for r in rows:
        b = r["s"]["browse"]
        ratio = "—"
        if base and base["browse"]["p95"] and b["p95"]:
            ratio = f"{b['p95']/base['browse']['p95']:.1f}x"
        print(f"| L{r['L']} | {b['n']} | {b['fail']} | {f(b['p50'],0,1000)}ms "
              f"| {f(b['p95'],0,1000)}ms | {f(b['p99'],0,1000)}ms | {ratio} |")

    print("\n### Cohort B by operation (p95, ms)\n")
    ops = ["list_agents", "list_instructions", "create_instruction",
           "edit_instruction", "list_reports"]
    print("| level | " + " | ".join(ops) + " |")
    print("|---" * (len(ops) + 1) + "|")
    if base:
        bp_ = base["browse"]["per_op"]
        print("| baseline | " + " | ".join(
            f(bp_.get(o, {}).get("p95"), 0, 1000) for o in ops) + " |")
    for r in rows:
        po = r["s"]["browse"]["per_op"]
        print(f"| L{r['L']} | " + " | ".join(
            f(po.get(o, {}).get("p95"), 0, 1000) for o in ops) + " |")

    ui_base = load_ui(os.path.join(d, "ui_baseline.json"))
    if any(r["ui"] for r in rows) or ui_base:
        print("\n## Real browser users (cohort C — Playwright)\n")
        uops = ["login", "page_agents", "page_instructions", "chat_turn"]
        print("| level | " + " | ".join(f"{o} p95" for o in uops) + " | fails | \"network error\" seen |")
        print("|---" * (len(uops) + 3) + "|")
        def uirow(tag, u):
            if not u:
                return
            po = u.get("per_op", {})
            fails = sum(v.get("fail", 0) for v in po.values())
            print(f"| {tag} | " + " | ".join(
                (str(po.get(o, {}).get("p95")) + "ms") if po.get(o, {}).get("p95") is not None else "—"
                for o in uops) + f" | {fails} | {u.get('network_errors_seen','—')} |")
        uirow("baseline", ui_base)
        for r in rows:
            uirow(f"L{r['L']}", r["ui"])

    print("\n## Resources and pool\n")
    print("| level | backend CPU p95 | backend CPU max | backend RSS | host CPU max | PG conns max | idle-in-txn max | PG lock waits max | QueuePool timeouts | \"too many clients\" | client loop lag p95 |")
    print("|---|---|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        m, s = r["me"], r["s"]
        lag = s.get("client_loop_lag_ms", {})
        bc = m.get("backend_cpu_p95")
        print(f"| L{r['L']} | {f(bc)}% | {f(m.get('backend_cpu_max'))}% "
              f"| {f(m.get('backend_rss_max'),0)}MB | {f(m.get('host_cpu_max'))}% "
              f"| {f(m.get('pg_conn_max'),0)} | {f(m.get('pg_idle_in_txn_max'),0)} "
              f"| {f(m.get('pg_lock_waits_max'),0)} | {r['qp']} | {r['tc']} "
              f"| {f(lag.get('p95'))}ms |")
    print("\nBackend CPU is a per-core sum: 400% = all 4 vCPU saturated.")
    print("Host CPU includes the load harness, mock LLM, frontend and Postgres,")
    print("which are test scaffolding — only the backend column describes the app.")
    print("Use the p95 column: `max` is a single 1s sample and jitter in the")
    print("sampling interval can push one sample above the physical 400% ceiling.\n")


if __name__ == "__main__":
    main()
