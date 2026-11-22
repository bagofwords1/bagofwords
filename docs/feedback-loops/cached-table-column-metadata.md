# Feedback Loop — cached tables inherit what the catalog knows about their columns

A BOW cached table ("custom table") used to be a name, column names and
types. The agent lost the meaning the source carried, and would sum a distinct
count or a ratio and return a confident wrong number. The claim validated
here: on publish and on every refresh, each column inherits the description
and metadata of the indexed source column it copies, gets a re-aggregation
("additive") flag, and the table gets a fact-based description — from the
catalog that is already indexed, with no new call to any source — and that
this changes what the agent does.

Driven end to end through the browser as an admin, against a local Postgres
with column comments, with Claude Haiku 5.5 as the agent's model.

## What was built

| Layer | Change |
|---|---|
| Lineage (`backend/app/data_sources/fast/lineage.py`) | Per output column: the indexed column it copies, or "derived" with its expression. SQL through sqlglot, DAX against the indexed model, field lists (Priority OData, BusinessObjects) one to one. Ambiguous or unparseable: nothing |
| Additivity (`fast/additivity.py`) | `true` / `false` / `"unknown"` with a reason, for SQL and DAX expressions |
| Enrichment (`fast/column_metadata.py`) | Copies description and metadata, adds `source`, `additive`, `fingerprint`; applies admin edits; detects drift; builds the table description and the agent's note |
| Service (`services/custom_query_service.py`) | `describe_columns` runs in the preview and in `_publish` (every refresh); `redescribe` after an edit; admin edits live in `metadata_json["column_overrides"]` |
| Agent context (`ai/context/sections/tables_schema_section.py`) | `aggregation="NOT ADDITIVE …"` on the column, in both render paths |
| Dialog (`frontend/components/datasources/CachedColumnsEditor.vue`, `CustomQueryModal.vue`) | Column list after preview, description and flag editable, edited / drift markers, source-RLS warning, generated description |
| Tables page (`frontend/components/datasources/TablesSelector.vue`) | A "Details" column for cached tables: description, origin, flag, edited and drift badges |

## Loop A — deterministic (no external services)

```bash
cd backend && TESTING=true ENVIRONMENT=production uv run pytest \
  tests/unit/test_custom_table_lineage.py \
  tests/unit/test_custom_table_additivity.py \
  tests/unit/test_custom_table_column_metadata.py --db=sqlite
cd frontend && node --test tests/unit/columnMetadata.mjs
```

Observed: `174 passed` on SQLite. The DB-backed file plus
`test_fast_artifact_lifecycle.py` also pass with `--db=postgres` against a
local Postgres 16 (`51 passed`). The frontend test passes.

Each behaviour was checked to be able to fail. Six mutations, one at a time,
against the same three files:

| Mutation | Result |
|---|---|
| COUNT(DISTINCT) no longer classified | 7 failed |
| An admin's description edit ignored | 3 failed |
| Drift never detected | 2 failed |
| An ambiguous unqualified column attributed anyway | 2 failed |
| Publish stores the bare columns | 5 failed |
| The note not rendered to the agent | 6 failed |

Power BI is covered here and only here: lineage, additivity of measures
(including measures that reference other measures), drift on a changed
measure expression and the `rowLevelSecurity` marker are tested against an
indexed-catalog fixture shaped like what `powerbi_client.py` stores. **No
live Power BI model was used.**

## Loop B — live sandbox

```bash
export BOW_ENCRYPTION_KEY=...          # any Fernet key, before the first boot
tools/agent/boot_stack.sh --dev
cd backend && uv run python ../tools/agent/seed_org.py
ANTHROPIC_API_KEY=... HAIKU_MODEL_ID=claude-haiku-5-5 uv run python ../tools/agent/setup_haiku_llm.py

createdb retail && psql -d retail -f ../tools/agent/cached_table_metadata_seed.sql
RETAIL_PG_URL=postgresql://USER:PASSWORD@127.0.0.1:5432/retail \
  uv run python ../tools/agent/cached_table_metadata_setup.py
```

The source is synthetic: 24 stores, 600 customers, 24,000 orders over 2025,
generated without `random()`. Every customer buys in many months and
regions, so per-row distinct counts do not add up: each store type has 600
distinct customers in the year, while the twelve monthly counts sum to about
6,200.

