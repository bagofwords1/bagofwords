# Repro & feedback loop — "metric broken down by a dimension renders as a single line"

Sandbox-ready reproduction harness for the bug reported in `#bow-joom`:

> When we ask BoW to build a metric in dynamics broken down by any dimension
> (e.g. *gmv by categories*), it ignores the breakdown and builds just **one
> line for the total**. At the same time, on the SQL level we see grouping by
> category.

This doc gives you a two-tier feedback loop to reproduce, diagnose, and (later)
verify a fix — entirely inside the sandbox, no Docker required.

---

## Root cause (one paragraph)

The breakdown is lost in `create_data`'s post-execution **visualization
inference**, not in SQL generation. The viz-inference LLM is prompted to emit
`group_by` as a **string** (`backend/app/ai/tools/implementations/create_data.py`
— every example and the OUTPUT FORMAT use `"group_by": "column_name_or_null"`).
That candidate is fed into `DataModel(**candidate_json)`, but
`DataModel.group_by` is typed `Optional[List[str]]`
(`backend/app/ai/tools/schemas/create_data_model.py:158`). Pydantic v2 does
**not** coerce a bare `str` into `List[str]` — it raises — and the surrounding
`except` resets the whole candidate to `{"type": "table", "series": []}`,
discarding `group_by` **and** the series. Downstream, `build_view_from_data_model`
gets no series, returns `None`, and the view falls back to a bare
`{"type": "line_chart"}` — which the frontend renders as a single total line.

There's also a type disagreement across the stack worth noting when fixing:

| Layer | File | Treats `group_by` as |
|---|---|---|
| Inference prompt | `implementations/create_data.py` | string |
| `DataModel` schema | `schemas/create_data_model.py:158` | `List[str]` |
| View schema `groupBy` | `schemas/view_schema.py:140` | `str` |
| Chart codegen | `services/artifact_codegen.py:45,77,86` | string |

---

## One-time environment setup

The codebase needs **Python 3.12+** (3.11 trips an f-string-with-backslash
`SyntaxError` in `app/ai/agents/planner/planner.py`). `psycopg2` won't build
without `libpq`, but tests default to **sqlite**, so we use `psycopg2-binary`.

```bash
cd backend
python3.12 -m venv venv
source venv/bin/activate
pip install --upgrade pip

# Install everything except the source psycopg2, then the prebuilt binary.
grep -iv '^psycopg2==' requirements_versioned.txt > /tmp/reqs_nopsy.txt
pip install -r /tmp/reqs_nopsy.txt
pip install psycopg2-binary
```

### Required environment variables

Run the e2e test exactly like CI does (`ENVIRONMENT=production TESTING=true`,
which loads `bow-config.yaml` with `auth.mode: hybrid` so local password
registration works):

```bash
export ENVIRONMENT=production
export TESTING=true
# Any valid Fernet key — used to encrypt the stored provider credential.
export BOW_ENCRYPTION_KEY="$(python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"
# Your Anthropic key. NEVER commit this.
export ANTHROPIC_API_KEY_TEST="sk-ant-..."
```

> The test installs an Anthropic provider with **Haiku**
> (`claude-haiku-4-5-20251001`) as the org default — the bug is
> model-independent (it's a schema/validation mismatch, not a planning
> failure), so Haiku keeps the loop cheap and fast (~40s/run). Override with
> `BOW_REPRO_MODEL=<model_id>`.

---

## The feedback loop

### Inner loop — fast, deterministic, no API key (~30s)

`backend/tests/unit/test_repro_group_by_dropped.py` pins the exact mechanism
with no LLM and no network: a string `group_by` (what the planner emits) is
rejected by `DataModel`, collapsing the candidate to a bare table.

```bash
cd backend && source venv/bin/activate
export BOW_DATABASE_URL="sqlite:///db/app.db"   # any string; harness overrides the real DB
python -m pytest tests/unit/test_repro_group_by_dropped.py -v
```

Expected on current `main` (bug present):

```
test_datamodel_rejects_string_group_by ............... PASSED
test_create_data_construction_drops_the_breakdown ... PASSED
test_planner_string_group_by_survives ............... XFAIL   <-- desired behavior, currently failing
====== 2 passed, 1 xfailed ======
```

`test_planner_string_group_by_survives` is the canary: it's `xfail(strict=True)`,
so **once the bug is fixed it flips to XPASS and the suite fails**, prompting you
to delete the marker. Use this loop while iterating on the fix.

### Outer loop — end-to-end against Chinook + real model (~40s)

`backend/tests/e2e/test_repro_viz_breakdown.py` drives the **real agent**
through the HTTP API against the committed `demo-datasources/chinook.sqlite`,
then inspects the chart that was actually persisted on the widget
(`last_step.data_model` + `last_step.view`).

```bash
cd backend && source venv/bin/activate
# (env vars from the setup section above must be exported)
python -m pytest tests/e2e/test_repro_viz_breakdown.py -s -m e2e
```

The prompt mirrors the customer's "metric broken down by category":

> *Plot total sales over time broken down by music genre, as a LINE chart with
> one line per genre … Join Invoice → InvoiceLine → Track → Genre …*

---

## Reproduced output (current `main`, Haiku)

```
========== REPRO DIAGNOSTIC: viz breakdown ==========
widgets produced: 1
{
  "title": "Sales by Genre Over Time (Monthly)",
  "type": "line_chart",
  "columns": ["Month", "GenreName", "TotalSales"],   <-- SQL DID group by genre
  "total_rows": 351,                                  <-- tall: many rows per month
  "x_key": null,
  "value_keys": [],
  "series_count": 0,                                  <-- series dropped
  "group_by": null,                                   <-- THE BUG: breakdown lost
  "view_group_by": null,
  "breakdown_dim_present": true,
  "breakdown_applied": false
}
=====================================================

AssertionError: Metric breakdown was lost on the chart — the data is granular
along a category dimension, but the chart applies no group_by / multi-series,
so it renders a single total line.

WARNING app.project_manager update_visualization_view failed: 2 validation
errors for ViewSchema
  view.line_chart.x  Field required ... input_value={'type': 'line_chart'}
  view.line_chart.y  Field required ...
```

The trailing `ViewSchema` warning is the tail of the same chain: with series
dropped, the view degrades to a bare `{"type": "line_chart"}` (no `x`/`y`),
and the frontend auto-renders a single line.

---

## What "fixed" looks like

After the fix (normalize `group_by` so the planner's choice survives into the
data model and the view), re-run both loops:

- **Inner:** `test_planner_string_group_by_survives` goes from `xfail` →
  `xpass`; remove the `@pytest.mark.xfail` marker so it's a plain green
  regression guard.
- **Outer:** the e2e diagnostic shows `group_by` (or `view_group_by`) set to
  the category column, `breakdown_applied: true`, and the test passes —
  i.e. the chart renders one line per genre.

---

## Files

| File | Role |
|---|---|
| `backend/tests/unit/test_repro_group_by_dropped.py` | Fast, key-free root-cause check (inner loop) |
| `backend/tests/e2e/test_repro_viz_breakdown.py` | End-to-end Chinook + real-model repro (outer loop) |
| `REPRO-viz-breakdown.md` | This guide |
