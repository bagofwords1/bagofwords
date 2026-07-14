# Feedback Loop — agent notes must be updated mid-run, not batched at the end

The agent-notes feature (`docs/feedback-loops/agent-notes.md`) shipped with a
known behavior gap, documented in its own Loop B: the agent's first action was
`create_note` with a `- [ ]` checklist, but it then ran every analysis step and
"finished with a single `edit_note` flipping every `- [ ]` → `- [x]`" at the
very end. A plan note that is stale for the whole middle of the run has failed
its purpose (cross-step memory). This loop validates the fix: the agent now
ticks items and records findings **after each step**, unprompted.

## Root cause (validated)

No structural blocker — pure incentive dynamics in the planner loop:

- Under serial emission ("HARD RULE: at most ONE tool_use per response",
  `prompt_builder_v3.py`), a note tick cost a full planner iteration, and
  "pick the smallest next action that produces observable progress" taught the
  model to defer it.
- With `ai_tool_concurrency` defaulting to 4, the MULTI-TOOL prompt variant
  could carry an `edit_note` alongside the next step's calls for free — but its
  text ("the same inspection repeated across data sources… dependent steps
  still go one per turn") steered models to file note ticks under "dependent",
  so they still batched them at the end.
- `notes_guidance` said *keep it ticked off* but never said **when**, and
  nothing deterministic connected "last action succeeded" with "the injected
  notes still show `- [ ]` items".

## The fix

Four changes, all in the planner surface:

1. **MULTI-TOOL piggyback rule** (`prompt_builder_v3.py`, system prompt,
   parallel mode only, gated on `notes_enabled`): a note update records the
   PREVIOUS action's outcome, so it is always independent of the next step —
   emit it in the same response instead of a separate turn.
2. **UPDATE TIMING rule in `notes_guidance`** (v3 user message): notes are
   updated as you go, never batched for the end; phrased per emission mode
   (same-response piggyback when parallel, immediate-next-action when serial).
   The v2 builder (knowledge harness) gets the same one-line cadence rule.
3. **Deterministic `<notes_nudge>`** (`PromptBuilderV3._notes_nudge`): fires on
   any iteration where the last action succeeded, was not itself a note tool,
   and the injected `<notes>` block still contains `- [ ]` items. Quiet on the
   first iteration, after failures, after note edits, and when the checklist is
   done. Prompt guidance decays over long runs; this fires exactly at the
   moment of the mismatch.
4. **`note_id` in the batch-aggregate whitelist** (`agent_v2.py`,
   `_aggregate_batch_observation`): a note edit inside a multi-action batch now
   keeps its `note_id` in the planner-facing observation entry.

Also: the RCA-loop prompt now tells the agent to record each hypothesis
verdict in the working note as it lands.

## Loop A — deterministic (no external services)

```bash
cd backend
export BOW_DATABASE_URL="sqlite:///db/app.db" TESTING=true
uv run pytest tests/e2e/test_agent_notes.py -v
# Regression: doc tools + concurrent dispatch + other prompt-builder suites
uv run pytest tests/unit/test_doc_markdown.py tests/e2e/test_doc_artifacts.py \
  tests/unit/test_concurrent_tool_dispatch.py \
  tests/unit/test_prompt_builder_v3_user_profile.py -q
```

Observed: the new
`test_notes_are_kept_current_mid_run_not_batched_at_the_end` FAILS on the
pre-fix code (verified by stashing the fix) and PASSES with it. It asserts the
three drivers independently: the timing rule renders whenever notes are
enabled; the `<notes_nudge>` fires exactly under the mismatch condition (and
stays quiet on first iteration / failure / note-edit / fully-ticked /
batch-containing-note-edit); the MULTI-TOOL text invites the edit_note
piggyback only when notes are enabled. All 12 agent-notes tests pass, plus 71
doc/dispatch regression tests and the other prompt-builder suites.

## Loop B — live confirmation (real LLMs; key via env only)

Full stack (`tools/agent/boot_stack.sh` + `seed_org.py --demo`), Music Store
(Chinook) demo, Anthropic provider with `claude-sonnet-5` (main default) and
`claude-haiku-4-5` (small default), org defaults untouched
(`enable_agent_notes` on, `ai_tool_concurrency` 4 → MULTI-TOOL mode).

**The prompt deliberately does NOT mention notes, checklists, plans, or
todos** — the behavior must emerge from the system prompt alone:

> Do a deep dive on our music store business — what is driving revenue across
> genres, countries and time, and is there anything surprising in the data?
> Finish with a short write-up of your findings and what we should focus on.

Observed (2026-07-14, run finished `success` in 190s). Notes timeline, polled
every 3s via `GET /reports/{id}/notes`:

| t (s) | note state |
|-------|-----------|
| 21.5 | note created: plan with 7 `- [ ]` items + a Definitions section |
| 30.7 | 1/7 checked — schema-inspection verdict recorded inline |
| 68.2 | 3/7 checked — genre + country findings appended to their items |
| 90.6 | 4/7 checked — flat-revenue anomaly recorded as SURPRISING |
| 114.5 | 5/7 checked |
| 127.5 | 7/7 checked — findings complete before the final write-up |

Ordered tool sequence from the completion blocks:

```
create_note → describe_tables → edit_note → create_data ×2 → edit_note
→ create_data → edit_note → create_data → edit_note → create_data
→ edit_note → create_doc
```

FIVE distinct mid-run `edit_note` calls, each landing right after the step it
records (piggybacked in the same decision as the next step's tools) — versus
exactly one end-of-run `edit_note` in the pre-fix Loop B of `agent-notes.md`.
Each checked item carries its finding inline ("→ Rock dominates: $826.65
(45%)…"), so the note doubles as the cross-step accumulator the design doc
asked for. Screenshots in `media/pr/notes-midrun-updates/` show the chat cards
(Edited note diffs between Created Data steps), the Summary Notes section, the
full plan note, and the final document.

## What this proves / regression notes

- The mid-run update behavior emerges without any note-related wording in the
  user prompt, at default org settings, on a real model (Sonnet 5 main +
  Haiku 4.5 small).
- The nudge is deterministic and cheap (a string count on the already-rendered
  notes block; no extra DB queries — the notes context was already rebuilt
  every iteration).
- Pre-existing quirks hit during setup (both unrelated to this change, both
  also noted or visible in the original agent-notes loop): `POST
  /api/llm/models` still references a missing `LLMService.create_model` (route
  exists, service method doesn't — models must be registered via the provider
  create/update payload), and provider-create model dicts require `name`
  despite the catalog carrying it (`NOT NULL constraint failed:
  llm_models.name` otherwise).
