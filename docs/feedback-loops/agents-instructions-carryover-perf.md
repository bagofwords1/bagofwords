# Sandbox Feedback Loop — `/agents` instructions still slow: the carry-over scan

Follow-up to `agents-instructions-perf.md` (per-instruction N+1, fixed) and
`agents-page-contention.md` (pool exhaustion / reports cascade). After both, the
**Agents** page still took seconds to show the per-agent instruction counts, and
seconds more to show an agent's instructions once expanded.

The remaining cost was a single statement whose size had nothing to do with what
the page displays.

---

## Root cause (validated, fixed)

Two endpoints on the page — `GET /api/instructions/counts` (the per-agent badges,
fired on mount by `frontend/components/KnowledgeExplorer.vue`) and
`GET /api/instructions?data_source_ids=…` (the rows for an expanded agent) — both
run `InstructionService.get_pending_change_instruction_ids`. Its first query had
to find, among all open draft builds, the rows that actually propose a change:

```sql
SELECT ... FROM build_contents
  JOIN instruction_builds ... JOIN instruction_versions ...
 WHERE instruction_builds.is_main IS 0
   AND instruction_builds.status IN ('draft','pending_approval')
   AND NOT EXISTS (SELECT 1 FROM build_contents base
                    WHERE base.build_id = instruction_builds.base_build_id
                      AND base.instruction_id = build_contents.instruction_id
                      AND base.instruction_version_id = build_contents.instruction_version_id)
```

**A build snapshots every instruction.** `BuildService._copy_build_contents`
copies all of main's contents into each new build, and builds are created on
instruction edits, self-learning runs and reliability runs. So `build_contents`
grows as **(builds × instructions)**, and the anti-join above walks every open
draft build's full snapshot to discover the handful of rows that differ:

```
rows scanned by the sweep : 1,040,940
real changes among them   :       260
```

The cost therefore scales with **(open draft builds × instructions)** — the
workspace's accumulated editing history — not with the number of pending changes
or the size of the page. Nothing prunes unapproved drafts, so it only grows.

Scoping to the on-screen rows (`candidate_ids`, the agent-expand path) does not
help: the anti-join still probes every draft build's copy of those instructions.

### What it is NOT

- Not the agent list: `GET /data_sources/active` measured **0.06s** in isolation
  (8 agents / 20 connections / 16k tables). It looks slow on the page because it
  queues behind this work — in the 7-call mount burst it went 0.07s → 0.19s.
- Not a missing index: adding a composite
  `(build_id, instruction_id, instruction_version_id)` index moved the org-wide
  sweep 1.63s → 1.46s and the agent expand 3.4s → 2.4s. The scan is structural.

---

## Environment setup

Same sandbox as `agents-instructions-perf.md` (Python 3.12, SQLite, backend on
`:8000`, `sandbox@bow.dev` registered).

```bash
cd backend
export BOW_DATABASE_URL="sqlite:///db/app.db" BOW_SMTP_PASSWORD=dummy ANTHROPIC_API_KEY=dummy
mkdir -p db && rm -f db/app.db && uv run alembic upgrade head
uv run python main.py &            # register sandbox@bow.dev, save token/org
```

## Loop A — seed the production shape

The stock pending seeder gives each suggestion build a **single** content row,
which hides the scaling entirely. `fatten_pending_builds.py` gives those builds
the full snapshot a real build carries:

```bash
uv run python scripts/seed_agents_page_perf.py 20 800 8      # agents/connections/tables
uv run python scripts/seed_instructions_pending.py 4000 0.3  # 4,000 instructions, 1,200 drafts
uv run python scripts/fatten_pending_builds.py 260           # → 1,044,940 build_contents rows
```

Measure:

```bash
uv run python scripts/profile_agents_page.py     # includes get_instruction_counts
# and the HTTP path:
curl -s -o /dev/null -w "counts: %{time_total}s\n" \
  "http://localhost:8000/api/instructions/counts" $H
curl -s -o /dev/null -w "expand: %{time_total}s\n" \
  "http://localhost:8000/api/instructions?skip=0&limit=100&include_own=true&include_drafts=true&include_archived=true&data_source_ids=$DS&include_global=false" $H
```

### Observed — cost tracks the snapshot corpus, not the page

`GET /api/instructions/counts`, same 4,000 instructions throughout:

| `build_contents` rows | shape | counts |
|---|---|---|
| 5,200 | stock seed (1 row per build — unrealistic) | 0.10 s |
| 245,140 | 60 full-snapshot builds | 0.62 s |
| 1,044,940 | 260 full-snapshot builds | **1.9 s** |

This is local SQLite with zero network latency; production Postgres pays the
scan against a heap that every build INSERTs into.

---

## Fix

Record at write time what the read path was rediscovering: `build_contents.is_change`
is true exactly when the row differs from its build's base (different version,
absent from base, or no base build at all) — the predicate the anti-join computed.

- `BuildContent.is_change` + index `(is_change, build_id)`; migration `bc0001chg`
  backfills existing rows by running the old anti-join once.
- `BuildService._copy_build_contents` marks copied rows (carry-over when copying
  the build's own base — the `copy_from_main` case; changes when there is no base,
  e.g. rollback). `add_to_build` sets the flag per row against the base, so
  re-pinning an instruction back to the base version clears it.
- `get_pending_change_instruction_ids` filters on the flag instead of anti-joining.
  The list's build-metadata pass (`_execute_instructions_query`) does the same —
  its carry-over rows were scanned only to be dropped in Python, and the build now
  attributed to a pending instruction is the one that actually changed it.

### Observed — after

Service level (`profile_agents_page.py` / per-statement timing), 1.04M rows:

| call | before | after |
|---|---|---|
| `get_instruction_counts` | 1.77 s (1,615 ms in one statement) | **0.175 s** (16 ms) |
| instruction list, one agent, 100 rows | 3.25 s (2,807 ms + 223 ms) | **0.47 s** (25 ms) |

HTTP end-to-end (same sandbox, warm):

| endpoint | before | after |
|---|---|---|
| `GET /api/instructions/counts` | 1.9 s | **0.21 s** |
| `GET /api/instructions?data_source_ids=…` | 3.4 s | **0.22 s** |

Result parity: both return the same payload as before —
`total=4000`, `pending_total=1200`, and the same 1,200 pending instruction ids.
The sweep now reads **1,200 rows instead of 1,040,940**, and that number no
longer moves when the workspace accumulates more draft builds.

## Regression cover

`tests/e2e/rbac/test_instruction_pending_carryover.py` — a non-admin author's
edit produces a build that stays pending and snapshots the whole org; the tests
assert the pending count, the pending ids, and the per-row flags track the edits
(1 edit → 1 pending; 2 edits → 2), not the number of instructions riding along.

Anything that writes `build_contents` outside `BuildService` must set the flag:
the seeders and the raw-SQL test injections in `tests/e2e/test_instruction.py`
were updated, and a row left at the default would silently stop reading as a
pending change.

## Repro artifacts

- `backend/scripts/fatten_pending_builds.py` — turns thin seeded drafts into
  full snapshots (the production shape).
- `backend/scripts/seed_instructions_pending.py`, `seed_agents_page_perf.py`,
  `profile_agents_page.py` — as in the earlier loops.
