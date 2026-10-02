# Feedback Loop — E5: edit → break → fix → rebuild (PR #1220, live on GPT-6 Luna)

Live run of eval **E5** from `artifact-resources-evals.md` against PR #1220 head
`96e8297`. It used the real planner and real `gpt-6-luna` completions, driven by
ordinary user prompts with no SDK hints. The full stack ran in the sandbox with
`BOW_ARTIFACT_RESOURCES_ENABLED=true`. Every step was checked at three layers: the DB
(versions, resources, row fingerprints), direct HTTP against the runtime API, and the
UI as Olivia in Chromium.

**Verdict: the build and repair loop is strong; resource changes are not.** Creating, editing,
repairing and rebuilding all worked, and no data was lost. But **any failed
resource change crashes the user's whole turn with a 500** (4 of 12 turns), so
the agent never gets to recover. That one bug blocks the delete, recreate and
schema-change parts of E5.

## Scorecard

| E5 check | Result | Evidence |
|---|---|---|
| Runtime error appears and is fed back to the agent | ✅ Pass | `edit_artifact` rejected the bad code: *"fails to render: Cannot read properties of undefined (reading 'useKanban')"* |
| Agent fixes the user-visible break in ≤3 turns | ✅ Pass (1 turn) | v3 board showed "Kanban SDK unavailable" → one "fix it" → v4 board works, and a move persists (`update status=done → revision 2`) |
| Each rebuild is a new completed version; same resource IDs; rows and revisions unchanged | ✅ Pass | v1→v6 all `completed`. `tasks` id `fd42cf23` throughout. Row fingerprint unchanged across Kanban, break, fix and full rebuild |
| Old versions still open and read the same live data | ✅ Pass | v2 opened from the version picker after v5 existed and showed the same 22 live tasks |
| 63-character resource name works | ✅ Pass (via `manage_artifact_resources`) | Created, record create/list 200. The `create_artifact` hashed-key path was not exercised |
| Recreating a deleted name gives a clear "name reserved" message | ❌ Fail | API says only *"Resource conflicts with an existing definition"*, and the agent turn crashes before the message reaches it (500) |
| Implicit schema/permission change on rebuild is resolved by an explicit update | ❌ Fail (blocked) | The agent correctly asked how to migrate, then sent an explicit `update`. The API correctly answered *"Existing records require an explicit migration"*, but the turn crashed (500) before the agent could adjust |
| No data loss at any point | ✅ Pass | 22 live rows, fingerprint stable through every failure |

Twelve live turns ran: 8 returned 200 and **4 returned 500**, all with the same root cause.

## Findings

### 1. [High] A failed resource change crashes the entire agent turn (500)

**What the user sees:** asking to delete a collection that still has data, recreate a
deleted name, or add a required field to a table with rows makes the chat fail with
*"Agent execution failed: greenlet_spawn has not been called"*. Nothing tells the user
what went wrong or how to proceed. Retrying hits the same failure every time (3/3 repeats).

**Root cause:** `backend/app/ai/tools/implementations/manage_artifact_resources.py:63`
calls `await db.rollback()` on the **agent's shared session** when a change is rejected.
The rollback expires every loaded ORM object, including the agent's own execution
row. The next access, `self.current_execution.id` at `backend/app/ai/agent_v2.py:6418`,
lazy-loads outside the async context and raises `MissingGreenlet`. The loop crashes
three times and the completion fails.

**The tell:** each rejected change is a *correct* 409 when replayed directly against the
API (`Nonempty resources cannot be deleted`, `Resource conflicts with an existing
definition`, `Existing records require an explicit migration`). The service is right;
the error never reaches the model.

**Scope:** this has been in the PR since `ab3c549`, not introduced by the review fixes. The same
`await db.rollback()` pattern is in `publish_artifact.py:63` and in
`create_artifact.py:1630`, the rebuild CONFLICT path added for review finding #4.
That path probably crashes the same way; it was not exercised live.

