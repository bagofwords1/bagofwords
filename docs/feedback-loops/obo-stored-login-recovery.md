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
- Retries are limited per process to one attempt per user per five minutes, at most 16 running jobs, a bounded 4096-key cooldown cache, and a 120-second job deadline. Multiple server workers have independent cooldown/admission limits; PostgreSQL advisory locks additionally exclude overlapping login/recovery jobs for the same user across workers.
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

Interactive popup behavior, MFA/Conditional Access challenges, and the customer production deployment remain deployment verification steps. Live Entra and Power BI results are recorded below; Fabric SQL was not the requested target. If Microsoft requires interaction, manual Sign in remains necessary. No migration or frontend changes are required.

## Observed verification

- PostgreSQL: **28 passed** (recovery scenarios plus the original background-loop regressions).
- SQLite plus catalog prefetch, Fabric second-admin overlay, and Graph connector regressions: **52 passed**.
- New-file Ruff and `git diff --check`: passed.
- The first adjacent-suite run lacked local test-server permissions and stalled; it was stopped and rerun with those permissions, producing the 52-pass result. No remaining failures in the selected suites; the full suite was not run.

An additional PostgreSQL verification run exhausted the disposable 512 MiB WAL filesystem after repeated full schema migrations (`pg_wal/xlogtemp: No space left on device`), causing 15 setup errors after 11 passing tests. Recreated the sandbox with 1 GiB tmpfs, `max_wal_size=128MB`, `min_wal_size=32MB`, and `checkpoint_timeout=30s` for the final rerun. These were test-environment errors, not an asserted baseline application defect.

## Live Entra + Power BI verification — 2026-10-06

Run against the supplied demo tenant using the actual Microsoft token endpoint,
Power BI REST API, and the unmocked BOW recovery implementation, with a disposable
PostgreSQL database. Each user was a member of a local organization with a public
Power BI agent configured for `user_required` OAuth. No service-account fallback.

| Case | Before recovery | After recovery | Real Power BI API |
|---|---|---|---|
| Demo1, stored login access token | 0 clients | 1 credential, 63 accessible catalog tables, `effective_auth=user` | 4 visible workspaces; DAX HTTP 200 |
| Demo2, forced stored-token expiry | 0 clients | 1 credential, 56 accessible catalog tables, `effective_auth=user`; login expiry renewed | 2 visible workspaces; DAX HTTP 200 |
| Invalid assertion + invalid refresh token | 0 clients | 0 credentials, 0 catalog tables, `effective_auth=none` | Access stayed denied |

Both successful cases produced 2 client dictionary entries after recovery (the
qualified connection key plus its single-connection alias, not two identities).
The only DAX query was `EVALUATE ROW("connection_verified", 1)`; no remote data
was changed. The catalog counts differ by user and reflect live delegated access.

Authentication used the password grant for the supplied demo users to obtain
real delegated login tokens with the configured API scope and `offline_access`.
This verified live token acceptance, OBO, refresh, persisted credentials, catalog
sync, and a Power BI query. It did **not** drive the popup UI or send a complete
LLM-backed SSE request: it invoked the same `prepare_run_agents` service path
that the completion SSE handler invokes. Demo2's local expiry metadata was set
past expiry to force a real refresh-token exchange without waiting an hour.

### Rerun

Use a disposable local PostgreSQL database with BOW migrations applied (the
script adds only local test fixtures). From `backend`, set non-secret identifiers
and the two demo usernames through environment variables:

```bash
export TESTING=true
export TEST_DATABASE_URL=postgresql://postgres:live-local-only@127.0.0.1:55440/obo_live
export BOW_DATABASE_URL=sqlite:///db/app.db
export BOW_LIVE_CLIENT_ID='<demo-app-client-id>'
export BOW_LIVE_TENANT_ID='<demo-tenant-id>'
export BOW_LIVE_USERS='<demo-user-1>,<demo-user-2>'
# Optional BOW_LIVE_LOGIN_SCOPE overrides the configured API scope assumption.
uv run python ../tools/agent/verify_obo_recovery_live.py
```

