# Feedback loop: Check Point Management connector — simulator, tests and full sandbox pass

Date: 2026-11-07 · Branch: `ai/loving-brahmagupta-d7snv9`
Evidence: `media/pr/checkpoint-connector/`

## What was built

- `backend/app/data_sources/clients/checkpoint_client.py` — `CheckPointClient`
  (Management Web API; catalog reference v1.9.1, R81.10/R81.20/R82). Every
  query opens a session with `login {"api-key"|"user","password", "read-only": true}`
  (never `continue-last-session`), sends `X-chkp-sid`, runs allow-listed
  `show-*` commands and always `logout`s. A session whose login or
  `show-session` reports `read-only: false` is refused (`ReadOnlyRequired`).
  Paging `limit 500 / offset = to` with strict `from/to/total` checks;
  rulebases fetched with `use-object-dictionary`, sections flattened and uid
  references resolved to names; `show-logs` as `new-query` + `query-id`
  paging; `changes` via inline tasks or `show-task` polling.
- `backend/app/data_sources/clients/checkpoint/catalog.json` — 26 tables over
  29 allowed commands (`show-*` plus `login/logout/keepalive/show-session/show-task`):
  `hosts`, `networks`, `address_ranges`, `groups`, `group_members`,
  `services_tcp/udp`, `service_groups`, `packages`, `access_layers`,
  `access_rules*`, `nat_rules*`, `threat_rules*`, `gateways_and_servers`,
  `simple_gateways`, `sessions`, `unused_objects`, `tasks`, `logs`,
  `audit_logs`, `api_versions`, `session`, `last_published_session`,
  `domains`, `vpn_communities`, `changes`. The catalog loader raises if the
  allow-list ever contains a non-`show-*` command, so a mutation cannot be
  added by accident.
- `CheckPointConfig` (+ `domain`, `api_version`) with two credential variants
  (`api_key`, `userpass`) in `configs.py`; `REGISTRY["checkpoint"]`
  (`infra`, `beta`, explicit `client_path`); icon `checkpoint.png`.
- `tools/checkpoint/simulated_api.py` — loopback simulator answering
  `/web_api/<cmd>` and `/web_api/v1.9.1/<cmd>` with the official reply shapes
  (taken from the API reference's own data files,
  `sc1.checkpoint.com/documents/latest/APIs/data/v1.9.1/dynamic/*.json`),
  the documented error codes (`err_login_failed`,
  `generic_err_wrong_session_id`, `generic_err_missing_session_id`,
  `generic_err_command_not_found`), 403 `generic_err_no_permissions` for any
  mutating command, and `show-logs` query-id paging. Estate: 16 hosts,
  8 networks, 5 groups, 10 services, packages Standard/DMZ, layer
  "Standard Network" with 3 sections and 20 rules (rule 12 Any-Any-Accept
  shadowing, disabled 4/14/19, Cleanup 20), NAT and threat layers, 3 gateways
  (one SIC-down with no policy, one 97-day-old policy), sessions with a stale
  lock, ~200 traffic + 50 audit logs over 48 h. The planted incident:
  **app-03 (10.1.20.23) is missing from `grp-app-servers`, so its PostgreSQL
  traffic falls through to the Cleanup rule**. `tools/checkpoint/SOURCES.md`
  lists every reference and the shapes that remain inferred.
- Tests: `backend/tests/unit/test_checkpoint_client.py` (89 tests incl. one
  real-HTTP run against the in-process simulator and a raw-`requests`
  simulator contract test), `backend/tests/e2e/test_checkpoint.py`,
  `checkpoint` remote-mode entry in `tests/integrations/ds_clients.py`.

## Loop A — deterministic

```bash
cd backend && export TESTING=true BOW_DATABASE_URL=sqlite:///db/cp-tests.db
uv run python -c "from app.schemas.data_source_registry import resolve_client_class; print(resolve_client_class('checkpoint'))"
uv run pytest tests/unit/test_checkpoint_client.py -q --db=sqlite
uv run pytest tests/e2e/test_checkpoint.py tests/e2e/test_data_source.py tests/e2e/test_connection.py --db=sqlite -q
```

