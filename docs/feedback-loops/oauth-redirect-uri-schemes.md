# Feedback Loop: "OAuth client updates accept `javascript:` redirect URIs" (pentest WP8)

Pentest findings **828511578** (medium) and **829383570** (low, same class).
An org admin could save an OAuth client whose `redirect_uris` contained
`javascript:...`. When a user approved that client on `/authorize`, the frontend
ran `window.location.assign(redirect_url)`, and the script executed in the BOW
origin (stored XSS). A connection's OAuth `authorization_url` (SAP Datasphere
config, an MCP OAuth-app `authorize_url`, or an MCP server's discovered
`authorization_endpoint`) had the same flaw: the frontend navigates there when
the user clicks **Sign in**.

## Root cause (validated)

- `backend/app/routes/oauth_server.py` `_validate_redirect_uris` only checked
  that the value was a non-empty list of strings. It ran on create and PATCH,
  and nothing checked the scheme.
- `backend/app/services/oauth_server_service.py` `validate_redirect_uri` was a
  plain membership test, so a stored bad URI passed `GET /authorize`,
  `GET /clients/{id}/info` and `POST /authorize`.
- `frontend/pages/authorize.vue` `approve()` and `deny()` navigated to the
  server's URL with no check.
- `backend/app/routes/connection_oauth.py` built `authorization_url` from
  `get_oauth_params(...)["authorize_url"]` with no check. That value comes from
  the connection config, MCP credentials or DCR discovery
  (`backend/app/services/mcp_dcr_service.py`). Four frontend sites then set
  `window.location.href` to it.

## Policy (decision)

- **Redirect URIs:** an allowlist. `https://` with a host is allowed, and
  `http://` is allowed in two cases: on loopback (`localhost`, `127.0.0.1`,
  `[::1]`), or when the URI's origin exactly matches the app's own configured
  `bow_config.base_url` origin (option (a)). The match covers scheme, host and
  port, so an on-prem install at `http://bow.corp:3000` keeps the pre-filled
  `${origin}/oauth/callback`. Lookalikes are rejected: `bow.corp.evil.com`,
  userinfo such as `bow.corp@evil.com`, a different port. If `base_url` is
  unset or still the `0.0.0.0` placeholder, there is no own origin and only
  loopback http is allowed. Everything else is rejected, including custom app
  schemes (`cursor://`, `vscode://`), other http hosts, relative URIs, and URIs
  containing whitespace, control or zero-width characters. These are the
  product decisions. Custom schemes are not supported for now.
- **Authorize URLs:** only absolute `http(s)` URLs with a host are accepted.
- **When it is enforced:** on create and update (OAuth clients and
  connections), during MCP discovery, and again when the URL is used:
  `GET`/`POST /api/oauth/authorize`, client info, and the connection authorize
  route. The frontend also checks before every navigation
  (`frontend/utils/safeUrl.ts`).

## Loop A: deterministic reproduction

```bash
cd backend
ENVIRONMENT=production TESTING=true .venv/bin/python -m pytest -q --db=sqlite --disable-warnings \
  tests/e2e/test_oauth_redirect_uri_schemes.py tests/unit/test_navigation_url_safety.py
```

With the fix reverted (`git checkout <base> -- backend/app`), the e2e file
gives **30 failed / 4 passed**: every rejection test fails and every
regression test passes. The unit file fails to import because the helpers
don't exist yet. With the fix, both files give **91 passed**. That count
includes the own-origin tests. Three of those fail on the earlier
loopback-only commit: own-origin save plus the full auth-code flow (2), and
"stops authorizing when base_url changes" (1). The 8 lookalike tests pass on
both commits, as regression guards against loosening the match.

### Loop A2: backend with an http, non-loopback base URL (option (a))

A copy of `bow-config.yaml` with `base_url: http://bow.corp:3000`, started via
`BOW_CONFIG_PATH=... uvicorn main:app --port 8811` and seeded with
`seed_org.py`, then probed through the API:

| Redirect URI | Loopback-only commit | Option (a) |
|---|---|---|
| `http://bow.corp:3000/oauth/callback` (modal default) | 400 | 200, then approve 200, then token 200 (`bow_oauth_` token) |
| `http://bow.corp.evil.com:3000/...` | 400 | 400 |
| `http://bow.corp@evil.com:3000/...` | 400 | 400 |
| `http://bow.corp:3001/...` | 400 | 400 |
| `http://intranet.corp/...` | 400 | 400 |
| `javascript:alert(1)` | 400 | 400 |
| `https://app.example.com/cb` | 200 | 200 |

Frontend guard (`safeUrl.isSafeRedirectUri`) in Chromium on a page served at
`http://bow.corp:3000`:

- **Allowed:** `http://bow.corp:3000/oauth/callback?code=x`,
  `https://app.example.com/cb`, `http://localhost:6274/cb`.
- **Blocked:** `bow.corp.evil.com:3000`, `bow.corp@evil.com:3000`,
  `bow.corp:3001`, `intranet.corp`, `javascript:`.

## Loop B: live stack over HTTP

The stack ran on a private port, because a sibling session held :8000:
`uvicorn main:app --port 8811`, seeded with `tools/agent/seed_org.py`. The
probe script posts each case through the real API.

