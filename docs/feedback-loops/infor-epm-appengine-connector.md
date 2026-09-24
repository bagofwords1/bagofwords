# Sandbox Feedback Loop — Infor EPM (Application Engine) connector

Validates the new `infor_epm` connector end to end through the real product
path — connection form → Test Connection → save → schema indexing → an agent
question answered from cube data — without an Infor farm. Infor d/EPM cannot
run in a sandbox (licensed, Windows-farm-only), so the loop uses
`tools/agent/mock_infor_epm_server.py`: a mock ION token endpoint plus the
three `BOW_*` Application Engine processes over a small in-memory OLAP model
with mock names (`DEMO_OLAP` / `Sales`, `Finance`). It speaks the exact
request/response contract the connector targets
(`backend/app/data_sources/clients/infor_epm_client.py`); design and the
customer-side process contract are in
`docs/design/infor-epm-appengine-connector.md`.

## Loop A — full stack against the mock farm (no external services)

Fresh sandbox, from the repo root.

```bash
# 1. Mock Application Engine (token endpoint + BOW_* processes) on :8765
setsid python3 tools/agent/mock_infor_epm_server.py --port 8765 > /tmp/bow-agent/mock_epm.log 2>&1 &
curl -s localhost:8765/health        # {"ok": true}

# 2. Full stack (backend :8000 + frontend :3000)
tools/agent/boot_stack.sh --dev

# 3. Seed org + admin (admin@example.com / Password123!) and an Anthropic
#    Haiku LLM (key from the environment only — never on disk)
cd backend
uv run python ../tools/agent/seed_org.py
ANTHROPIC_API_KEY=... uv run python ../tools/agent/setup_haiku_llm.py
cd ..

# 4. Drive the real UI end to end (screenshots land in /tmp/bow-agent/epm-media)
PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers node tools/agent/e2e_infor_epm_sandbox.mjs
```

Form values used by the script: API URL `http://localhost:8765/api/rest/BowService/v1`,
OLAP database `DEMO_OLAP`, ION token URL `http://localhost:8765/token`,
client id/secret `demo-client` / `demo-secret`.

## Observed output

`tools/agent/e2e_infor_epm_sandbox.mjs` (2026-09-23, production frontend build,
Claude Haiku 4.5 as the only enabled model):

```
>> test connection
TEST CONNECTION => Connected successfully. Found 2 tables.
>> save and continue
>> wait for schema indexing to complete
CONNECTION => Infor EPM (mock farm) infor_epm 9618fc72-… is_active: true
INDEXING => completed tables: 2
>> create agent over the connection via API
ACTIVATE TABLES => 200 {"activated_count":2,"deactivated_count":0,"total_selected":2}
>> type + send the prompt
REPORT URL => http://localhost:3000/reports/a7b27bcb-…
COMPLETIONS => user:success, system:success
ANSWER MENTIONS REGIONS => true
E2E RESULT: PASS
```

The agent's turn (from `completions` / `tool_executions`): `create_data`
wrote MDX against `[Sales]` in a single attempt (an earlier run, before the
mock's WHERE grammar was relaxed — see finding 2 — needed one self-correction
after the process rejected the statement; the rejection text reached the
agent verbatim as the tool error) and rendered:

| Region | Revenue |
|---|---|
| East | 60,828 |
| North | 74,578 |
| South | 69,898 |
| West | 66,384 |

— identical to the mock cube's own aggregation for
`{[SalesMeasures].[Revenue]} ON COLUMNS, [Region].[All].Children ON ROWS … WHERE ([Period].[2024])`.
The mock log shows one `BOW_ExecuteMdx/async` call for the turn with two
`getasyncresult` polls (first poll `Running`, second `Completed`).

Screenshots: `docs/feedback-loops/assets/infor-epm/` (form, Test Connection,
agent answer).

## Verification at the lower layers

- **Mock log** (`/tmp/bow-agent/mock_epm.log`): `POST /token`, then
  `BOW_GetCubeList/async`, one `BOW_GetCubeSchema/async` per cube and
  `getasyncresult` polls during indexing; `BOW_ExecuteMdx/async` during the
  agent turn — proves the query really went through the process API.
- **Backend log** (`/tmp/bow-agent/backend.log`): `httpx` lines show the real
  `POST https://api.anthropic.com/v1/messages` calls for the Haiku turn.
- **DB** (`backend/db/agent.db`): `connections.type = 'infor_epm'`, the
  indexing run `completed` with two tables, `completions` for the report
  with a `success` assistant turn.

## Findings fixed in this loop

0. **Stale-key credentials silently hide an agent's tables.** After a backend
   restart without `BOW_ENCRYPTION_KEY`, the process-local key changes; the
   connection's stored client secret no longer decrypts, its health probe
   fails, `Connection.is_active` flips to false, and the schema builder
   drops every table of that connection from the agent's context — the
   agent then reports "no queryable tables" although indexing shows 2.
   Not a connector bug, but the loop's most confusing failure: the script now
   prints the connection's `is_active` flag, and sandboxes should export a
   fixed `BOW_ENCRYPTION_KEY` before booting.

1. **Form-blocking validation on a float config field.** `poll_interval_sec`
   was declared `float` with `ge=0.1`; `ConnectForm.vue` renders number
   fields with `min` but no `step`, so the browser anchored valid values at
   0.1, 1.1, 2.1… and rejected the default `1` — "Save and Continue" silently
   did nothing. Fixed by declaring the field an `int` (1–30 s), the
   convention every other connector config already follows. Rule for future
   configs: number fields must be integers unless the form gains `step`.
2. **Mock stricter than real MDX.** The mock's first grammar required
   `WHERE (...)`; real MDX accepts a bare `WHERE [Dim].[Member]`. The agent
   hit it, recovered, and recorded a false "rule" as an instruction — fixed
   in the mock so it never teaches the agent something the farm won't enforce.
3. **Engine-level error on the async submit was masked.** An
   `{"error": …}` body on `POST <Process>/async` surfaced as "returned no
   task id"; the unit test caught it, and `_raise_engine_error` now runs on
   the submit, poll and sync responses alike.

## Loop B — deterministic regression

```bash
cd backend
TESTING=true uv run pytest tests/unit/test_infor_epm_client.py -q
```

Covers registry/form shape, token exchange + 401 refresh, failure
classification, schema parsing (embedded prologs, `Alea:Error` nodes, ODBO
vs name-pattern measure detection, cube-order sorting), progress callbacks,
async polling, row limits, `BOW_ERROR`/engine-error propagation, empty
results, text cells, and duplicate-dimension column naming.
