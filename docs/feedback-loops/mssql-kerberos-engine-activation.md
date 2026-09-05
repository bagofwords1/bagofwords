# Feedback Loop — Delegated SQL connections use the wrong identity

Concurrent cold-start SQL Server connections authenticated successfully with
Kerberos but sometimes used the service identity instead of the requested
delegated user. Authentication success alone is not proof of identity isolation.

## Validated boundary

In `backend/app/data_sources/clients/mssql_client.py:143`, engine lookup and lazy
driver initialization must execute inside `KerberosTicketManager.activate`, just
like the connection handshake. Previously, `get_engine` ran before activation,
outside both the requested `KRB5CCNAME` selection and its process-wide lock.

Moving engine creation into that scope eliminated the observed live failure.
This identifies the unsafe application boundary; the precise internal native
driver/GSS cache operation has not been traced. The patch does not change ticket
acquisition, pool identity keys, or SQL-login authentication.

## Loop A — deterministic, self-contained regression

From a checkout with Python 3.12 and the backend development dependencies:

```bash
cd backend
TESTING=true BOW_DATABASE_URL=sqlite:///db/app.db PYTHONPATH=. \
  uv run pytest tests/unit/test_mssql_kerberos.py \
  -k connections_preserve_requested_kerberos_identity -q --disable-warnings
```

The regression exercises the public client connection API with the real ticket
manager, engine pool, and cancellation tracking. Fake GSSAPI and an external
SQLAlchemy/driver adapter remove any need for AD, ODBC, SQL Server, or credentials.
The adapter models credential-sensitive initialization by capturing the selected
identity when the engine is created and returning it from an identity query.

Coverage includes both supported ODBC driver selections, sequential and
concurrent use, two delegated identities, an explicit service principal, a
default service cache, cold engines, and warm pool reuse. A barrier inside the
connection body also ensures established queries can overlap; there are no sleep
or wall-clock performance assertions.

Before the fix: **4 failed**, with identity mismatches such as
`ambient-service != reader@EXAMPLE.TEST`.
After the fix: all four cases must pass. Restoring the engine lookup to before
activation makes the same regression fail again.

Local verification after porting the patch: **75 unit tests passed** across
`test_mssql_kerberos.py`, `test_engine_pool_identity.py`,
`test_engine_pool_lifecycle.py`, and `test_query_cancellation.py`; **4 API tests
passed** in `tests/e2e/test_kerberos_sso_member_overlay.py` (SQLite).

## Loop B — live lab evidence

The candidate was tested in disposable Linux containers against real AD and SQL
Server before being ported into this checkout. No image rebuild was needed.
Only synthetic users/data were used; keytabs were mounted read-only and no
credentials, customer identifiers, host addresses, or raw session logs are
included here.

- Unchanged image: two cold-start runs each returned the wrong identity for
  **24 of 48** concurrent queries.
- Minimal patch: six final fresh-container runs returned the correct identity
  for **288 of 288** queries, with SQL reporting `KERBEROS`.
- Two additional runs each checked 32 cold, 32 warm, and 32 credential-reacquired
  connections: **192 identity/permission cases passed**. The reader was denied
  the restricted table while the authorized analyst could read it.
- Four two-second SQL queries overlapped and completed in approximately two
  seconds in each run, rather than being serialized into eight seconds.
- Service-account queries also passed.

For live re-verification, use an isolated AD/SQL lab with two domain users whose
read permissions differ and an appropriately scoped delegation service account.
Acquire credentials through a mounted secret, start fresh processes, alternate
the users across concurrent connections, and assert both `ORIGINAL_LOGIN()` and
`CONNECTIONPROPERTY('auth_scheme')` before checking allowed and denied reads.
Repeat after pool reuse and credential invalidation. Never infer isolation from
successful queries or `KERBEROS` alone.

## Fix and boundaries

Engine lookup/creation and `engine.connect()` now share the same credential-cache
activation. The existing lock is released before yielding the connection, so
query bodies remain concurrent. No new global setup lock was introduced.

The live results above concern the isolated remote candidate, not a full browser,
SSO, or LLM flow. Credential rotation/expiry and all native-driver interleavings
are not certified by these runs. This patch does not add a separate fail-closed
SQL identity check or change the application's trusted SSO-to-principal mapping.
