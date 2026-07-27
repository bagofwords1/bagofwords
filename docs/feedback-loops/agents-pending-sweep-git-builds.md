# Sandbox Feedback Loop — `/agents` "Pending changes" pane stalls

Follow-up to `agents-instructions-perf.md`. That round batched
`InstructionService.get_pending_change_instruction_ids` (the per-instruction
`review_hunks` N+1) and pushed the carry-over skip into SQL. This round covers
the amplifier that survived it: **builds with no base build**.

Reported symptom: on the **Agents** page the left pane's `PENDING CHANGES`
section sits on `Loading…` for a very long time while the org shows only
`21 pending`.

That panel is gated on
`GET /api/instructions?pending_only=true&limit=200&include_drafts=true&include_archived=true`
(`KnowledgeExplorer.vue` → `fetchPendingRows`), which — like
`/instructions/pending-changes` and `/instructions/counts` — runs the shared
sweep `get_pending_change_instruction_ids`.

---

## Root cause (validated)

The sweep's carry-over prune excludes a `build_contents` row when **the build's
base build holds that instruction at the same version**:

```python
_carryover = select(_BaseBC.id).where(and_(
    _BaseBC.build_id == InstructionBuild.base_build_id,      # <-- NULL for git builds
    _BaseBC.instruction_id == BuildContent.instruction_id,
    _BaseBC.instruction_version_id == BuildContent.instruction_version_id,
)).exists()
sug_where.append(~_carryover)
```

That works for `user`/`ai` builds, which are created with `copy_from_main=True`
and so carry `base_build_id` plus verbatim copies of main's version ids.

It does **nothing** for a git-sync build. `GitService` creates those with
`copy_from_main=False` ("Start fresh from the branch",
`app/services/git_service.py:819`), so `base_build_id IS NULL`, the correlated
subquery matches nothing, and **every row survives**. Git builds are squarely
inside the pending selector (`status in (draft, pending_approval)`,
`source in (user, ai, git)`), so each one contributes all of its contents as
candidate "changes" — each shipped with its full version text and word-diffed in
Python — even when the synced file is byte-identical to what is already live.

Cost grows as `git builds x instructions touched`, unbounded in time: a
scheduled git sync leaves one such draft build behind per run, and they are never
pruned. Meanwhile the *answer* stays tiny (21), which is why the UI shows a small
badge and a pane that will not load.

Note this was **never a correctness bug** — the Python pass reached the right
answer. It just paid ~7,000x more I/O and CPU to get there.

---

## Environment setup (fresh sandbox)

Python 3.12. Backend deps via `uv`.

```bash
cd backend
uv sync --extra dev
export BOW_DATABASE_URL="sqlite:///db/app_sweep.db" BOW_SMTP_PASSWORD=dummy ANTHROPIC_API_KEY=dummy
mkdir -p db && rm -f db/app_sweep.db
uv run alembic upgrade head
uv run python main.py &          # only needed for the signup call below
```

Create the admin user + org (dev config allows uninvited signups):

```bash
BASE=http://localhost:8000
curl -s -X POST $BASE/api/auth/register -H "Content-Type: application/json" \
  -d '{"email":"sandbox@bow.dev","password":"Sandbox123!","name":"Sandbox Admin"}'
```

Then stop the server — the profiler talks to the DB directly, and leaving
uvicorn's reloader attached to the same SQLite file causes `database is locked`
during seeding.

---

## Loop A — Seed + reproduce

`scripts/seed_pending_snapshot_shape.py` models what `BuildService.create_build`
actually writes: **every** build copies all of main's contents, so
`build_contents` grows as `builds x instructions`. (The older
`seed_pending_prodshape.py` gives each history build a single content row, which
under-models this.)

```bash
cd backend
export BOW_DATABASE_URL="sqlite:///db/app_sweep.db" BOW_SMTP_PASSWORD=dummy ANTHROPIC_API_KEY=dummy

# 600 instructions, 250 pending-selector builds (each a full snapshot), 21 live pending
uv run python scripts/seed_pending_snapshot_shape.py 600 250 21

# add git-shaped builds: source=git, status=draft, base_build_id=NULL
N_GIT_BUILDS=240 uv run python -c "
import asyncio, sys; sys.path.insert(0,'scripts')
import seed_pending_snapshot_shape as s; asyncio.run(s.add_git_builds())"

uv run python -u scripts/profile_pending_snapshot_shape.py
```

