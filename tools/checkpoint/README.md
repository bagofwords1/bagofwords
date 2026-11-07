# Check Point Management API connector (read-only)

Native connector for the Check Point **Management Web API** (R81.10 / R81.20 / R82;
contract frozen at API **v1.9.1**, the R81.20 JHF Take 43 reference, and verified
against the v2 / R82 reference data). No MCP server, SDK or Check Point license is
needed for local development. Customer validation on a real Security Management
Server (or Smart-1 Cloud tenant) is still required before acceptance.

## What the connector does

Every query opens a session with `login` **always sending `"read-only": true`**,
issues allow-listed `show-*` commands with `X-chkp-sid`, and always `logout`s.
The allow-list lives in
`backend/app/data_sources/clients/checkpoint/catalog.json` (regenerate with
`python3 tools/checkpoint/build_catalog.py`); the client cannot build a request
for a command outside it, so `publish`, `set-*`, `add-*`, `delete-*`,
`install-policy`, `run-script` and `discard` are unreachable by construction.

Tables (26): `hosts`, `networks`, `address_ranges`, `groups`, `group_members`,
`services_tcp`, `services_udp`, `service_groups`, `packages`, `access_layers`,
`access_rules`, `nat_rules`, `threat_rules`, `gateways_and_servers`,
`simple_gateways`, `sessions`, `unused_objects`, `tasks`, `logs`, `audit_logs`,
`api_versions`, `session`, `last_published_session`, `domains`,
`vpn_communities`, `changes`.

```python
client.execute_query({"table": "access_rules", "parent": {"layer": "Standard Network"},
                      "fields": ["section", "rule-number", "name", "enabled", "action", "source", "destination", "service"]})
client.execute_query({"table": "logs", "lookback": "24h", "search": "action:Drop AND blade:Firewall", "limit": 500})
client.execute_query({"table": "gateways_and_servers",
                      "fields": ["name", "version", "sic-status", "policy.access-policy-installation-date.iso-8601"]})
```

Queries are JSON objects (or strings). Controls: `table`, `fields`, `filter`
(client-side, `{"op": "eq|ne|gt|ge|lt|le|like|in|contains", "value": ...}`),
`search` (server-side Check Point filter text), `parent` (`layer` for
`access_rules`/`threat_rules`, `package` for `nat_rules`), `limit` (complete-result
ceiling, never a silent truncation), `order_by`, `lookback`/`start_time`/`end_time`
and `time_frame` for logs, tasks and changes. Rulebases come back flattened in
rulebase order with names resolved through the `objects-dictionary`.

## Run the simulator

```bash
# From the repository root. Use throwaway values; never customer credentials.
export CHECKPOINT_USERNAME='reader'
export CHECKPOINT_PASSWORD='synthetic-only'
export CHECKPOINT_API_KEY='synthetic-key'      # optional second login method
python3 tools/checkpoint/simulated_api.py --port 18092 [--domain "SMC User"]
```

Then in BOW add a **Check Point Management** connection with URL
`http://127.0.0.1:18092`, *Allow HTTP for a simulator* enabled, and either the
API key or the username/password above. Or from Python:

```python
from app.data_sources.clients.checkpoint_client import CheckPointClient
c = CheckPointClient(url="http://127.0.0.1:18092", username="reader", password="synthetic-only", allow_http=True)
c.test_connection()
```

Hand-check the handshake with curl exactly as the documentation describes it:

```bash
curl -s -X POST http://127.0.0.1:18092/web_api/v1.9.1/login -H 'Content-Type: application/json' \
     -d '{"user":"reader","password":"synthetic-only","read-only":true}'
# -> {"sid": "...", "url": "...", "uid": "...", "session-timeout": 600, "api-server-version": "1.9.1", "read-only": true, "standby": false}
curl -s -X POST http://127.0.0.1:18092/web_api/show-hosts -H 'Content-Type: application/json' -H "X-chkp-sid: $SID" \
     -d '{"limit":50,"offset":0,"details-level":"standard"}'
curl -s -X POST http://127.0.0.1:18092/web_api/logout -H 'Content-Type: application/json' -H "X-chkp-sid: $SID" -d '{}'
```

### What the simulator models

* `POST /web_api/<command>` and `POST /web_api/v<version>/<command>` (versions 1 ... 1.9.1), JSON bodies only.
* `login` with `user`/`password` or `api-key`, optional `domain` (only the simulator's
  single domain is accepted), `read-only`; reply with `sid`, `uid`, `url`,
  `session-timeout` 600, `api-server-version`, `read-only`, `standby`,
  `last-login-was-at`. Bad credentials -> HTTP 400 `err_login_failed`.
