# Feedback Loop — "the URL is checked, then the HTTP client resolves it again" (SSRF package A)

Several outbound HTTP paths validated a URL's host (`app.core.url_safety`) and
then handed the URL to an HTTP client that did its **own** DNS lookup. A DNS
server that answers "fine" for the check and `169.254.169.254` for the connect
(DNS rebinding) reaches the cloud metadata service. Some paths had no check at
all (custom API / MCP URL probing, OAuth discovery endpoints, Google service
account `token_uri`). This loop shows each path reaching a fake metadata
service on the base branch, and refused after the fix, while internal
(private-network) destinations keep working by default.

Policy (unchanged): metadata + link-local are always refused; private / LAN /
loopback only when `security.block_private_outbound: true`.

## Root cause (validated)

- `backend/app/core/url_safety.py:19-21` documents it: "the address is checked
  at validation time and the HTTP client resolves again when it connects".
- web_fetch / sandbox `http`: `web_fetch.py` and `ai/http/safe_client.py`
  checked `host_is_public(...)` then `cf_requests.AsyncSession.get(url)`
  (libcurl resolves again).
- LLM `base_url`: `llm_service.py:_check_provider_url` + `llm.py:325-327`
  check, then the OpenAI SDK's httpx client resolves again.
- Audit streams: `ee/audit/streams/service.py:78-80` checks at **save**;
  `destinations/http.py:_post` sends with a plain `httpx.AsyncClient`.
- custom API / MCP: `custom_api_client.py` (`httpx.Client`) and
  `mcp_client.py` (`sse_client` / `streamablehttp_client`) had no check.
- MCP OAuth: `mcp_dcr_service.discover_mcp_oauth` fetched whatever the server
  and its metadata documents named; `register_client` POSTed to the advertised
  `registration_endpoint`; token exchange used the advertised `token_endpoint`.
- Google Chat: `google_chat_listener_service.py`, `google_chat_adapter.py`,
  `external_platform_service.py` let google-auth POST a signed assertion to
  whatever `token_uri` the uploaded key contained.

## The fix

`backend/app/core/egress.py` — resolve once, validate **every** address,
connect to a validated address while keeping the hostname for `Host`, SNI and
certificate verification. Applied in the transport, so it runs for every
request and every redirect hop:

- httpx: `PinnedTransport` / `AsyncPinnedTransport`, `pinned_client()`,
  `pinned_async_client()`, `mcp_client_factory()`.
- curl_cffi: `curl_pinned_request()` → canonical URL + `CURLOPT_RESOLVE` pin;
  `safe_client.pinned_curl_get()` per redirect hop.
- `check_url()` / `check_host()` save-time guards; `check_google_token_uri()`.

## Loop A — deterministic (no network, no services)

```bash
cd backend
ENVIRONMENT=production TESTING=true .venv/bin/python -m pytest -q --db=sqlite \
  --disable-warnings tests/unit/test_egress.py tests/unit/test_egress_callers.py
```

DNS is faked at `egress._getaddrinfo` (`tests/utils/egress_fakes.FakeDNS`, a
rebinding resolver: answer 1 fine, answer 2 metadata) and servers are real
sockets on 127.0.0.1 addressed through `*.invalid` names only the fake can
resolve — reaching the server proves the pinned address was used.

| | result |
|---|---|
| fix | `111 passed` (85 egress + 26 callers) |
| base + `egress.py` copied in (callers unpatched) | `test_egress_callers.py`: **18 failed, 8 passed** — web_fetch rebinding/redirect, custom API, MCP discovery, Google Chat, LLM gateway, audit stream, sandbox http |

## Loop B — live API with a fake metadata service and a rebinding DNS

`tools/agent/ssrf_egress_sandbox.py serve` runs the real FastAPI app in one
process with: an IMDS stand-in (records every hit), a `socket.connect` shim
routing `169.254.169.254` to it, a `getaddrinfo` shim (`imds.ssrf.test` →
metadata; `rebind<N>-x.ssrf.test` → 127.0.0.1 for the first N lookups, then
metadata; `internal.ssrf.test` → 127.0.0.1) and fake internal services (LLM
gateway, MCP + OAuth AS, SIEM, REST API). `drive` calls the real endpoints.

