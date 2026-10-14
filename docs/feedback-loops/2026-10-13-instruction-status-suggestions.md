# Feedback Loop — "in the edit instruction tool, can we set inactive?"

The agent had no way to turn an instruction off: `edit_instruction` could only
change text and metadata. The review flow couldn't have shown such a change
either, because it only understood text. This loop shows a status-only
suggestion is now staged, reviewable, pending, and applied (or not) exactly as
the reviewer decides, in both the Knowledge Explorer and the chat card.

## Root causes (validated)

1. **No status input.** `EditInstructionInput` had no status field, and the
   tool always staged `status="published"`
   (`backend/app/ai/tools/implementations/edit_instruction.py`, old `:776`). So
   whole-build publish of an AI *text* edit to an Inactive instruction turned it
   back on.
2. **`load_mode` silently coerced.** Any value outside `always|intelligent`
   (e.g. `disabled`, the closest thing to "off") became `intelligent` (old
   `:690-692`), so the instruction loaded in *more* places.
3. **Review was text-only.** `review_hunks`, the org-wide pending sweep
   (`get_pending_change_instruction_ids`), the settle pass and
   accept/reject all diffed text only (`backend/app/services/instruction_service.py`).
   A status-only suggestion had zero hunks, so it showed no pending badge. The
   review panel emitted `empty`, so the chat card read "Accepted"
   (`frontend/components/tools/EditInstructionTool.vue`, `@empty`). And
   `accept_all_hunks` rebuilt the version from MAIN's `status`/`load_mode`
   (old `:2533-2534`), dropping any metadata change the suggestion carried.
4. **Promotion only ever activated.** `promote_build` copied a version's
   status only when it was `published`
   (`backend/app/services/build_service.py`, old `:1269`).

## Loop A — deterministic (no external services)

```bash
cd backend && export BOW_DATABASE_URL="sqlite:///db/app.db" TESTING=true
uv run pytest -m e2e --db=sqlite -q tests/e2e/test_instruction_status_suggestion.py
uv run pytest -q tests/unit/test_field_changes.py
```

On the base commit, 12 of the 16 e2e tests fail. No `status` input, no
`field_changes` in `/review-hunks`, nothing pending, and a whole-build publish of
a text edit re-activates an Inactive instruction. The other 4 pass there; they
guard that publishing still behaves the same when no status is involved. One of
them (a text edit on a create suggestion that isn't live yet) caught a real bug
in an intermediate version of the fix. With the fix, all 16 pass on SQLite and
on Postgres (`--db=external` against a local PG16).

## Loop B — live UI (seeded stack, no LLM)

1. `tools/agent/boot_stack.sh --dev`, then `seed_org.py --demo`.
2. Create a live instruction via `POST /api/instructions/global`, then run
   `EditInstructionTool` in-process against the stack's DB with
   `{"status": "inactive", "evidence": ...}`. That is a real tool result, not a
   mock.
3. Open `/agents/instructions/<id>`. Separately, inject the tool block into
   `/reports/<id>` (same technique as
   `frontend/tests/reports/reading-stream-evidence.mjs`).

| | Before | After |
|---|---|---|
| Agents page | "Active", no pending badge, nothing to review | "1 pending", **Status Active → Inactive** row with Accept / Reject; text muted |
| Chat card | "Staged in draft build", no mention of status | "Turn off" chip, same row inline; Accept → "Accepted", instruction shows **Inactive**, badge cleared |

A text-only edit on the now-Inactive instruction shows one text hunk and no
status row. The instruction stays Inactive.

Evidence: `media/pr/instruction-status-suggestions/` (en, he, dark, accept
flow GIF).

## The fix

- `app/services/field_changes.py`: a pure 3-way rule for field changes
  (`status`, `load_mode`). A change is the suggestion's intent (base →
  proposed). It stays live until main carries it or a reviewer resolves it.
  Keys (`field:<field>:<value>`) share the build's `rejected_hunks` ledger with
  text hunks.
- `instruction_service`: `review_hunks` returns `field_changes` per
  suggestion. The pending sweep, settle, accept and reject all treat them as
  action units. Accept applies only the accepted field values; everything else
  still comes from main.
- `build_service.promote_build`: applies a staged `draft` for AI builds
  (including when auto-merge publishes a stale AI build). User, git and rollback
  builds are unchanged.
- `edit_instruction`: `status: active|inactive`. With no status requested, the
  version carries the current status, so a text edit never toggles it. Invalid
  `load_mode` values are rejected, and status changes are refused in knowledge
  mode.
- UI: field-change rows in `InstructionTrackedChanges.vue`, counted in the
  totals and in Accept/Reject all. The chat card gets a "Turn off/on" chip, and
  `useTrackedChanges` includes field keys in its selections.

## Regression notes

- The related backend suites (builds, git sync, every `test_instruction*`,
  overlay review, read/search instructions, training accept, text hunks) pass:
  239 tests.
- During Loop B the dev backend twice stopped answering requests after the
  report-page captures. It accepted connections, but the event loop sat idle.
  It happened on the base code as well (after the "before" capture), so it's
  unrelated to this change; restarting the backend cleared it.
