"""Canonical fingerprint of the whole instruction/build/version state.

Used to prove a refactor of the write paths loses nothing. Generated UUIDs and
timestamps differ between runs, so the fingerprint is keyed on *stable*
identity (instruction id, build_number) and compares CONTENT by sha, never by
generated id. Two runs of the same operations from the same seed must produce
byte-identical fingerprints.

Captures:
  main_snapshot   what every read resolves against: instruction -> live text/title/status
  instructions    the live rows themselves
  versions        every version's text, per instruction (nothing dropped or invented)
  builds          per build_number: status/source/is_main/counts/rejected-hunk keys
  build_contents  per build_number: rows, is_change count, (instruction, version-text) pairs
  associations    instruction -> data sources

Usage:
  python scripts/fingerprint_instruction_state.py <out.json>
"""
import os
import sys
import json
import hashlib
import sqlite3


def sha(s) -> str:
    if s is None:
        return "-"
    return hashlib.sha256(str(s).encode()).hexdigest()[:16]


def main():
    out_path = sys.argv[1] if len(sys.argv) > 1 else "fingerprint.json"
    db_path = sys.argv[2] if len(sys.argv) > 2 else "db/app.db"
    c = sqlite3.connect(db_path)
    q = lambda s, *a: c.execute(s, a).fetchall()

    fp = {}

    # ---- the live snapshot every read resolves against ----
    fp["main_snapshot"] = sorted(
        [iid, sha(text), sha(title), status]
        for iid, text, title, status in q("""
            select bc.instruction_id, iv.text, iv.title, iv.status
              from build_contents bc
              join instruction_builds ib on ib.id = bc.build_id
              join instruction_versions iv on iv.id = bc.instruction_version_id
             where ib.is_main = 1 and ib.deleted_at is null
        """)
    )

    # ---- live instruction rows ----
    fp["instructions"] = sorted(
        [iid, sha(text), sha(title), status, category, load_mode,
         "deleted" if deleted_at else "live"]
        for iid, text, title, status, category, load_mode, deleted_at in q("""
            select id, text, title, status, category, load_mode, deleted_at
              from instructions
        """)
    )

    # ---- every version (nothing dropped, nothing invented) ----
    fp["versions"] = sorted(
        [iid, str(vnum), sha(text), status]
        for iid, vnum, text, status in q("""
            select instruction_id, version_number, text, status
              from instruction_versions
        """)
    )

    # ---- builds, keyed on build_number (stable across runs) ----
    fp["builds"] = sorted(
        [str(bn), status, source, str(is_main), str(total), str(added),
         str(modified), str(removed), sha(rejected)]
        for bn, status, source, is_main, total, added, modified, removed, rejected in q("""
            select build_number, status, source, is_main, total_instructions,
                   added_count, modified_count, removed_count,
                   coalesce(rejected_hunks, '')
              from instruction_builds
             where deleted_at is null
        """)
    )

    # ---- build contents, per build_number, content-addressed ----
    rows = q("""
        select ib.build_number, bc.instruction_id, iv.text, bc.is_change
          from build_contents bc
          join instruction_builds ib on ib.id = bc.build_id
          join instruction_versions iv on iv.id = bc.instruction_version_id
         where ib.deleted_at is null
    """)
    per_build = {}
    for bn, iid, text, is_change in rows:
        e = per_build.setdefault(bn, {"n": 0, "n_change": 0, "pairs": []})
        e["n"] += 1
        e["n_change"] += 1 if is_change else 0
        e["pairs"].append(f"{iid}:{sha(text)}:{1 if is_change else 0}")
    fp["build_contents"] = {
        str(bn): {"n": e["n"], "n_change": e["n_change"],
                  "digest": sha("|".join(sorted(e["pairs"])))}
        for bn, e in sorted(per_build.items())
    }

    # ---- instruction -> data sources ----
    fp["associations"] = sorted(
        [iid, dsid] for iid, dsid in
        q("select instruction_id, data_source_id from instruction_data_source_association")
    )

    fp["_totals"] = {
        "instructions_live": q("select count(*) from instructions where deleted_at is null")[0][0],
        "instructions_all": q("select count(*) from instructions")[0][0],
        "versions": q("select count(*) from instruction_versions")[0][0],
        "builds": q("select count(*) from instruction_builds where deleted_at is null")[0][0],
        "build_contents": q("select count(*) from build_contents")[0][0],
        "main_builds": q("select count(*) from instruction_builds where is_main=1 and deleted_at is null")[0][0],
        "main_rows": len(fp["main_snapshot"]),
    }

    with open(out_path, "w") as f:
        json.dump(fp, f, indent=0, sort_keys=True)
    print(f"wrote {out_path}")
    for k, v in fp["_totals"].items():
        print(f"  {k:20} {v}")
    c.close()


if __name__ == "__main__":
    main()
