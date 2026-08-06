# Sandbox Feedback Loop — saving and deleting an instruction take forever

A customer with **many pending changes, instructions and builds** reports that
saving an instruction and deleting an instruction both take forever.

The instruction **read** paths have already been through two rounds of this
(`agents-instructions-perf.md`, `agents-instructions-carryover-perf.md`,
`agents-pending-reconciliation-perf.md`). This loop covers the **write** paths,
which were never profiled:

    PUT    /api/instructions/{id}   ->  InstructionService.update_instruction
    DELETE /api/instructions/{id}   ->  InstructionService.delete_instruction

There are two independent causes, plus one correctness hazard found on the way.

---

## Cause 1 — every write forks a full snapshot of main (save *and* delete)

Both writers end in the same build sequence:

```
get_or_create_draft_build(source='user')     # no draft exists -> create_build
  -> create_build(copy_from_main=True)
       -> _copy_build_contents(main -> new)  # one INSERT per instruction in the org
add_to_build / remove_from_build
_auto_finalize_build -> submit -> approve -> promote_build   # the draft BECOMES main
```

The last step is what makes this per-save rather than occasional. An org admin's
save auto-promotes the draft to main (`instruction_service.py:4729`), so the draft
is consumed — and the **next** save finds none to reuse and forks the whole
snapshot again. `_copy_build_contents` (`build_service.py:143`) inserts one
`build_contents` row for **every instruction in the organization**, on every save
and every delete, to record a one-row change.

Measured on local SQLite (zero network latency), one save + one delete:

| instructions | save | delete | `build_contents` rows added |
|---:|---:|---:|---:|
| 200 | 0.31 s | 0.18 s | **+399** |
| 1,000 | 0.45 s | 0.75 s | **+1,997** |
| 3,500 | 1.30 s | 1.86 s | **+6,995** |

Phase breakdown of a single save (`bench_save_breakdown.py`):

| | 1,000 instructions | 3,500 instructions |
|---|---:|---:|
| `update_instruction` total | 0.542 s | 1.384 s |
| ↳ `get_or_create_draft_build` | 0.265 s (49%) | 0.961 s (69%) |
| ↳ ↳ `_copy_build_contents` | **0.218 s (40%)** | **0.843 s (61%)** |
| ↳ `add_to_build` | 0.032 s | 0.063 s |
| ↳ `submit` + `approve` + `promote` | 0.084 s | 0.183 s |

The snapshot copy is the majority of a save and grows linearly with the org's
instruction count. Everything else in the save is flat.

Two knock-on effects:

- **`build_contents` grows by N rows per write, forever, and nothing prunes it.**
  This is the corpus `agents-instructions-carryover-perf.md` measured at 1.04 M
  rows. That loop fixed the *read* by adding `is_change` so the sweep stops
  scanning carry-over rows — but the write amplification that manufactures those
  rows is still there, so the heap every other query lives on keeps growing.
- **Agent admins pay a second O(N) pass.** For a non-org-admin,
  `_can_auto_publish_build` (`instruction_service.py:3455`) selects every
  instruction id in the build — i.e. the entire snapshot — then loads all of their
  data-source associations and checks a permission per data source. Org admins
  short-circuit on line 3447 and never see this; agent admins pay it on every save.

## Cause 2 — delete runs the quadratic rebase inline on the event loop

On top of the snapshot fork, `delete_instruction` calls `_void_pending_suggestions`
(`instruction_service.py:2430`), which walks every live suggestion build on the row
and runs `rebased_hunks_against_main` — `difflib.SequenceMatcher` over word tokens,
quadratic in text length — once per build.

Measured, 1,000-instruction workspace, one delete:

| suggestions on the row (3,500-char text) | delete |
|---:|---:|
| 1 | 0.49 s |
| 5 | 0.96 s |
| 15 | 2.61 s |
| 35 | **5.15 s** |

| text length (10 suggestions) | delete |
|---:|---:|
| 1,500 chars | 0.56 s |
| 3,500 chars | 1.71 s |
| 6,000 chars | 4.92 s |
| 8,000 chars | **6.70 s** |

Linear in suggestions, quadratic in text — 5.3× the characters costs 12× the time.
The customer profile in `agents-pending-reconciliation-perf.md` (p90 8,235 chars,
up to 35 suggestions on one instruction) sits exactly in the expensive corner.

**The important part is where it runs.** The reconciliation loop moved this work
off the event loop — but only in `review_hunks`, which is the one caller that
wraps it in `asyncio.to_thread` behind the `_HUNK_CPU` semaphore
(`instruction_service.py:1696`). Every *mutating* path still calls it inline:

| call site | line | offloaded? |
|---|---:|---|
| `review_hunks` | 1697 | yes — `to_thread` + `_HUNK_CPU` |
| `accept_hunk` | 2057 | **no** |
| `accept_all_hunks` | 2259 | **no** |
| `_void_pending_suggestions` (delete) | 2446 | **no** |
| `reject_all_hunks` | 2473 | **no** |
| `reject_hunk` | 2525 | **no** |

