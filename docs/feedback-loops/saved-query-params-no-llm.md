# Feedback Loop — load a saved query with parameters, no code generation

Customer ask: `create_data` is slow because every request goes through LLM
code generation. They want to **save a query once (with parameters) and then
load it with different values without paying for codegen again**.

What shipped (branch `claude/saved-queries-llm-perf-7jd3jz`):

1. **Entity run API takes values.** `POST /api/entities/{id}/run` with
   `{"params": {name: value}}` executes the entity's SAVED code with those
   values as the caller, caches the result per (entity, user, values) in
   `entity_user_results`, and returns that slice in `data` /
   `applied_params`. The shared snapshot is never rewritten by a values run,
   so `load_entity` / the catalog keep serving the defaults. Bad values → 400.
   A values run needs only read access (entity visible + DS access), so any
   member can use it; rewriting the shared snapshot keeps the authoring gate.
2. **Save Query modal shows the parameters** the entity will carry
   (`EntityCreateModal` → read-only chips), and the **entity page has a
   parameter bar** (value inputs + Run + "ran with …").
3. **Chat path skips codegen.** `describe_entity` accepts `params`, the
   `<entities>` planner context renders each entity's declared `<parameters>`,
   the planner gets an `<entities_guidance>` block, and the `create_data`
   description no longer steers the planner to regenerate code over an
   existing entity. The materialized step keeps `parameters` /
   `applied_params` so the widget's param bar and dashboard viewer runs work.
4. `POST /api/entities` now persists `parameters` / `applied_params` (the
   schema declared them; the row never stored them).

## Loop A — deterministic (no LLM)

```bash
cd backend
export BOW_DATABASE_URL="sqlite:///db/app.db"   # any sqlite url; tests use their own file
uv run pytest tests/e2e/test_entity_run_params.py \
              tests/e2e/rbac/test_entity_run_params_access.py \
              tests/e2e/test_describe_entity_params.py \
              tests/unit/test_entities_section_parameters.py -v
# neighbours that must stay green
uv run pytest tests/unit/test_entity_params.py tests/unit/test_identity_taint.py \
              tests/e2e/test_loadables.py tests/e2e/test_entity.py \
              tests/e2e/rbac/test_rbac_entity_creation.py tests/e2e/test_report_rerun_params.py -q
```

Observed: 13 new tests PASS (values slice + untouched snapshot; cache hit
until `force_refresh`; unknown / identity-locked / undeclared params → 400;
identity params bind to the caller; member with access can run values but
gets 403 on a snapshot refresh; member without access refused; DS-less
snapshot refresh stays admin-only; `describe_entity` with values returns the
slice + real code + parameters, and fails instead of serving the default
snapshot for other values). 50 neighbouring tests PASS.

## Loop B — live user story (Haiku 4.5, Music Store demo, Playwright)

Setup: `tools/agent/seed_org.py --demo`, `tools/agent/setup_haiku_llm.py`
(key via env), Haiku set as default. Scripts drive the real UI at
`http://localhost:3000` and log every `/api/` response.

| Step | Prompt (typed in the UI) | Result |
|------|--------------------------|--------|
| 1. Build | *"Show total invoice revenue by billing country for one year at a time. Make the year a parameter I can change later, defaulting to 2010."* | `create_data` declared `year` (number). Widget shows the param bar. **Save Query** modal lists `Year · number · = 2021`; saved entity is published with `parameters` + `applied_params`. |
| 2. New report | *"Use the saved query "Invoice Revenue by Billing Country" for 2023."* | Planner called **`describe_entity(should_create=True, params={year: 2023})`** — no `create_data`. Widget shows `ran with year = 2023`; the report's query carries `parameters=[year]`, `applied_params={year: 2023}`. |
| 3. Entity page | Typed `2022` in the Year input → Run | `POST /api/entities/{id}/run {"params":{"year":"2022"}}` → 200, chart + "ran with year = 2022"; second identical Run served from cache. |

### Evidence KPI — LLM work per turn (`llm_usage_records`, same org, Haiku)

| Turn | LLM calls | Prompt tokens | Completion tokens | Of which codegen / viz-infer |
|------|-----------|---------------|-------------------|------------------------------|
| Build with `create_data` (step 1) | 11 | 33,275 | 2,443 | `create_data.code_gen` ×2, `create_data.viz_infer` ×2, `create_data.inspection` ×1 |
| Load saved query with values (step 2) | 4 | 6,376 | 408 | **none** (planner ×2 + title + follow-ups) |

The remaining calls in step 2 are the planner's own decision (cannot be
skipped in chat — something has to pick the entity and read the value) and
the post-turn title/follow-up passes. Direct API / entity-page runs (step 3)
make **zero** LLM calls.

Screenshots: `media/pr/saved-query-params/` (before/after entity page and
Save Query modal; new-report load; entity run with a value).

## Notes

- `describe_entity` create-mode used to persist `"[code hidden]"` as the
  step's code when `allow_llm_see_data` was off; the tool now always hands
  the orchestrator the real code (the LLM-facing field stays redacted).
- Locale catalogs: keys added to `en`/`es`/`he`. The pre-existing
  `share.*` drift between `en` and `es`/`he` is untouched.
- Not in scope: dashboards already run parameterized queries through
  `/api/queries/{id}/run` (viewer mode); a query materialized from an entity
  via `describe_entity` follows that same path unchanged.

## Loop C — entity attached to TWO agents (follow-up)

Concern: a saved query that reads from several agents must run with values
from the entity page and via `describe_entity`, building a client for every
attached agent.

Deterministic: `tests/e2e/rbac/test_entity_multi_agent_params.py` (3 PASS) —
two sqlite agents, code addressing both `ds_clients["<agent>:<connection>"]`
keys with `:year` bound in each query; run API and `describe_entity` return
rows from both; a member needs access to BOTH agents before a values run
succeeds.

Live (Haiku, "Music Store" + a second agent "Music Store EU" over a modified
copy of the demo DB, report scoped to both agents):

| Step | Result |
|------|--------|
| *"Compare total invoice revenue by billing country between Music Store and Music Store EU for one year, side by side. Make the year a parameter, defaulting to 2023."* | `create_data` produced code reading `ds_clients["Music Store:…"]` and `ds_clients["Music Store EU:…"]`, both bound with `:year`. Save Query modal shows **Agents: Music Store, Music Store EU** and `Year · number · = 2023`; saved entity carries both agents + the parameter. |
| New report (home page, single pinned agent): *"Load the saved query "…" for 2022."* | `describe_entity(should_create=True, params={year: 2022})`, no `create_data`; 40 rows, 20 per store. |
| Entity page, Year = 2021 → Run | `POST /run {"params":{"year":"2021"}}` → 200, 44 rows, 22 per store, "ran with year = 2021". |

Screenshots: `media/pr/saved-query-params/save-modal-two-agents.png`,
`new-report-loads-two-agent-entity.png`, `entity-page-two-agents-run.png`.

Sandbox notes (not feature bugs): an agent created through `POST
/data_sources` has its tables inactive until activated
(`PUT /data_sources/{id}/update_tables_status`), so the planner ignores it
until then; and restarting the backend without `BOW_ENCRYPTION_KEY` makes
previously stored credentials/snapshots unreadable — set the key explicitly.

## UI wording

The Save Query / Suggest Query / Edit entity form now labels the attachment
picker **Agents** (was "Data Sources"), localized in en/es/he.
