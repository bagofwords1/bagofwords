# Feedback Loop — "create a demo for finance in the e-commerce sector"

An admin in **training mode** asks for demo data for any domain (finance,
sales, HR, logs/monitoring, machine data…). The agent designs a schema,
shows it on a review card (tables, columns, row counts, suggested agents
with emoji icons and checkboxes), and on approval the org's **small default
model** generates the rows table by table. The rows are written to a SQLite
file chosen by the server, registered as a connection, and the ticked agents
are created on it with their active tables, starters and instructions.

Gates:
- **Org setting** `enable_demo_data_generation`. Default off, nested under
  Training Mode in AI Settings.
- **Training mode only** (`allowed_modes=["training"]`).
- **`manage_connections`**, the same permission as `POST /connections`.
- **`create_data_source`**, additionally needed to create agents; without it
  only the connection is created.
- **The `demo-data` skill** is installed by default but only advertised while
  the setting is on (new frontmatter `requires_setting`).

## How it works (validated)

| Piece | Where |
|---|---|
| Spec (declarative: no code, no paths) + structural validation, FK ordering | `backend/app/schemas/demo_dataset_schema.py` |
| Per-table small-model codegen → sandboxed exec (`validate_python_code` + no file sinks) → coercion + PK/FK/null/future-date checks → retry with the error → SQLite with real PK/FK constraints | `backend/app/services/demo_data/generator.py` |
| Connection (`uploads/demo_data/<org>/<id>.sqlite`, `system_only`), catalog descriptions, agents via `DataSourceService.create_data_source`, icons, instructions, attach to report; file removed when the connection is deleted | `backend/app/services/demo_data/installer.py`, `connection_service.delete_connection` |
| Tool: gates → spec check → review pause → generate (progress per table) → install on its own DB session | `backend/app/ai/tools/implementations/create_demo_dataset.py` |
| Review answer carries data (ticked agents / feedback) | `tool_confirmations.response` (migration `toolconfresp01`), `routes/completion.py` |
| Tool runner honors a tool's declared `timeout_seconds` (review + generation > 300s) | `backend/app/ai/runner/tool_runner.py` |
| Catalog hidden when the setting is off | `backend/app/ai/agent_v2.py` (catalog filter) |
| Skill gated by setting | `backend/app/ai/skills/library/demo-data.md`, `catalog.py` (`requires_setting`), `instruction_context_builder._skill_setting_enabled` |
| Card UI (review / progress / created / declined) | `frontend/components/tools/CreateDemoDatasetTool.vue` |

### Bug found by this loop

The first live UI run showed *"Hit an internal error — retrying from the
latest context"*. Root cause: re-running the same demo made
`DataSourceService.create_data_source` raise 409 (name taken). Its rollback
ran on the **agent's shared session**, which expired the run's own
`current_execution`, and the next attribute read raised `MissingGreenlet`
(`agent_v2.py` `_run_one`). The fix has two parts:
1. Install runs on its own `async_session_maker()` session, then
   `db.refresh(report, ["data_sources"])`.
2. A taken agent name gets a `" (2)"` suffix instead of failing.

Regression test:
`test_rerun_with_taken_names_suffixes_agents_and_keeps_the_run_session_usable`.
It fails on the old code and passes now.

## Loop A — deterministic (no external services)

```bash
cd backend
BOW_DATABASE_URL=sqlite:///db/app.db TESTING=true uv run pytest \
  tests/unit/test_demo_dataset_generator.py \
  tests/training/test_create_demo_dataset_tool.py \
  tests/e2e/test_skill_catalog.py -k "demo or setting_gated or ticked or rerun or catalog" -q
```

The LLM is the only stub. Covered:
- spec errors (unknown FK, non-PK ref, cycle, future end, unknown agent table)
- retry until FKs resolve, with a constrained SQLite file
- future dates rejected
- generators that write files are never run
- attempt budget
- emoji normalization; file deletion confined to the demo-data root
- setting off → `disabled`
- agent creator without `manage_connections` → `permission_denied`
- invalid spec → errors without a review
- reject with feedback → nothing created
- approve → only the ticked agents, exact active tables, icon, attached to
  the report, file removed with the connection
- re-run → suffixed agents and an intact run session
- setting-gated skill advertised only while the toggle is on

Each guard was mutation-checked; the test fails with the guard removed.