The setup script prints what indexing stored, which confirms the comments are
in the catalog before anything is built on them:

```json
"public.orders": {
  "order_id": "Order identifier.",
  "store_id": "Store that took the order.",
  "customer_id": "Customer who placed the order.",
  "order_date": "Calendar date the order was completed.",
  "amount": "Order value in USD, net of tax and discounts.",
  "quantity": "Number of units in the order."
},
"public.stores": {
  "store_id": "Store identifier.",
  "region": "Sales region the store reports to (North, South, East, West).",
  "store_type": "Store format: Flagship, Outlet or Online."
}
```

### The run, as a user

1. **Settings → AI Settings → Agent capabilities:** switched "Custom tables
   (beta)" on.
2. **Agents → Retail → Tables:** enabled the three tables, then
   **+ Custom table**, name `sales_by_region_month`, this query, **Run preview**:

   ```sql
   SELECT s.region,
          date_trunc('month', o.order_date)::date AS order_month,
          SUM(o.amount) AS revenue,
          COUNT(*) AS orders,
          COUNT(DISTINCT o.customer_id) AS customers,
          SUM(o.amount) / COUNT(*) AS avg_order_value
   FROM public.orders o
   JOIN public.stores s ON s.store_id = o.store_id
   GROUP BY 1, 2
   ```

   The dialog listed the six columns with inherited descriptions and flags and
   pre-filled the description. **Create & cache.**
3. Expanded the table on the Tables page: the same metadata per column.
4. Asked the agent questions that tempt a sum (below).
5. Changed column comments at the source, reloaded the schema from the Tables
   page (**Refresh postgresql-1 · Shared schema**), **Refresh now** in the
   dialog.
6. Edited the `region` description in the dialog, saved; changed the comment
   again and the type of `orders.amount` at the source; reloaded; refreshed.

### What is stored

All three are the table's `columns` as returned by
`GET /api/connections/{id}/custom-queries`.

**Before — the same query on unmodified `main`** (run as a second backend
from a worktree of `origin/main` against the same database):

```json
[
  {
    "name": "region",
    "dtype": "string"
  },
  {
    "name": "order_month",
    "dtype": "date32[day]"
  },
  {
    "name": "revenue",
    "dtype": "decimal128(8, 2)"
  },
  {
    "name": "orders",
    "dtype": "int64"
  },
  {
    "name": "customers",
    "dtype": "int64"
  },
  {
    "name": "avg_order_value",
    "dtype": "decimal128(19, 16)"
  }
]
```

**After — as created through the dialog.** The table's `description` was
stored empty because the generated text was left as it was. Its
`generated_description`: "One row per region, order_month. Measures: revenue (additive), orders (additive), customers (not additive: distinct count), avg_order_value (not additive: ratio). Source: public.orders, public.stores on postgresql-1. Refreshed every hour."

