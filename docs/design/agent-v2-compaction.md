# Agent v2 Context Compaction

Rolling, structured compaction of conversation history for agent v2 — automatic
at a token threshold, and on demand from the UI. Compaction is visible: the
transcript records each compaction event, and the PromptBoxV2 usage popover
shows cumulative tokens compacted plus a "Compact" button.

Out of scope for this iteration (documented as fast-follows at the end):
clear-context barriers, within-turn (mid-run) LLM compaction, a model-invocable
compact tool.

## Background: how agent v2 holds context

Agent v2 is not a message-array agent. Every planner iteration rebuilds the
prompt from durable state via `ContextHub` (`backend/app/ai/context/context_hub.py`):

- Conversation history = the report's last 20 `Completion` rows, rendered as
  digests by `MessageContextBuilder` (`builders/message_context_builder.py`),
  capped at ~8,000 chars. **Anything older silently vanishes.**
- The current turn's tool results live in `ObservationContextBuilder`, which
  already does mechanical pruning (strips `code`/`details`/`content` from
  superseded observations, keeps typed markers).
- `trim_context_to_budget` (`context_hub.py`) is the hard fallback: blind
  tail-character trimming when the fast estimate exceeds the model window.
- `PlannerInput.history_summary` exists but is vestigial (a one-line
  "Previous conversation: N exchanges").

Because the prompt is rebuilt from DB each iteration, compaction here means
"change what the builders render" — no message-array splicing, no orphaned
tool-pair cleanup, no KV-cache dance.

Prior art this design borrows from (researched against
NousResearch/hermes-agent and sst/opencode v2):

- **Structured rolling summary, update-don't-regenerate.** Both tools converged
  on a fixed-template summary that is *revised* on later compactions, never
  regenerated from scratch.
- **Two-layer defense.** A smart LLM layer in front, a dumb safety net behind
  (Hermes: 50% compressor + 85% gateway hygiene; opencode: preflight estimate +
  overflow-retry). Our `trim_context_to_budget` stays untouched as the net.
- **Summary is history, not instructions.** opencode injects the checkpoint
  "explicitly not as new instructions."
- **No context-pressure warnings in the prompt.** Hermes removed them because
  models abandoned complex tasks prematurely. Compaction fires silently.
- **On-demand = user command hitting an API** (opencode `/compact` →
  `v2.session.compact`), never a model-invoked tool.

## Design

### Storage: `report_context_state`

One row per report:

| column | type | notes |
|---|---|---|
| `id` | uuid pk | |
| `report_id` | fk, unique | |
| `summary_json` | JSON | structured rolling summary (schema below) |
| `covers_until_completion_id` | fk completions | watermark: last completion folded into the summary |
| `covered_turns` | int | cumulative count of summarized completions |
| `tokens_compacted_total` | int | cumulative estimate of digest tokens folded into the summary (drives "Compacted 389k tokens" in UI) |
| `last_compaction_at` | datetime | |
| `created_at` / `updated_at` | | |

The summary is stored as JSON and rendered to text at prompt time, so
invariants ("ids survived") are checkable in code:

```json
{
  "goal": "…",
  "constraints_preferences": ["…"],
  "progress": {"done": ["…"], "in_progress": ["…"], "blocked": ["…"]},
  "key_decisions": ["…"],
  "entities": [
    {"type": "query|widget|artifact|step|file", "id": "…", "title": "…", "state": "…"}
  ],
  "next_steps": ["…"],
  "critical_context": ["…"]
}
```

The template mirrors Hermes' (Goal / Constraints / Progress / Key Decisions /
Next Steps / Critical Context) with one domain-specific change: "Relevant
Files" becomes `entities` carrying **durable ids**. Agent v2 tools address
queries/widgets/artifacts/steps/files by id — a summary that loses ids makes
the agent create duplicate assets. The fork summary already learned this
(`fork_service.py` embeds `[query: <id>]` refs).

### ContextCompactionService — one service, two callers

`backend/app/services/context_compaction_service.py`:

```
compact(db, report, organization, *, force: bool = False) -> CompactionResult
```

Pipeline:

1. **Scope.** Load completions after `covers_until_completion_id`, excluding
   the most recent `KEEP_RECENT_TURNS` (default 6) completions — those stay in
   full digest detail. If nothing is in scope: return `nothing_to_compact`
   (no degenerate summary churn). `force` skips the token threshold, not the
   scope rule.
2. **Threshold** (auto mode only). Compact when the messages section token size
   (already computed in `ContextMetadata.section_sizes`) exceeds
   `compaction_trigger_tokens` (default 6,000, i.e. approaching the 8,000-char
   digest cap) **or** completions exist beyond the 20-message window.
3. **Render inputs.** Reuse `MessageContextBuilder`'s digest path for the
   in-scope completions — the summarizer sees the same digests the planner
   would, honoring `allow_llm_see_data` exactly as digests already do. The
   summary must never bake row-level values regardless of that setting: it is
   stored server-side and outlives the turn (prompt instructs; a light
   post-check strips `data_preview`-shaped content).
