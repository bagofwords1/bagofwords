# Feedback Loop — "Setting the org row limit to 100,000 still limits to 1,000"

Reproduces and validates the reported bug: an org sets **`limit_row_count`** to
100,000 in settings, yet widget/step/entity data is still capped at **1,000
rows**. The claim being validated: the org's configured row limit is ignored on
the data-(re)generation paths (query run/preview, report step rerun, entity
refresh/preview), which always fall back to a hardcoded 1,000-row cap.

---

## Root cause (validated)

Row truncation happens in `StreamingCodeExecutor.format_df_for_widget`
(`backend/app/ai/code_execution/code_execution.py:903`). It only reads the org
limit when the executor was constructed **with** `organization_settings`;
otherwise it silently falls back to `max_rows = 1000`
(`code_execution.py:926-927`).

The agent's initial `create_data` path passes `organization_settings`
correctly, but every path that **re-generates** a widget/step/entity's data
built the executor without it, so they always hit the 1,000 fallback:

- `backend/app/services/step_service.py:137` — `rerun_step` (report rerun)
- `backend/app/services/query_service.py:266` — `run_query_new_step` (query run)
- `backend/app/services/query_service.py:404` — `preview_query_code` (query preview)
- `backend/app/services/entity_service.py:475` — `run_entity_with_update` (entity refresh)
- `backend/app/services/entity_service.py:537` — `preview_entity` (entity preview)

A second, related defect surfaced while verifying: **`limit_row_count = 0`
("Set to 0 for no limit") returned 0 rows**, not all rows. Persisting 0 through
the settings-update path stores `value=0` with `state="enabled"` (the
schema-level validator that maps `<=0` to `DISABLED` does not run when a
`FeatureConfig` is rebuilt in `organization_settings_service.update_settings`),
so `format_df_for_widget` computed `df.head(0)`.

## The fix

1. Thread the org's `OrganizationSettings` (via `Organization.get_settings(db)`)
   into the executor at all five sites above.
2. In `format_df_for_widget`, treat a non-positive value as "no cap"
   regardless of the persisted `state` flag, so the documented "0 = no limit"
   contract holds.

---

## Loop A — deterministic reproduction (real backend, no external services)

Backend only; user code is pure pandas (`pd`/`np` are injected into the sandbox),
so no data source or LLM is involved.

```bash
cd backend
export TESTING=true ENVIRONMENT=production \
       TEST_DATABASE_URL="sqlite:///db/agent.db" BOW_DATABASE_URL="sqlite:///db/agent.db"
uv sync --frozen --extra dev
mkdir -p db uploads/files uploads/branding && rm -f db/agent.db
uv run alembic upgrade head
setsid uv run python main.py > /tmp/bow-backend.log 2>&1 &
until curl -sf http://localhost:8000/health >/dev/null; do sleep 1; done
uv run python ../tools/agent/seed_org.py --org-name "RowLimit Org" || true   # first user auto-gets an org
```

The two harnesses below drive the real HTTP endpoints. Each:
creates an entity/query whose saved code returns **5,000 rows**, sets the org
`limit_row_count`, then re-runs and counts the rows actually returned.

- Harness 1 (`tools/agent/verify_row_limit_entity.py`): entity **run** (refresh)
  and **preview**.
- Harness 2 (`tools/agent/verify_row_limit_query.py`): query **run**, query
  **preview**, and report **rerun** (step refresh).

**Observed BEFORE the fix** (executor built without settings): every path
returns `1000` at `limit=100000`, and `0` at `limit=0`.

**Observed AFTER the fix:**

```
# entity paths
run@100k=5000  preview@100k=5000   run@1k=1000  preview@1k=1000   run@0=5000
VERDICT: PASS ✅ limit honored

# query run / query preview / report rerun
query_run@100k=5000  query_preview@100k=5000  report_rerun@100k=5000
query_run@1k=1000    query_preview@1k=1000    report_rerun@1k=1000
VERDICT: PASS ✅ limit honored on run/preview/rerun
```

To watch it fail (validate the repro), revert any one site to
`StreamingCodeExecutor()` / `CodeExecutionManager()` (no `organization_settings`)
and re-run the matching harness: the `@100k` count drops back to `1000`.

## What this proves / regression notes

- The org's `limit_row_count` is now honored on all five re-generation paths, at
  100k (raised), 1000 (default), and 0 (no cap).
- The fix is plumbing plus the `0 = no limit` correctness fix in
  `format_df_for_widget`; the truncation logic and the settings schema are
  otherwise unchanged.
- The harnesses seed only through the real API and assert the invariant (rows
  returned == generated when under the limit, == limit when over it), not the
  single reported value.
