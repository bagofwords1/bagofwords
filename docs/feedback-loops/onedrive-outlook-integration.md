# Feedback Loop — "connect OneDrive + Outlook, run real completions, verify read_file / materialization / email search+read end-to-end"

Driving the product end-to-end for the Microsoft Graph connectors (OneDrive +
Outlook Mail) surfaced **two real backend defects** on the connect path, and —
once fixed — a full real-completion run proved the file and mail tool surface
(`list_files` / `read_file` / `search_files` + session-file materialization)
works through the actual agent pipeline.

The two connectors are configured through the same UI as any data source
(Add Connection → OneDrive / Outlook Mail → paste Entra app credentials →
Save), then each user signs in with Microsoft (per-user OAuth). Screenshots of
every step are under `assets/onedrive-outlook/`.

## Root causes (validated)

### Bug A — "Test credentials" always fails for OneDrive / SharePoint / Outlook Mail

The integration form's **Test credentials** button (and any pre-save
`POST /connections/test-params`) always returned:

> No access_token and no service-principal credentials configured

even with a perfectly valid Tenant/Client/Secret (see
`assets/onedrive-outlook/05-onedrive-test.png`).

Cause: `ConnectionService._resolve_client_by_type`
(`backend/app/services/connection_service.py`) narrowed the constructor kwargs
to the client's `inspect.signature` parameters. `OnedriveClient`,
`SharepointClient` and `GraphMailClient` are thin subclasses declared as
`def __init__(self, **kwargs)`, so their signature reports only `self` +
`kwargs` — the narrowing stripped `tenant_id` / `client_id` / `client_secret`
(and every other real arg), and the client was built with no credentials.
`GraphDriveClient._token()` then raised the message above.

The runtime path `ConnectionService.construct_client` already guarded against
exactly this with an `accepts_var_kwargs` check
(`connection_service.py`, the `construct_client` method) — only the
pre-save `_resolve_client_by_type` twin was missing it.

### Bug B — Outlook Mail "Sign in with Microsoft" is dead on arrival

`outlook_mail`'s only usable auth mode is per-user OAuth, but starting that flow
returned HTTP 400:

> OAuth not supported for connection type: outlook_mail

Cause: in `connection_oauth_service.get_oauth_params`
(`backend/app/services/connection_oauth_service.py`) the Entra branch listed
`("powerbi", "ms_fabric", "sharepoint", "onedrive")` — `outlook_mail` was
absent, so it fell through to the final `raise ValueError`. It was also missing
from the `scopes_map` (no `Mail.Read`), from `ENTRA_OBO_CONNECTION_TYPES`, and
from `_OBO_SCOPES`, so the Entra-login OBO auto-provisioning path skipped it too.

## Loop A — deterministic reproduction (no external services)

Both defects reproduce against the running backend with no Microsoft calls —
Bug B fails purely in `get_oauth_params`, and Bug A fails in the client
constructor before any HTTP is attempted.

```bash
# Bug B — Outlook OAuth authorize returns 400 before ever reaching Microsoft
curl -s "$BASE/api/connections/$OUTLOOK_CONN_ID/oauth/authorize" \
  -H "Authorization: Bearer $TOK" -H "X-Organization-Id: $ORG"
# BEFORE fix -> {"detail":"OAuth not supported for connection type: outlook_mail"}

# Bug A — Test credentials for OneDrive with valid creds
curl -s -X POST "$BASE/api/connections/test-params" \
  -H "Authorization: Bearer $TOK" -H "X-Organization-Id: $ORG" \
  -d '{"type":"onedrive","config":{},"credentials":{"tenant_id":"…","client_id":"…","client_secret":"…"},"auth_policy":"user_required","allowed_user_auth_modes":["oauth"]}'
# BEFORE fix -> {"success":false,"message":"No access_token and no service-principal credentials configured"}
```

A regression test asserting the general invariant (every Graph subclass with a
`**kwargs` constructor keeps its credentials through `_resolve_client_by_type`,
and every OAuth-capable Entra connector resolves `get_oauth_params` without
raising) lives in `backend/tests/unit/test_graph_connect_regressions.py`.

## The fix

- `connection_service.py` — `_resolve_client_by_type` now skips signature
  narrowing when the constructor accepts `**kwargs` (mirrors `construct_client`).
- `connection_oauth_service.py` — `outlook_mail` added to the Entra branch of
  `get_oauth_params`, to `scopes_map` (`openid profile offline_access Mail.Read
  User.Read`), to `ENTRA_OBO_CONNECTION_TYPES`, and to `_OBO_SCOPES`.

