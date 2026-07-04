# Sandbox Feedback Loop — Tables Selector "Save button only shows when scrolling"

Reproduces and validates the reported bug: in the agent **Tables** panel
(Sales › Tables, "0 / 2628 active"), the **Save button is only visible after
scrolling the panel to the bottom**. It needs to be frozen (pinned) so it is
always visible.

This doc is the runnable feedback loop used to confirm the cause and verify the
fix with Playwright in a fresh cloud sandbox.

---

## Root cause (validated)

The Save bar in `frontend/components/datasources/TablesSelector.vue:465` is a
**normal in-flow element rendered after the table list**:

```html
<!-- Save button -->
<div v-if="showSave && canUpdate" class="mt-3 flex items-center justify-end">
```

`TablesSelector` is mounted inside a scroll container — e.g.
`KnowledgeExplorer.vue:341` (`<div class="flex-1 overflow-auto">`) — and the
inner table list has its own cap (`TablesSelector.vue:259`,
`max-height: calc(100vh - 240px)`). Once the component chrome (header, the
Connections/Reload row, search, stats, pagination) plus the capped list exceed
the panel height, the **last in-flow child — the Save bar — is pushed below the
fold**. It only appears when the outer container is scrolled down. This is the
behavior in the report.

---

## The fix

Pin the Save bar to the bottom of its scroll container so it never leaves the
viewport, regardless of list length or scroll position
(`TablesSelector.vue:465`):

```diff
- <div v-if="showSave && canUpdate" class="mt-3 flex items-center justify-end">
+ <div v-if="showSave && canUpdate" class="sticky bottom-0 z-10 mt-3 flex items-center justify-end border-t border-gray-100 bg-white py-2">
```

`sticky bottom-0` pins it to the bottom of the nearest scrolling ancestor; the
solid `bg-white` + `border-t` keep the scrolling list from showing through. One
self-contained change fixes every embed of `TablesSelector` (agents wizard
schema step, old-agents tables, `KnowledgeExplorer`, `ReportAgentPanel`,
`NewAgentWizardModal`, onboarding).

---

## Verification loop (Playwright, no backend needed)

The bug is CSS layout only, so the loop runs against a static harness
(`/tmp/sticky-verify/harness.html`) that reproduces the **exact panel DOM** —
outer `flex-1 overflow-auto` scroll container, the `TablesSelector` chrome, the
`max-height: calc(100vh - 240px)` list, and the Save bar. `?sticky=1` toggles
the fix on, so the same harness drives both the broken and fixed cases.

### Environment

A Playwright browser is pre-provisioned at `/opt/pw-browsers`
(`chromium-1194`, matching `@playwright/test@1.56.x`). Browser downloads are
blocked in the sandbox, so point Playwright at that path and use the matching
version:

The harness + specs live in `tools/sandbox/save-button-sticky/`
(`harness.html`, `sticky.spec.ts`, `shots.spec.ts`).

```bash
mkdir -p /tmp/sticky-verify && cd /tmp/sticky-verify
cp <repo>/tools/sandbox/save-button-sticky/* .
npm i @playwright/test@1.56.1
export PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers
```

### Run

```bash
cd /tmp/sticky-verify
PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers npx playwright test --reporter=list
```

**Observed (PASS, 5/5):**

```
✓ panel overflows so the save bar is below the fold (precondition)
✓ BROKEN (non-sticky): save button is NOT visible at scrollTop=0
✓ FIXED (sticky): save button stays visible at every scroll position
✓ shot broken   -> before-broken.png  (no Save button visible at top of panel)
✓ shot fixed    -> after-fixed.png    (Save & Continue pinned bottom-right)
```

`saveButtonVisible()` checks the button's bounding box is inside the scroll
container's visible box. The BROKEN spec asserting `false` and the FIXED spec
asserting `true` at scrollTop `[0, mid, bottom]` together prove the test
discriminates and the fix works.

### What this proves

- The panel genuinely overflows (precondition test) — the bug is real, not an
  artifact.
- Without `sticky`, the Save button is off-screen at the top of scroll
  (reproduces the report).
- With `sticky bottom-0`, the Save button is in-viewport at every scroll
  position (fix confirmed), as captured in `after-fixed.png`.
