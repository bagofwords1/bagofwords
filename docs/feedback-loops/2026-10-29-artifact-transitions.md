# Feedback Loop — "Transitions in artifacts are quite bad. Modals, tabs etc."

Reported: opening a modal, switching tabs and closing panels in generated
artifacts (dashboards / data apps) looks janky. Claim validated here: the jank
comes from the artifact runtime (`frontend/public/libs/artifact-globals.js`),
not from the AI's individual code, so it can be fixed once for every artifact.

## Root cause (validated)

1. **Every chart is fully redrawn on any state change.**
   `EChart` called `chart.setOption(option, true)` (notMerge) whenever the
   `option` prop changed identity (`artifact-globals.js`, EChart's option
   effect). Artifact code builds `option` inline, so it is a new object on
   every render of the parent. Opening a modal, switching a tab or typing in a
   search box re-rendered the page, and every chart replayed its intro
   animation, including the ones behind the modal.
   Observed in Loop A: 3 unrelated clicks → 3 extra full rebuilds (4 vs 1).
2. **No modal, drawer or tabs components in the kit**, so the AI hand-rolls them
   as `{open && <div className="fixed inset-0 …">}` and `{tab === 'x' && …}`.
   - They pop in and vanish instantly, because the element is unmounted before
     any exit animation could run.
   - There is no focus trap and no scroll lock, and Escape does nothing. In the
     evidence run, Escape did not close the hand-rolled drawer.
   - Tabs unmount their panels, so every switch re-creates the charts
     (`echarts.init`) and replays them.
3. **Ruled out: Tailwind's in-browser compiler.** A class first used when a
   modal opens is compiled before the first frame (probe:
   `getComputedStyle` in the next `requestAnimationFrame` already has the
   generated rule), so there is no unstyled flash.

## Loop A — deterministic reproduction (real Chromium, no backend)

```bash
bash scripts/download-vendor-libs.sh          # vendored libs (once)
cd frontend && yarn install --frozen-lockfile
node tests/unit/artifactMotion.mjs
# If the pinned Playwright's own browser is missing (cloud sandbox):
PW_CHROMIUM=/opt/pw-browsers/chromium node tests/unit/artifactMotion.mjs
```

The test renders artifacts through the real `buildArtifactIframeHtml`, served
from a tiny local HTTP server, and counts `echarts.init` and `setOption` calls.

Before the fix:

```
AssertionError [ERR_ASSERTION]: unrelated re-renders never rebuild the chart (no replayed intro animation)
  actual: 4,
  expected: 1,
```

The kit section then fails with `ReferenceError: Tabs is not defined`.

## The fix

All of it is in `frontend/public/libs/artifact-globals.js`, plus prompt docs.

- **EChart** compares a content signature of each new option (functions by
  source):
  - same content → no redraw (formatters are refreshed silently);
  - same shape, new data → merge, so the marks animate to their new values;
  - different shape → full rebuild, as before, so nothing stale survives.
  - Charts in hidden panels (0×0) are no longer resized to nothing.
- **New kit globals** `Modal`, `Drawer`, `Tabs`, `usePresence`:
  - Modal and Drawer stay mounted while they animate out. They trap Tab, close
    on Escape (topmost overlay only) and on a backdrop click, restore focus to
    the trigger, lock page scroll without a sideways jump (`scrollbar-gutter`),
    and honour `prefers-reduced-motion`. Drawer `side="end"` follows RTL.
  - Tabs: sliding indicator, panel fade-in, roving tabindex with arrow, Home
    and End keys (mirrored in RTL). A panel mounts on its first visit and then
    stays mounted, so its chart instance survives switching back.
- The planner prompt (`_sandbox_context.py`, `data_app_authoring.py`) teaches
  these components. `read_artifact`'s outline lists them as landmarks.
- The cache-buster `revision=imports-1` became `motion-1`. Note that
  `ARTIFACT_GLOBALS_VERSION` is tied to the runtime version, and bumping it
  would flip v11 artifacts to the legacy look.

After the fix: `artifactMotion: OK`.

A regression found during the loop was fixed and guarded. The first `Tabs`
created its `ResizeObserver` in an effect with no dependencies. Each render's
new observer re-measured and re-rendered at frame rate, which starved real
mouse input (Playwright clicks never returned). The observer is now created
once. The test asserts that an idle tab bar does not re-render, and fails when
the loop is reintroduced.

## Visual verification

Throwaway harness (not committed), one demo dashboard rendered in two
versions.

- **Hand-rolled** (how the AI writes it today), on the base runtime.
- **Kit components**, on the new runtime, captured in light, dark, RTL, at
  400px and with the legacy (pre-v11) look.

Evidence is in `media/pr/artifact-transitions/`.

## What this proves / regression notes

- Unrelated re-renders cost zero chart work. Data changes morph, structural
  changes rebuild.
- The overlay and tabs behaviour (enter/exit, focus, Escape, scroll lock, RTL,
  lazy-then-kept panels) is covered by `frontend/tests/unit/artifactMotion.mjs`.
- All other artifact Chromium tests still pass (`artifact*.mjs`,
  `polishPicker.mjs`, `dashboardViewing.mjs`). They run with a Playwright that
  matches the installed Chromium.
- Pre-existing and unrelated: `backend/tests/unit/test_artifact_precompile.py`
  has 4 failures in this sandbox both with and without this change.
- Existing artifacts benefit from the EChart fix immediately. Modal, Drawer and
  Tabs only appear in artifacts generated or edited after this ships.
