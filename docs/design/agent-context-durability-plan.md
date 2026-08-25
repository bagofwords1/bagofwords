# Durable agent context across completions — implementation plan

**Status:** implemented and locally verified (2026-08-24).

The implementation refined this plan after the failing loop and query-path
review. It uses an ordered projection over the existing Completion,
PlanDecision/CompletionBlock, and ToolExecution records rather than a second
append-only transcript table. Only durable provider call identity and
decision-local action order were missing from the canonical rows, so migration
`durctx01` adds those nullable fields without a historical backfill. The main
planner persists call intents inside its existing PlanDecision commit and
rehydration reuses MessageContextBuilder's existing batched tool query; this
avoids a new write, read, or model-summary call in the hot path.

Completed verification and exact replay steps are recorded in
`docs/feedback-loops/durable-agent-context.md`. The work-package text below is
retained as the design history and acceptance rationale; where it proposes a
new part table or rollout flag, the lower-overhead canonical-row projection
above is the implemented decision.

This plan closes the gap between the typed transcript available during one
agent run and the lossy history rebuilt for a later completion. It extends the
existing native transcript design in `docs/design/agent-tool-results.md`; it
does not replace the raw execution, query, file, artifact, or step records.

## Outcome

A report/task owns one ordered, typed, durable transcript across completion
boundaries. A new completion rehydrates that transcript instead of beginning
with empty transcript and observation state. Tool intent is committed before
dispatch, and every settled, failed, denied, or interrupted call has a paired
result.

`context_summary_json` remains the bounded model-facing projection for large
tool results. It is payload inside a typed result part, not the structure of
conversation history.

## Validated current boundaries

- `backend/app/ai/agent_v2.py:610` creates a fresh `Transcript()` for each
  `AgentV2` instance.
- `backend/app/ai/context/context_hub.py:357` creates a fresh
  `ObservationContextBuilder()`.
- `backend/app/ai/context/builders/message_context_builder.py:951-974` loads
  `ToolExecution.context_summary_json` and renders it back into message
  history, rather than restoring typed calls and results.
- `backend/app/ai/persisted_summary.py:27-38` bounds and supports summaries for
  only selected high-volume tools.
- `backend/app/project_manager.py:1034-1058` creates an in-memory execution
  stub and performs no database write until the tool finishes.
- `backend/app/ai/context/sections/messages_section.py:15-16,43-59,66-90`
  keeps seven recent message items in full and minifies older items to short
  prose snippets.

Consequently, completed tool data may survive in storage while its ordering,
roles, call/result pairing, and exact recent model-visible context do not. A
process death between tool dispatch and the finish write can lose the call
record itself.

## Required invariants

1. The report/task, not a completion, is the transcript lifetime.
2. Transcript ordering is durable, deterministic, and idempotent.
3. A tool-call record commits before its side effect can begin.
4. Every call has exactly one terminal result: `success`, `failed`, `denied`,
   `interrupted`, or `outcome_unknown`.
5. Recovery never silently retries an `outcome_unknown` side effect.
6. Tool calls and results remain paired in model context and at compaction
   boundaries.
7. User instructions remain verbatim; derived assistant/tool material may
   decay or be summarized.
8. Raw result data is not duplicated into the transcript when an existing
   durable record can be referenced.
9. Model-visible persisted content follows the existing visibility, PII,
   retention, and organization-isolation rules.
10. The existing path remains available during rollout and for legacy reports.

## Work packages

### 0. Build the failing feedback loop first

Follow `.agents/skills/sandbox-feedback-loop/SKILL.md` before implementation.

Create a focused regression module under `backend/tests/` after reading
`backend/tests/AGENTS.md`. It must fail on the current code for the observed
reason and cover these general invariants:

- A fresh follow-up completion restores a recent typed tool call and result,
  not only its textual digest.
- A tool-call envelope is queryable before the dispatch callback begins.
- Recovery converts an unmatched call into `outcome_unknown` and does not
  redispatch it.
