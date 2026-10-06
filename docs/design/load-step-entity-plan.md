# Plan: `load_step` / `load_entity` — Reuse previous results in code generation

## Motivation

Today the coder's generated function signature is:

```python
def generate_df(ds_clients, excel_files):
```

Steps store their execution output in `step.data` (JSON with rows/columns), and entities store theirs in `entity.data` (same format). But there's no way for generated code to **reference the output of a previously executed step or entity** — even though that data is already persisted.

This forces the coder to re-derive everything from raw SQL every time, even when the user says "take that table and pivot it by region."

### What this unlocks

- **Chaining steps** — step B transforms step A's output without re-running A's query
- **Cross-query composition** — merge outputs from two independent queries
- **Incremental refinement** — "pivot that table" → new step loads previous, no SQL re-run
- **Entity reuse in code** — published models/metrics become loadable DataFrames

---

## Design overview

```
planner_v2  ──sees──▶  steps in message_context + entities in entities_context
     │
     │ decides create_data with depends_on_steps / depends_on_entities
     ▼
create_data  ──resolves──▶  loads Step/Entity rows from DB
     │
     │ passes to coder as context + injects into runtime
     ▼
coder prompt  ──describes──▶  "Available steps/entities to load"
     │
     │ LLM generates code using load_step() / load_entity()
     ▼
code_execution  ──provides──▶  load_step / load_entity callables in exec namespace
     │
     │ generate_df(ds_clients, excel_files, load_step, load_entity)
     ▼
DataFrame returned
```

---

## Scoping rules

