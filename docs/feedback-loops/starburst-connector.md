# Starburst (Galaxy / Enterprise) connector — feedback loop

How to reproduce, fix and verify the Trino/Starburst connector end to end:
unit tests with a faked driver, a live Starburst Galaxy cluster through the
app's own client, the agent's sandboxed execution path, and the UI.

## What was broken (before)

Measured against the pinned `trino` driver and a live Galaxy cluster:

| Symptom | Cause |
|---|---|
| Starburst Enterprise on 8443 (its default TLS port) failed to connect; basic-auth credentials went out over plain HTTP | `protocol` was stored on the client but never passed to the driver, which only infers HTTPS from port 443 |
| No way to trust an internal CA or to disable verification | no `verify` passthrough |
| A service account with a restricted default role saw no tables | no `roles` passthrough; Galaxy/Enterprise pick visibility by role |
| HTTPS without a password raised; JWT / token logins impossible | `password` was mandatory over HTTPS, no token credential variant |
| One catalog + one schema per connection, bare table names | discovery read one `information_schema` filtered by schema, named tables `table` |
| No table/column comments, no view flag | discovery read only `information_schema.columns` without `comment` |
| Timed-out queries kept running on the cluster | `query_cancellation` had no Trino branch (reported `unsupported: trino`) |
| "Starburst" / "Galaxy" found nothing in the connector picker | no registry entry, no aliases, no icon |
| Agent guidance said the clock is UTC and nothing about Trino SQL | stale `relative_date_hint`, generic `description` |

## The fix (what to look at)

- `backend/app/data_sources/clients/trino_client.py` — single client for
  `trino`, `starburst` and the legacy `presto`. URL carries identity only;
  `connect_args` carry `http_scheme`, `verify`, `roles`, `source`, `timezone`.
  `execute_query` runs on a raw driver cursor parked on the pooled connection's
  `info` so `query_cancellation` can `cancel()` it cross-thread. Discovery walks
  `[catalog] + additional_catalogs`, reads comments and view flags, names
  tables `schema.table` (session catalog) / `catalog.schema.table` (others),
  and exposes `count_tables()` for the connection test.
- `backend/app/schemas/data_sources/configs.py` — `TrinoConfig` (+ the
  `StarburstConfig` subclass with 443/https defaults), `TrinoCredentials`,
  `TrinoTokenCredentials`.
- `backend/app/schemas/data_source_registry.py` — `starburst` entry with a
  Galaxy + Enterprise setup guide; both entries use an explicit `client_path`.
- `backend/app/data_sources/query_cancellation.py` — `trino` branch.
- `backend/app/data_sources/fast/sql_dialect.py` — `trino` bounds with LIMIT.
- `backend/app/ai/code_execution/code_execution.py` — Trino error hints
  (`CATALOG_NOT_FOUND`, `SCHEMA_NOT_FOUND`, `TABLE_NOT_FOUND`, `PERMISSION_DENIED`).
- `frontend/public/data_sources_icons/starburst.png`,
  `frontend/composables/useDataSourcePicker.ts` (aliases).
- Driver: `trino` 0.337 → 0.340 (0.339 refuses credentials over plain HTTP,
  which is why the HTTPS passthrough has to land first).

## 1. Unit tests (no network)

```bash
cd backend
TESTING=true BOW_DATABASE_URL='sqlite:///db/app.db' uv run pytest \
  tests/unit/test_trino_client.py tests/unit/test_query_cancellation.py \
  tests/unit/test_engine_pool_identity.py tests/unit/test_fast_sql_dialect.py \
  tests/unit/test_db_error_hints.py -q
```

`test_trino_client.py` fakes `engine_pool.get_engine`; it pins the URL /
connect-args contract, the discovery naming, comments, view flags, the schema
filter, the fallback, `count_tables()`, and the cancellation hook.

## 2. Live cluster through the app's client

Needs a Galaxy service account (Access control > Service accounts) with a
role that has *Use cluster* and *Select from table* on the catalogs. Never
commit the password; pass it through the environment.

```bash
cd backend
SB_USER='<service-account>@<account>.galaxy.starburst.io' SB_PW='<password>' uv run python - <<'EOF'
import os, threading, time
from app.data_sources.clients.trino_client import TrinoClient
from app.data_sources import query_cancellation as qc
c = TrinoClient(host='<account>-<cluster>.trino.galaxy.starburst.io', port=443, protocol='https',
                catalog='tpch', schema='sf1', additional_catalogs='tpcds', role='public',
                user=os.environ['SB_USER'], password=os.environ['SB_PW'], timezone='Asia/Jerusalem')
print(c.test_connection())                      # {'success': True, ...}
print(c.count_tables(), len(c.get_tables()))    # equal; sf1.* and tpcds.sf1.* names
print(c.execute_query('SHOW CURRENT ROLES').values.tolist())      # [['public']]
print(c.execute_query('SELECT current_timezone()').values.tolist())  # [['Asia/Jerusalem']]
out = {}
def long():
    try: c.execute_query('SELECT count(*) FROM tpch.sf100.lineitem a, tpch.sf100.lineitem b WHERE a.orderkey=b.partkey')
    except Exception as e: out['r'] = str(e)[:80]
t = threading.Thread(target=long); t.start(); time.sleep(6)
print(qc.cancel_thread(c, t.ident))             # cancelled (trino cursor cancel)
t.join(); print(out)                            # USER_CANCELED from the cluster
EOF
```

