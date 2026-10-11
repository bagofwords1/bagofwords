# Feedback Loop — OAuth bearer tokens outlive a password change; OAuth tokens can approve consent

Two findings, one root theme: delegated OAuth credentials (`bow_oauth_…`) were
treated like a login session in places they must not be.

- **#14** "Changing a password doesn't revoke OAuth bearer tokens already issued."
- **#17** "`/api/oauth/authorize` accepts an OAuth bearer token issued to one
  client and issues a code for a different client."

## Root cause (validated)

- **#14** — `on_after_reset_password` / `on_after_update` only called
  `bump_session_epoch`. Login JWTs check the epoch, but OAuth access tokens are
  validated by hash in `OAuthServerService.validate_access_token_context`
  (`backend/app/services/oauth_server_service.py:647`) and never look at it. The
  admin force-sign-out path already used `revoke_user_sessions`
  (`backend/app/core/session_revocation.py`), which also deletes OAuth codes and
  tokens. The password paths did not.
- **#17** — `POST /api/oauth/authorize` (`backend/app/routes/oauth_server.py:158`)
  authenticated with `current_user`, which accepts `app`-scoped OAuth tokens and
  API keys. A delegated credential could approve consent for any client in the
  user's org, including one with broader scopes (`mcp`), and start a fresh
  refresh chain without the user seeing a consent screen.

## Loop A — deterministic reproduction (no external services)

```bash
cd backend && uv sync --frozen --extra dev
export BOW_DATABASE_URL="sqlite:///db/app.db" TESTING=true; mkdir -p db
uv run pytest -m e2e --db=sqlite tests/e2e/test_oauth_consent_requires_session.py \
  tests/e2e/test_session_revocation.py -k "consent or oauth_access_and"
```

Before the fix (`git stash -- app`):

```
FAILED test_oauth_access_token_cannot_approve_consent[other_client]
FAILED test_oauth_access_token_cannot_approve_consent[same_client]
FAILED test_api_key_cannot_approve_consent
FAILED test_password_change_revokes_oauth_access_and_refresh_tokens[Changed1234!]
FAILED test_password_change_revokes_oauth_access_and_refresh_tokens[Other-Pass9876]
5 failed, 1 passed
```

After: `6 passed`.

## Loop B — live backend (real uvicorn + alembic-migrated SQLite, no secrets)

```bash
cd backend && export BOW_DATABASE_URL="sqlite:///db/live.db"
uv run alembic upgrade head && uv run uvicorn main:app --port 8765 &
uv run python ../docs/feedback-loops/2026-10-11-oauth-session-revocation-live.py
```

| Step | Before | After |
|---|---|---|
| consent via login JWT | 200 | 200 |
| client A's OAuth token on app API | 200 | 200 |
| **#17** consent for client B via A's OAuth token | **200** (code issued) | **401** |
| password change | 200 | 200 |
| **#14** old OAuth access token | **200** | **401** |
| **#14** old refresh token | **200** (new pair) | **400 invalid_grant** |
| fresh login with new password | 200 | 200 |

## The fix

- `backend/app/core/auth.py:704` — password reset and self-service password
  change call the new `revoke_all_user_sessions`, which runs the existing
  `revoke_user_sessions`: it bumps the epoch and deletes outstanding OAuth codes
  and access/refresh tokens. Profile edits that don't touch the password still
  leave sessions alone.
- `backend/app/core/auth.py:1214` — new `current_session_user` dependency (login
  JWT only). `POST /api/oauth/authorize` uses it, so OAuth bearer tokens and API
  keys get 401. The consent page (`frontend/pages/authorize.vue`) already sends the
  login JWT, so it is unaffected.

## What this proves / regression notes

- OAuth, session-revocation, Entra and auth e2e suites: 180 passed.
  `test_oauth_connection_recovery.py` fails 6/6 with the change stashed too
  (`libodbc.so.2` missing in the sandbox), so it is unrelated.
- Logout still only bumps the epoch, by design: logging out of the web app should
  not disconnect authorized MCP and OAuth apps.
