# Feedback Loop — a project's agent was invisible in its own reports

A project's rail promises its agents are "copied onto every new report created
in this project". The backend keeps that promise. The UI hid it twice over: the
prompt box labelled the copied agent "Auto", and two of the three "New report"
buttons dropped the project entirely.

## Reproduced live (2026-08-05)

Sandbox: sqlite backend on :8000, Nuxt dev on :3000, one org, project
**Marketing** with default agent **Chinook Music Store** (a second agent, *Ads
Warehouse*, exists in the org but is not a project default). Driven through the
real UI with Playwright; `POST /api/reports` request/response logged per click.

| entry point | `project_id` sent | agents on the report | prompt-box chip |
|---|---|---|---|
| Project page, top-right button | ✅ set | `["Chinook Music Store"]` | **"Auto"** |
| Sidebar "New report", on the project page | ❌ `null` | `["Chinook Music Store", …]` (org fallback) | **"Auto"** |
| Sidebar "New report", outside a project | ❌ n/a | org fallback | "Auto" (correct) |

The backend was never at fault. Verified directly:

```
POST /api/reports {"data_sources": [], "project_id": "<marketing>"}
→ project_id: <marketing>, data_sources: ['Chinook Music Store']
```

## Two independent defects

**1. Two entry points dropped the project.** `report_service.create_report`
copies the defaults only when the body carries `project_id` *and* no explicit
agents. Only `pages/projects/[id]/index.vue` sent that. `layouts/default.vue`
and `components/CommandPalette.vue` sent `useAgent`'s workspace-wide selection —
which falls back to *every* org agent — and no `project_id`, so a report started
from inside a project left the project.

**2. The copied agent rendered as "Auto".** `DataSourceSelector` defined auto
mode as "the selection equals the project's defaults". Since the backend copies
exactly those defaults, **every** new report in a project opens in that state,
so the chip always showed the generic bolt. Worse, auto is emitted to consumers
as the absence of a choice (`update:autoMode` → `agentsAreAuto` →
`isAgentSelected()` returns false), so the blank-report picker highlighted
nothing either. The agent was attached and answering, invisible in both places.

The tell that #2 is a bug and not intent: the panel's own Auto row already
carries a "from {project}" sub-label and groups the defaults under a "From
{project}" header. The mode was always meant to be legible; only the collapsed
chip wasn't.

## Fix

Auto splits in two — `utils/agentSelection.ts`:

- **Workspace auto** (every visible agent): keeps the bolt, still reports as no
  selection. No chip can name that set.
- **Project defaults**: a short, concrete set the report really carries, so it
  renders and highlights like any other selection.

A single selected agent is now named beside its icon rather than shown as a bare
icon — the state these reports open in. Route→project detection moved to
`utils/projectRoute.ts` and payload construction to
`useNewReportProjectContext()`, so all three entry points build the request
identically.

## After

| entry point | `project_id` sent | agents on the report | prompt-box chip |
|---|---|---|---|
| Project page, top-right button | ✅ set | `["Chinook Music Store"]` | **"Chinook Music Store"** |
| Sidebar, on the project page | ✅ set | `["Chinook Music Store"]` | **"Chinook Music Store"** |
| Sidebar, outside a project | n/a | org fallback | "Auto" (unchanged) |

The blank-report picker highlights *Chinook Music Store* and leaves *Ads
Warehouse* unselected, matching what the report holds.

## Regression cover

`frontend/tests/unit/agentSelection.mjs` pins the cases that made this subtle:
superset and cleared selections inside a project, and a project default the user
cannot see (the backend drops those, so they must not hold the match open).
`frontend/tests/unit/projectRoute.mjs` pins which routes count as "inside a
project". Run with `node --experimental-strip-types <file>`.

Backend semantics were already covered by
`backend/tests/e2e/test_project_collaboration.py::test_project_default_agents_override_not_union`.
