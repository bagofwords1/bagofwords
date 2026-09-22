# Feedback Loop — deleting an agent leaked its private instructions org-wide

Deleting an agent (data source) with N instructions attached kept all N
instruction rows and only dropped the agent ↔ instruction association rows.
"Global" is defined as *attached to no agent*, so every instruction that had
been visible only to that agent's members became readable by every org member
and started loading into every other agent's AI context. This is a permission
leak, not a data-loss bug. Saved queries (entities) lost their agent links the
same way, and eval test cases were left pointing at a dead agent id.

Rule implemented: content that is attached **only** to the deleted agent is
deleted with it; content shared with another agent is just detached from the
deleted one; global / agent-less content is untouched. This applies to
instructions, saved queries (entities) and eval test cases. The agent's
instruction folders are removed with it.

## Root cause (validated)

- `backend/app/services/data_source_service.py` — `delete_data_source` never
  touched instructions or entities. The association rows disappeared because
  the ORM cleans the loaded `DataSource.instructions` / `DataSource.entities`
  collections on delete, leaving the rows with zero data sources.
- `backend/app/services/instruction_service.py:4105` (list visibility) and the
  `InstructionContextBuilder` both treat "no data sources" as global:
  `~Instruction.data_sources.any()` short-circuits the per-agent membership
  check. Confirmed live: after the delete a non-member saw all three private
  instructions and the global count rose from 3 to 6.
- `TestCase.data_source_ids_json` is a plain JSON list; nothing removed the
  deleted agent's id, so a case that targeted only that agent kept a dangling
  reference that no agent view lists and no one below org admin can act on.
  `TestSuite.data_source_id` was already re-homed to the org (`SET NULL`), by
  design; that is kept.
- `backend/alembic/versions/instrdir01_add_instruction_directories.py:36` —
  `instruction_directories.data_source_id` has a plain FK (no `ON DELETE`),
  and nothing deleted folders, so on Postgres an agent with folders could
  not be deleted at all (FK violation → 8 retries → 500). SQLite does not
  enforce FKs here, so it only orphaned the rows.

## Loop A — deterministic reproduction (pytest, no external services)

```bash
cd backend
TESTING=true ENVIRONMENT=production uv run pytest \
  tests/e2e/rbac/test_delete_agent_content_scope.py -q --db=sqlite
```

The test creates two private agents, an outsider member (not on either
agent), then on the doomed agent: three instructions only on it, one shared
with the survivor, a folder with one placement, a saved query only on it and
one shared, and an eval suite homed on it with a doomed-only case, a shared
case and an agent-less case. It deletes the doomed agent via
`DELETE /api/data_sources/{id}` and asserts, through the public API and the
AI context builder, that:

- the outsider still cannot see the doomed agent's instructions,
- the admin gets 404 for them (deleted with the agent),
- the shared instruction now lists only the survivor agent (not global),
- the survivor agent's `load_always_instructions` set is unchanged,
- the global count is still 1 and the agent's folder tree is empty,
- the doomed-only saved query is gone (404), the shared one lists only the
  survivor,
- the doomed-only eval case is gone from its suite, the shared case now
  targets only the survivor, the agent-less case is unchanged, and the suite
  survives as org-level content.

Observed before the fix:

```
>       assert outsider_sees & set(only_doomed) == set(), "private instructions leaked to a non-member"
E       AssertionError: private instructions leaked to a non-member
1 failed, 1 passed
```

With only the instruction half of the fix in place, the entity assertion
fails (`assert 200 == 404` on the doomed-only saved query). With the full
fix:

```
2 passed
```

## Loop B — live stack, real UI (Playwright + API probe)

Boot the stack (`tools/agent/boot_stack.sh --dev`, or the manual steps in
`.claude/skills/sandbox-feedback-loop/SKILL.md`), then run the numbered
scripts from a scratch directory with `npm install playwright`:

1. `01_signup.js` — registers the admin through `/users/sign-up`, saves
   `storageState`.
2. `02_seed.js` — via the real API (JWT from the `auth.token` cookie): two
   private sqlite agents, 3 doomed-only + 1 shared + 1 survivor-only + 1
   global instruction, a folder with one placement on the doomed agent, and
   an invited outsider who registers through the invite link (the sign-up
   form needs the `token` query parameter; read it from `memberships`).
3. `03_verify.js before` — lists instructions as the outsider and the admin,
   reads `/instructions/counts`, the shared instruction's data sources and
   the doomed agent's folder tree.
4. `04_delete_ui.js` — dismisses onboarding, opens
   `/agents/<doomed>/settings`, clicks **Remove agent**, screenshots the
   confirmation, confirms, and records the `DELETE /api/data_sources/<id>`
   response.
5. `03_verify.js after` — same probe again.
6. `05_probe_entities_evals.py` — same shape for a saved query and an eval
   case only on a fresh agent plus one of each shared with the survivor.

Observed before the fix (pre-fix backend, same scripts):

```
before: outsider_sees_doomed_only 0 | counts_global 3 | doomed_folders 1
DELETE 200
after:  outsider_sees_doomed_only 3 | admin_sees_doomed_only 3
        counts_global 6            ← the 3 private rules became global
        doomed_folders 1           ← orphaned folder pointing at a dead agent
```

Observed after the fix:

```
before: outsider_sees_doomed_only 0 | counts_global 7 | doomed_folders 1
DELETE 200 {"message":"Data source deleted successfully"}
after:  outsider_sees_doomed_only 0 | admin_sees_doomed_only 0
        shared_data_sources ["survivor"] | counts_global 7
        by_agent { survivor: 2 } | doomed_folders 0

entities/evals probe:
[after] entity doomed-only listed: False | shared listed: True
[after] case doomed-only listed: False | shared targets: [<survivor>] | agent-less targets: []
suite data_source_id: None
```

DB check (`backend/db/agent.db`): the three doomed-only instruction rows and
the doomed-only eval case have `deleted_at` set; the doomed-only entity row is
gone; the shared instruction and shared entity each have exactly one
association row left (the survivor); `instruction_directories` and
`instruction_directory_placements` have no rows for the deleted agent. The
backend log shows `Deleted 3 instruction(s) scoped only to data source …`
and no errors.

## Fix and scope

- `data_source_service.delete_data_source` now runs three cleanups first:
  `_delete_agent_scoped_instructions` (soft delete through
  `InstructionService.delete_instruction`, so pending suggestions are voided,
  a removal build is recorded and an audit row written; then detaches the
  rest, expires the ORM collection, and removes the agent's folders and
  placements), `_delete_agent_scoped_entities` (hard delete through
  `EntityService.delete_entity`, then detach the rest) and
  `_delete_agent_scoped_test_cases` (soft delete, as `delete_case` does;
  shared cases lose the agent id from `data_source_ids_json`).
- `AgentSettingsPanel.vue` confirmation copy now says what goes with the
  agent and that shared content is kept.

Not changed: the non-delete path that widens scope the same way — editing an
instruction to remove its last agent, or a bulk update with an empty agent
list, still makes it global (`instruction_service.py:3132`). That is a
separate permission question and should get its own issue.

Not verified here: the Postgres leg (`--db=postgres`) could not run in this
sandbox (no Docker daemon). The folder cleanup is what makes the delete
succeed there; CI runs both database legs.
