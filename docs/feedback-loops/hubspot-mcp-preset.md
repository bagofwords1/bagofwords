# Feedback Loop — HubSpot MCP connector preset

Adds HubSpot's hosted CRM MCP server (`https://mcp.hubspot.com`) as a one-click
catalog tile, connected with per-user OAuth through an admin-registered HubSpot
app. This doc is the runnable loop that validates the preset in a fresh sandbox:
registry entry → catalog API → Add Connection tile → oauth_app form → live
reachability probe → generated authorize URL.

## What was added (validated)

| Piece | File |
|---|---|
| Preset entry (`key="hubspot"`, `auth="oauth_app"`) | `backend/app/schemas/data_source_registry.py` (`MCP_PRESETS`) |
| Brand icon + key→file mapping | `frontend/public/data_sources_icons/hubspot.png`, `frontend/components/DataSourceIcon.vue` (`CONNECTOR_ICON_FILE`) |
| Regression tests | `backend/tests/unit/test_mcp_presets.py` (the `test_hubspot_*` block) |

No new backend plumbing: the preset is a named instance of the existing `mcp`
type, and the `oauth_app` path already supports PKCE, per-provider endpoints and
a configurable `token_endpoint_auth_method`. `_conn_connector_key`
(`backend/app/services/data_source_service.py`) resolves the brand icon by
matching `config.server_url` against the presets, and `agent_icon.py` derives
`icon_token` from the same list — so nothing needs a hardcoded `hubspot` entry
beyond the frontend icon filename map.

## Three findings that shaped the preset

**1. HubSpot has no Dynamic Client Registration.** Its authorization-server
metadata advertises no `registration_endpoint` (live probe, 2026-09), so this
cannot be a zero-setup DCR tile like Notion/Linear/Monday/Sentry. It is an
`oauth_app` preset: the admin registers a HubSpot public app and supplies
client_id/secret, like `github`.

```console
$ curl -s https://mcp.hubspot.com/.well-known/oauth-authorization-server
{"issuer":"https://mcp.hubspot.com",
 "authorization_endpoint":"https://mcp.hubspot.com/oauth/authorize/user",
 "token_endpoint":"https://mcp.hubspot.com/oauth/v3/token",
 "token_endpoint_auth_methods_supported":["client_secret_post"],
 "code_challenge_methods_supported":["S256"], ...}   # no registration_endpoint
```

`mcp.hubspot.com` still enters `allowed_dcr_hosts()` (it is derived from preset
`server_url` hosts). That is harmless: `ensure_mcp_oauth_config` fails closed on
the missing `registration_endpoint` with an explicit message.

**2. `server_url` is the bare origin — HubSpot serves MCP at the root path.**
`https://mcp.hubspot.com/mcp` is a 404. This is the only preset in the list
without a path segment, so it is the one most likely to get "corrected". Since
`MCPConnectionForm` matches a preset by *exact* `server_url`, adding `/mcp`
breaks preset recognition in edit mode as well as the connection itself. Loop B
below asserts both directions.

**3. Scopes are set on the HubSpot app, not by us; audience is unset.** HubSpot
advertises `scopes_supported: []`, and Loop D showed why: for an MCP Connector
app the `scope` parameter is inert — 4 requested, **37 granted**, the bundle
coming from the app's own configuration. The preset's `scopes` value is therefore
documentation of a sane minimum, not a control. No `audience` is set: HubSpot
does not advertise RFC 8707 resource indicators, and our token exchange would
send an unexpected `resource` parameter if one were configured.

`sample_tools` shipped as `None` (no machine-readable list is published, and
`tools/list` needs auth) and was filled in from a **live** `tools/list` once a
real portal was connected — see Loop D. They are discovered names, not guesses.

## Loop A — deterministic (no external services)

```bash
cd backend
pip install uv && uv sync --extra dev
export BOW_DATABASE_URL="sqlite:///db/app.db" && mkdir -p db
uv run pytest tests/unit/test_mcp_presets.py -q
```

Observed:

```
24 passed, 255 warnings in 51.47s
```

The wider MCP suite (`test_mcp_presets.py test_mcp_tool_registry.py
test_execute_mcp_routing.py test_mcp_context_injection.py`): `98 passed in
186.96s`.

On code without the preset, `test_hubspot_preset_is_oauth_app_not_dcr` fails
first with `hubspot must be in the MCP catalog` (`mcp_preset("hubspot")` returns
`None`).

## Loop B — live confirmation (no HubSpot credentials required)

Everything below runs against the real `mcp.hubspot.com` with a dummy client id
— reachability and the authorize-URL shape do not need a real HubSpot app.

Boot the stack per `.claude/skills/sandbox-feedback-loop`, sign up, then:

```bash
JWT=...   # auth.token cookie
ORG=...   # GET /api/organizations

# 1. The tile is in the catalog API
curl -s -H "Authorization: Bearer $JWT" -H "X-Organization-Id: $ORG" \
  localhost:8000/api/connectors/catalog | jq '.[] | select(.key=="hubspot")'
```

