# Feedback Loop — nine new connectors: object stores, NoSQL, lakehouse

New connection types: **Google Cloud Storage** and **Azure Blob Storage** (file
sources, built like Amazon S3), **Apache Iceberg** (REST catalog, AWS Glue,
Amazon S3 Tables), **Amazon DynamoDB**, **Azure Cosmos DB**, **Couchbase**,
**Apache Cassandra** (also ScyllaDB / Amazon Keyspaces), **Dremio**, and
**Apache Hive / Impala** (HiveServer2, also Spark Thrift). The claim validated
here: every one connects, indexes its catalog, answers real agent questions
with correct numbers, caps unbounded reads, and refuses writes.

## Design (validated)

- **Object stores share one implementation.** The provider-neutral logic of
  the S3 client (prefix confinement, include-globs, junk filtering, parsing,
  windowed reads, resumable grep, catalog) moved to
  `backend/app/data_sources/clients/_object_store_common.py`
  (`ObjectStoreClient`). `s3_client.py`, `gcs_client.py` and
  `azure_blob_client.py` implement only six storage primitives. S3 keeps its
  exact AWS call sequence — `tests/unit/test_s3_client.py`,
  `test_grep_files.py`, `test_large_file_streaming.py` pass unmodified.
- **NoSQL clients are read-only by construction**: PartiQL / CQL / SQL++ /
  Cosmos SQL statements go through `ensure_single_select`
  (`_document_schema.py`) before anything is sent; Couchbase queries also
  carry the server-side `readonly` flag. Schemaless stores infer columns from
  a bounded sample; partition keys, sort keys and indexes are written into
  each table's description so the agent queries by key. Every read is capped
  and flags `df.attrs["truncated"]`.
- **Iceberg is catalog-first** (`iceberg_client.py`): PyIceberg plans each
  scan with column projection and, for single-table queries, WHERE conjuncts
  pushed down for partition pruning (date strings coerced to the column type —
  without this, `order_ts >= '2025-03-01'` failed to bind and pruning was
  silently skipped). A scan larger than `max_scan_rows` is refused; the SQL
  runs in an in-memory DuckDB over the Arrow results — no DuckDB network
  extensions, so it works air-gapped. Time travel via `snapshot_id` / `as_of`.
