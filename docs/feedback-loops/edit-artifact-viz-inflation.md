# Feedback Loop — /r/{id} artifact page "loads data/steps/viz that are not in use in the artifact"

The public artifact view (`frontend/pages/r/[id]/index.vue`) drives its whole
data waterfall off `artifact.content["visualization_ids"]`
(`GET /r/{id}/queries?artifact_id=…` → one full `/step` payload per returned
query, all injected into the iframe's `ARTIFACT_DATA` and listed in the Data
tab). The claim validated here: **`edit_artifact` inflates that ID list with
visualizations the dashboard code never uses**, so every public page view
downloads step payloads (and shows Data-tab entries) for charts that are not
on the dashboard.

## Root cause (validated)

`backend/app/ai/tools/implementations/edit_artifact.py`, viz-ID merge block
(~lines 658-682):

1. **Auto-merge** — every `Visualization` in the report with
   `created_at > artifact.created_at` is merged into the edited artifact's
   ID list, whether or not the edit (or the resulting code) references it.
   Any `create_data` call made after the artifact exists gets its viz
   permanently attached by the next edit — even a purely cosmetic one.
2. **No prune** — nothing ever removes an ID, so the list only grows across
   versions.

Contrast: `create_artifact` (implementations/create_artifact.py:916-918)
stores exactly the requested+valid IDs, and the MCP variant
(`app/ai/tools/mcp/edit_artifact.py:131-136`) merges only explicitly passed
IDs — the leak is specific to the planner's `edit_artifact`.

Trigger condition (confirmed in Loop B, turn T4): the auto-merge fires only
when report vizs are **newer than the artifact being edited**. An edit on a
freshly rebuilt artifact stays clean; an edit on any artifact older than the
latest `create_data` calls inflates.

Impact scale: with the chinook demo the wasted payloads are kBs, but step
payloads are full query result sets — see
`docs/feedback-loops/artifact-large-data-perf.md`, where a single `/step` is
~2.4 MB. The multiplier (unused/used) is what matters, not the absolute kB.

## Loop A — deterministic reproduction (no LLM, no live services)

`backend/tests/e2e/test_edit_artifact_viz_inflation_repro.py` seeds
10 query+viz pairs (viz == query, 1:1), an artifact built from the first 3
(the code references exactly those IDs), then 7 more vizs created *after*
the artifact. It runs the real `EditArtifactTool.run_stream` with the LLM
stubbed to a single SEARCH/REPLACE diff renaming the dashboard title — an
edit that adds nothing — and then replays the public page's waterfall.

```bash
cd backend
pip install uv && uv sync --frozen --extra dev --python /usr/bin/python3.12
BOW_DATABASE_URL=sqlite:///db/app.db \
  .venv/bin/python -m pytest tests/e2e/test_edit_artifact_viz_inflation_repro.py -s --runxfail
```

**Observed (FAIL — the leak):**

```
[inflation] artifact v1 visualization_ids: 3 (all referenced by code)
[inflation] artifact v2 visualization_ids: 10 (3 referenced by code, 7 never referenced)
[inflation] later (auto-merged) vizs attached: 7/7
[inflation] public /queries?artifact_id= returned 10 queries (dashboard uses 3)
[inflation] /step payload downloaded by /r page: 75.0 kB (needed: 22.5 kB — 3.3x)
AssertionError: edit_artifact attached 7 visualization(s) the artifact code
never references (auto-merge leak)
```

The test asserts the desired invariant (a cosmetic edit must not grow the
visualization set; the public page must only load steps in use) and is
marked `xfail` until the fix lands — **remove the xfail marker when fixing**.

## Loop B — live confirmation (full stack, chinook, real LLM)

Real agent, real codegen. Requires an Anthropic key in the environment —
never hardcode or commit it.

```bash
tools/agent/boot_stack.sh --dev
bash scripts/download-vendor-libs.sh frontend/public/libs   # artifact iframe libs

export ANTHROPIC_API_KEY_TEST=<key>          # env var only
cd backend && uv run python scripts/repro_edit_artifact_viz_inflation_live.py
```

The script drives three chat turns: T1 create 3 vizs + dashboard; T2 create
7 more vizs ("do NOT touch the dashboard"); T3 cosmetic edit ("rename the
title — the ONLY change").

**Observed (exit 1 — leak reproduced):**

```
[T1] artifact v1 visualization_ids=3
[T3] (edit_artifact) →
[result] artifact v2 after cosmetic edit:
[result]   visualization_ids: 10 (was 3 before the edit)
[public] GET /r/{id}/queries?artifact_id= returned 10 queries
[public] /step payload the /r page downloads: 26.9 kB (dashboard renders 3 queries, 8.4 kB — 3.2x)
LEAK REPRODUCED
```

Tool trace for T3 (from `tool_executions`): `edit_artifact success` (creates
the inflated v2) → `edit_artifact error` → `create_artifact success`. The
accidental rebuild produced a *newer* 3-ID artifact, which is what the /r
page then picked — the planner's fallback masked the leak in this
particular run. Two follow-up turns removed the luck:

- **T4** — cosmetic edit on the freshly rebuilt artifact → v2 with **3** IDs
  (no vizs newer than the artifact ⇒ no auto-merge; confirms the trigger).
- **T5** — create 2 more vizs ("do NOT touch the dashboard");
  **T6** — cosmetic edit (title color) →

```
d73356db v3 'Chinook Executive Overview' viz_ids=5   ← what /r now uses
/r page fetches 5 step payloads, 13.1 kB (dashboard renders 3)
```

Browser-side (headless Chromium against the dev stack):

```bash
NODE_PATH=frontend/node_modules FRONT=http://localhost:3000 REPORT=<id> \
  node backend/scripts/pw_viz_inflation_repro.js
```

```
/step requests: 5, total 12.8 kB     ← page renders 3 charts
```

and the page header reads **"Data (5)"** while the dashboard shows 3 charts —
the 2 auto-merged vizs surface in the user-facing Data tab too, not just in
network traffic.

## The fix

Not applied yet (this doc records the reproduction). Direction discussed:

1. Drop the "auto-merge every report viz newer than the artifact" block in
   `implementations/edit_artifact.py`; only merge IDs explicitly passed via
   `visualization_ids` (the input schema already instructs the planner to
   pass new IDs — same contract as the MCP variant).
2. Prune: derive the final list from what the edited artifact actually
   retains (or at minimum stop growing it on cosmetic edits).
3. Defense in depth for historical artifacts: the public endpoint /
   frontend currently fail *open* (empty or unresolvable
   `visualization_ids` ⇒ all report queries are returned; unmatched vizs
   are appended, not dropped, in `index.vue`).

After fixing, remove the `xfail` marker from
`tests/e2e/test_edit_artifact_viz_inflation_repro.py` and re-run both loops:
Loop A must pass; Loop B must print `INVARIANT HOLDS` (exit 0).

## What this proves / regression notes

- The inflation needs no data change at all: a title rename attaches every
  visualization created since the artifact — 7/7 in Loop A, 7/7 in Loop B
  T3, 2/2 in T6.
- The cost is per-public-page-view: one extra full `/step` download per
  unused viz, plus phantom entries in the Data tab and dead weight in the
  iframe's `ARTIFACT_DATA`.
- `edit_artifact` failures during T3 (second edit errored → planner rebuilt
  via `create_artifact`) are planner nondeterminism, not part of the leak;
  the leak reproduces deterministically in Loop A.
