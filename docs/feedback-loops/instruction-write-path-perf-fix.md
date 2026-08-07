# Sandbox Feedback Loop — fixing the instruction save/delete write paths

Companion to `instruction-write-path-perf.md`, which profiled the write paths and
proposed a staged plan. This is the runnable measure → fix → measure loop for
stages 0–2, run against a seeded workspace at the customer's shape, with an
exhaustive check that the change loses nothing.

**Headline: delete went 16.7s → 0.59s (28×), and a trivial concurrent request
went from 0.68s back to 0.03s. Save did NOT improve — and the reason corrects a
claim in the earlier doc.**

---

## The sandbox

Backend `:8000`, frontend `:3000`, SQLite, seeded to the shape a long-lived
workspace reaches (`scripts/seed_acme_shape.py`):

| | |
|---|---:|
| instructions | 1,200 (160 never published) |
| open draft/pending builds | 220 |
| `build_contents` rows | 231,040 |
| drifted (expensive) rows | 700 |
| suggestions on the hottest instruction | 35 |
| instruction text | median 3.5k, p90 8.2k, max 15.2k chars |

A pristine copy of the seeded DB is kept and restored before every run
(`scripts/sandbox_reset.sh`), so the before-run and the after-run start from
byte-identical state and can be compared row by row.

### A sandbox trap worth writing down

`main.py` spawns its uvicorn workers through `multiprocessing`, and **those
children's command lines contain neither `main.py` nor `uv`**. A pattern kill on
`main.py` therefore leaves the workers alive, still bound to `:8000` and still
holding an open handle on the database file the reset is about to replace. They
keep serving from the deleted inode: the API answers `200` with data that is not
in `db/app.db`, writes vanish, and rows created seconds earlier come back
"not found".

This cost several bogus measurements before it was spotted — including a "save"
that looked 5× faster because it had silently become a no-op. Two guards now:

- `sandbox_reset.sh` kills on the **venv interpreter path** (which every
  descendant shares) and refuses to continue while anything still answers `:8000`.
- `measure_write_ops.py` compares the API's view of the target row against the
  database file before it times anything, and aborts if they disagree.

Any measurement in this doc that could have been affected was re-run after both
guards were in place.

---

## Measured — HTTP, sequential, data-verified

`scripts/measure_write_ops.py`. Same pristine DB, same two target rows, chosen
deterministically. The "collateral" probe is a trivial `GET` fired *during* the
delete — it measures what the delete does to everyone else on the worker.

| | before | after |
|---|---:|---:|
| `PUT /api/instructions/{id}` (save) | 0.87 s | **0.85 s** |
| `DELETE /api/instructions/{id}` (35 suggestions) | **16.66 s** | **0.59 s** |
| trivial `GET`, worker idle | 0.02 s | 0.02 s |
| trivial `GET`, **during the delete** | **0.68 s** | **0.03 s** |
| `build_contents` rows written by the two ops | 2,079 | 2,079 |

Delete is **28× faster**, and the collateral damage is gone: a concurrent request
no longer pays for someone else's delete.

Confirmed at the service level too (`scripts/bench_delete_breakdown.py`, after):

```
delete_instruction TOTAL     0.435s
  _void_pending_suggestions   0.014s  ( 3.2%)   <- was the dominant cost
  get_or_create_draft_build   0.273s  (62.8%)
    _copy_build_contents      0.241s  (55.3%)
  _auto_finalize_build        0.090s  (20.7%)
```

## Measured — end to end through the real UI

Playwright drives the actual `/agents` flow: open the instruction, click **Edit**,
type into the editor (asserting the body really changed, so a no-op save can
never masquerade as a fast one), click **Save**; then open the instruction
carrying 35 suggestions and click **Delete**. Both runs from the same pristine
DB, both verified afterwards to have landed (1,199 live rows, 2,462 versions,
the target soft-deleted).

| click → HTTP response | before | after |
|---|---:|---:|
| Save | 1.58 s | 1.38 s |
| Delete (35 suggestions) | **33.6 s** | **13.2 s** |

2.5× end to end — much less than the 28× the endpoint itself gained, and the
difference is worth being precise about. In the UI the delete is no longer the
expensive thing on the page: opening that instruction fires
`GET /instructions/{id}/review-hunks`, which measured **5.5 s** in the after-run,
and the post-mutation refresh re-runs `counts` (~5 s on this corpus). Those are
**read** paths — already off the event loop, and not what this change targeted.
The write path is fixed; what is left in front of the user is the authoritative
review of the instruction they have open.

## What was actually wrong with delete — two things, not one

**1. The void pass rebased every suggestion to describe hunks of a row being
deleted.** `_void_pending_suggestions` ran the word-level 3-way diff (quadratic
in the text) once per suggestion build, purely to enumerate hunk keys and record
each one rejected. Nothing reads those keys afterwards — the instruction is gone,
so "which hunk" is not a question anyone can ask. It now writes one unconditional
`__voided__` marker per proposing build, answering the only question the sweep
asks, with no diff at all.