### Observed

With **only** the 250 based builds (no git builds) the sweep is already fine —
the carry-over prune does its job:

```
get_pending_change_instruction_ids     200.3 ms   sql=3   -> 21 pending
```

Adding base-less git builds is what breaks it:

| git draft builds (base NULL) | sweep | counts | `pending_only` list |
|---|---|---|---|
| 0   | 200 ms | 231 ms | 308 ms |
| 40  | 1067 ms | 735 ms | 996 ms |
| 240 | **5524 ms** | 4355 ms | 4261 ms |

Rows the sweep had to fetch and diff, at 240 git builds:

```
before fix: 144,021 rows  (18.7 MB of version text)
after  fix:      21 rows  (0.0039 MB)
reduction : 6,858x
```

All measurements are local SQLite (~0 network latency). In production on
Postgres those 144k rows — each carrying a full instruction body — also cross the
wire and are materialized as ORM objects, which is how the same shape stretches
to the minute-scale hang in the report.

---

## The fix

`proposed == main` cannot yield a hunk that changes main —
`text_hunks.has_live_hunk_against_main` already short-circuits on exactly that
(`text_hunks.py:294`), and `rebased_hunks_against_main` returns `[]` for it.
So evaluating the same predicate in SQL is **output-identical**; it just moves
the decision in front of the row transfer instead of behind it:

```python
_main_build_id = await resolve_main_build_id(db, org_id)   # resolved once
if _main_build_id:
    _already_live = (
        select(_MainBC.id)
        .join(_MainIV, _MainIV.id == _MainBC.instruction_version_id)
        .where(and_(
            _MainBC.build_id == _main_build_id,             # unique-index probe
            _MainBC.instruction_id == BuildContent.instruction_id,
            _MainIV.text == _IV.text,
        ))
        .exists()
    )
    sug_where.append(~_already_live)
```

Matching on `(build_id, instruction_id)` hits the unique index on
`build_contents` as a single probe; correlating on `instruction_id` alone would
instead walk that instruction's row in *every* build in the org just to find the
main one.

`review_hunks` (the per-instruction review, which loads every pending build
containing the instruction) gets the same short-circuit in its Python loop, since
git builds defeat its base-version skip for the identical reason.

Deliberately conservative in two places, both safe:
- NULL text compares as "not equal" in SQL, so such rows are kept and the Python
  pass (which coerces NULL to `""`) still decides.
- An instruction absent from main matches nothing and is kept — that is a
  genuine create suggestion.

### After

```
get_pending_change_instruction_ids    1101.7 ms   sql=4   -> 21 pending
get_instruction_counts                1009.2 ms   sql=17
get_instructions(pending_only)        1142.2 ms   sql=18  -> 21 rows (total=21)
```

5x faster at 240 git builds, and the residual is now a pure index-probe scan of
`build_contents` (`EXPLAIN QUERY PLAN` shows only `SEARCH … USING INDEX`, no
table scans) rather than megabytes of text crossing into Python.

---

## What this does NOT fix

The sweep still *examines* one row per (pending build x instruction) — 294k rows
here, ~1s on SQLite. That is inherent to `create_build(copy_from_main=True)`
snapshotting the entire instruction set into `build_contents` on every build.
Bounding it further needs a data-model change (e.g. storing only the delta
against the base build, or reaping abandoned draft/git builds), which is out of
scope here.

## Repro artifacts

- `backend/scripts/seed_pending_snapshot_shape.py` — full-snapshot builds
  (`main()`) plus base-less git builds (`add_git_builds()`).
- `backend/scripts/profile_pending_snapshot_shape.py` — wall time + SQL count for
  the sweep, the counts badge and the `pending_only` list.
- `backend/tests/e2e/test_instruction.py::test_git_sync_build_without_base_does_not_flood_the_pending_sweep`
  — semantics guard on the new prune (the old path was slow, not wrong, so this
  cannot catch the speed regression; the profiler above is what measures it).
