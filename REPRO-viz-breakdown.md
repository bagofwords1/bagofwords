# Fix & feedback loop — "metric broken down by a dimension renders as a single line"

Reproduction + fix for the bug reported in `#bow-joom`:

> When we ask BoW to build a metric in dynamics broken down by any dimension
> (e.g. *gmv by categories*), it ignores the breakdown and builds just **one
> line for the total**. At the same time, on the SQL level we see grouping by
> category.

Status: **fixed**. Both feedback loops below are green.

---

## Root cause

The breakdown was lost in `create_data`'s post-execution **visualization
inference** (`backend/app/ai/tools/implementations/create_data.py`), not in SQL
generation. The SQL was always correct (the result is granular: many rows per
x-axis value, one per category). Two layered defects threw the breakdown away:

1. **Brittle JSON parse (the one that actually bit in practice).** The
   inference asks the model for "only valid JSON", but models routinely wrap it
   in ```` ```json ```` fences and append a prose *Rationale*. The code did a
   bare `json.loads(raw)`, which throws on that, and the `except` set
   `candidate_json = None` → the candidate fell back to
   `{"type": "table", "series": []}`. Observed verbatim with Haiku: a perfect
   `{"type":"line_chart","series":[…],"group_by":"GenreName"}` payload, discarded
   because of the surrounding fence + rationale.

2. **Schema type drop (latent, behind #1).** Even with clean JSON, the inference
   prompt emits `group_by` as a **string**, but `DataModel.group_by` was typed
   `Optional[List[str]]`. Pydantic v2 rejects a bare `str`, and the same
   `except` collapsed the candidate to a table — dropping `group_by` and the
   series.

With series + `group_by` gone, `build_view_from_data_model` returned `None`, the
view degraded to a bare `{"type": "line_chart"}` (no `x`/`y`/`groupBy`), and the
frontend rendered a single total line.

There was also a stack-wide disagreement on `group_by`'s shape (the inference
prompt and the ECharts codegen treat it as a single string; `DataModel` and
`create_widget` use a list; `view.groupBy` is a single string).

## The fix

`backend/app/ai/tools/implementations/create_data.py`
- `_extract_json_object()` — tolerant extraction: direct parse → strip code
  fences → scan for the first balanced `{…}` object (drops trailing prose).
  Used in place of `json.loads(raw)` in the viz-inference pass.
- `build_view_from_data_model()` — normalize `group_by` to a single column name
  before constructing the view.

`backend/app/ai/tools/schemas/create_data_model.py`
- `group_by` is now `Optional[Union[str, List[str]]]` so the planner's string
  validates instead of nuking the candidate.
- `normalize_group_by()` — shared helper that flattens str/list/empty → a single
  column name (charts render one breakdown dimension).

`backend/app/services/artifact_codegen.py`
- Normalize `group_by` before embedding it as a JS key, so a list can never
  reach `_js_str` (it would have crashed).

---

## Environment setup (sandbox)

Needs **Python 3.12+** (3.11 trips an f-string `SyntaxError` in
`planner.py`). `psycopg2` won't build without `libpq`, but tests default to
**sqlite**, so use `psycopg2-binary`.

```bash
cd backend
python3.12 -m venv venv
source venv/bin/activate
pip install --upgrade pip
grep -iv '^psycopg2==' requirements_versioned.txt > /tmp/reqs_nopsy.txt
pip install -r /tmp/reqs_nopsy.txt
pip install psycopg2-binary
```

Env vars (the e2e test mirrors CI: `ENVIRONMENT=production TESTING=true` loads
`bow-config.yaml` with `auth.mode: hybrid` so password registration works):

```bash
export ENVIRONMENT=production
export TESTING=true
export BOW_ENCRYPTION_KEY="$(python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"
export ANTHROPIC_API_KEY_TEST="sk-ant-..."   # NEVER commit this
```

The e2e test installs an Anthropic provider with **Haiku**
(`claude-haiku-4-5-20251001`) as the org default — the bug was
model-independent. Override with `BOW_REPRO_MODEL=<model_id>`.

---

## The feedback loop

### Inner loop — fast, deterministic, no API key

`backend/tests/unit/test_repro_group_by_dropped.py` pins both mechanisms with no
LLM and no network: tolerant JSON extraction (fences + prose), the schema
accepting a string `group_by`, and `normalize_group_by`.

```bash
cd backend && source venv/bin/activate
export BOW_DATABASE_URL="sqlite:///db/app.db"   # any string; harness overrides the real DB
python -m pytest tests/unit/test_repro_group_by_dropped.py -v
```

### Outer loop — end-to-end against Chinook + real model

`backend/tests/e2e/test_repro_viz_breakdown.py` drives the **real agent** through
the HTTP API against the committed `demo-datasources/chinook.sqlite`, with the
prompt "total sales over time broken down by music genre, one line per genre",
then asserts the breakdown survived to **both** the persisted `data_model` and
the rendered `Visualization.view` (the layer the customer sees).

```bash
cd backend && source venv/bin/activate
# (env vars from the setup section above must be exported)
python -m pytest tests/e2e/test_repro_viz_breakdown.py -s -m e2e
```

---

## Before / after (Chinook, Haiku)

**Before** — SQL grouped by genre (`351 rows`), but the chart dropped it:

```
"columns": ["Month", "GenreName", "TotalSales"], "total_rows": 351
"series_count": 0, "group_by": null, "breakdown_applied": false
WARNING update_visualization_view failed: view.line_chart.x/y Field required
```

**After** — the breakdown reaches the rendered visualization view:

```
"series_count": 1, "group_by": "GenreName", "x_key": "Month", "breakdown_applied": true

VIZ VIEWS: [{"version":"v2","view":{"type":"line_chart","x":"YearMonth",
  "y":"TotalSales","groupBy":"GenreName","legend":{"show":true,...},
  "seriesStyles":[{"key":"TotalSales","label":"Total Sales","aggregation":"sum"}]}}]
```

`groupBy: "GenreName"` → one line per genre. The `update_visualization_view`
warning is gone.

---

## Files

| File | Role |
|---|---|
| `backend/app/ai/tools/implementations/create_data.py` | Fix: tolerant JSON parse + group_by normalization in the view builder |
| `backend/app/ai/tools/schemas/create_data_model.py` | Fix: `group_by` accepts str-or-list + `normalize_group_by` helper |
| `backend/app/services/artifact_codegen.py` | Fix: normalize `group_by` before JS embedding |
| `backend/tests/unit/test_repro_group_by_dropped.py` | Inner-loop regression guard (no key) |
| `backend/tests/e2e/test_repro_viz_breakdown.py` | Outer-loop end-to-end repro (Chinook + real model) |
| `REPRO-viz-breakdown.md` | This guide |
