# Context compaction: one budget, one ladder, `/compact`

Status: implemented (single PR) · Owner: agent runtime · Related:
`docs/design/agent-context-durability-plan.md` (§6 compaction ordering)

## 1. Problem

Users report that compaction "doesn't work". Reading the code, it runs, but:

- **It compacts the wrong slice.** Rolling compaction only folds cross-turn
  chat history, which is budgeted at 12.5% of the model window. The usage bar
  shows the whole prompt (schemas, instructions, tool transcript). Clicking
  **Compact** barely moves it, or is disabled ("Nothing to compact yet").
- **The trigger measures the wrong thing.** `_maybe_schedule_compaction`
  (`backend/app/ai/agent_v2.py:8605`) estimates `len(rendered)/4` over a
  history render that minifies all but the last 7 items
  (`backend/app/ai/context/sections/messages_section.py:16`). So it really
  fires on a 40-item count. It also sizes the budget from the **small** model's
  window and ignores the provider-reported prompt size that we already record
  (`_last_planner_prompt_tokens`, `agent_v2.py:8563`).
- **It fires late.** Compaction runs in the background, once per run, and only
  affects the *next* run.
- **The summary is dropped first under pressure.** `trim_context_to_budget`
  (`backend/app/ai/context/context_hub.py:177`) halves `messages_context`
  keeping the *tail* (`context_hub.py:142-148`). The `<history_summary>` is
  rendered at the *head* of that section, so it is the first text cut.
- **The summarizer is fragile.** Its input is unbounded (the first fold of a
  long report sends every digest in one small-model call). There is no output
  cap, no retry, no fallback, and no cooldown. On failure it returns `error`
  and tries again on every run.
- **There is no feedback.** Manual errors go to `console.error`
  (`frontend/components/prompt/PromptBoxV2.vue`, `compactContext`). Auto
  compaction only moves a divider. No one ever sees "X → Y tokens".
- **There is no `/compact` in the composer.** The only entry is a button in the
  usage popover, it is idle-only (409 while a run streams), and it takes no
  focus instructions.

Today, four uncoordinated mechanisms each use their own budget and estimator:

| Mechanism | When | Budget | Estimator |
|---|---|---|---|
| `trim_context_to_budget` | before each request | window − 8k | chars/4 per section |
| `Transcript.fit_to_budget` (`prompt_builder_v3.py:159`) | before each request | 50% of window | transcript estimate |
| Rolling compaction (`context_compaction_service.py`) | background, next run | 6.25% of small-model window, or 40 items | chars/4 of minified render |
| `_handle_context_overflow` (`agent_v2.py:3091`) | after a provider rejection | shrink factor | provider message |

## 2. Goals / non-goals

**Goals**
1. One budget model: a single token count and a single set of thresholds,
   derived from the **main** model's window.
2. Compaction runs **before the request** that would exceed the budget, never
   one run late.
3. A cheap, no-LLM step runs first. The LLM summary runs only if the cheap step
   is not enough.
4. The summary and the opening exchange are never trimmed.
5. Summarization is bounded, retried, has a deterministic fallback, and backs
   off after failures.
6. `/compact [focus]` works from the prompt composer, including while a run is
   streaming (it is deferred, not rejected).
7. Users see what happened: progress, before → after tokens, and warnings.

**Non-goals (this PR)**
- Provider-native server-side compaction.
- A memory flush turn before compaction.
- Rewriting history builders onto the durable transcript (plan §7).
- Deleting or rewriting stored completions. Originals stay as they are; only
  the watermark moves.

## 3. Design

### 3.1 Budget model (`backend/app/ai/context/context_budget.py`, new)

A single module that every caller uses:

```python
@dataclass(frozen=True)
class ContextBudget:
    window: int          # main model context_window_tokens (fallback 200k)
    output_reserve: int  # min(20_000, window // 4)
    usable: int          # window - output_reserve
    cheap_at: int        # 0.60 * usable  → run the cheap step
    summarize_at: int    # 0.75 * usable  → run the LLM fold
    target: int          # 0.40 * usable  → aim here after compacting
    tail_tokens: int     # clamp(0.025 * window, 2_000, 25_000), ≤ 20% of usable
    summary_max: int     # clamp(0.05 * window, 2_000, 12_000)

def budget_for(model) -> ContextBudget: ...
```

