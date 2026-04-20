# Plan: Power BI Report Server (PBIRS) on-prem support — `PowerBIReportServerClient`

## Mission
Add a new data source type `powerbi_report_server` to bagofwords that mirrors the existing cloud `PowerBIClient` as closely as possible, but targets on-premises Power BI Report Server via its REST API at `/Reports/api/v2.0/` with NTLM auth. Iterate against a real, live PBIRS server until all acceptance criteria pass. **Do not stop** until every item in "Definition of Done" is green.

## Live test environment
- Server: `http://20.157.199.20/Reports/api/v2.0/` (DNS: `pbi-demo.israelcentral.cloudapp.azure.com`, may or may not have SSL yet — assume HTTP for now)
- Auth: NTLM, local Windows user
  - Username: `PBI\yochze` (domain = machine name `PBI`, workgroup not AD)
  - Password: supplied via env var `PBIRS_PASSWORD` at session start (do not hardcode; rotate after session)
- PBIRS version: `1.24.9466.3830` (Microsoft Power BI Report Server)
- Uploaded demo content: `AdventureWorks Sales` (`.pbix`, Import mode, id `3306b9d4-6762-4ca2-9cae-87eda19dd9fb`), plus a folder `/hi` and a DataSource `/hello` (for catalog variety). No paginated (RDL) reports yet.

## Known findings from the real server (already validated)
1. `GET /CatalogItems` → returns mixed types (`Folder`, `PowerBIReport`, `DataSource`) with fields `Id, Name, Path, Type, ParentFolderId, Size, ModifiedBy, CreatedBy, ModifiedDate, CreatedDate, HasDataSources`. `@odata.type` prefixed with `#Model.`.
2. `GET /PowerBIReports` → one item (the AdventureWorks report). Same shape as `PowerBIReport` in CatalogItems.
3. `GET /Datasets` → **empty** for `.pbix`-embedded models. This endpoint only returns shared/standalone datasets, never embedded ones. Don't rely on it.
4. `GET /PowerBIReports({id})/DataSources` → returns `DataSource` objects with `DataModelDataSource` sub-object (Type: `Import|DirectQuery`, Kind: `File|SQL|…`, AuthType, ModelConnectionName).
5. `GET /Reports` → empty (no RDLs in demo).
6. `GET /System` → `{ProductName, ProductVersion, ReportServerAbsoluteUrl, ReportServerRelativeUrl, WebPortalRelativeUrl, TimeZone, ProductType}`.
7. `GET /PowerBIReports({id})/DataModelParameters` → empty for AdventureWorks.

Key architectural fact: **PBIRS has no REST endpoint equivalent to cloud's `/executeQueries`.** DAX against `.pbix`-embedded models requires XMLA against the internal SSAS Tabular engine (TCP/2383 area, Windows-specific client libs). This is explicitly **out of scope for v1**.

## v1 scope (what to build)
1. **Metadata/discovery** via REST (folders, Power BI reports, shared datasets, data sources, parameters)
2. **Paginated report (RDL) execution** via `GET /Reports({id})/Export/CSV` → pandas DataFrame (even though the demo has none, code path should exist so the feature is unblocked as soon as a .rdl is uploaded)
3. **DAX execution** → `NotImplementedError` with a clear message pointing to (a) upload data as paginated report or (b) enable XMLA endpoint; leave an `xmla_endpoint` extension hook in config
4. **Connection test** → authenticated `GET /System` + `GET /PowerBIReports` count

