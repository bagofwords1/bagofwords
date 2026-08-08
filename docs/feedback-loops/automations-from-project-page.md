# Feedback Loop — create automations from the project page

Feature request: a scheduled task or a trigger should be creatable **from the
project page**, and what it creates should be **filed into that project**, with
the embedded PromptBoxV2 naming the project so the binding is visible before you
save. This loop validates the flow end to end in a real sandbox (backend +
Nuxt + Anthropic), down to a live webhook delivery landing in the project.

## The gap (validated before the change)

- Both automation kinds could only be created from `/automations`
  (`ScheduledTab.vue` → `ScheduledPromptModal`, `TriggersTab.vue` → its inline
  setup modal). The project page listed automations read-only via
  `GET /projects/{id}/automations` and had no way to add one.
- The backend was already there: `POST /triggers` accepts `project_id`
  (`WebhookTriggerCreate`, validated by `WebhookService._validate_project`), and
  spawned sessions are created inside it (`webhook_service.py`); a scheduled
  task inherits its project from its host report.
- The trigger setup modal lived **inside** `TriggersTab.vue`, so nothing else
  could open it.
- The project chip in the embedded PromptBoxV2 stayed blank whenever the caller
  didn't pass `project` — including every scheduled task opened from
  `/automations`, which always has one via its report.

## The implementation

Frontend only.

- **`components/automations/TriggerModal.vue` (new)** — the trigger setup modal,
  extracted from `TriggersTab.vue` verbatim plus the provisioning it used to do
  in the tab. Opening with `trigger: null` provisions a fresh trigger (so its
  delivery URL exists immediately) and binds `project_id`; opening with a
  trigger shows it read-only. Emits `changed` for the caller's list.
- **`TriggersTab.vue`** — now just the list; it says which trigger to open
  (`null` = new) and refetches on `changed`.
- **`pages/projects/[id]/index.vue`** — a `+` on the Automations header offers
  "New task" / "New trigger". A task creates its host report with `project_id`
  first (cancelling deletes that report again, so cancelling leaves nothing
  behind); a trigger opens the modal seeded with the project. Trigger rows now
  open their config in place instead of linking to `/automations`.
- **`ScheduledPromptModal.vue`** — takes an optional `project`, otherwise reads
  it off the host report; shows it in the read-only summary and passes it to the
  prompt box. On the first save it also renames a still-placeholder host report
  to the task's title, so a project's report list reads as itself.
- **Modal transition fix (pre-existing bug).** Both automation modals drove
  `UModal`'s `ui.width` off `viewMode`; flipping it restarted the panel's enter
  transition and left it stuck at `opacity: 0` — clicking **Edit** made the
  dialog vanish. The dialog is now sized once with its own surface left bare,
  and the narrower editing width lives on the `UCard` inside.

## The loop

Sandbox per `.claude/skills/sandbox-feedback-loop`: backend on `:8000`
(sqlite), Nuxt on `:3000`, a real Anthropic provider with Claude 4.5 Haiku,
driven through the UI with Playwright and checked against sqlite.

```bash
# backend
cd backend && uv sync --extra dev
BOW_DATABASE_URL='sqlite:///db/app.db' uv run alembic upgrade head
BOW_DATABASE_URL='sqlite:///db/app.db' uv run python main.py &
# frontend
cd frontend && yarn install && yarn dev &
```

### What was verified

| # | Through the UI | Verified at |
|---|---|---|
| 1 | Project → `+` → New task → save | `reports.project_id` = the project; `scheduled_prompts` row on that report |
| 2 | Project → `+` → New trigger → save | `webhooks.project_id` = the project, name + task persisted |
| 3 | `POST` to the trigger's delivery URL | spawned report `event` created with `project_id` = the project |
| 4 | Prompt box inside both modals | chip reads `Revenue Ops` (screenshotted) |
| 5 | Cancel a new task | host report archived, project report list unchanged |
| 6 | Cancel an untouched new trigger | trigger soft-deleted, list back to 1 card |
| 7 | Second "New task" after a cancel | form starts empty (modal is keyed by host report) |
| 8 | `/automations` both tabs | list, view, edit, abandon-draft all unchanged |
| 9 | View → Edit in both modals | panel stays at `opacity: 1` (regression probe) |
| 10 | Light / dark / Hebrew RTL | screenshots under `media/pr/automation-trigger-project-page-d87mwe/` |

Two defects were found and fixed inside the loop:

1. **The trigger list didn't refresh after save.** `save()` no longer refetched
   (the tab used to do it), so the row kept the draft's "Trigger" label while
   the DB had the real name. Fixed by emitting `changed` from `save()` and from
   every close.
2. **A placeholder card broke the modal.** Rendering a loading `UCard` while a
   new trigger provisioned swapped `UModal`'s child mid-transition and left the
   real card at `opacity: 0`. Fixed by opening the dialog only once there is a
   trigger to show — which is what surfaced the same failure mode in the
   pre-existing `ui.width` binding above.

### Layer checks

```bash
sqlite3 backend/db/app.db \
  "select title, project_id, status from reports;
   select title, report_id, cron_schedule from scheduled_prompts;
   select name, project_id, is_active from webhooks where deleted_at is null;"
```

```
Weekly revenue digest | 6e059b61-… | draft
Monthly close checklist | 6e059b61-… | draft
event                 | 6e059b61-… | draft      <- spawned by the trigger delivery
Pagerduty alerts      | 6e059b61-… | 1
```
