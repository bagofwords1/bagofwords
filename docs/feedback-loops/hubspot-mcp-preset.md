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

**3. Scopes and audience are deliberately conservative.** HubSpot advertises
`scopes_supported: []` — its scopes are declared on the app and further gated by
the portal's subscription tier, so no list is universally valid. The default is
the read-only CRM set available on every tier, plus `oauth` (which HubSpot apps
must request); admins widen it in the form. No `audience` is set: HubSpot does
not advertise RFC 8707 resource indicators, and our token exchange would send an
unexpected `resource` parameter if one were configured.

`sample_tools` is `None` on purpose. HubSpot publishes no machine-readable tool
list (the docs page is client-rendered; the `@hubspot/mcp-server` npm package is
the *developer* MCP, a different server) and `tools/list` needs auth — so any
list here would be guesswork shown to admins as fact. The form falls back to
"discovered after connecting" and `refresh_tools` fills in the real catalog.

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
  "sample_tools": null }
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
converts), and **no `resource` parameter**. Fetching that URL returns HTTP 200
and HubSpot's own OAuth UI (`__hsipltan = "oauth-ui"`), not a 400 — i.e. HubSpot
accepts the endpoint, parameters and scopes as a well-formed authorize request.
With a real HubSpot app's client_id this is the consent screen.

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

## What is not covered

A real portal sign-in (token exchange, refresh, and live `tools/list`) needs a
HubSpot public app's client id/secret. The tool catalog, and therefore whether
any tool needs scopes beyond the read-only default, is unverified until then.
