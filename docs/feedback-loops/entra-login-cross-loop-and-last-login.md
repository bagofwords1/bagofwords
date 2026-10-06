# Feedback Loop — "OBO auto-provision (background) failed … attached to a different loop"

After a successful Entra ID login on a Postgres deployment, the backend logs:

- `ERROR sqlalchemy.pool … Exception terminating connection … got Future … attached to a different loop`
- `WARNING app.services.connection_oauth_service: OBO auto-provision (background) failed for user …: … attached to a different loop`
- `ERROR asyncio: Future exception was never retrieved … InternalClientError('got result for unknown protocol state 3')`
- `WARNING app.services.auth_providers: Failed to record last_login … (can't subtract offset-naive and offset-aware datetimes)`

Login itself succeeds, but per-user OBO credentials are never provisioned and
`last_login` is never written.

## Root cause (validated)

1. **Cross-loop pool use.** `schedule_auto_provision` (`backend/app/services/connection_oauth_service.py`)
   submits `_auto_provision_in_background` to the background daemon-thread loop
   (`connection_indexing_service._get_background_loop`). That coroutine opened its
   session with `app.dependencies.async_session_maker`: the main engine, whose
   pooled asyncpg connections are bound to the request loop. Introduced in
   `ed1709a`. `app/settings/database.py:create_async_database_engine_for_indexing`
   documents exactly this failure and the NullPool remedy; the indexing runner
   uses it, auto-provisioning did not.
2. **Aware datetime into a naive column.** `users.last_login` is
   `TIMESTAMP WITHOUT TIME ZONE`. `_record_login` (`backend/app/services/auth_providers.py`)
   and `on_after_login` (`backend/app/core/auth.py`, error swallowed by
   `except: pass`) passed `datetime.now(timezone.utc)`; asyncpg rejects it.
   SQLite accepts it, which is why tests never caught it.

## Loop A — deterministic reproduction (local Postgres, no Entra)

```bash
P=/usr/lib/postgresql/16/bin; mkdir -p /tmp/pg && chown postgres /tmp/pg
su postgres -c "$P/initdb -D /tmp/pg/data -A trust && $P/pg_ctl -D /tmp/pg/data -l /tmp/pg/log -o '-p 5433 -k /tmp/pg -c listen_addresses=localhost' start"
psql -h /tmp/pg -p 5433 -U postgres -c "create database bowtest"
cd backend && export BOW_DATABASE_URL=postgresql://postgres@localhost:5433/bowtest
uv run alembic upgrade head
uv run python ../tools/agent/repro_entra_login_pg.py
```

The script uses the production (pooled) engine, warms it on the main loop as a
live server would, then runs `schedule_auto_provision` and `_record_login`.

Before the fix (same signatures as the reported log):

```
RuntimeError: Task <… _terminate_graceful_close() …> got Future <Future pending> attached to a different loop
asyncpg.exceptions._base.InternalClientError: got result for unknown protocol state 3
WARNING | app.services.auth_providers:_record_login:547 - Failed to record last_login … (can't subtract offset-naive and offset-aware datetimes)
last_login = None
FAIL
```

Unit regression (SQLite, no services): `uv run pytest tests/unit/test_entra_login_side_effects.py`
gives `2 failed` before the fix.

## Loop B — live Entra confirmation

Not run. Loop A reproduces the exact log signatures without a tenant. To run it
live, boot the stack (`tools/agent/boot_stack.sh`) against Postgres, configure
the Entra OIDC provider and a Fabric connection from env vars, sign in as a
tenant user and grep the backend log for the lines above.

## The fix

- `_auto_provision_in_background` builds its session from a dedicated NullPool
  engine (`create_async_database_engine_for_indexing`) and disposes it afterwards.
- `_record_login` and `on_after_login` write `datetime.now(timezone.utc).replace(tzinfo=None)`.

After:

```
last_login = 2026-10-06 11:31:46.599961
PASS
```
and `2 passed` for the unit tests.

## What this proves / regression notes

The unit tests check the general rule (no aware `last_login` writes; background
provisioning never touches the request engine), not the single reported case.
