# Table-level `metadata_json` never reaches agent context

**Status:** ✅ **Fixed** — `tables_schema_section.py` + `tests/unit/test_source_metadata_context_rendering.py`.
This document is retained as the rationale for what was surfaced and, more importantly,
what was deliberately left out.
**Found:** 2026-07-25, while specifying the Priority ERP connector — but this is independent
of Priority and worth fixing on its own.

---

## The bug

`Table.metadata_json` is populated by **at least 37 of 38** data-source clients and
rendered for **three**. (An initial scan said 34 — it missed the three XMLA/OLAP clients,
which build the key from a variable, `{self.META_KEY: meta}`.) Everything else is written to the catalog and silently dropped before the agent
sees it.

The agent's schema context is built by
`app/ai/context/sections/tables_schema_section.py::TablesSchemaContext.DataSource._render_topk_tables_full`
(:499-580), called from `agent_v2.py` (:1708, :2930, :4048, :4926) and from the
`describe_tables` / `create_data` tools.

It renders **generically**, for every connector:

- `<table name= description= score= usage= instructions= cols=>`
- `<column name= dtype= description= role=>` (`role` from `metadata["kind"] or ["role"]`)
- `<pks>`, `<fks>`

Table-level `metadata_json` renders **only** via hardcoded branches:

| Branch | Covers |
|---|---|
| `metadata_json["type"] == "semantic_view"` (:538) | Snowflake semantic views |
| `metadata_json["powerbi_report_server"]` (:562-576) | Power BI Report Server |
| `_render_powerbi_cloud_metadata_xml` (:68) | Power BI cloud |

Nothing else has one.

### Verified by execution

Rendering a Tableau-shaped `Table` through the real code path:

```xml
<table name="Superstore" cols="2" description="Sales datasource">
<columns>
<column name="Order Date" dtype="DATE" description="Date the order was placed" role="dimension"/>
<column name="Sales" dtype="REAL" description="Total sales amount" role="measure"/>
</columns>
</table>
```

Input `metadata_json={"tableau": {"datasourceLuid": "abc-123", "projectName": "Finance"}}`
— **absent from the output.** Same result for `oracle_bi` and `businessobjects`.

---

## What to actually fix — and what to leave alone

Most of the 37 are **false alarms**: the value is already recoverable from
`Table.name` or `Table.description`, both of which do render. Only cases where the
information is genuinely unrecoverable are worth fixing.

### Tier 1 — the object's ID is lost, so the agent cannot address it

This is the same problem Power BI's branch was added to solve: `datasetId` is how you
address a dataset in `executeQueries`. These four have the identical need and no branch.

| Connector | Lost | Why it matters | Recoverable from name? |
|---|---|---|---|
| **`tableau`** | `datasourceLuid` | VizQL / Metadata API addresses datasources by LUID | ❌ name is `"{project}/{datasource}"` |
| **`qlik_sense`** | `appId`, `spaceId` | Engine API opens apps by `appId` | ❌ name is `"{app}/{table}"` |
| **`sisense`** | `datamodelId`, `dashboards` | queries are addressed by datamodel id | ❌ name is `"{model}/{table}"` |
| **`businessobjects`** | `universe_id` | `/biprws` addresses universes by id | ❌ name is the universe *name* |

### Tier 1b — OLAP/XMLA: the system prompt promises fields the context drops

**`analysis_services`, `sap_bw`, `infor_olap`** — all three subclass `XmlaClient`
(`xmla_base.py`). My first enumeration **missed them**: they write
`metadata_json={self.META_KEY: meta}` with a *variable* key, which the literal-key scan
didn't match. They are the strongest case in this document.

`execute_query` passes the statement **straight to XMLA Execute** — *"the server parses
the language"* — with no caption→unique-name resolution anywhere. So the agent must author
MDX/DAX itself, using real identifiers. Each client's `system_prompt()` tells it exactly
where to find them:

| Client | What the system prompt says | Where it lives | Rendered? |
|---|---|---|---|
| `sap_bw` | *"Each column's query identifier is in `metadata.unique_name` — reference members/measures by that unique name in MDX."* | `TableColumn.metadata["unique_name"]` | ❌ |
| `infor_olap` | *"The MDX `unique_name` for every column lives in its `metadata.unique_name` (e.g. `[Time].[Calendar]`) … the cube's unique name is in `metadata.infor_olap.cubeUniqueName`."* | column `metadata` + table `metadata_json` | ❌ |
| `analysis_services` | *"Every table records its model type in `metadata.analysis_services.modelType` … **Pick the language from the model type**"* (MDX vs DAX) | table `metadata_json` | ❌ |

The renderer emits only `metadata["kind"] or metadata["role"]` per column, so
`unique_name` is dropped; and no table-level branch exists, so `cubeUniqueName`,
`modelType` and `supportsDax` are dropped too.

`TableColumn.name` is the **caption** (`h["caption"]`, e.g. `Category`), not the MDX
identifier (`[Product].[Category]`). Captions are display strings — the bracket/hierarchy
structure is not derivable from them. Note the catalog/cube *are* fine: `Table.name` is
`f"{catalog}/{cube_name}"`.

Net effect: instructed to use `unique_name`, shown only captions. For Analysis Services it
is worse — the agent is told to choose **MDX vs DAX** from a field it cannot see.

