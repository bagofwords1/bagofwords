# Feedback Loop — "Does the agent see the measures inside the app's charts, or only Master Items?"

A Qlik Sense on-prem connection indexed the data model and the app's master
measures/dimensions, but nothing about the sheets the app's authors built: a
measure written inline on a chart (the common case) was invisible to the agent,
variables were stored but never rendered, and the agent could not read a chart's
own numbers or check how a value is spelled. This loop validates the gap, then
verifies the change that closes it: sheet objects in the catalog, variables in
the prompt, `execute_query(object_id=…)`, `execute_query(search=…)`, and an
Engine-side check that turns a misspelled field into an error instead of zeros.

## Root cause (validated)

- **Sheets were read as titles only.** `_SHEET_LIST_DEF`
  (`backend/app/data_sources/clients/qlik_sense_onprem_client.py`, the
  `qAppObjectListDef` for `sheet`) requested `title`/`description` and nothing
  else; no object on a sheet was ever opened, so a chart's `qHyperCubeDef`
  (its dimensions and measures) never reached the catalog.
- **Variables never reached the prompt.** They were written to the Master Items
  table's `metadata_json.qlik_sense_onprem.variables`, but table metadata only
  renders for namespaces listed in `_TABLE_META_KEYS`
  (`backend/app/ai/context/sections/tables_schema_section.py`), and
  `qlik_sense_onprem` was not listed. Column descriptions are the channel that
  does render.
- **No way to read an existing object or look a value up.** `execute_query`
  only built ad-hoc hypercubes. Any other client method would bypass capture,
  timing and metering (`__getattr__` delegation in
  `backend/app/ai/code_execution/code_execution.py`), so new capabilities have
  to be modes of `execute_query`.
- **Misspelled fields return zeros.** Qlik evaluates an unknown field as an
  empty value: `Sum([Sales Qty])` (real field: `Sales Quantity`) yields `0` per
  row, not an error. Nothing called `CheckExpression` before running the cube.
- **Found while building the fake (pre-existing):**
  - `GetLineage` returns `{"qLineage": [...]}` (every Engine method wraps its
    return value), but the client iterated the reply itself, so lineage was
    silently empty against a real Engine. The old unit test stubbed the reply
    as a bare list, which is why it never failed.
  - The sheet list did not filter private sheets: when the crawl acts as the
    app owner, the owner's private sheets come back too, and their titles were
    stamped onto every table of the app (`metadata.sheets`).

## Loop A — deterministic reproduction (no external services)

`backend/tests/fixtures/fake_qlik_engine.py` is a fake QSEoW site that answers
the real Engine JSON-RPC methods (`OpenDoc`, `GetObject`, `GetFullPropertyTree`,
`GetProperties`, `GetEffectiveProperties`, `GetChildInfos`, `CreateSessionObject`
with list/hypercube/selection definitions, `GetLayout`, `GetHyperCubeData`,
`GetField`/`SelectValues`, `CheckExpression`, `SearchResults`, `GetLineage`,
`GetScript`) over the WebSocket boundary, computing hypercubes over a synthetic
dataset. Its demo app has an approved sheet with every object shape (a chart on
a master measure, a KPI with `$(vCurrentYear)` inside set analysis, a filter
pane, a container, a linked master visualization, a pivot table, a text box), a
community sheet, and a private sheet.

```bash
cd backend
TESTING=true BOW_DATABASE_URL=sqlite:///db/app.db \
  uv run pytest tests/unit/test_qlik_onprem_sheet_objects.py -q
```

Before the change (client changes stashed, fake + tests kept):

```
27 failed, 6 passed
```

The 6 that pass on both sides guard against over-blocking (valid expressions
must not be rejected; the query must still run when the Engine cannot check).
Observed old behaviour against the same fake:

```
typo measure -> [{'Region Name': 'Midwest', 'Qty': 0.0}, {'Region Name': 'Northeast', 'Qty': 0.0}, ...]
lineageSources on data table -> None
sheets on app meta -> ['Revenue Overview', 'Regional Deep Dive', 'Private scratch']
object_id query -> ValueError: At least one dimension or measure is required
```

## Loop B — the same site over real TLS sockets

`docs/feedback-loops/2026-11-16-qlik-onprem-sheet-objects.py` serves the fake
site as QRS on `https://localhost:<port>/qrs` and the Engine on
`wss://localhost:<port>/app/<id>`, both TLS, and drives the unmodified client
through the real `requests` and `websockets` transports. Loopback only,
synthetic data, no credentials.

```bash
cd backend
TESTING=true BOW_DATABASE_URL=sqlite:///db/app.db \
  uv run python ../docs/feedback-loops/2026-11-16-qlik-onprem-sheet-objects.py
```

Before (current `main` client): `[FAIL] approved sheet catalogued`,
`[FAIL] variable rendered`, `[FAIL] object ids rendered`, then
`ValueError: At least one dimension or measure is required` on the first
`object_id` call.

After:

