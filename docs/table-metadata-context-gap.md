# Table-level `metadata_json` never reaches agent context

**Status:** Bug report / analysis. No implementation.
**Found:** 2026-07-25, while specifying the Priority ERP connector — but this is independent
of Priority and worth fixing on its own.

---

## The bug

`Table.metadata_json` is populated by **34 of 38** data-source clients and rendered for
**three**. Everything else is written to the catalog and silently dropped before the agent
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

Most of the 34 are **false alarms**: the value is already recoverable from
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

## Suggested fix: one generic renderer, not five more branches

Adding a fourth, fifth and sixth hardcoded branch repeats the mistake. A small
namespace→keys allowlist covers every case above and makes the next connector a one-line
change rather than a code change in two files.

```python
# tables_schema_section.py — surface identifying metadata the agent needs to
# address an object. Allowlisted per namespace so we emit IDs, not noise
# (rowCount, dashboards[], fields_sampled…).
_META_KEYS = {
    "tableau":         ("datasourceLuid", "projectName"),
    "qlik_sense":      ("appId", "spaceId"),
    "sisense":         ("datamodelId",),
    "businessobjects": ("universe_id", "universe_name"),
}

def _render_source_metadata_xml(t) -> str:
    meta = t.metadata_json if isinstance(t.metadata_json, dict) else {}
    for ns, keys in _META_KEYS.items():
        blob = meta.get(ns)
        if isinstance(blob, dict):
            attrs = {k: str(blob[k]) for k in keys if blob.get(k) not in (None, "")}
            if attrs:
                return xml_tag(ns, "", attrs)
    return ""
```

Prometheus is flat rather than namespaced (`{"metric_type":…, "unit":…}`), so it needs
either its own two-line case or — better — a `role`/`description` carry at the **column**
level, which already renders with no change at all.

**Scope check before building:** confirm each Tier 1 ID is actually what the
corresponding client's `execute_query` needs. If a client already resolves names→IDs
internally on every call, surfacing the ID is cosmetic and that row should be dropped.

---

## Why this matters beyond the five connectors

The pattern — populate `metadata_json`, assume it reaches the model — is easy to repeat.
It nearly shipped in the Priority ERP spec (`docs/priority-erp-connector-analysis.md` §6e)
for exactly this reason. A generic renderer removes the trap for future connectors.
