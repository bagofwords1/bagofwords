# Feedback Loop - pending instruction diff monopolizes a CPU core

A production deployment spent about 43 seconds computing pending instruction
hunks for `/api/instructions/counts`. This loop isolates the CPU work, validates
which comparisons can be decided without a diff, and verifies a
semantics-preserving optimization.

## Root cause (validated)

The production service profile recorded 133 calls to
`rebased_hunks_against_main` during one counts request. The diff calls consumed
essentially the whole request:

- normal request wall time: 42.5-43.4 seconds;
- profiled run: 94.4 seconds due to profiler overhead;
- profiled diff total: 94.0 seconds;
- profiled p90 diff: 2.7 seconds; slowest single diff: 5.3 seconds;
- `difflib.find_longest_match`: 7,774 calls;
- dictionary lookups inside `difflib`: about 280 million.

The SQL work is not responsible: the equivalent production queries complete in
126-148 ms and every pending-sweep index is valid.

The hot loop was at
`backend/app/services/instruction_service.py:1626`, and the quadratic matching
is in `backend/app/services/text_hunks.py:91` and `:124`.

Production aggregate-only analysis found:

| Shape | Count |
|---|---:|
| Changed suggestion rows | 167 |
| Changed instruction IDs | 103 |
| Rows where `base == main`, proposal differs, no rejected hunks | 105 |
| Instruction IDs with at least one such conclusive row | 94 |
| Instruction groups that genuinely require a diff after prioritization | 9 |

For a suggestion with unchanged main, a changed proposal, and no rejected
hunks, pending is mathematically certain. Building full hunks cannot change the
boolean result. Likewise, `proposed == base` and `proposed == main` are certainly
not pending.

## Loop A - deterministic reproduction

Run the production-shaped repetitive-text benchmark:

```bash
cd backend
PYTHONPATH=. .venv/bin/python scripts/profile_text_hunks_optimization.py
```

Observed comparison (same process, same generated inputs):

```text
suggestions: 18
characters_per_base: 14940
baseline_ms: 7606.6
optimized_ms: 2157.4
speedup: 3.5
outputs_equal: true
```

The baseline always builds full hunks to answer a boolean question. The
optimized path uses equality fast paths and a request-local cache. The script
asserts every boolean result is identical before printing.

## Loop B - live confirmation

Authenticated read-only samples before deployment of this change:

```text
/api/instructions/counts: 42532 ms, 42732 ms, 43361 ms
controlled final sample: 42847 ms; app CPU about 101%
```

### Live hot-patch verification

On 2026-07-11, the two optimized service modules were copied into the running
`bow-app` container and the Compose `app` service was restarted. Python syntax
was validated inside the production image before restart, and the container
returned healthy with no post-restart errors or tracebacks.

Three authenticated after samples:

```text
/api/instructions/counts: 3403 ms, 3445 ms, 3444 ms
```

That is about 12.5x faster than the 42.5-43.4 second baseline. Browser
verification also confirmed that `/agents` renders its pending state and the
root page reached its main heading in 734 ms.

This production verification is a container hot-patch, not a durable image
release. A future image pull or container recreation will replace it; the code
must still ship through the normal build pipeline.

## The fix

`backend/app/services/text_hunks.py` now provides:

- `has_live_hunk_against_main`, which answers conclusive equality cases without
  calling `SequenceMatcher`;
- `RebasedHunkCache`, scoped to one request, which reuses identical intent and
  base-to-main alignment results;
- an identity alignment when `base == main`, avoiding the second
  `SequenceMatcher` even when rejected keys require full hunk generation.

`backend/app/services/instruction_service.py` sorts conclusive pending rows
first. Once one marks an instruction pending, older/stale suggestions for that
instruction are skipped. On the measured production shape, 94 of 103 changed
instruction groups can exit this way, leaving only nine groups for real diffing.

The diff algorithm and `autojunk=False` remain unchanged for those nine groups.
This preserves hunk boundaries, stable rejection keys, stale-main behavior, and
accept/reject semantics.

## Verification

Pure hunk contract and cache equivalence:

```bash
cd backend
uv run --extra dev pytest tests/unit/test_text_hunks.py -q
# 7 passed
```

Counts, deleted-pending, and list/badge agreement:

```bash
uv run --extra dev pytest tests/e2e/test_instruction.py -q --db=sqlite \
  -k 'agent_count_matches_list_under_inaccessible_table or pending_badge_clears_when_instruction_deleted'
# 2 passed
```

Tracked-change semantics:

```bash
uv run --extra dev pytest tests/e2e/test_instruction.py -q --db=sqlite \
  -k 'pending_status_consistent or pending_builds_agrees or pending_builds_partial or accept_and_reject_all or pending_badge_excludes'
# 5 passed
```

## What this proves / regression notes

- The safe local optimization is 3.5x faster on deterministic repetitive text
  while producing identical pending decisions.
- The production data shape gives the fast path much better coverage than the
  synthetic benchmark: 94 of 103 changed instruction groups.
- The remaining nine groups still use the original diff semantics.
- The live hot-patch measured a 12.5x improvement on the affected deployment.
- A normal image release is required to make the production change durable.
- Cross-request caching/singleflight and lazy root mention loading remain the
  next steps if the deployed cold path is still above the sub-second target.