- **Dremio / Hive / Iceberg** are registered in the sandbox SQL guard
  (`sql_guard.CLIENT_DIALECTS`: dremio / hive / duckdb). Dremio's HTTP goes
  through `app.core.egress.pinned_session` (the repo's outbound policy).

Bugs the loops caught during development (all fixed, all now covered by tests):
Azure's ranged download reports the *range* length as `properties.size`
(cursor total was wrong → `test_windowed_read_reports_the_object_total`);
the Iceberg date-literal pushdown above
(`test_iceberg_refuses_scans_over_the_limit_but_prunes_partitions`); Dremio
initially used `requests` directly (`test_connector_egress.py`).

## Loop A — deterministic (no Docker, no credentials)

```bash
cd backend
uv run pytest tests/unit/test_object_store_clients.py tests/unit/test_nosql_clients.py \
  tests/unit/test_lakehouse_clients.py tests/unit/test_s3_client.py tests/unit/test_grep_files.py \
  tests/unit/test_large_file_streaming.py tests/unit/test_connector_egress.py -n 4
cd ../frontend && node tests/unit/useDataSourcePicker.mjs
```

Observed: `457 passed`; `data source picker: popular subset + search + category scoping`.
Iceberg runs end to end here against PyIceberg's SQL catalog on a temp dir
(planning, pruning, time travel, DuckDB execution). Breaking the date coercion
fails exactly the three pruning tests; reverting the Azure Content-Range fix
fails exactly the four window tests.

## Loop B — real servers (Docker)

```bash
cd backend
uv run pytest tests/integrations/test_object_store_live.py   # fake-gcs-server + Azurite
uv run pytest tests/integrations/test_nosql_live.py          # DynamoDB Local, Cassandra 5, Couchbase 7.6, Cosmos emulator
uv run pytest tests/integrations/test_lakehouse_live.py      # Iceberg REST fixture, Dremio OSS, HiveServer2
```

Observed: 38 + 24 + 17 passed (1 intended skip: Iceberg refuses oversized
scans instead of truncating). Each backend is seeded with the same orders and
held to one contract: catalog lists the table and its key, a key-scoped or
partition query returns exactly the seeded rows, an aggregate matches the
seed, a full read is capped and flagged, writes are refused and data stays
intact. Containers are stopped even when seeding fails half-way.

## Loop C — the real app + a real LLM (Claude Haiku 5.5)

```bash
tools/agent/boot_stack.sh --dev && (cd backend && uv run python ../tools/agent/seed_org.py)
docs/feedback-loops/new-connectors/servers.sh      # every backing server, in Docker
(cd backend && uv run python ../docs/feedback-loops/new-connectors/objstore_smoke.py)   # seeds GCS + Azure
docs/feedback-loops/new-connectors/seed_all.sh     # seeds the rest
cd backend && ANTHROPIC_API_KEY=... uv run python ../docs/feedback-loops/new-connectors/live_agent_loop.py
# real AWS (optional): AWS_* env, SUF=<namespace suffix>, ICEBERG_S3TABLES_ARN=<table bucket ARN>, add --aws
```

For each connector the script creates the connection through `POST
/api/data_sources` (what the connect form posts), activates its tables, asks
the agent a question whose answer is known from the seed, and checks the
numbers in the stored answer. Observed — **12/12 PASS**:

| Connector | Agent tool | Question → answer |
|---|---|---|
| GCS | read_file | AMER revenue in sales/2025/q1.csv → 340 |
| Azure Blob | grep_files | ERROR line in logs/app.log → `ERROR café exploded` |
| Cassandra | create_data | total for customer c-1 → 1015 |
| Couchbase | create_data | revenue of OPEN orders → 2062.5 |
| Cosmos DB | create_data | total for customer c-3 → 1080 |
| DynamoDB (Local) | create_data | SHIPPED orders → 20 |
| Iceberg (REST) | create_data | orders in March 2025 → 150 |
| Dremio | create_data | top region by revenue → APAC 205,725 |
| Hive | create_data | rows in partition 2025-03 → 150 |
| DynamoDB (**AWS**) | create_data | customer c-2 → 10 orders, 1,047.5 |
| Iceberg on **S3 Tables** (AWS) | create_data | AMER amount → 205,275 |
| Iceberg on **Glue** (AWS) | create_data | gold-tier orders (join) → 450 |

UI evidence (`catalog_shots.mjs`, `ui_flow.mjs`): the add-connection catalog
before/after, schema-generated forms for Cassandra / Iceberg / GCS / Dremio
filled and connected through the UI ("Discovered N tables"), and agent answers
rendered on the report page — in `media/pr/compassionate-cori-33e8g6/`.

## What this proves / regression notes

- Every new type resolves through its explicit `client_path`, every auth
  variant's fields are accepted by its client, and every description tells the
  agent the source is read-only and how to query it.
- The add-connection modal now searches the per-type aliases too, so "nosql",
  "lakehouse", "bucket", "impala", "scylla", "adls" find the right tiles
  (`frontend/tests/unit/useDataSourcePicker.mjs`). Amazon S3 moved to the
  *Files & object store* category next to GCS and Azure Blob.
- Pre-existing, unrelated: 168 unit tests in `test_sharepoint_*` and
  `test_egress.py` fail in this sandbox on the base commit too (identical
  failure list with the change stashed in a base worktree).
- Not covered live: Impala itself (no container image; it shares the
  HiveServer2 path with Hive), Kerberos for Hive, Dremio Cloud, Cosmos DB with
  Entra auth, GCS/Azure against the real clouds (emulators only).