```json
[
  {
    "name": "region",
    "dtype": "string",
    "description": "Sales region the store reports to (North, South, East, West).",
    "metadata": {
      "source": {
        "kind": "column",
        "table": "public.stores",
        "column": "region"
      },
      "fingerprint": "2bc2009fecc9b847",
      "role": "dimension",
      "additive": false,
      "additive_reason": "grouping_key"
    }
  },
  {
    "name": "order_month",
    "dtype": "date32[day]",
    "description": "CAST(DATE_TRUNC('MONTH', o.order_date) AS DATE). Reads public.orders.order_date: Calendar date the order was completed.",
    "metadata": {
      "source": {
        "kind": "derived",
        "expression": "CAST(DATE_TRUNC('MONTH', o.order_date) AS DATE)",
        "reads": [
          {
            "table": "public.orders",
            "column": "order_date"
          }
        ]
      },
      "fingerprint": "3a977cf5ea9b4e54",
      "role": "dimension",
      "additive": false,
      "additive_reason": "grouping_key"
    }
  },
  {
    "name": "revenue",
    "dtype": "decimal128(8, 2)",
    "description": "SUM(o.amount). Reads public.orders.amount: Order value in USD, net of tax and discounts.",
    "metadata": {
      "source": {
        "kind": "derived",
        "expression": "SUM(o.amount)",
        "reads": [
          {
            "table": "public.orders",
            "column": "amount"
          }
        ]
      },
      "fingerprint": "290fc73212a28908",
      "role": "measure",
      "additive": true,
      "additive_reason": "sum"
    }
  },
  {
    "name": "orders",
    "dtype": "int64",
    "description": "COUNT(*)",
    "metadata": {
      "source": {
        "kind": "derived",
        "expression": "COUNT(*)",
        "reads": []
      },
      "fingerprint": "ed7ff541b9baa0f4",
      "role": "measure",
      "additive": true,
      "additive_reason": "count"
    }
  },
  {
    "name": "customers",
    "dtype": "int64",
    "description": "COUNT(DISTINCT o.customer_id). Reads public.orders.customer_id: Customer who placed the order.",
    "metadata": {
      "source": {
        "kind": "derived",
        "expression": "COUNT(DISTINCT o.customer_id)",
        "reads": [
          {
            "table": "public.orders",
            "column": "customer_id"
          }
        ]
      },
      "fingerprint": "1e133092b9ca9bc3",
      "role": "measure",
      "additive": false,
      "additive_reason": "distinct_count"
    }
  },
  {
    "name": "avg_order_value",
    "dtype": "decimal128(19, 16)",
    "description": "SUM(o.amount) / COUNT(*). Reads public.orders.amount: Order value in USD, net of tax and discounts.",
    "metadata": {
      "source": {
        "kind": "derived",
        "expression": "SUM(o.amount) / COUNT(*)",
        "reads": [
          {
            "table": "public.orders",
            "column": "amount"
          }
        ]
      },
      "fingerprint": "f4ababc99aab4743",
      "role": "measure",
      "additive": false,
      "additive_reason": "ratio"
    }
  }
]
```

**After the source changed and an admin edited.** Sequence: two comments
reworded → refresh (descriptions followed, no drift flag); `region` edited by
the admin; the `region` comment reworded again and `orders.amount` changed
from `numeric(12,2)` to `double precision` → refresh. The admin's wording
stayed and `inherited` tracks the newest comment; the two columns that read
`amount` carry `drift`; `recompute` (added while iterating on the note, see
below) says how to roll the ratio up.

This capture predates one fix: `drift.previous_expression` is no longer
recorded when the expression text itself did not change, as here.

```json
[
  {
    "name": "region",
    "dtype": "string",
    "description": "Region of the store that took the order (finance wording)",
    "metadata": {
      "source": {
        "kind": "column",
        "table": "public.stores",
        "column": "region"
      },
      "fingerprint": "2bc2009fecc9b847",
      "role": "dimension",
      "additive": false,
      "additive_reason": "grouping_key",
      "edited": [
        "description"
      ],
      "inherited": {
        "description": "Sales territory; third wording of this comment."
      }
    }
  },
  {
    "name": "order_month",
    "dtype": "date32[day]",
    "description": "CAST(DATE_TRUNC('MONTH', o.order_date) AS DATE). Reads public.orders.order_date: Calendar date the order was completed.",
    "metadata": {
      "source": {
        "kind": "derived",
        "expression": "CAST(DATE_TRUNC('MONTH', o.order_date) AS DATE)",
        "reads": [
          {
            "table": "public.orders",
            "column": "order_date"
          }
        ]
      },
      "fingerprint": "3a977cf5ea9b4e54",
      "role": "dimension",
      "additive": false,
      "additive_reason": "grouping_key"
    }
  },
  {
    "name": "revenue",
    "dtype": "double",
    "description": "SUM(o.amount). Reads public.orders.amount: Order value in USD, net of tax, discounts and returns.",
    "metadata": {
      "source": {
        "kind": "derived",
        "expression": "SUM(o.amount)",
        "reads": [
          {
            "table": "public.orders",
            "column": "amount"
          }
        ]
      },
      "fingerprint": "fe0fa2840e56ff3f",
      "role": "measure",
      "additive": true,
      "additive_reason": "sum",
      "drift": {
        "detected_at": "2026-11-22T11:23:40",
        "previous_fingerprint": "290fc73212a28908",
        "previous_expression": "SUM(o.amount)"
      }
    }
  },
  {
    "name": "orders",
    "dtype": "int64",
    "description": "COUNT(*)",
    "metadata": {
      "source": {
        "kind": "derived",
        "expression": "COUNT(*)",
        "reads": []
      },
      "fingerprint": "ed7ff541b9baa0f4",
      "role": "measure",
      "additive": true,
      "additive_reason": "count"
    }
  },
  {
    "name": "customers",
    "dtype": "int64",
    "description": "COUNT(DISTINCT o.customer_id). Reads public.orders.customer_id: Customer who placed the order.",
    "metadata": {
      "source": {
        "kind": "derived",
        "expression": "COUNT(DISTINCT o.customer_id)",
        "reads": [
          {
            "table": "public.orders",
            "column": "customer_id"
          }
        ]
      },
      "fingerprint": "1e133092b9ca9bc3",
      "role": "measure",
      "additive": false,
      "additive_reason": "distinct_count"
    }
  },
  {
    "name": "avg_order_value",
    "dtype": "double",
    "description": "SUM(o.amount) / COUNT(*). Reads public.orders.amount: Order value in USD, net of tax, discounts and returns.",
    "metadata": {
      "source": {
        "kind": "derived",
        "expression": "SUM(o.amount) / COUNT(*)",
        "reads": [
          {
            "table": "public.orders",
            "column": "amount"
          }
        ]
      },
      "fingerprint": "7b1aa762be2b0ede",
      "role": "measure",
      "additive": false,
      "additive_reason": "ratio",
      "drift": {
        "detected_at": "2026-11-22T11:23:40",
        "previous_fingerprint": "f4ababc99aab4743",
        "previous_expression": "SUM(o.amount) / COUNT(*)"
      },
      "recompute": "SUM(revenue) / SUM(orders)"
    }
  }
]
```

