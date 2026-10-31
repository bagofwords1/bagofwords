# Feedback loop: CrowdStrike Falcon connector — full sandbox pass

Date: 2026-10-30 · Branch: `ai/bold-mendel-g1ar0n` · Evidence: `media/pr/crowdstrike-connector/`

## What there is to test against

CrowdStrike has **no emulator, container or public sandbox** for the Falcon
platform API. Options, in order of fidelity:

1. **A real tenant** — a Falcon trial (15 days, business email, sales-gated)
   or a customer tenant. Create an API client under *Support and resources →
   API clients and keys* with read scopes (Hosts, Host groups, Alerts,
   Incidents, Vulnerabilities, Prevention policies, IOC Management).
2. **`tools/crowdstrike/mock_falcon.py`** (this loop) — routes, parameter
   names and `limit` maxima taken from CrowdStrike's official SDK
   (FalconPy 1.6.6 endpoint definitions), with a seeded "Northwind Health"
   tenant (48 hosts, 87 alerts with a credential-theft chain, 7 incidents,
   CrowdScore, 85 Spotlight vulns, 4 policies, 10 IOCs). It enforces the
   behaviors the client must get right: bearer auth + token expiry,
   `X-Cs-Region` autodiscovery header, per-scope 403s (`bow_limited`),
   per-endpoint `limit` caps (400), each endpoint's sort separator (`|` vs
   `.`), Spotlight's required filter, and a real FQL evaluator.
   Knobs: `FALCON_MOCK_TOKEN_TTL`, `FALCON_MOCK_NO_COMBINED_DEVICES=1`
   (legacy hosts fallback), `FALCON_MOCK_RATE_LIMIT_EVERY=N` (429s).
3. FalconPy itself has no offline mode, so it is a *reference* (routes,
   payload keys), not a test target.

**Not verified against a real tenant.** Response *field* shapes in the mock
come from CrowdStrike docs/SDK knowledge, not captured payloads; the
`/devices/combined/devices/v1` pagination key (`next` vs `offset`) is
handled both ways for that reason. First real-tenant run should capture
payloads into fixtures the way the AppDynamics loop did.

## Verified, layer by layer

| Layer | Check | Result |
|---|---|---|
| Registry | `resolve_client_class('crowdstrike')` | `CrowdStrikeClient` |
| Unit | `tests/unit/test_crowdstrike_client.py` (fake Falcon at the `requests` boundary) | 44 passed; 6 mutations of the client each caught |
| Client ↔ mock | every table, FQL, sort, projections, escape hatch, bad specs | all expected row counts |
| Edge paths | token refresh (expiry + server 401), 429 back-off, hosts 404 → scroll fallback | all pass |
| Integration harness | `tests/integrations/ds_clients.py -k crowdstrike` vs mock | 1 passed |
| Generic e2e | `test_data_source.py`, `test_connection.py`, `test_license.py` | 35 passed |
| UI | catalog tile + icon → schema-generated form → Connect → "Discovered 8 tables" → agent with 8 tables (FK edges alerts/vulns → hosts) → Manage connection → swap credentials → re-test | all pass |
| HTTP | `POST /api/connections/{id}/test` with the limited client | message lists the two missing scopes |
| Agent runtime | prompt → planner `create_data` ×2 → generated code → `execute_query` → chart + table + answer | numbers identical to a raw-HTTP ground truth |

Falcon calls made by the chat turn (mock log) — FQL pushed down, sort
normalized to the incidents endpoint's `.` syntax, page size capped at 500:

```
POST /oauth2/token
GET  /alerts/queries/alerts/v2?filter=status:['new','in_progress','reopened']+created_timestamp:>='…'&limit=5000
POST /alerts/entities/alerts/v2
GET  /incidents/queries/incidents/v1?filter=state:'open'&sort=fine_score.desc&limit=500
POST /incidents/entities/incidents/GET/v1
```

## LLM caveat

The sandbox Anthropic key had no credit, so the chat turn was driven by
`tools/crowdstrike/stub_llm_crowdstrike.py` — a deterministic
OpenAI-compatible stub (same pattern as `tools/agent/stub_llm_documentum.py`).
Only the model's *choices* are scripted; the planner, code sandbox, client,
Falcon calls and UI are real, and the stub's codegen refuses to query unless
the connector's own prompt (`## CrowdStrike Falcon Integration`) reached the
model. **A real-LLM run (does a model write good FQL from the prompt?) is
still owed.**

## Findings along the way

- Bug (fixed): with region `auto`, the first API call used the pre-discovery
  URL — the token fetch switched `base_url` after the URL was built. Caught by
  `test_auto_region_follows_token_region_header`.
- Bug (fixed): bad credentials were reported as "no readable scopes"; token
  failures now raise `CrowdStrikeAuthError` and skip the scope probe.
- Platform (not changed here): the indexer persists only columns, not the
  client's table descriptions, so value semantics (status codes, severity
  scales) are restated in the client's `system_prompt`.
- Platform (not changed here): the Manage-connection dialog shows only
  pass/fail for a successful test, so the "missing scope" message is visible
  via the API but not in the UI.

## Gotchas

- `uvicorn --reload` hangs after editing a client file while the app is
  serving; kill by PID (`fuser -k 8000/tcp` + the backend venv python
  processes). That sweep also kills the mock if it runs from the same venv —
  restart it.
- New report from an agent page: `getByTestId('agent-new-report')` (the
  sidebar has another "New report").
- Agent code reaches the client as `ds_clients['<Agent>:<Connection>']`
  (the `client_key:` line in the codegen prompt).

## Re-run

```bash
cd tools/crowdstrike && ../../backend/.venv/bin/python -m uvicorn mock_falcon:app --port 8095 &
cd backend && TESTING=true BOW_DATABASE_URL='sqlite:///db/app.db' uv run pytest tests/unit/test_crowdstrike_client.py -q
# integration: tests/integrations/integrations.json =
#   {"crowdstrike": {"enabled": true, "base_url": "http://localhost:8095",
#                    "client_id": "bow_api", "client_secret": "FalconSecret123!"}}
uv run pytest tests/integrations/ds_clients.py -k crowdstrike -v
# UI: connect with API Base URL http://localhost:8095, client bow_api / FalconSecret123!
#     (bow_limited / Limited123! to see missing-scope reporting)
# no-credit LLM: uv run python ../tools/crowdstrike/stub_llm_crowdstrike.py &
#     uv run python ../tools/agent/setup_stub_llm.py --email <admin> --stub-url http://127.0.0.1:9098/v1
```
