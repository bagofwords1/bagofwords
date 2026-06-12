# `load_step` / `load_entity` in generated code — implementation plan

## Goal

Let the coder reuse data the user already has, directly inside `generate_df`:

```python
def generate_df(ds_clients, excel_files, load_step, load_entity):
    revenue  = load_entity("Monthly Revenue Model")  # published catalog entity
    previous = load_step("Raw Customer Data")         # a step from THIS report
    return revenue.merge(previous, on="customer_id")
```

Both accept an **id or a name**, and both resolve only to things the caller may
access:
- `load_step` → steps in the **current report** (Report → Query → default Step).
- `load_entity` → published entities whose **data sources the user can access**.

## Design spine: pre-resolve before exec

`exec()` runs in a worker thread with no event loop, and the sandbox AST-forbids
I/O. So DB access cannot happen lazily inside `generate_df`. Instead:

```
codegen ─▶ AST-scan final_code for load_step()/load_entity() literal args
        ─▶ async resolve only those refs → DataFrames (+ access checks)
        ─▶ pass {ref → df} registry into execute_code_async
        ─▶ execute_code builds load_step/load_entity closures over the registry
        ─▶ inject into generate_df namespace (name-based binding)
```

Resolution cost is **O(refs in code)** (typically 1–3 indexed single-row
lookups), never O(steps in org) — eager prefetch is impossible at scale
(thousands of steps).

**Discovery is split from resolution:**
- *Discovery* (bounded, for the prompt): steps already surfaced in this
  conversation's `message_context` + a small most-recent-N from the report;
  entities via the existing keyword-filtered `EntityContextBuilder` (`top_k`).
- *Resolution* (unbounded, indexed): `load_step("anything")` resolves against
  the report's full default-step set by id/name regardless of what the prompt
  listed.

## Canonical relationships (post-Widget)

Widget is transitional/deprecated. The chain is **Report → Query → Step**:
- `Query.report_id` (`models/query.py:14`) — owning report.
- `Query.default_step_id` (`:24`) — the published step.
- `Step.query_id` (`models/step.py:40`) — back-link.

Resolution joins `Step.query_id == Query.id` where `Query.report_id == report.id`;
the resolver never reads `Step.widget_id`.

## Data shapes (confirmed)

`step.data` and `entity.data` share the same grid:
```json
{"rows": [{col: val, ...}], "columns": [{"headerName","field"}], "info": {...}}
```
Reconstruct: `pd.DataFrame(data["rows"])` reordered to `[c["field"] for c in data["columns"]]`.

Caveats baked into v1 + documented in the prompt:
- **Cached** snapshot (not a re-run) — `should_rerun` deferred to a later version.
- **Row-capped** (~`limit_row_count`, default 1000).
- **String-typed** dates/decimals/UUIDs (from `to_json`) — treat as reference
  data; `pd.to_datetime`/`astype` if needed.

## Decisions locked

- Step scope: **default steps only** (no historical reruns).
- Miss behavior: **feed the retry loop** — an unresolved/forbidden ref becomes a
  `code_and_error_messages` entry so the coder regenerates with feedback.
- Scope: **both** `load_step` and `load_entity`, by **id or name**, access-checked.

---

## Work items

### 1. `backend/app/ai/code_execution/loadables.py` (new)

`LoadablesResolver(db, organization, report, current_user)`:

- `async list_for_discovery() -> dict` — bounded manifest for the prompt:
  - steps: `report.queries → query.default_step` with `status == "success"`,
    plus any step ids present in this turn's `message_context`; emit
    `{id, title, slug, columns, row_count}` (no `rows` blob).
  - entities: reuse `EntityContextBuilder.load_entities(require_source_assoc=True)`
    then drop any failing `user_can_access_data_source`.
- `async resolve(step_refs, entity_refs) -> {"steps":{ref:df}, "entities":{ref:df}, "errors":[str]}`:
  - steps: select `Step` join `Query` on `Step.query_id == Query.id` where
    `Query.report_id == report.id` and `Step.id == Query.default_step_id`;
    match `ref` as id → exact slug → exact title (latest `created_at` on
    collision). Reconstruct df.
  - entities: reuse `describe_entity._find_entity` resolution (id → slug →
    title → fuzzy), enforce access via `user_can_access_data_source` over
    `entity.data_sources` (mirrors `entity_service.get_entity:341-348`),
    require `published`. Reconstruct df from `entity.data`.
  - unresolved/forbidden refs → human-readable strings in `errors` (never raise).
- `_grid_to_df(data)` — shared reconstruction helper.

