"""Standalone worker: decode Power BI (.pbix) model tables to Parquet.

Run as a *script* (not imported) by pbix_common.materialize_pbix_parquets_isolated:

    python pbix_extract_worker.py <pbix_path> <cache_dir> <max_rows> <memory_mb> <exclude_json>

Deliberately imports nothing from `app.*` so it starts without settings/DB.
Isolation contract with the parent:
  - RLIMIT_AS is set to <memory_mb>, so a runaway Vertipaq decode raises
    MemoryError inside the child (caught per table) instead of letting the
    OOM killer take down the API server.
  - Progress is journaled: manifest.partial.json accumulates completed tables,
    state.json names the table currently decoding. If the child is killed
    anyway, the parent excludes state.json's table and re-runs; already
    finished parquets are reused.
  - On success the partial manifest is atomically renamed to manifest.json.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

AUTO_DATE_TABLE_RE = re.compile(r"^(LocalDateTable|DateTableTemplate)_[0-9a-fA-F\-]+$")
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9_]+")


def _safe_name(name: str) -> str:
    cleaned = _SAFE_NAME_RE.sub("_", name).strip("_") or "tbl"
    if cleaned[0].isdigit():
        cleaned = f"t_{cleaned}"
    return cleaned


def main(argv: list[str]) -> int:
    pbix_path, cache_dir_s, max_rows_s, memory_mb_s, exclude_json = argv
    cache_dir = Path(cache_dir_s)
    max_rows = int(max_rows_s)
    memory_mb = int(memory_mb_s)
    excluded = set(json.loads(exclude_json))

    if memory_mb > 0:
        try:
            import resource
            limit = memory_mb * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
        except Exception as e:  # pragma: no cover - platform-dependent
            print(f"warning: could not set memory limit: {e}", file=sys.stderr)

    from pbixray import PBIXRay  # noqa: PLC0415 - after rlimit on purpose

    cache_dir.mkdir(parents=True, exist_ok=True)
    partial_path = cache_dir / "manifest.partial.json"
    state_path = cache_dir / "state.json"

    manifest: dict[str, str] = {}
    if partial_path.exists():
        try:
            manifest = json.loads(partial_path.read_text())
        except Exception:
            manifest = {}
    # Only trust entries whose parquet actually exists.
    manifest = {t: f for t, f in manifest.items() if (cache_dir / f).exists()}

    model = PBIXRay(pbix_path)
    schema_df = model.schema
    if schema_df is None or schema_df.empty:
        print(f"PBIX '{pbix_path}' has no tables in its semantic model.", file=sys.stderr)
        return 2

    candidates: list[str] = []
    seen: set[str] = set()
    for _, row in schema_df.iterrows():
        tname = str(row.get("TableName") or "")
        if not tname or tname in seen or AUTO_DATE_TABLE_RE.match(tname):
            continue
        seen.add(tname)
        candidates.append(tname)

    used_filenames = set(manifest.values())
    for tname in candidates:
        if tname in manifest or tname in excluded:
            continue
        # Journal the in-flight table BEFORE decoding so a hard kill tells the
        # parent exactly which table to exclude on the next attempt.
        state_path.write_text(json.dumps({"current": tname}))
        try:
            df = model.get_table(tname)
            if df is None:
                continue
            if len(df) > max_rows:
                print(f"skip {tname}: {len(df)} rows > {max_rows}", file=sys.stderr)
                continue
            base = _safe_name(tname).lower()
            fname = f"{base}.parquet"
            i = 1
            while fname in used_filenames:
                i += 1
                fname = f"{base}_{i}.parquet"
            df.to_parquet(cache_dir / fname, index=False)
            used_filenames.add(fname)
            manifest[tname] = fname
            partial_path.write_text(json.dumps(manifest))
        except (Exception, MemoryError) as e:
            print(f"skip {tname}: {type(e).__name__}: {e}", file=sys.stderr)
            continue

    state_path.write_text(json.dumps({"current": None}))
    if not manifest:
        print(f"PBIX '{pbix_path}' produced no materializable tables.", file=sys.stderr)
        return 3

    partial_path.write_text(json.dumps(manifest))
    partial_path.rename(cache_dir / "manifest.json")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