```bash
cd backend
TESTING=true ENVIRONMENT=production TEST_DATABASE_URL=sqlite:///db/ssrf.db .venv/bin/alembic upgrade head
TESTING=true ENVIRONMENT=production TEST_DATABASE_URL=sqlite:///db/ssrf.db \
  .venv/bin/python ../tools/agent/ssrf_egress_sandbox.py serve --port 8731 [--block-private] &
.venv/bin/python ../tools/agent/ssrf_egress_sandbox.py drive --port 8731 --expect fixed|vulnerable [--block-private]
```

### Before (base branch, `--expect vulnerable`) — metadata reached

```
PASS | LLM test_connection, base_url rebinds after 2 checks      | VULNERABLE (metadata hit)
PASS | custom_api test, base_url = metadata name                 | VULNERABLE (metadata hit) | success=True Connected to API at http://imds.ssrf.test/latest/meta-data
PASS | custom_api test, base_url rebinds after 1 check           | n/a on base (no check at all; previous row)
PASS | MCP OAuth authorize, server_url = metadata name           | VULNERABLE (metadata hit)
PASS | MCP OAuth authorize, AS advertises metadata registration  | VULNERABLE (metadata hit)
PASS | MCP OAuth authorize, server rebinds after 2 lookups       | VULNERABLE (metadata hit)
PASS | audit stream test, url rebinds after 1 check              | VULNERABLE (metadata hit) | {'ok': True, 'status': 200}
PASS | Google Chat create, token_uri = metadata                  | VULNERABLE (metadata hit) | signed assertion POSTed to the IMDS stand-in
```

### After (this branch, flag off, `--expect fixed`) — all 13 PASS, rc=0

```
PASS | LLM test_connection, base_url rebinds after 2 checks      | blocked | Connection error.
PASS | custom_api test, base_url = metadata name                 | blocked | base_url: Refusing to connect to 'imds.ssrf.test': link-local or cloud metadata address.
PASS | custom_api test, base_url rebinds after 1 check           | blocked | Refusing to connect to 'rebind1-api….ssrf.test': link-local or cloud metadata address
PASS | MCP OAuth authorize, server_url = metadata name           | blocked | HTTP 400 server_url: Refusing to connect …
PASS | MCP OAuth authorize, AS advertises metadata registration  | blocked | HTTP 400 OAuth metadata token_endpoint refused …
PASS | MCP OAuth authorize, server rebinds after 2 lookups       | blocked | HTTP 400 Could not discover OAuth metadata …
PASS | audit stream test, url rebinds after 1 check              | blocked | destination refused: Refusing to connect …
PASS | Google Chat create, token_uri = metadata                  | blocked | HTTP 400 service account token_uri must be https://oauth2.googleapis.com/token
PASS | LLM test_connection, internal OpenAI-compatible gateway   | reached internal service | success=True Successfully connected to LLM
PASS | custom_api test, internal API                             | reached internal service | success=True
PASS | MCP OAuth authorize, internal MCP + authorization server  | reached internal service | authorization_url returned (DCR client registered)
PASS | audit stream test, internal SIEM                          | reached internal service | ok
PASS | Google Chat create, genuine token_uri                     | passes validation; Google answered invalid_grant (fake key)
```

### After, `--block-private` — every internal destination refused, rc=0

```
PASS | LLM test_connection, internal OpenAI-compatible gateway   | refused | URL must point at a public address.
PASS | custom_api test, internal API                             | refused | base_url: … non-public address.
PASS | MCP OAuth authorize, internal MCP + authorization server  | refused | server_url: … non-public address.
PASS | audit stream test, internal SIEM                          | refused | url: URL must point at a public address.
```

The base run also passes every "internal" row, so the fix changes nothing for
legitimate private destinations with the flag off.

## What this proves / limits

- Pinned connect + per-hop re-check closes rebinding on every HTTP path in
  scope; TLS still verifies the hostname (`test_tls_certificate_is_verified_against_the_hostname`:
  same IP, wrong name → certificate error).
- web_fetch / sandbox `http` use libcurl, which the Loop B Python shims
  cannot reach; they are covered by Loop A (`CURLOPT_RESOLVE` pin, real local
  server reached only through the pin).
- When an environment proxy (`HTTPS_PROXY`) applies, the proxy resolves the
  name: the hostname is still checked but the connection cannot be pinned.
