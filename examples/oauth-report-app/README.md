# Northstar Insights OAuth demo

A dependency-free browser app proving that a third-party product can use BOW's
OAuth 2.1 `app` scope to create reports and stream completions—including tool
activity—without possessing a BOW password, API key, or client secret.

## Run it

1. In BOW, open **Settings → Integrations → OAuth Apps** and register an app:
   - Redirect URI: `http://127.0.0.1:4173/callback`
   - Scope: **BOW app API** (`app`)
   - Leave **Trusted** off to exercise consent; turn it on to exercise seamless SSO.
2. Copy the client ID. The one-time client secret is not used by this public PKCE client.
3. Start the demo:

   ```bash
   node examples/oauth-report-app/server.mjs
   ```

4. Open `http://127.0.0.1:4173`, paste the client ID, and connect.

For an untrusted app, choosing **Deny** returns the browser to the demo with the
standard `access_denied` result. No authorization code or token is created, the
OAuth `state` is verified, and the demo remains disconnected.

The local server only serves static assets and forwards `/bow/*` to BOW's API
at `http://127.0.0.1:8000`. Override that target with `BOW_API_ORIGIN`. OAuth
access and refresh tokens are kept in `sessionStorage`; only the public client
ID and chosen BOW web origin are kept in `localStorage`.