### UI (real stack, scripted LLM)

```bash
tools/agent/boot_stack.sh --dev
cd backend && uv run python ../tools/agent/seed_org.py
uv run python ../tools/agent/mock_llm_demo_data.py --port 8765 --gen-delay 3.5 &
# custom provider → http://127.0.0.1:8765/v1, model "mock-demo" as default + small default;
# org settings enable_training_mode + enable_demo_data_generation on
cd ../frontend && PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers BOW_TOKEN=… BOW_ORG=… \
  node tests/demo_data/demo-dataset-flow.mjs
```

Observed: `PASS: review card → untick → approve → progress → created (only
ticked agent); he + mobile; reject with feedback → revised proposal.` Evidence
is in `media/pr/demo-data-generation/`: `01_setting`, `02_review_card`,
`03_review_unticked`, `04_generating`, `05_created_*`, `06_created_he`,
`07_created_mobile`, `08_feedback`, `09_revised_review`, and `flow.gif`.

## Loop B — live model eval (hr, finance, sales, monitoring)

```bash
# Anthropic provider with Claude Haiku 5.5 as default AND small default
cd backend && uv run python ../tools/agent/demo_data_eval.py --token … --org … \
  --case hr --case finance --case sales --case monitoring
```

The script drives a real completion per use case and approves the card
through the real endpoint. It then scores the SQLite file:
- **Hard checks:** created; ≥ 3 tables; no empty table; FK check clean; no
  accidental future dates; agents with emoji icons.
- **Soft signals:** skewed numerics, weighted categories, monthly variation.

The harness was validated against the scripted mock. It correctly flags the
mock's uniform categories (0/2 weighted).

**Status:** the Haiku 5.5 run has not been done yet. The sandbox's
environment key was rejected by Anthropic ("credit balance is too low").
Re-run the command above with a funded key in the environment.

## Follow-up — production trace and shared conversations

**Production trace** ("Mock Agents for Payroll and Finance", 3 tool calls,
about 445s in total):
1. All five tables generated (172s), then the write failed with `FOREIGN KEY
   constraint failed`. Root cause: `write_sqlite` loaded rows with
   `PRAGMA foreign_keys = ON`, so a self-reference
   (`employees.manager_id → employees.employee_id`) pointing at a row inserted
   later in the same table failed on insert, even though validation had
   already proven every reference resolves. Fix: load with enforcement off,
   then check every row once with `PRAGMA foreign_key_check`. Real orphans
   are still rejected. Tests: `test_self_reference_to_a_later_row_writes`
   (failed before the fix) and `test_writer_still_rejects_real_orphans`.
2. `employees` failed 3 times with `TypeError: Index does not support mutable
   operations`: the model mutated a pandas `Index`, and every retry received
   only the exception text. Fixes:
   - The retry prompt now names the failing line (`line N: <source>`).
   - The system prompt warns that `Index` objects are immutable and explains
     how to build self-references.
   - The final code for each table is saved in the connection config
     (`demo_generator_code`).
   - A failed table returns its last attempt as `failed_code`.
   Test: `test_retry_prompt_names_the_failing_line`.
3. To get through, the agent cut the spec (480 → 120 employees, no
   `manager_id`). With fixes 1 and 2, the original spec builds.

**Shared conversation / reload.** A decision block is linked to its tool
execution only when the tool finishes. So while `create_demo_dataset` waited
for review:
- the shared page `/c/<token>` showed a bare "Designing the dataset."
- a reload lost the owner's review card.

Fixes:
- **Read paths** (completions list, watch stream, public conversation) look up
  the running tool execution by plan decision. They attach it only when a
  review explains the pause, with `review_state` (pending / approved / …).
  - The owner's payload also carries the confirmation, so the card renders
    with its controls after a reload.
  - The public payload carries only the state.
- **Shared page:** renders the tool read-only (no checkboxes, buttons or links
  into the workspace) and refreshes every 4s while a run is in progress.
- **Empty workspace:** the prompt box can send in Training mode when demo data
  generation is on, even with no agents, so a fresh workspace can ask for its
  first dataset.

Loop: `frontend/tests/demo_data/demo-dataset-share.mjs` → `PASS: shared view
shows waiting-for-review (read-only), owner card survives reload, shared view
refreshes to created.` Evidence: `media/pr/demo-data-share/`.