**Correct pattern already in the repo:** `execute_mcp.py:1430`: *"Persist within a
savepoint so a failure here rolls back cleanly instead of poisoning the shared
agent-execution transaction"*, using `async with db.begin_nested():`.

**Proposed fix:** wrap `service.configure(...)` / `publish(...)` / the
`create_artifact` resource block in `db.begin_nested()` and drop the session-wide
`rollback()`, so a rejected change rolls back only its savepoint and returns the 409
message as the tool observation. Add a regression test: a tool call that gets a 409
followed by a second tool call in the same turn must complete with HTTP 200.

### 2. [Medium] Regression in the fix commit: `create_artifact` loses the prior-app reference

`96e8297` added `from app.models.artifact import Artifact` inside `run_stream`
(`create_artifact.py:1606`). That makes `Artifact` a local name for the whole function,
so line 1280 raises `UnboundLocalError` on every page create. The error is caught and
logged as *"Failed to load prior artifact code for rebuild reference"*. **Effect:** when
the agent builds a new page in a report that already has one (without
`replaces_artifact_id`), it silently loses the prior code it is supposed to preserve.
**Fix:** delete line 1606; `Artifact` is already imported at line 31. No other local
import in that block shadows an earlier use.

### 3. [Medium] Publication's safety check misses resources added by edits

`edit_artifact.py:315` copies `resource_requirements` forward unchanged. When the
agent adds a collection with `manage_artifact_resources` and then wires it in with
`edit_artifact`, the new version's requirements still list only the old resources.
Live: v6 uses the 63-character subtask collection. After that collection was
deleted, **publishing v6 succeeded (200)**. The check in
`artifact_publication.py:44` exists to refuse exactly this case.
**Fix:** recompute the requirements from current definitions the code references, or
merge the referenced resources in `edit_artifact`.

### 4. [Low] The reserved-name error doesn't say the name is reserved

A deleted name can't be reused, by design. The 409 says *"Resource conflicts with an
existing definition"*, which reads like a schema clash. Something like *"`<name>` was deleted and stays
reserved so older versions never bind to new data; choose a new name"* would let both
users and the agent recover.

### 5. [Info] Agent honesty and judgment

- 👍 Asked for the `useKanban` contract rather than inventing one, and asked how to
  migrate existing rows before adding a required field.
- 👎 Given a confidently wrong contract, it shipped a v3 that guards the missing hook,
  passes validation, and **told the user it "uses `useKanban`"** while the board showed
  "Kanban SDK unavailable". The repaired v4 also keeps the dead hook path.
- 👎 In the fix turn it referred to "your screenshot", but no screenshot was sent.
- Every turn says the preview is read-only, so the agent can't verify a single write it builds.
  That's expected, but it means interaction checks never cover persistence.

### 6. [Info, test tooling] Simulated drag-and-drop doesn't work in opaque-origin frames

With the PR's sandbox (`allow-scripts allow-downloads allow-forms`, no
`allow-same-origin`), Playwright's `dragTo` and its mouse-based drag deliver no `drop` in
Chromium. With the old sandbox they do. **Real X11 input under Xvfb works in all three
sandbox variants**, so users aren't affected. But Playwright end-to-end tests, and the agent's own
`browser_act`, can't exercise drag-and-drop in artifacts. The generated drop handlers
were verified by dispatching the HTML5 events (they persisted `status=done`).

## Sandbox notes (not product findings)

- Backend Playwright expects `chromium_headless_shell-1223`; the sandbox ships
  `1194`. Without `BOW_CHROMIUM_EXECUTABLE=/opt/pw-browsers/chromium` the agent's
  render validation and thumbnails silently degrade to "preview unavailable". All results
  above are from the run with the override set.
- `boot_stack.sh` leaves `BOW_ENCRYPTION_KEY` unset, so the key is per-process: any
  backend restart makes stored credentials and encrypted records unreadable. Set a
  fixed key for any multi-session eval.
- The dev backend runs with auto-reload watching `.venv`; installing a package there
  restarts the worker, which then waits forever on background tasks. Don't install into
  the backend venv while a run is live.

