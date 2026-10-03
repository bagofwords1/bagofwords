# Feedback Loop — answer arrives but the report stays “Thinking”

An active completion must continue accepting SSE events when its optimistic ID
is replaced by the persisted ID. Superseded streams must not reset a newer
stream's controller or loading state.

## Root cause (validated)

At base `8d25044c5`, `frontend/pages/reports/[id]/index.vue:5112` closes over the
optimistic `sysId`. `loadCompletions` at line 4072 replaces message objects with
persisted IDs. Later SSE events fail the ID lookup and are silently dropped.
The unconditional cleanup at line 5233 also clears shared state after a newer
stream starts. An older `[DONE]` can initiate a stale refresh.

## Loop A — deterministic, no server or credentials

Install frontend dependencies, then from the repository root:

```sh
node --test frontend/tests/unit/kickoff-stream.test.mjs
```

The test executes the actual page consumer and refresh function with controlled
network responses. It varies canonical IDs, replaces message objects, and
interleaves old EOF, error and DONE with a newer stream.

Against the base page: **6 failed, 0 passed**. With the fix: **6 passed, 0 failed**.
To reproduce against a separate base checkout without changing this worktree:

```sh
BOW_REPORT_PAGE=/path/to/base/frontend/pages/reports/\[id\]/index.vue \
  node --test frontend/tests/unit/kickoff-stream.test.mjs
```

## Loop B — real browser, controlled SSE

Boot the local stack, seed a throwaway user and an empty report. Supply credentials
through environment variables. Run on the base and fixed frontends respectively:

```sh
node tools/agent/verify_stream_identity.mjs http://127.0.0.1:3000 REPORT_ID before
node tools/agent/verify_stream_identity.mjs http://127.0.0.1:3001 REPORT_ID after
```

Required environment: `BOW_TEST_EMAIL`, `BOW_TEST_PASSWORD`.
The script uses Vue's development setup state to simulate a canonical message
replacement, while the actual page consumes controlled SSE bytes through its
normal fetch path. It sends no model request and does not save synthetic answers.
This is a development-server test, not a production-bundle integration test.

Observed on a fresh local report:

| Version | Message status | Answer blocks | Browser errors |
|---|---|---|---|
| Before | in_progress | 0 | 0 |
| After | success | 1 | 0 |

Screenshots, short GIFs and machine-readable results are in
`media/pr/stream-message-identity/`. The first browser attempt used an existing
dashboard and encountered missing local artifact assets on the new frontend;
the final evidence uses an empty report to isolate this conversation behavior.
Artifact rendering was not validated by this loop.

## Fix

- Retain the canonical ID in the stream and resolve both identities.
- Fence stream dispatch, error recovery, watchdog cleanup and finalization by
  the originating controller; fence watcher callbacks by their generation.
- Defer conversation snapshot replacement while kickoff owns the timeline.
  Ignore older refresh responses when a newer refresh has started. DONE reloads
  canonical state after releasing stream ownership.
- Recover interrupted streams by canonical ID even after hydration.

## Scope

Vue script/template compilation passes. This proves event delivery, refresh
protection and stream ownership. It does not measure provider latency, full
artifact correctness or every browser/network recovery scenario.