The refresh that found the change recorded it on its run
(`GET …/custom-queries/{id}/runs/{run_id}`):

```
manual success [{"level": "warning", "message": "Column revenue: the definition of public.orders.amount changed at the source since this table last described it."},
                {"level": "warning", "message": "Column avg_order_value: the definition of public.orders.amount changed at the source since this table last described it."}]
```

### What the agent sees

`tools/agent/cached_table_metadata_context.py Retail sales_by_region_month`
renders the table with the application's own `SchemaContextBuilder`. This
capture has the first wording of the note; the final wording is in the next
section.

```xml
<table name="sales_by_region_month" cols="6" description="One row per region, order_month. Measures: revenue (additive), orders (additive), customers (not additive: distinct count), avg_order_value (not additive: ratio). Source: public.orders, public.stores on postgresql-1. Refreshed every hour." score="0.0" cached="true" as_of="2026-11-22T10:54">
<columns>
<column name="region" dtype="string" description="Sales region the store reports to (North, South, East, West)." role="dimension"/>
<column name="order_month" dtype="date32[day]" description="CAST(DATE_TRUNC('MONTH', o.order_date) AS DATE). Reads public.orders.order_date: Calendar date the order was completed." role="dimension"/>
<column name="revenue" dtype="decimal128(8, 2)" description="SUM(o.amount). Reads public.orders.amount: Order value in USD, net of tax and discounts." role="measure"/>
<column name="orders" dtype="int64" description="COUNT(*)" role="measure"/>
<column name="customers" dtype="int64" description="COUNT(DISTINCT o.customer_id). Reads public.orders.customer_id: Customer who placed the order." role="measure" aggregation="not additive (distinct count): do not SUM or AVG this column across rows; a total over several rows must be recomputed from additive columns or asked of the source"/>
<column name="avg_order_value" dtype="decimal128(19, 16)" description="SUM(o.amount) / COUNT(*). Reads public.orders.amount: Order value in USD, net of tax and discounts." role="measure" aggregation="not additive (ratio): do not SUM or AVG this column across rows; a total over several rows must be recomputed from additive columns or asked of the source"/>
</columns>
</table>
```

### What the agent did

Each question was typed into the chat as a new report. Truth for the store
type table: 600 distinct customers per store type; ticket 261.75 / 260.50 /
259.25.

**Round 1 — `sales_by_region_month`, source tables also enabled.** "Using
the sales_by_region_month table, how many customers did each region have in
total across all of 2025?" The agent did not sum `customers`; it counted
distinct customers at the source and answered 600 per region. A control with
the note and descriptions removed (through admin edits) **gave the same
correct answer**. On this question the note made no measurable difference:
Haiku inferred the trap from the table's shape and had the source to
recompute from. No learned note or instruction existed between the two runs
(checked in the database).

