# Feedback Loop — sub-agent question/answer messaging: recovery paths (PR #1406 review)

The review of `codex/sub-agent-messaging` (head `6a1be30`) left nine inline
findings. This loop reproduces each one deterministically against the real
services and DB, fixes what reproduced, and shows the two UI-facing changes in
the live stack. One finding did not reproduce and one was judged not worth a
code change; both are recorded below with the evidence.

## Root causes (validated)

1. **`status='waiting'` meant two things** (`sub_agent_service.py` snapshot /
   `_run_brief`, `read_sub_agent.py:78`). A child whose question was answered
   but cannot reclaim a concurrency slot stays `waiting`. Every surface said
   "waiting for an answer", so the parent re-answered and `send()` rejected it
   ("This question already has an answer"); the UI said "Waiting for parent".
2. **Only an authorization failure released blocked children**
   (`completion_service.py` `_run_dispatched_agent`). A question wake that died
   for any other reason (no default model, provider error) left the asking
   child in `waiting` until the wave deadline, and the queue stayed paused.
3. **The wake was a bare hidden `role='user'` row** (`_queue_parent`). Every
   other machine turn uses the three-row idiom (visible `role='external'`
   event + hidden trigger + reply), so the parent's answer appeared as an
   orphan assistant turn with no strip, in any locale.
4. **Finish replay raised `FinishConflict` after its note was deleted**
   (`record_finish`). The incoming record is normalized (`report_note_id` →
   `None` when the note is gone) but the stored one was not, so an identical
   retry compared unequal.
5. **Stale-turn recovery was rolled back** (`start_next_queued_if_idle`). The
   early return on "another turn is running" closed the session without a
   commit, discarding the `status='error'` mutation
   `_get_running_system_completion` had just made.
6. **Mailbox queries on every planning step** (`agent_v2.py`
   `_apply_sub_agent_planner_input`). `receive()` opened a session and ran
   ~5 queries plus an UPDATE/commit per step for every sub-agents-enabled chat,
   even reports that never delegated.
7. **Queued JSON fallback pushed a bare placeholder**
   (`pages/reports/[id]/index.vue` `startStreaming`). The row had no
   `created_at` / `completion_blocks`, unlike the page's own optimistic
   `system-*` placeholder, and it survives a failed history refresh.

Not reproduced / not changed:

- **NULL `wave.user_id` defeats coalescing** — refuted. SQLAlchemy compiles
  `Completion.user_id == None` to `IS NULL`; two questions on a wave with no
  principal coalesce onto one wake and `fail_waiting_children_for_parent(
  user_id=None)` matches them. Kept as a regression test
  (`test_wave_without_a_principal_still_coalesces_wakes_and_releases_its_children`
  passes on the unfixed head).
- **`lock_report` on every streamed message** — left as is. The lock is also
  what stops the queue dispatcher from double-starting a turn against a submit
  that carries no queue flag: the dispatcher locks, checks for a running turn,
  and would otherwise claim a wake while the user's system row is still
  uncommitted. It is held only until the completions insert commits.

## Loop A — deterministic (no external services)

```bash
cd backend && export BOW_DATABASE_URL="sqlite:///db/app.db" TESTING=true
uv run pytest -q -m e2e --db=sqlite tests/e2e/test_sub_agent_messaging_recovery.py \
  tests/e2e/test_sub_agent_messages.py tests/e2e/test_completion_queue_steer.py tests/e2e/test_sub_agents.py
```

Before the fix (head `6a1be30`): the new module fails `7 failed, 1 passed, 1 error` (the error is the visible-event test, which needs the `live_client` fixture export added alongside):

| Test | Failed on |
|---|---|
| `test_answered_child_waiting_for_a_slot_is_not_reported_as_needing_an_answer` | `KeyError: 'waiting_for'` |
| `test_read_sub_agent_tells_the_parent_an_answered_child_waits_for_a_slot` | summary still says "…answer it" after the answer |
| `test_any_failed_question_wake_releases_the_blocked_children[no_model]` | child `pending`, expected `failed` |
| `test_any_failed_question_wake_releases_the_blocked_children[provider_error]` | child `pending`, expected `failed` |
| `test_question_wake_is_a_visible_machine_event_with_a_hidden_trigger` | no `role='external'` row |
| `test_finish_replay_is_idempotent_after_its_note_disappears` | `FinishConflict` on the identical retry |
| `test_dispatcher_retires_abandoned_turns_even_while_another_turn_is_live` | stale row still `in_progress` |
| `test_wave_without_a_principal_still_coalesces_wakes_and_releases_its_children` | passes (finding refuted) |