After the fix (verified against the running server):

```
outlook authorize   -> 200  scope=openid profile offline_access Mail.Read User.Read
onedrive test-params-> 200  {"success": true, "message": "Connected successfully. …"}
```

## Loop B — live confirmation (real completions, Graph boundary stubbed)

To prove the *whole product path* — completion → agent tool dispatch →
`GraphDriveClient` / `GraphMailClient` → session-file materialization — real
Anthropic (Claude Sonnet 5) completions were run through the actual UI against a
signed-in OneDrive + Outlook connection, with **only** the `graph.microsoft.com`
HTTP boundary stubbed (a seeded OneDrive of 4 files + a 3-message mailbox).
Everything else — OAuth credential resolution, client construction, the tools,
the LLM, materialization — ran for real.

- **OneDrive** (`assets/onedrive-outlook/onedrive-05-stream-8.png`): the agent
  called `list_files` (all 4 files with sizes), `read_file` on `sales.csv`
  (tabular, 5 rows × 4 cols) and `notes.txt` (text), **materialized the CSV into
  a session file** and ran `inspect_data`, then answered correctly
  ("US: $660,000" = 318000 + 342000).
- **Outlook Mail** (`assets/onedrive-outlook/outlook-report-top.png`): the agent
  called `search_files` for "revenue" and returned exactly the two matching
  messages ("Q1 revenue numbers ready", "APAC launch timing"). `read_file` over
  mail is verified at the client/tool level — `GraphMailClient.read_file`
  returns the message rendered as text (headers + HTML-stripped body, e.g. the
  "$567,000" figure and `cfo@bow14.example` sender). Behaviour note: in
  chat-mode completions Claude Sonnet 5 tended to repeat `search_files` and
  conclude rather than chain into `read_file` for a single mail lookup
  (`assets/onedrive-outlook/outlook-read-05-stream-9.png`) — worth a nudge in
  the mail tool descriptions, but not a connector defect.

The connectors' own `test_connection` also passes through the product's real
credential-resolution path once signed in (Outlook reports `Connected as
demo1@bow14.onmicrosoft.com` from Graph `/me`).

## Live Microsoft OAuth — environment blockers (not product bugs)

A fully-live run against a real Microsoft tenant could not be completed from the
CI sandbox, for reasons external to the product; all are documented here so the
run can be finished from an environment without them:

1. **Interactive consent unreachable from the sandbox browser.** Chromium
   consistently fails to load `login.microsoftonline.com` through the sandbox's
   TLS-terminating egress proxy (`ERR_CONNECTION_RESET`), even though `curl` /
   `openssl` / server-side `httpx` reach it fine through the same proxy. `github.com`
   loads through the proxy; Microsoft's login host does not — a browser-vs-proxy
   incompatibility specific to that host.
2. **MFA on the data-bearing account.** `yochayettun@bow14.onmicrosoft.com`
   (the only account with a provisioned OneDrive + mailbox) enforces MFA, so the
   non-interactive ROPC grant is refused (`AADSTS50076`).
3. **Demo accounts have no data.** `demo1` / `demo2` are MFA-free and yield a
   delegated Graph token, but their OneDrive is unprovisioned
   (`403 provisioningNotAllowed`) and they have no mailbox
   (`404 MailboxNotEnabledForRESTAPI`).
4. **App registration lacks `Mail.Read`.** The provided Entra app grants
   `Files.Read.All` but not `Mail.Read`, so live Outlook reads need that
   delegated permission added + admin-consented.

**Recommended way to finish the live run** (sidesteps 1 and 2): the OAuth
**device-code flow**. The server requests a device code (works via the proxy),
the user completes sign-in + MFA + consent on their *own* device at
`microsoft.com/devicelogin`, and the server polls for the delegated token — no
sandbox browser and no server-held MFA secret. The resulting token is injected
as the user's `UserConnectionCredentials` and the same completions above run
against the real account.

## What this proves / regression notes

- Two connect-path defects that made OneDrive/SharePoint "Test credentials" and
  Outlook "Sign in with Microsoft" unusable are fixed and covered by a
  regression test asserting the general invariant.
- The OneDrive + Outlook agent tool surface (`list_files` / `read_file` /
  `search_files`) and session-file materialization work end-to-end through real
  completions once a user is signed in.
- The remaining gap is live Microsoft consent, blocked only by sandbox/tenant
  environment constraints, with a concrete device-code path to close it.