### 2. AST pre-scan (same module)

`extract_loadable_refs(code) -> (step_refs, entity_refs)`: walk AST for `Call`
nodes named `load_step`/`load_entity` with a single string-literal arg.
Non-literal args are ignored (prompt requires literals).

### 3. `backend/app/ai/code_execution/code_execution.py`

- `generate_and_execute_stream_v2` (`:1043`): add optional async
  `loadable_resolver_fn`. After `final_code` is generated (`:1101`) and before
  `execute_code_async` (`:1123`): `extract_loadable_refs` →
  `await loadable_resolver_fn(...)` → fold `errors` into
  `code_and_error_messages` (drives a regenerate when non-empty) → pass the
  registry down.
- `execute_code_async` (`:708`) + `execute_code` (`:575`): add
  `loadables: Optional[Dict] = None`.
- In `execute_code`, build `load_step`/`load_entity` closures over the registry
  and add to `local_namespace` (`:636-643`) beside `excel_files`. A miss inside
  the closure raises a clear message naming what's available (defensive; covers
  dynamic args that bypassed the pre-scan).
- Refactor `_invoke_generate_df` (`:684-706`) from positional-arity to
  **name-based binding**: inspect parameter names, pass only the declared
  injectables (`excel_files`, `http`, `load_step`, `load_entity`) by keyword,
  keeping `ds_clients`/`excel_files` positional. Backward compatible with the
  2-arg and 3-arg (`http`) forms.

### 4. `backend/app/ai/tools/implementations/create_data.py`

At the `generate_and_execute_stream_v2` call (`:1188`), construct
`LoadablesResolver` from `runtime_ctx` (`db`, `report`, `current_user`,
`organization`) and pass `resolver.resolve` as `loadable_resolver_fn`.

### 5. Rerun paths (so saved code keeps working)

`query_service.run_query_new_step` (`:230`) and `step_service.rerun_step`
(`:140`) call the executor directly — wire the same resolver there, else
`load_step`/`load_entity` are undefined on rerun.

### 6. Prompt + discovery section

- `coder.py` (`:405-542`): add a `load_step`/`load_entity` subsection —
  opt-in signature, **string-literal id-or-name only**, returns a DataFrame,
  cached/row-capped/string-typed caveats.
- New `StepsSection` (parallel to `context/sections/entities_section.py`) fed by
  `LoadablesResolver.list_for_discovery()`, rendered as `<available_steps>` in
  the coder prompt. Entities already render via `<entities>`; note they are now
  also runtime-loadable.

---

## Test plan (sandbox-feedback-loop)

**Layer 1 — deterministic pytest (`-m ai`, isolated DB).** Seed Report → Query
(`default_step_id`) → Step (`query_id`) with known `data`; drive
`execute_code_async` with hand-written `generate_df`:
- resolve by id / slug / title → same df; column order preserved.
- title collision → latest `created_at`.
- cross-report step id → miss.
- `load_entity`: seed entity + data source; resolve by name; second user lacking
  access → miss.
- `_invoke_generate_df` binding matrix: `(ds_clients, excel_files)`, `+http`,
  `+load_step`, `+load_step,load_entity`.
- AST scan: literal extracted, `load_step(var)` ignored.
- miss → error string lands in `code_and_error_messages`, triggers regenerate.

**Layer 2 — deterministic live API (curl + sqlite).** Via
`query.py:run_query_new_step` (accepts `code`): seed "Customer Sales" step on
chinook, POST a new step whose code calls `load_step("Customer Sales")` and
merges with a live query; assert `status=success`, `code` contains `load_step(`,
row count correct. Bad ref → `status=error` with ref-not-found `status_reason`.

**Layer 3 — agent loop (curl `/api/completions` + sqlite).** Turn 1 builds the
step; turn 2 ("using the Customer Sales step you just built …") should emit
`load_step(`. If the model re-queries instead, tune the `<available_steps>`
wording / guideline (and a temporary org instruction as a forcing function).
Layers 1–2 guarantee the call works once emitted.

**Layer 4 — access-scoping negative.** Second user with report access but not the
entity's data source → agent gets not-accessible error, never the data.

**Layer 5 — eval case.** YAML under `tests/evals/suites/` (tagged `load_step`):
turn-1 builds, turn-2 reuses; rules check the tool trace shows reuse and final
numbers match an independent join.

---

## Out of scope for v1

- Rerun mode (`should_rerun`) — cached only → no recursion, no extra SQL cost.
- Dynamic (non-literal) ref arguments in pre-resolution.
- Cross-report step loading.
