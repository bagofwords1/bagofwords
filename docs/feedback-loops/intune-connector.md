# Feedback loop: Microsoft Intune connector — sandbox pass (Graph stand-in + Playwright)

Date: 2026-10-30 · Branch: `ai/ecstatic-hawking-yuzyab`
Evidence: `media/pr/ecstatic-hawking-yuzyab/`

New connector type `intune`: Intune device management (managed devices,
per-policy compliance, configuration profiles, apps, discovered software,
Autopilot) as 7 read-only tables over Microsoft Graph v1.0, app-only auth.

## Why a stand-in, and what it does NOT prove

There is no Intune container or emulator — Intune is SaaS-only and its Graph
API answers only for a tenant with an active Intune license. The options for a
real tenant are a Microsoft 365 E5 developer sandbox (eligibility is now gated
on a Visual Studio subscription / partner programme) or an Intune / M365 E5
trial tenant. So, like `tools/appdynamics/mock_controller.py`, this loop runs
against `tools/intune/mock_graph.py`: the token endpoint + the 7 Intune
collections, with JSON shapes copied from the Graph v1.0 reference sample
payloads. It enforces per-app permissions (403), OData paging
(`@odata.nextLink`), `$select`, a `$filter` allowlist (400 otherwise),
sentinel "never" dates, a no-license tenant (400) and optional 429 injection.

Not proven here — needs **Loop B** on a real tenant:
- which properties Intune really accepts in `$filter` / `$orderby` (the mock's
  allowlist is an approximation; the client falls back to "filter in pandas"
  on any 400, so a wrong guess costs one retry, not a wrong answer);
- real `$top` limits and throttling thresholds on large fleets.

## Root behaviours under test (client: `backend/app/data_sources/clients/intune_client.py`)

- `TABLES` — fixed catalog; each table names the least-privileged Graph
  application permission per the v1.0 docs.
- `_probe` / `get_schemas` — a table is left out of the catalog **only** on a
  definitive 401/403; throttles are retried by the inherited
  `GraphDriveClient._request`, other errors propagate (never a silently
  shrunk catalog).
- `execute_query` — JSON spec → one Graph list call (`$select` always sent,
  `$filter`/`$orderby` passed through), paged to `limit`;
  `compliance_policy_device_statuses` fans out per policy.
- `_clean` — Intune's `0001-01-01…` / `9999-12-31…` "never" sentinels → null.
- Never exposed: `activationLockBypassCode`, Autopilot `productKey`.
- `system_prompt` — query-spec guide **plus per-table enum vocabularies**
  (catalog table descriptions are not rendered to the agent, and Intune spells
  enums differently per table: `noncompliant` vs `nonCompliant`).

## Loop A — deterministic, no external services

```bash
cd backend
export BOW_DATABASE_URL="sqlite:///db/app.db" TESTING=true
# Registry resolves the client via its explicit client_path
uv run python -c "from app.schemas.data_source_registry import resolve_client_class; print(resolve_client_class('intune'))"
# Client-to-ASGI contract tests against the Graph stand-in (in-process; no network)
HTTPS_PROXY=http://127.0.0.1:9 HTTP_PROXY=http://127.0.0.1:9 uv run pytest tests/unit/test_intune_client.py -q
# Generic data-source flows still green
uv run pytest tests/e2e/test_data_source.py tests/e2e/test_connection.py -m e2e --db=sqlite -q
```

Observed: `33 passed`; e2e `12 passed`. The dead proxy proves the unit suite
makes no real network call (an earlier fixture version did — a collected
client closed the shared in-process transport and the next one silently
fell back to real `login.microsoftonline.com`; the fixture now pins it).

Mutation check (each mutation applied, suite run, file restored):

| Mutation in `intune_client.py` | Caught by |
|---|---|
| sentinel dates not nulled | `test_never_sentinel_dates_come_back_as_null` |
| `@odata.nextLink` ignored | `test_query_pages_through_every_record[7,50]` |
| `activationLockBypassCode` added to columns | `test_secrets_never_leave_the_connector` |
| any probe error treated as "no access" | `test_non_permission_failure_surfaces_instead_of_shrinking_catalog` |

Driving the client by hand against the running mock:

