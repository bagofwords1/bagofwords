# Feedback Loop — "add retention for reports and artifacts on org settings … fully deleted"

An org admin wants old conversations and dashboards gone after N days: the
report **and everything it produced** (completions, agent executions, tool
executions, plans, blocks, snapshots, widgets, steps, queries…), deleted for
good, configured in org settings and empty (off) by default. File retention
already existed; it must delete for good too.

## Root cause (validated)

Nothing deleted a report. "Deleting" a conversation only archives it
(`report_service.py:2159`, `status='archived'`); artifact delete is soft
(`artifact_service.py:505`). No setting existed: `PUT /api/organization/settings`
with `report_retention_days` answered 200 and silently dropped the key.
Most foreign keys into a report's tree have no `ON DELETE CASCADE`, and SQLite
runs without FK enforcement, so a naive delete leaves dangling rows on SQLite
and fails on Postgres. File retention deleted rows for good but only tried
once to remove the bytes (`file_service.discard_file_bytes`), and logged and
forgot a failure.

## Loop A — deterministic (pytest, no LLM)

```bash
cd backend
export BOW_DATABASE_URL=sqlite:///db/app.db TESTING=true
uv run pytest tests/unit/retention tests/e2e/rbac/test_report_artifact_retention.py \
  tests/e2e/rbac/test_file_retention.py -q
# Postgres (enforces every FK): a throwaway server, never the app database
TEST_DATABASE_URL=postgresql://postgres@localhost:55432/bowtest \
  uv run pytest tests/e2e/rbac/test_report_artifact_retention.py --db=external -q
```

- `tests/fixtures/retention_tree.py` — `seed_tree` walks `Base.metadata` and
  writes **one row in every table** a purge may delete (68 tables), all hanging
  off one report, plus every table that keeps a link into it (instructions,
  usage records, eval cases…). `orphans` checks **every FK and logical
  reference in the schema** for rows pointing at nothing.
- `tests/unit/retention/test_policy_coverage.py` — every reference into the
  purge tree has a policy (`DELETE` or `NULL`); a new model that points into it
  fails CI until it gets one.

**Before** (base code, same scenario: set 30 days, run the nightly jobs 31 days
later):

```
PUT settings -> 200 | stored: None
report retention job: No module named 'app.services.retention'
old report rows left after the nightly run: 1
E       AssertionError: the 31-day-old report is still there: report retention does not exist
1 failed
```

## Loop B — live stack, real agent runs (stub LLM)

```bash
export BOW_ENCRYPTION_KEY=$(python -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())")
tools/agent/boot_stack.sh --dev
cd backend && uv run python ../tools/agent/seed_org.py --demo
uv run python ../tools/agent/seed_org.py --sqlite-sources 1 --db-path db/agent.db   # -> sources.json
STUB_SOURCES_FILE=sources.json uv run python ../tools/agent/stub_llm.py &
uv run python ../tools/agent/setup_stub_llm.py --stub-url http://127.0.0.1:9099/v1
TESTING=true ENVIRONMENT=production TEST_DATABASE_URL=sqlite:///db/agent.db \
  uv run python ../tools/agent/retention_live.py --db-path db/agent.db
```

(DevBox: skip boot/seed, `source tools/agent/devbox_env.sh`, `--db-path "$BOW_SQLITE_DB"`.)
Pin `BOW_ENCRYPTION_KEY`: the dev backend reloads on file edits, and a new
random key makes the stub provider's stored credentials unreadable.

`retention_live.py` drives real conversations through the API, ages some of
them, turns retention on through the API, reads the preview, runs the nightly
job (the function the scheduler calls at 04:00) and file retention, and checks
each scenario, the whole database and the disk. Observed:

