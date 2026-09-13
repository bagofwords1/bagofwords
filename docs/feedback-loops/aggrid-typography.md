# Feedback loop — tiny AG Grid text with disproportionate padding

The dark-mode palette fix exposed a dormant 9px font token in both query-result and dashboard tables. Rows remained 28px tall, making the text look undersized relative to the spacing.

## Root cause

`frontend/components/AgGridComponent.vue:147` re-inherits `--ag-font-size` from its wrapper. The declarations in `frontend/components/RenderTable.vue:77` and `frontend/components/dashboard/table/TableAgGrid.vue:73` supplied 9px, overriding Balham's previous effective 12px default.

## Runnable reproduction

With frontend dependencies and Playwright Chromium installed, run from the repository root:

```sh
node docs/feedback-loops/aggrid-typography/verify.cjs after
```

The harness renders the installed AG Grid library with the actual three components' styles and each wrapper's font token, using synthetic genre rows. It requires no backend, credentials, or listening port. It checks both wrappers in light and dark mode.

Before the fix, running the same harness with the `before` screenshot label failed: `9px !== 12px`. All four cases measured 9px text, 28px rows, and 33px headers including the bottom border.

## Fix and verification

Both wrappers now declare 12px and use `text-xs` instead of `text-[9px]`. All four cases pass: 12px text, 28px rows, 33px headers, and matching light/dark foreground and background colors. The dark-mode palette and tinted headers are preserved.

Evidence: `media/pr/aggrid-typography/{before,after}-{light,dark}.png`.

This is an isolated browser CSS/runtime fixture, not a full authenticated app test. It verifies typography, dimensions, and theme colors; it does not exercise query execution or custom header interactions.