- Out of scope for package A (covered by package B below): syslog / S3 audit
  destinations, git remotes, browser tool / PDF render, Azure SDK &
  Anthropic-on-Foundry LLM clients. Still out of scope: kernel-level sandbox
  network limits.
- Pre-existing failures (also fail on base, sandbox env): `test_openai_tool_call_ids.py`
  (5), `test_sharepoint_onprem_client.py` / `e2e/test_sharepoint_onprem.py` /
  `test_connection_file_browse.py::test_contract_sharepoint_server`
  (`REQUESTS_CA_BUNDLE` set by the sandbox proxy).

---

# Package B — non-HTTP destinations, SDK clients and server-side Chromium

Same policy (`Policy.ADMIN`: metadata / link-local always refused; private /
LAN only with `security.block_private_outbound: true`), applied where package
A's pinned HTTP transport could not reach: database drivers, SMTP / IMAP,
syslog, botocore, git, the Azure SDKs, and headless Chromium.

## Root cause (validated)

| Path | Before | Evidence |
|---|---|---|
| Connection create / `PUT /api/connections/{id}` / `POST …/{id}/test` (+ overrides) / indexing / queries | Only MCP / custom-API URLs were checked (`_check_tool_provider_url`); every other connector's `host` / `*_url` went straight to the driver | `ConnectionService.construct_client`, `_resolve_client_by_type`, `DataSourceService.construct_client(s)`, `Connection.get_client` built clients with no destination check |
| SMTP send + SMTP/IMAP "Test" | `aiosmtplib.SMTP(hostname=…)` / `imaplib.IMAP4(host)` resolve and connect on their own | `email/sender.py`, `external_platform_service._test_email_credentials`, `email/mailbox_reader.py` |
| Syslog audit stream | `host` is a `text` field (no save-time check); `asyncio.open_connection(host, …)` at send | `ee/audit/streams/destinations/syslog.py` |
| S3 / GCS audit stream | `endpoint_url` checked at save only; botocore resolves at send | `ee/audit/streams/destinations/s3.py` |
| Git | Remote checked, then git resolves again; PR API calls to `custom_host` used a plain `httpx.AsyncClient` | `services/git_service.py` |
| Azure OpenAI / Foundry LLM | `check_admin_url` at construction, then the Azure / Anthropic / OpenAI SDK resolves again | `ai/llm/llm.py` azure branch, `azure_client.py`, `anthropic_client.py` |
| Thumbnail render | Model-authored page code rendered by Chromium with **no** request guard | `services/thumbnail_service.py` |
| PDF export | #1309's route guard allowed any public host and never sees redirect hops (Chromium follows a 3xx without re-entering the Playwright route handler) | `report_pdf_service._guard_requests`; reproduced: `<img src=/r>` → `302 → 127.0.0.2` reached |
| Artifact preview screenshot / agent browser tool | Same Chromium; preview had no guard; browser tool's allowlisted hosts were never checked for metadata and redirects escaped the route handler | `create_artifact._take_preview_screenshot`, `_browser_common` |

## The fix

- `app/core/egress.py` (additive): `connect_pinned_socket` / `open_pinned_socket`
  (resolve once, validate every address, connect to a validated one; caller
  keeps the hostname for TLS); `hosts_in` / `connection_hosts` /
  `check_connection_params` / `acheck_connection_params`.
- Connections: `check_connection_params(type, params)` right before every
  client is built (`construct_client`, `_resolve_client_by_type`, both
  `DataSourceService` factories, `Connection.get_client`, the per-user
  credential test) — on the *effective* config, i.e. after `/test` overrides.
- SMTP / IMAP: `app/services/email/pinned.py` — `PinnedSMTP` hands aiosmtplib
  a socket connected to the validated IP (`sock=`) and keeps `hostname`, which
  aiosmtplib uses as the TLS server name for implicit TLS and STARTTLS;
  `PinnedIMAP4(_SSL)` overrides imaplib's `_create_socket`.
- Syslog: save-time `check_host` on `host`; send connects through
  `open_pinned_socket` with `server_hostname=host`.
- S3: `endpoint_url` / `sts_endpoint_url` re-checked at send (`check_url`).
- Git: `GitService._git_env(url)` re-runs the remote check right before
  **each** network command and pins git's libcurl with `http.curloptResolve`
  (via `GIT_CONFIG_COUNT/KEY/VALUE`, git ≥ 2.31 env, ≥ 2.37 option); PR API
  calls use `pinned_async_client`.
