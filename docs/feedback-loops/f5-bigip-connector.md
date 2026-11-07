# Feedback loop: F5 BIG-IP connector — simulator, tests and full sandbox pass

Date: 2026-11-07 · Branch: `ai/loving-brahmagupta-d7snv9`
Evidence: `media/pr/f5-bigip-connector/`

## What was built

- `backend/app/data_sources/clients/f5_bigip_client.py` — `F5BigIpClient`
  (iControl REST, TMOS 15.x–17.x). Token login per operation
  (`POST /mgmt/shared/authn/login` → `X-F5-Auth-Token`), one re-login on 401,
  guaranteed `DELETE /mgmt/shared/authz/tokens/<token>`. `$top`/`$skip` paging
  with continuation checks, `$select` projection, `partition eq` server-side
  filter, `/stats` `entries → nestedStats.entries` flattening.
- `backend/app/data_sources/clients/f5_bigip/catalog.json` — frozen allow-list
  of 26 tables / 511 fields. Config tables (`virtual_servers`, `pools`,
  `pool_members`, `nodes`, `irules`, `profiles_*`, `ssl_certs`,
  `ltm_monitors_http`, `cm_devices`, `net_*`, `partitions`), live status
  tables (`*_stats`), system tables (`sys_version`, `sys_cpu`, `sys_memory`,
  `cm_failover_status`, `cm_sync_status`, …).
- `F5BigIpConfig` / `F5BigIpCredentials` in
  `backend/app/schemas/data_sources/configs.py`; `REGISTRY["f5_bigip"]`
  (`category="infra"`, `beta`, explicit `client_path`); icon
  `frontend/public/data_sources_icons/f5_bigip.png`.
- `tools/f5_bigip/simulated_api.py` — stdlib loopback simulator with the
  documented wire shapes (token object, paging envelope with
  `nextLink`/`totalItems`/`pageIndex`, 401/404 error bodies, 403 on every
  mutating verb under `/mgmt/tm`, `--token-timeout` to exercise expiry).
  `tools/f5_bigip/SOURCES.md` lists the 18 references (iControl REST User
  Guide, clouddocs API refs, f5-ansible fixtures/module_utils, f5-common-python,
  DevCentral articles) and grades each shape verified / partial / inferred.
- Tests: `backend/tests/unit/test_f5_bigip_client.py` (91 tests; two run the
  real client against the in-process simulator over HTTP),
  `backend/tests/e2e/test_f5_bigip.py`, and an `f5_bigip` remote-mode entry in
  `backend/tests/integrations/ds_clients.py`.

## Loop A — deterministic (no LLM, no appliance)

```bash
cd backend && uv sync --frozen --extra dev
export TESTING=true BOW_DATABASE_URL=sqlite:///db/f5-tests.db
uv run python -c "from app.schemas.data_source_registry import resolve_client_class; print(resolve_client_class('f5_bigip'))"
uv run pytest tests/unit/test_f5_bigip_client.py -q --db=sqlite
uv run pytest tests/e2e/test_f5_bigip.py tests/e2e/test_data_source.py tests/e2e/test_connection.py --db=sqlite -q
```

Observed: registry resolves `F5BigIpClient`; **91 unit tests passed**; the
connector e2e plus 13 generic connection/data-source tests passed. Only the
`requests.Session.send` boundary is replaced; validation, catalog, paging,
flattening and dtype handling run for real. Covered contracts: token header
and logout, re-login once on 401, `$skip` must advance and continuations may
not change host/path/scope, `limit` never truncates, 401/403/404 → stable codes
without echoing bodies, invalid queries make zero requests, anything outside
the GET allow-list raises `ForbiddenOperation` before I/O, monitor `password`
is stripped, uint64 counters preserved.

Mutation checks (rule 6 of `backend/tests/AGENTS.md`): removing the logout,
the re-login path and the allow-list each turned the matching test red.

## Loop B — real HTTP against the simulator

```bash
export F5_USERNAME=reader F5_PASSWORD=sim-pass-1
python3 tools/f5_bigip/simulated_api.py --port 18094
# other terminal
cd backend && F5_TEST_URL=http://127.0.0.1:18094 F5_TEST_USERNAME=reader F5_TEST_PASSWORD=sim-pass-1 \
  F5_TEST_ALLOW_HTTP=true uv run pytest tests/integrations/ds_clients.py -k f5_bigip -q --db=sqlite
```

