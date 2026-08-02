# Feedback Loop — Snowflake semantic views lost their model

A semantic view is one queryable object whose columns come from several base
tables, joined by relationships declared inside the view. The connector read
only its dimensions and metrics, so two things never reached the agent: which
base table each column came from, and that the view's logical tables were
related at all.

A third defect was shared with every other connector: the `kind` marking a
column as a dimension vs a metric was discarded at persist time — while the
connector's own prompt instructed the agent to sort columns into `DIMENSIONS`
and `METRICS` using exactly that label. `SEMANTIC_VIEW(view DIMENSIONS ...
METRICS ...)` is unusable without it.

## What `DESC SEMANTIC VIEW` actually returns

Measured live (2026-08-02) against a five-table fixture view:

| object_kind | rows | read before? |
|---|---|---|
| `DIMENSION` | 16 | yes |
| `METRIC` | 20 | yes |
| `FACT` | 16 | yes |
| `TABLE` | 20 | **no — dropped** |
| `RELATIONSHIP` | 20 | **no — dropped** |

The dropped rows carry the model:

```
('TABLE','DEPOTS',None,'BASE_TABLE_NAME','DIM_DEPOT')
('TABLE','DEPOTS',None,'PRIMARY_KEY','["DEPOT_KEY"]')
('RELATIONSHIP','SERVICES_TO_DEPOT','SERVICES','TABLE','SERVICES')
('RELATIONSHIP','SERVICES_TO_DEPOT','SERVICES','REF_TABLE','DEPOTS')
('RELATIONSHIP','SERVICES_TO_DEPOT','SERVICES','FOREIGN_KEY','["DEPOT_KEY"]')
('RELATIONSHIP','SERVICES_TO_DEPOT','SERVICES','REF_KEY','["DEPOT_KEY"]')
```

`parent_entity` on each DIMENSION/METRIC row names its logical table, so column
attribution was available all along and simply not read. Key lists arrive as
JSON strings, not arrays.

## The fix

- Parse `TABLE` rows into a logical-table map (alias → base table, primary key)
  and `RELATIONSHIP` rows into the view's internal joins.
- Attribute every dimension/metric to its logical table via `parent_entity`.
- Carry both as `metadata_json.semantic_model` and render them as
  `<semantic_model>` with `<logical_table>` and `<join>` children, in **both**
  schema renderers.
- Deliberately NOT `fks`: a foreign key renders as a reference to another
  *indexed* table, and these joins are internal to a single semantic view.
  Presenting them as `fks` would point the agent at tables that do not exist in
  its catalog.
- The `kind` loss is fixed by `normalize_indexed_columns` (see the Power BI
  loop) — the same truncation affected every connector that puts column
  descriptors in metadata.

## Fixture

`tools/agent/sf_build_fixture_semantic_view.py` builds
`DEMO_SEMANTIC_DB.BOW_FIXTURES.FLEET_OPS_SV`: two facts over three dimensions,
`depots` conformed across both facts, a snowflake chain
`shipments → parts → categories`, five relationships and five metrics. Answering
a category-level question therefore requires traversing a relationship that is
not on the fact table.

## Verification

Live, through the product UI with real LLM completions, with the five base
tables deactivated so only the semantic view was available:

| Question | Requires | Generated | Result |
|---|---|---|---|
| parts cost by depot | metric + dimension on different logical tables | `SEMANTIC_VIEW(... DIMENSIONS DEPOT_NAME METRICS TOTAL_PARTS_COST)` | Northgate 2000, Southfield 1800 ✅ |
| freight by part category | **snowflake hop** shipments → parts → categories | `SEMANTIC_VIEW(... DIMENSIONS CATEGORY_NAME METRICS TOTAL_FREIGHT)` | Drivetrain 750, Consumables 120 ✅ |

Both sorted the requested column into `DIMENSIONS` vs `METRICS` correctly, which
is only possible with the `kind` metadata that was previously stripped.

Indexed shape after the fix (13 columns: 4 dimensions, 4 facts, 5 metrics):

```
<column name="CATEGORY_NAME" dtype="VARCHAR(50)" role="dimension"/>
<column name="TOTAL_FREIGHT" dtype="FLOAT" role="metric"/>
<semantic_model>
  <logical_table alias="DEPOTS" base_table="BOW_FIXTURES.DIM_DEPOT" primary_key="DEPOT_KEY"/>
  <join from_table="PARTS" to_table="CATEGORIES" from_columns="CATEGORY_KEY" to_columns="CATEGORY_KEY"/>
  ...
</semantic_model>
```

## Regression notes

- Key lists are JSON strings (`'["DEPOT_KEY"]'`); `_key_list` parses them and
  falls back to the raw value, so a composite key does not silently become one
  malformed column name.
- A relationship missing either side is skipped rather than half-rendered —
  a join pointing at nothing is worse than no join.
- Snowflake per-user OAuth was not exercised. A `BOW_E2E_OAUTH` custom OAuth
  integration exists on the account, but Snowflake custom OAuth is
  authorization-code only (no ROPC), so a headless delegated token needs a
  browser hop. The per-user overlay and context code is connector-agnostic and
  is covered by the Power BI OBO run.