Observed: **89 unit tests passed**; connector e2e + 13 generic tests passed.
Covered: login payload and `read-only: true`, `X-chkp-sid`, guaranteed logout
(also on failure), refusal of a non-read-only session, paging envelope
invariants (no progress, cursor regression, missing total → `InvalidResponse`,
never a truncated result), rulebase flattening and name resolution, log
query-id continuity, error-code mapping without body echo, retries on
429/502/503/504, invalid queries make zero requests, `publish`/`set-*`/
`add-*`/`install-policy` impossible (`ForbiddenOperation`), secret-like keys
stripped (`shared-secret`, `api-key`, …).

## Loop B — real HTTP against the simulator

```bash
export CHECKPOINT_USERNAME=reader CHECKPOINT_PASSWORD=synthetic-only CHECKPOINT_API_KEY=synthetic-key
python3 tools/checkpoint/simulated_api.py --port 18092
cd backend && CHECKPOINT_TEST_URL=http://127.0.0.1:18092 CHECKPOINT_TEST_API_KEY=synthetic-key \
  CHECKPOINT_TEST_ALLOW_HTTP=true uv run pytest tests/integrations/ds_clients.py -k checkpoint -q --db=sqlite
```

Observed: `test_connection` → "Connected to Check Point Management API 1.9.1
as api-key-admin with a read-only session; 26 tables available."; integration
harness **passed**. curl reproduces the documented handshake and refusals
(bad credentials 400 `err_login_failed`, missing sid 401
`generic_err_missing_session_id`, `publish` → 403, unknown command → 404
`generic_err_command_not_found`, `logout` → `{"message":"OK"}`).

## Loop C — sandbox UI with Claude Haiku 5.5

| Step | Observed |
|---|---|
| Catalog tile | Check Point Management with the Check Point logo (`01-catalog-tile.png`) |
| Form | API-key variant selected by default; URL, domain, API version, CA, timeout, max rows, HTTP toggle (`02-connect-form.png`) |
| Connect | "Connection saved · Schema discovery · Discovered 26 tables" (`03-connection-tested.png`) |
| Tables | 26/26 selected (`04-tables-selected.png`) |
| Agent | generated overview and suggested prompts (`05-agent.png`) |

Prompt: *"Since this morning app-03 (10.1.20.23) cannot reach the PostgreSQL
database and connections are being dropped at the firewall. Investigate on the
Check Point management server: which access rule is dropping this traffic,
whether app-03 is missing from the group the allow rule uses, and whether any
gateway has a stale or failed policy install."*

Haiku 5.5 queried `logs` (`time_frame: today`, `src 10.1.20.23`,
`action in [Drop, Reject]`) and found **29 drops of 10.1.20.23 → 10.1.30.31:5432
all matched by "Cleanup rule" (rule 20) in layer Standard Network** — i.e. no
earlier enabled rule matched. Sub-agents established from
`gateways_and_servers` and `tasks` that gw-hq-fw01 has Standard installed
after the last publish (ruled out), gw-dmz-fw03 is not communicating with a
failed install task, and gw-branch-fw02 runs a 97-day-old policy; from
`changes`/`audit_logs` that `grp-app-servers` was last changed to add app-02,
and app-03 host objects were re-created six times (`06-rca-report.png`,
`07-rca-report-full.png`). It stopped short of naming rule 9 "App to DB"
because its own pandas filter looked for `"PostgreSQL"`/`"5432"` in the
resolved service names while the object is named `postgresql` — a planner
string-match slip, not a connector gap: the same `access_rules` and
`group_members` frames (see the generated code captured in the sandbox DB)
contain rule 9 with source `grp-app-servers` and the group's members
app-01/app-02 only.

### Connector defects found by the live runs (fixed in this branch)

1. `audit_logs` with `start_time` only, or with an `end_time` in the future,
   was rejected ("Supply both start_time and end_time" / "cannot extend into
   the future"). An omitted or future `end_time` now means "up to now".
2. Provenance columns requested in `fields` and the opaque
   `ResultLimitExceeded` message (shared with the other connectors).

Regression tests cover each. Remaining transcript failures
(`SyntaxError: unterminated string literal`, `create_data` outside the
sub-agent profile) belong to the platform's code generator / tool profile.

## Faithfulness limits (see `tools/checkpoint/SOURCES.md`)

Inferred: the HTTP status (401) for wrong/missing-sid codes, 403 for a
mutating command under a read-only session, the `custom-start`/`custom-end`
format, `query-status` in `show-logs` replies and audit-log record keys. The
official commands, request objects and reply examples are verified. A
Multi-Domain Server was not simulated beyond echoing `domain`. A read-only
administrator on a real R81.20/R82 management server is still required
before GA.
