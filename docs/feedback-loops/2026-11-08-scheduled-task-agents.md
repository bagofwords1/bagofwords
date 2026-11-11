# Feedback Loop: "scheduled tasks run with the report's agent scope, not the agents selected in the task"

**Reported:** a customer chose different agents for two scheduled tasks in one
report, but both tasks ran with the report's agents. They asked whether this is
expected, and how to run two tasks with different agents in the same report.

## Root cause (validated)

- **A task stored no agents.**
  - `ScheduledPrompt` (`backend/app/models/scheduled_prompt.py`) had no column
    for agents.
  - `scheduled_run_prompt` called `create_completion` with only the prompt, so
    `prepare_run_agents` used `report.data_sources`.
- **The task form wrote its pick onto the report.**
  `frontend/components/ScheduledPromptModal.vue` `syncReportDataSources()` sent
  `PUT /reports/{id}` with `data_sources`. Saving task B therefore changed the
  agents for task A, and for every chat on the report.
- **Found while fixing (self-introduced, caught before commit).**
  `PromptBoxV2` re-reads the report's agents whenever it has a `report_id`.
  Opening a task with its own agents and pressing Update unchanged saved the
  report's agents onto the task. The Playwright check below caught it.

## Loop A: deterministic reproduction

```bash
cd backend
export TESTING=true BOW_DATABASE_URL="sqlite:///db/app.db"
uv run pytest tests/e2e/rbac/test_scheduled_task_agents.py -m e2e --db=sqlite -n 4
TEST_DATABASE_URL=postgresql://… uv run pytest tests/e2e/rbac/test_scheduled_task_agents.py -m e2e --db=external
```

- **Stubbed:** the LLM only.
- **Real:** the scheduler run, routes, the agent loop and the custom API
  clients.
- **What it checks:** which `mcp__…` tools each run is offered. Those come from
  the agents the run actually uses.
- **Before the fix** (app changes stashed): `8 failed`.

## The fix

- **`scheduled_prompts.data_source_ids`** (JSON, migration `schedagents01`).
  NULL means "the host report's agents", which is the previous behavior.
- **Create/update validation.** Every id must be a live agent the task's owner
  can use, or the request gets a 400. `[]` goes back to inheriting.
- **Run.**
  - `scheduled_run_prompt` passes the task's agents to `create_completion`
    (`run_data_source_ids`), then to `prepare_run_agents(agent_ids=…)`.
  - Access is re-checked at run time.
  - `scope_report_to_run_agents` makes the run see only those agents as the
    report's attachments, via `set_committed_value`, so nothing is written to
    the report.
  - A spawned run report is created with the task's agents.
- **UI.**
  - The task form saves its agents on the task and no longer rewrites the
    report.
  - The view shows the task's agents, or "Uses the report's agents".
  - A task that follows the report keeps following it when its pick is left
    unchanged.
  - `PromptBoxV2` with `persistSelection=false` (only the task form) keeps its
    caller's agents instead of re-reading the report's.

**After the fix:**

- `tests/e2e/rbac/test_scheduled_task_agents.py`: 8 passed on SQLite and on
  PostgreSQL.
- Existing scheduled-task e2e suites: 98 passed.
- Unit tests touching run agents and scheduled prompts: 173 passed.
- **Devbox (Playwright):**
  - An unchanged edit of a task with its own agent saves
    `data_source_ids: [that agent]`.
  - An unchanged edit of an inheriting task sends no `data_source_ids`.

## What this proves, and regression notes

- **Covered by tests:**
  - two tasks in one report each run on their own agents;
  - the report's attached agents are never changed by a task;
  - inheriting tasks are unchanged;
  - spawned reports get the task's agents;
  - member and admin can only name agents they can use.
- **Known edges, not covered:**
  - the agent's `create_scheduled_task` / `edit_scheduled_task` tools do not
    take agents yet;
  - a scheduled run that calls `set_report_agents` mid-run edits the real
    report from a view scoped to the task's agents.
- **PostgreSQL harness note.** Creating a connection starts a background
  discovery run that writes tools with the fixture's mock client. On Postgres
  it can land after the test's real `refresh-tools`, so the test waits for it
  (`wait_for_connection_indexing`).
