# Build a data app on BOW with OAuth

BOW can act as an OAuth 2.1 authorization server for a customer-facing data
application. End users sign in to BOW with their existing local or SSO account,
then the external app can create reports, run completions, and stream agent and
tool events on that user's behalf.

The OAuth grant does not replace BOW permissions. The signed-in user's current
organization membership and RBAC permissions are still evaluated on every API
request.

## Register the application

An administrator with `manage_settings` opens **Settings → Integrations →
OAuth Apps**, then registers:

- a descriptive application name;
- one or more exact redirect URIs;
- the **BOW app API** (`app`) access surface;
- optionally, the separate **MCP** (`mcp`) access surface;
- optionally, **Trusted** for an application operated by the same organization.

The client ID is safe to use in a browser. BOW shows a client secret once, but
a public browser client must not embed that secret. It should use Authorization
Code with PKCE (`S256`) and OAuth `state`. A backend-for-frontend may keep the
secret server-side.

Trusted applications still require the user to authenticate. They only skip
the consent confirmation after authentication. Enable this for first-party
applications operated by the organization, never for arbitrary third parties.
If a user denies an untrusted request, BOW issues no authorization code or
token and returns `error=access_denied` with the original `state` to the exact
registered redirect URI. The user's BOW login session is left intact.

## Endpoints and scopes

Discovery is available from:

```text
GET /.well-known/oauth-authorization-server
GET /.well-known/oauth-protected-resource/api
GET /.well-known/oauth-protected-resource
```

The authorization and token endpoints are:

```text
GET  /api/oauth/authorize
POST /api/oauth/token
```

| Scope | Access | Access-token lifetime |
| --- | --- | --- |
| `app` | The main BOW REST API, subject to the user's RBAC | 8 hours |
| `mcp` | The BOW MCP endpoint only | 365 days, unchanged |

A token containing both scopes uses the shorter 8-hour lifetime. Refresh
tokens last 365 days and rotate on every successful refresh. Reusing an old
refresh token fails. Deleting an application or changing its access surfaces
immediately revokes its pending grants and existing access/refresh tokens.

## Recommended browser architecture

For a browser SPA, use a small same-origin backend proxy: the browser starts
PKCE, stores the short-lived credentials in session storage or a secure server
session, and calls the proxy; the proxy streams BOW's SSE response without
buffering. This avoids distributing a client secret and usually avoids a new
CORS policy. A browser that calls BOW directly from a different origin requires
that origin to be explicitly allowed by the deployment's CORS configuration.

The included dependency-free example implements the proxy pattern:

```bash
node examples/oauth-report-app/server.mjs
```

Register `http://127.0.0.1:4173/callback`, select `app`, copy the public client
ID, and open `http://127.0.0.1:4173`. The example creates a report, streams a
completion, renders incremental answer and tool events, supports the interactive
`clarify` response flow, and displays `create_data` and `create_artifact` work.

## Security invariants

- An OAuth token is pinned to the client application's organization. A caller's
  `X-Organization-Id` cannot move it to another tenant.
- BOW rechecks that the user is active and still belongs to that organization
  on every request. Removing the membership invalidates access immediately.
- `app` tokens cannot call MCP, and `mcp`-only tokens cannot call the main API.
- Existing BOW RBAC remains the permission boundary inside the selected access
  surface.
- Tokens are stored as SHA-256 hashes. Authorization codes are single-use and
  require PKCE S256.

Local password sign-in and the configured SSO providers preserve the original
authorization request across their sign-in round trip, so the same integration
works with local accounts, Entra ID, OIDC, and Google without provider-specific
application code.

## Deliberately deferred

RFC 8693 token exchange for headless Entra-to-BOW calls is not part of this
version. It can be added later with explicit per-client enablement, accepted
audiences, Entra JWKS validation, existing SSO identity mapping, and JIT
provisioning disabled by default. Granular resource scopes, dynamic client
registration, token introspection/revocation endpoints, and a user-facing
connected-apps page are also deferred.