The script prompts invisibly for the app secret and the shared demo password
(or reads `BOW_LIVE_CLIENT_SECRET` / `BOW_LIVE_USER_PASSWORD`). Tokens stay in
process memory and the disposable database; remove the container/volume after
verification. Do not run against a real BOW deployment database. The script
requires a localhost database and `TESTING=true`.

A preliminary Fabric SQL probe was stopped when the user clarified Power BI as
the target; its local ODBC/OpenSSL load failure was unrelated to token exchange.
The final Power BI test does not use ODBC. Initial live-harness seeding omitted a
required report slug; correcting the harness resolved that setup error without
any application change.


## Final concurrency verification — PostgreSQL, no migration

On head `47d86e69a`, the new deterministic reproduction produced **7 failures**:
Disconnect and membership removal during an exchange still produced credentials
(admin and member), concurrent recovery/recovery and recovery/login both
provisioned, and five connections made five identical rejected exchanges.
The race was in `connection_oauth_service.py:899-1000`: eligibility/credential
snapshots preceded network calls, with no database coordination at persistence.

The final implementation uses two separate stable per-user advisory keys in
`credential_coordination.py`:

- `provisioning_lock`: nonblocking PostgreSQL session lock on a dedicated physical
  connection, shared by login provisioning and recovery (including assertion
  refresh). ORM commits cannot release it. Cancellation shields cleanup, explicitly
  unlocks, and closes the physical connection. Nested calls in the same task/session
  reuse ownership. Catalog crawling starts after this lock is released.
- `lock_credential_writes`: short transaction lock shared by final provisioning,
  manual OAuth Connect/failed-verification restoration, Disconnect and query-identity
  writes. No remote calls happen while holding it. Final provisioning re-reads user,
  membership, connection configuration and credential choices; shared row locks keep
  the checked user/membership/connection state stable through commit. A changed
  choice or ineligible user/connection discards the exchanged token.

Failures, as well as successful exchanges, are cached per app/scope/secret identity
within a batch. Manual Connect reuses an inactive Disconnect row. Failed manual
verification restores its previous state only if a newer Connect/Disconnect has
not changed the saved credential.

These locks are not database-wide: other users and unrelated queries keep running.
They require all participating workers to run this implementation. No migration,
unique constraint, or cleanup of historical duplicates is introduced. PostgreSQL
session-lock semantics require a direct or session-affine database connection;
transaction-pooling middleware is not validated. SQLite has process-local job
admission, not the cross-process PostgreSQL guarantee.

### Commands and observed results

Start a disposable PostgreSQL 16 database on localhost:55441 with database
`obo_locks`, username `postgres`, synthetic password `local-test-only`, and apply
existing migrations through the test fixture. From `backend`:

```bash
TESTING=true BOW_DATABASE_URL=sqlite:///db/app.db \
TEST_DATABASE_URL=postgresql://postgres:local-test-only@127.0.0.1:55441/obo_locks \
uv run pytest --db=external -q \
  tests/unit/test_obo_provisioning_concurrency.py \
  tests/unit/test_obo_recovery_per_user.py \
  tests/unit/test_obo_stored_login_recovery.py \
  tests/unit/test_obo_background_database.py

TESTING=true BOW_DATABASE_URL=sqlite:///db/app.db \
uv run pytest --db=sqlite -q \
  tests/e2e/test_connection_oauth_flow.py \
  tests/unit/test_obo_provisioning_concurrency.py
```

| Final verification | Observed |
|---|---|
| PostgreSQL concurrency + recovery + cross-loop regressions | **45 passed** |
| SQLite OAuth callback flows + concurrency contracts | **37 passed** |
| New coordination/recovery modules and new tests, Ruff | Passed |
| Diff whitespace | Passed |
| Live Entra + Power BI rerun on final implementation | Both users recovered; DAX HTTP 200; invalid tokens denied |

The 15 concurrency cases cover both roles, Disconnect/manual Connect/membership
removal/connection deactivation during paused HTTP, competing recovery/login jobs,
other-user progress, cancellation/transport failure cleanup, failed-exchange
cache, and failed manual verification racing a later user choice. The PostgreSQL
cleanup test reads the server's actual advisory lock and attempts to acquire it
from a separate physical connection; it also verifies no advisory locks remain.