Two details matter:

- The marker binds **no versions**, unlike `__settled__`. A settled marker
  expires when main drifts or the proposal restages — correct for a live row,
  wrong for a deleted one, where re-evaluation would resurrect the badge forever.
- It is scoped to builds whose row for this instruction is `is_change`. A build
  snapshots the whole org, so `_pending_suggestion_builds` returns **every** open
  build in the workspace (220 here), nearly all carrying the instruction as an
  unchanged carry-over that never made it pending. Marking those would be
  hundreds of pointless JSON rewrites per delete. Missing this first cost 5.4s.

**2. The DELETE route ran the authoritative review just to authorize.** The route
called `get_instruction`, whose detail schema includes the *authoritative*
pending check — the quadratic per-hunk rebase over every open suggestion on the
row. It needs only the owner, the status and the attached agents, and a
purpose-built `get_instruction_access_view` already existed for exactly this.
This was **~5.6 s of the remaining 6.0 s**: the endpoint was deciding whether a
row it is about to delete has pending changes worth reviewing.

## What was wrong with my earlier analysis of save

The previous doc claimed `_copy_build_contents` was 40–61% of a save and framed
it as Python-object overhead to be removed with a set-based insert. The insert is
now one Core `executemany` instead of N ORM objects — and **save did not get
meaningfully faster** (0.87 s → 0.85 s).

Direct micro-benchmark of the two strategies, same 1,040 rows, same corpus:

| | build | commit | total |
|---|---:|---:|---:|
| ORM `db.add()` per row | 0.022 s | 0.289 s | 0.311 s |
| Core `executemany` | 0.111 s | 0.165 s | **0.276 s** |

**The copy is I/O-bound, not Python-bound.** The cost is SQLite writing 1,040
rows into a 233k-row table and maintaining its indexes; the Python object churn
was ~11% of it. The bulk insert is kept — it is faster and simpler — but the
honest conclusion is that **no amount of optimizing the copy fixes save. Only not
writing the rows fixes save**, which is stage 3 (changes-only drafts,
materialized at promote) in the plan. The earlier doc over-attributed the cost;
this corrects it.

## `_can_auto_publish_build` — a bug after all, and how the tests misled me

The earlier doc reported this as buggy: it loads the build's whole snapshot, so a
single global instruction anywhere in the org blocks an agent admin from
auto-publishing. Filtering it to `is_change` broke six tests in
`tests/e2e/rbac/test_instruction_pending_carryover.py`, whose fixture appears to
state the rule as intended:

> *"The second agent is what keeps the author's build in review: auto-publish
> requires authority over every instruction the build contains, and a build
> contains all of them."*

**I read that as a deliberate authorization boundary and reverted my change. That
was wrong.** The fixture was *using* the bug to manufacture a pending build, not
documenting an invariant — and a failing test that encodes a bug is not evidence
the bug is a feature.

PR #922 (per-agent RBAC) independently reached the opposite conclusion and has
the consequence I missed: with the unfiltered check, an agent manager's accept
"returned 200, recorded the hunk as accepted and settled, and never reached main
— **losing the change**." It filters to `is_change` (and returns `True` rather
than `False` for an empty set, so a no-op build doesn't strand itself in
`pending_approval`), and rewrites the carryover fixture to stage its pending
build through the AI capture path as a grantless member — how production actually
produces pending work — leaving that file's assertions unchanged.

On merge this branch takes #922's version. The filter is the correct behaviour
*and* it removes the O(instructions-in-the-org) scan from every non-org-admin
save, so the two changes agree.

The lesson worth keeping: I treated six red tests as proof the existing behaviour
was intended. The right question was *why* the fixture needed that rule to
produce a pending build at all — which is exactly the question #922 asked.

---

## Nothing is lost — the verification

Timing is easy to fake; the important question is whether the same operations
leave the workspace in the same state. `scripts/fingerprint_instruction_state.py`
takes a canonical, content-addressed fingerprint of the entire instruction
surface — the main snapshot every read resolves against, every instruction row,
every version, every build, every build's content digest, every association —
keyed on stable identity (instruction id, `build_number`) and comparing content
by sha, never by generated id.

Procedure: restore pristine → run the two operations → fingerprint. Once on the
original code, once on the fixed code.

**Control first** (original code, twice) — proves the fingerprint is deterministic
and the diff below is real signal, not run noise:

```
=== CONTROL: OLD vs OLD ===
  [OK ] builds: 224 builds, none lost or invented
  [OK ] main_snapshot: 1039 rows identical
  [OK ] instructions: 1200 rows identical
  [OK ] versions: 2462 rows identical
  [OK ] associations: 1200 rows identical
  [OK ] build_contents: 224 builds, every snapshot identical
RESULT: IDENTICAL
```

**Original vs fixed:**

