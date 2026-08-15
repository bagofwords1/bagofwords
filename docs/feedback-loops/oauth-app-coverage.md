# Feedback Loop — OAuth access to the full BOW data-app surface

This loop validates the claim that an external application can authenticate a
BOW user with OAuth 2.1, create and use reports through the main API, and stream
agent/tool work without allowing app and MCP credentials to cross surfaces.

## Root cause (validated)

The authorization server was reusable but its reach stopped at MCP:

- `backend/app/routes/oauth_server.py:31` advertised only `mcp`;
- `backend/app/core/auth.py:979` authenticated JWTs and API keys but had no
  OAuth-token branch, and the generic `bow_` API-key prefix also matched
  `bow_oauth_`;
- `backend/app/dependencies.py:99` resolved organizations from a caller-provided
  header or API key, with no OAuth token organization binding;
- `backend/app/routes/mcp.py:88` was the only surface validating OAuth tokens.

The existing client, authorization-code, access-token, and refresh-token models
already carried scope and organization data. The missing behavior was therefore
central authentication and tenant resolution, not a new OAuth implementation.

## Loop A — deterministic reproduction (no external services)

From a fresh sandbox:

```bash
cd backend
uv sync --frozen --extra dev
TESTING=true uv run pytest -q \
  tests/e2e/test_oauth_app.py \
  tests/e2e/test_oauth_mcp.py \
  tests/e2e/test_oauth_mcp_multiorg.py \
  --db=sqlite
```

The app-coverage tests use real FastAPI routes, migrations, SQLite, client
registration, PKCE, authorization-code exchange, report APIs, membership APIs,
and refresh rotation. No OAuth provider or LLM is required.

Before the fix, the new app-coverage suite produced the observed result:

```text
5 failed, 2 passed
```

The failures were the expected ones: `app` was not advertised or accepted and
main API authentication could not resolve an OAuth user/organization.

## The fix

- `backend/app/services/oauth_server_service.py:33-69` adds the `app` lifetime
  policy while preserving the 365-day MCP lifetime.
- `backend/app/services/oauth_server_service.py:618-683` returns a complete,
  organization-pinned token context and reasserts client, user, organization,
  membership, expiry, and requested scope on every request.
- `backend/app/services/oauth_server_service.py:498-504` and `:576-582` repeat
  that live-subject check at delayed code exchange and refresh time.
- `backend/app/core/auth.py:996-1017` recognizes `bow_oauth_` before the generic
  API-key path and requires `app` for the main API.
- `backend/app/dependencies.py:108-129` pins organization resolution to the
  token row before considering `X-Organization-Id`.
- `backend/app/routes/mcp.py:88-95` explicitly requires `mcp`.
- `backend/app/routes/oauth_server.py:31-81` publishes separate app/MCP
  protected-resource metadata.
- `backend/app/services/oauth_server_service.py:173-198` revokes pending grants
  and existing access/refresh credentials when an administrator changes a
  client's access surfaces.
- `frontend/pages/authorize.vue:111-176` validates public client metadata,
  renders per-scope consent, and auto-approves trusted clients after sign-in.
- `frontend/components/OAuthClientsModal.vue` provides a compact OAuth app
  list with scopes, trust, activity, and token count at a glance. Registration,
  editing, credentials, and endpoint details live in focused modals; rotate and
  delete remain in each row's action menu.
- `backend/alembic/versions/oauthapp01_app_scope_and_client_trust.py` adds one
  zero-downtime `trusted` boolean column with a false server default.

## Verification flip

Re-running the same command after the fix:

```text
27 passed, 965 warnings in 49.73s
```

The warnings are pre-existing dependency and datetime deprecations. The 27
passing contracts cover discovery, PKCE, app access, both scope-crossing
rejections, organization pinning, deletion revocation, scope-edit revocation,
refresh rotation/reuse rejection, removed-membership rejection (including
refresh), activity stats, `manage_settings` enforcement, and the pre-existing
MCP and multi-organization flows.

Frontend production verification:

```bash
cd frontend
NUXT_IGNORE_LOCK=1 npm run build
```

The build completes successfully. Locale verification also confirms the OAuth
namespace has the same key shape in all ten catalogs, with Hebrew exercised as
the RTL reference.

## Loop B — live browser and real LLM confirmation

This optional loop uses the already configured local SSO and LLM providers.
Credentials stay outside the repository and are never printed into this doc.

```bash
# BOW frontend/backend already running locally
node examples/oauth-report-app/server.mjs
# open http://127.0.0.1:4173
```

Observed in a real browser:

| Flow | Result |
| --- | --- |
| Untrusted `app` client | Local sign-in returned to a scope-specific consent screen; approval returned to the demo |
| Denied untrusted request | Returned to the demo disconnected, with no code/token and a clear “nothing was shared” result |
| Trusted `app mcp` client | Authentication returned to the demo without rendering consent actions |
| Local domain signup | A new allowed-domain account joined with the configured member role and completed OAuth |
| Entra existing user | Microsoft sign-in preserved the authorization request and returned to the demo |
| Entra pending invite | First login materialized the pending membership and completed OAuth |
| Report + completion | Demo created its own report and rendered the SSE stream |
| `clarify` | Three choices rendered; the selected response was persisted and execution resumed |
| `create_data` | Metric, line-chart, and bar-chart datasets completed successfully |
| `create_artifact` | A one-page executive brief completed and passed render validation |

Evidence lives under
`media/pr/claude-bow-oauth-app-coverage-pa7nnb/`, including:

- `oauth-apps-minimal.png` — compact OAuth application list;
- `oauth-app-create-modal.png` / `oauth-app-edit-modal.png` — focused app forms;
- `oauth-app-management-flow.gif` — list-to-modal management flow;
- `oauth-consent-app-scope.png` — untrusted `app` consent;
- `oauth-apps-minimal-hebrew.png` — compact Hebrew RTL layout;
- `entra-domain-signup-policy.png` — enabled allowed-domain signup policy;
- `third-party-app-clarify.png` — interactive streamed clarification;
- `third-party-app-streaming-tools.png` — streamed data/artifact tool execution.

## What this proves / regression notes

The deterministic loop proves the security boundaries without external
availability. The live loop separately proves browser navigation, both login
families, invite/domain provisioning, streaming, and configured LLM tool work.
The implementation remains backward compatible for existing MCP clients and
tokens; `mcp` remains the default scope when older clients omit `scope`.