| Request | Before | After |
|---|---|---|
| `POST /api/oauth/clients` with `javascript:alert(document.domain)` | 200 (stored) | 400 |
| `POST /api/oauth/clients` with `" JaVaScRiPt:alert(1)"` / `"java\tscript:..."` / `data:` | 200 | 400 |
| `POST /api/oauth/clients` with `cursor://...` / `http://bow.example.com/cb` | 200 | 400 (policy) |
| `POST /api/oauth/clients` with `https://app.example.com/cb` / `http://localhost:6274/...` | 200 | 200 |
| `PATCH` a client to `javascript:alert(1)` | 200 | 400 |
| `GET /api/oauth/authorize` with a stored `javascript:` URI | 302 to consent | 400 |
| `POST /api/oauth/authorize` with a stored `javascript:` URI | 200 `{"redirect_url":"javascript:alert(document.domain)?code=..."}` | 400 |
| `POST /api/connections` (sap_datasphere) with `authorization_url=javascript:...` | 200 | 400 |
| `GET /connections/{id}/oauth/authorize` | 200 `{"authorization_url":"javascript:alert(document.cookie)//?..."}` | 400 for a legacy row |
| The same with `https://auth.example.com/oauth/authorize` | 200 | 200 (unchanged) |

## Loop C: real browser (Chromium via Playwright)

Booting the Nuxt dev server repeatedly got OOM-killed (exit 137) because the
sandbox was shared with other agents. Instead, a harness runs the exact
navigation step from `authorize.vue` `approve()` in real Chromium, using the
`redirect_url` values that the **base** backend actually returned. It runs the
step once unguarded (base) and once through the compiled
`frontend/utils/safeUrl.ts` (fix).

![consent redirect before/after](../../media/pr/wp8-oauth-redirect/consent-redirect-before-after.png)

- **Before:** `javascript:alert(document.domain)//?code=...` fires
  `alert("bow.example.test")` in the app origin. `" JaVaScRiPt:alert(1)"` also
  fires.
- **After:** both are blocked, and the page shows its existing
  "authorization failed" state. The https callback still navigates with its
  `code`.

## Regression notes

- Related suites (oauth*, mcp*, connection oauth, entra exchange, session
  revocation, mcp_dcr) ran on the merged branch. There are 8 failures, and all
  of them were already failing on the base branch with the fix reverted:
  `test_oauth_connection_recovery.py::test_existing_custom_app_prompt_recovers_only_usable_stored_login[*]`
  (6) and `test_connection_oauth_flow.py::TestUserOverlaySync::*` (2). The
  cause is `ImportError: libodbc.so.2` (no ODBC driver in the sandbox), which
  is environmental.
- Every redirect URI in the repo's own code and tests is https or loopback
  http: `DEFAULT_REDIRECT_URIS` and the test fixtures. Nothing in-repo used a
  custom scheme.
- The OAuth Clients modal pre-fills `${window.location.origin}/oauth/callback`
  (`frontend/components/OAuthClientsModal.vue:453`). On an http on-prem
  deployment this keeps working, provided `base_url` is set to that origin
  (option (a); see Loop A2). If an http deployment leaves `base_url` unset or
  at the placeholder, the default is rejected and the admin must set
  `base_url` or use https.

## Follow-up: no base_url, fall back to the request's own origin

Problem: an on-prem plain-http install that never set `base_url` (served at,
e.g., `http://bow.corp:3000`) could not save the OAuth Clients modal's
pre-filled `${window.location.origin}/oauth/callback`. With `base_url` unset
or at the `0.0.0.0` placeholder only loopback http was allowed.

Fix: when `base_url` is unset/placeholder, the app's own http origin is the
origin the browser used to reach the server (`request_http_origin` in
`backend/app/core/url_safety.py`). It is checked at save time (create/update
client, the admin's request) and at use time (`GET`/`POST /api/oauth/authorize`,
`GET /api/oauth/clients/{id}/info`, the authorize request). A configured
`base_url` is still used exclusively. Matching stays exact (scheme, lowercased
host, port with defaults); userinfo and lookalikes are still refused.

Proxy trust is reused, not added: the scheme is `request.url.scheme`, which
uvicorn's `--proxy-headers` rewrites from `X-Forwarded-Proto` only for peers in
`--forwarded-allow-ips` (`start.sh`, default `127.0.0.1`). The host is the
`Host` header. `X-Forwarded-Host` is never read, because nothing in the server
is configured to trust it.

### Loop D: live backend, no base_url, reached as `http://bow.corp:8823`

`uvicorn main:app --port 8823 --proxy-headers --forwarded-allow-ips 127.0.0.2`
on a scratch sqlite DB, `base_url` at the `http://0.0.0.0:3000` placeholder,
seeded with `tools/agent/seed_org.py`. Requests carry `Host: bow.corp:8823`.

| Request | Before | After |
|---|---|---|
| `POST /api/oauth/clients` `http://bow.corp:8823/oauth/callback` | 400 | 200 |
| `GET /api/oauth/authorize` with that URI (Host `bow.corp:8823`) | n/a | 302 to consent |
| `GET /api/oauth/authorize` with that URI (Host `other.corp`) | n/a | 400 |
| `POST /api/oauth/authorize` then `POST /api/oauth/token` | n/a | 200, then 200 with a `bow_oauth_` token |
| `http://other.corp/cb`, `http://bow.corp.evil.com:8823/cb`, `http://bow.corp@evil.com:8823/cb`, `http://bow.corp:8823@evil.com/cb`, `http://bow.corp:8824/cb`, `javascript:alert(1)` | 400 | 400 |
| Own-origin URI with `Host: 127.0.0.1:8823` + `X-Forwarded-Host: bow.corp:8823` | 400 | 400 |

Tests: `tests/e2e/test_oauth_redirect_uri_schemes.py` (`unset_base_url`,
`configured_base_url`, `request_http_origin` cases). 11 of them fail on the base
branch and all pass with the fix. The negative cases pass on both, because they
guard against widening.
