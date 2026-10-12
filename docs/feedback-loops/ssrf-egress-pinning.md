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
- Out of scope here (package B): syslog / S3 audit destinations, git remotes,
  browser tool / PDF render, Azure SDK & Anthropic-on-Foundry LLM clients
  (still `check_admin_url` only), kernel-level sandbox network limits.
- Pre-existing failures (also fail on base, sandbox env): `test_openai_tool_call_ids.py`
  (5), `test_sharepoint_onprem_client.py` / `e2e/test_sharepoint_onprem.py` /
  `test_connection_file_browse.py::test_contract_sharepoint_server`
  (`REQUESTS_CA_BUNDLE` set by the sandbox proxy).