```
     catalog: Sales/Consumer Sales/Master Items (5 columns)
     catalog: Sales/Consumer Sales/Orders (4 columns)
     catalog: Sales/Consumer Sales/Sales (4 columns)
     catalog: Sales/Consumer Sales/Sheets/Regional Deep Dive (1 columns)
     catalog: Sales/Consumer Sales/Sheets/Revenue Overview (6 columns)

   what the agent sees for the overview sheet:
     <column name="Revenue by Region" dtype="barchart" description="barchart id=obj-rev-region; dimensions: Region Name; measures: master measure 'Revenue'" role="object"/>
     <column name="Current Year Revenue" dtype="kpi" description="kpi id=obj-cy-kpi; measures: CY Revenue = Sum({&lt;[Year]={$(vCurrentYear)}&gt;} [Sales Quantity]*[Sales Price])" role="object"/>
     <column name="Filter pane: Year, Region" dtype="filterpane" description="filterpane id=obj-filters; filter fields: Year, Region" role="object"/>
     <column name="Orders by Year" dtype="table" description="table id=obj-orders-year; dimensions: Year; measures: Orders = Count(distinct [Order Number])" role="object"/>
     <column name="Revenue Trend" dtype="linechart" description="linechart id=obj-trend-link; dimensions: Year; measures: master measure 'Revenue'; linked to master visualization mv-trend" role="object"/>
     <column name="Product Mix" dtype="pivot-table" description="pivot-table id=obj-mix; dimensions: Product Group, Region Name; measures: Quantity = Sum([Sales Quantity])" role="object"/>
     <qlik_sense_onprem objectType="sheet" sheetDescription="Top-line revenue by region and year">

2. read an existing chart
     Region Name  Revenue
            West   1024.0
           South    898.0
         Midwest    772.0
       Northeast    646.0
  [PASS] selection narrows the chart — 1460.0 < 3340.0
  [PASS] pivot read flat — 12 rows, columns ['Product Group', 'Region Name', 'Quantity']

3. look a value up
           field     value  field_match_count
     Region Name Northeast                  1

4. misspelled measure
     ValueError: Qlik rejected the query before running it — measure 'Sum([Sales Qty])': unknown field(s) Sales Qty. ...

RESULT: PASS
```

## The fix

All in `backend/app/data_sources/clients/`:

- `_qix_common.py` — a type-agnostic object parser: `summarize_object_properties`
  finds every `qHyperCubeDef` / `qListObjectDef` in an object's properties
  (charts, KPIs, maps with layered cubes, extensions), resolves `qLibraryId` to
  the master item's name, folds an inline copy of a master expression into a
  reference, and records alternatives, calculation conditions, alternate states
  and the variables an expression expands. `describe_object` renders it as one
  line; `flatten_property_tree` walks `GetFullPropertyTree`.
- `qlik_sense_onprem_client.py`
  - Crawl: one `GetObject` + `GetFullPropertyTree` per shared sheet, plus
    `GetProperties` per master visualization. Private sheets are filtered out
    (in a published app a sheet must be `approved` or `published`).
  - Catalog: one `Stream/App/Sheets/<title>` entry per sheet, one column per
    data-bearing object (filter panes absorb their listboxes, containers and
    text boxes drop out, linked master visualizations resolve). Variables and
    master visualizations become Master Items columns.
  - `execute_query(object_id=…)` computes the object's own cube (effective
    properties, so linked master visualizations work; pivot forced to straight
    mode; sorting/suppression kept; filters applied as selections). A container
    answers with its children's ids.
  - `execute_query(search=…, fields=…)` runs `SearchResults` and returns
    `field / value / field_match_count`.
  - Every ad-hoc cube is checked with `CheckExpression` first; unknown fields
    raise a `ValueError` naming them. Advisory: if the Engine cannot run the
    check, the query proceeds.
  - `GetLineage` reads the wrapped `qLineage` array.
  - The connector's system prompt explains the new entries and modes.
- `backend/app/ai/context/sections/tables_schema_section.py` — renders
  `objectType` and `sheetDescription` for `qlik_sense_onprem` tables.

After: `34 passed` for the new file; the existing Qlik suites (on-prem client,
query contract, cloud client) stay green with three tests updated for the new
Engine conversation (the extra `CheckExpression` round trips and the
`MasterObjectList` session list).

## What this proves / regression notes

- Proven against the fake: sheet objects reach the rendered schema; private
  sheets reach neither the catalog nor any metadata; object queries equal the
  hand-written cube for the same definition, honour selections and read pivots;
  search returns exact spellings; misspelled fields are rejected; valid set
  analysis with variables is not.
- **Not proven:** behaviour against a real QSEoW server. The fake follows the
  Engine API's documented shapes, but property layouts of specific extensions
  and `CheckExpression` verdicts on exotic syntax (alternate-state set
  identifiers, `P()`/`E()`) come from the real Engine, not the fake. Run a
  reindex on a staging site and compare a few chart descriptions and
  `object_id` results with the hub before rolling out.
- Qlik Cloud (`qlik_sense_client.py`) is unchanged; the parser lives in
  `_qix_common.py` so it can adopt the same catalog later.
- Unrelated: `tests/unit/test_fabric_pool_identity.py` fails to import in a
  sandbox without `libodbc.so.2`; it fails identically without this change.
