# Wizard Visual expansion and long table names

The reported wizard expansion put the diagram behind the modal overlay. Table and source names also truncated too early. The wizard's primary action should sit on the left, immediately followed by Cancel.

## Validated causes

- `frontend/components/datasources/TablesSelector.vue:2`: standalone full screen teleported the selector to `body`. In the wizard this escaped the containing Headless UI dialog and its focus trap. The previous production build failed the assertion that the expanded canvas belongs to the wizard.
- `frontend/components/datasources/TableCanvasNode.vue:12`: single-line truncation and a 280px node width limited both names. The previous build rendered the synthetic long table name on one 17px line; the two-line assertion failed.
- `frontend/components/NewAgentWizardModal.vue:2`: changing the Nuxt UI modal width also reapplied its entrance classes. Browser screenshot review found a transparent panel even though ordinary Playwright visibility checks passed. This wizard disables the panel transition; the regression now checks ancestor opacity as well.

- The delayed-response regression also exposed the earlier loading overlay crossing over the filter popover. The diagram container now establishes a local stacking context (`TablesSelector.vue`), keeping the overlay below toolbar menus.

## Fix

The wizard grows to 1152px on the table step and nearly the viewport width when expanded. Its selector stays inside the same modal. The wizard keeps its own Save & Continue and Cancel controls and reserves space for them below the canvas. Dropdown Escape closes the dropdown first; a subsequent Escape inside the selector exits expansion.

Cards use shared 360×224 geometry for rendering, layout and viewport fitting. The initial minimum zoom is reduced to keep wider neighboring cards in view. Table and source names each allow two lines; the full table name remains available in its title. Selected connection chips also allow longer names. Primary actions precede Cancel on the physical left, including Hebrew.

## Runnable loop

Use the isolated seeded stack from [the ERD feedback loop](tables-selector-erd-canvas.md) and its synthetic agent/source fixture. Do not use customer data. Authentication remains in a private seed JSON outside the repository. The test creates and removes a synthetic wizard agent through the API and stubs the LLM-sync boundary.

```bash
cd frontend
export PLAYWRIGHT_BASE_URL=http://127.0.0.1:3113
export BOW_ERD_SEED=/private/tmp/bow-tables-erd-run/prompts-seed.json
export BOW_ERD_RESULTS=/private/tmp/bow-tables-erd-run/wizard-results
export PLAYWRIGHT_CHROMIUM_EXECUTABLE='/Users/yochze/Library/Caches/ms-playwright/chromium-1193/chrome-mac/Chromium.app/Contents/MacOS/Chromium'
node node_modules/@playwright/test/cli.js test --config playwright.tables-canvas.config.ts --grep 'wizard expands|longer table'
BOW_WIZARD_LOCALE=he node node_modules/@playwright/test/cli.js test --config playwright.tables-canvas.config.ts --grep 'wizard expands'
node --experimental-strip-types tests/unit/tableGraph.mjs
```

For the old production baseline, set `PLAYWRIGHT_BASE_URL` to that server and `BOW_WIZARD_BASELINE=1`. Observed: both tests fail for the causes above. The baseline flag accommodates the old wizard locator and button order; it does not relax the canvas-containment or long-name assertions.

## Evidence and result

Screenshots and flow recording live under `media/pr/tables-erd/`: before/after wizard actions, expanded wizard, and long names, plus Hebrew captures. The initial production run passed 15 scenarios. The exploration test required clearing focus before clicking a now-offscreen table; that corrected test passed separately. The other failure was the loading-overlay defect described above, rather than a delayed-request interception issue. At the user-requested push: production build, 30 locale checks, graph invariants, the wizard (English and Hebrew), long names, and the corrected exploration scenario passed. The final rebuild and rerun for the loading-overlay stacking fix are still pending; no full-green suite is claimed.
