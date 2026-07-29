# Sandbox Feedback Loop — slow agent picker in the prompt box / on a new report

Reported after the `/agents` instruction fixes: the **data source selector in
PromptBoxV2** takes seconds to populate, and creating a new report pays it
twice — once on the home prompt box, once again when the new report page mounts
its own prompt box. The reporting workspace has **~200 agents** in the selector.

## Root causes (validated, fixed)

The selector, the `@` mention menu and the /agents tree all read
`GET /data_sources/active?include_unconnected=true`. Two costs inside it, and
one on top of it:

1. **A catalog-wide aggregate nobody reads.** `_bulk_connection_aux` counted
   active tables per (data source, connection) — a `GROUP BY` joining
   `datasource_tables` × `connection_tables`, i.e. a scan of the org's entire
   catalog, on every call. At 131k catalog rows that was **454 ms of a 558 ms
   response (81%)**. No consumer of this endpoint renders a count:
   `table_count` is read from `/data_sources`, `/connections` and
   `/data_sources/{id}` (`useCatalogCount`, `layouts/data.vue`,
   `pages/old_agents`), never from the active list.

2. **A per-connection credential N+1 behind `user_status`.** For every
   `user_required` connection, `build_user_status_for_connection` issued its own
   `UserConnectionCredentials` lookup (plus a `UserDataSourceCredentials` lookup
   per data source). With 270 OBO connections that is **546 statements per
   request** — serialized round-trips, each paying network RTT on Postgres.

3. **The same list fetched 2–3× per page.** No shared cache across the 13
   callers of this endpoint:
   - `DataSourceSelector.onMounted` (inside `PromptBoxV2`)
   - `MentionInput.onMounted` → `fetchAvailableMentions`
   - `MentionInput`'s `selectedDataSourceIds` watcher — which fires because the
     selector emits its default "all agents" selection as soon as its own fetch
     resolves

   At 415 KB per response that is over a megabyte of duplicate payload per page
   load, and again on the report page after "create report".

## Environment + repro

Sandbox as in `agents-instructions-carryover-perf.md`. Seed the reported shape:

```bash
cd backend
export BOW_DATABASE_URL="sqlite:///db/app.db" BOW_SMTP_PASSWORD=dummy ANTHROPIC_API_KEY=dummy
uv run python scripts/seed_agents_page_perf.py 200 200 200   # 200 agents, 200 connections
# OBO worst case (what a delegated-auth workspace looks like):
sqlite3 db/app.db "UPDATE connections SET auth_policy='user_required'"
```

Measure the service call (wall + statement count) and the HTTP path:

```bash
uv run python scripts/profile_agents_page.py
curl -s -o /dev/null -w "active: %{time_total}s size=%{size_download}\n" \
  "http://localhost:8000/api/data_sources/active?include_unconnected=true" $H
```

### Observed — 212 agents / 270 connections / 131k catalog rows

| | wall | SQL | payload |
|---|---|---|---|
| before, 20/270 connections OBO | 0.56 s | 46 | 415 KB |
| before, all 270 OBO | **1.05 s** | **546** | 415 KB |
| after (both fixes) | **0.069 s** | **6** | 415 KB |

HTTP end-to-end, all-OBO: **1.00 s → 0.095 s**. Local SQLite has ~0 network
latency, so the 540 removed statements are worth far more on production
Postgres than the wall time here suggests.

## Fix

- `_bulk_connection_aux(include_table_counts=False)` skips both count queries;
  `_build_connections_list(include_table_counts=False)` then reports
  `table_count = None` rather than a zero that would read as an empty catalog
  (`ConnectionEmbedded.table_count` is now `Optional[int]`). Only
  `get_active_data_sources` passes it — `/data_sources` and
  `/data_sources/{id}`, whose consumers do render counts, are unchanged.
- `connection_identity.UserCredentialIndex` loads the caller's
  `UserConnectionCredentials` + `UserDataSourceCredentials` rows for the whole
  list in two queries, with the same "active, `is_primary` then newest" ranking
  the per-connection queries used; `build_user_status_for_connection`,
  `build_token_identity_status` and `build_kerberos_sso_status` read from it
  when given one. Single-agent callers keep their existing queries.
- `composables/useActiveAgents.ts` gives the frontend one shared in-flight
  request + 10 s freshness window (the shape `useAgent.initAgent()` already
  used). `DataSourceSelector` and `MentionInput` both go through it, so the
  mount pair and the selection-change refetch collapse to a single request.
  Connecting an agent refetches with `{ force: true }`.

## What this does NOT fix

The response is still **415 KB for 200 agents** — every agent carries its
connections, `user_status` and config. Now that the queries are flat, payload
build + transfer is what is left; a lighter projection for picker callers (id,
name, icon, auth state) is the next lever if the selector still drags.
