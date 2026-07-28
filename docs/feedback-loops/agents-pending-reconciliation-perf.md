# Sandbox Feedback Loop — the /agents page waits on pending-change reconciliation

A customer profile made the real cost visible for the first time. Their numbers:

| | value |
|---|---:|
| instructions in the database | 139 |
| "All instructions" badge | **220 — inflated** |
| instruction builds / still open | 320 / **169** |
| candidate changed rows / needing expensive reconciliation | 180 / **76** |
| instruction text: median / mean / p90 / max (chars) | 3,568 / 4,382 / 8,235 / **15,242** |
| suggestions on a single instruction | **35** |
| PostgreSQL candidate query | **7.4 ms** |
| connections API | 0.22–0.26 s |
| instruction-list API | **19.8 s** |
| instruction-counts API | **23.5 s** |
| browser: click → instructions appear | **30.6 s** |

The database was answering in 7 milliseconds. Everything else was Python.

## Root cause

Both endpoints run `get_pending_change_instruction_ids`, which asks per suggestion
"does this still have a live hunk against main?" (`has_live_hunk_against_main`).
That short-circuits on equality — but when **main has moved since the suggestion
forked**, it must rebase the suggestion's intent onto the current text, which is
`difflib.SequenceMatcher(autojunk=False)` over word tokens. Measured at their
text sizes:

| chars | ms per row |
|---:|---:|
| 3,568 (their median) | 105 |
| 4,382 (their mean) | 220 |
| 8,235 (their p90) | 694 |
| 15,242 (their max) | **3,423** |

Quadratic: 4.3× the characters costs 24× the time. 76 drifted rows × ~260 ms ≈ 20 s,
which is their instruction-list number; counts pays the same and the page fires
both at once on one worker, which is their 30.6 s.

Separately, `total = live_total + not_live` **summed two overlapping populations**:
an unpublished proposal is folded into the live surfaces as pending *and* counted
as not-live. 139 + 81 = the 220 they saw.

## Reproducing it

`scripts/seed_acme_shape.py` builds the shape, including the mechanism that makes
rows expensive — drafts fork from main, then **main is promoted forward**, exactly
how a workspace gets there when people keep publishing while suggestions sit
unreviewed.

```bash
cd backend
export BOW_DATABASE_URL="sqlite:///db/acme.db" BOW_SMTP_PASSWORD=dummy ANTHROPIC_API_KEY=dummy
uv run alembic upgrade head && uv run python main.py &      # register sandbox@bow.dev
uv run python scripts/seed_acme_shape.py acme               # 1x — their profile
uv run python scripts/seed_acme_shape.py custom --instructions 3500 --open-builds 800 \
    --expensive 2900 --max-suggestions 35 --agents 8 --not-in-main 580   # 50x
```

The 1× preset reproduces their profile closely: the connections API lands at
0.245 s against their measured 0.22–0.26 s, and the badge reports **220 for 139
instructions** — the same inflation, from the same cause.

## Fix

1. **Two tiers in the pending check** (`get_pending_change_instruction_ids(verify=…)`).
   The tree badges and the instruction list pass `verify=False`: equality only,
   never a diff. Conclusive cases stay exact — a suggestion proposing what main
   already has is not pending; one whose base still matches main is. Only the
   drifted remainder is reported optimistically, and it resolves the moment the
   instruction is opened. Rows carrying **rejected hunks keep the exact check**,
   because "I rejected this, stop showing it" cannot be answered by equality.
2. **A cap on the diff** (`text_hunks.MAX_DIFF_TOKENS = 2000`). Past ~8k characters
   the comparison degrades to one whole-text hunk (`oversized: true`) instead of
   blocking a request for seconds. Below the cap nothing changes — deliberately
   *not* a faster matcher: trimming or swapping algorithms moves hunk keys
   (measured: 147 of 400 randomised edits produced different keys), and
   `rejected_hunks` stores those keys, so it would resurface rejected hunks.
3. **Off the event loop.** `review_hunks` runs its whole rebase batch in a worker
   thread behind a semaphore, so one heavy instruction can't starve the worker —
   the gap between their 23.5 s API and 30.6 s page.
4. **One shared cache per batch.** `review_hunks` was rebasing every suggestion
   independently even though they share a base and main; the batch cache it
   already had was never passed in.
5. **The badge counts a union**, not a sum.

## Measured

Endpoint wall time, same database, HEAD vs this branch (SQLite, warm):

| | 1× (139 instr, 58 drifted) | | 50× (3,500 instr, 2,900 drifted) | |
|---|---:|---:|---:|---:|
| | before | after | before | after |
| `GET /instructions/counts` | 13.1 s | **0.08 s** | **229.5 s** | **0.35 s** |
| `GET /instructions?data_source_ids=…` | 4.8 s | **0.09 s** | 30.8 s | **0.18 s** |
| "All instructions" badge | 220 (139 real) | **139** | 4,080 (3,500 real) | **3,500** |

At 50× the counts endpoint took **3 minutes 49 seconds** before; it now answers in
a third of a second — 655×, and it no longer scales with the drifted set at all.

Opening an instruction (the authoritative path) stays exact:

| | before | after |
|---|---:|---:|
| typical instruction (1 suggestion) | 0.04 s | **0.04 s** |
| the pathological one (46 suggestions) | 15.5 s | **7.1 s** |

## Known limits

- An instruction that has accumulated dozens of suggestions still costs seconds to
  open. It is bounded, it is off the event loop, and archiving obsolete pending
  builds is the real remedy — 169 open builds is itself the anomaly.
- A drifted suggestion whose change was already applied elsewhere shows a dot until
  opened, then clears itself (`KnowledgeExplorer.loadPending`). That is the
  deliberate trade for never diffing on a page load.
- Instructions past ~8k characters are reviewed as one whole-text change rather
  than per phrase.

## Artifacts

- `backend/scripts/seed_acme_shape.py` — the shape, at any scale.
- `tests/e2e/rbac/test_instruction_pending_carryover.py` — badge-counts-once and
  rejected-suggestion-stops-being-pending regressions.
