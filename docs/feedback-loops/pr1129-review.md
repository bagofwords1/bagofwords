# PR #1129 review — runtime compatibility

Reviewed commit `3e32c09db2e88815bd2b9910db84ddd71d145d7f` against `origin/main`.
Historical review snapshot. These findings were subsequently addressed locally; see
[the implementation feedback loop](artifact-data-app-generation.md). No GitHub comments were submitted.

## Findings

### P1: Validation and thumbnail shells initialize the legacy runtime for v11 data

`frontend/public/libs/artifact-globals.js:43` freezes `LEGACY` at script evaluation.
`CreateArtifactTool._build_thumbnail_html` loads `page_scripts` in the head
(`backend/app/ai/tools/implementations/create_artifact.py:305`) but assigns
`window.ARTIFACT_DATA` later in the body (`:315`). Those scripts already include
the globals. `backend/app/services/thumbnail_service.py` has the same order.
Neither assigning the payload nor calling `setTheme` recomputes `LEGACY`.

Observed in Chromium with the same runtime, React, Tailwind, ECharts and card code:

| Data timing | Legacy | fmt(0.42, {pct:true}) | KPI comparison/sparkline |
|---|---|---|---|
| Before globals (live iframe order) | false | 42.0% | Visible |
| After globals (validation/thumbnail order) | true | 0.4% | Missing |

The wrong preview is supplied to the planner's validation/repair loop, and gallery
thumbnails differ from the live artifact. Both cases ran without JavaScript errors.
Initialize the payload before loading globals, or defer all version-dependent
initialization until the payload exists. The standalone HTML export already seeds
data before its separate globals script.

### P2: MCP paths do not create, preserve, or deliver the runtime stamp

The registered MCP `create_artifact` tool reuses the newly themed page prompt
(`backend/app/ai/tools/mcp/create_artifact.py:335`) but saves only code and
visualization IDs (`:189`). MCP `edit_artifact` also rebuilds content with those
two fields (`backend/app/ai/tools/mcp/edit_artifact.py:287`), dropping the v11 stamp
from a previously themed artifact. Therefore these rows render using legacy
component/formatter semantics even in the regular app.

The MCP viewer additionally drops runtime metadata: `get_artifact_data` in
`backend/app/ai/tools/mcp/app_tools.py` does not return it, and
`frontend/public/mcp-artifact-app.html:354` constructs a payload without it.
Its globals also load before data arrives. Stamp MCP creates, retain the source
version on edits, and propagate it through the viewer with corrected initialization.
This finding is established by the registered tool implementations and the runtime
reproduction; no live MCP/LLM request was made.

### P2: Legacy artifacts are not visually isolated from the new defaults

`frontend/public/libs/artifact-globals.js:378` applies the new ECharts theme to
legacy artifacts as well. The browser comparison for an unstamped artifact shows
line `smooth` changing from true to false, `showSymbol` from true to false, and
grid margins from `{left:40,right:20,top:20,bottom:40}` to
`{left:8,right:12,top:24,bottom:8}`. EChart's default height also changes from 400
to 320 without a legacy guard. Separately, the unconditional `#root h1` rule at
`:306` overrides a stored `font-bold` heading from weight 700 to 600.

Keep pre-v11 chart defaults and base typography behind the legacy branch. Copying
the old palette alone does not preserve saved artifact appearance.

## Reproduction

Run `node docs/feedback-loops/pr1129-review/reproduce.cjs` from this worktree.
The harness uses the installed Playwright and vendor libraries in the original
checkout, and the PR runtime from `/private/tmp/bow-pr1129`. It renders a small
synthetic artifact, once for each initialization order and once for each legacy
runtime. It does not need a backend, database, network, or credentials. Chromium
must be allowed to launch. Browser output is recorded in
`pr1129-review/results.json`; the script also writes fresh copies under `/private/tmp`.

Screenshots: [correct initialization](pr1129-review/data-first.png),
[late initialization](pr1129-review/data-after.png),
[legacy baseline](pr1129-review/legacy-base.png),
[legacy on PR](pr1129-review/legacy-pr.png).

## Tests

14 new design-system tests passed. The wider focused unit selection passed with
107 passed and 1 skipped:

```sh
cd backend
BOW_DATABASE_URL=sqlite:////private/tmp/pr1129-tests.db PYTHONWARNINGS=ignore PYTHONPATH=. \
  /Users/yochze/Desktop/bagofwords/backend/.venv/bin/python -m pytest \
  tests/unit/test_artifact_design_system.py tests/unit/test_artifact_refs.py \
  tests/unit/test_artifact_params_wiring.py tests/unit/test_artifact_feedback_loop.py \
  tests/unit/test_artifact_parse.py tests/unit/test_artifact_viewer_identity.py \
  tests/unit/test_validation_runtime_parity.py tests/unit/test_artifact_loop_guard.py \
  tests/unit/test_read_artifact_windowing.py -q --confcutdir=tests/unit --disable-warnings
```

Initial collection without `BOW_DATABASE_URL` failed configuration loading; supplying
the isolated SQLite URL resolved it. These are focused unit checks with the root
database fixture excluded, not a full-stack or live LLM QA pass. The current tests
do not catch the observed initialization or visual-compatibility regressions.
