# Feedback Loop — IBM Db2 and IBM i connectors (new)

Validates two new data source connectors end to end: schema-driven connect
form → connection test → schema discovery (tables, views, comments, primary
and foreign keys) → table preview → agent chat on a real LLM (Claude 4.5
Haiku) → read-only enforcement.

Db2 ships as three products with one SQL dialect but three system catalogs,
and the drivers split them the same way:

| Product | Catalog | Connector | Driver |
|---|---|---|---|
| Db2 LUW (Linux/Unix/Windows, Db2 on Cloud, Db2 Warehouse) | `SYSCAT.*` | `db2` | `ibm_db` (free for LUW) |
| Db2 for z/OS (mainframe) | `SYSIBM.SYS*` | `db2` | `ibm_db` + Db2 Connect (usually activated on the server) |
| Db2 for IBM i (AS/400) | `QSYS2.SYS*` | `ibmi` (recommended) | Mapepire: pure Python, no license, any architecture |
| Db2 for IBM i, alternative | `QSYS2.SYS*` | `db2` with product `ibmi` | `ibm_db` + Db2 Connect |

`db2` detects the product from the driver's DBMS name (`DB2/LINUXX8664` →
LUW, `DB2` → z/OS, `AS` → IBM i) and reads the matching catalog; the
**Db2 product** field forces it.

## What was added

- `backend/app/data_sources/clients/_db2_catalog.py`: catalog SQL per
  product (objects, columns, PKs, FKs, all with the same column order), one
  row mapper, system-schema rules (`SYS*`/`NULLID`/`SQLJ` on LUW, plus `DSN*`
  on z/OS, `Q*`/`SYS*` on IBM i), the Db2 SQL hints for the agent, and
  product detection.
- `backend/app/data_sources/db2_dialect.py`: a minimal SQLAlchemy dialect
  over `ibm_db_dbi` so Db2 uses the shared pooled engines (`engine_pool`),
  like Teradata. Queries go to the driver as text.
- `backend/app/data_sources/clients/db2_client.py` (`Db2Client`): TLS
  (`SECURITY=SSL` plus a pasted CA written to a file for the CLI driver),
  `CURRENTSCHEMA` from the first listed schema, and readable errors (SQL1598N
  explains the Db2 Connect license).
- `backend/app/data_sources/clients/ibmi_client.py` (`IbmiClient`):
  Mapepire SQL job with `naming=sql` and `access=read only`, terse
  (array) rows, and real certificate verification.
- `Db2Config`/`Db2Credentials`, `IbmiConfig`/`IbmiCredentials` and registry
  entries `db2` (`requires_module="ibm_db"`, hidden where the driver is
  missing) and `ibmi`.
- `sql_guard.py`: sqlglot has no Db2 dialect, so a Db2 read-only check
  allows one statement starting with SELECT/WITH/VALUES and refuses generic
  write keywords plus Db2-specific ones (`QCMDEXC`, `ADMIN_CMD`, `LOCK`,
  `REFRESH`, `SET`, …). `SELECT … FROM FINAL TABLE (INSERT …)` is caught.
- `table_preview_service.py`: `fetch_first` bound (`FETCH FIRST n ROWS
  ONLY` works on every Db2 release; `LIMIT` does not).
- `connection_service`: `db2_uri` disposes pooled engines on edit or delete.
- Icons `frontend/public/data_sources_icons/{db2,ibmi}.png` (IBM Db2 logo
  for `db2`, the Db2 wordmark for `ibmi`) and aliases in `DataSourceIcon.vue`.
- Dependencies: `ibm-db` (marker `platform_machine != 'aarch64'`, as IBM
  publishes no Linux ARM64 build) and `mapepire-python`.

## Findings fixed during the loop

1. **Mapepire drops same-named columns.** Its default result format keys
   each row by column name, so `SELECT a.ID, b.ID` lost a column and the
   IBM i FK query (`TABLE_SCHEMA` twice) returned nothing. The client now
   asks for terse rows. The DB-API cursor cannot request them on a prepared
   statement, so it drives the query API directly, and the catalog queries
   alias every column uniquely.
2. **"Verify certificate" verified nothing in Mapepire's client.** Given no
   CA, it downloads the server's own certificate and trusts it. With
   verification on, the connector passes the pasted certificate or the
   system trust store, so an unknown self-signed certificate is refused.
3. Long port help text squeezed the narrow port column in the form;
   shortened (the Mapepire install hint moved to the Host field).
4. `SYSCAT.REFERENCES` is written `SYSCAT."REFERENCES"` (same object in Db2)
   so the catalog SQL also runs on the sqlite stand-ins in unit tests.

## Simulation (no mainframe or IBM i needed)

Only Db2 LUW runs in a container (`icr.io/db2_community/db2`, ~2.6 GB,
privileged, about 2 minutes to first boot). The other two products are
simulated on it:

- `tests/integrations/db2_sim.py` seeds a commented `SALES` schema (PKs, FKs,
  ~200 order lines) and builds stand-in views: `ZOSSIM.SYSTABLES/SYSCOLUMNS/
  SYSRELS/SYSFOREIGNKEYS` with z/OS column layouts (the client points at them
  through the test-only `Db2Client.catalog_schemas`), and
  `QSYS2.SYSTABLES/SYSCOLUMNS/SYSCST/SYSKEYCST/SYSREFCST` with the IBM i
  layouts under the real schema name.
- `tests/mocks/fake_mapepire.py` speaks Mapepire's protocol (JSON over a TLS
  WebSocket at `/db/`, Basic auth, `connect` / `prepare_sql_execute` /
  `sqlmore` / `sqlclose`, terse and keyed rows) in front of any DB-API
  database. IBM's real `mapepire-python` client runs unchanged against it.
- `tests/mocks/db2_catalog_sqlite.py` builds all three catalogs in sqlite for
  unit tests.

Not simulated: Db2 Connect licensing, EBCDIC sort order, CCSID 65535 columns,
IBM i system naming (`LIB/FILE`), z/OS-specific catalog quirks beyond the
columns read, and Mapepire server behaviours beyond the requests above.

## Run it

```bash
# 1. Db2 LUW
docker run -d --name db2 --privileged -p 50000:50000 -e LICENSE=accept \
  -e DB2INSTANCE=db2inst1 -e DB2INST1_PASSWORD=<pw> -e DBNAME=TESTDB \
  -e ARCHIVE_LOGS=false -e AUTOCONFIG=false icr.io/db2_community/db2:latest
# wait for "Setup has completed" in `docker logs db2`
export DB2_TEST_DSN="DATABASE=TESTDB;HOSTNAME=127.0.0.1;PORT=50000;PROTOCOL=TCPIP;UID=db2inst1;PWD=<pw>;"
cd backend && uv run python tests/integrations/db2_sim.py "$DB2_TEST_DSN"

# 2. Simulated IBM i for the UI (prints the port; writes its cert)
FAKE_MAPEPIRE_CERT_OUT=/tmp/mapepire.pem FAKE_MAPEPIRE_USER=ANALYST FAKE_MAPEPIRE_PASSWORD=<pw> \
  PYTHONPATH=. uv run python tests/mocks/fake_mapepire.py "$DB2_TEST_DSN" 8076

# 3. Tests
uv run pytest tests/unit/test_db2_client.py tests/unit/test_ibmi_client.py -q   # 74 passed
uv run pytest tests/integrations/test_db2_live.py -v                            # 6 passed
# shared harness, container mode: integrations.json
#   {"data_sources": {"db2": {"enabled": true, "container": true},
#                     "ibmi": {"enabled": true, "container": true}}}
uv run pytest tests/integrations/ds_clients.py -k "db2 or ibmi" -q              # 2 passed (~2 min)
uv run pytest tests/e2e/test_data_source.py tests/e2e/test_connection.py --db=sqlite -q  # 13 passed
```

## Results (2026-11-11, Db2 12.1.5 Community, Claude 4.5 Haiku)

- Unit: 74 passed. Every test was checked to fail when its fix is reverted:
  terse rows, system CA trust, the guard hook, the read-only job property,
  the z/OS FK join, and the IBM i `Q*` exclusion.
- Live: LUW detected (`DB2/LINUXX8664 12.01.0500`); the same model is
  discovered through the LUW, z/OS and IBM i catalogs, and IBM i over
  Mapepire across several result batches.
- UI, `db2`: the form renders from `Db2Config`; Connect → "Discovered 4
  tables"; the table preview reads `SELECT … FROM "SALES"."ORDERS" FETCH
  FIRST 100 ROWS ONLY`; the agent overview Haiku generated cites the status
  and region code comments and the FK join path straight from the catalog.
- Chat, `db2`: "total revenue by region in 2026, excluding cancelled
  orders" → bar chart. The generated SQL used `YEAR()`, `TRIM()` and the
  status code, and the figures (AMER 267,500 / APAC 147,100 / EMEA 102,900)
  match a direct Db2 query.
- UI and chat, `ibmi`: certificate pasted in the form, 4 tables discovered,
  "top 5 customers by 2026 revenue" ran `… FETCH FIRST 5 ROWS ONLY` through
  Mapepire.
- Read-only: asked to run `DELETE … WHERE STATUS = 'C'`, Haiku refused
  before running anything (all 9 cancelled orders remain). The code-level
  guard, driven through `QueryCapturingClientWrapper.execute_query`, refuses
  the DELETE on both connectors and still runs the SELECT.

## Known gaps

- **Per-table scope enforcement does not apply to Db2/IBM i**, the same as
  SAP HANA and Vertica today: it needs a sqlglot dialect, and sqlglot
  rejects about a third of everyday Db2 SQL (labeled durations such as
  `CURRENT DATE - 30 DAYS`). The agent only sees selected tables in its
  schema, but a query naming another table is not blocked. A sqlglot Db2
  dialect would close this.
- No server-side cancel: `ibm_db` binds no SQLCancel and Mapepire has no
  cancel request, so a timed-out query keeps running on the server.
- Linux ARM64 images do not get `db2` (no IBM driver build); `ibmi` works
  there.
- z/OS and real IBM i are untested against live systems; the first customer
  connection is the real check.
