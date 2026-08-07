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

---

# Fix plan

Four stages, ordered so each ships and is measurable on its own, and so the risky
one lands last. Stages 0–2 change no data model and no diff output.

## Invariants any fix must hold

Carried over from the earlier loops — these are the traps already paid for once:

| # | Invariant | Source |
|---|---|---|
| I1 | **Hunk keys must not move.** `rejected_hunks` stores them; 147 of 400 randomised edits produced different keys when the matcher was changed. Do not touch the diff algorithm, its tokenizer, or its inputs. | `agents-pending-reconciliation-perf.md` |
| I2 | A `__settled__` marker is bound to the exact `(main version, proposed version)` pair it was verified against; both sweep tiers treat a match as conclusive **not pending**. | `agents-pending-review-clearing.md` |
| I3 | `build_contents.is_change` is set at write time and is what the read paths filter on. **Anything writing `build_contents` outside `BuildService` must set it** or the row silently stops reading as pending. | `agents-instructions-carryover-perf.md` |
| I4 | `base_version_id` is stamped once, on the write that first makes a row a change — never overwritten, so a chained edit keeps the first baseline. | `build_service.py:270` |
| I5 | Badges/list use the non-diffing tier (`verify=False`); only opening an instruction is authoritative. Rows carrying rejected hunks keep the exact check. | `agents-pending-reconciliation-perf.md` |
| I6 | **Main must stay a complete snapshot** — `_main_text_of`, the context loaders, and `rollback_to_build`'s restore of soft-deleted instructions all read main's contents directly. | `build_service.py:1617`, `instruction_service.py:1461` |
| I7 | Deletion is currently expressed by **absence** from a snapshot (`diff_builds` computes `removed = ids_a - ids_b`). | `build_service.py:1381` |
| I8 | Each request holds its DB connection for its whole lifetime; the pool is 40/worker and the knee is exactly at the pool size. A slow write is a held connection. | `agents-page-contention.md` |

## Stage 0 — get the CPU work off the event loop, and out of delete entirely

The highest value per unit of risk. Nothing structural changes.

**0a. Offload the diff at the five mutating call sites** — `accept_hunk:2057`,
`accept_all_hunks:2259`, `_void_pending_suggestions:2446`, `reject_all_hunks:2473`,
`reject_hunk:2525`.

`review_hunks` already has the exact pattern to copy (`instruction_service.py:1672-1697`):
read everything off the ORM first on the event loop, hand the worker thread plain
strings and sets, run the whole batch in **one** `asyncio.to_thread` under
`_HUNK_CPU`, sharing a single `RebasedHunkCache`. The comment there spells out why
the ORM must not be touched from the thread (an expired attribute would emit SQL on
the async session from the wrong thread).

Same function, same inputs, so I1 holds. The shared cache is a second win: every
suggestion on an instruction rebases against the same main text and suggestions
forked from the same build share a base, so the quadratic `(base, main)` alignment
is computed once instead of once per suggestion.

Given I8, this is the change that stops one delete from being a page-wide outage.

**0b. Delete should not diff at all.** `_void_pending_suggestions` computes
per-hunk keys purely to record each one as rejected — for a row that is being
removed. Replace with the settled marker from I2, stamped per pending suggestion
build as `_settle_resolved_suggestion_rows` already does.

One thing to verify before relying on it: a settled marker is invalidated when main
drifts or the build stages a new proposed version. For a deleted instruction
neither should be able to happen, but confirm against `_settled_marker_matches`
(`instruction_service.py:2163`) — if the marker can go stale, use an unconditional
void marker instead. This is the difference between correct and *nearly* correct,
and this exact surface is what `agents-pending-review-clearing.md` was about.

> Target: delete of the pathological row (35 suggestions, 8k chars) from ~5–7 s to
> sub-second, and zero event-loop block. Verify with `bench_delete_with_pending.py`.

## Stage 1 — make the snapshot copy one statement instead of N inserts

`_copy_build_contents` (`build_service.py:179-188`) builds N ORM objects in a Python
loop. Replace with a single set-based `INSERT ... SELECT` writing the same columns
with the same flags:

- `copy_from_main` (the hot path): the target's base **is** the source, so
  `is_change` is the literal `false` for every row — no per-row lookup at all.
