# Sandbox Feedback Loop — `/agents` page slow with many connections × tables

Reproduces the reported deployment: **50 connections × ~1,500 tables each,
3 agents**, where the agents page took 40s+ to load (`GET /api/data_sources`
alone measured at 43.7s server wait in production DevTools) and each
`review-hunks` call took ~14s.

## Root causes (validated, fixed)

1. **`GET /api/data_sources` per-connection COUNT N+1** —
   `DataSourceService.get_data_sources` built each agent's connection list
   without the batched maps `get_active_data_sources` already used: one COUNT
   join per connection per agent (+ a legacy-fallback COUNT and a per-agent
   latest-indexing query). Same N+1 in `get_data_source` (polled every ~2s
   while indexing runs) and, route-level, in `GET /api/connections`.
2. **Megabyte list payloads** — every list endpoint inlined each connection's
   latest `indexing.events` (up to 200 log entries × 50 connections ≈ 1.5 MB
   per response) that nothing in a list view renders. Serialized on the event
   loop, delaying every concurrent request; the pooled DB connection was held
   through serialization on the unfixed routes.
3. **`review-hunks` O(all pending builds)** — a build snapshots every
   instruction, so `_pending_suggestion_builds` returns every pending build in
   the org for any instruction; the endpoint paid one base-text query + a word
   diff per build even for carry-over rows with no actual change, plus the
   heavy `get_instruction` detail graph just for the access check.
4. **Frontend double-fetch** — the layout and the command palette both fired
   `GET /data_sources` on every page load.

## Repro / benchmark loop

Backend sandbox (same setup as `agents-instructions-perf.md`): fresh SQLite DB,
`alembic upgrade head`, run `main.py`, register `sandbox@bow.dev`. Then:

```bash
cd backend
export BOW_DATABASE_URL="sqlite:///db/app.db" BOW_SMTP_PASSWORD=dummy ANTHROPIC_API_KEY=dummy
uv run python scripts/seed_agents_page_perf.py 50 1500 3   # connections × tables × agents
uv run python scripts/seed_review_hunks_perf.py 40 60      # instructions × pending builds
uv run python scripts/profile_agents_page.py               # wall + SQL count + payload
```

## Measured (local SQLite, zero network latency)

| endpoint | before | after |
|---|---|---|
| `GET /data_sources` | 2.66s · **59 SQL** · 1,644 KB | 0.30s · **9 SQL** · 53 KB |
| `GET /data_sources/active` | 0.31s · 9 SQL · 1,644 KB | 0.26s · 9 SQL · 53 KB |
| `GET /data_sources/{id}` (2s poll) | 0.82s · **22 SQL** · 560 KB | 0.11s · **7 SQL** · 560 KB* |
| `GET /connections` | 0.18s · **105 SQL** · 1,628 KB | 0.06s · **7 SQL** · 37 KB |
| `GET .../review-hunks` (60 pending builds) | 0.09s · **67 SQL** · 1.7 KB | 0.03s · **8 SQL** · 1.7 KB |

\* the single-agent detail keeps `indexing.events` — the connections modal
renders the live indexing log from it.

HTTP end-to-end (`curl`, same sandbox): `/api/data_sources` 2.76s → 0.29s;
`/api/connections` 0.46s → 0.23s; review-hunks 0.18s → 0.03s with a
byte-identical response body.

On production Postgres the win is larger than the local wall times suggest:
the removed statements each paid network RTT, and the COUNT scans ran against
a `datasource_tables` heap continuously rewritten (bloated) by the reindex
sweeper — that multiplication is what stretched 59 queries into 43s.

## Fix summary

- `_bulk_connection_aux` now keys table counts by *(data source, connection)*
  (a shared connection no longer reports the sum of both agents' tables) and
  can defer `events_json`; wired into `get_data_sources`, `get_data_source`,
  and both active-list paths. Lists pass `include_indexing_events=False`.
- `GET /connections` list route batched to 4 grouped queries (catalog counts,
  legacy fallback, latest indexing with deferred events, tool counts).
- `review_hunks` bulk-loads base versions/texts and agent-execution traces,
  and skips carry-over rows (`proposed version == base version`) before
  diffing — the same rule the batched pending sweep applies. The route uses a
  light instruction+agent-ids fetch (`get_instruction_access_view`) for the
  visibility check.
- `release_request_db` added to `GET /data_sources`, `GET /connections`, and
  review-hunks so pooled connections are freed before serialization.
- Frontend: `useAgent.initAgent()` dedupes concurrent callers (shared
  in-flight promise + 10s freshness window, `{ force: true }` to bypass); the
  command palette reuses the shared list instead of its own
  `GET /data_sources`.

## Repro artifacts

- `backend/scripts/seed_agents_page_perf.py` — connections/tables/agents shape.
- `backend/scripts/seed_review_hunks_perf.py` — copy-from-main suggestion
  builds with carry-over rows.
- `backend/scripts/profile_agents_page.py` — wall time + SQL statement count +
  payload size per endpoint; runs on both pre- and post-fix code.
