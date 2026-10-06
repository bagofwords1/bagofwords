# Feedback Loop — Microsoft login succeeds but Fabric is not connected

Microsoft sign-in completed, but automatic OBO provisioning logged `got Future ... attached to a different loop`. Manual connection sign-in worked. This loop reproduces the database failure without tenant credentials, then verifies persisted delegated credentials and per-user catalogs.

## Root cause (validated)

At baseline `73e9ffe72`, `backend/app/services/connection_oauth_service.py:796` imported the global request session factory inside `_auto_provision_in_background`, although `schedule_auto_provision` dispatched that coroutine to the dedicated indexing thread/event loop. A fresh session still borrowed an asyncpg connection from the main pooled engine. Reusing a request-loop connection on the worker loop failed at the user lookup, before token exchange.

The same unsafe global factory appeared in the subsequent overlay sessions (`connection_oauth_service.py`, `auto_provision_connection_credentials`) and the multi-connection catalog fan-out (`data_source_service.py`, `get_user_data_source_schema`). All three stages must preserve the worker engine.

## Loop A — deterministic reproduction

Requirements: Python 3.12, backend dev dependencies, Docker, and a fresh checkout. No Microsoft credentials are needed. The only mocked boundaries are Microsoft's token HTTP response and Fabric schema discovery; ORM, PostgreSQL, worker scheduling, credential encryption/persistence, and catalog persistence are real.

```bash
docker run -d --name bow-obo-repro-memory \
  --tmpfs /var/lib/postgresql/data:rw,size=512m \
  -e POSTGRES_PASSWORD=repro-local-only -e POSTGRES_DB=obo_repro \
  -p 127.0.0.1:55439:5432 postgres:16
docker exec bow-obo-repro-memory pg_isready -U postgres -d obo_repro
cd backend
uv sync --frozen --extra dev
BOW_DATABASE_URL=sqlite:///db/app.db TESTING=true \
TEST_DATABASE_URL=postgresql://postgres:repro-local-only@127.0.0.1:55439/obo_repro \
uv run pytest tests/unit/test_obo_background_database.py --db=external -q --disable-warnings
```

Wait for `pg_isready` to report accepting connections before running the test. The test suite resets the target database schema: use only this disposable database.

The regression deliberately warms a one-connection pool on the request loop before invoking the public OBO scheduler. The default test PostgreSQL engine uses NullPool and would hide this production bug. Cases cover both one and two connections on an agent. A worker-loop barrier waits for completion without sleeps.

Observed before the fix (first case, stopped with `-x`):

```text
OBO auto-provision (background) failed ... got Future ... attached to a different loop
AssertionError: assert set() == {<expected connection id>}
1 failed
```

Observed after the fix:

```text
2 passed
```

Both cases assert persisted OAuth credentials with refresh tokens and a catalog overlay for every connection. The test also verifies subsequent request-side database IO still succeeds.

## The fix

- `_auto_provision_in_background` uses the existing `create_async_database_engine_for_indexing` NullPool factory and disposes its engine in `finally`.
- Follow-on overlay sessions derive their engine from the caller's session.
- Multi-connection catalog workers also retain the caller's engine, while keeping separate sessions for concurrency.

This follows the existing indexing runner's database lifecycle rather than changing global production pooling or making login wait for provisioning.

## Loop B — live confirmation (not run)

After deployment, sign in with a fresh Entra user, confirm the `OBO auto-provisioned` and `OBO overlay sync` logs, then submit a programmatic Fabric prompt without manual agent sign-in. Real Entra consent, token audience, tenant policy, and Fabric SQL execution are not verified by Loop A. Supplied customer credentials were not used or saved.

## Scope and cleanup

This fixes the observed PostgreSQL loop-ownership crash. Login remains asynchronous; it does not guarantee Fabric is ready immediately on redirect. The separate `last_login` timezone error is out of scope. No UI, OAuth scopes, consent, or redirect configuration changes.

```bash
docker rm -f bow-obo-repro-memory
```

## Adjacent verification

```bash
BOW_DATABASE_URL=sqlite:///db/app.db TESTING=true uv run pytest \
  tests/unit/test_obo_background_database.py \
  tests/unit/test_refresh_overlay_prefetch.py \
  tests/e2e/test_fabric_second_admin_overlay_repro.py \
  --db=sqlite -q --disable-warnings
```

Observed: **9 passed**. New-test Ruff checks and `git diff --check` also passed. No failures remained in these selected suites; the full repository suite was not run.
