# Feedback Loop — "viewing a CreateData output should open the side menu, full width like ArtifactFrame, with the edit/etc buttons"

A `read_file` card can already open its document in the report's right pane.
Query results (`create_data`, `read_query`, `write_csv`, `describe_entity`,
generic `execute_code`) could not: the only way to see a chart or table was
the inline card, whose chart is fixed at 340px tall inside a ~500px chat
column. This loop proves the new affordance end to end against a real LLM:
the card grows an "Open in side panel" button, the pane renders the same
`ToolWidgetPreview` at full width and height, and every control the card has
(Edit, PNG/CSV/Excel download, filters, params bar, Chart/Data/Code tabs,
Add to Dashboard, Save Query) works from the pane.

## Root cause (validated)

Not a bug — a missing surface. The pieces that existed:

- `frontend/pages/reports/[id]/index.vue` — `rightPanelView` union
  (`grid | artifact | agent | summary | file`) and the `panelFile` /
  `openFilePreview` / `closeFilePanel` trio that `read_file` cards use.
- `frontend/components/FilePreview.vue:6-15` — the opt-in `canExpand`
  button pattern (`heroicons:arrows-pointing-out`) that emits `expand`.
- `frontend/components/tools/ToolWidgetPreview.vue` — the card. It had no
  expand affordance and hard-coded its heights
  (`h-[340px]` chart, `h-[400px]` table, `h-[250px]` code), so even if
  mounted in the pane it would not fill it.

Two real defects surfaced while building the loop and are fixed in the same
change:

1. `ToolWidgetPreview.vue` kept its window listeners on **window globals**
   (`__tw_preview_handlers__`, `__tw_preview_artifact_handler__`). With
   several previews mounted at once, the last one mounted overwrote the slot,
   and any unmount removed *that* instance's listeners while leaking its own.
   The pane instance mounts and unmounts often, so this had to become a
   per-instance listener list.
2. The row label read the **preview's** length. The completions projection
   ships only `PREVIEW_ROWS = 20` rows and marks the data
   `truncated` + `total_rows` (`backend/app/serializers/completion_v2.py:120-190`),
   so a 24-row result said "20 rows" until the full step landed — visible in
   the pane the instant it mounted. The label now reports `total_rows` while
   truncated, with a spinner until hydration completes.

## Loop A — deterministic reproduction (no LLM)

The reproduction is the *absence* of the affordance on the base commit. The
same helper that captures evidence asserts it:

```bash
tools/agent/boot_stack.sh --dev
cd backend && uv run python ../tools/agent/seed_org.py --demo
# any report that already holds a create_data result:
cd frontend && PW_CHROMIUM_PATH=/opt/pw-browsers/chromium \
  node -e "…open /reports/<id>, count [data-testid=widget-open-panel]…"
```

Observed on the base commit (change stashed):

```
expand button present: false html dir: ltr
```

Observed with the change:

```
expand button present: true html dir: ltr
```

## Loop B — live confirmation (real LLM: Claude Haiku 4.5)

The pane only means something on a real result, so the loop drives a real
prompt. Secrets via env only.

```bash
tools/agent/boot_stack.sh --dev
cd backend && uv run python ../tools/agent/seed_org.py --demo
cd backend && ANTHROPIC_API_KEY=… uv run python ../tools/agent/setup_haiku_llm.py
cd frontend && PW_CHROMIUM_PATH=/opt/pw-browsers/chromium \
  node tests/reports/shoot-data-side-pane.mjs ../media/pr/toolwidget-preview-side-pane
```

`tests/reports/shoot-data-side-pane.mjs` creates a report on the demo Music
Store, asks for "total invoice revenue by genre as a bar chart", waits for the
run to finish, opens the result in the pane and asserts geometry + controls
at every step. Exits non-zero on any failed expectation. Final observed run:

```
ok - prompt box shows the Haiku default model
ok - data tab is the active right-pane tab
ok - pane hydrated to the full result (24 rows)
ok - pane is wide (809px of 1440)
ok - pane is tall (822px of 900)
ok - chart canvas fills the pane (643px tall)
ok - Edit button present in the pane header
ok - Add to Dashboard present in the pane
ok - Save Query present in the pane
ok - no nested "open in panel" button inside the pane
ok - grid fills the pane (641px tall)
ok - data tab remains while the dashboard is shown
ok - data tab re-opens the pane, now marked "Added to Dashboard"
ok - closing the tab unmounts the pane
ok - no page errors (0)
```

Note on `setup_haiku_llm.py`: it PATCHed `is_default`, which
`LLMModelUpdate` does not carry, so the org silently stayed on Sonnet 5 and
the first live run used the wrong model (backend log:
`[routing] disabled: user picked Claude Sonnet 5 explicitly`). The helper now
calls `POST /api/llm/models/{id}/set_default` and asserts the default.

## The fix

| Where | What |
|---|---|
| `frontend/components/tools/ToolWidgetPreview.vue` | `canExpand` prop → header button emitting `openDataPanel({ toolExecution, title, visual })`; `expanded` prop → no collapse, the card's own toolbar pinned at the top (same presentation as the file panel, not the dashboard header), flex layout so chart / grid / Monaco fill the pane; per-instance window listeners; `total_rows`-aware row label + hydration spinner |
| `frontend/pages/reports/[id]/index.vue` | `rightPanelView` gains `'data'`; `panelData` + `openDataPanel` / `closeDataPanel`; a transient tab (icon by chart vs table, title, ✕) right after the agent tab; the pane renders `<ToolWidgetPreview :expanded="true">` in the same padded slot as the file panel, wired to `handleEditQuery` / `handleAddWidgetFromPreview`; closing returns to the view that was open before; desktop-only (`:can-expand="!isMobile"`) |
| `CreateDataTool`, `CreateWidgetTool`, `ReadQueryTool`, `DescribeEntityTool`, `WriteCsvTool`, `ChatSummary` | forward `canExpand` and re-emit `openDataPanel` |
| `locales/{en,es,he}.json` | `tools.widgetPreview.{openInPanel,closeDataPanel,dataTab,loadingRows}` |
| `tools/agent/setup_haiku_llm.py` | use `set_default` (see above) |

Evidence (`media/pr/toolwidget-preview-side-pane/`): `00-before-inline-card`
(base), `01-inline-card` (button), `02-pane-chart`, `03-pane-data`,
`04-pane-code`, `05-pane-edit-modal`, `06-added-to-dashboard`,
`07-pane-after-add`, `08-after-close`, `09-pane-he-rtl`.

## What this proves / regression notes

- The pane is the same component as the card, so any control added to the
  card later appears in the pane for free; only sizing is mode-dependent.
- Add to Dashboard from the pane creates the artifact and switches the pane
  to it; the data tab survives and reopens with "Added to Dashboard".
- RTL (`he`) mirrors the toolbar, tabs and action bar correctly.
- Pre-existing, not touched: the locale sync check reports eight `share.*`
  keys present in `en` but missing from `es`/`he`; the message wrapper
  (`w-full ms-0 md:ms-4 max-w-2xl`) overflows a narrow chat column by its
  margin; the model's `$` amounts render as math in markdown.
