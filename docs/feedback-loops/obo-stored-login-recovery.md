# Feedback Loop — existing Microsoft users remain disconnected after the OBO fix

The cross-loop fix in #1255 prevents future provisioning crashes, but existing BOW sessions do not replay the Entra login callback. Recover a missing connection from that user's stored Entra account when its status is read or a query needs credentials. No interactive login is fabricated: Microsoft must issue a valid delegated token before BOW marks the connection authenticated.

## Root cause and baseline

Before recovery, `build_token_identity_status` only looked up existing connection credentials and returned `effective_auth="none"` when absent. `ConnectionService.resolve_credentials` returned Connect required. Neither retried OBO using the account saved by the successful Microsoft login.

Baseline test on the already-fixed background implementation: `test_missing_connection_recovers_from_stored_login_or_fails_closed[valid-admin]` failed with `assert 0 == 1` recovered credentials. The stored login token and successful provider response were available, but no recovery occurred.

## Implementation

- A missing credential row on connection status/query paths schedules `schedule_stored_login_recovery`; the current request remains fail-closed.
- The worker uses the background-safe database engine introduced in #1255, checks active user/connection and current organization membership, and only selects that user's enabled Entra provider with a matching OAuth client ID.
- It tries the stored access token if not known expired. If unavailable/rejected, it makes at most one refresh attempt with the original login scopes, then retries OBO once. It never substitutes a Graph-scoped refresh request or system credentials.
- Credentials are written only after a successful exchange, and the existing catalog sync runs afterward. Existing rows, including inactive rows and service-account preferences, are not overwritten.
- Explicit Disconnect clears tokens and retains an inactive marker to prevent silent reconnection. A future explicit sign-in can reconnect normally. Historical hard-deleted disconnects cannot be distinguished from never-provisioned users.
- Retries are limited per process to one attempt per user per five minutes, at most 16 running jobs, a bounded 4096-key cooldown cache, and a 120-second job deadline. Multiple server workers have independent limits.
- Failures preserve Connect required and do not log raw provider response bodies, assertions, refresh tokens, or exception messages. Numeric AADSTS codes remain available.

## Review follow-up: per-user recovery

Recovery was first keyed per (user, connection), so a status read of an agent
with N missing connections started N jobs: N OBO exchanges (bypassing the
per-app dedup cache), N parallel refreshes of the same refresh token, and a
whole-data-source catalog sync per recovered connection (N x N crawls). It is
now one job per user that calls `auto_provision_connection_credentials(...,
missing_only=True, client_id=...)`, which reuses the exchange cache and the
per-data-source catalog sync. `missing_only` adds the recovery guards
(current memberships, active connections, no existing row of any kind,
matching app).

Login provisioning now also skips connections with an inactive
`auto_recovery_disabled` marker, so a Disconnect is not undone at the next
sign-in. Transport errors in the shared provisioning loop are logged by type
only (a timeout message could carry request data).

`tests/unit/test_obo_recovery_per_user.py`: 5 connections -> 1 exchange
(was `assert 5 == 1`); Disconnect then login provisioning -> nothing
re-provisioned (was 1 row). Both fail on the previous head, pass now.

## Reproduce / verify

Use a fresh sandbox and backend Python 3.12 dependencies. External boundaries only: Microsoft token/discovery HTTP and Fabric schema discovery. Tokens are synthetic; no customer secrets are used.

```bash
cd backend
uv sync --frozen --extra dev
BOW_DATABASE_URL=sqlite:///db/app.db TESTING=true uv run pytest \
  tests/unit/test_obo_stored_login_recovery.py \
  tests/unit/test_obo_background_database.py \
  --db=sqlite -q --disable-warnings
```

For PostgreSQL use the disposable container setup in [the original feedback loop](obo-background-postgres-loop.md), then:

```bash
BOW_DATABASE_URL=sqlite:///db/app.db TESTING=true \
TEST_DATABASE_URL=postgresql://postgres:repro-local-only@127.0.0.1:55439/obo_repro \
uv run pytest tests/unit/test_obo_stored_login_recovery.py \
  tests/unit/test_obo_background_database.py --db=external -q --disable-warnings
```

Coverage includes the completion SSE client-preparation path, direct credential resolution (success versus 403), both admin/member roles, valid and expired tokens, recovery after an assertion rejection, rejected refresh/OBO, no account, no membership, wrong client, explicit Disconnect, service-account preferences, missing refresh tokens, timeouts, malformed token responses, catalog persistence, repeat-read cooldown, and log redaction. The original PostgreSQL cross-loop tests still cover one/multiple connections.

## Operational expectations / limitations

Recovery starts on use, not immediately at deployment. Status polling or a later request sees the recovered credentials/catalog. The first request can still show Connect required until recovery finishes. Unknown/deleted historical credential rows cannot prove whether the user intentionally disconnected before this change. Users with existing credential rows but broken tokens/catalogs still use the normal refresh/reconnect paths.

Live Entra consent, MFA/Conditional Access, provider configuration, and Fabric SQL execution remain a deployment verification step. If Microsoft requires interaction, manual Sign in remains necessary. No migration or frontend changes are required.

## Observed verification

- PostgreSQL: **28 passed** (recovery scenarios plus the original background-loop regressions).
- SQLite plus catalog prefetch, Fabric second-admin overlay, and Graph connector regressions: **52 passed**.
- New-file Ruff and `git diff --check`: passed.
- The first adjacent-suite run lacked local test-server permissions and stalled; it was stopped and rerun with those permissions, producing the 52-pass result. No remaining failures in the selected suites; the full suite was not run.

An additional PostgreSQL verification run exhausted the disposable 512 MiB WAL filesystem after repeated full schema migrations (`pg_wal/xlogtemp: No space left on device`), causing 15 setup errors after 11 passing tests. Recreated the sandbox with 1 GiB tmpfs, `max_wal_size=128MB`, `min_wal_size=32MB`, and `checkpoint_timeout=30s` for the final rerun. These were test-environment errors, not an asserted baseline application defect.
