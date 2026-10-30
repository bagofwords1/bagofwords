# Feedback loop: Configuration Manager (SCCM / MECM) connector — full sandbox pass

Date: 2026-10-30 · Branch: `ai/magical-shannon-exlaig` · Evidence: `media/pr/mecm-connector/`

New connection type `mecm`: Microsoft Configuration Manager through its
**AdminService** REST API (OData v4 on the SMS Provider,
`https://<provider>/AdminService/{wmi|v1.0}/<Class>`). Read-only, Windows auth
(NTLM, or a Kerberos keytab service account), with trust for the site's
self-signed certificate. This loop proves the connector end to end in a sandbox
and lists what only a real site can confirm.

## Design (validated against Microsoft's documentation)

- **Classes are tables.** `wmi/$metadata` (+ optional `v1.0/$metadata`) builds the
  catalog in one request; a table's name is its path (`SMS_R_System`,
  `v1.0/Device`), so a query is `Class?$filter=…&$select=…`. A curated set of 19
  reporting classes is the default; *Discover all classes* indexes all ~700.
- **Join keys become foreign keys** (`ResourceID`/`MachineID` → `SMS_R_System.ResourceId`,
  `CollectionID` → `SMS_Collection.CollectionID`), and each class carries a plain
  description in `metadata_json.mecm.description`, rendered into the planner's
  schema via `_TABLE_META_KEYS` (`app/ai/context/sections/tables_schema_section.py`).
  `Table.description` alone is not persisted with the catalog.
- **Read-only**: only GET is sent. Static WMI methods and CMPivot (both POST)
  are not exposed.
- **Paging**: `@odata.nextLink` is followed, re-anchored onto the configured
  AdminService root (survives FQDN-vs-short-name aliasing and relative links,
  and can never leave the configured host).
- **TLS**: paste the SMS Provider certificate (PEM) instead of disabling
  verification; `REQUESTS_CA_BUNDLE` is honoured as the deployment CA.
- **`$metadata` is parsed with defusedxml** (DTD/entity payloads refused; Aikido SAST clean).
- **Timeouts**: 15 s connect, 30 s for the connection test, 120 s per read, and a
  per-query budget tied to the connection's query timeout.

## Loop A — deterministic (no external services)

Simulator: `tools/mecm/mock_adminservice.py` — HTTPS (self-signed), a **real NTLM
handshake** (pyspnego acceptor, connection-based like HTTP.sys; curl's own
`--ntlm` also authenticates against it), 403 for a non-admin, security-scoped
user, case rules per route, lazy properties, WMI system properties, server
paging, OData errors.

```bash
cd backend
uv sync --frozen --extra dev
BOW_DATABASE_URL=sqlite:///db/app.db TESTING=true \
  uv run pytest tests/unit/test_mecm_client.py -q          # 61 passed
```

Every test was checked to fail for the right reason by breaking the client:
ignoring `nextLink` (5 fail), dropping origin re-anchoring (3 fail), keeping WMI
system properties (2 fail), removing the `$top` fallback (1 fail), removing the
`mecm` entry from `_TABLE_META_KEYS` (2 fail).

Registry-wide contracts: `tests/unit/test_discovery_progress_contract.py`
(17 passed; needs `unixodbc` installed for the Fabric import), e2e
`test_data_source.py test_connection.py test_license.py` (35 passed).

## Loop B — full stack, real UI, stub LLM

Stack: `tools/agent/boot_stack.sh` (backend :8000, production frontend :3000) +
simulator (:8443) + `tools/agent/stub_llm_mecm.py` (:9099) registered with
`tools/agent/setup_stub_llm.py`. No funded LLM key was available, so the stub
plays planner and coder: it emits one `create_data` per question and returns
Python that calls `ds_clients[<client_key>].execute_query("<Class>?<OData>")`.
It **refuses to answer** if the connector's OData guide is missing from the
codegen prompt, so the prompt wiring is verified too.