- Ratios can be overridden through `BOW_CONTEXT_CHEAP_RATIO`,
  `BOW_CONTEXT_SUMMARIZE_RATIO` and `BOW_CONTEXT_TARGET_RATIO`, so tests and
  the sandbox loop can force each rung (same pattern as
  `BOW_TRANSCRIPT_BUDGET_RATIO`).
- Always built from `self.model`, never `self.small_model`. The small model
  only writes the summary. Its own window bounds the summarizer **input**
  (§3.4), not the trigger.
- `compaction_budgets()` and `transcript_budget_tokens()` are reimplemented on
  top of `budget_for()`. The ratio constants in
  `context_compaction_service.py` are removed.

**Token count ("anchor + delta").** `estimate_planner_input_tokens()` is a
cheap estimate of the variable part of the prompt: the free-text
`PlannerInput` fields plus the larger of the transcript and the JSON
observations (they describe the same steps on the two prompt paths). After
every planner call, the provider's `prompt_tokens` and our estimate of that same
request give an **offset**. That offset is mostly the fixed prompt we never
estimate: system prompt, tool catalog, file schemas. `TokenAnchor.measure()` is
estimate + offset, floored at half the estimate.

- The offset is persisted on `ReportContextState.last_prompt_tokens` and
  `last_prompt_estimate` at the end of each run, so a run's first request is
  measured from reality too.
- Without any anchor, the estimate alone is used. The overflow path still
  catches an under-count.

Measured live: the fixed prompt of this app is about 44k tokens (system prompt
+ tools + one CSV schema). That is why a percentage-of-window trigger on a
history render alone could never be right.

### 3.2 The ladder (`ContextGovernor`, new, in `context_budget.py`)

It runs **synchronously before every planner request**, at the point where the
loop calls `trim_context_to_budget` today (`agent_v2.py:5655-5678`). It
replaces that call, the build-time scheduler and the overflow handler's ad-hoc
levers.

```
n = measure();  history = rendered conversation + rehydrated history turns
if n <  cheap_at:      send
1. CHEAP (no LLM)
   - prepare ahead: background fold (once per run, only if history ≥ tail)
   - transcript: Transcript.fit_to_budget() toward cheap_at, at most ~40% of
     the transcript per request unless the prompt would not fit at all
   - history render: decay_level 1, then 2 (older messages minified; ids kept)
2. FOLD (LLM, persisted) — only when n ≥ summarize_at AND history ≥ tail_tokens
   - context_compaction_service.compact(mode="auto_sync"), emits
     context.compacting / context.compacted
   - reload the messages section AND the transcript's rehydrated history prefix
     (it was loaded before the fold and still holds the folded turns)
3. LAST RESORT (lossy, logged)
   - trim_context_to_budget: <history_summary> and <opening_exchange> pinned,
     conversation trimmed by whole oldest lines
send
```

- **Nothing to fold, nothing to show.** When the fixed prompt alone is over a
  threshold and history is small, the governor neither folds nor schedules a
  background fold. The user never sees a pointless "Compacting…" row.
- **Prepare ahead.** When `n` crosses `cheap_at` at the end of a run, schedule
  the existing background fold (reuse `_run_auto_compaction`). In most cases
  the watermark has already advanced before the next request reaches
  `summarize_at`, so rung 2 is a no-op and adds no latency. This replaces the
  current background trigger; it no longer uses the chars/4 or 40-item rules.
- **Overflow.** On a `context_length` rejection, re-enter the ladder with
  `target` tightened from the provider's actual/limit numbers (keep
  `_shrunk_context_factor`). Allow at most 3 overflow attempts per run, shared
  across the planner-error and loop-error paths. After that, fail the turn with
  a typed error: "This conversation is too large for {model}. Use /compact,
  start a new report, or pick a model with a larger context window."
