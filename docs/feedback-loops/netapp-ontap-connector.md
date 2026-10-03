# Feedback Loop — NetApp tables and queries for root-cause investigations

The request is to connect BOW to the customer's on-premises ONTAP 9.14.1P9,
discover diagnostic tables, and query evidence for root-cause investigations.
This loop verifies the implementation against a synthetic HTTP API and the real
BOW registry, API, database, code executor and built UI. It does **not** certify
an appliance, patch release, hardware diagnosis or generated causal conclusions.

## Missing capability and fix

At base revision `935d456dc`, `resolve_client_class("netapp_ontap")` raised
`ValueError: Unknown data source type: netapp_ontap`; the connection picker had
no NetApp result. The registry now resolves the native client in
`backend/app/schemas/data_source_registry.py:853`.
`backend/app/data_sources/clients/netapp_ontap_client.py:139` exposes the table
contract; `:344` implements validated JSON queries; `:558` follows bounded,
same-resource pagination. No general route or database migration was added.

The frozen 9.14.1 specification has 504 GET operations: 464 resource profiles
and 40 explicit exclusions. Selectable tables total 466 after adding the
constituent view and volume-to-aggregate relationship. This is documented
coverage, not a statement that every appliance implements or authorizes it.

## Loop A — deterministic contracts

Use a fresh checkout of this branch and Python 3.12:

```bash
cd backend
uv sync --frozen --extra dev
mkdir -p db
export BOW_DATABASE_URL=sqlite:///db/netapp-tests.db
export TESTING=true
uv run pytest tests/unit/test_netapp_ontap_client.py -q --noconftest
uv run pytest tests/e2e/test_netapp_ontap.py tests/e2e/test_data_source.py tests/e2e/test_connection.py --db=sqlite -q
uv run pytest tests/unit/test_query_timeout.py tests/unit/test_query_concurrency.py tests/unit/test_query_cancellation.py tests/unit/test_usage_metering_buffer.py --db=sqlite -q
```

Observed initial red: the registry test failed with the unknown-source exception
above. Observed final green: **44 connector unit tests**, **1 NetApp API/database
end-to-end test**, **11 generic connection/data-source end-to-end tests**, and
**62 shared-executor regressions** (timeouts, concurrency, cancellation and usage).
The HTTP request boundary alone is replaced in deterministic connector tests;
the client, normalization, dispatch, schema indexing and code executor are real.

The executable RCA example queries volume capacity through BOW's
`StreamingCodeExecutor`, computes utilization and verifies sorting and query
capture. Its test supplies the connection identity normally attached by
`DataSourceService`; the client opts into JSON query capture in the shared executor.
No LLM-generated response is asserted.

Regression coverage includes short and empty continuation pages, preservation
of integers above 2^53, complete-result limits, scope/redirect rejection,
permission/unsupported distinctions, partial errors, transient retry,
retention-tier selection by incident age, missing data, logical/constituent
grains, parent scopes, structured counter arrays, and forbidden reads/controls.

Self-introduced defects caught and fixed during this loop:

- Optional `version.full` became `pd.NA`; boolean evaluation broke the real
  connection API despite successful HTTP. The API test now covers its absence.
- Singleton requests initially inherited collection-only controls. A regression
  test checks that the cluster request only sends applicable projection controls.
- Constituent discovery needed a separate view to preserve logical capacity grain.
- Dictionary queries needed explicit query-history capture in BOW's executor;
  the NetApp client opts into capture so JSON evidence is retained.
- The integration harness inferred `NetappOntapClient`; it now uses the production
  registry's explicit `NetAppOntapClient` mapping for this source.

## Loop B — actual HTTP against a simulated API

From the repository root, choose throwaway credentials and start the server:

```bash
export NETAPP_USERNAME='<local-test-user>'
export NETAPP_PASSWORD='<local-test-password>'
python3 tools/netapp/simulated_api.py --port 18091
```

In another terminal, export the same throwaway values, then:

```bash
cd backend
export BOW_DATABASE_URL=sqlite:///db/netapp-tests.db
export NETAPP_URL=http://127.0.0.1:18091
export NETAPP_USERNAME='<same-local-test-user>'
export NETAPP_PASSWORD='<same-local-test-password>'
export NETAPP_SIMULATOR=true
PYTHONPATH=. uv run python ../tools/netapp/verify.py --simulator \
  --queries ../tools/netapp/simulator_queries.json --output /tmp/ontap-report.json
uv run pytest tests/integrations/ds_clients.py -k netapp_ontap --db=sqlite -q
```

Observed: **12/12 diagnostic queries passed** through actual HTTP, with forced
two-row pages; **1 integration test passed**, 21 unrelated connector cases
deselected. The output report contains no row data, object identifiers,
credentials or source URL. Do not run the supplied synthetic parent IDs on a
customer system.

