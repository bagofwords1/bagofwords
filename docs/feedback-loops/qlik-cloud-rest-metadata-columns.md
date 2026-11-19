# Feedback loop — Qlik Cloud discovers tables without columns

All examples, fixtures, screenshots, and counts below use synthetic data.
No customer identifiers, credentials, tenant URLs, production logs, or production
data are included.

## Root cause

The Cloud REST mapper read `fields[].srcTables` and `tables[].rows`.
[Qlik's REST contract](https://qlik.dev/apis/rest/apps/) uses
`src_tables` and `no_of_rows`. The old fixture repeated the incorrect names.
A populated raw response passed the discovery check even when its mapped
tables had no columns.

Relevant code:

- `backend/app/data_sources/clients/_qix_common.py`: REST membership,
  relationships, row counts, and system-table exclusion.
- `backend/app/data_sources/clients/qlik_sense_client.py`: mapping validation,
  Engine/REST fallback, scoped app resolution, and unread-app reporting.

## Loop A — synthetic reproduction and regression suite

From the repository root, use Python 3.13 and the backend development dependencies:

```sh
cd backend
BOW_DATABASE_URL=sqlite:///db/app.db TESTING=true uv run pytest \
  tests/unit/test_qlik_cloud_catalog.py \
  tests/unit/test_qlik_sense_client.py \
  tests/unit/test_qlik_sense_onprem_client.py \
  tests/unit/test_qlik_query_contract.py \
  tests/unit/test_qlik_onprem_sheet_objects.py \
  tests/unit/test_qlik_onprem_dynamic_measures.py \
  tests/unit/test_qlik_nested_variables.py -q --disable-warnings
```

A minimal metadata fixture with one `Orders` table, `no_of_rows: 5`, and an
`Amount` field assigned through `src_tables: ["Orders"]` produces:

| Mapper | Columns | Row count |
|---|---:|---:|
| Base implementation | 0 | absent |
| Corrected implementation | 1 | 5 |

The generalized tests cover documented REST shapes, zero row counts,
relationships, excluded system tables, incomplete-metadata fallback, partial
Engine failures, total failure, scope enforcement, query resolution beyond an
indexing cap, and Cloud/on-prem semantic catalog parity. They exercise public
client methods through HTTP/WebSocket boundaries.

## Loop B — actual HTTPS and WebSocket transport, synthetic server

```sh
cd backend
BOW_DATABASE_URL=sqlite:///db/app.db TESTING=true uv run pytest \
  tests/unit/test_qlik_cloud_catalog.py::test_cloud_catalog_and_object_query_over_real_tls \
  -q --disable-warnings
```

This test starts a local TLS server on an ephemeral port, serving synthetic
REST app listings and the repository's fake Qlik Engine protocol. It tests
connection, catalog discovery, and object execution over actual HTTPS/WSS.
A throwaway self-signed certificate is used with verification explicitly off;
no external Qlik tenant or live credential is needed.

## The changes

- Read the documented REST keys and omit system tables and links to them.
- Validate business-table column coverage before accepting REST metadata.
- Share Engine catalog and object-query code in `_qix_catalog.py`, keeping
  Cloud bearer/OAuth and on-prem certificate/QRS adapters separate.
- Discover master measures/dimensions, variables, sheets and chart objects,
  dynamic chart choices, lineage, script context, and field profiles.
- Render Cloud semantic entry types and sheet descriptions in agent context.
- Support the discovered objects through object/search/variable queries.
- Apply space/app filters before the optional indexing cap; queries resolve
  full table paths against the uncapped configured scope. With no filters,
  legacy direct app-ID queries remain possible.
- Default Cloud indexing concurrency to four, expose an app filter and a
  one-to-four concurrency selector, and retain constructor options for
  master items, lineage, sheet objects, and indexing caps.
- Preserve earlier semantic entries when Engine fails but a valid REST model
  remains readable; mark the fallback with a discovery warning. If all apps
  fail, raise an indexing error rather than claiming an empty successful crawl.
- Keep private sheets out of catalogs for shared Cloud spaces.

The extraction preserves the on-prem query implementation. Cloud's existing
ad-hoc hypercube path remains compatible; object, search, and variable queries
use the shared implementation.

## UI evidence

`media/pr/qlik-cloud-catalog/` contains English before/after and Hebrew RTL
screenshots from a fresh local database with a synthetic demo admin.

The before image uses the base connection schema (the two newly added fields
removed from the schema response). The after images use the updated schema.
The enterprise tile's license flag is cleared only in the browser's test
response to make the form selectable without a license; backend licensing is
unchanged. App Filter input and concurrency selection were exercised. No
connection was created against a real service and no credential appears.

## Limits and deployment

The transport test is a synthetic protocol integration, not a live Qlik tenant
certification. UI evidence verifies the generated form, not an end-to-end
licensed connection/create/index/chat flow. Optional Engine metadata remains
permission-dependent. Rich discovery opens apps in Engine and costs more than
the previous REST-only path; reduce concurrency for large models.

Deploy through the normal release process, then reindex connections to replace
old zero-column catalogs. This PR does not deploy or modify production.