```
S1 tree before: {"agent_executions": 2, "completion_blocks": 4, "completions": 6, "context_snapshots": 8, "dashboard_layout_versions": 1, "plan_decisions": 4, "queries": 1, "report_context_states": 1, "reports": 1, "steps": 1, "table_usage_events": 1, "tool_executions": 2, "visualizations": 1, "widgets": 1}
PASS real agent runs wrote the run tree
PASS no dangling references before the run
PASS retention set through the API  {'report_retention_days': 30, 'artifact_retention_days': 30, 'file_retention_days': 30}
preview: {"reports": 4, "artifacts": 2, "files": 1}
PASS preview counts the reports tonight's run deletes  4 == 4
nightly run: {"reports": 4, "artifacts": 1, "files": 1, ...} | file retention: 1
PASS S1 old chat, two turns, with its own upload: deleted with its whole tree  34 rows in 14 tables -> 0
PASS S2 recent chat (also uses S1's second upload): kept whole  26 rows -> 26
PASS S3 old chat with activity yesterday: kept whole  25 rows -> 25
PASS S4 old report with an active schedule: kept whole  26 rows -> 26
PASS S6 old conversation shared by link: kept whole  25 rows -> 25
PASS S7 old chat whose dashboard is shared: kept whole  27 rows -> 27
PASS S8 old chat with a never-published dashboard: deleted with its whole tree  27 rows in 16 tables -> 0
PASS S9 old chat with a rewound turn: deleted with its whole tree  32 rows in 14 tables -> 0
PASS S10 old chat an instruction was learned from: deleted with its whole tree  25 rows in 14 tables -> 0
PASS S13 old chat with a run in progress: kept whole  25 rows -> 25
PASS S14 old dashboard that left its (recent) conversation: dashboard deleted, conversation kept  2 rows -> 0
PASS S15 recent standalone dashboard: kept  2 rows -> 2
PASS S10 the instruction stays, its link to the run cleared
PASS S9 the rewound (hidden) turn is deleted too
PASS S1's own upload: row and bytes gone
PASS S2 still uses S1's second upload: kept
PASS S16 old upload deleted by file retention, bytes gone
PASS no dangling reference anywhere after the run
PASS LLM usage history kept  107 -> 107 records
PASS deletions are in the audit log (counts only)  [('retention.reports_deleted', 4), ('retention.artifacts_deleted', 1)]
PASS a second run deletes nothing
PASS preview is empty after the run
27/27 passed
```

The first live run found three bugs that Loop A could not, all fixed before
the run above:
- `NOT (… OR content_updated_at >= cutoff)` is NULL for a never-run artifact,
  so artifact retention never picked one. The comparisons are now guarded.
- The orphan census correlated self-references (`completions.parent_id`) to the
  outer row. It now uses an alias.
- Uploading a file counts as activity on a report. That is right, and the
  scenario now ages the report after the upload.

UI: `tools/agent/retention_ui.mjs` (Settings → AI settings → Data retention,
en and he). Clearing a field saves `0` and shows "Keep forever". `-5` is refused:
`400 Retention must be empty or a whole number of days between 0 and 3650`.

## The fix

- `app/services/retention/purge.py`: a generic purge that walks the schema.
  `POLICY` decides, for each of the 142 foreign keys into the tree, whether the row
  goes with it (`DELETE`) or stays with the link cleared (`NULL`: instructions,
  eval cases, usage records, data sources, other conversations, entities).
  Plus 20 `LOGICAL_REFERENCES` for id columns with no FK. A reference with no
  policy raises, and nothing is deleted. The purge clears links, breaks cycles,
  then deletes children first, in batches, and includes rewound completions.
- `reports.py` / `artifacts.py`: what expires (untouched for N days) and what
  never does: a schedule, an active webhook, any sharing, eval test reports, a
  run in progress, and a report whose tree would take a dashboard that must
  stay. Files only the deleted rows used go through `purge_files`.
- `blobs.py` + `pending_blob_deletes` (migration `retention01`): bytes a
  delete could not remove are kept and retried nightly. This applies to every
  file delete, not only retention.
- `jobs.py`: a nightly job at 04:00 (`purge_retention_daily`, one worker claims
  it), per-org isolation, and a count-only audit entry.
  `GET /api/organization/retention/preview` (`manage_settings`).
- Settings: `report_retention_days`, `artifact_retention_days`; `file_retention_days`
  takes empty too. Off is stored as `0` (`FeatureConfig` turns a `None` value
  into `True`) and shown as an empty "Keep forever" field. There is a new
  "Data retention" section with a "deleted for good" warning and the preview
  line, in all 10 locales.

**After** (same baseline scenario):

```
PUT settings -> 200 | stored: {'value': 30, 'name': 'Report retention (days)', ...}
report retention job: exists -> {'reports': 1, 'artifacts': 0, 'files': 0, ...}
old report rows left after the nightly run: 0
1 passed
```

Suites: **74 passed** on SQLite (new suite, unit guard, existing file-retention
suite) and **58 passed** on Postgres. Neighbouring suites that touch file
delete, storage, folder deletion and org settings: 972 passed, 67 skipped, 1
failed. The failure
(`test_dashboard_publish_browser.py::test_publish_reruns_a_browser_step_in_a_real_browser`,
`no such column: connection_tools.policy_locked`) fails the same way on the
unchanged base.

## What this proves / regression notes

- Every table of a report's tree is deleted, in both a synthetic tree (every
  table) and real agent-produced trees. Nothing anywhere is left pointing at a
  deleted row, on SQLite and on Postgres.
- What outlives a report stays, with the link cleared.
- Kept items are kept whole.
- A second run is a no-op.
- Not covered live: an object-store failure. Loop A covers it by refusing the
  store's delete, then letting it succeed on the next run.
- `app/models/application.py` is a dead model. No migration creates its table,
  and importing it breaks mapper configuration, so the purge's model loader
  skips it.