### Tier 2 — semantics lost, so the agent writes *wrong* queries

| Connector | Lost | Why it matters |
|---|---|---|
| **`prometheus`** | `metric_type`, `unit` | **counter vs gauge decides whether `rate()` is required** — querying a counter without it returns meaningless monotonic values. `unit` decides whether a number is seconds or bytes. Neither is reliably in the metric name. |

Prometheus is arguably the most damaging of the five: Tier 1 failures are *loud* (the agent
can't find an ID), while this one is *silent* — the query runs and returns plausible,
wrong numbers.

### Tier 3 — no fix needed

| Connector(s) | Stored | Why it's fine |
|---|---|---|
| All SQL clients (`postgresql`, `mssql`, `bigquery`, `snowflake`, `oracledb`, `databricks_sql`, `redshift`, `teradata`, `vertica`, `spark_connect`, `ms_fabric`, `clickhouse`, `druid`, `sqlite`, `sap_hana`, `timbr`) | `schema` / `database` / `catalog` / `dataset` | All build `name=fqn` — the qualifier is already in the name. Pure duplication. |
| `splunk` | `index`, `sourcetype` | Already written into `description`: `"Splunk events: index='X', sourcetype='Y'…"` |
| `oracle_bi` | `subjectArea` | name is `"{subjectArea}/{table}"` |
| `sap_datasphere` | `space` | name is `"{space}/{asset}"` |
| `google_drive`, `graph_drive`, `s3`, `network_dir` | `file_id`, `mime_type`, paths | `data_shape="files"` — rendered by `files_schema_section` via `FileScopeItem`, a different path entirely |

---

## Severity check: `describe_tables` already exposes all of it

Before sizing this, one mitigation matters. The `describe_tables` tool returns the
**full** blobs — `"metadata": t.metadata_json` per table (:258) and
`"metadata": c.metadata` per column (:233). So none of this is unreachable; it is missing
from the **default schema context** but recoverable with an explicit tool call.

That reframes the whole document. The real cost is not "impossible" but:

1. **A wasted round trip** on every OLAP query, and on any Tableau/Qlik/Sisense/BO query
   needing an object id.
2. **Silent wrong answers when the agent doesn't make that call** — it has a cube, captions
   and roles in front of it, which *looks* sufficient, so nothing signals that MDX
   identifiers are missing. It will compose plausible unique names and fail (or worse,
   for Prometheus, succeed with wrong numbers).
3. **System prompts that reference invisible fields** (Tier 1b) — the instruction
   "reference members by `metadata.unique_name`" reads, in context, as though that data is
   present.

So: real, worth fixing, but a *latency-and-reliability* bug rather than a hard blocker.
Tier 1b is the one I would fix first — it is the only group where the agent is explicitly
told to use something it cannot see.

---

## What was actually built

A shared allowlist rather than more hardcoded branches, in
`app/ai/context/sections/tables_schema_section.py`:

- `_COLUMN_META_KEYS = ("unique_name",)` — surfaced per column alongside the existing
  `kind`/`role`. Fixes `analysis_services`, `sap_bw`, `infor_olap` in one change.
- `_TABLE_META_KEYS` — namespace → keys, currently `tableau: (datasourceLuid,)` and
  `analysis_services: (modelType, supportsDax)`.
- `_FLAT_META_KEYS = ("metric_type", "unit")` — Prometheus stores these unnamespaced;
  rendered as `<source_meta .../>`.
- Wired into **both** render paths (`_render_table_xml` and
  `_render_topk_tables_full`), which previously disagreed.

### The scope check changed the answer

Verifying each Tier 1 id against its client's `execute_query` — the check flagged as a
prerequisite — **dropped three of the four**:

| Connector | Finding | Outcome |
|---|---|---|
| `tableau` | `execute_query(datasource_luid, …)` — **required positional** | ✅ surfaced |
| `qlik_sense` | `execute_query(app=…)` accepts `table_name` as an alias | ❌ dropped |
| `sisense` | resolves `datamodelTitle` from `table_name` via `get_schema` | ❌ dropped |
| `businessobjects` | takes the universe **name**, not id | ❌ dropped |
| `infor_olap` / `sap_bw` | `cubeUniqueName` is `f"[{cube}]"`, and `cube` is segment 2 of `name` | ❌ dropped (derivable) |

So the table-level half shrank to Tableau + SSAS, and the **column-level** half — the
3-line `unique_name` change — turned out to be the valuable one.

### One inconsistency found while wiring it

`_render_table_xml` (used by `render()`) already had a Tableau branch emitting
`datasourceLuid`; `_render_topk_tables_full` (used by `render_combined()`, which is what
`agent_v2` actually calls) did not. Tableau's LUID was reaching one path and not the
other. Both now go through the shared helper.

### Tests

`tests/unit/test_source_metadata_context_rendering.py` — 10 cases covering both render
paths, additive-not-replacing behaviour, malformed `metadata_json`, and explicit
**negative** assertions that the dropped ids stay out so nobody "helpfully" re-adds them.

## Why this matters beyond the five connectors

The pattern — populate `metadata_json`, assume it reaches the model — is easy to repeat.
It nearly shipped in the Priority ERP spec (`docs/priority-erp-connector-analysis.md` §6e)
for exactly this reason. A generic renderer removes the trap for future connectors.
