# Feedback Loop — "If I refresh /reports/[id] the SSE disconnects and starts polling"

Validates the reported behavior and its fix: the completion SSE stream only
existed as the body of the kickoff `POST /reports/{id}/completions` request.
Any interruption — page refresh, network blip, proxy timeout, backgrounded
mobile tab — permanently lost the live stream. The page degraded to 1.2s
polling that showed almost no progress (partial planner text was never
persisted) and silently gave up after 2 minutes; a mid-stream network error
marked the run as failed even though the agent kept running server-side.

## Root cause (validated)

1. **Create and watch were one request.** The stream was the response body of
   the kickoff POST (`frontend/pages/reports/[id]/index.vue` `startStreaming`,
   `backend/app/services/completion_service.py` `create_completion_stream`).
   There was no endpoint to watch a completion you didn't just create, so
   nothing could be retried after a disconnect.
2. **The event queue was request-local.** `CompletionEventQueue` was a plain
   single-consumer `asyncio.Queue` created inside the request and registered
   nowhere; after a disconnect the agent's events piled into an orphaned queue.
3. **Partial planner text was never persisted.**
   `agent_v2._persist_partial_decision_text` was dead code (zero call sites),
   so the polling fallback rendered "…" for the whole planning phase.
4. **No heartbeats, no stall detection.** Quiet streams (long tool runs) sent
   zero bytes; clients could not distinguish a quiet stream from a dead one,
   and a silently-dead mobile connection hung forever.
5. **Polling gave up after 2 minutes** (`maxDurationMs` in
   `startPollingInProgressCompletion`) while runs can last much longer.

## Loop A — reproduction and verification (Playwright, sandboxed)

Setup (fresh sandbox):

```bash
tools/agent/boot_stack.sh --dev
# Stable key so restarts keep tokens/credentials valid:
export BOW_ENCRYPTION_KEY=$(cd backend && uv run python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
cd backend && uv run python ../tools/agent/seed_org.py
# LLM: either the deterministic slow stub…
uv run python ../tools/agent/slow_stub_llm.py &      # + register as default provider
# …or a real provider (Anthropic key from env) for tool-using runs.
# Data: create a report; for tool runs attach demo-datasources/chinook.sqlite.
# For the network-drop scenario, run the severable TCP proxy:
node ../tools/agent/chaos_proxy.mjs &                # browser -> :3001 -> :3000
```

Reproduce / verify:

```bash
cd frontend && export PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers
REPORT_ID=<id> node ../tools/agent/sse_reconnect_probe.mjs /tmp/evidence refresh
BASE=http://localhost:3001 REPORT_ID=<id> node ../tools/agent/sse_reconnect_probe.mjs /tmp/evidence drop
```

**Observed FAIL (before the fix)** — refresh mid-stream:

```
polling banner visible: true
completions requests after reload:
  GET /api/reports/<id>/completions?limit=10     # every ~1.3s, 11 polls in 12s
  ...
```

UI showed the "Loading… showing recent progress" banner and a "." (then "…")
where the streaming answer had been — for the rest of the run (screenshots:
`media/pr/sse-reconnect/before-*.png`). A TCP sever mid-stream produced the
same degraded state.

**Observed PASS (after the fix)** — same probe:

```
completions requests after reload:
  GET /api/reports/<id>/completions/<completion_id>/stream   # watch stream
  GET /api/reports/<id>/completions?limit=10                 # one snapshot load
text growth after refresh: 10486 -> 15729 (LIVE STREAMING)
polling banner visible: false
```

TCP sever mid-stream: `error UI visible: false`,
`text growth after sever: 7258 -> 13949 (LIVE STREAMING)`, and the run
converged to success (screenshots: `media/pr/sse-reconnect/after-*.png`).

A long-running tool gauntlet (Chinook analysis with `create_data` +
`create_artifact`, hit with a TCP sever during the first tool run and a
refresh later in the run) keeps live-streaming through both disruptions and
converges with all tool blocks intact. Use a FRESH report per run — rerunning
on a report that already holds the results lets the model answer from history
without tools, which fails the tool-count assertion for unrelated reasons.

```bash
# copy .mjs into frontend/ or run with node_modules resolvable
BASE=http://localhost:3001 REPORT_ID=<fresh-chinook-report> \
  node ../tools/agent/sse_chinook_gauntlet.mjs /tmp/evidence
```

