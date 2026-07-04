# Sandbox Feedback Loop — `/agents` tree pane: instructions load very slowly

Reproduces the reported bug: on the **Agents** page, when an agent has many
instructions, the left **agents tree pane** takes a very long time (a minute+ in
production) to show the Instructions rows. During the wait every agent's
**Instructions** node renders as `0` / "No instructions yet", then the rows snap
in all at once.

This doc is the runnable feedback loop used to confirm the root cause in a fresh
SQLite sandbox.

---

## Root cause (validated)

The tree pane (`frontend/components/KnowledgeExplorer.vue`, mounted by
`frontend/pages/agents/[...slug].vue`) fires, on mount, both:

- `GET /api/instructions?...&limit=200` — the instruction rows (`fetchAll`)
- `GET /api/instructions/pending-changes` — the per-row "pending review" dots (`fetchPendingMap`)

Both are slow because both run the **same per-instruction N+1 hotspot**:

`InstructionService.get_pending_change_instruction_ids`
(`backend/app/services/instruction_service.py:1196`) loops over every candidate
instruction and calls `review_hunks` once per instruction
(`instruction_service.py:1240`):

```python
cand_rows = (await db.execute(cand_stmt)).all()
for (iid,) in cand_rows:                       # one iteration per pending instruction
    r = await self.review_hunks(db, str(iid), ...)   # ~8 queries + text diff, EACH
    if r and r.get("suggestions"):
        pending.add(str(iid))
```

Each `review_hunks` (`instruction_service.py:1148`) is **not** a single query — per
instruction it runs `_get_instruction_by_id`, `_main_text_of` (main-build text +
version), `_pending_suggestion_builds`, then per suggestion build
`_build_base_text` + a `rebased_hunks_against_main` CPU diff + an `AgentExecution`
query. Measured: **8 SQL statements per pending instruction**.

Two scoping differences make the endpoints behave differently:

- **`/instructions/pending-changes`** is called with **no `candidate_ids`**
  (`backend/app/routes/instruction.py:326`), so the loop runs **org-wide** over
  *all* pending instructions → cost grows without bound as the org accumulates
  instructions. This is the long pole.
- The **list** path (`_execute_instructions_query:2652`) passes
  `candidate_ids=<on-screen rows>`, so it's bounded by the page size (200) — still
  ~2s, but it doesn't grow past a page.

A fully **batched** alternative already exists but is unused:
`_get_pending_change_instruction_ids_legacy` (`instruction.py:332`) computes the
same set with ~4 bulk queries (it uses the coarser `covers()` rule, not the
per-hunk model).

---

## Environment setup (fresh sandbox)

Python 3.12, Node 22. Backend on `:8000`, frontend on `:3000`.

```bash
cd backend
uv sync --extra dev
export BOW_DATABASE_URL="sqlite:///db/app.db"
export BOW_SMTP_PASSWORD="dummy"
export ANTHROPIC_API_KEY="dummy"
mkdir -p db && rm -f db/app.db
uv run alembic upgrade head
uv run python main.py            # backend at http://localhost:8000
```

Create an admin user + org (dev config allows uninvited signups):

```bash
BASE=http://localhost:8000
curl -s -X POST $BASE/api/auth/register -H "Content-Type: application/json" \
  -d '{"email":"sandbox@bow.dev","password":"Sandbox123!","name":"Sandbox Admin"}'
TOK=$(curl -s -X POST $BASE/api/auth/jwt/login \
  -d "username=sandbox@bow.dev&password=Sandbox123!" \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['access_token'])")
ORG=$(curl -s $BASE/api/organizations -H "Authorization: Bearer $TOK" \
  | python3 -c "import sys,json;print(json.load(sys.stdin)[0]['id'])")
echo "$TOK" > /tmp/token.txt; echo "$ORG" > /tmp/org.txt
```

---

## Loop A — Seed + reproduce (no live LLM needed)

Seed N instructions, each with a live main-build version (v1) plus a **draft,
non-main suggestion build** whose proposed text (v2) differs from main — i.e. a
real "pending review" hunk for every instruction. They attach to one data source
("Perf Agent") that the admin is a member of, so they also render under one agent
in the tree.

```bash
cd backend
export BOW_DATABASE_URL="sqlite:///db/app.db" BOW_SMTP_PASSWORD=dummy ANTHROPIC_API_KEY=dummy

uv run python scripts/seed_instructions_pending.py 300 1.0   # 300 instructions, all pending
# re-run to add another 300 (→ 600 total) and show linear scaling
uv run python scripts/seed_instructions_pending.py 300 1.0
```

Time the two tree-pane calls:

```bash
TOK=$(cat /tmp/token.txt); ORG=$(cat /tmp/org.txt)
H="-H Authorization:Bearer\ $TOK -H X-Organization-Id:$ORG"
curl -s -o /dev/null -w "pending-changes: %{time_total}s\n" \
  "http://localhost:8000/api/instructions/pending-changes" $H
curl -s -o /dev/null -w "list(limit=200):  %{time_total}s\n" \
  "http://localhost:8000/api/instructions?skip=0&limit=200&include_own=true&include_drafts=true&include_archived=true" $H
```

Profile the query count (confirms it's N+1, not one heavy query):

```bash
uv run python scripts/profile_pending_changes.py
```

### Observed (PASS — bug reproduced)

| Pending instructions | `GET /pending-changes` | `GET /instructions?limit=200` |
|---|---|---|
| 300 | **2.5–3.2 s** | **~2.3 s** |
| 600 | **4.8 s**     | **~2.0 s** (capped at page size) |

```
# profile_pending_changes.py @ 600 instructions
pending instructions: 600
wall time:            5.41s
SQL statements fired: 4801      #  ~8 queries per instruction  ← N+1
queries per pending:  8.0
```

- `pending-changes` grows **linearly** with org-wide instruction count
  (300→2.5s, 600→4.8s); the list is bounded by the 200-row page.
- This is on **local SQLite** (≈0 network latency). On production Postgres each
  of those 4801 round-trips also pays ~1–5 ms of network latency, so the same
  shape stretches to the **minute+** in the report.

> The frontend symptom (Instructions node stuck at `0` / "No instructions yet"
> for several seconds, then a bulk snap-in) follows directly: the rows are gated
> by these two slow calls, and the empty state is shown while they're in flight.

---

## What this proves

- Both tree-pane instruction calls share one per-instruction loop
  (`review_hunks`) firing **8 SQL statements per instruction**.
- `/instructions/pending-changes` is **unbounded** (org-wide, no `candidate_ids`)
  → it's the dominant, minute-scale stall as instruction count grows.
- The work is fully batchable: the same answer needs ~4 bulk queries
  (main texts, pending suggestion builds, base texts) + an in-memory diff pass.

## Candidate fix (not yet applied)

Rewrite `get_pending_change_instruction_ids` to bulk-load main texts, pending
suggestion builds, and base texts in ~4 queries, then run
`rebased_hunks_against_main` in a pure-Python pass (no awaits in the loop) — same
per-hunk semantics, queries drop from `O(N×8)` to ~4. Plus a frontend loading
state so the Instructions node shows "loading" instead of `0` while in flight.

Iterate here: edit the candidate fix and re-run Loop A — `profile_pending_changes.py`
should report a small, constant SQL count and sub-second wall time regardless of N.

## Repro artifacts

- `backend/scripts/seed_instructions_pending.py` — seeds instructions + versions
  + main build + per-instruction pending suggestion builds.
- `backend/scripts/profile_pending_changes.py` — wall time + SQL statement count
  for `get_pending_change_instruction_ids`.
