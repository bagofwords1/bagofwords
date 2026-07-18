# Feedback Loop — "the agent can't see out-of-band UI actions (feedback, model change, share…)"

The agent only sees `role='user'` / `role='system'` completions. When a user
thumbs a message, switches model, uploads/removes a file, changes agent scope,
or shares an artifact, nothing records that in the conversation, so the next
turn builds a stale picture of the world (re-suggests a rejected instruction,
doesn't know why the previous run was thumbed down, etc.).

This introduces a **silent session-event ledger**: `role='event'` completions,
the passive sibling of `role='external'`.

- `role='external'` — something happened AND the agent runs now (eval/webhook):
  keeps the three-completion trigger idiom (hidden trigger + system reply).
- `role='event'` — something happened and nobody runs: it sits in the log and
  is read on the agent's next natural turn, interleaved chronologically.

Policy is three per-kind maps in code (not columns, so flipping a kind is a
one-line edit, not a migration):

| map | meaning | default |
|---|---|---|
| `EVENT_LLM_HIDDEN` | kinds NOT rendered into agent context | LLM-visible |
| `EVENT_UI_VISIBLE` | kinds rendered as a timeline strip | hidden |
| `EVENT_DURABLE` | kinds folded into the compaction summary | ephemeral |

Source of truth: `backend/app/ai/context/session_events.py`.

## Root cause (validated)

`MessageContextBuilder.build_context()` / `build()`
(`backend/app/ai/context/builders/message_context_builder.py`) only branch on
`role == 'user'` and `role == 'system'`; any other role in the window renders
nothing. There was no table/row for out-of-band UI actions and no emit path.

## Loop A — deterministic reproduction (no external services)

`backend/tests/unit/test_session_events.py` — SQLite in-memory, no LLM/network.

```bash
cd backend
export BOW_DATABASE_URL="sqlite:///db/app.db"
uv run pytest tests/unit/test_session_events.py -q -p no:warnings
```

Observed **before** the change (conceptually — the emit path and the `role='event'`
branch did not exist): events could not be inserted, and even a hand-inserted
`role='event'` row rendered nothing in context. The tests assert the fixed
behavior:

- `test_emit_inserts_event_without_incrementing_turn` — emit writes a
  `role='event'` row carrying the last turn's `turn_index` (a silent event does
  not start a turn).
- `test_event_renders_interleaved_in_context` — an LLM-visible event renders as
  `Event (HH:MM): …` between the turns it falls between.
- `test_events_do_not_consume_turn_budget` — 10 events + `max_messages=4` still
  renders all 4 conversational turns (events are fetched by time-range, never as
  window slots).
- `test_hidden_kind_never_reaches_context` — `export_downloaded` (audit-only)
  never renders.
- `test_consecutive_same_kind_events_collapse` — three model toggles collapse to
  one `(×3)` line keeping the latest value.
- `test_compaction_folds_durable_drops_ephemeral` — in the fold digest, durable
  feedback survives while an ephemeral model-change is dropped.
- `test_feedback_service_hook_emits_events` — the real
  `CompletionFeedbackService` hook emits `feedback_given/changed/removed` keyed
  to the completion.

**After the fix:**

```
30 passed
```

(the file above plus `test_context_compaction.py` and the two
`test_report_context_*` regressions — no compaction regressions).

## The fix

- `backend/app/ai/context/session_events.py` — kinds + the three policy maps +
  `default_event_content()`.
- `backend/app/services/session_event_service.py` — `emit()` / `emit_safe()`:
  insert a `role='event'` completion (turn_index = last turn's; empty
  `completion`; `prompt.content` + `prompt.meta`). The existing
  `after_insert_completion` listener broadcasts it over the websocket for free.
- `MessageContextBuilder` — excludes events from the `max_messages` window query,
  merges LLM-visible events back in by timestamp, collapses same-kind runs, and
  in the compaction fold path renders only durable events.
- `ContextCompactionService._load_scope` — computes the tail/watermark over
  conversational turns only, then appends durable events in the folded range so
  feedback/rejections survive the summary while ephemeral events vanish.
- Emit hooks wired: `CompletionFeedbackService` (create → `feedback_given` /
  `feedback_changed` on a direction flip, delete → `feedback_removed`) and the
  report-scoped file routes (`POST /files` with a `report_id` → `file_uploaded`;
  `DELETE /reports/{id}/files/{fid}` → `file_removed`).
- Event text is an **impersonal announcement** ("targets.xlsx was uploaded",
  "Agent scope changed — added …", "Model was switched to …"), not actor-subject
  phrasing. The acting user is still recorded in `prompt.meta.actor` for a
  future name/avatar.
- Frontend: `components/SessionEvent.vue` (minimalistic gray strip, per-kind icon
  map, dark-mode aware) + a `role='event'` branch in
  `pages/reports/[id]/index.vue` gated on `isEventUiVisible` (mirrors
  `EVENT_UI_VISIBLE`). Live surfacing does **not** rely on the websocket: a
  report-scoped upload/removal emits a `filesChanged` event that debounce-reloads
  the timeline.

## Loop B — live confirmation (real running stack)

```bash
tools/agent/boot_stack.sh --dev
cd backend && uv run python ../tools/agent/seed_org.py       # org + admin token
# dismiss onboarding (PUT /api/organization/onboarding {dismissed,completed})
uv run python scripts/insert_events_report.py <admin_user_id> <org_id>   # prints report id
cd ../frontend && EVENTS_REPORT_ID=<id> node tests/reports/shoot-session-events.mjs
```

Confirmed end-to-end through the real backend: `GET /reports/{id}/completions`
returns the `role='event'` rows, and the timeline renders only the UI-visible
kinds (`file_uploaded`, `agent_scope_changed`, `artifact_shared`) as strips —
`feedback_given` and `llm_changed` are correctly hidden. Evidence:
`docs/design/session-events-samples/session-events-{light,dark}.png`.

## Samples

`docs/design/session-events-samples/`:

- `01_context_build_context.txt` — the LLM-facing conversation with events
  interleaved.
- `02_context_conversation_section.txt` — the object builder's `<conversation>`.
- `03_compaction_fold_digest.txt` — durable events kept, ephemeral dropped.
- `00_policy_manifest.txt` — the per-kind llm/ui/durable policy.
- `session-events-{light,dark}.png` — the timeline strips in both themes.

Regenerate the text samples: `cd backend && uv run python
scripts/gen_session_event_samples.py docs/design/session-events-samples`.

## What this proves / follow-ups

Proves events insert without disturbing turns, render (or hide) per policy in
both the LLM context and the UI, never steal the turn budget, and fold correctly
through compaction. Feedback is the one production hook wired here; the remaining
kinds in the taxonomy (`run_stopped`, `llm_changed`, `file_*`,
`agent_scope_changed`, `report_*`, `artifact_*`, `instruction_*`) are one
`SessionEventService.emit_safe(...)` call at their existing UI hook points — the
infrastructure and rendering already support every kind.