## Files to create / modify
1. **Create** `backend/app/data_sources/clients/powerbi_report_server_client.py`
   - Class `PowerBIReportServerClient(DataSourceClient)` + alias `PowerbiReportServerClient`
   - Constructor: `__init__(server_url: str, username: str, password: str, domain: Optional[str] = None, verify_ssl: bool = True, ca_bundle_path: Optional[str] = None, xmla_endpoint: Optional[str] = None)`
   - Uses `requests_ntlm.HttpNtlmAuth` — add to `backend/pyproject.toml` / requirements
   - Normalize `server_url` so both `http://host` and `http://host/Reports` work; derive API base as `{server_url}/Reports/api/v2.0` (strip trailing slash, don't double `/Reports`)
   - Methods mirroring cloud: `connect`, `test_connection`, `list_folders`, `list_powerbi_reports`, `list_shared_datasets`, `list_paginated_reports`, `get_report_datasources`, `get_report_parameters`, `get_schemas`, `get_schema`, `execute_query`, `prompt_schema`, `description`, `system_prompt`
   - `get_schemas()` returns one `Table` per PowerBIReport + one per shared Dataset. Columns are best-effort from datasource metadata; measures left empty (we can't introspect embedded models). Table naming: `"{Folder}/{ReportName}"` (so it feels like the 2-level cloud naming)
   - `execute_query(query, table_name=None, report_id=None, format="CSV", parameters=None)` — routes to paginated export if `report_id` resolves to a Report (RDL), otherwise raises `NotImplementedError` with guidance
2. **Edit** `backend/app/schemas/data_sources/configs.py` (after the existing `PowerBIConfig` block at ~L580)
   - `PowerBIReportServerCredentials(username, password, domain)` — `password` uses `ui:type: password`
   - `PowerBIReportServerConfig(server_url, verify_ssl, ca_bundle_path, xmla_endpoint)` — `server_url` required, others optional
3. **Edit** `backend/app/schemas/data_source_registry.py`
   - Import `PowerBIReportServerConfig, PowerBIReportServerCredentials`
   - Add entry `"powerbi_report_server"` with `auth="username_password"`, `client_path="app.data_sources.clients.powerbi_report_server_client.PowerBIReportServerClient"`, `requires_license="enterprise"`, title "Power BI Report Server", scopes `["system", "user"]`
4. **Edit** `backend/pyproject.toml` — add `requests-ntlm` dependency (pin compatible with current `requests`)
5. **No changes needed** in `services/data_source_service.py` — it resolves clients dynamically via the registry + `inspect.signature` filtering.

## Feedback loop protocol (how to iterate against live PBIRS)

Since the agent cannot reach the server directly from its tool sandbox, use the **fixtures harness** approach so the agent can iterate locally between human curl runs.

**Step 1: Create `scripts/pbirs_capture.sh`** (commit this to the repo in the feature branch)
```bash
#!/usr/bin/env bash
# Usage: PBIRS_USER='PBI\yochze' PBIRS_PASSWORD='xxx' PBIRS_BASE='http://20.157.199.20' \
#        scripts/pbirs_capture.sh
# Writes JSON fixtures into tests/fixtures/pbirs/ for the agent to read.
set -euo pipefail
OUT="tests/fixtures/pbirs"
mkdir -p "$OUT"
CURL="curl -sS --ntlm -u $PBIRS_USER:$PBIRS_PASSWORD"
BASE="$PBIRS_BASE/Reports/api/v2.0"

$CURL "$BASE/System"                                   > "$OUT/system.json"
$CURL "$BASE/CatalogItems"                             > "$OUT/catalog_items.json"
$CURL "$BASE/Folders"                                  > "$OUT/folders.json"
$CURL "$BASE/PowerBIReports"                           > "$OUT/powerbi_reports.json"
$CURL "$BASE/Datasets"                                 > "$OUT/datasets.json"
$CURL "$BASE/Reports"                                  > "$OUT/paginated_reports.json"
$CURL "$BASE/DataSources"                              > "$OUT/datasources.json"

# Per-report sub-resources for each PowerBIReport
python3 - <<'PY'
import json, os, subprocess
user=os.environ["PBIRS_USER"]; pw=os.environ["PBIRS_PASSWORD"]; base=os.environ["PBIRS_BASE"]
reports=json.load(open("tests/fixtures/pbirs/powerbi_reports.json"))["value"]
for r in reports:
    rid=r["Id"]
    for sub in ("DataSources","DataModelParameters"):
        out=f"tests/fixtures/pbirs/pbireport_{rid}_{sub}.json"
        subprocess.run(["curl","-sS","--ntlm","-u",f"{user}:{pw}",
                        f"{base}/Reports/api/v2.0/PowerBIReports({rid})/{sub}"],
                       stdout=open(out,"w"), check=True)
PY
echo "Fixtures written to $OUT"
```

**Step 2: Feedback loop rules (the new session MUST follow)**
1. On session start, ask user to run `scripts/pbirs_capture.sh` once. Read all files in `tests/fixtures/pbirs/`. These are the source of truth for response shapes.
2. When the agent changes the set of API calls it makes, it updates `pbirs_capture.sh` to capture the new endpoints and asks user to re-run it. Do not guess at response shapes.
3. Write **pytest unit tests** in `backend/tests/data_sources/test_powerbi_report_server_client.py` that use the fixtures with `responses` or `requests_mock` to stub HTTP. Every code path must have a fixture-backed test.
4. Write **one live integration test** guarded by env var `PBIRS_LIVE=1` in `backend/tests/integration/test_powerbi_report_server_live.py` that hits the real server. User runs it after each iteration:
   ```
   PBIRS_LIVE=1 PBIRS_USER='PBI\yochze' PBIRS_PASSWORD=... PBIRS_BASE=http://20.157.199.20 \
     pytest backend/tests/integration/test_powerbi_report_server_live.py -v
   ```
5. Loop: write code → run unit tests locally (agent can do this in Bash) → ask user to run the live test → read output → fix → repeat. Do not mark the task done until the live test passes end-to-end.

## Test matrix (required passing cases in the live test)
1. `test_connection()` returns `success=True` with sensible message mentioning PBIRS version
2. `get_schemas()` returns at least one Table with name containing `AdventureWorks Sales` and valid metadata (`report_id`, `path`, datasource list)
3. `get_schema("AdventureWorks Sales")` returns the Table
4. `execute_query("EVALUATE X", table_name="AdventureWorks Sales")` raises `NotImplementedError` mentioning XMLA or paginated alternative
5. `list_folders()` returns at least `/` and `/hi`
6. `list_shared_datasets()` returns `[]` without error
7. Invalid credentials → `test_connection()` returns `success=False` with a helpful message (not a stack trace)
8. Bad URL → clean `success=False`, no unhandled exception
9. (skipped if no RDL in server) Paginated report CSV export path — add an RDL upload step to the runbook if we want to cover this; otherwise test that the method raises a clear error when `report_id` is not a paginated report

## Definition of Done
- [ ] `requests-ntlm` added to `pyproject.toml` and lock file regenerated
- [ ] `PowerBIReportServerCredentials` and `PowerBIReportServerConfig` added to `configs.py`
- [ ] `powerbi_report_server` registered in `data_source_registry.py`
- [ ] `powerbi_report_server_client.py` implemented with all methods above
- [ ] `scripts/pbirs_capture.sh` committed
- [ ] `tests/fixtures/pbirs/*.json` committed (redact nothing — they're non-sensitive metadata)
- [ ] Unit tests covering every method, all passing under `pytest backend/tests/data_sources/test_powerbi_report_server_client.py`
- [ ] Live integration test passing with real server (all 8–9 cases in test matrix)
- [ ] `ruff`/linters clean for the new files
- [ ] Commit messages follow repo convention; all commits on branch `claude/powerbi-onprem-support-quABU`
- [ ] Branch pushed to origin
- [ ] **Only then** update todo list marking everything complete

## Security / hygiene notes for new session
- Never echo the password back to the user or into files
- Read `PBIRS_PASSWORD` from env only; never commit it
- The demo server IP and username in this plan are fine to commit (they're throwaway)
- Remind the user to destroy/rotate the Azure VM and password after the feature merges

## Git flow
- Branch: `claude/powerbi-onprem-support-quABU` (already designated)
- Commit in logical chunks: (1) deps + schemas + registry, (2) client implementation, (3) fixtures + capture script, (4) tests
- Do **not** open a PR unless explicitly asked
- Push with `git push -u origin claude/powerbi-onprem-support-quABU`

## Anti-patterns to avoid (lessons from prior session)
- Don't try to curl the server from the agent's tool sandbox — it can't reach it. Always route live HTTP through the user.
- Don't assume `/Datasets` has content — embedded models never appear there.
- Don't add XMLA / DAX execution in v1 — it's a separate feature with Windows-only dependencies.
- Don't add premature abstractions (auth strategy classes, pluggable transports). Keep it a single class using `requests` + `requests_ntlm.HttpNtlmAuth`.
- Don't silently catch exceptions in the client — surface them with context.

## Reference: existing cloud client
`backend/app/data_sources/clients/powerbi_client.py` is the blueprint for shape/interface. Mirror its public surface (`connect`, `test_connection`, `get_schemas`, `get_schema`, `execute_query`, `prompt_schema`, `description`, `system_prompt`) and its multi-phase `test_connection` pattern (auth → list → sample → summary).

## Kickoff prompt for the new session
> Read `docs/powerbi-report-server-plan.md` end-to-end. Then execute it: implement the client, write tests, iterate against the live PBIRS server via the fixtures harness and the live integration test, and do not stop until every item in "Definition of Done" is checked off. Credentials for the live server will be provided as env vars in the first user message.