Live rerun: Demo1 recovered 63 tables/4 workspaces; Demo2 forced real refresh,
renewed expiry and recovered 56 tables/2 workspaces; both constant read-only DAX
queries succeeded. The same password-grant and SSE-preparation limitations from
the live section above apply. Disposable databases and live tokens were removed.

Development failures were caught and corrected: the first lock helper selected
SQLAlchemy's synchronous `.engine` facade, causing MissingGreenlet; it now keeps
an AsyncEngine or unwraps only an AsyncConnection. An intermediate suite saw
mixed module versions while edits were in progress (7 TypeError recovery failures),
and the old cross-loop fixture lacked membership (2 failures under the new final
eligibility check). Added the missing member fixture and ran a fresh, consistent
final process: all 45 passed. These intermediate failures are not counted as passes.
The full repository suite, popup/MFA flow and production deployment remain untested.

### Broad lint attribution

A broader `ruff check --select F` reports 17 findings unchanged from base `47d86e69a`; comparing `(code, message)` per file introduced none. They are outside this change. New modules/tests pass their configured Ruff checks.

| Finding at base | Classification / evidence |
|---|---|
| `backend/app/routes/connection.py:24` — F401 `app.models.membership.Membership` imported but unused | Existing lint; last touched `75850f14b`, already present at `47d86e69a` |
| `backend/app/routes/connection.py:1039` — F841 Local variable `connection` is assigned to but never used | Existing lint; last touched `1aad2dce7`, already present at `47d86e69a` |
| `backend/app/routes/connection.py:1346` — F811 Redefinition of unused `Membership` from line 24: `Membership` redefined here | Existing lint; last touched `e10b17fe5`, already present at `47d86e69a` |
| `backend/app/services/connection_oauth_service.py:14` — F401 `urllib.parse.urlencode` imported but unused | Existing lint; last touched `4526c9d52`, already present at `47d86e69a` |
| `backend/app/services/connection_oauth_service.py:18` — F401 `sqlalchemy.update` imported but unused | Existing lint; last touched `aee9f7eca`, already present at `47d86e69a` |
| `backend/app/services/connection_service.py:5` — F401 `importlib` imported but unused | Existing lint; last touched `27b3fb9f0`, already present at `47d86e69a` |
| `backend/app/services/connection_service.py:10` — F401 `uuid.UUID` imported but unused | Existing lint; last touched `27b3fb9f0`, already present at `47d86e69a` |
| `backend/app/services/connection_service.py:11` — F401 `uuid` imported but unused | Existing lint; last touched `27b3fb9f0`, already present at `47d86e69a` |
| `backend/app/services/connection_service.py:28` — F401 `app.models.user_connection_overlay.UserConnectionTable` imported but unused | Existing lint; last touched `27b3fb9f0`, already present at `47d86e69a` |
| `backend/app/services/connection_service.py:28` — F401 `app.models.user_connection_overlay.UserConnectionColumn` imported but unused | Existing lint; last touched `27b3fb9f0`, already present at `47d86e69a` |
| `backend/app/services/connection_service.py:32` — F401 `app.schemas.data_source_registry.list_available_data_sources` imported but unused | Existing lint; last touched `7a242196f`, already present at `47d86e69a` |
| `backend/app/services/connection_service.py:1285` — F811 Redefinition of unused `UserConnectionTable` from line 28: `UserConnectionTable` redefined here | Existing lint; last touched `86589db63`, already present at `47d86e69a` |
| `backend/app/services/connection_service.py:1328` — F811 Redefinition of unused `UserConnectionTable` from line 28: `UserConnectionTable` redefined here | Existing lint; last touched `ea63c4277`, already present at `47d86e69a` |
| `backend/app/services/connection_service.py:1525` — F541 f-string without any placeholders | Existing lint; last touched `888476432`, already present at `47d86e69a` |
| `backend/app/services/connection_service.py:1610` — F541 f-string without any placeholders | Existing lint; last touched `888476432`, already present at `47d86e69a` |
| `backend/app/services/connection_service.py:1784` — F541 f-string without any placeholders | Existing lint; last touched `888476432`, already present at `47d86e69a` |
| `backend/app/services/connection_service.py:2309` — F541 f-string without any placeholders | Existing lint; last touched `315e713bb`, already present at `47d86e69a` |