Observed 2026-11-04 against `bow14-free-cluster`: test 28 s on a cold
cluster (Galaxy auto-suspends; warm round trips are 1–2 s), 33 tables across
two catalogs, role and timezone honoured, cancel returned in 0.5 s and the
cluster reported `USER_CANCELED`.

To prove the 8443 downgrade without an Enterprise cluster, build the driver's
connection the way the dialect did before the fix:

```bash
uv run python -c "
from sqlalchemy.engine import make_url; from trino.sqlalchemy.dialect import TrinoDialect; import trino.dbapi as d
for u in ['trino://u:p@host:443/c','trino://u:p@host:8443/c']:
    a,k=TrinoDialect().create_connect_args(make_url(u)); print(u, '->', d.Connection(*a,**k).http_scheme)"
# 443 -> https, 8443 -> http  (driver 0.337; 0.339+ raises TrinoAuthError instead)
```

## 3. The agent's execution path without an LLM

The sandbox's LLM key had no credit, so the coder was scripted. This drives
`StreamingCodeExecutor` (sandbox, read-only guard in the `trino` sqlglot
dialect, timeout wrapper, error hints) against the live cluster:

```bash
cd backend
SB_USER=... SB_PW=... TESTING=true BOW_DATABASE_URL='sqlite:///db/app.db' uv run python - <<'EOF'
import asyncio, os
from app.ai.code_execution.code_execution import StreamingCodeExecutor
from app.ai.schemas.codegen import CodeGenContext, CodeGenRequest
from app.data_sources.clients.trino_client import TrinoClient
client = TrinoClient(host='<host>', port=443, protocol='https', catalog='tpch', schema='sf1',
                     additional_catalogs='tpcds', role='public', user=os.environ['SB_USER'], password=os.environ['SB_PW'])
code = lambda sql: f"def generate_df(ds_clients, excel_files):\n    return ds_clients[list(ds_clients)[0]].execute_query({sql!r})\n"
def drive(codes):
    calls = []
    async def gen(**kw): calls.append(kw); return codes[min(len(calls)-1, len(codes)-1)]
    ev = []
    async def go():
        async for e in StreamingCodeExecutor().generate_and_execute_stream_v2(
            request=CodeGenRequest(context=CodeGenContext(user_prompt='x', schemas_excerpt=''), retries=2),
            ds_clients={'Starburst Galaxy:starburst-1': client}, excel_files=[], code_generator_fn=gen): ev.append(e)
    asyncio.run(go()); return calls, ev
calls, ev = drive([code("SELECT orderpriority, count(*) n FROM sf1.orders WHERE orderdate >= date '1997-01-01' GROUP BY 1")])
print([e for e in ev if e['type']=='done'][-1]['payload']['df'].head())
calls, ev = drive([code("SELECT count(*) n FROM sf1.nation n JOIN tpcds.sf1.customer_address a ON n.name = upper(a.ca_country)")])
calls, ev = drive([code("DELETE FROM sf1.orders WHERE 1=0"), code("SELECT 1")]); print(calls[1]['code_and_error_messages'][-1][1][:80])
calls, ev = drive([code("SELECT * FROM sf1.no_such_table"), code("SELECT 1")]); print('Hint: Trino could not find that table' in calls[1]['code_and_error_messages'][-1][1])
EOF
```

Observed: group-by returned in 10 s with one codegen call; the cross-catalog
join returned 48,475; `DELETE` was refused by the sandbox before reaching the
cluster; `TABLE_NOT_FOUND` came back with the Trino hint in the retry feedback.

## 4. UI

```bash
BOW_ENCRYPTION_KEY=<fernet key> BOW_CHROMIUM_EXECUTABLE=/opt/pw-browsers/chromium tools/agent/boot_stack.sh --dev
```

Then, with Playwright (`executablePath: '/opt/pw-browsers/chromium'`):

1. `/users/sign-up` → lands on `/onboarding`.
2. `/onboarding/data`: type `starburst` (or `galaxy`) in
   `[data-testid="onboarding-ds-search"]` → the **Starburst** tile (and Trino,
   via alias) → click it.
3. Fill `#connection-name`, `#host`, `#port` (443), `#catalog`, `#schema`,
   `#additional_catalogs`, `#role`, `#user`, `#password`; the setup guide
   (`[data-testid="setup-guide"]`) sits above the fields.
4. **Test connection** → wait until "Testing…" disappears; **Save and
   Continue** → `/onboarding/data/<id>/schema` lists `sf1.*` and `tpcds.sf1.*`.
5. `connection_tables` in `db/agent.db` holds 33 rows with
   `metadata_json = {"catalog","schema","type"}`.

Screenshots: `media/pr/ai-dreamy-franklin-gkbpbu/`.

Gotchas met on the way: the boot script's backend did not inherit
`BOW_ENCRYPTION_KEY` from the shell (restart it with the key exported, or
JWTs die on the next restart); once onboarding is dismissed,
`/onboarding/data/...` redirects to `/`, so take onboarding screenshots before
"Skip onboarding"; the chat step needs an LLM key with credit.

## Not verified here

- Starburst Enterprise itself (no cluster available): the 8443/HTTPS path is
  covered by the driver-level proof above and by unit tests, not by a live
  Enterprise coordinator.
- Token (JWT) login against Galaxy: Galaxy's API client tokens are not SQL
  tokens (the SQL endpoint answers 401 to them), and no user JWT was at hand.
  The variant is wired exactly like the driver's `access_token` URL parameter.
- A full LLM-driven chat turn (no credit on the sandbox key).
