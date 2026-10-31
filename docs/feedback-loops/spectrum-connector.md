# Feedback loop: DX NetOps Spectrum connector — full sandbox pass (mock OneClick + Playwright)

Date: 2026-10-30 · Branch: `ai/clever-euler-vqvlk0`
Research/design doc: `docs/research/broadcom-spectrum-connector.md` (§5 is the build plan, §7 why there is no public instance)
Evidence: `media/pr/spectrum-connector/`

## What was built

- `backend/app/data_sources/clients/spectrum_client.py` — OneClick Web
  Services client (`/spectrum/restful`): Basic (local user) or Bearer (24.3.10+
  token) auth, `Accept: application/xml` + XML request bodies on every call,
  `rs:model-request` / `rs:alarm-request` / `rs:get-event-request` builders
  (Filter.xsd `and/or/equals/has-substring`), `next`-link paging (GET for
  stored model result sets, re-POST for GET-tunnelled event searches),
  600 s / 500-row defaults per Broadcom's guidance, enum labels from
  `/attribute/<id>/enums`, epoch-second dates → ISO. 12 fixed virtual tables
  (landscapes, devices, models, interfaces, model_attributes, alarms,
  alarm_counts, alarm_filters, events, associations, connectivity,
  attribute_enums); JSON query spec documented in `system_prompt()`.
  Read-only: no PUT/DELETE/POST-create is ever issued.
- `SpectrumConfig` / `SpectrumUserPassCredentials` / `SpectrumTokenCredentials`
  in `backend/app/schemas/data_sources/configs.py`; `REGISTRY["spectrum"]`
  (`category="infra"`, `version="beta"`, `requires_license="enterprise"`,
  two auth variants with `system` + `user` scopes, explicit `client_path`).
- Icon `frontend/public/data_sources_icons/spectrum.png` (the Broadcom mark
  the user supplied, converted from webp; resolved by the default `<type>.png`
  rule in `DataSourceIcon.vue`).
- `tools/spectrum/mock_oneclick.py` + `docker-compose.yaml` — doc-shaped mock
  OneClick (TechDocs XML bodies per noun; Basic + Bearer; 403 on writes for
  the read-only user; 415 on JSON bodies; 1000 default throttle with
  `next` links; result sets expire after 10 min), seeded two-landscape
  estate (13 devices, 36 interfaces, connectivity graph) and ONE deterministic
  incident (`rtr-core-01_Gi0/1` LINK DOWN after 15 min of saturation →
  fw-edge-01 lost → sw-acc-11/12 impact symptoms). Its five undocumented
  assumptions are listed in the module docstring for the first live pass.
- `backend/tests/unit/test_spectrum_client.py` (38 tests, `requests` mocked),
  `backend/tests/unit/test_spectrum_mock_contract.py` (client ↔ mock over
  ASGI, incident reconstructable), `spectrum` entry in
  `backend/tests/integrations/ds_clients.py` (remote mode).
- Drivers: `tools/agent/spectrum_sandbox_setup.py` (API: create agent, test,
  index, activate tables), `tools/agent/spectrum_ui_flow.mjs` (Playwright:
  connect wizard + chat), `tools/agent/stub_llm_spectrum.py` (scripted
  planner/coder so the real tool pipeline runs without an LLM key).

## Loop A — deterministic (no LLM, no OneClick)

```bash
cd backend && uv sync --frozen --extra dev
export BOW_DATABASE_URL="sqlite:///db/app.db" TESTING=true
# 1. registry resolves through the explicit client_path (+ enterprise gate)
uv run python -c "from app.schemas.data_source_registry import resolve_client_class; print(resolve_client_class('spectrum'))"
# 2. unit + contract tests
uv run pytest tests/unit/test_spectrum_client.py tests/unit/test_spectrum_mock_contract.py -q   # 42 passed
# 3. generic data-source e2e suites still green
uv run pytest tests/e2e/test_data_source.py tests/e2e/test_connection.py --db=sqlite -q          # 12 passed
# 4. the mock + the real client over HTTP
(cd ../tools/spectrum && uv run --project ../../backend uvicorn mock_oneclick:app --port 8880 &)
SPECTRUM_TEST_URL=http://127.0.0.1:8880 SPECTRUM_TEST_USERNAME=bow_api SPECTRUM_TEST_PASSWORD='Spectrum!2024' \
  uv run pytest tests/integrations/ds_clients.py -k spectrum -v                                   # 1 passed
```

Observed (client → mock): `test_connection` → "Connected to DX NetOps Spectrum
24.3.10: 2 landscape(s) [ss-dc1.corp.local (0x100000), ss-dc2.corp.local
(0x200000)], 13 device models."; `get_schemas` → 12 tables with enriched
descriptions (landscape names, device count, live alarm counts); `devices`
pages transparently through the stored result set at throttlesize 3;
`alarms min_severity 2 primary_only` → 3 rows, the LINK DOWN alarm carrying
`symptom_alarm_ids 0x1000004,0x1000005,0x1000006`; `alarms alarm_id [...]` →
the three impact alarms with `cause_alarm_ids 0x1000003`; `events` on
rtr-core-01 (+interfaces) → utilisation 92→94 % then LINK DOWN then CPU;
`connectivity rtr-core-01` → four neighbours, Gi0/1 condition 6 towards
fw-edge-01; bad password / forged token → `success: false` with the
"local user database / Token Configuration" hint.

