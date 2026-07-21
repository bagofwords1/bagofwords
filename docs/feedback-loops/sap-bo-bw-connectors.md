# Feedback Loop — SAP BusinessObjects + SAP BW (XMLA) connectors

Two on-prem SAP connectors were added so users can index and query the SAP
semantic layer with per-user security:

- **`businessobjects`** — SAP BusinessObjects universes via the `/biprws`
  RESTful Web Service SDK.
- **`sap_bw`** — SAP BW / BW4HANA InfoProviders & BEx queries via the XMLA web
  service (`/sap/bw/xml/soap/xmla`), a thin subclass of the existing
  `XmlaClient`.

This loop validates the claim: **both connectors register, discover their
objects, execute queries, and connect end-to-end through the real
Add-Connection UI with the SAP icon — without a licensed SAP system.**

Design rationale and the on-prem auth story live in
[`docs/sap-connector-analysis.md`](../sap-connector-analysis.md) §11.

## What was built (validated facts, file:line)

- Clients: `backend/app/data_sources/clients/businessobjects_client.py`,
  `backend/app/data_sources/clients/sap_bw_xmla_client.py`.
- Config + credential schemas (the schema-generated forms):
  `backend/app/schemas/data_sources/configs.py` (`BusinessObjectsConfig`,
  `BusinessObjectsCredentials`, `BusinessObjectsTrustedCredentials`,
  `SapBwXmlaConfig`, `SapBwXmlaCredentials`).
- Registry entries: `backend/app/schemas/data_source_registry.py`
  (`businessobjects`, `sap_bw`) — both `category="bi"`, explicit `client_path`,
  auth variants with `scopes=["system","user"]`.
- Icon reuse: `frontend/components/DataSourceIcon.vue` aliases both types to the
  generic SAP logo `sap_datasphere.png` (no new asset).

## Root cause (validated) — the one bug the loop caught

Test Connection for BusinessObjects failed in the UI with
`BusinessObjectsClient.__init__() got an unexpected keyword argument 'user'`.
The credential schema field was named `user`, but the framework maps credential
field names **directly** to client-constructor kwargs, and the constructor
parameter is `username`. Fix: rename the field to `username`
(`configs.py:BusinessObjectsCredentials.username`) so it maps. This is the exact
class of bug the add-connection-type skill warns about — field name must equal
the constructor parameter. The unit tests (which construct the client directly)
did **not** catch it; only the live UI Test Connection did.

## Loop A — deterministic reproduction (no external services)

Boundary stubbed: the SAP HTTP servers. Everything else is real code.

### A1 — unit tests (mock the HTTP boundary only)

```bash
cd backend
export BOW_DATABASE_URL="sqlite:///db/app.db"
uv run pytest tests/unit/test_businessobjects_client.py \
              tests/unit/test_sap_bw_xmla_client.py -q
# => 36 passed
```

Covers logon-token lifecycle (userpass / trusted / pre-minted token; token sent
double-quoted), universe & cube discovery into role=dimension/measure columns,
query dispatch + result normalization, and `test_connection` classification.

### A2 — full UI e2e against mock SAP servers

The mock servers implement just enough of each protocol to drive discovery and
queries: `tools/agent/mock_sap_servers.py` (BusinessObjects `/biprws` on
`:6405`, BW XMLA SOAP on `:8410`).

```bash
# stack + seed (see .agents/skills/sandbox-feedback-loop)
tools/agent/boot_stack.sh
cd backend && export BOW_DATABASE_URL="sqlite:///db/agent.db" \
  && uv run python ../tools/agent/seed_org.py && cd ..

# mock SAP servers
cd backend && uv run python ../tools/agent/mock_sap_servers.py &   # :6405 + :8410
cd ..

# Node 22 ESM needs node_modules reachable from the script dir:
ln -sfn frontend/node_modules node_modules          # gitignored; dev-only
cd frontend && PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers \
  node ../tools/agent/sap_connectors_e2e.mjs
```

**Observed FAIL (before the fix):**
`TEST SAP BusinessObjects: BusinessObjectsClient.__init__() got an unexpected
keyword argument 'user'` → `E2E FAIL`.

**Observed PASS (after the fix):**

```
TILE SAP BusinessObjects: visible=true icon=/data_sources_icons/sap_datasphere.png
TILE SAP BW (XMLA):       visible=true icon=/data_sources_icons/sap_datasphere.png
TEST SAP BusinessObjects: Connected successfully. Found 2 tables.
TEST SAP BW (XMLA):       Connected successfully. Found 1 table.
E2E PASS
```

Screenshots: `media/sap-connectors/` — `01-catalog-sap-search.png` (both tiles
with the SAP icon under BI & analytics), `bo-form-filled.png`,
`bo-test-result.png`, `bw-test-result.png`.

## Loop B — live confirmation (real SAP, pending)

There is **no free/redistributable BusinessObjects or BW server**, so the true
end-to-end (real logon plugins, trusted auth, and — critically — that universe
security / BW analysis authorizations actually restrict rows per user) must run
against a real system before production. Options (secrets via env only, never
committed):

- BusinessObjects: a licensed BI 4.x tenant (trusted-auth shared secret in CMC).
- BW: an **SAP CAL BW/4HANA** appliance, or point `sap_bw` at an open-source
  **XMLA provider (Mondrian / icCube)** to exercise the XMLA transport with a
  live server.
- Configure via `backend/tests/integrations/integrations.json` and run
  `uv run pytest tests/integrations/ds_clients.py -k "businessobjects or sap_bw"`
  (these skip cleanly without credentials).

The per-user row-level-security check is the one assertion Loop A cannot make —
it is mandatory for go-live (see analysis §11.3).

## What this proves / regression notes

- Both connectors are registry-resolvable, render schema-generated forms, and
  perform discovery + query end-to-end through the real app against a faithful
  mock of each protocol.
- The `user`→`username` fix is locked by the live UI loop; the 36 unit tests are
  the standing regression for client behavior.
- Not proven here (needs Loop B): real SAP auth plugins/trusted auth and
  server-side per-user row-level security enforcement.