Observed: `test_connection` → "Connected to F5 BIG-IP 17.1.1 bigip-a.example.com
(active). 26 read-only tables available."; integration harness **passed**;
every table returns rows (see `tools/f5_bigip/README.md` for one query per
table). curl against the simulator reproduces the documented handshake: 401
`Authorization failed…` without a token, 401 `Authentication failed.` on bad
credentials, `PATCH /mgmt/shared/authz/tokens/<t>` extends, 404
`01020036:3: The requested Pool (/Common/nope) was not found.`, 403 on
`POST/PUT/PATCH/DELETE` under `/mgmt/tm`.

## Loop C — sandbox UI with Claude Haiku 5.5

Stack: backend `:8000` (no reloader), Nuxt dev `:3000`, sqlite, Haiku 5.5
configured through `tools/agent/setup_haiku_llm.py` (`HAIKU_MODEL_ID=claude-haiku-5-5`).
Driver: a Playwright flow (sign-in → Agents → New → catalog search "f5" → tile →
form → **Connect** → tables → context → agent), then **New report** from the
agent page with the prompt below.

| Step | Observed |
|---|---|
| Catalog tile | F5 BIG-IP with the F5 logo (`01-catalog-tile.png`) |
| Schema-generated form | URL, login provider, CA, timeout, max rows, HTTP toggle, username/password (`02-connect-form.png`) |
| Connect | "Connection saved · Schema discovery · Discovered 26 tables · 0s" (`03-connection-tested.png`) |
| Tables | 26/26 selected (`04-tables-selected.png`) |
| Agent | overview generated by Haiku; 26 tables, suggested prompts (`05-agent.png`) |

Prompt: *"Users report intermittent 502 errors on the web application behind
the F5 pair in the last hour. Investigate: which pool members are down or
disabled and why, whether the HA pair is healthy, and whether any SSL
certificates are expired or expiring within 30 days."*

Haiku 5.5 generated `execute_query` calls against `pool_members`,
`pool_member_stats`, `pools`, `cm_failover_status`, `cm_sync_status` and
`ssl_certs` (code captured in `steps.code` of the sandbox DB), spawned three
sub-agents, and reported the planted root cause: **web03 marked down by its
monitor in both `/Common/web_pool` and `/Common/ssl_pool`**, HA pair ACTIVE /
In Sync (ruled out), `/Common/legacy-api.crt` expired 30 days ago, app1-web02
user-disabled, db02 forced-down (`06-rca-report.png`, `07-rca-report-full.png`).

### Connector defects found by the live run (fixed in this branch)

1. The planner copies `_bow_retrieved_at` from `get_schema` into `fields` →
   `InvalidQuery: Unknown or excluded field`. Real provenance columns are now
   accepted in `fields` (other `_bow_*` names are still rejected before I/O).
2. The planner asked `virtual_server_stats` for `serverside.totConns`, a real
   TMOS counter the catalog did not list → rejected. `/stats` and nested
   tables now accept undocumented device keys as fields/filters (null column
   when absent); config tables stay a frozen contract.
3. "Peek 3 rows" set `limit: 3` → `ResultLimitExceeded` with no guidance. The
   message now explains that `limit` is a complete-result ceiling and to
   preview with pandas `.head(n)`.

Each has a regression test in `test_f5_bigip_client.py`. A follow-up prompt
after the fix ("show availability state, status reason and client-side
connection counters from `virtual_server_stats` for the listeners on
web_pool/ssl_pool, and whether legacy-api.crt is bound to a client-ssl
profile — include `_bow_retrieved_at`") completed in one turn: both listeners
available with their counters and snapshot time, and `/Common/legacy-api.crt`
bound to `/Common/legacy_clientssl` (`08-followup-report.png`,
`09-followup-report-full.png`). It also showed the planner writing
`{"op":"in"}` filters as on the sibling connectors, which F5 now accepts. The
remaining failures in the transcript (`SyntaxError: unterminated string literal`,
`invalid character '≤'`) are the platform's code generator, not the connector.

## Faithfulness limits (see `tools/f5_bigip/SOURCES.md`)

`sys_cpu`, `sys_memory` and parts of `sys_hardware` are **inferred** from
`tmsh show` output; the client flattens whatever keys the device returns.
Whether `$filter`/`$top` apply to `/stats` collections is undocumented, so the
client never sends them there. No real BIG-IP was used: a read-only pass on a
customer or trial VE is still required before GA.
