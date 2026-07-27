# Projects (Phase 1+2) — sandbox validation

Feature validation for the new Projects concept: private-by-default shared
folders that group reports, with sidebar management, a project page, report
move flows, and a PromptBoxV2 project chip.

## Environment

```bash
cd backend
uv sync --extra dev
mkdir -p db
BOW_DATABASE_URL='sqlite:///db/app.db' uv run alembic upgrade head   # includes proj0001
BOW_DATABASE_URL='sqlite:///db/app.db' uv run python main.py &
cd ../frontend && yarn install && yarn dev &
```

Seed: sign up at `/users/sign-up`, "Skip onboarding". No LLM needed for these
flows (report creation and moves never call a model).

## What was driven through the UI (Playwright, chromium at /opt/pw-browsers/chromium)

1. **Create project** — sidebar PROJECTS section → "New project" → dialog →
   name "Marketing" → Create. Navigates to `/projects/{id}`; project appears in
   the sidebar. `POST /api/projects` 200.
2. **Create root report** — sidebar "New report". Report lands in the personal
   REPORTS list (fetched with `project_id=none`).
3. **Move report → project** — report row hover menu → "Move to project" →
   modal lists projects (shared ones carry a "Shared" badge + disclosure
   copy) → click "Marketing". `PUT /api/reports/{id}` with `project_id`; the
   report leaves the root sidebar list and appears on the project page.
4. **Create report inside project** — project page "New report" button posts
   `project_id` with the create payload; the new report is born in the folder.
5. **PromptBoxV2 chip** — the report page shows a quiet `📁 Marketing` chip
   (`data-testid="project-chip"`) in the prompt box toolbar; clicking it
   navigates back to `/projects/{id}`.
6. **Settings tab** — rename/description/icon/color editable for managers,
   save toast, defaults placeholder card, owner-only danger zone.

Screenshots: captured in the session scratchpad `shots/` (01–12) and attached
to the PR (ui-evidence).

## Lower-layer verification

DB (`backend/db/app.db`):

```
projects: ('8bec6dda…', 'Marketing', 'private', owner set, deleted_at NULL)
reports:  root report project_id NULL; moved + born-inside reports carry the project id
```

HTTP log: zero 4xx/5xx across the whole flow (`http_log.txt` in scratchpad).

## Bug found & fixed during the loop

`GET /api/reports/{id}` returned `project_id: null` even for project reports —
`ReportService.get_report` builds `ReportSchema` field-by-field and didn't
include the new fields, so the PromptBoxV2 chip never rendered. Fixed by
adding `selectinload(Report.project)` to the load options and passing
`project_id` / `project` into the schema (report_service.py). Verified via
curl (single GET now matches the list payload) and a Playwright re-run
(chip visible, navigates to the project).

## Automated coverage

`backend/tests/e2e/test_projects.py` (17 tests): CRUD + validation, private
default (404 to non-members, no id existence leak), view/manage grant matrix
via `PUT /projects/{id}/members`, org-wide access, move semantics (owner-only,
invisible-target 404, bulk move), delete archives contained reports and stops
their scheduled prompts, minimal-view `project_id` for the sidebar,
report_count behavior, org scoping.

Run: `TESTING=true ENVIRONMENT=production uv run pytest tests/e2e/test_projects.py -m e2e --db=sqlite`


## QA sweep (button-by-button, multi-member) — 2026-07-27

Owner sweep (Playwright, 20 checks, all green): create dialog open/cancel,
create → navigate, sidebar rename, move modal (incl. its New-project hop),
chip picker (move / remove / open project), tabs, share modal add/remove,
settings save, defaults save, delete cancel/confirm.

Member sweep — five collaborators (Dana/Eli/Maya/Noa/Tom), 10 checks each,
50/50 green: shared project in sidebar, no manage menu, project page lists
reports, Share button hidden, settings read-only, no danger zone, read-only
bar on owner reports, composer hidden, fork → editable copy, create own
report inside the project.

**Bug found & fixed by the sweep:** re-adding a previously removed member
500'd — remove_member soft-deletes the ResourceGrant but uq_resource_grant
still holds the key, so the re-add INSERT hit a UNIQUE violation. Fix:
upsert_member now loads the row regardless of deleted_at and resurrects it.
Regression covered in test_projects.py (grant → remove → re-grant →
visibility restored).
