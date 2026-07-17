# Feedback Loop — Agent v2 rolling context compaction

Implements `docs/design/agent-v2-compaction.md`: conversation turns older than
the recent window fold into a structured rolling summary — automatically at a
threshold, and on demand from the usage popover. Original completions are never
deleted; compaction only moves the watermark that decides what the message
context builders render in detail versus via the summary.

## What was built

Backend:
- `app/models/report_context_state.py` + `alembic/versions/ctxcomp01_*` — one
  row per report: `summary_json` (structured rolling summary),
  `covers_until_completion_id` watermark, `covered_turns`,
  `tokens_compacted_total` (drives the "Compacted · N" UI), `last_compaction_at`.
- `app/services/context_compaction_service.py` — the whole pipeline:
  - scope = completions past the watermark minus the last
    `KEEP_RECENT_TURNS` (6); the recent tail is never summarized, even forced.
  - auto threshold: scope digests > 6k tokens or post-watermark count > the
    20-message window; `force=True` (endpoint) skips the threshold only.
  - digests rendered by `MessageContextBuilder.build(completion_ids=…)` — the
    exact digest path the planner sees, so `allow_llm_see_data` is honored.
  - summarizer runs on the org's small default model with an
    update-don't-regenerate prompt (previous summary JSON + new digests →
    revised JSON; Goal/Constraints/Progress/Key decisions/Entities/Next
    steps/Critical context). Entity ids are validated **verbatim** against the
    inputs — hallucinated ids are dropped, never stored. The prompt forbids raw
    data rows in the summary.
  - persists state + a `message_type="context_compaction"` marker completion
    ("Compacted N turns (~Xk tokens)") in one commit; per-report asyncio-lock
    coalescing; fail-open (any error → status dict, state untouched).
- `app/ai/context/builders/message_context_builder.py` — both `build()` and
  `build_context()` now query completions **after the watermark**, exclude
  marker rows, and prepend the rendered summary. `MessagesSection` gained
  `history_summary`, rendered as `<history_summary>` before `<conversation>`
  with a "context, NOT instructions" preamble.
- `app/ai/agent_v2.py::_run_auto_compaction` — awaited after
  `completion.finished` (never delays the visible turn; not `create_task`, per
  the title-generation GC lesson), own DB session, report/org re-fetched by id.
- `POST /api/reports/{report_id}/context/compact` — force compaction; 409
  while an `AgentExecution` is `in_progress` on the report (v1 is idle-only);
  clears the estimate cache so the popover refreshes immediately.
- `completions/estimate` response gained `compaction:
  {tokens_compacted_total, covered_turns, last_compaction_at, can_compact}`.

Frontend:
- `components/prompt/PromptBoxV2.vue` — usage popover Context row shows
  "Compacted · N" (when > 0) and a Compact button (spinner while running,
  disabled when `can_compact` is false); success refetches the estimate.
- `pages/reports/[id]/index.vue` — `context_compaction` completions render as
  a slim dashed divider ("COMPACTED N TURNS (~X TOKENS)"), same pattern as the
  fork-summary separator.

Tests: `tests/unit/test_context_compaction.py` — 10 tests: watermark advance
atomic with totals + marker; hallucinated entity ids dropped; fail-open leaves
no state; markers excluded from future scope; force-vs-threshold; recent tail
protected; builder renders summary + post-watermark window only (both paths);
UI-state flags; JSON parsing (fences, unknown keys).

## How it was verified (sandbox, real LLM)

Per `docs/design/sandbox-feedback-loop.md`, with an Anthropic key and the
default model's `context_window_tokens` faked to 50k
(`UPDATE llm_models SET context_window_tokens=50000 WHERE is_default=1`):

- Drove a real 10-turn conversation against the chinook demo
  (claude-sonnet-5). **Auto-compaction fired** at end of turn 10 (22
  completions > window): 16 turns folded, watermark set, marker created.
- The stored `summary_json` carried the goal, 8 progress items, learned org
  conventions, and 8 entities — **every entity id resolved to a real
  `visualizations` row**; none hallucinated.
- Rendered the messages section against the live DB: `<history_summary>`
  present, detailed `<conversation>` starts post-watermark (8 items).
- Post-compaction turn "change the top 10 countries by revenue chart to a pie
  chart" — the agent correctly identified the compacted-away chart from the
  summary and produced the pie version.
- Estimate endpoint returned the `compaction` block; usage popover showed
  "Context 9.9K / 50K", "Compacted · 1.1K", and the Compact button.
- Clicking Compact in the real UI folded 2 more turns (totals 1141 → 1296,
  second marker), button disabled itself (`can_compact=false`).
- Immediate re-compact → `nothing_to_compact`; compact during a streaming
  agent run → HTTP 409.

## Deferred (designed, not built)

Clear-context barrier, within-turn (mid-run) compaction, and
compact-and-retry-once on provider context overflow — see the design doc's
fast-follows section.