The simulator models representative inventory, capacity relationships, SAN,
EMS, performance, replication, snapshots, NFS exports and counters. It deliberately
does not implement all 464 endpoints or faithfully reproduce ONTAP filtering,
projection, retention and operating conditions. The request contracts and
failure paths are additionally checked by Loop A.

## Loop C — production build and UI

For a fresh local sandbox, use `tools/agent/boot_stack.sh` and
`tools/agent/seed_org.py`; set an isolated SQLite database and test credentials.
Then create the NetApp connection, use the loopback simulator URL and explicitly
enable **Allow HTTP for a simulator**. Exercise Test connection → Save and
Continue → schema discovery → Connect. Create a data agent, choose that
connection and inspect the tables selector.

This run used a separate worktree and SQLite database, backend port 18000,
frontend port 13000 and simulator port 18091 to avoid the existing developer
stack. The frontend production build used:

```bash
cd frontend
BOW_API_TARGET=http://127.0.0.1:18000 NODE_OPTIONS=--max-old-space-size=4096 npm run build
PORT=13000 HOST=127.0.0.1 node .output/server/index.mjs
```

Observed build: exit 0, `Build complete!`. The first attempt ran while Nuxt dev
held its build lock; stopping that isolated dev server allowed the build.
A shared-executor test attempt incorrectly used `--noconftest` for database-dependent
usage tests, causing missing ORM-model registration; those checks passed after restoring normal
conftest loading (62 passed, no skips).

The build emits warnings; this report does not claim warning-free output.

UI evidence lives in `media/pr/netapp-rca-connector/`: matched 1440×1000 picker
screenshots, successful connection test and connection creation, table selection,
and an actual browser flow GIF. The before image used the base registry loaded
in a separate backend on port 18001; the after flow used the final production
frontend and final backend. No customer credentials or data were used.

No LLM was configured in this isolated application. A natural-language prompt
was therefore not verified end to end. The actual generated-code execution path
is covered deterministically in Loop A; prompt quality remains a separate check.

## Customer acceptance and scope

Follow `tools/netapp/README.md` to run the same GET-only verification kit against
HTTPS using local credential environment variables and explicitly chosen real
parent IDs. Do not broadly crawl every endpoint. Reconcile representative values
with System Manager/CLI at matching grain and time, verify exact P9/local Swagger,
role permissions, hardware and FC, and record absent history.

Current topology cannot prove past placement. Native performance history is not
a historical capacity/configuration store. Raw counters require their definitions
and appropriately timed samples. CLI-only diagnostics, excluded operations and
external switch/host evidence remain visible coverage gaps. The release state is
**implemented and simulated-API verified; awaiting customer validation**.

## PR review follow-up — catalog/executor contract

Review of PR #1122 found six valid issues. The generator at the original
`tools/netapp/generate_catalog.py:175` read operation parameters only, losing
path-item parameters on 19 resources. The runtime at the original
`netapp_ontap_client.py:378` could then send an unsubstituted path and attribute
its failure to the appliance. The generator also discarded NFS's documented
`timestamp` filter because response timestamps are nested under protocol versions.

The additional regressions first produced **13 failures** against the reviewed
code. Generator/loader guard tests were separately run with the original generator
and client restored temporarily: **7 failures**, then restored to the fixed code.
The permanent tests are `test_netapp_ontap_client.py` and
`test_netapp_catalog_generator.py`; run both with `--noconftest` for the pure
HTTP-boundary/contract loop. The same API/database commands above remain valid.

The corrected generator resolves parameter references, merges path and operation
parameters by `(in, name)` with operation overrides, and applies the result to
parents, filters and controls. Generation and loading reject inconsistent parent
or history contracts; execution independently refuses leftover braces before HTTP.
NFS remains historical, with the documented time filter and nested timestamps,
durations and status fields retained in results. All three transport controls are
sent only when declared. Empty/blank cluster UUIDs fail explicitly; retrieval times
have UTC datetime dtype, including empty and derived frames. Numeric parents are
validated against both the path parameter and any corresponding numeric column
before making a request; parent-only identity columns retain their existing string
representation.

Observed follow-up results: **65 unit/generator tests passed**, including request
construction for all **465 REST-backed selectable tables** (464 profiles plus the
constituent view), catalog-wide parent/time invariants, and both empty/populated
retrieval timestamps. **12 API/database tests passed**. The expanded simulator
verified **20/20 actual HTTP diagnostic queries**, adding FC fabric switches/zones,
FC interface detail, NFS history, a numeric sensor identifier and all three
control-free resources. The simulator rejects undeclared controls on those three
resources and requires NFS time bounds. The standard NetApp integration check
also passed. Catalog regeneration was checked for exact deterministic equality.

These tests establish structural request correctness across the catalog and
representative HTTP behavior. They still do not establish that all 466 tables
return data on an actual appliance. Existing NetApp connections indexed before
this fix should refresh their schemas to pick up the corrected parent metadata.
The frontend was unchanged by this follow-up; the previously captured build/UI
evidence remains applicable.
