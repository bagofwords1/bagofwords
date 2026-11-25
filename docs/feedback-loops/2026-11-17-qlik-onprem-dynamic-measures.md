# Feedback Loop — "In dynamic measures … the tool only sees the default formula"

A field test of the Qlik Sense on-prem connector found that it reads formulas
defined in the UI, terms that exist only in the UI, and visualizations
(including pivots), but fails on **dynamic measures**: charts where the user
switches what is shown (for example sales amount vs. quantity). The agent saw
only the default formula and inferred the rest itself, often missing a filter
or adjustment that the Qlik app applies. This loop reproduces that, then
verifies the change that gives the agent every choice with its explicit
formula and a way to run the chart under that choice.

## Root cause (validated)

Qlik apps build dynamic measures in three common shapes, and the connector
lost the information that decides each one:

| Shape | How it is built | What was lost |
|---|---|---|
| Variable switch | A button (`action-button`, `setVariable`) or variable input (`variable`, `variableName` + `alternatives`) sets `vMetric`; the measure reads it: `Pick(Match(vMetric,'Sales','Quantity'), …)`, `If(vMetric='Sales', …)`, `$(vExpr)` | `_sheet_objects` dropped every object without a hypercube, so the switches — and therefore the list of choices — never reached the catalog. |
| "Show column if" | Each table column has `qCalcCondition` on the choice | `_summarize_measure` read only the chart-level condition, so the agent saw every column and not when each applies. |
| Data island | A small unlinked table (`MetricSelector`: `Metric`, `MetricExpr`); the measure is `$(=Only(MetricExpr))` | The choices *and their formulas* live in the island's rows, which were never read. |

On the query side, `execute_query(object_id=…)` could only run a chart under
the app's current state. `variables=…` was swallowed by `**_kwargs`, so asking
for the "Quantity" choice silently returned the "Sales" numbers.

A label written as an expression (`qLabelExpression`) was also read after the
static `qLabel`, while Qlik shows the expression when both are set.

## Loop A — deterministic reproduction (no external services)

`backend/tests/fixtures/fake_qlik_engine.py` gained a "Dynamic Metrics" sheet
with all three shapes, a data-island table, `Doc.GetMeasure/GetDimension`, and
an Engine-side evaluator for `Pick/Match/If/Only/$(=…)` written independently
of the client's resolver.

```bash
cd backend
TESTING=true BOW_DATABASE_URL=sqlite:///db/app.db \
  uv run pytest tests/unit/test_qlik_onprem_dynamic_measures.py -q
```

Before (client changes stashed, fake + tests kept): **26 failed, 2 passed**.
The two that pass on both sides are guards: static charts get no options, and
a query never changes the app. Observed old behaviour on the same fake:

```
catalog columns -> ['Filter pane: Metric', 'Metric by Group', 'Metric by Region', 'Selected metric by Year']
Metric by Region -> barchart id=obj-dyn-bar; dimensions: Region Name; measures: Metric = master measure 'Dynamic Metric'
Metric by Group -> table id=obj-dyn-table; dimensions: Product Group; measures: master measure 'Revenue'; Units = Sum([Sales Quantity]); Orders = Count(distinct [Order Number])
object_id + variables Quantity -> {'Region Name': ['Midwest', 'Northeast', 'South', 'West'], 'Metric': [772.0, 646.0, 898.0, 1024.0]}
```

Those last numbers are sales amounts. Units by region are 216 / 180 / 252 / 288.

After: **31 passed**. The central test runs *every* option the catalog
lists with the exact call it shows and asserts the result equals the option's
explicit formula run as a plain query — the listed formula and the chart agree
for every choice, not one picked example.

## Loop B — over real TLS sockets

`docs/feedback-loops/2026-11-17-qlik-onprem-dynamic-measures.py` serves the
fake site as QRS + Engine on localhost over TLS (reusing the helpers of the
2026-11-16 loop) and drives the unmodified client.

```bash
cd backend
TESTING=true BOW_DATABASE_URL=sqlite:///db/app.db \
  uv run python ../docs/feedback-loops/2026-11-17-qlik-onprem-dynamic-measures.py
```

Before (current `main` client): `[FAIL] switches catalogued`,
`[FAIL] dynamic charts catalogued`.

After:

```
    <column name="Metric" dtype="variable" description="switch id=var-metric; a user choice, not data: sets vMetric='Sales' (Sales amount), vMetric='Quantity' (Units). ..." role="switch"/>
    <column name="Metric by Region" dtype="barchart" description="barchart id=obj-dyn-bar; dimensions: Region Name; measures: =If(vMetric='Sales','Revenue','Units') = master measure 'Dynamic Metric'; DYNAMIC — what it shows depends on a user choice; when vMetric='Sales' (Sales amount): by Region Name: Revenue = Sum([Sales Quantity]*[Sales Price]) [execute_query(object_id='obj-dyn-bar', variables={"vMetric": "Sales"})] | when vMetric='Quantity' (Units): by Region Name: Units = Sum([Sales Quantity]) [execute_query(object_id='obj-dyn-bar', variables={"vMetric": "Quantity"})]" role="object"/>
    <column name="Selected metric by Year" dtype="barchart" description="...; when Metric='Quantity': by Year: Selected metric = Sum([Sales Quantity]) [execute_query(object_id='obj-island-bar', filters={"Metric": ["Quantity"]})] | when Metric='Sales': ..." role="object"/>

  [PASS] Metric by Region when vMetric='Sales' — {'variables': {'vMetric': 'Sales'}} -> Revenue=3340
  [PASS] Metric by Region when vMetric='Quantity' — {'variables': {'vMetric': 'Quantity'}} -> Units=936
  [PASS] Metric by Group when vMetric='Sales' — {'variables': {'vMetric': 'Sales'}} -> Revenue=3340, Orders=48
  [PASS] Metric by Group when vMetric='Quantity' — {'variables': {'vMetric': 'Quantity'}} -> Units=936, Orders=48
  [PASS] Selected metric by Year when Metric='Quantity' — {'filters': {'Metric': ['Quantity']}} -> Selected metric=936
  [PASS] Selected metric by Year when Metric='Sales' — {'filters': {'Metric': ['Sales']}} -> Selected metric=3340
  [PASS] default unchanged after a choice
  [PASS] no write calls reached the Engine
RESULT: PASS
```

## The fix

- `_qix_common.py`
  - **Resolver.** `substitute_expression` puts a choice into an expression the
    way Qlik would see it: `$(v)` → raw text, bare `v` → literal,
    `Only(F)` / `GetFieldSelections(F)` → literal, `$(=Only(F))` → the
    formula text stored in the island row.
    `simplify_expression` collapses `Pick(Match(…))`, `Match/MixMatch/WildMatch`
    and `If(lit = lit, …)` once their deciding argument is a literal, and
    leaves everything else exactly as written. Quoted text, `[fields]` and
    qualified names are never touched.
  - **Switches.** `summarize_switch` reads buttons' `setVariable` and
    field-select actions and variable inputs' `alternatives` /
    `dynamicvalues`.
  - **Options.** `object_options` turns one object into one entry per choice,
    with the dimensions, measures and labels that choice shows. Columns its
    `qCalcCondition` hides are dropped.
  - **Parser fixes.** Per-column "show column if" conditions are read, and a
    label expression now wins over the static label, as in Qlik.
- `qlik_sense_onprem_client.py`
  - **Crawl.** Switches are kept as catalog columns (`role: switch`). Small
    unlinked tables whose fields an expression reads (data islands) are read
    in full: at most 50 rows, 8 fields and 10 islands per app.
  - **Catalog.** Every dynamic object's description carries
    `DYNAMIC — when <choice>: <formula> [execute_query(...)]`.
  - **Query.** `execute_query(..., variables={...})` works for `object_id`
    and ad-hoc cubes. Master items are inlined first (`GetMeasure` /
    `GetDimension`) so the override reaches their text. "Show column if"
    conditions are applied on every object query: decided locally when a
    choice settles them, otherwise evaluated by the Engine under the query's
    selections.
  - **Prompt.** The system prompt explains dynamic charts and tells the agent
    to run the listed call instead of the default.

**Nothing is written to the Engine.** The override is text substituted into
this one query. It does not call `SetStringValue` / `SetProperties`, because a
variable change in a shared QSEoW app can be visible to other users.

## What this proves / regression notes

- **Proven against the fake:**
  - every listed option runs, and equals its explicit formula;
  - variable and island choices give different numbers;
  - the default run equals the current variable's choice;
  - hidden columns are dropped;
  - choices never reach the app;
  - quoted text and fields are left alone;
  - nested choices resolve.
- **Not proven:** the exact property shapes of buttons and variable inputs on
  every Qlik release, and of third-party switch extensions. Those come from
  the Qlik dashboard bundle and button documentation, as the fake models
  them. Run a reindex on the real site and check that each switch shows what
  it sets.
- **Not covered:**
  - choices driven by alternate states or bookmarks;
  - alternative measures picked from a chart's exploration menu (listed, but
    not selectable in `object_id`);
  - island formulas read through `Only({<set>} F)`.
- Existing suite: the sheet-objects test that counted variables now counts
  the demo's own variables instead of a fixed number.
