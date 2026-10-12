# Feedback Loop — "`/integrations/<id>/connection` stays on Loading... forever"

Reported: the connection page for an agent (linked from `ViewerRunGate`'s
**Connect** button when a viewer's credentials are missing) never leaves
"Loading..." for a viewer or a manager. Claim validated: the page reads its
data from a layout that no longer exists.

## Root cause (validated)

`frontend/pages/integrations/[id]/connection.vue` declared
`definePageMeta({ layout: 'integrations' })` and took its data only from
`inject('integration', ref(null))` / `inject('fetchIntegration', ...)`. Those
were provided by `frontend/layouts/integrations.vue`.

- `6f352fbdb` ("domains - ui ux") renamed `layouts/integrations.vue` to
  `layouts/data.vue` and moved the `/integrations/[id]/*` pages to
  `/data/[id]/*`, deleting `pages/integrations/[id]/connection.vue`.
- `95f8cc930` re-added `pages/integrations/[id]/connection.vue` with the old
  `layout: 'integrations'` + inject wiring; `git log -- frontend/layouts/integrations.vue`
  on the current branch is empty, so nothing provides `integration`.
- The inject default `ref(null)` keeps `v-if="!integration"` true forever, and
  the page itself never calls the API (network log: zero `/api/data_sources/<id>`
  requests on load).
- Second latent bug: the page read `integration.connection` (singular), but
  `DataSourceSchema` exposes `connections: []` — so even with data it would
  have shown no config.

## Loop A — deterministic reproduction

Stack: backend `uvicorn main:app --port 8031`, frontend `nuxt dev --port 3031`
with `BOW_API_TARGET=http://127.0.0.1:8031`, seeded with
`tools/agent/seed_org.py --demo --invite viewer@example.com`.

```
BASE=http://localhost:3031 DS_ID=<demo ds id> node frontend/tests/data_sources/integration-connection-page.mjs
```

- Base code: fails — `getByTestId('connection-edit')` never appears; page body is `Loading...`.
- Fixed code: `PASS: page loads for manager (edit + /connections/{id}) and viewer
  (no edit, no config fetch); 403/404 show an error, not a spinner`.

API facts the fix relies on (probed directly):

| Call | admin | viewer (member) |
|---|---|---|
| `GET /data_sources/{id}` | 200, `connections[0].config` present | 200, `config` null (PR #1329) |
| `GET /connections/{conn_id}` | 200 with `config`, `has_credentials` | 403 |
| `GET /data_sources/<missing>` | 404 | 403 |

## The fix

The page fetches `GET /data_sources/{id}` itself (default layout), reads the
primary connection from `connections[0]`, and only when a manager opens the
edit modal loads `GET /connections/{conn_id}` for `config`. The modal is not
rendered at all without `manage_connections`. Explicit loading, error
(403/404/other, localized en/es/he) and empty ("no connection") states.

## What this proves / regression notes

- Viewer never requests `/connections/{id}` and gets no edit control.
- `/agents` and `/old_agents/[id]/*` (the `data` layout) are untouched.
- Known, out of scope: `pages/old_agents/[id]/files.vue` `editConn()` links to
  `/integrations/<connection id>/connection` with a *connection* id; the page
  keys on a data-source id, so that link now lands on the not-found state
  instead of an infinite spinner.