```json
{ "key": "hubspot", "title": "HubSpot",
  "server_url": "https://mcp.hubspot.com", "transport": "streamable_http",
  "auth": "oauth_app", "category": "services", "allowed_auth": ["oauth_app"],
  "oauth_defaults": {
    "authorize_url": "https://mcp.hubspot.com/oauth/authorize/user",
    "token_url": "https://mcp.hubspot.com/oauth/v3/token",
    "scopes": "oauth, crm.objects.contacts.read, crm.objects.companies.read, crm.objects.deals.read",
    "audience": null, "token_endpoint_auth_method": null },
  "sample_tools": ["query_crm_data", "search_crm_objects", ...] }
```

```bash
# 2. Reachability, and the root-path-vs-/mcp assertion
for U in https://mcp.hubspot.com https://mcp.hubspot.com/mcp; do
  curl -s -X POST -H "Authorization: Bearer $JWT" -H "X-Organization-Id: $ORG" \
    -H 'Content-Type: application/json' localhost:8000/api/connections/test-params \
    -d "{\"name\":\"probe\",\"type\":\"mcp\",\"config\":{\"server_url\":\"$U\",
         \"transport\":\"streamable_http\",\"auth_type\":\"oauth_app\"},
         \"credentials\":{\"client_id\":\"x\",\"client_secret\":\"y\"}}"
done
```

Observed — the 401 challenge from the root path is correctly reinterpreted as
"reachable, needs sign-in", and the `/mcp` path is not:

```
https://mcp.hubspot.com      → success=True  "Server reachable — sign-in required (as configured)."
https://mcp.hubspot.com/mcp  → success=False "Failed to connect to MCP server: Session terminated"
```

```bash
# 3. The authorize URL the backend builds for a saved connection
curl -s -H "Authorization: Bearer $JWT" -H "X-Organization-Id: $ORG" \
  "localhost:8000/api/connections/$CID/oauth/authorize" | jq -r .authorization_url
```

```
https://mcp.hubspot.com/oauth/authorize/user
  ?response_type=code
  &client_id=sandbox-client-id
  &redirect_uri=http%3A%2F%2Flocalhost%3A3000%2Fapi%2Fconnections%2Foauth%2Fcallback
  &state=<jwt>
  &code_challenge=<...>&code_challenge_method=S256
  &scope=oauth+crm.objects.contacts.read+crm.objects.companies.read+crm.objects.deals.read
```

Points to check: PKCE `S256` (what HubSpot advertises), scopes space-delimited
per RFC 6749 (the registry stores them comma-separated; `_normalize_scopes`
converts), and **no `resource` parameter**.

> **Do not try to validate this URL with curl.** An earlier version of this doc
> claimed that fetching it returns HTTP 200 and HubSpot's OAuth UI
> (`__hsipltan = "oauth-ui"`) "i.e. HubSpot accepts the endpoint, parameters and
> scopes". That inference is wrong. `/oauth/authorize/user` serves a static SPA
> shell and validates client-side: with a real client_id, a bogus one, an added
> `scope`, or the canonical vs. a regional host, the response is **byte-identical**
> (91583 bytes, HTTP 200). The same is true of `app.hubspot.com/oauth/authorize`.
> A 200 here proves only that the host is reachable.
>
> To validate credentials server-side, use the **token** endpoint instead — it
> discriminates (see below). To validate the authorize URL itself, a human has to
> open it in a browser.

A server-side credential check that does work, with no consent and no browser:

```bash
curl -s -X POST https://mcp.hubspot.com/oauth/v3/token \
  -d "grant_type=client_credentials&client_id=$CID&client_secret=$SECRET"
```

- correct secret → `{"status":"BAD_SCOPES", ...}` — client auth passed
- wrong secret   → `{"status":"BAD_CLIENT_SECRET", ...}`

Adding valid-looking scopes then returns
`USER_LEVEL_CLIENT_CREDENTIALS_NOT_SUPPORTED`: the `client_credentials` grant is
**not** available for a HubSpot *MCP Connector* app, so there is no way to obtain
a token without a human completing consent. Plan verification around that.

## Loop C — UI (Playwright)

Sign up, dismiss onboarding, open `/agents/new` → "Create new connection",
search `hubspot`. Observed:

- The tile renders under **Services** with the HubSpot sprocket and an **MCP**
  badge.
- Clicking it opens `MCPConnectionForm` with name `HubSpot`, the description,
  and the Authentication select containing **only** "OAuth (admin-registered
  app)" — `allowed_auth: ["oauth_app"]` gating the form.
- Under **Advanced**: Server URL `https://mcp.hubspot.com`, Transport
  `streamable_http`, Authorize/Token URLs as above, Scopes prefilled, and
  "Resource (audience, optional)" **empty**.
- After saving with a client id/secret, the stored connection is
  `type=mcp`, `auth_policy=user_required`, `allowed_user_auth_modes=["oauth"]`,
  `config={"server_url":"https://mcp.hubspot.com","transport":"streamable_http","auth_type":"oauth_app"}`.