- Azure / Foundry LLM: `AzureClient`, the Foundry OpenAI-compatible and
  Responses clients and Anthropic-on-Foundry all use the pinned transport.
- Chromium: `app/core/render_guard.py` — `RendererPolicy` (own render origin
  + `RENDER_ASSET_HOSTS`, which is **empty**: every library and font a page
  artifact uses is vendored and inlined by `artifact_libs`, and the in-app
  iframe CSP already forbids external scripts / styles / fonts / connect),
  `guard_page_requests` (route abort), and `RenderEgressProxy` — an in-process
  forward proxy Chromium is launched behind (`<-loopback>` removes the
  implicit loopback bypass) that decides on every connection, including each
  redirect hop, and connects to the validated address. Used by PDF export
  (page + document), thumbnails, the artifact preview screenshot and the
  agent browser tool (allowlisted hosts: admin tier; others: public tier; not
  used when the deployment routes the browser through an environment proxy).

## Loop A — deterministic

```bash
cd backend
for f in tests/unit/test_egress_package_b.py tests/unit/test_render_guard.py tests/e2e/test_connection_egress.py; do
  ENVIRONMENT=production TESTING=true .venv/bin/python -m pytest -q --db=sqlite --disable-warnings "$f"
done
```

`tests/utils/egress_fakes.NetTrap` plays the network at the socket boundary
(rebinding resolver + a router sending every connect to 169.254.169.254 to a
local metadata stand-in), so drivers / SDKs that resolve on their own are
covered too; `trap.hits` is the ground truth. Chromium tests use real local
servers on 127.0.0.1 / 127.0.0.2.

| | fix | base (package A branch) |
|---|---|---|
| `test_egress_package_b.py` | 55 passed | **48 failed**, 7 passed (git metadata refusal already existed; Azure internal endpoint reachable) |
| `test_connection_egress.py` (e2e, real routes) | 4 passed | **4 failed** |
| `test_render_guard.py` (real Chromium) | 9 passed | **7 failed**, 1 passed (normal PDF renders) |

## Loop B — live API (`tools/agent/ssrf_egress_sandbox.py --rows b`)

New fakes: Prometheus API, S3-compatible PUT, Azure-shaped LLM routes, git
repo over dumb HTTP, the app's own print origin (`BOW_RENDER_ORIGIN`, whose
`/print/doc` links a same-origin `/redir` → 302 → metadata stand-in), an SMTP
relay and a syslog collector; `flip-*.ssrf.test` names re-point to metadata on
demand; the stand-in counts TCP connections (SMTP / syslog / DB clients never
send an HTTP line). Chromium does its own DNS, so renderer rows point pages at
the stand-in's loopback address.

```bash
.venv/bin/python ../tools/agent/ssrf_egress_sandbox.py serve --port 8761 --imds-port 18361 --svc-port 18362 \
    --smtp-port 18363 --syslog-port 18364 [--block-private] &
.venv/bin/python ../tools/agent/ssrf_egress_sandbox.py drive --port 8761 --imds-port 18361 --svc-port 18362 \
    --smtp-port 18363 --syslog-port 18364 --rows b --expect fixed|vulnerable [--block-private] [--only ROW]
```

### Before (package A branch, each row driven alone, `--expect vulnerable`)

```
PASS | connection PUT, host -> metadata name                  | VULNERABLE (metadata hit)
PASS | connection test, override host -> 0xa9fea9fe           | VULNERABLE (metadata hit) | success=True Successfully connected to Prometheus
PASS | connection test, name re-pointed after save            | VULNERABLE (metadata hit) | success=True
PASS | mysql test-params, host = 169.254.169.254              | VULNERABLE (metadata hit)
PASS | SMTP settings test, smtp_host = metadata name          | VULNERABLE (metadata hit)
PASS | syslog stream test, host = metadata name               | VULNERABLE (metadata hit) | {'ok': True}
PASS | S3 stream test, endpoint rebinds after 1 check         | VULNERABLE (metadata hit)
PASS | Azure LLM test, endpoint rebinds after 2 checks        | VULNERABLE (metadata hit)
PASS | Azure Foundry Claude test, endpoint rebinds after 1 check | VULNERABLE (metadata hit)
PASS | thumbnail render, page fetches internal endpoint       | VULNERABLE (metadata hit)
PASS | document PDF, own-origin redirect -> metadata          | VULNERABLE (metadata hit) | PDF rendered
PASS | (internal Prometheus / SMTP relay / syslog / S3 / Azure, normal page PDF) | reached / rendered
PASS | git test, internal server (name only the pin resolves) | n/a on base (git cannot resolve it without the pin)
```

