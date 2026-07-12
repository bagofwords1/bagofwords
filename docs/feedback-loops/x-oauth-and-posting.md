# X OAuth fix + posting via Custom API

## Problem

Connecting X via per-user OAuth (`oauth_app`) never completed: X consent
succeeded but the callback failed token exchange with
`401 unauthorized_client — Missing valid authorization header`, so no per-user
credential was stored and the UI kept showing "Sign in". Two root causes plus
follow-on UX/feature gaps.

## Root causes

1. **Wrong refresh-token scope spelling.** The X preset sent `offline_access`
   (underscore). X spells it `offline.access` (dot); the underscore makes X drop
   the refresh token.
2. **Wrong client-auth method.** X is a confidential client and requires HTTP
   Basic auth on the token request. The backend sent `client_secret` in the form
   body → 401.
3. **Secret wiped on edit.** Editing an OAuth connection without re-entering the
   (write-only) secret replaced the whole credential blob, dropping
   `client_secret`. The next token exchange then failed with
   `client_secret_basic requires a client_secret`.

## Fixes

- `McpAuthDefaults` / `MCPOAuthAppCredentials` gained
  `token_endpoint_auth_method` (`client_secret_basic` | `client_secret_post` |
  `none`), propagated through the catalog and connect form.
- X preset: `offline.access`, `client_secret_basic`, authorize URL on `x.com`.
- `exchange_code_for_tokens` / `refresh_access_token` branch on the method —
  Basic auth keeps id/secret out of the body; post keeps the body form; none is
  the public client. `get_oauth_params` falls back to Basic for `api.x.com`.
- `update_connection` preserves write-only secrets (`client_secret` / `token` /
  `api_key`) when an edit omits them.
- OAuth callbacks redirect to `/agents` and surface the provider's real error.
- The "Connector" badge in the agents tree is a passive label, not a clickable
  key-button.

## X posting via Custom API

X's hosted MCP server exposes read/search tools only — no "create post". Posting
is done through the `custom_api` connector calling `POST /2/tweets` with the
user's OAuth token:

- `custom_api` gained an `oauth_app` auth variant; `CustomApiClient` accepts a
  per-user `access_token` and sends it as `Authorization: Bearer`.
- `get_oauth_params` handles `custom_api` (same OAuth-app shape as `mcp`), so the
  existing authorize/callback/refresh flow works unchanged.
- Write endpoints (POST/PUT/PATCH/DELETE, or `confirm: true`) default to policy
  `ask` at tool discovery → confirmation required before running.
- The **X Write** preset (`GET /connectors/custom-api-presets`) pre-fills
  `base_url`, `create_post` / `delete_post` endpoints, and the X OAuth defaults;
  the admin only supplies client id/secret, and each user signs in themselves.
- Link the Custom API connection to the same agent as the read-only X MCP
  connection so both tool sets coexist.

## Verifying

- Unit: `tests/unit/test_connection_oauth.py`, `test_mcp_presets.py`,
  `test_custom_api_oauth.py` (scopes, Basic-auth header, secret absent from body,
  write-endpoint policy, preset shape).
- End-to-end (manual, needs a live X app + interactive consent): sign in with X,
  confirm the hosted MCP tools load and `create_post` appears from Custom API,
  publish + delete a test post, and confirm refresh works after the access token
  expires.
