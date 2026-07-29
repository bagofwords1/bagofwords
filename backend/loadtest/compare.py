#!/usr/bin/env python3
"""
Before/after comparison, scoped to the instruction-write path.

Reads two ramp output dirs (default: results/full = before, results/after =
after) and prints the metrics the instruction fixes are supposed to move:
cohort-B write latency + failures, and the instruction_builds lock query in
pg_stat_statements. Everything else is noise for this evaluation.

Usage: python loadtest/compare.py [before_dir] [after_dir]
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def load(d, L):
    p = os.path.join(d, f"results_L{L}.json")
    return json.load(open(p))["summary"] if os.path.exists(p) else None


def ms(v):
    return "—" if v is None else f"{v*1000:.0f}ms"


def lock_total(d, L):
    """Total ms of the instruction_builds FOR UPDATE query, from the level's
    query snapshot. Returns (total_ms, calls) or None."""
    p = os.path.join(d, f"queries_L{L}.txt")
    if not os.path.exists(p):
        return None
    for line in open(p):
        parts = line.split("|")
        if len(parts) >= 4 and "instruction_builds" in parts[-1] and "FOR UPDATE" in parts[-1]:
            try:
                return (float(parts[0]), int(parts[1]))
            except Exception:
                return None
    return None


def main():
    before = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "results", "full")
    after = sys.argv[2] if len(sys.argv) > 2 else os.path.join(HERE, "results", "after")
    levels = [30, 60]

    print(f"BEFORE: {before}")
    print(f"AFTER:  {after}\n")

    print("## Instruction writes (cohort B) — the target of the fixes\n")
    print("| level | metric | before | after |")
    print("|---|---|---|---|")
    for L in levels:
        b, a = load(before, L), load(after, L)
        if not b or not a:
            print(f"| L{L} | (missing) | {bool(b)} | {bool(a)} |")
            continue
        for label, key in [("create_instruction p95", "create_instruction"),
                           ("edit_instruction p95", "edit_instruction")]:
            bv = b["browse"]["per_op"].get(key, {})
            av = a["browse"]["per_op"].get(key, {})
            print(f"| L{L} | {label} | {ms(bv.get('p95'))} | {ms(av.get('p95'))} |")
        # write failures = create + edit failures
        def wf(s):
            po = s["browse"]["per_op"]
            return (po.get("create_instruction", {}).get("fail", 0)
                    + po.get("edit_instruction", {}).get("fail", 0))
        print(f"| L{L} | instruction-write failures | {wf(b)} | {wf(a)} |")
        print(f"| L{L} | all browse failures | {b['browse']['fail']} | {a['browse']['fail']} |")

    print("\n## instruction_builds FOR UPDATE lock (pg_stat_statements)\n")
    print("| level | before total | before calls | after total | after calls |")
    print("|---|---|---|---|---|")
    for L in levels:
        lb, la = lock_total(before, L), lock_total(after, L)
        bt = f"{lb[0]/1000:.1f}s" if lb else "—"
        bc = str(lb[1]) if lb else "—"
        at = f"{la[0]/1000:.1f}s" if la else "—"
        ac = str(la[1]) if la else "—"
        print(f"| L{L} | {bt} | {bc} | {at} | {ac} |")

    print("\n## Agent flow + server-side truth (should be unchanged or better)\n")
    print("| level | metric | before | after |")
    print("|---|---|---|---|")
    for L in levels:
        b, a = load(before, L), load(after, L)
        if not b or not a:
            continue
        for label, fn in [
            ("agent p50", lambda s: f"{s['agent']['t_total_p50']:.1f}s" if s['agent']['t_total_p50'] else "—"),
            ("agent p95", lambda s: f"{s['agent']['t_total_p95']:.1f}s" if s['agent']['t_total_p95'] else "—"),
            ("real success", lambda s: str((s.get('server_side') or {}).get('by_status', {}))),
        ]:
            print(f"| L{L} | {label} | {fn(b)} | {fn(a)} |")


if __name__ == "__main__":
    main()
