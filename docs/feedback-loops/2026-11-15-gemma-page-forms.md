# Feedback Loop — a weaker model's dashboards fail on how they reach the runtime helpers

**Status: IMPLEMENTED and VERIFIED (2026-11-15)** against a real Gemma 4 31B (an Azure-hosted,
vLLM-served FP8 build) through the full app, and by replaying real failed attempts. NOT run on an
MLX-served Gemma 26B or on any customer deployment.

## What was observed

Same request in every case: a data question, then "create dashboard".

| Model and server | Result before | What the model wrote |
|---|---|---|
| Gemma 4 31B, vLLM | 15 failed `create_artifact` calls in one turn, ~900k tokens, still retrying when stopped | `const { KPICard, useArtifactData, vizById } = bow;` then `bow.useArtifactData()`, `<bow.KPICard>` |
| Gemma 4 26B, MLX | 5 failed calls, then "I'm working on a fix" | `import { … } from 'bow'`, `export default App`, no mount call |
| Gemma 4 31B (earlier report) | saved "successfully", then "Dashboard failed to render" | `const { useState, KPICard } = React;` |

One mistake in three spellings: the model assumes the page's building blocks live in a namespace.
In the page runtime they are bare globals; `bow` is a real object, but it only held the SDK features
(`bow.data`, `bow.records`, …) — which is what the authoring reference documents, and plausibly
why the model reaches for it.

Errors seen in the 15-call run: `useArtifactData is not a function` (7),
`bow.useArtifactData is not a function` (3), `bow.setTheme is not a function` (3),
`setTheme is not a function` (1), one JSX syntax error.

## The change

| Piece | Where |
|---|---|
| Helpers also readable through `bow` (additive: non-enumerable getters, an SDK member is never replaced, bare globals untouched, no runtime version bump) | `frontend/public/libs/artifact-globals.js` (`BOW_HELPER_NAMES`); cache token `revision=bow-helpers-1` |
| `import` / `export` removed before checking, imports handled exactly as the runtime already handles them; a page that defines `App` and has no mount call gets the mount line | `backend/app/ai/tools/implementations/_artifact_refs.py` (`normalize_page_code`), called from `create_artifact.py` and `edit_artifact.py` |
| Render errors about a missing name say which global to use | `_artifact_refs.py` (`render_error_hint`) |
| Four consecutive failed builds end the turn with an honest message; the second adds a note | `backend/app/ai/agent_v2.py` (`ARTIFACT_BUILD_STOP_AT`) |
| One worked example and "helpers are globals" in the authoring reference | `backend/app/ai/agents/planner/data_app_authoring.py`, `_sandbox_context.py` |

Existing dashboards: stored versions are never rewritten; every repair returns code that already
runs byte-for-byte (asserted over every page-shaped script already in the repo); the backend's
import handling emits the same declarations the runtime does.

## Loop A — deterministic

```bash
cd backend
export BOW_DATABASE_URL="sqlite:///db/app.db" TESTING=true
uv run pytest tests/unit/test_artifact_page_normalization.py tests/unit/test_artifact_react_destructure.py -q
uv run pytest tests/e2e/test_artifact_page_normalization.py tests/e2e/test_artifact_render_unchecked.py \
  tests/e2e/test_visualization_id_resolution.py -m e2e --db=sqlite -q
cd ../frontend && node tests/unit/artifactBowHelpers.mjs
```

Observed: unit `128 passed, 22 skipped` (the skips are repo pages that the repairs exist for),
e2e `104 passed` together with `test_artifact_automatic_check.py`, and the runtime test `OK` on all
three page shapes (bare globals, destructured from `bow`, `bow.<name>` everywhere).

Can it fail? Without the runtime change the new runtime test fails on its first `bow` assertion
while the bare-globals page still renders. An earlier version of the cleanup matched `ReactDOM` as
`React`; a unit test caught it.

## Loop B — real Gemma through the real app

Local stack (backend, Nuxt dev frontend, real server-side browser), custom LLM provider pointed at
the hosted model, two chat messages per conversation sent through the UI.

| | Before | After |
|---|---|---|
| Conversations | 1 | 6 |
| Dashboard built on the first `create_artifact` call | 0 | 6 |
| `create_artifact` calls in the dashboard turn | 15+ (all failed) | 1 each |
| Tokens for the dashboard turn | ~810k | ~92k |
| Dashboard renders its rows in the browser | no | 6 of 6 |

In all six "after" runs the model wrote the supported form (no `bow`, no `import`). So the worked
example and guidance changed the outcome; the runtime change and cleanup were not exercised live.
They are verified by replay instead.

## Loop C — replaying real failed attempts, unmodified, through the new code

| Source | Before | After |
|---|---|---|
| 15 attempts from the 31B run | 0 pass | 14 pass as written; 1 is a genuine JSX tag mismatch, still rejected |
| 5 attempts from the 26B MLX run | 0 pass, all stopped at `import`/`export` | all get past `import`/`export`/mount; 4 then fail on a real bug in the model's own code (`data.albumsViz.rows`), 1 passes |

## Loop D — the retry cap, scripted model

`run.sh <out> capped broken` in `2026-11-15-viz-id-resolution/`: a scripted model resubmits a page
that always throws. Observed: 4 failed builds, then the turn ends in 57s with
"I could not build the dashboard: it failed 4 times in a row, so I stopped retrying. Nothing was
saved. Last error: …".

## Not fixed

- **Garbled tool arguments** from some Gemma 4 servers (a key such as
  `Title<|"|>,visualization_ids`): the title and chart ids never arrive. Seen on the MLX 26B, not
  on the vLLM 31B. Upstream parser bugs of this family are open (vLLM #51284 and others).
- **Icon components imported by name** (`<Search />` from `lucide-react`): still undefined, and the
  hint for it suggests the wrong component.
- **A model that fails one to three builds and then claims success**: the turn is still recorded as
  a success. The cap only covers four failures.
- The hosted Google 31B deployment could not be tested: its 16k context is smaller than the app's
  prompt.