```bash
cd tools/intune && uv run --project ../../backend python mock_graph.py &      # :8095
cd ../../backend
export BOW_GRAPH_BASE_URL=http://localhost:8095/v1.0 BOW_GRAPH_TOKEN_BASE_URL=http://localhost:8095
uv run python -c "
from app.data_sources.clients.intune_client import IntuneClient
for cid, sec in [('intune-reader','Reader123!'),('intune-partial','Partial123!'),('intune-none','None123!')]:
    print(cid, IntuneClient(tenant_id='t', client_id=cid, client_secret=sec).test_connection()['message'])"
```

Observed:
```
intune-reader  Connected to Intune. 7 of 7 tables readable.
intune-partial Connected to Intune. 6 of 7 tables readable. Missing permissions: DeviceManagementServiceConfig.Read.All (autopilot_devices).
intune-none    Token works but no Intune data is readable. Grant these application permissions (with admin consent): ...
```
Tenant `nolicense` → "Connected to Microsoft Graph, but this tenant has no
active Intune license."; wrong secret → the AADSTS error. With
`INTUNE_MOCK_THROTTLE_EVERY=3` the shared Graph throttle retries each 429
(`graph throttle[...] → 429 (Retry-After=1); retry 1/4`) and the catalog stays
at 7 tables.

Integration suite (generic, `tests/integrations/ds_clients.py`) with a local,
uncommitted `integrations.json` pointing at the mock:
`test_data_source_connection[intune] PASSED`.

## Loop A′ — live UI pass (real stack, scripted LLM)

```bash
export BOW_ENCRYPTION_KEY=$(cd backend && uv run python -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())")
export BOW_GRAPH_BASE_URL=http://localhost:8095/v1.0 BOW_GRAPH_TOKEN_BASE_URL=http://localhost:8095
tools/agent/boot_stack.sh --dev
cd backend && uv run python ../tools/agent/seed_org.py
uv run python ../tools/agent/stub_llm_intune.py &        # :9099
uv run python ../tools/agent/setup_stub_llm.py --stub-url http://127.0.0.1:9099/v1
mkdir -p ../frontend/.agent-tmp && cp ../tools/agent/intune_ui_flow.mjs ../frontend/.agent-tmp/ && cd ../frontend/.agent-tmp
STOP_AFTER_TEST=1 node intune_ui_flow.mjs connect /tmp/intune/none intune-none None123!
node intune_ui_flow.mjs connect /tmp/intune/connect
node intune_ui_flow.mjs chat /tmp/intune/chat "How many Intune devices are non-compliant, by operating system?" chat 120
```

Set a stable `BOW_ENCRYPTION_KEY`: without it the backend generates one per
start and a restart makes the stored connection/LLM credentials undecryptable.

Observed (fresh DB):
1. Catalog search "intune" → **Microsoft Intune** tile under Infrastructure;
   the schema-generated form shows Tenant ID / Client ID / Client Secret.
2. App with no permissions → red status listing the 4 permissions to grant;
   nothing saved.
3. `intune-reader` → "Connection saved · Discovered 7 tables"; Select Tables
   lists all 7 with FK links on `autopilot_devices` and
   `compliance_policy_device_statuses`.
4. Chat → planner `create_data` on `managed_devices` → generated code calls
   `client.execute_query({"table": "managed_devices", "filter":
   "complianceState eq 'noncompliant'", ...})` in the sandbox → the mock logs
   `GET /v1.0/deviceManagement/managedDevices?$select=...&$top=500&$filter=complianceState eq 'noncompliant'`
   → bar chart + answer **18 non-compliant: Windows 6, macOS 5, iOS 4,
   Android 3** — equal to the mock's seeded ground truth.
5. The codegen prompt contains the Intune guide and the per-table enum notes.

The chat leg used a scripted stub because the sandbox's Anthropic key had no
credit. The stub reads ids from the prompt and quotes the real tool
observation, so the planner → codegen → sandbox → Graph path is real; what it
does not exercise is a real model *choosing* the right table and filter.

## Loop B — real tenant (pending, needs credentials)

Entra app registration with application permissions
`DeviceManagementManagedDevices.Read.All`, `DeviceManagementConfiguration.Read.All`,
`DeviceManagementApps.Read.All`, `DeviceManagementServiceConfig.Read.All`
(admin consent). Unset `BOW_GRAPH_*`, put the app in `integrations.json`
(`{"intune": {"enabled": true, "tenant_id": ..., "client_id": ..., "client_secret": ...}}`)
and run `uv run pytest tests/integrations/ds_clients.py -k intune -v`, then the
UI loop above with a real LLM. Record which `$filter` properties Intune
rejects and align the mock's allowlist with them.