- `GET /api/connections` derives `connector_key: "hubspot"` and
  `GET /api/data_sources` derives `icon_token: "type:hubspot"`; the agents page
  renders `/data_sources_icons/hubspot.png` (512×512, loaded) with a "Sign in"
  badge.

Selector notes for anyone re-running this: the Add Connection modal's portal
root (`#headlessui-portal-root`) is a zero-size wrapper, so Playwright reports
it hidden even when the modal is up — gate on modal content (a category chip)
instead. And when reading field values back, enumerate `input` and `select`
separately: indices taken from a combined query do not line up against an
input-only locator.

## Loop D — full sign-in against a real portal (2026-09)

Run with a real HubSpot **MCP Connector** app against an EU portal
(`hub_id 149316337`). This closed every open question above.

**The install flow.** A human must click through consent — `client_credentials`
is unavailable (see above), so there is no headless path. Generate the PKCE pair
yourself rather than using HubSpot's builder: the builder's *Regenerate* button
silently replaces the verifier, and a challenge/verifier mismatch fails the
exchange with `BAD_CODE_VERIFIER` (which, usefully, is a *different* error from
`BAD_AUTH_CODE` — the code was recognised).

Confirmed working, exactly as the preset generates it:

```
https://mcp.hubspot.com/oauth/authorize/user
  ?response_type=code&client_id=…&redirect_uri=…
  &code_challenge=…&code_challenge_method=S256&scope=…
```

HubSpot redirects that to a **portal-scoped regional** URL
(`https://mcp-eu1.hubspot.com/oauth/<hub_id>/authorize/user?…`), preserving the
query string. So the preset's canonical, portal-less `server_url` is correct for
an EU portal — HubSpot does the regional routing itself. Do not hardcode a
regional host or a portal segment.

Token exchange at `https://mcp.hubspot.com/oauth/v3/token` returns a bearer token
(`expires_in: 1800`) plus a refresh token, and carries `hub_id`, `user_id`,
`token_use` and `scopes`.

**The `scope` parameter does not do what it looks like.** We requested 4 scopes;
HubSpot granted **37**, a fixed bundle from the app's own configuration. The
parameter is neither honoured nor rejected — sending it is harmless but controls
nothing. Scopes are configured on the HubSpot app, not by us. The preset keeps
its `scopes` value only as documentation of a sane minimum.

Notably granted: `crm.hubsql.execute`, `crm.objects.custom.read`,
`crm.objects.owners.read`.

**MCP works over the canonical host for an EU portal.** `initialize` →
`HubSpot MCP 1.0`; `tools/list` → **25 tools**. Session id comes back in the
`mcp-session-id` response header and must be echoed on later calls, after a
`notifications/initialized`.

**`query_crm_data` — HubSpot CRM over SQL.** The headline find, and the reason
the connector design doc changed. Verified live:

```sql
SELECT COUNT(*) FROM CONTACT            -- → "The total count is 1239."
```

Supported: aggregates, `GROUP BY`, `DATE_TRUNC(prop,'DAY|WEEK|MONTH|QUARTER|YEAR')`,
`MEDIAN`, `ORDER BY`, `LIKE`, `IS NULL`, `BETWEEN`, and cross-object traversal via
`OBJECT.property` (max 2 associated types).
Unsupported: `JOIN`, `UNION`, subqueries, CTEs, `HAVING`, `SELECT DISTINCT`,
`COUNT(DISTINCT x)`, `AS` aliases, `CASE WHEN`, `IF()`, `COALESCE`, string
functions. `tool_guidance` must be called first (argument is `toolNames`, an
array — not `toolName`).

**One app, one token, both surfaces.** The MCP-issued token is an ordinary
HubSpot token:

| Check | Result |
|---|---|
| `GET api.hubapi.com/oauth/v1/access-tokens/{token}` | 200 — app_id, hub_id, user, 37 scopes |
| `GET api.hubapi.com/crm/v3/properties/contacts` | 200 — **409 properties**, custom ones included |
| `POST /crm/v3/objects/contacts/search` | 200 — `total: 1239` |
| `GET /crm-object-schemas/v3/schemas` | 200 — 0 (portal has no custom objects) |
| `POST /collector/graphql` | 403 — "app hasn't been granted all required scopes" |

So a planned native CRM connector needs **no second sign-in** — one app and one
token serve both the MCP tools and the REST API.

## What is not covered

- **The Search API's 10,000-result cap.** The test portal holds 1,239 contacts,
  so the ceiling could not be reached. Still unverified.
- **HubSQL over REST.** `crm.hubsql.execute` is granted and SQL works through the
  MCP tool, but no public REST endpoint was found (six plausible paths, all 404)
  and HubSQL appears in none of the 121 published API specs. Treat SQL as
  MCP-only until HubSpot documents otherwise.
- **`/collector/graphql`** is real and reachable but scope-gated (403); its
  capabilities remain unassessed.