- Compaction never separates a call/result pair or paraphrases a user
  instruction.
- Parallel calls, retries, and duplicate delivery preserve stable ordering and
  idempotency.

Record the observed pre-fix failures and final passes in
`docs/feedback-loops/agent-context-across-completions.md`.

The live loop is secondary evidence. With the already-running stack at
`localhost:3000`, authenticate through environment-provided credentials and:

1. Run a report task that reads a seeded unique value and uses it in subsequent
   tool work.
2. Wait for the completion to finish, then ask a follow-up whose correct
   answer requires the prior typed result rather than the final prose answer.
3. Stop a controlled run after dispatch and verify the next completion sees an
   interrupted/unknown call and does not repeat it automatically.
4. Inspect persisted transcript parts and the final planner input to prove the
   same context reached the model.

Never place credentials in the feedback-loop document, command arguments,
screenshots, or captured request logs. The deterministic loop must not depend
on the live stack or an external model.

### 1. Freeze the durable transcript contract

Write an architecture decision before the migration. The proposed part shape
is:

- Identity: `id`, `report_id`, `completion_id`, `agent_execution_id`, ordered
  `sequence`, timestamps.
- Type: `user`, `assistant`, `tool_call`, `tool_result`, `compaction`, or
  `status`.
- Call linkage: stable `call_id`, `tool_name`, arguments, provider identity,
  and provider-opaque signature where applicable.
- Result: terminal outcome, bounded model content, digest, token estimate, and
  references to `ToolExecution`, query, step, file, artifact, or note rows.
- Lineage: optional parent/compaction boundary plus a flag for reconstructed
  legacy entries.

Decide whether this is a small append-only table or an append-only projection
over an existing event store. Do not make `ToolExecution` alone the transcript:
it cannot represent user/assistant ordering, compaction entries, or calls that
never reach completion.

Define database constraints for sequence allocation, one terminal result per
call, idempotent retries, concurrent tool batches, and organization-scoped
reads. Include retention and deletion behavior in the decision.

**Gate:** schema review demonstrates that a provider-valid recent transcript
can be rebuilt without parsing prose.

### 2. Add incremental persistence boundaries

Change the write lifecycle in this order:

1. Persist the user turn when the completion accepts it.
2. Persist assistant text/reasoning metadata and the complete tool-call batch.
3. Commit the call batch before dispatching any tool in it.
4. Append each result/error immediately when the tool settles.
5. Append the final assistant message and completion status.

The pre-dispatch commit is the safety boundary. A database failure there must
prevent dispatch; otherwise a side effect could occur without durable intent.
Result persistence must be idempotent so retrying a database write cannot
create duplicate transcript parts.

**Gate:** Loop A proves the call is visible from a separate database session
before the mocked side effect runs.

### 3. Reuse existing result storage without duplicating data

At tool completion:

- Keep the full auditable payload in its current canonical storage.
- Ensure `context_summary_json` is produced for every tool whose full result is
  too large, sensitive, or unsuitable for direct replay.
- Store a reference plus the exact bounded model-facing representation in the
  typed result part.
- Preserve structured identifiers and result shape as fields; do not scrape
  them back from digest prose.
- Use the exact small result when safe and within budget; otherwise use
  `context_summary_json`, then a shorter digest/reference at later decay tiers.

**Gate:** prompt reconstruction performs no large-result database parse and
contains the identifiers required to re-read the canonical result.

### 4. Rehydrate across completions

Load the active transcript before the first planner call of every new
completion. Populate the existing native `Transcript` with the persisted recent
parts and derive observation compatibility data only where older code still
requires it.

The first model request should contain:

- Stable system and report context.
- Any persisted compaction summary.
- The exact protected recent transcript tail.
- The new user message and current volatile context.

Avoid rendering the same execution both as a typed result and as a
`MessageContextBuilder` tool digest.

**Gate:** capture the planner input for two separate completions and show that
the second contains the first completion's typed call/result pair.

### 5. Recover stops, crashes, and orphan calls