After: `9 passed` in the new module; `43 passed` across the four files above
(29 pre-existing + 9 new + 5 wave tests).

## Loop B — live stack (seeded state, no LLM needed)

```bash
cd backend && BOW_DATABASE_URL=sqlite:///db/agent.db BOW_ENCRYPTION_KEY=<fernet> \
  BOW_CHROMIUM_EXECUTABLE=/opt/pw-browsers/chromium uv run python main.py &
cd frontend && yarn dev &
cd backend && uv run python ../tools/agent/seed_org.py --demo   # admin + Music Store
# POST /api/llm/providers (any provider), POST /api/reports on the demo source,
# then seed through the real services: two pending children in one wave,
# child A asks a question, child B asks, is answered, and is set to 'waiting'.
```

The seeded report has, in `completions`: one `role='external'`
`trigger_source='sub_agent_message'` row (`in_progress`) and one hidden
`role='user'` queued trigger. `GET /reports/{id}/completions` lists only the
external row.

| Check | Before | After |
|---|---|---|
| Timeline after a child asks | no row (orphan reply later) | strip "Sub-agent 'Conversion funnel check' asked a question" (`media/pr/sub-agent-messaging-recovery/after-timeline.png`) |
| Background tasks popover, answered child | "Waiting for parent" (`media/pr/sub-agent-messaging-recovery/before-tasks.png`) | "waiting for a slot" (`media/pr/sub-agent-messaging-recovery/after-tasks.png`) |
| `read_sub_agent` on the answered child | "is waiting for an answer… answer it" | "has its answer and is waiting for a free slot… Nothing to answer." |
| Browser console errors on the report page | 0 | 0 |

Not covered live: the queued-JSON submit path (root cause 7). Opening the
report page stops a hand-seeded `in_progress` system row within seconds
("Run was stopped" event), so the busy branch never triggers without a real
running turn, and the sandbox's Anthropic key had no credit. The change is a
pure shape alignment with the existing optimistic placeholder; the queued
JSON contract itself is covered by `tests/e2e/test_completion_queue_steer.py`.

## The fix

- **Backend**
  - `sub_agent_message_service.py`: `_queue_parent` writes the visible
    `role='external'` event (meta `wave_id`/`sub_agent_id`/`title`) and links it
    from the hidden trigger's prompt; `close_wake_event` and
    `cancel_answered_wakes` mirror the outcome on the event;
    `children_with_open_questions` for list views.
  - `sub_agent_service.py`: `snapshot`/`_run_brief` expose `waiting_for:
    'answer' | 'slot'` and keep `elapsed_s` while waiting; `record_finish`
    normalizes the stored note id before the replay comparison; `has_waves`.
  - `completion_service.py`: dispatcher commits stale recovery on the
    early-return path; any failure of a question wake releases the blocked
    children (auth or not) and marks the event; success closes the event.
  - `agent_v2.py`: parent mailbox is read only once the report has waves (one
    EXISTS per step until then, remembered after the first delegation).
  - `read_sub_agent.py`: distinct summary for an answered child waiting for a
    slot.
- **Frontend**
  - `pages/reports/[id]/index.vue`: queued placeholder matches the optimistic
    one; `sub_agent_message` event strip label/icon.
  - `utils/subAgentStatus.ts` + `BackgroundTasksIndicator`, `DelegateTool`,
    `ReadSubAgentTool`, `ChatSummary`: status key resolves `waiting_slot`.
  - `locales/*.json`: `tools.delegate.status.waiting_slot` (all ten),
    `events.subAgentQuestion` (en/es/he, where the other wave events live);
    `waiting` lowercased in en/es to match its siblings.

## Regression notes

- A wake that fails for a runtime reason now fails the asking child with the
  error text, the same outcome the authorization path already had. A system-
  authored "answer" that lets the child finish on its own would be a product
  decision, not taken here.
- The caveat suffix-stripping in `record_finish` is unchanged; the replay
  bug was the note id, not the caveat.