| Source | Scope | Rationale |
|--------|-------|-----------|
| **Steps** | Same **report** (all successful steps from report's queries) | Mirrors how `excel_files` are report-scoped |
| **Entities** | Same **data sources** as the report | Entity ↔ DataSource association already exists via `entity_data_source_association`. If a report has a data source, all entities linked to that data source are in scope |

No new access-control tables needed — both scopes derive from existing relationships.

---

## Changes by layer

### 1. Tool input schemas

**File:** `backend/app/ai/tools/schemas/create_data.py`

Add to `CreateDataInput`:

```python
depends_on_steps: Optional[List[str]] = Field(
    default=None,
    description="Step IDs or titles from message_context whose output this step should load"
)
depends_on_entities: Optional[List[str]] = Field(
    default=None,
    description="Entity IDs or titles from entity context whose output this step should load"
)
```

**File:** `backend/app/ai/tools/schemas/inspect_data.py`

Add to `InspectDataInput`:

```python
depends_on_steps: Optional[List[str]] = Field(
    default=None,
    description="Step IDs or titles to peek at directly (no SQL needed)"
)
depends_on_entities: Optional[List[str]] = Field(
    default=None,
    description="Entity IDs or titles to peek at directly"
)
```

---

### 2. Planner prompt directives

**File:** `backend/app/ai/agents/planner/prompt_builder.py`

Add directive in the PLAN TYPE DECISION FRAMEWORK section (around line ~121, near existing create_data/inspect_data guidance):

```
- **Reusing previous results**: If the user's request builds on a previously created
  step (visible in message_context with viz_id/query_id) or a known entity (visible
  in <entities>), include their IDs in `depends_on_steps` or `depends_on_entities`
  in the create_data arguments. The coder will then have access to load those results
  as DataFrames via `load_step()` / `load_entity()` instead of re-querying from scratch.
- For inspect_data: if the user asks to peek at a previous step's output or an entity,
  pass the ID in depends_on_steps / depends_on_entities. No SQL code generation needed —
  the tool will return the stored data directly.
```

Update the tool schema section to include the new fields so the planner sees them.

---

### 3. Step/entity resolution in `create_data` tool

**File:** `backend/app/ai/tools/implementations/create_data.py`

In `_run_stream_traced()`, after table resolution (~line 700-720), add a resolution phase:

```python
# --- Resolve loadable steps and entities ---
loadable_steps = {}    # {title_or_id: {"df_json": ..., "columns": [...], "row_count": int}}
loadable_entities = {}

if validated_input.depends_on_steps:
    loadable_steps = await self._resolve_steps(
        db, report, validated_input.depends_on_steps
    )

if validated_input.depends_on_entities:
    loadable_entities = await self._resolve_entities(
        db, report, validated_input.depends_on_entities
    )
```

New private methods:

#### `_resolve_steps(db, report, identifiers) -> dict`

- Query `Step` where `status='success'` and `widget.report_id = report.id`
- Match by `step.id`, `step.slug`, or `step.title` (case-insensitive)
- For each match, extract `step.data` (already has `rows` and `columns`)
- Return dict keyed by title: `{"Monthly Revenue": {"rows": [...], "columns": [...], "row_count": N}}`

#### `_resolve_entities(db, report, identifiers) -> dict`

- Query `Entity` where entity's data_sources overlap with report's data_sources
- Match by `entity.id`, `entity.slug`, or `entity.title`
- Filter to published entities only (`private_status='published'` or `global_status='approved'`)
- Return same format as steps

---

### 4. CodeGenContext extension

**File:** `backend/app/ai/schemas/codegen.py`

Add to `CodeGenContext`:

```python
loadable_steps_context: str = ""       # Rendered description of available steps
loadable_entities_context: str = ""    # Rendered description of available entities
loadable_steps_data: Dict[str, Any] = {}    # Raw data for runtime injection
loadable_entities_data: Dict[str, Any] = {} # Raw data for runtime injection
```

---

### 5. Coder prompt changes

**File:** `backend/app/ai/agents/coder/coder.py`

In both `generate_code()` and `data_model_to_code()`, add a new prompt section after the excel_files section:

```
<loadable_steps>
{loadable_steps_context}
</loadable_steps>

<loadable_entities>
{loadable_entities_context}
</loadable_entities>
```

Where the context is rendered like:

```
Available steps from this report (use load_step("title") to get a pandas DataFrame):
  - "Monthly Revenue": 1,200 rows, columns: [date, region, revenue, currency]
  - "Customer Segments": 45 rows, columns: [segment, count, avg_ltv]

Note: Step data is a snapshot from the last successful execution.
```

```
Available entities from connected data sources (use load_entity("title") to get a pandas DataFrame):
  - "Revenue Model" (model): 5,000 rows, columns: [date, product, amount]
  - "Active Users" (metric): 800 rows, columns: [user_id, last_login, plan]
```

Update the function signature instructions (currently at ~lines 473-489 and 737-749):

```
- Your function signature is: def generate_df(ds_clients, excel_files, load_step, load_entity)
- Use `load_step("Step Title")` to load a previously created step's output as a DataFrame.
- Use `load_entity("Entity Title")` to load a published entity's data as a DataFrame.
- These return pandas DataFrames directly. No SQL needed for loaded data.
```

---

### 6. Code execution runtime injection

**File:** `backend/app/ai/code_execution/code_execution.py`

#### Changes to `execute_code()` (line ~325)

Update signature:

```python
def execute_code(
    self,
    *,
    code: str,
    ds_clients: Dict,
    excel_files: List,
    captured_timings: Optional[List[dict]] = None,
    loadable_steps_data: Optional[Dict[str, Any]] = None,
    loadable_entities_data: Optional[Dict[str, Any]] = None,
) -> Tuple[pd.DataFrame, str, List[str]]:
```

Build callable closures before exec:

```python
def _make_load_step(steps_data):
    def load_step(title_or_id: str) -> pd.DataFrame:
        key = title_or_id
        # Try exact match, then case-insensitive
        match = steps_data.get(key)
        if not match:
            key_lower = key.lower()
            for k, v in steps_data.items():
                if k.lower() == key_lower:
                    match = v
                    break
        if not match:
            raise ValueError(
                f"Step '{title_or_id}' not found. "
                f"Available: {list(steps_data.keys())}"
            )
        return pd.DataFrame(match["rows"])
    return load_step

def _make_load_entity(entities_data):
    # Same pattern as load_step
    ...
```

Update `local_namespace` (line ~353):

```python
local_namespace = {
    'pd': pd,
    'np': np,
    'db_clients': wrapped_clients,
    'excel_files': excel_files,
}
```

Update `generate_df` call (line ~364):

```python
generate_df = local_namespace.get('generate_df')
if not generate_df:
    raise Exception("No generate_df function found in code")

# Inspect function signature to determine call convention
import inspect
sig = inspect.signature(generate_df)
params = list(sig.parameters.keys())

if len(params) >= 4:
    df = generate_df(wrapped_clients, excel_files, load_step_fn, load_entity_fn)
elif len(params) >= 3:
    df = generate_df(wrapped_clients, excel_files, load_step_fn)
else:
    # Backward compatible: old code without load_step/load_entity
    df = generate_df(wrapped_clients, excel_files)
```

> **Backward compatibility**: existing steps with 2-param `generate_df` keep working.
> New code generated after this change will use the 4-param signature.

---

### 7. `inspect_data` tool — direct peek at step/entity data

**File:** `backend/app/ai/tools/implementations/inspect_data.py`

In `run_stream()`, add an early exit path before code generation (~line 60):

```python
# If depends_on_steps or depends_on_entities provided, skip code generation
# and return the stored data directly as inspection output
if validated_input.depends_on_steps or validated_input.depends_on_entities:
    return await self._inspect_stored_data(
        db, report, validated_input, runtime_ctx
    )
```

New method `_inspect_stored_data()`:
- Resolve steps/entities same as create_data
- Format as inspection output: column names, dtypes, row count, sample rows (respecting `allow_llm_see_data`)
- Return `ToolEndEvent` with the summary — no coder or executor involved
- This makes "what did that step return?" instant

---

### 8. Wiring through `create_data` tool to coder

**File:** `backend/app/ai/tools/implementations/create_data.py`

In `_run_stream_traced()`, after resolving steps/entities, update the context building:

```python
# Build loadable context descriptions
loadable_steps_context = self._render_loadable_steps(loadable_steps)
loadable_entities_context = self._render_loadable_entities(loadable_entities)

# Pass to CodeGenContext
codegen_context = CodeGenContext(
    ...existing fields...,
    loadable_steps_context=loadable_steps_context,
    loadable_entities_context=loadable_entities_context,
    loadable_steps_data=loadable_steps,       # for runtime
    loadable_entities_data=loadable_entities,  # for runtime
)
```

When calling `StreamingCodeExecutor.execute_code()`:

```python
exec_df, execution_log, _ = executor.execute_code(
    code=step.code,
    ds_clients=ds_clients,
    excel_files=excel_files,
    loadable_steps_data=codegen_context.loadable_steps_data,
    loadable_entities_data=codegen_context.loadable_entities_data,
)
```

---

### 9. `prompt_builder` — surface loadable steps/entities to planner

**File:** `backend/app/ai/agents/planner/prompt_builder.py`

The planner already sees:
- **Steps** via `message_context` (tool digests with viz_id, query_id, column info)
- **Entities** via `entities_context` (from EntityContextBuilder)

No new context section needed. The planner has enough info to populate `depends_on_steps` / `depends_on_entities` from what it already sees.

Only change: add the directive text (section 2 above) and ensure tool schemas include the new fields.

---

## Data flow example

User: "Show me monthly revenue by region"
→ Planner calls `create_data(title="Monthly Revenue", ...)`
→ Coder generates SQL, step executes, `step.data` saved

User: "Now pivot that by region"
→ Planner sees in `message_context`: `create_data → query_id: abc, 1200 rows × 4 cols, cols: date, region, revenue`
→ Planner calls `create_data(title="Revenue by Region", depends_on_steps=["abc"], ...)`
→ `create_data` resolves step `abc`, finds 1200 rows
→ Coder prompt includes: `load_step("Monthly Revenue") → 1200 rows, [date, region, revenue, currency]`
→ Coder generates:

```python
def generate_df(ds_clients, excel_files, load_step, load_entity):
    df = load_step("Monthly Revenue")
    pivot = df.pivot_table(index='date', columns='region', values='revenue', aggfunc='sum')
    return pivot.reset_index()
```

→ `execute_code()` provides `load_step` closure that reads from `step.data["rows"]`
→ New DataFrame returned, formatted, saved as new step

---

## Entity example

User: "Compare active users entity against our monthly revenue"
→ Planner sees entity "Active Users" in `<entities>` context
→ Planner sees step "Monthly Revenue" in `message_context`
→ Planner calls `create_data(depends_on_steps=["Monthly Revenue"], depends_on_entities=["Active Users"], ...)`
→ Coder generates:

```python
def generate_df(ds_clients, excel_files, load_step, load_entity):
    revenue = load_step("Monthly Revenue")
    users = load_entity("Active Users")
    merged = revenue.merge(users, left_on='date', right_on='last_login', how='inner')
    return merged[['date', 'region', 'revenue', 'user_id', 'plan']]
```

---

## Edge cases

| Case | Handling |
|------|----------|
| Step not found | `load_step()` raises `ValueError` with available step names — coder retry can fix |
| Entity not in scope | Resolution phase filters by report's data sources — entity excluded, coder doesn't see it |
| Empty step data | `load_step()` returns empty DataFrame — code handles naturally |
| Step in error status | Only `status='success'` steps are resolvable |
| Circular dependency | Not possible — `load_step` reads persisted data, doesn't re-execute. Step A loading step B is just a JSON read |
| Backward compat | `execute_code()` inspects `generate_df` signature — 2-param old code still works |
| Multiple steps with same title | Resolve to the **latest successful** step (order by `updated_at DESC`) |

---

## What we're NOT doing in v1

| Deferred | Rationale |
|----------|-----------|
| `refresh=True` parameter | Re-executing upstream steps adds dependency resolution complexity (needs their `ds_clients`, handles cascading refresh). Users can manually re-run upstream first. Revisit in v2. |
| Cross-report step loading | Report scope is sufficient. Cross-report adds permission complexity. |
| Agent-scoped entity filtering | Existing data source filtering on entities is already implicit agent scoping. If agent's report only has certain data sources, entities are already scoped. |
| Step dependency tracking in DB | No formal DAG — `depends_on` is a hint for the coder, not a persisted relationship. Can add later if needed for lineage/refresh. |

---

## Files changed (summary)

| # | File | Change |
|---|------|--------|
| 1 | `ai/tools/schemas/create_data.py` | Add `depends_on_steps`, `depends_on_entities` to `CreateDataInput` |
| 2 | `ai/tools/schemas/inspect_data.py` | Add `depends_on_steps`, `depends_on_entities` to `InspectDataInput` |
| 3 | `ai/schemas/codegen.py` | Add `loadable_steps_context`, `loadable_entities_context`, `loadable_steps_data`, `loadable_entities_data` to `CodeGenContext` |
| 4 | `ai/agents/planner/prompt_builder.py` | Add directive about `depends_on` usage |
| 5 | `ai/tools/implementations/create_data.py` | Add `_resolve_steps()`, `_resolve_entities()`, `_render_loadable_*()`, wire through to coder + executor |
| 6 | `ai/tools/implementations/inspect_data.py` | Add early-exit path for stored data peek via `_inspect_stored_data()` |
| 7 | `ai/agents/coder/coder.py` | Add `<loadable_steps>` / `<loadable_entities>` prompt sections, update function signature instructions |
| 8 | `ai/code_execution/code_execution.py` | Add `load_step`/`load_entity` closures, update `execute_code()` signature, backward-compat `generate_df` dispatch |

---

## Implementation order

1. **Schema layer** (files 1-3) — no runtime impact, just new fields
2. **Code execution** (file 8) — `load_step`/`load_entity` closures + backward-compat dispatch
3. **Coder prompt** (file 7) — new prompt sections + updated signature instructions
4. **create_data tool** (file 5) — resolution + wiring (this is the biggest change)
5. **inspect_data tool** (file 6) — stored data peek shortcut
6. **Planner directive** (file 4) — teach planner when to use `depends_on`
7. **Test** — end-to-end: create step A, then create step B that loads A