```bash
export BOW_ENCRYPTION_KEY=$(backend/.venv/bin/python -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())")
tools/agent/boot_stack.sh
uv run --project backend python tools/mecm/mock_adminservice.py &
(cd backend && STUB_DUMP=/tmp/mecm-prompts uv run python ../tools/agent/stub_llm_mecm.py &)
(cd backend && uv run python ../tools/agent/seed_org.py && uv run python ../tools/agent/setup_stub_llm.py)
mkdir -p frontend/.agent-tmp && cp tools/agent/mecm_ui_flow.mjs frontend/.agent-tmp/ && cd frontend/.agent-tmp
MECM_CA_FILE=../../tools/mecm/.certs/adminservice.pem node mecm_ui_flow.mjs connect ../../media/pr/mecm-connector
node mecm_ui_flow.mjs chat ../../media/pr/mecm-connector "How many devices run each operating system version?" os
```

Observed:

1. **Catalog** — searching "sccm" or "configuration manager" finds the tile
   (before: "No data sources found matching 'sccm'").
2. **Connect form** — schema-generated fields, NTLM/Kerberos auth dropdown, the
   three-step setup guide (localized in all 10 locales; `he` checked RTL).
   *Connect* → "Connection saved · Discovered 19 classes".
3. **Database** — credentials encrypted, no secret in `config`, 19
   `connection_tables` rows with route/path/description in `metadata_json`; the
   saved connection still tests OK after a backend restart.
4. **Agent runs** — four prompts, one `create_data` each. The simulator log shows
   exactly the OData the generated code sent, e.g.
   `GET /wmi/SMS_UpdateComplianceStatus?$filter=Status eq 2&$select=CI_ID,MachineID`.
   Results recomputed independently from the simulator's data: **OS breakdown,
   missing-update counts per KB, 14 low-disk devices, 9 device collections — all
   match**.
5. **Planner schema** — every class renders `<mecm description="…"/>` and its
   `<fk>` join hints.
6. **Resilience** — against a black-hole server (TCP accept, never answers) the
   backend `/health` stayed at ~50 ms while the test ran; the test now gives up
   after 15 s with "did not answer in time".

API-level (real routes): `GET /available_data_sources` and
`/data_sources/mecm/fields` (6 config fields, `ntlm`+`kerberos`, 3 setup steps);
`POST /connections/test-params` → "Connected successfully. Found 19 classes." /
401 message on a bad password / TLS message without the certificate.

## Lab probe (for a real site)

`tools/mecm/lab_probe.py` runs 23 read-only checks of the connector's
assumptions and prints a reconciliation report; against the simulator: 23/23.
Steps to stand up a lab (Azure quickstart template) are in `tools/mecm/README.md`.

## Not verified (needs a real site)

- Real `$metadata` shape and size (entity set names = class names, base types,
  ~700 classes), and parse time.
- Whether the real WMI route pages with `nextLink`, and which of `$top`,
  `$skip`, `$orderby`, `contains()` it implements (the client works either way:
  it follows links when present and falls back when `$top` is rejected).
- NTLM against real HTTP.sys with Extended Protection (channel binding is sent
  by requests-ntlm on HTTPS).
- Kerberos variant (no KDC in the sandbox; same code path as SharePoint on-prem).
- Internet access through a cloud management gateway needs Microsoft Entra
  tokens — not supported in this version.
- Answers from a real LLM (no funded key in the sandbox).

## Sandbox gotchas (for the next agent)

- `tools/agent/restart_backend.sh` **requires** the DB path
  (`restart_backend.sh db/agent.db`); without it, it exits before doing anything.
- Set a stable `BOW_ENCRYPTION_KEY` before booting. Otherwise each restart makes
  a new key and every stored credential (LLM provider, connections) becomes
  undecryptable ("Failed to decrypt credentials for provider 'custom'").
- A backend that served the UI can hang in graceful shutdown while holding
  :8000; the restart script waits only 2 s. Wait for the port, or `kill -9` the
  old PID found via `/proc/*/cmdline` (never `pkill -f`, which matches your own shell).
- The sandbox exports `REQUESTS_CA_BUNDLE`; SharePoint on-prem unit tests assert
  `session.verify is True` and fail under it (167 failures, unrelated). Run them
  with `env -u REQUESTS_CA_BUNDLE`.
- The connect form has no separate "Test connection" button any more:
  **Connect** runs the check and schema discovery in the status panel.