On explicit stop and on session rehydration:

- Find calls without terminal results.
- Reconcile known cancellation/error state when available.
- Otherwise append a synthetic `outcome_unknown` result that states the tool
  may have performed its side effect.
- Require an explicit planner/user decision before any repeat of a potentially
  unsafe call.
- Keep the reconstructed pair provider-valid without inventing a successful
  result.

SIGKILL testing belongs in an isolated worker subprocess with a controlled
fake side effect, not by killing the developer's running stack.

**Gate:** killing the subprocess at each write boundary leaves either no
dispatch or a durable call with a terminal recovery result; it never leaves an
invisible side effect.

### 6. Move compaction to typed transcript boundaries

Apply compaction in this order:

1. Keep the session head and user instructions verbatim.
2. Keep a token-budgeted recent tail in full.
3. Keep call/result pairs atomic.
4. Decay old heavy result content to `context_summary_json`.
5. Decay further to digest plus durable references.
6. Summarize old assistant/tool exchanges only when deterministic decay is
   insufficient.

Do not run an LLM summary after every turn by default; threshold-based batches
preserve prompt-cache prefixes and avoid unnecessary latency. Summary failure
must leave the prior active transcript authoritative.

**Gate:** long-session tests prove user constraints and exact identifiers
survive while prompt size stays below the configured budget.

### 7. Make history builders consumers, not authorities

Refactor `MessageContextBuilder` to render/index the durable transcript for
new reports. Keep its current completion/tool-execution reconstruction only as
a legacy fallback.

Remove duplicate observation/digest rendering incrementally after the flagged
path proves parity. Reusable query/file/artifact indexes remain separate
context sections, but should reference the same durable result identities.

**Gate:** one canonical persisted part produces every model-visible history
representation; no structured data makes a string-to-regex round trip.

### 8. Support legacy reports conservatively

For reports without typed parts:

- Continue the current builder path initially.
- Optionally reconstruct ordered user/assistant/completed-tool entries from
  messages, completions, plan decisions, and tool executions.
- Mark backfilled entries as reconstructed and never claim exact ordering or
  content where it cannot be established.
- Do not synthesize historical in-flight calls that were never persisted.

Prefer lazy backfill on first use or a resumable bounded job over a blocking
all-history migration.

**Gate:** old reports remain usable before, during, and after rollout.

### 9. Roll out behind a flag

Run the old and new context builders in shadow mode before sending the new
path to a model. Record structural comparisons without storing raw sensitive
prompt content.

Track:

- Typed call/result pairing and orphan counts.
- Transcript write/recovery failures.
- Context tokens, uncached input tokens, cache-hit ratio, and prompt-build
  latency.
- Repeated identical tool calls after a completion boundary.
- Follow-up task/eval success.
- Legacy fallback and backfill rates.

Rollout order: tests only, local live loop, opt-in development organizations,
small production cohort, then default-on. Retain the old path until recovery,
provider parity, and context-cost gates pass.

## End-to-end acceptance criteria

- A follow-up completion can use an exact recent tool result without rereading
  the source or depending on the previous final answer.
- A process death after tool dispatch cannot erase evidence that the call
  began.
- No unsafe `outcome_unknown` call is automatically replayed.
- Every provider receives valid ordered call/result pairs after resume and
  compaction.
- User constraints remain verbatim throughout a long report session.
- Large results remain bounded and referenceable without duplicating raw data.
- Existing report UI and audit history remain unchanged.
- The deterministic feedback loop fails before the fix and passes afterward;
  the localhost loop confirms the same behavior through the real application.

## Delivery sequence

1. Work package 0: failing deterministic and live reproductions.
2. Work packages 1-2: contract, migration, and pre/post-dispatch writes.
3. Work packages 3-5: result projection, rehydration, and recovery.
4. Work packages 6-7: compaction and builder consolidation.
5. Work packages 8-9: legacy support, shadow comparison, and rollout.

Do not begin a later group until the preceding group's gate passes.