```
=== OLD vs NEW ===
  [OK ] build_contents       233119 vs 233119
  [OK ] builds               224 vs 224
  [OK ] instructions_all     1200 vs 1200
  [OK ] instructions_live    1199 vs 1199
  [OK ] main_builds          1 vs 1
  [OK ] main_rows            1039 vs 1039
  [OK ] versions             2462 vs 2462
  [OK ] builds: 224 builds, none lost or invented
  [NOTE] builds.rejected_hunks_sha: differs on 35 builds
         ^ review METADATA only (the per-hunk rejection list vs the
           single void marker a delete now writes).
  [OK ] main_snapshot: 1039 rows identical
  [OK ] instructions: 1200 rows identical
  [OK ] versions: 2462 rows identical
  [OK ] associations: 1200 rows identical
  [OK ] build_contents: 224 builds, every snapshot identical
```

The **only** difference in the entire workspace is the `rejected_hunks` metadata
on exactly the 35 builds that proposed a change to the deleted instruction — the
deliberate representation change. No instruction, version, snapshot, association
or build-content row differs anywhere.

### And the same thing is true of what users see

Stored equality is necessary, not sufficient — the two representations must also
*read* the same. `scripts/capture_pending_api_state.py` captures every pending
surface the /agents page renders from, after the same operations:

| | original | fixed |
|---|---:|---:|
| `counts.total` | 1,199 | 1,199 |
| `counts.pending_total` | 475 | 475 |
| `counts.pending_instruction_ids` | 475 ids | **same 475 ids** |
| `GET /instructions/pending-changes` | 475 ids | **same 475 ids** |
| `pending_only` list | 0 | 0 |
| `review-hunks` (deleted row) | HTTP 404 | HTTP 404 |
| `review-hunks` (saved row) | 0 suggestions | 0 suggestions |

```
RESULT: OBSERVABLE STATE IDENTICAL on every pending surface
```

### Regression cover

**88 passed** across `tests/e2e/test_instruction.py`, `test_build.py`,
`test_instruction_resolve.py`, `test_training_multi_instruction_accept.py`,
`tests/e2e/rbac/test_global_instruction_authority.py`, `test_rbac_instructions.py`,
plus `tests/e2e/rbac/test_instruction_pending_carryover.py` (7 passed).
- New: `test_delete_voids_pending_suggestions_on_every_surface` — pins the
  observable contract (a deleted instruction stops being pending on the counts
  badge, the sweep and the pending list, for both a clean and a **drifted**
  suggestion, and stays gone on re-evaluation). Verified to pass on the
  **original** code too, so it guards the contract rather than encoding the
  change.

---

## Reproducing

```bash
cd backend
export BOW_DATABASE_URL="sqlite:///db/app.db" BOW_SMTP_PASSWORD=dummy ANTHROPIC_API_KEY=dummy
mkdir -p db && rm -f db/app.db && uv run alembic upgrade head
uv run python main.py &
curl -s -X POST http://localhost:8000/api/auth/register -H "Content-Type: application/json" \
  -d '{"email":"sandbox@bow.dev","password":"Sandbox123!","name":"Sandbox Admin"}'

uv run python scripts/seed_acme_shape.py custom --instructions 1200 --open-builds 220 \
  --expensive 700 --max-suggestions 35 --agents 5 --not-in-main 160
cp db/app.db db/pristine.db          # the byte-identical starting point

bash scripts/sandbox_reset.sh                        # restore + restart, verified
uv run python scripts/measure_write_ops.py before /tmp/m_before.json
uv run python scripts/fingerprint_instruction_state.py /tmp/fp_old.json
uv run python scripts/capture_pending_api_state.py OLD /tmp/api_old.json <deleted_id>
# ...apply the change, then repeat with `after` / fp_new / api_new and:
uv run python scripts/compare_fingerprints.py /tmp/fp_old.json /tmp/fp_new.json
```

## Artifacts

- `scripts/sandbox_reset.sh` — restore pristine + restart, with the worker-kill guard
- `scripts/measure_write_ops.py` — save/delete timing + the collateral probe + coherence guard
- `scripts/bench_save_breakdown.py`, `scripts/bench_delete_breakdown.py` — per-phase splits
- `scripts/fingerprint_instruction_state.py`, `scripts/compare_fingerprints.py` — the data-integrity proof
- `scripts/capture_pending_api_state.py` — the observable-contract proof

## Still open (stage 3, unchanged)

Save is unchanged, and `build_contents` still grows by one row per instruction on
every write. Drafts must stop carrying full snapshots — materialize at promote
instead — which needs an explicit tombstone since "removed" is currently inferred
from absence. That work also subsumes the `_can_auto_publish_build` cost and the
draft-adoption hazard, both of which exist *because* a draft is a whole-org
snapshot.

Separately, and now the visible bottleneck on the page rather than the write
path: `review-hunks` costs **5.5 s** for an instruction carrying 35 drifted
suggestions, and `counts` ~5 s on this corpus. `agents-pending-reconciliation-perf.md`
already flagged the first ("an instruction that has accumulated dozens of
suggestions still costs seconds to open... archiving obsolete pending builds is
the real remedy — 169 open builds is itself the anomaly"). This loop measured 220
open builds behaving exactly that way.
