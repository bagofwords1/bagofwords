# Plan + Sandbox Feedback Loop — create_data preview budget

Fixes the reported bug: **prompt builder v3 sometimes shows only 5 rows of a
create_data result even when 10 (or more) were generated.** The full data is
persisted on the step; only the *prompt-facing preview* was truncated.

This doc is both the implementation plan and the runnable manual-verification
loop (no automated tests — manual only, per request).

**Status: implemented (single commit).** A read_query pagination follow-up was
scoped but intentionally deferred (see "Deferred" below) — it may not be needed.

Loop A (below) was run in the sandbox and passes — observed output inline.

---

## Root cause (confirmed in code)

The generated rows are real and persisted (`step.data["rows"]`, up to
`limit_row_count`, default **1000**). But the paths that render a step back into
the LLM prompt truncated to **5 rows**, silently:

| Site | What it did | File:line |
|---|---|---|
| create_data preview | `rows[:5]` into the observation | `backend/app/ai/tools/implementations/create_data.py:1444` |
| observation compaction | **deleted** `data_preview` on the next tool call | `backend/app/ai/context/builders/observation_context_builder.py:66-69` |
| deep minify | dropped `data_preview` for obs >5 back | `backend/app/ai/agents/planner/prompt_builder.py` (`_OBS_KEEP_KEYS`) |

The observation only ever carried `data_preview` — never the full rows
(`create_data.py:1544`). The full rows live in `output.data`, which goes to the
widget/persistence, not the planner context.

### Confirmed data shape (`step.data`, JSON column)

`format_df_for_widget` (`code_execution.py:903-958`) produces:

```python
{
  "rows":    [ {field: value, ...}, ... ],   # orient='records', up to limit_row_count (1000)
  "columns": [ {"headerName": str, "field": str}, ... ],
  "info":    { "total_rows": N, "column_info": { col: {dtype, null_count, unique_count, ...} } },
}
```

So `info.total_rows` (the true total) and per-column stats are **free** — already
computed.

---

## Design

Mirror the existing `read_artifact` behavior (full `code` in the latest
observation, held one iteration, then compacted — `read_artifact.py:268`):

1. **Generous latest window (Snowflake-style byte budget).** The *latest*
   create_data observation carries full rows up to a byte budget
   (`DEFAULT_PREVIEW_BUDGET_BYTES = 48 KB`, ~12k tokens — read_artifact-scale,
   well under Snowflake's 250 KB ceiling). Small results (the common case) come
   through **whole**. Larger results degrade to **head + tail** with the true
   `row_count` and a note. The byte budget is a true ceiling (inter-row
   separators counted).
2. **Sample, don't delete, on compaction.** Older observations keep a 3-row
   labeled sample (`sampled`, `note`, `row_count`) instead of dropping rows.
3. **Preserve the sample through the deep minify** by adding `data_preview` to
   `_OBS_KEEP_KEYS`.

---

## Implementation (single commit)

1. **New `backend/app/ai/context/data_preview.py`** — `build_data_preview()` and
   `SAMPLE_ROWS`. Budgeted, self-describing preview; single source of truth
   replacing the scattered `[:5]` slices. Privacy-off path returns columns +
   `row_count` + stats only (today's behavior).
2. **`create_data.py:1438-1451`** — replace the `rows[:5]` block with
   `build_data_preview(formatted, allow_llm_see_data=…)`. (The viz-inference
   `head_rows[:5]` at `:493` is left as-is — intentionally tiny, used only for
   chart-type inference.)
3. **`observation_context_builder.py:67-80`** — replace `del data_preview` with a
   3-row labeled sample (`sampled` + `note`); leave privacy-off previews (no
   rows) untouched.
4. **`prompt_builder.py` `_OBS_KEEP_KEYS`** — add `data_preview` so the sample
   survives the deeper minify.
5. **`prompt_builder_v3.py`** — planner guidance: previews may be partial; trust
   `row_count`, don't treat a sample/truncated preview as the full result.
6. **`read_query.py`** — reuse `build_data_preview` instead of `rows[:5]`, so
   reading a prior result returns the full budgeted set (e.g. all 30 rows of a
   "Top 30" result) rather than 5. `read_query` is added to the observation
   compaction loop (single result + each `results_summary` entry) so its now-full
   preview is sampled after one iteration like create_data.

---

## Verification (manual — spin up the sandbox)

Environment per `sandbox-feedback-loop.md`. App targets **Python 3.12**.

```bash
cd backend
pip install uv
uv sync --frozen --extra dev
export BOW_DATABASE_URL="sqlite:///db/app.db"
mkdir -p db
```

### Loop A — in-process logic check (no live data source)

Run in the synced venv (`uv run --directory backend python - <<'PY' … PY`):

1. **Budget helper** — `build_data_preview`:
   - 10-row result → `truncated=False`, **all 10 rows** (the reported bug — must
     now pass).
   - 2000-row wide → `truncated=True`, head+tail, serialized rows ≤ 48 KB.
2. **Compaction keeps a sample** — `ObservationContextBuilder`: add a
   create_data obs (full preview), then add another obs; assert the prior obs
   keeps a 3-row `data_preview` with `sampled`/`note` (not `del`'d).
3. **Keep-key** — assert `data_preview` ∈ `_OBS_KEEP_KEYS`.

**Observed (PASS) — run in sandbox:**
```
[budget] 10-row full; 2000-wide shown=245 note='showing first 188 and last 57 of 2000 rows'
[compact] latest full=40 -> older sample=3 note='sample of 3; 40 rows total'
[keepkey] data_preview preserved in deep minify
ALL PASS (core fix only)
```

> Byte budget verified as a true ceiling (inter-row separators counted) — all
> windows ≤ 48 KB.

### Loop B — full app, end to end

```bash
cd backend
export BOW_DATABASE_URL="sqlite:///db/app.db"
uv run python main.py            # uvicorn; serves API + SPA
```

1. Seed a data source with a table of **>50 rows**.
2. Prompt something that triggers `create_data` returning **>50 rows**.
3. Inspect the planner input (`<past_observations>` in `prompt_builder_v3`):
   the **latest** create_data observation carries the **full/budgeted** rows
   (not 5), truncated only if it exceeded budget.
4. Send a follow-up so a new tool runs; re-inspect — the now-older create_data
   observation shows a **3-row sample + note**, still carrying `row_count`.

**What this proves:** the latest result is shown in full (read_artifact-scale),
older results degrade to a labeled sample, and `row_count` always reflects the
true total — over data already sitting in `step.data["rows"]`.

---

## Deferred — read_query pagination (not in this change)

`read_query` now shares the budgeted preview and the compaction loop (above), so
it reads the **full** result under the same cap. Only explicit **paging**
(`step_id` + `offset`/`limit` to scroll beyond the byte budget) was left out — the
budgeted read covers results that fit ~48 KB, which is the common case, and the
paging path may not be needed. If revisited it's additive: `read_query` already
loads the full `step.data` into `output.data`; add a single `step_id` +
optional `offset`/`limit` while `query_ids`/`visualization_ids` stay multi-id
batch reference.

## Rollout / safety

- Byte budget + sample size are constants in one module — easy to tune.
- Hard ceiling: previews top out at what was persisted (`limit_row_count`,
  default 1000).
