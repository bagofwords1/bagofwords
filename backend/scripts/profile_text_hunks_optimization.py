"""Deterministic benchmark for the pending-review boolean hot path."""

from __future__ import annotations

import time

from app.services.text_hunks import (
    RebasedHunkCache,
    has_live_hunk_against_main,
    rebased_hunks_against_main,
)


def make_dataset(suggestions: int = 18, repeated_lines: int = 180):
    line = "- metric: revenue status: active owner: finance rule: always include source column\n"
    base = line * repeated_lines
    rows = []
    for index in range(suggestions):
        start = (index * 7 % (repeated_lines - 12)) * len(line)
        end = start + (3 + index % 7) * len(line)
        replacement = f"- metric: revenue_{index} status: reviewed owner: finance\n"
        proposed = base[:start] + replacement + base[end:]
        main = base if index % 3 else base + "- final policy: preserve audited totals\n"
        rows.append((base, proposed, main))
    return rows


def main():
    rows = make_dataset()

    started = time.perf_counter()
    baseline = [bool(rebased_hunks_against_main(*row)) for row in rows]
    baseline_ms = (time.perf_counter() - started) * 1000

    cache = RebasedHunkCache()
    started = time.perf_counter()
    optimized = [has_live_hunk_against_main(*row, cache=cache) for row in rows]
    optimized_ms = (time.perf_counter() - started) * 1000

    assert optimized == baseline
    print({
        "suggestions": len(rows),
        "characters_per_base": len(rows[0][0]),
        "baseline_ms": round(baseline_ms, 1),
        "optimized_ms": round(optimized_ms, 1),
        "speedup": round(baseline_ms / optimized_ms, 1),
        "outputs_equal": True,
        "intent_cache_entries": len(cache.intents),
        "alignment_cache_entries": len(cache.alignments),
    })


if __name__ == "__main__":
    main()