So a delete — or any accept/reject — blocks the worker's event loop for its whole
duration. Every other request on that worker stalls behind it. That is the
amplifier that turns "delete is slow" into "the whole page is slow", and it is the
same gap (23.5 s API vs 30.6 s page) the reconciliation loop identified and fixed
on the read side only.

Two smaller wins in the same loop: `_void_pending_suggestions` and
`reject_all_hunks` rebase each suggestion independently, without the shared
`RebasedHunkCache` that `review_hunks` was given. And on **delete** specifically
the per-hunk keys are computed only to be recorded as rejected — the instruction is
going away, so the exact hunk keys buy nothing.

## Hazard — a lingering `draft` build is adopted, then promoted wholesale to main

Found while benchmarking; not a performance issue, but it fires on this same path.

`get_or_create_draft_build` (`build_service.py:571`) looks up a reusable draft by
`(organization_id, status='draft', source='user')`, newest first. It is **not
scoped by user**, and it does not check what the build contains. `_auto_finalize_build`
then submits, approves and promotes whatever it returns straight to main.

So any `source='user'` build left sitting in `draft` is adopted by the next
person's save and published. A build in that state carries a snapshot of an
*older* main, so promoting it silently reverts every instruction changed since
that fork. Two ways a build stays `draft`:

- the `target_build_id` branch of `update_instruction`
  (`instruction_service.py:1123`) adds to an existing build and commits without
  ever calling `_auto_finalize_build`;
- a finalize that throws before `submit_build` — `_auto_finalize_build` swallows
  the exception, rolls back and returns `False` (line 4774).

In the sandbox this promoted a build to main holding **zero** content rows, which
emptied the org's live instruction set (`select count(*) from build_contents where
build_id in (select id from instruction_builds where is_main=1)` → 0). Worth
confirming against the customer's build table before assuming it hasn't happened.

---

## Reproducing

Python 3.12, backend on `:8000`, `sandbox@bow.dev` registered (same setup as
`agents-instructions-perf.md`).

```bash
cd backend
export BOW_DATABASE_URL="sqlite:///db/app.db" BOW_SMTP_PASSWORD=dummy ANTHROPIC_API_KEY=dummy
mkdir -p db && rm -f db/app.db && uv run alembic upgrade head
uv run python main.py &
curl -s -X POST http://localhost:8000/api/auth/register -H "Content-Type: application/json" \
  -d '{"email":"sandbox@bow.dev","password":"Sandbox123!","name":"Sandbox Admin"}'

# Cause 1 — cost of one save / one delete as the workspace grows
uv run python scripts/seed_instructions_pending.py 1000 0.0
uv run python scripts/bench_instruction_write.py       # wall time, SQL count, rows added
uv run python scripts/bench_save_breakdown.py          # per-phase share of one save
uv run python scripts/seed_instructions_pending.py 2500 0.0   # -> 3,500
uv run python scripts/bench_save_breakdown.py          # copy share climbs 40% -> 61%

# Cause 2 — delete against a row carrying many pending suggestions
uv run python scripts/bench_delete_with_pending.py 35 3500
uv run python scripts/bench_delete_with_pending.py 10 8000
```

All numbers above are **local SQLite**. Production Postgres pays network latency
per round trip and WAL for every one of those N inserts, so the same shape
stretches accordingly.

## Directions (none applied)

1. **Stop snapshotting on every write.** `build_contents.is_change` already marks
   the handful of rows a build actually changes, and the read paths already filter
   on it. Either make a build store only its changes and resolve carry-over via
   `base_build_id` at read time, or keep the snapshot but have `promote_build`
   *apply* the build's `is_change` rows onto the previous main instead of the new
   build having to carry a full copy. Either removes the O(N) insert from the
   write path and stops `build_contents` growing as (builds × instructions).
2. **Offload the diff in the mutation paths too** — reuse `_HUNK_CPU` +
   `asyncio.to_thread` at lines 2057, 2259, 2446, 2473, 2525. This alone stops one
   delete from stalling every other request on the worker.
3. **Skip the rebase entirely on delete.** `_void_pending_suggestions` needs the
   instruction to stop counting as pending; settling the suggestion rows (or
   soft-deleting their content rows for this instruction) achieves that without
   computing per-hunk keys for a row that is being removed.
4. **Share one `RebasedHunkCache`** across `_void_pending_suggestions` and
   `reject_all_hunks`, as `review_hunks` already does.
5. **Prune obsolete draft builds.** Called out as the real remedy in
   `agents-pending-reconciliation-perf.md` and still unaddressed.
6. **Scope `get_or_create_draft_build` by user**, and do not auto-promote a build
   the current request did not create.

## Artifacts

- `backend/scripts/bench_instruction_write.py` — save/delete wall time, SQL count,
  and `build_contents` growth at the current workspace size.
- `backend/scripts/bench_save_breakdown.py` — per-phase share of one save.
- `backend/scripts/bench_delete_with_pending.py` — delete cost vs. suggestion
  count and text length.