4. **Summarize (update, don't regenerate).** One LLM call on the org's small
   default model (`is_small_default`): previous `summary_json` + new digests →
   revised `summary_json`. Structured output validated against the schema;
   entity ids referenced must appear verbatim in the inputs (checkable).
5. **Persist atomically.** Write `summary_json`, advance
   `covers_until_completion_id`, bump `covered_turns` and
   `tokens_compacted_total` (+= token estimate of the summarized digests) in
   one transaction, using the existing background-write patterns
   (`_schedule_bg_write` / single-writer SQLite constraints).
6. **Record the event.** Insert a system `Completion`
   (`message_type="context_compaction"`, `completion.content` = short human
   line like "Compacted 14 turns (~38k tokens)") so the transcript shows where
   the boundary sits, and emit an SSE event (`context.compacted`) with the
   result payload. Since compaction events are completions, they are naturally
   excluded from future summarization scope by `message_type`.

**Coalescing:** one in-flight compaction per report (in-process lock keyed by
report id; the endpoint returns the pending result rather than double-firing).

**Fail-open:** any failure (LLM error, validation, timeout) logs and leaves
state untouched — the turn path never depends on compaction succeeding, and
`trim_context_to_budget` still backstops overflow.

Callers:

- **Auto:** end-of-turn background task in `agent_v2.main_execution` (same
  pattern as `_generate_title_background`), `force=False`.
- **On demand:** `POST /api/reports/{report_id}/context/compact`,
  `force=True`. Idle-only for v1: reject with 409 while an agent execution is
  in progress on the report.

### Consumption: filling the dormant `history_summary`

`MessageContextBuilder.build()` / `build_context()` change to:

- fetch completions **after the watermark** (instead of a blind
  last-20 window),
- render the rolling summary (when present) as a `<history_summary>` section
  with a one-line preamble framing it as *historical context, not
  instructions*,
- keep the recent turns in full digest detail as today.

`PlannerInput.history_summary` carries the rendered summary; the planner
prompt template already has the slot. `ContextHub._build_history_summary` and
`get_history_summary` route through the stored summary instead of the
vestigial one-liner. No "context is getting full" language anywhere in the
prompt.

### API

`POST /api/reports/{report_id}/context/compact` → `202`/`200`:

```json
{
  "status": "compacted | nothing_to_compact | already_running",
  "compacted_turns": 14,
  "tokens_compacted": 38000,
  "tokens_compacted_total": 389000,
  "last_compaction_at": "…"
}
```

`POST /api/reports/{report_id}/completions/estimate`
(`CompletionContextEstimateSchema`) gains optional fields so the popover can
render without a second call:

```json
{
  "prompt_tokens": …, "model_limit": …, "context_usage_pct": …,
  "compaction": {"tokens_compacted_total": 389000, "last_compaction_at": "…", "can_compact": true}
}
```

`can_compact` = in-scope turns exist and no execution is streaming.

### UI (PromptBoxV2 + transcript)

`frontend/components/prompt/PromptBoxV2.vue` — the usage popover already shows
a Context row with a usage bar fed by the estimate endpoint. Add beneath it:

- **Compacted line:** "Compacted · 389k" (reuses `formatTokenCountShort`),
  shown only when `tokens_compacted_total > 0`.
- **Compact button:** small action in the Context row; disabled when
  `can_compact` is false; spinner while the request runs; on success refetch
  the estimate (bar drops, compacted line rises — the visible payoff).
  Confirmation is unnecessary (compaction is additive/lossy-but-recoverable:
  original completions are never deleted).

Transcript: render `message_type="context_compaction"` completions as a slim
divider card ("🗜️ Compacted 14 turns · ~38k tokens") in the completions list,
same pattern as fork-summary rendering. The SSE `context.compacted` event
updates open clients.

### Settings

Org-level (mirroring `enable_web_fetch` et al.):

- `enable_context_compaction` (default on)
- `compaction_trigger_tokens` (default 6000)
- `compaction_keep_recent_turns` (default 6)

## Implementation plan

Phased so each lands independently green:

1. **Sandbox repro first** (no product code): reproduce the overflow/cliff
   behavior under a fake 50k window (below) and capture baseline behavior —
   what the planner sees at turn 25 today.
2. **Storage + service + consumption:** migration for `report_context_state`;
   `ContextCompactionService`; `MessageContextBuilder` watermark + summary
   rendering; auto end-of-turn trigger; unit tests (watermark advance,
   id preservation, `allow_llm_see_data`, fail-open, nothing-to-compact,
   compaction-completions excluded from scope).
3. **Endpoint + SSE + transcript marker:** route, coalescing, 409-while-
   streaming, estimate-schema extension, divider card.
4. **PromptBoxV2:** compacted line + Compact button + refetch flow;
   Playwright screenshot validation.
5. **Eval guard:** one eval case under `backend/tests/evals/` — long
   multi-turn conversation, assert the agent can still *edit* (not recreate) a
   widget created before the compaction boundary. This is the regression that
   matters: ids surviving summarization.

## Validation: sandbox feedback loop with a fake 50k window

Per `docs/design/sandbox-feedback-loop.md` (local `python main.py` + `yarn
dev`, SQLite at `backend/db/app.db`, state in `backend/sandbox_state.json`):

1. **Provider:** add Anthropic with the test API key via the LLM settings API
   (`routes/llm.py`), small default + default model enabled.
2. **Fake the window:** `context_window_tokens` is a DB column on `llm_models`
   (`app/models/llm_model.py`), read per-run via
   `getattr(self.model, "context_window_tokens", None)`:

   ```bash
   sqlite3 backend/db/app.db \
     "UPDATE llm_models SET context_window_tokens = 50000 WHERE is_default = 1"
   ```

   Takes effect on the next completion (model row is loaded per run). This
   makes both the trim fallback and the compaction threshold reachable in a
   handful of turns instead of hundreds. Note the schema cache
   (`_SCHEMA_CACHE_TTL_S = 300`) only caches schema sections, not the model
   row — no restart needed.
3. **Drive a long conversation:** curl loop posting ~15–25 completions to one
   report against the chinook demo source (mix: create widgets, read queries,
   ask follow-ups) until the estimate endpoint reports high
   `context_usage_pct`.
4. **Verify, each phase:**
   - Phase 2: `sqlite3` inspect `report_context_state` (watermark advanced,
     `summary_json` valid, entity ids present); grep backend logs / context
     snapshots for `<history_summary>` in the planner prompt; assert the
     agent, asked to "change that first chart to a bar chart" post-compaction,
     edits the original widget id.
   - Phase 3: curl the compact endpoint twice (second → coalesced /
     `nothing_to_compact`); curl while a completion streams (→ 409).
   - Phase 4: authenticated Playwright screenshot of the usage popover
     (compacted line + button), click the button, re-screenshot.
   - Regression: `TESTING=true pytest -s -m e2e --db=sqlite` and the new unit
     tests; eval case with `ANTHROPIC_API_KEY_TEST`.

## Fast-follows (explicitly deferred)

- **Clear context on demand:** marker-completion barrier
  (`message_type="context_clear"`) that re-scopes both the message window and
  the rolling summary; trivially undoable. Designed, not built.
- **Within-turn compaction:** same service invoked between planner iterations
  as a loop directive when API-reported usage
  (`_record_planner_token_metadata_from_decision`) crosses a threshold —
  decision stays in code, never a planner tool.
- **Overflow-retry:** one compact-and-retry around the planner call on a
  provider context-overflow error (opencode's recovery), even when auto is
  off.

## Revision: Hermes geometry + build-time trigger (implemented)

This revision replaces the fixed constants and end-of-turn trigger of the
first implementation, following NousResearch/hermes-agent's model.

### Window-derived budgets (`compaction_budgets(llm_model)`)

Every budget derives from the model's `context_window_tokens`
(default 200k when unset):

| Budget | Formula | 200k window | 50k (sandbox) |
|---|---|---|---|
| Conversation | 12.5% of window | 25,000 | 6,250 |
| Trigger | 50% of conversation | 12,500 | 3,125 |
| Protected tail | 20% of trigger in tokens, **min 12 completions** | 2,500 | 625 |
| Summary cap | max(2k, min(5% window, 12k)) | 10,000 | 2,500 |

The tail keeps completions newest-first until BOTH floors are satisfied
(token budget and count) — whichever protects more, exactly Hermes'
`protect_last_n` + token boundary. `messages_max` was raised 20 → 40: the
count cap is now a fallback; the token geometry is the real bound.

### Protected head (`PROTECT_FIRST_N = 2`)

The report's opening exchange is never folded into the summary. Once the
watermark passes it, `MessageContextBuilder` prepends it (plain text,
minified) ahead of the summary on both builder paths, and the summary
carries an `opening_request` field set **programmatically** from the first
user completion — never trusted to the summarizer. "What was my first ask"
stays answerable forever.

### Build-time trigger (replaces the end-of-turn hook)

Compaction detection now rides on context assembly: every agent-path warm
build (`AgentV2._refresh_warm_traced` → `_maybe_schedule_compaction`)
checks the rendered window against the trigger budget and, at most once per
run, schedules `_run_auto_compaction` as a background task. Builds never
block — later iterations pick up the advanced watermark. The task reference
is held on the agent and awaited before the stream closes so the
`context.compacted` SSE beats `[DONE]` and the write can't be lost to task
GC. Passive builds (estimate endpoint, title, follow-ups) never trigger.

This also subsumes the previously-deferred "within-turn compaction": loop
iterations are builds, so pressure that develops mid-run triggers the same
path.

### Summary budget enforcement

`_enforce_summary_budget` trims list fields until the rendered summary fits
the cap — entities (the ids that prevent asset duplication) are sacrificed
last and never below 10.