- **Pinned content in `trim_context_to_budget`.** Today `MessagesSection` puts
  the summary at the head of `messages_context`. The trimmer gets a
  `pinned_prefix` (summary + opening exchange) that it never cuts. It trims the
  remainder by dropping whole items oldest-first, not by slicing chars.
- **`messages_max` stops being a silent drop.** Items past the count cap are
  either folded (rung 2) or kept. The 40-item cap stays only as a safety
  backstop at 200, and hitting it schedules a fold.

### 3.3 Cheap step for cross-turn history

`MessageContextBuilder.build()` gets a `decay_level: int` (0 = today,
1 = tool digests outside the tail become `Tool: <name> → <ids> (<status>)`
stubs, 2 = also drop assistant prose older than the tail down to its first
sentence plus ids). The governor rebuilds the messages section at a higher
decay level instead of char-slicing. Ids always survive, so `read_query`,
`read_artifact` and `read_file` can re-fetch.

### 3.4 Summarizer hardening (`context_compaction_service.py`)

- **Bounded input.** The digest text is capped at
  `min(0.5 * small_window, 160k chars)` and filled by priority:
  1. all user messages verbatim (≤2k chars each)
  2. the newest turns in scope
  3. the oldest turns in scope
  4. sampled middle runs with `[… N turns omitted …]` markers

  If the scope still does not fit, fold in **chunks**, oldest first. Each
  chunk updates the rolling summary and advances the watermark, so a partial
  success is kept.
- **Output size.** Enforced after the call: the rendered summary is trimmed
  to `summary_max`, with entities trimmed last. A 60s timeout bounds the call
  itself; on timeout the deterministic fallback runs.
- **Retry.** On unparseable JSON, retry once with a "return only the JSON
  object" corrective suffix.
- **Deterministic fallback.** If the LLM fails or times out, build the summary
  without an LLM:
  - `opening_request`
  - the last 10 user asks verbatim (`pending_user_asks`)
  - entities from tool executions in scope (ids + titles)
  - the last assistant reply, first 500 chars (`critical_context`)

  It is stored with `method="fallback"`, `degraded=true`.
- **Grow guard.** If the rendered summary is ≥ the digest tokens it replaces,
  do not commit. Return `status="refused_grow"`.
- **Cooldown.** After a failure, `failure_count += 1`. Auto mode is skipped
  until `cooldown_until` (60s → 5m → 15m). Manual `/compact` ignores the
  cooldown.
- **Schema additions.** Add `pending_user_asks` and `errors_and_fixes` to
  `_SUMMARY_KEYS`, the prompt and `render_summary_for_prompt`. Keep
  `_validate_entities` and the programmatic `opening_request`.
- **Focus instructions.** `compact(..., instructions: str | None)` appends an
  escaped block, as data, to the summarizer prompt: "Prioritise preserving
  information about: <focus>. Spend most of the summary budget on it."
- **Manual tail.** `mode="manual"` keeps the last **2 user exchanges** (or
  `keep=N`) instead of the 12-completion floor. Auto modes use
  `budget.tail_tokens` with a floor of 1 user exchange.
- **Telemetry.** Log one content-free line per attempt: `trigger`
  (auto_bg / auto_sync / overflow / manual), `method` (llm / llm_retry /
  fallback), `turns`, `tokens_before`, `tokens_after`, `duration_ms`, `status`.

### 3.5 `/compact` in the prompt composer

**Frontend (`frontend/components/prompt/PromptBoxV2.vue`, `MentionInput.vue`)**
- **Parsing.** `submit()` checks `^\s*/compact(?:\s+(.*))?$`s **before** the
  queue/submit branch. Matched text never becomes a completion.
  - `/compact`: plain manual compaction.
  - `/compact keep 4`: keep the last 4 exchanges.
  - `/compact <anything else>`: focus instructions (max 800 chars).
- **Slash menu.** Typing `/` as the first character opens a small command menu
  (reuse the mention dropdown styles; one entry: `/compact` with a hint). The
  menu is a registry (`frontend/utils/slashCommands.ts`) so more commands can
  be added later.