* `X-chkp-sid` validation: missing -> 401 `generic_err_missing_session_id`, unknown
  -> 401 `generic_err_wrong_session_id`, idle > 600 s -> 401 `generic_err_session_expired`.
  `logout` invalidates the sid; `keepalive` answers `{"message":"OK"}`.
* Collections: `limit` (default 50, max 500, else 400 `generic_err_invalid_parameter`),
  `offset`, `details-level` `uid|standard|full`, `order` `[{"ASC":"name"}]`, `filter`
  (SmartConsole-style text search with `AND`/`OR`), `show-membership`,
  `dereference-group-members`, `show-as-ranges`; envelope `from`/`to`/`total`.
  Unknown parameter names -> 400 `generic_err_invalid_parameter_name`.
* Rulebases: `show-access-rulebase` (layers *Standard Network* with 3 sections and 20
  rules, *DMZ Network*), `show-nat-rulebase` (package *Standard*),
  `show-threat-rulebase`; `use-object-dictionary`, `show-as-ranges`, `show-hits`,
  `filter`, paging over rules. Unknown layer/package -> 404 `generic_err_object_not_found`.
* `show-logs`: `new-query` (`filter` in the Check Point query language, `time-frame`
  including `custom` with `custom-start`/`custom-end`, `max-logs-per-request`, `type`
  `logs|audit`, `top`) then `query-id` paging; ~200 traffic logs and 50 audit logs
  over the last 48 hours; `logs-count` as a string like the reference example.
* `show-gateways-and-servers`, `show-simple-gateways`, `show-sessions`
  (`view-published-sessions`), `show-session`, `show-last-published-session`,
  `show-tasks`/`show-task`, `show-changes` (synchronous `tasks` reply like the
  reference example), `show-unused-objects`, `show-domains`,
  `show-vpn-communities-meshed|star`, `show-objects`, `show-api-versions`.
* **Every mutating command** (`publish`, `discard`, `add-*`, `set-*`, `delete-*`,
  `install-policy`, `run-script`, ...) -> 403 `generic_err_no_permissions` after the
  session check. Unknown commands -> 404 `generic_err_command_not_found`.
  Unknown API version -> 400 `err_unknown_api_version`. GET -> 405.

### The estate and its built-in incidents

* 16 hosts, 8 networks, 2 address ranges, 5 groups, 10 services, 3 service groups,
  2 packages (`Standard`, `DMZ`), 2 access layers, 1 threat layer.
* Policy smells: rule 12 *TEMP troubleshooting - Any Any Accept* shadows rules 13-14;
  rules 4, 14 and 19 are disabled; rule 20 is the cleanup rule; DMZ rule 2 allows
  DMZ -> DB.
* `app-03` (10.1.20.23) was added last week but never put in `grp-app-servers`, so its
  PostgreSQL traffic is dropped by the cleanup rule (visible in `logs`, `unused_objects`
  and `changes`).
* Gateways: `gw-hq-fw01` healthy (R81.20, policy 2 days old); `gw-branch-fw02` on
  R81.10 with a 97-day-old policy; `gw-dmz-fw03` with SIC *not communicating* and no
  policy installed (failed install task 3 days ago, failed audit entries).
* Sessions: `svc-automation` holds a stale lock (open, not in work, last login 5 days ago).

### What it does not model

Multi-Domain Servers (`domain` is accepted only for the single domain), Smart-1 Cloud
proxies, `show-as-ranges` for service groups, log `top` beyond simple counts, task
polling (`show-changes` completes synchronously), policy installation or any write.
Its data does not demonstrate real Check Point performance or exact JHF behaviour.

## Tests

```bash
cd backend && mkdir -p db
export TESTING=true BOW_DATABASE_URL=sqlite:///db/checkpoint-tests.db
uv run pytest tests/unit/test_checkpoint_client.py -q --db=sqlite
uv run pytest tests/e2e/test_checkpoint.py tests/e2e/test_data_source.py tests/e2e/test_connection.py --db=sqlite -q
```

The unit suite mocks only `requests.Session.send`; one test boots the simulator on
an ephemeral port and drives the real client over HTTP.

## Provenance

Field names, parameters and envelopes come from the official Management API reference
data (`data/v1.9.1/dynamic/apis.json` + `examples.json`), the Check Point Python SDK,
the Ansible collection, the official MCP servers and CheckMates threads; every URL and
every inferred shape is listed in `tools/checkpoint/SOURCES.md`.
