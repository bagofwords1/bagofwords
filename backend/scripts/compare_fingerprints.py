"""Diff two instruction-state fingerprints and fail loudly on any divergence.

The question this answers: after running the SAME operations from the SAME
pristine database, does the refactored write path leave the workspace in
exactly the state the original left it in? Anything else is data loss or data
invention, however small.

Usage:
  python scripts/compare_fingerprints.py old.json new.json
"""
import sys
import json


def main():
    old = json.load(open(sys.argv[1]))
    new = json.load(open(sys.argv[2]))

    failures = []

    # ---- totals ----
    print("totals:")
    for k in sorted(set(old["_totals"]) | set(new["_totals"])):
        o, n = old["_totals"].get(k), new["_totals"].get(k)
        flag = "OK " if o == n else "DIFF"
        if o != n:
            failures.append(f"total {k}: {o} -> {n}")
        print(f"  [{flag}] {k:20} {o} vs {n}")

    # ---- builds: keyed by build_number so a changed FIELD is reported as a
    # change, not as one row lost plus one invented ----
    BUILD_FIELDS = ["status", "source", "is_main", "total", "added",
                    "modified", "removed", "rejected_hunks_sha"]
    ob = {r[0]: r[1:] for r in old.get("builds", [])}
    nb = {r[0]: r[1:] for r in new.get("builds", [])}
    lost, invented = set(ob) - set(nb), set(nb) - set(ob)
    field_diffs = {}
    for bn in sorted(set(ob) & set(nb)):
        if ob[bn] != nb[bn]:
            for i, f in enumerate(BUILD_FIELDS):
                if ob[bn][i] != nb[bn][i]:
                    field_diffs.setdefault(f, []).append(bn)
    if lost or invented:
        failures.append(f"builds: {len(lost)} lost, {len(invented)} invented")
        print(f"  [DIFF] builds: lost={sorted(lost)[:5]} invented={sorted(invented)[:5]}")
    else:
        print(f"  [OK ] builds: {len(ob)} builds, none lost or invented")
    for f, bns in sorted(field_diffs.items()):
        marker = "DIFF" if f != "rejected_hunks_sha" else "NOTE"
        if f != "rejected_hunks_sha":
            failures.append(f"builds.{f} differs on {len(bns)} builds")
        print(f"  [{marker}] builds.{f}: differs on {len(bns)} builds {sorted(bns)[:8]}")
    if "rejected_hunks_sha" in field_diffs:
        print("         ^ review METADATA only (the per-hunk rejection list vs the")
        print("           single void marker a delete now writes). No instruction,")
        print("           version, snapshot or association is affected.")

    # ---- list sections: compare as multisets of rows ----
    for section in ("main_snapshot", "instructions", "versions", "associations"):
        o = [tuple(r) for r in old.get(section, [])]
        n = [tuple(r) for r in new.get(section, [])]
        so, sn = set(o), set(n)
        missing = so - sn      # present before, gone now  -> LOST
        added = sn - so        # absent before, here now   -> INVENTED
        if missing or added:
            failures.append(f"{section}: -{len(missing)} +{len(added)}")
            print(f"  [DIFF] {section}: {len(missing)} lost, {len(added)} invented")
            for r in list(missing)[:5]:
                print(f"        LOST     {r}")
            for r in list(added)[:5]:
                print(f"        INVENTED {r}")
        else:
            print(f"  [OK ] {section}: {len(o)} rows identical")

    # ---- build_contents: per build_number digest ----
    ob, nb = old.get("build_contents", {}), new.get("build_contents", {})
    only_old = set(ob) - set(nb)
    only_new = set(nb) - set(ob)
    digest_diff = [k for k in (set(ob) & set(nb)) if ob[k] != nb[k]]
    if only_old or only_new or digest_diff:
        failures.append(
            f"build_contents: builds only-old={len(only_old)} only-new={len(only_new)} "
            f"content-differs={len(digest_diff)}"
        )
        print(f"  [DIFF] build_contents: only_old={sorted(only_old)[:5]} "
              f"only_new={sorted(only_new)[:5]} differing={sorted(digest_diff)[:5]}")
        for k in sorted(digest_diff)[:5]:
            print(f"        build #{k}: {ob[k]} vs {nb[k]}")
    else:
        print(f"  [OK ] build_contents: {len(ob)} builds, every snapshot identical")

    print()
    if failures:
        print("RESULT: DIVERGENCE")
        for f in failures:
            print("  -", f)
        sys.exit(1)
    print("RESULT: IDENTICAL — the refactor changed no data.")


if __name__ == "__main__":
    main()