- **Idle.** Call `POST /reports/{id}/context/compact` with
  `{instructions, keep}`. Show an inline status chip above the composer
  ("Compacting…"), then a result:
  `Compacted 18 turns · ~120k → ~38k tokens`. If `degraded`, add "(fallback
  summary)".
- **While a run streams.** Keep a single pending local command (chip: "Will
  compact when this run finishes · ✕"). Fire it when `latestInProgressCompletion`
  clears, *before* the next queued prompt is dispatched. Queued prompts wait
  for it.
- **Errors.** Show them as a toast with the server's message, never only in the
  console.
- The popover **Compact** button calls the same function. It stays enabled
  whenever a manual run would find more than 2 exchanges.

**Backend (`backend/app/routes/completion.py:57`)**
- **Request body.**
  `CompactRequest {instructions: str | None (≤800), keep: int | None (1..20)}`.
- **Response.** Adds `tokens_before`, `tokens_after` (prompt estimates for the
  next request, measured with `measure()`), `method` and `degraded`.
- **409 while busy is kept** as a server-side guard. The composer never hits it
  because it defers.

### 3.6 What the user sees

- **SSE events.**
  - `context.compacting {trigger}`: shows a transient "Compacting context…" row
    in the stream.
  - `context.compacted`: now also carries `status`, `trigger`,
    `history_tokens_before`, `history_tokens_after`, `method` and `degraded`.
    A synchronous attempt that folded nothing sends a non-`compacted` status,
    which only clears the row.
  - Handled in `frontend/pages/reports/[id]/index.vue` (the existing
    `context.compacted` case near line 4480).
- **Divider** (`index.vue` ~line 663): reads
  `Compacted history above · {turns} turns · ~{before} → ~{after} tokens`
  (history tokens folded → summary size). Clicking it opens a popover with the
  summary exactly as the agent sees it (`GET /api/reports/{id}/context/summary`,
  owner-only like the transcript). It shows a warning icon when `degraded`.
- **Composer receipt.** It shows the *whole prompt* before → after, from the
  estimate the usage popover already fetches. It falls back to history tokens
  when the estimate is unavailable.
- **Usage popover.** The `compacted · Nk` counter and the Compact button stay;
  the button runs the same path as `/compact`.
- **Strings.** New strings go under `prompt.*` / `reportView.*` in **all ten**
  `locales/*.json` (identical shape; Hebrew vocabulary rules apply).

## 4. Data model

Migration `ctxcomp02_context_budget_fields` (down_revision = current head) on
`report_context_states`:

| Column | Type | Purpose |
|---|---|---|
| `last_prompt_tokens` | Integer, null | provider-reported anchor |
| `last_prompt_estimate` | Integer, null | our estimate of that same request (offset = tokens − estimate) |
| `failure_count` | Integer, default 0 | cooldown ladder |
| `cooldown_until` | DateTime, null | auto-mode backoff |
| `last_method` | String(16), null | llm / llm_retry / fallback |
| `last_tokens_before` / `last_tokens_after` | Integer, null | divider + popover |
| `degraded` | Boolean, default false | fallback / partial summary |

`CompactionStateSchema` (`backend/app/schemas/completion_v2_schema.py:127`)
gains the user-visible fields. `get_ui_state` computes `can_compact` with the
manual tail rule.

## 5. Change list

**Backend**
- `app/ai/context/context_budget.py` (new): `ContextBudget`, `budget_for`,
  `measure`, `ContextGovernor`.
- `app/ai/agent_v2.py`:
  - call the governor before each planner request (replaces the
    `trim_context_to_budget` call at ~5655)
  - make `_maybe_schedule_compaction` a "prepare-ahead" step based on
    `measure()`
  - make `_handle_context_overflow` re-enter the governor, with a shared
    3-attempt cap
  - persist the anchor in `_record_planner_token_metadata_from_decision`
  - emit `context.compacting`
- `app/ai/context/context_hub.py`: `trim_context_to_budget(pinned_prefix=…)`,
  whole-item trimming for messages, and a larger `messages_max` backstop.
- `app/ai/context/builders/message_context_builder.py`: `decay_level`, and
  expose the pinned prefix separately.
- `app/ai/context/sections/messages_section.py`: render the summary and the
  opening exchange as a separately addressable prefix.
- `app/ai/agents/planner/transcript_bridge.py`: `transcript_budget_tokens` via
  `budget_for`.
- `app/services/context_compaction_service.py`:
  - modes (`auto_bg`, `auto_sync`, `overflow`, `manual`), `instructions`,
    `keep`
  - bounded and chunked input, output cap, retry, fallback, grow guard,
    cooldown
  - new summary keys and telemetry
- `app/routes/completion.py`, `app/schemas/completion_v2_schema.py`: the
  request body and the extra response/state fields.
- `app/models/report_context_state.py` + alembic migration.

**Frontend**
- `components/prompt/PromptBoxV2.vue`: `/compact` parsing, deferral, status
  chip, toast, shared `compactContext`.
- `components/prompt/MentionInput.vue` + `utils/slashCommands.ts` (new): the
  slash menu.
- `pages/reports/[id]/index.vue`: `context.compacting`, the richer divider,
  the summary popover.
- `locales/*.json` (all ten).

## 6. Tests (`backend/tests/AGENTS.md` rules apply)

**Unit (`backend/tests/unit/test_context_compaction.py` + new
`test_context_budget.py`)**
- **Budget model**
  - `budget_for` derives every threshold from the main window, and uses the
    main model's window even when the small model's window differs.
  - `measure` prefers anchor + delta, then the persisted anchor, then the
    estimate.
- **Ladder**
  - below `cheap_at`: no-op
  - between `cheap_at` and `summarize_at`: the cheap step runs and there is
    no LLM call
  - above `summarize_at`: a sync fold runs before the request
  - a last-resort trim never removes the summary or the opening exchange
    (regression for the head-trim bug)
- **Overflow:** retries are capped at 3 across both error paths, then the
  typed error is raised.
- **Summarizer**
  - bounded input with priority fill, and chunked folding that advances the
    watermark per chunk
  - retry on bad JSON, then the deterministic fallback (`degraded=true`)
  - the grow guard refuses to commit
  - cooldown skips auto mode but not manual
- **Manual:** keeps 2 exchanges by default, `keep=N` works, and `instructions`
  reach the prompt escaped.
- **Long session:** about 60 turns with tool output. The prompt stays
  ≤ `usable`, exact ids and user constraints survive in the summary, and the
  first ask stays answerable.

**Frontend**
- **Slash parsing:** `/compact`, `/compact keep 3`, `/compact focus on churn`,
  and `/compactor` (must not match).
- **Deferral:** `/compact` typed while a run is in progress fires after the run
  and before the next queued prompt.

## 7. Verification

- **sandbox-feedback-loop** (`docs/feedback-loops/context-compaction.md`):
  1. Boot the stack with `BOW_CONTEXT_*_RATIO` lowered.
  2. Drive a long report through the UI and assert each rung in the backend
     logs (telemetry line), the DB (`report_context_states`) and SSE.
  3. Inject `BOW_AGENT_LOOP_FAULT_KIND=context` to prove overflow → ladder →
     success.
  4. Type `/compact focus on revenue` mid-run and confirm it is deferred, then
     runs.
- **ui-evidence:** before/after screenshots of the composer slash menu, the
  status chip, the divider with before → after, and the summary popover. Add a
  GIF of `/compact` during a run.
- After merge: **release-notes** and **docs-update** (the `/compact` command
  and what compaction does).

## 8. Risks

- **Latency of a synchronous fold.** Mitigated by prepare-ahead and the 45s
  timeout with a deterministic fallback. Rung 2 should be rare; the telemetry
  `trigger=auto_sync` count measures it.
- **Prompt-cache breaks.** Folding changes the prompt prefix. Thresholds are
  batched (fold to `target`, not just under `summarize_at`), so this happens
  rarely.
- **Lossy cheap step.** Ids always survive and results can be re-fetched. The
  cheap step only changes rendering, never stored data.
- **Migration.** Additive, nullable columns only.