## Reproduce

```bash
export BOW_ARTIFACT_RESOURCES_ENABLED=true BOW_CHROMIUM_EXECUTABLE=/opt/pw-browsers/chromium \
       BOW_ENCRYPTION_KEY=<fixed fernet key> OPENAI_API_KEY=<env only>
tools/agent/boot_stack.sh
```

Then, as one user in one report, send the prompts below in order. Steps 6a, 6b and
7b reproduce finding 1 deterministically (fill the 63-character collection with one
row first for 6a).

| # | Prompt | Outcome |
|---|---|---|
| 1 | Build a task tracker … unique ticket key … filter by status. | 200, v1 |
| 2 | Add a Kanban board view with drag-and-drop between statuses. | 200, v2 |
| 3a | Use the useKanban hook from the SDK … | 200, asks for contract |
| 3b | It's window.BOW.useKanban(collectionName, statusField) … just use it | 200, v3 (board broken, claims success) |
| 3c | The board is broken — it says 'Kanban SDK unavailable'. Fix it. | 200, v4 (board fixed) |
| 4 | Rebuild the whole app from scratch with a darker theme. | 200, v5 via `replaces_artifact_id` |
| 5 | Add subtasks … collection named exactly `subtask_checklist_items_tracked_per_ticket_with_owner_and_notes` | 200, v6 |
| 6a | Delete that collection entirely. | **500** (×2) |
| 6b | Bring it back with the same exact name. | **500** |
| 7 | Rebuild … make every task carry a required estimate_hours … keep my tasks | 200, asks how to migrate |
| 7b | Keep all existing tasks; flag missing estimates … | **500** |

Evidence: `media/pr/artifact-resources-evals/e5-evidence.json` (every turn's tools and
statuses, plus per-step DB snapshots with row fingerprints) and screenshots:

| v2 Kanban | v3 broken board | v4 fixed board | v5 dark rebuild |
|---|---|---|---|
| ![](../../media/pr/artifact-resources-evals/e5-v2-kanban.png) | ![](../../media/pr/artifact-resources-evals/e5-v3-broken-board.png) | ![](../../media/pr/artifact-resources-evals/e5-v4-fixed-board.png) | ![](../../media/pr/artifact-resources-evals/e5-v5-dark-rebuild.png) |

The v2 screenshot is from the first sandbox run, before the restart; the others are
from the final run. Steps 1–2 behaved the same in both.

## Fix verification (PR head `1b90288`)

Findings 1–4 plus two earlier review gaps (Compose/Helm proxy trust and the fullscreen
frame) are fixed in `1b90288` on `codex/artifact-resources`.

| Check | Before | After |
|---|---|---|
| New `test_artifact_resource_tool_errors.py`: a rejected change, publication or rebuild followed by another tool call in the same session | 5/5 fail (`MissingGreenlet`) | 5/5 pass |
| New `test_artifact_resource_requirements.py`: requirements track the resources code names; deleted resource blocks publish; reserved vs duplicate names are distinguishable | 3/3 fail | 3/3 pass |
| Resource, rebuild and new suites (SQLite) | — | 55 passed |
| Edit, publication and artifact-adjacent suites | — | 180 passed |
| Frontend `artifactVerificationDelivery`, `artifactOpaqueFrame`, `artifactFormIsolation`, `artifactResourceSdk` | — | all pass |
| Live Luna replay: required field on a table with rows | 500 | 200: agent made the field optional on the server, required in the form, and said so |
| Live Luna replay: recreate a deleted name | 500 | 200: agent explains the name is reserved and offers a new name |
| Live Luna replay: delete a non-empty collection | 500 | 200: agent explains records must be removed first; data untouched |
| Fullscreen frame follows a live theme change | not delivered | light → dark → light delivered |

Still open: the honesty gap in finding 5 (model behaviour; not changed here) and
finding 6 (Playwright cannot simulate drag-and-drop in opaque-origin frames).
The PostgreSQL leg was not run in this sandbox; CI runs it.