Mutation check (tests can fail): breaking the severity filter attribute and
the `EndOfResults` stop made 7 of 42 tests fail for the right reasons; the
restore brought all 42 back.

## Loop B — the running product (stub LLM, real tool pipeline)

No LLM key is available in this sandbox, so the chat leg uses
`tools/agent/stub_llm_spectrum.py`: a scripted OpenAI-compatible planner +
coder. Everything else is the real product — agent runtime, `create_data`,
the code sandbox, `SpectrumClient`, the mock. The stub is the LLM boundary
only; it never fabricates rows.

Stack: `tools/agent/boot_stack.sh` (backend :8000 sqlite, frontend :3000 prod
build; **set a stable `BOW_ENCRYPTION_KEY` before booting** — see gotchas),
mock on :8880, `seed_org.py`, `setup_stub_llm.py`, enterprise license from
`BOW_LICENSE_KEY`, then `spectrum_sandbox_setup.py` (API) and
`spectrum_ui_flow.mjs connect` (UI) each created an agent.

1. **Connect wizard (schema-generated form)** — `/agents/new` → catalog
   modal → search "spectrum" / *Infrastructure* chip → **DX NetOps Spectrum**
   tile with the Broadcom mark (`01`) → form with Connection Name, OneClick
   URL, Landscapes, Verify SSL, CA Bundle Path, Request Timeout (600), Page
   Size (500), auth selector *Username + Password* / *Bearer Token* (`02`,
   `03`) → **Connect** → status panel "Connection saved ✓ · Schema discovery ✓
   · Discovered 12 tables · 0s" (`05`) → **Done** → agent name → **Select
   Tables** lists the 12 tables with their FK link counts (`06`, `07`) →
   *Select all* → *Save & Continue* → "Tables updated" → Set Context (`09`).
2. **API path** — `POST /data_sources` (type spectrum, inline connection)
   → `GET …/test_connection` 200 "Connected to DX NetOps Spectrum 24.3.10 …
   13 device models." → indexing `running` → `completed` → 12 tables active.
3. **RCA prompt** ("What critical alarms are open in Spectrum right now, what
   caused them and what is impacted?") — the run executed three `create_data`
   steps through the sandbox, each a real `ds_clients["Spectrum (prod):spectrum-3"].execute_query(...)`:
   `alarms` → 6 rows × 29 cols; `events` → 6 rows × 12 cols; `connectivity`
   → 4 rows × 14 cols; then the written answer. Mock log for the run:
   `POST /alarms`, `GET /attribute/0x11f56/enums`, `POST /models`,
   `POST /events/getEvents`, `GET /connectivity/10.1.0.1`. Screenshots
   `12-rca-final.png`, `13-rca-final-full.png`, `14-rca-steps-expanded.png`
   (the connectivity table open: rtr-core-01's four ports and neighbours),
   `15-rca-steps-expanded-full.png`.
4. **Lower layers** — every call carried `Authorization: Basic` and
   `Accept: application/xml`; the mock's read-only user never saw a PUT/DELETE;
   `completions` status `success`, each tool execution `success` with the row
   counts above.

## What this proves / notes for the next agent

- The registry-driven form, both auth variants, test-connection, background
  indexing, table activation and the agent's `create_data` → sandbox → client
  path all work for Spectrum against the documented API shapes.
- **Enterprise-gated** (`requires_license`): the sandbox needs
  `BOW_LICENSE_KEY`; without it the tile is locked and creation is refused.
- Gotchas hit in this sandbox, none connector-specific:
  - `boot_stack.sh` without `BOW_ENCRYPTION_KEY` lets each backend worker
    mint its own key, so credentials saved by one worker fail to decrypt in
    another (`InvalidToken`, "Agent failed"). Export a Fernet key before
    booting; `tools/agent/restart_backend.sh` needs the same env.
  - An agent created via the API is private (`is_public=false`); the home
    composer's Auto scope only resolves public agents or ones the user is a
    member of, so `spectrum_sandbox_setup.py` now creates it public.
  - Connections left behind by deleted agents are still swept by the
    status refresher and show as `not_connected`; harmless, but confusing in
    `connections` when reading the DB by hand.
  - `pkill -f <pattern>` kills the calling shell; free ports with
    `fuser -k <port>/tcp` instead.
- Not verified here: a real OneClick (none available — Broadcom entitlement).
  The mock's five ASSUMPTIONS (JSON rendering, `next`-link spelling incl.
  re-POST for getEvents, `Bearer` header, severity enum ints, relation
  handles) are the first things to confirm at the customer, in this order:
  connection test → schema index → `devices` → `alarms min_severity 2` →
  `events` on one device for the last hour → `connectivity` for one IP.
  Because the client parses XML (the only documented format), a different
  JSON shape cannot break it; a different `next` spelling would surface as a
  truncated page (`total-*` > rows returned) rather than wrong data.
