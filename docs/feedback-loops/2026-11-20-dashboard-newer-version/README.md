# Feedback loop: an open dashboard never shows the version the agent just saved

Reported: "Each time you ask to add something to a dashboard you need to click
on the dashboard link again in order to see the new version in the dropdown."
The ask was to refresh the open dashboard automatically, or veil it with a
Refresh button.

## Root cause (validated)

The conversation's Dashboard pane (`ArtifactFrame`) learns about new versions
only from the run's SSE stream: `tool.finished` for `create_artifact` /
`edit_artifact` → `artifact:created` → `handleArtifactCreated`. Its version
list is re-read only on those events, on a click on a dashboard link
(`artifact:select` with an unknown id → `fetchArtifactsList(true)`), or on remount.
The dashboard's draft page (`/d/<id>/draft`, where "Open in new tab" and the
Dashboards list lead) reads its draft and history **once on mount**
(`SharedDashboardView.loadReport`, `DraftVersionBar.loadHistory`) and gets no
stream at all.

So any dashboard view that is not attached to the editing run stays on the old
version, and so does its picker:

| Where the dashboard is open | Before | Evidence |
|---|---|---|
| Same tab as the chat, pane open | follows (v1 → v3) | `01_same_tab.cjs` |
| Second tab of the same conversation, watcher attached | follows (v3 → v4) | `03_scenarios.cjs` `SCEN=C` |
| Pane whose stream is cut (dropped connection, run it isn't attached to, another conversation) | **stays on v11, v12 never listed** | `SCEN=E`, `before-pane-stale.png` |
| Draft page in another tab | **stays on v6 after v7 is saved** | `SCEN=D`, `before-draft-stale.png` |

Clicking the dashboard link "fixes" it only because that path forces a list re-read.

## Fix

- `composables/useVisiblePoll.ts`: runs a check every N s while the tab is
  visible, and right away when the tab comes back into view or the window
  regains focus. No polling while the tab is hidden, and a check never starts
  while the previous one is still running.
- `utils/newerDashboardVersion.ts`: the pure "is there a newer version to
  offer?" decision for both views (unit-tested in
  `tests/unit/newerDashboardVersion.mjs`).
- `components/dashboard/NewerVersionOverlay.vue`: a milky veil over the stale
  dashboard with **Refresh to vN** and **Keep viewing vM**.
- `ArtifactFrame`: re-reads the list and history every 15 s. The picker grows,
  and a newer version of the open dashboard is offered through the overlay.
  Versions from another conversation open read-only (`openVersionById`). It is
  never swapped under the reader, and the agent's own run still auto-follows
  with no overlay.
- `SharedDashboardView` (draft page): reads "latest" every 10 s (the same
  draft read without `?version`). The version list reloads (`refreshKey` on
  `DraftVersionBar`). A page showing "latest" is veiled. A page opened on a
  specific `?version=N` is the reader's pick, so it is never veiled.

## Verification (real LLM: Claude Haiku 5.5, demo Music Store data)

1. `01_same_tab.cjs <out> [reportId]`: create a dashboard, open it via its
   card, ask to add a chart. After the fix the pane still moves v10 → v11 on
   its own, and no overlay is left behind.
2. `SCEN=D node 03_scenarios.cjs <out> <reportId>`: draft page open, edit in
   the conversation. Before: stale. After: overlay "A newer version is ready
   (v13)" → Refresh → the new title.
3. `SCEN=E node 03_scenarios.cjs <out> <reportId>`: the pane tab's stream
   routes are aborted, and a second tab runs the edit. Before: the picker
   lists up to v11 while the server has v12. After: the picker lists v14, the
   overlay offers v14, and Refresh selects it.

`01_same_tab.cjs` signs in and writes `<out>/state.json`. Point
`03_scenarios.cjs` at it with `STATE=<out>/state.json`. Both scripts expect the
stack from `tools/agent/boot_stack.sh --dev`.

Screenshots: `media/pr/dashboard-newer-version/`.
