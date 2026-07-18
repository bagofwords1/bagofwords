# Feedback Loop — SAP HANA / Datasphere connector (new)

Validates the new `sap_hana` data source connector end-to-end: schema-driven
admin form → connection → live schema discovery (tables **and views** from
`SYS.TABLE_COLUMNS` / `SYS.VIEW_COLUMNS`, with comments and primary keys) →
SQL execution returning DataFrames — against a real SAP HANA Express
container.

One client covers the whole SAP HANA family, because they all speak the same
SQL over the same wire protocol (`hdbcli`, SAP's official DBAPI driver):

- **SAP HANA / HANA Express** (on-premise): host + SQL port (e.g. 39041),
  optionally `encrypt=false`.
- **SAP HANA Cloud**: hostname `…hanacloud.ondemand.com`, port 443, TLS
  mandatory (the config defaults — `port=443`, `encrypt=true`).
- **SAP Datasphere** (the ask that motivated this connector): Datasphere runs
  on HANA Cloud. A space admin creates a *database user* (`SPACE#NAME`) and
  views marked **"Expose for Consumption"** become plain SQL views in the
  space schema. Set `schema` to the space schema. Two tenant-side
  prerequisites the connect form can't do for the user: the backend's public
  IP must be on the tenant's **IP allowlist**, and only exposed views are
  visible. Views being first-class in discovery is why `get_tables()` unions
  `VIEW_COLUMNS` in — on Datasphere there are *only* views.

Modeled on the in-repo **oracledb** connector (plain SQL DB, enriched
introspection with comment fallback, comma-separated multi-schema scoping).
Community data source (NOT enterprise-gated).

## What was added

- `backend/app/data_sources/clients/sap_hana_client.py` — `hdbcli` DBAPI
  client. Enriched discovery (tables + views + comments + PKs from
  `SYS.CONSTRAINTS`) falling back to a basic no-comments query on permission
  errors; default system-schema exclusion (`_SYS*`, `SAP_*`, `SYS`, …) unless
  schemas are scoped explicitly; schema case is **preserved** (Datasphere
  space schemas are case-sensitive). `description` teaches the coder agent
  HANA SQL: double-quoted case-sensitive identifiers, `LIMIT`, `FROM DUMMY`,
  `ADD_DAYS`, and the Datasphere exposed-views rule.
- `SapHanaConfig` / `SapHanaCredentials` in
  `backend/app/schemas/data_sources/configs.py`; registry entry (type
  `sap_hana`, explicit `client_path`, auth `userpass` with system+user
  scopes) in `backend/app/schemas/data_source_registry.py`.
- `hdbcli>=2.29.25` dependency (manylinux wheels, no system libraries).
- `frontend/public/data_sources_icons/sap_hana.png` + type aliases
  (`hana`, `sap_datasphere`, `datasphere` → `sap_hana`) in
  `DataSourceIcon.vue`.
- `backend/tests/unit/test_sap_hana_client.py` (23 tests).
- `tools/hana/` — reproducible local environment: `docker-compose.yaml`
  (saplabs/hanaexpress, sysctls + license flag + password-file mount) and
  `seed_hana.py` (BOW_DEMO schema: 2 tables with comments/PKs + 1 view,
  mirroring Datasphere's view-consumption shape).
- `sap_hana` in `DATA_SOURCES` (remote mode) in
  `backend/tests/integrations/ds_clients.py`.

## Loop A — deterministic reproduction (no external services)

Mock the driver boundary; runs in a clean sandbox with no HANA server.

```bash
cd backend
uv run pytest tests/unit/test_sap_hana_client.py -v   # 23 passed
uv run python -c "from app.schemas.data_source_registry import resolve_client_class; print(resolve_client_class('sap_hana'))"
```

## Loop B — live HANA Express container

HANA Express needs ~8GB RAM and 4–10 minutes for first boot. Verified on a
sandbox whose file-descriptor hard limit is 4096 — HANA 2.00.088 boots fine
without the recommended `nofile` ulimit (compose file documents this).

```bash
mkdir -p /data/hxe
echo '{"master_password": "HXEHana1"}' > /data/hxe/password.json
chown 12000:79 /data/hxe/password.json && chmod 600 /data/hxe/password.json
docker compose -f tools/hana/docker-compose.yaml up -d
docker logs -f bow-hana          # wait for "Startup finished!" (~4 min)
python tools/hana/seed_hana.py   # pip install hdbcli
```

Then, with `tests/integrations/integrations.json`:

```json
{"data_sources": {"sap_hana": {"enabled": true, "host": "127.0.0.1", "port": 39041,
  "user": "SYSTEM", "password": "HXEHana1", "schema": "BOW_DEMO", "encrypt": false}}}
```

```bash
cd backend
uv run pytest tests/integrations/ds_clients.py -k sap_hana -v   # 1 passed
uv run pytest tests/e2e/test_data_source.py tests/e2e/test_connection.py --db=sqlite -q
```

Live results observed (2026-07-15, HANA Express 2.00.088):

- `test_connection()` → success.
- `get_schemas()` with `schema="BOW_DEMO"` → 3 objects: `CUSTOMERS` (table,
  comment "Demo customers", PK `ID`, column comment on `COUNTRY`), `ORDERS`
  (table, PK `ID`), `V_REVENUE_BY_COUNTRY` (**view** — the Datasphere shape).
- `get_schemas()` unscoped → system schemas (`SYS`, `_SYS_*`, …) excluded,
  only `BOW_DEMO` returned.
- `execute_query('SELECT … FROM "BOW_DEMO"."V_REVENUE_BY_COUNTRY" ORDER BY
  REVENUE DESC LIMIT 10')` → 3-row DataFrame with correct aggregates.
- `prompt_schema()` renders all three objects with PKs and comments.

## Loop C — live UI pass (schema-generated form)

`tools/agent/boot_stack.sh --dev` + `seed_org.py`, then
`tools/agent/sap_hana_connect.mjs` (Playwright; run from a copy inside
`frontend/`, e.g. `frontend/.agent-tmp/`) drives the real AddConnectionModal:
catalog search "sap" → SAP HANA tile (brand icon) → form fill → Test
Connection → create → schema discovery → Tables Selector. Evidence in
`media/sap-hana/`:

- `01-catalog-sap.png` — tile under "Databases & warehouses".
- `03-sap-hana-form-filled.png` / `04-test-connection-result.png` — the
  schema-generated form; Test Connection returns **"Connected successfully.
  Found 3 tables."**
- `06-schema-tables.png` — Tables Selector lists `BOW_DEMO.CUSTOMERS`,
  `BOW_DEMO.ORDERS`, `BOW_DEMO.V_REVENUE_BY_COUNTRY`.

Finding from this loop: HANA Express 2.00.088 serves TLS with a self-signed
certificate on the tenant SQL port, so the realistic local settings are
**Encrypt ON + Verify SSL OFF** (exactly the `verify_ssl` escape hatch the
config exposes); with Verify SSL on, hdbcli fails with
`-10709 … SSL certificate validation failed`, surfaced verbatim in the form.

## Known limits / follow-ups

- Datasphere **Analytic Models** (semantic layer with measures) are not
  reachable over SQL — they need the OData consumption API (a Power BI-shaped
  connector; see the SAP Datasphere research in the PR conversation).
- No Kerberos / X.509 / JWT auth variants — userpass only (matches what
  Datasphere database users and most HANA setups use).
- HANA Express can't reproduce Datasphere tenant specifics (spaces, IP
  allowlist, `SPACE#USER` naming); final validation against a real Datasphere
  tenant still recommended before calling the Datasphere story fully proven.