### After (this branch, flag off, `--rows all`) — 31/31 PASS, rc=0

```
PASS | connection PUT, host -> metadata name                  | blocked | Updated configuration is invalid: Refusing to connect to 'imds.ssrf.test' …
PASS | connection test, override host -> 0xa9fea9fe           | blocked | Refusing to connect to '0xa9fea9fe' …
PASS | connection test, name re-pointed after save            | blocked | Refusing to connect to 'flip-prom….ssrf.test' …
PASS | mysql test-params, host = 169.254.169.254              | blocked
PASS | SMTP settings test, smtp_host = metadata name          | blocked
PASS | syslog stream test, host = metadata name               | blocked | HTTP 400 host: Refusing to connect …
PASS | S3 stream test, endpoint rebinds after 1 check         | blocked | destination refused …
PASS | Azure LLM test, endpoint rebinds after 2 checks        | blocked
PASS | Azure Foundry Claude test, endpoint rebinds after 1 check | blocked
PASS | thumbnail render, page fetches internal endpoint       | blocked (thumbnail still written)
PASS | document PDF, own-origin redirect -> metadata          | blocked | PDF rendered (6682 bytes)
PASS | connection create, internal Prometheus                 | reached internal service
PASS | SMTP settings test, internal relay                     | reached internal service | success=True
PASS | syslog stream test, internal collector                 | reached internal service
PASS | S3 stream test, internal S3-compatible store           | reached internal service
PASS | Azure LLM test, internal endpoint                      | reached internal service | success=True
PASS | git test, internal server (name only the pin resolves) | reached internal service | success=True
PASS | PDF export, normal page artifact                       | rendered | PDF rendered (8313 bytes)
(+ the 13 package-A rows, unchanged: all PASS)
```

### After, `--block-private` — every internal destination refused, metadata never hit, PDFs still render (31/31 PASS)

```
PASS | connection create, internal Prometheus                 | refused | Refusing to connect to 'internal.ssrf.test': non-public address.
PASS | SMTP settings test, internal relay                     | refused
PASS | syslog stream test, internal collector                 | refused
PASS | S3 stream test, internal S3-compatible store           | refused
PASS | Azure LLM test, internal endpoint                      | refused
PASS | git test, internal server                              | refused
PASS | PDF export, normal page artifact                       | rendered   (own render origin is deployment config, not policed)
```

## Residual risk (package B)

- **Database / SDK drivers are checked at use, not pinned.** libpq, ODBC,
  pymysql, oracledb, vendor SDKs and botocore resolve the name themselves, so
  a DNS answer that flips between our check and the driver's own lookup
  (milliseconds) is not closed. The check runs on every client build (test,
  index, query), so a name re-pointed after save is refused at the next use.
  Pinning libpq via `hostaddr` was considered and rejected: pooled engines
  would keep a stale address across legitimate DNS fail-overs.
- **Connector hosts are found by key** (`host`, `hostname`, `server`,
  `server_hostname`, `*_url`, Salesforce `domain`). A destination hidden in an
  opaque `connection_string` / DSN is not parsed.
- **git over ssh** is checked before each command but not pinned (ssh does
  its own lookup); `http.curloptResolve` is ignored by git < 2.37 (check still
  applies). git still follows an initial-request HTTP redirect from an
  allowed server.
- **Environment proxy**: when `HTTPS_PROXY` applies (git, botocore, the agent
  browser), the proxy resolves; the hostname is still checked.
- **Agent browser tool behind an environment proxy** keeps the route guard
  only (redirect hops then pass through the deployment's proxy).
- **Renderer**: the app's own render origin is trusted as configured; pages
  that load other external assets now render without them in exports and
  thumbnails (intended).

## Pre-existing failures (also fail on the package A branch)

- `tests/e2e/test_llm_providers.py` — 5 (needs `OPENAI_API_KEY_TEST`).
- `tests/e2e/test_connection_oauth_flow.py::TestUserOverlaySync` — 2
  (`libodbc.so.2` missing in this sandbox; `ms_fabric` import).
