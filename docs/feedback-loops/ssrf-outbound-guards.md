# Feedback Loop — SSRF guards on outbound URLs

Fixes the "Yearly security audit" SSRF findings: git repository test,
LLM `test_connection` (custom/OpenAI `base_url`), audit-stream test, PDF export
of page artifacts, and the `web_fetch` filter bypass (CGNAT/metadata IPs).

## Design: two tiers, one module

`backend/app/core/url_safety.py`

| Tier | Who controls the URL | Rule | Used by |
|---|---|---|---|
| strict — `host_is_public` | the agent (anyone who can chat) | every resolved address must be `is_global` (excludes private, loopback, link-local, **CGNAT 100.64/10**, reserved), IPv4-mapped IPv6 unwrapped, metadata IPs (`fd00:ec2::254`, `100.100.100.200`) denied | `web_fetch`, browser tool, PDF render request guard |
| admin — `check_admin_url` / `check_git_remote` | an admin with `manage_llm` / settings perms | http(s) only (git: https/http/ssh/git/scp-style; **no `file://`, local paths, `ext::`/`fd::` helpers**); link-local + cloud metadata always denied; private LAN **allowed by default** (Ollama, internal GitLab, private SIEM) | git test/create/update/clone/push, LLM provider save + every `LLM()` construction, audit-stream create/update/test |

Operators who want the strict rule for admin URLs too:
`security.block_private_outbound: true` in bow-config.

PDF export: both headless pages (`set_content` dashboards and `/print/doc`)
get a `page.route` guard — `data:`/`blob:`/`about:`, the app's own render
origin, and public hosts pass; everything else is aborted and logged.

Known limit (documented in the module): the check resolves DNS separately from
the HTTP client, so DNS rebinding is not addressed here.

## Loop

Sandbox per `.agents/skills/sandbox-feedback-loop`: backend on :8000 (sqlite,
pinned `BOW_ENCRYPTION_KEY`), frontend on :3000, seeded with
`tools/agent/seed_org.py --demo`. A **canary** HTTP server
(`ssrf-outbound-guards/canary.py`) on `0.0.0.0:9999` logs every hit — it stands
in for an internal service; a hit means the server made the request.

### 1. Admin endpoints through the real API (`probe.py`)

```
LLM custom base_url http://169.254.169.254/v1          -> 400 URL points at a link-local or cloud metadata address.
LLM custom base_url http://[fd00:ec2::254]/v1          -> 400 URL points at a link-local or cloud metadata address.
LLM custom base_url http://metadata.google.internal/v1 -> 400 URL points at a cloud metadata service.
LLM custom base_url file:///etc/passwd                 -> 400 URL scheme must be one of: http, https.
LLM custom base_url http://127.0.0.1:9999/.../v1       -> 200 (reached canary: private LAN still allowed)
git test file:///etc                                   -> 400 Repository URL must use https:// or ssh ...
git test /home/user/bagofwords                         -> 400 Repository URL must use https:// or ssh ...
git test ext::sh -c touch% /tmp/pwned                  -> 400 Git remote helpers are not supported.
git test https://169.254.169.254/x.git                 -> 400 URL points at a link-local or cloud metadata address.
git test http://127.0.0.1:9999/git-private-ok.git      -> 400 Repository is empty (reached canary)
audit stream test https://169.254.169.254/...          -> 400 audit_stream.invalid_field
audit stream test http://127.0.0.1:9999/audit-...      -> 200 ok (reached canary)
```
Canary: exactly the three "private-ok" hits; `/tmp/pwned` not created.

### 2. PDF render guard with the real headless Chromium (`pdf_guard.py`)

Artifact HTML with `<img>` to `127.0.0.1:9999`/`localhost:9999` and a
`fetch('http://127.0.0.1:9999/pdf-fetch-exfil?data=secret')`:

- without guard: canary got 3 hits including `/pdf-fetch-exfil?data=secret`
- with `_guard_requests`: **0 hits**, 3 `PDF render blocked request` warnings

### 3. `web_fetch` tool, before vs after (`web_fetch_live.py`)

The real `WebFetchTool` run in-process (real DNS/network path):

| URL | base | this branch |
|---|---|---|
| `http://100.64.0.1/latest/meta-data/` (CGNAT) | **success=True** (request made) | refused |
| `http://100.100.100.200/latest/meta-data/` (Alibaba metadata) | **success=True** | refused |
| `http://[::ffff:169.254.169.254]/` | refused | refused |
| `http://127.0.0.1:9999/agent-internal` | refused | refused |

A live agent turn through the chat UI was attempted (prompt typed and sent at
`/`), but the sandbox Anthropic key returned "credit balance is too low", so the
tool was driven directly instead.

### 4. Tests

- New `backend/tests/unit/test_url_safety.py` (66 cases); CGNAT/metadata cases
  added to `test_web_fetch_tool.py`.
- Unit: web_fetch, browser tools, git test connection, audit-stream
  destinations, doc PDF render, PDF timeout, artifact browser verification —
  all pass.
- e2e (sqlite): git repository/sync/write, audit streams, PDF completeness,
  LLM providers — 74 passed; the 5 `test_llm_providers.py` failures need
  `OPENAI_API_KEY_TEST` and fail identically on the base commit.
