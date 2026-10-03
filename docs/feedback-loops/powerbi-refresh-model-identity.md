# Feedback Loop — Power BI refresh retains old columns or selects a namesake model

Explicit refresh must discover current columns, and identically named semantic models must remain separate throughout discovery, shared indexing, and per-user schema persistence. This investigation and verification uses synthetic fixtures and a demo tenant only.

## Root causes (validated)

- Agent Reload requested incremental discovery. Personal Refresh access passed prior tables without requesting fresh introspection. Power BI's `force_refresh` previously bypassed only its instance cache, while still reusing prior column definitions (`backend/app/data_sources/clients/powerbi_client.py:1489`).
- Discovery named every table `Dataset/Table`. Different datasets produced identical keys in the service normalization dictionaries. Shared/user matching and orphan repair could also adopt a same-named table from another model.

## Loop A — deterministic reproduction

From `backend`, using Python 3.12 and the dev dependencies:

```bash
TESTING=true BOW_DATABASE_URL=sqlite:///db/app.db .venv/bin/python -m pytest \
  tests/unit/test_powerbi_schema_refresh.py \
  tests/e2e/test_powerbi_refresh_identity.py -q
```

Only the external Power BI HTTP boundary is substituted. The endpoint tests register users and organizations through the existing API fixtures, then seed legacy schema records which the fixed API must no longer create. Services, credential resolution, database persistence, and schema reads execute normally.

Before the fix:

- Five initial connector regressions failed: a forced refresh returned `old_name`/`removed`, and four ordering/workspace variants emitted duplicate names.
- Running the new admin/member personal-refresh endpoint regressions against a separate baseline checkout produced **2 failures**: HTTP refresh completed, but subsequent schema reads returned `old` instead of `renamed`/`added`.
- A follow-up regression reproduced sign-in reverting a successful personal refresh: the routine sync returned `old` instead of `current_a`. Personal snapshot reuse fixes that rollback.

The final tests cover:

- Rename/add/remove columns, even with a prior catalog supplied.
- Distinct models with identical names in one workspace or separate workspaces, independent of discovery order.
- Item-shared models absent from workspace listings and explicit workspace-filter enforcement.
- Foreign keys remaining within their own model across collision/subset discovery.
- Repair of legacy user links, stable canonical IDs and active selections, repeat refresh, and models disappearing from a user's view.
- Shared sync refusing to adopt/delete another model's user-contributed row.
- Admin/member synchronous and background personal-refresh endpoints and unreadable-model diagnostics.
- Exact-name query routing and rejection of ambiguous aliases.

PostgreSQL verification uses the same e2e suite against a disposable database:

```bash
TESTING=true BOW_DATABASE_URL=sqlite:///db/app.db \
TEST_DATABASE_URL="$LOCAL_TEST_POSTGRES_URL" \
.venv/bin/python -m pytest tests/e2e/test_powerbi_refresh_identity.py --db=external -q
```

The normal testcontainers attempt could not initialize because Docker's disk was full. Verification instead used a disposable PostgreSQL 15 container with its data directory on tmpfs. No existing database was used.

Validation results:

- Broad SQLite connector/service regression run: **202 passed**.
- Follow-up query-resolution/indexing suite: **109 passed** (overlaps the broad suite).
- Final focused persistence/API suite: **17 passed on SQLite and 17 on PostgreSQL**, including the real background queue.
- Final shared-sync foreign-key assertion: passed on SQLite and PostgreSQL.
- Follow-up sign-in/identity service regressions: **32 passed on SQLite**; the focused PostgreSQL suite was rerun with the sign-in preservation assertion.
- Ruff checks on new Python files and `git diff --check`: passed.

## Loop B — read-only demo verification

Supply credentials through environment variables (never commit them):

- `PBI_TENANT_ID`, `PBI_CLIENT_ID`, `PBI_CLIENT_SECRET`.
- For a demo user's delegated token, also `PBI_USERNAME`, `PBI_PASSWORD`.

From the repository root:

```bash
backend/.venv/bin/python tools/agent/verify_powerbi_schema_refresh.py
```

The script lists models and reads metadata using the real connector. It disables permission-cache refresh and admin scan-job creation. It does not write BOW records, alter models, or change upstream columns. A stale column definition is injected **only in memory**; routine incremental discovery retains it, while explicit refresh must replace it with current live metadata.

Observed after the fix:

| Identity | Tables | Distinct names | Same-label model/table groups | Explicit refresh replaced stale columns |
|---|---:|---:|---:|---|
| Demo service principal | 52 | 52 | 0 | Yes |
| Demo user A | 64 | 64 | 12 | Yes |
| Demo user B | 51 | 51 | 0 | Yes |

Before the fix, Demo user A's 64 discovered tables collapsed into 52 name keys; reversing discovery order changed the winner for all 12 collisions. After the fix all 64 survive name-keyed normalization. Unreadable-model counts differed by identity (2, 1, 0); the script reports them rather than claiming all models were readable.

## Fix

- Explicit agent/personal/shared refresh requests full introspection. Routine sign-in retains incremental reuse, preferring the user's last verified columns so an older shared catalog cannot undo a successful personal refresh. Prior metadata remains available as a candidate list for item-shared models, even during a full refresh.
- Colliding table names include workspace/model identity before normalization. Foreign-key targets are renamed within their own model.
- Shared catalog and user overlay reconciliation compare workspace/dataset/table identity within the connection. Existing IDs/selections are retained for the matching model; namesakes receive separate rows. Incorrect legacy overlay links are repaired on refresh.
- Shared-to-agent sync and orphan healing enforce the same identity rule.
- Personal refresh exposes unreadable-model diagnostics in its response/background job.

## Limits

This proves connector behavior and local persistence/endpoint behavior, plus read-only discovery against real demo models. It does not simulate upstream column renaming by modifying a live model, does not deploy a fix, and does not resolve a provider-side capacity/permission failure. Full refresh can take longer and remains subject to Power BI API rate limits.