**Round 2 — a stricter setup.** A second table, `store_type_month_kpis`
(created through the API with the same query shape), with the distinct count
named `active_accounts` and the ratio `ticket`, and every other table
switched off for the agent on the Tables page, so the cached table is all it
has. "From store_type_month_kpis, give me total active_accounts and the
overall ticket for each store type for the full year 2025."

| Arm | Runs | What the agent did |
|---|---|---|
| No metadata (control) | 1 | `SUM(active_accounts)`; reported **6,209 / 6,217 / 6,212 "total active accounts"** as fact. Wrong by a factor of ten |
| First note: `not additive (distinct count): do not SUM or AVG this column across rows; a total over several rows must be recomputed from additive columns or asked of the source` | 1 | Still ran `SUM(active_accounts)` and reported 6,209 as the total, adding one sentence that it is not a count of unique accounts. **Not good enough** |
| Final note (below) | 3 | 2 runs: no sum; said the table cannot answer it and why. 1 run: showed the monthly sum only as a labelled reference beside the explanation, not as the answer. Ticket computed as `SUM(revenue) / SUM(orders)` in all 3 |

The final wording, as rendered:

```
aggregation="NOT ADDITIVE (distinct count): each row's value stands alone. SUM or AVG over several rows is wrong, also when a 'total' is asked for. For a coarser grain do not derive it from this column: query the source tables if you have them, otherwise say this table cannot answer it"
aggregation="NOT ADDITIVE (ratio): read it only row by row. Never SUM or AVG it. Over several rows compute SUM(revenue) / SUM(orders) instead"
```

Two things changed between the first and the final note. It says what to do
instead, including when the user literally asks for a total; and a ratio
whose numerator and denominator are themselves additive columns of the table
carries the exact recomputation (`metadata.recompute`), found by matching
expressions, never approximated. It is still a note on the column, not a
prompt rule.

These are small samples (one, one and three runs of one question with one
model). They show the direction, not a rate.

## Screenshots

`media/pr/cached-table-column-metadata/`: the dialog and the Tables page
before and after (English), after in Hebrew, the edited and drift badges,
and the chat with and without the metadata.

## Found and fixed during the run

- **The first note did not change behaviour** when the user asked for a
  "total" (above). Reworded, and ratios gained `recompute`.
- **Stored types are Arrow names** (`int64`, `decimal128(8, 2)`,
  `date32[day]`), not SQL type names; the numeric test covers both.
- **A measure reference does not name its table**, so for DAX the whole
  model's catalog is loaded, not only the tables the text mentions.
- **`previous_expression` was recorded on drift even when the text had not
  changed** (only a column it reads had). It is now kept only when the
  expression itself moved.
- **`<i18n-t>` needs `scope="global"`** in this app, or vue-i18n warns on
  every render.
- **The dialog sent `description: null` for an empty description**, which
  the update route reads as "keep". It now sends a string, so a description
  can be cleared.

## What this proves / regression notes

Proved: lineage, classification, inheritance, follow-on-refresh, admin edits
surviving, drift flag and run event, the rendered note, and the UI in
English and Hebrew, on Postgres. With the final note Haiku 5.5 stopped
presenting a summed distinct count as a total in the setup where it
otherwise did.

Not covered:

- **Power BI against a live model.** Fixture tests only. Measures are the
  most important real case for additivity; this needs a tenant.
- Analysis Services, Priority and BusinessObjects lineage: fixture tests only.
- Other models than Haiku 5.5, and more than a handful of chat runs.
- The source-RLS warning in the browser: no source in this sandbox carries
  the marker. The detection is unit-tested; the banner was not seen live.
- Postgres as the *application* database in the browser run (SQLite there;
  the tests ran on both).
- A connection with more than 2,000 candidate catalog tables is not matched
  (no lineage).

Environment, not the change: `tests/unit/test_fabric_pool_identity.py` cannot
be collected in this sandbox (`import pyodbc`: `libodbc.so.2` missing).

Sandbox notes: uvicorn's reload hung after backend edits ("Waiting for
background tasks to complete"); kill the process group and run
`tools/agent/restart_backend.sh db/agent.db` with the same
`BOW_ENCRYPTION_KEY`.