Observed PASS:

```
after sever: error UI=false, growth 1966 -> 3022 (LIVE)
after refresh: polling banner=false
final status: success; blocks=4, tool blocks=3, widget/step creations=3
tools used: create_data:success, create_data:success, create_artifact:success
GAUNTLET PASS
```

## Loop B — API-level check (no browser)

```bash
# Kick off a streamed completion and abandon the connection after 6s:
curl -N -X POST http://localhost:8000/api/reports/$RID/completions \
  -H "Authorization: Bearer $TOKEN" -H "X-Organization-Id: $ORG" \
  -H "Content-Type: application/json" \
  -d '{"prompt": {"content": "…", "mentions": [], "mode": "chat"}, "stream": true}' &
sleep 6 && kill %1
# Re-attach — replays a block snapshot, then live token deltas:
curl -N http://localhost:8000/api/reports/$RID/completions/$CID/stream \
  -H "Authorization: Bearer $TOKEN" -H "X-Organization-Id: $ORG"
```

Before: 404 (endpoint didn't exist). After: `completion.resumed` →
`block.upsert` snapshot (with partial content) → live `block.delta.token`
events → `completion.finished` → `[DONE]`.

## The fix

Backend:

- `app/streaming/completion_stream.py`: `CompletionEventQueue` became a
  fan-out broadcaster (bounded per-subscriber queues, non-blocking producer,
  `finish()` terminates all consumers including late subscribers) plus an
  in-process registry keyed by system completion id.
- `app/routes/completion.py` + `completion_service.watch_completion_stream`:
  new `GET /api/reports/{report_id}/completions/{completion_id}/stream` —
  side-effect-free, retryable. Emits `completion.resumed`, replays an
  idempotent `block.upsert` snapshot from the DB, then attaches to the live
  queue (same worker → token-level) or tails the DB (other worker / late
  attach → block-level), and always terminates with `completion.finished` +
  `[DONE]`. Subscription starts before the snapshot read so no event falls in
  the gap; overlap is safe because upserts are idempotent.
- Heartbeats: both streams emit `: ping` comments every 15s
  (`BOW_SSE_HEARTBEAT_SECONDS`) so intermediaries don't reap idle connections
  and clients can detect dead ones.
- `agent_v2._persist_planning_block_partial` + a `persist` hook on
  `PlanningTextStreamer`: partial reasoning/content is now persisted on the
  1.2s snapshot cadence (skeleton block row inserted lazily, completed in
  place by `upsert_block_for_decision` at decision.final; deleted if the
  planning attempt is cancelled), so DB-based resume paths show live text.

Frontend (`pages/reports/[id]/index.vue`):

- On mount with an in-progress completion: open the watch stream (was:
  polling). Watch reconnects with backoff until `[DONE]`, then converges via
  one `loadCompletions()`.
- Kickoff-stream network errors now recover via the watch stream instead of
  marking the run failed; if the POST died before `completion.started`, the
  in-progress completion is found via the API and adopted.
- Stall watchdogs (45s without bytes → abort → reconnect) on both streams,
  plus a `visibilitychange` handler for backgrounded mobile tabs.
- Polling remains only as a last-resort fallback (watch endpoint unreachable
  5 times in a row); its 2-minute give-up cap is gone and it backs off
  1.2s → 5s.

## What this proves / regression notes

- Regression tests: `backend/tests/unit/test_completion_stream_broadcaster.py`
  (fan-out/termination/non-blocking/heartbeat/registry contracts) and
  `backend/tests/e2e/test_completion_watch_stream.py` (watch endpoint replay,
  status propagation, 404/401) — all pass on sqlite.
- Multi-worker deployments: a reconnect landing on a worker that doesn't own
  the run gets the DB-tail path — block-level updates at ~0.7s cadence
  (`BOW_WATCH_TAIL_INTERVAL_SECONDS`) instead of token-level typing. Same
  events, same convergence; no cross-process bus required because blocks are
  already persisted incrementally.
- Pre-existing unrelated failures: `tests/e2e/test_completion.py::
  test_completion_background` and `::test_completion_streaming` fail in this
  sandbox with "OPENAI_API_KEY_TEST is not set" (env-gated in
  `tests/fixtures/llm.py`, fails before reaching any code touched here).
