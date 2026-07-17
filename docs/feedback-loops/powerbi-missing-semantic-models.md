# Feedback Loop — Power BI: "not all the semantic models are selectable in schema, some are missing from the fetch"

Reported against a live Entra tenant (SP-seeded catalog + OBO users): some
Power BI semantic models that are visible in app.powerbi.com never show up as
selectable tables in the schema step, with no error anywhere. This loop
validates the claim that models are **silently dropped** at three independent
places in the fetch pipeline, and pins each mechanism with a deterministic
reproduction (no live credentials needed).

Status: **reproduction only — no fix applied yet.** The invariant tests are
committed as `strict xfail`; whoever lands the fix flips them to plain asserts
by deleting the markers.

---

## Root cause (validated)

The selectable list is whatever `PowerBIClient.get_schemas()` emits into the
canonical catalog (plus, for OBO users, an overlay filter on top). A semantic
model can fall out at each stage:

1. **Admin-scan shadowing** — `_batch_admin_scan` records an entry for every
   dataset present in the scan result, including datasets the Scanner API
   returned **no schema** for (model not refreshed since enhanced-metadata
   scanning was enabled, DirectLake, all tables `isHidden`). Because
   `get_schemas()` treats "dataset id present in admin scan results" as final
   (`backend/app/data_sources/clients/powerbi_client.py:770-773`), the
   COLUMNSTATISTICS fallback is **never attempted** for those datasets — even
   when it would have worked. The model vanishes.

2. **Silent fallback failure** — without admin-API rights every dataset relies
   on `EVALUATE COLUMNSTATISTICS()` via `executeQueries`, which needs **Build**
   permission and fails for RLS-protected / Viewer-only / some DirectLake
   models. The failure is swallowed into `([], [])`
   (`powerbi_client.py:523-525`), the REST `/tables` fallback only answers for
   Push datasets (`powerbi_client.py:472-477`), and Phase 4 emits rows only by
   iterating discovered tables (`powerbi_client.py:812`) — a dataset with zero
   tables produces **zero schema rows and no surfaced error** (a
   `logging.warning` is the only trace).

3. **Overlay ∩ canonical intersection (OBO)** — for a `user_required` + oauth
   connection, a signed-in user's selector is scoped to their overlay rows
   with `data_source_table_id IS NOT NULL`
   (`backend/app/services/data_source_service.py:2668-2676`), but
   `_upsert_user_overlay` links overlay rows to canonical `DataSourceTable`
   rows **by name only** and leaves `NULL` on a miss
   (`data_source_service.py:3400`). Power BI is a `shared`-catalog connector
   (`backend/app/schemas/data_source_registry.py:870` — registry default), so
   the per-user "create canonical rows on demand" escape hatch does not apply.
   Net effect: a model the user's own token can crawl but the service
   principal cannot (SP not a Member of that workspace, or dropped by 1/2
   above) is fetched into the overlay **and then filtered out of the
   selector**.

Additional scoping facts (validated by API semantics, not loop-reproducible):
`list_workspaces` uses `GET /v1.0/myorg/groups` (`powerbi_client.py:356`),
which only returns workspaces where the identity holds a workspace role —
datasets shared directly (dataset-level Build/Read) and "My Workspace" content
never enter the crawl at all.

---

## Loop A — deterministic reproduction (no external services)

All Power BI HTTP traffic is faked at the `requests.Session` boundary; every
line of client/service logic runs real. Overlay leg runs on SQLite via the
standard test harness.

```bash
cd backend
export BOW_DATABASE_URL="sqlite:///db/app.db"
TESTING=true uv run pytest \
  tests/unit/test_powerbi_missing_models_repro.py \
  tests/e2e/test_powerbi_overlay_intersection_repro.py \
  -v -s --runxfail
```

**Observed (2026-07-17, current code):**

Mechanism 1 — admin scan covers a dataset with no schema, fallback never tried:

```
[repro] emitted schema tables: ['CoveredModel/Sales']
[repro] COLUMNSTATISTICS attempted for d2: False
AssertionError: dataset covered by the admin scan without schema was dropped
                instead of falling back to COLUMNSTATISTICS
  assert 'ShadowedModel/Facts' in ['CoveredModel/Sales']
```

Mechanism 2 — COLUMNSTATISTICS forbidden (no Build), not a Push dataset:

```
WARNING root:powerbi_client.py:524 COLUMNSTATISTICS failed for dataset d1:
        DAX query failed: HTTP 403 {"error": {"code": "PowerBINotAuthorizedException"}}
[repro] emitted schema tables: ['ReadableModel/Orders']
AssertionError: dataset with failed table introspection was silently dropped from the schema
```

(`test_current_behavior_drops_are_silent_no_exception` PASSES today — it pins
that both drops complete without raising: the caller just gets a smaller
schema.)

Mechanism 3 — overlay intersection (signed-in OBO user, SP catalog narrower
than the user's access):

```
[repro] user's live fetch returned:   ['ModelA/T1', 'ModelB/T2']
[repro] overlay rows (name, linked-to-canonical): [('ModelA/T1', True), ('ModelB/T2', False)]
[repro] selectable tables in schema:  ['ModelA/T1']
AssertionError: model returned by the user's own fetch is not selectable —
                dropped by the overlay∩canonical intersection
  assert 'ModelB/T2' in ['ModelA/T1']
```

Without `--runxfail` the same run is green (`1 passed, 3 xfailed`) — the
invariants are recorded as strict xfails so CI stays green until the fix.

## Loop B — live confirmation (optional, real tenant)

Only needed to confirm which mechanism is eating a *specific* model in a real
tenant. Secrets via env vars only (`BOW_ENTRA_*`, `BOW_PBI_MASTER_*` — same
set as `fabric-powerbi-obo-creator-zero-tables.md`). Triage per model:

1. Present in `GET /connections/{id}/tables` (canonical, SP view)?
   - **No**, but visible in app.powerbi.com → mechanism 1/2 on the SP crawl
     (check backend logs for `COLUMNSTATISTICS failed for dataset …`), or the
     SP isn't a Member of that workspace (membership scoping).
   - **Yes**, but missing from a signed-in user's selector → mechanism 3
     only bites the other direction (user-visible, SP-invisible); check the
     user's `UserDataSourceTable` rows for `data_source_table_id IS NULL`.

## The fix

Not applied (reproduction requested only). Candidate directions, per
mechanism:

1. Fall back to COLUMNSTATISTICS whenever the admin scan yielded an **empty**
   table list for a dataset, not just when the dataset id is absent.
2. Emit a dataset-level placeholder row (or surface a per-dataset warning in
   the indexing job) when every introspection path fails, so the model stays
   visible/selectable instead of disappearing.
3. Union user-crawled tables into the selector for the overlay identity (or
   create canonical rows on demand as the `per_user` catalogs already do),
   instead of intersecting with the SP catalog by name.

When a fix lands: delete the `xfail` markers in the two repro test files and
re-run Loop A — all four tests must pass.

## What this proves / regression notes

- Datasets are **listed** correctly in every reproduced case — the loss is in
  table introspection and in the overlay link step, which matches the report
  ("some are missing from the fetch" while others from the same tenant show
  up).
- All three mechanisms are silent by construction: no exception reaches the
  caller, nothing is surfaced to the UI (mechanism 2 leaves only a
  `logging.warning`).
- Pre-existing suites unaffected: `tests/unit/test_powerbi_client.py`
  (36 passed) on the same run.
- Sibling loops: `fabric-powerbi-obo-creator-zero-tables.md` (zero tables
  before first sign-in — different mechanism, already fixed),
  `powerbi-dataset-id-resolution.md`.