- rollback (`base_id is None`): `is_change` is the literal `true`.
- The mixed case (copying a build that is neither the target's base nor base-less)
  needs the anti-join. Check whether any caller actually reaches it; if not, keep
  the Python loop there as a rarely-taken fallback rather than porting it.

Note the current loop does not copy `base_version_id` — new rows get NULL. That
looks intentional under I4 (a carry-over row is not a change, so it has no
baseline), but preserve it exactly either way.

Byte-identical rows, so I3 and I4 hold. This removes 0.84 s of a 1.38 s save at
3,500 instructions locally, and on Postgres collapses N round trips into one.

> Target: `_copy_build_contents` flat and small in `bench_save_breakdown.py`
> regardless of instruction count.

## Stage 2 — stop the two O(snapshot) scans on the non-admin save path

Both read the entire snapshot to find one changed row, and both have `is_change`
sitting there unused:

- **`_can_auto_publish_build`** (`instruction_service.py:3455`) — filter to
  `BuildContent.is_change == True`. This is also a **correctness fix**: today
  `instr_ids` is the whole snapshot, so if the org holds *any* global instruction
  (no data source), `any(not ds_by_instr.get(iid))` is True and an agent admin can
  **never** auto-publish — directly contradicting the method's own docstring.

  > **Resolved in PR #922** (per-agent RBAC), which makes exactly this change and
  > names the consequence: an agent manager's accept "returned 200, recorded the
  > hunk as accepted and settled, and never reached main — losing the change."
  > It also rewrites `tests/e2e/rbac/test_instruction_pending_carryover.py`, whose
  > fixture had been *relying* on this bug to manufacture a pending build. See
  > `instruction-write-path-perf-fix.md` for how those red tests briefly talked me
  > out of the right fix.
- **`_changed_instructions_for_build`** (`review_producers.py:148`) — materializes
  main's snapshot, the base's snapshot and the build's snapshot into Python dicts
  (3N rows) to derive the changed set `is_change` already records.

## Stage 3 — stop manufacturing the rows (structural)

This is what actually ends the (builds × instructions) growth measured at 1.04M rows
in `agents-instructions-carryover-perf.md`. Draft builds hold only their changes;
the full snapshot is materialized once, at promote, by the Stage 1 statement
(previous main's rows, overridden by the build's changed rows).

Because promote still writes N rows, the win here is on **unpublished** drafts —
which is precisely the customer's shape (169 open builds in the profiled
deployment). Every unreviewed suggestion stops costing a full org snapshot.

Two things must be built for it, both consequences of I6/I7:

- **An explicit tombstone.** With changes-only drafts, "removed" can no longer be
  inferred from absence. `build_contents` needs a deleted flag, and `diff_builds`,
  `publish_build`'s merge path and `remove_from_build` must read it.
- **Promote materializes.** Main keeps its complete snapshot (I6) so
  `_main_text_of`, the context loaders and rollback are untouched.

`_filter_build_contents` gets *simpler*: its "inherited vs newly added" comparison
against the base's contents becomes trivial when everything in the draft is a change.

Do not start Stage 3 before Stages 0–2 are in and measured — most of the customer's
pain is Stage 0 and 1, at a fraction of the risk.

## Stage 4 — bound the history

Nothing prunes `build_contents`. `agents-pending-reconciliation-perf.md` already
named archiving obsolete pending builds as the real remedy and it is still
unaddressed. Retention on superseded non-main builds: keep the build row for audit,
drop its carry-over rows past a window, keep its `is_change` rows so pending review
still works.

## Fix alongside: the draft-adoption hazard

The hazard in the section above sits on this exact path, and **Stage 3 makes it
worse** — a changes-only draft adopted and promoted as if it were complete would
publish an empty main. Fix it with Stage 0:

- scope `get_or_create_draft_build`'s lookup by `created_by_user_id`, and
- do not let `_auto_finalize_build` promote a build the current request did not
  create.

## Regression cover that must stay green

- `tests/e2e/test_instruction.py` — `test_reject_all_settles_drifted_noop_suggestion`,
  `test_partial_reject_keeps_pending_badges` (I1, I2)
- `tests/e2e/rbac/test_instruction_pending_carryover.py` — counts and per-row flags
  track edits, not instruction count (I3)
- `tests/e2e/test_training_multi_instruction_accept.py` — siblings stay
  independently acceptable; the shared draft is never finalized or pruned
- `tests/e2e/test_build.py`, `test_git_sync_builds.py` — lifecycle, merge, rollback
  (I6, I7)

New cover this change needs: an agent-admin auto-publish test with a global
instruction present (Stage 2), and a delete-with-many-suggestions test asserting
the row stops being pending on every surface without a diff (Stage 0b).

## Artifacts

- `backend/scripts/bench_instruction_write.py` — save/delete wall time, SQL count,
  and `build_contents` growth at the current workspace size.
- `backend/scripts/bench_save_breakdown.py` — per-phase share of one save.
- `backend/scripts/bench_delete_with_pending.py` — delete cost vs. suggestion
  count and text length.
