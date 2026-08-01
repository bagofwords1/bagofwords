# LLM Fallback (Enterprise)

Availability-driven model substitution. When the effective model fails with a
rate limit, an exhausted quota or credit balance, a provider overload, or a
network error, the agent swaps to the next model in the org's admin-configured
fallback order for the rest of the run — instead of failing the whole turn. Typical deployment: an on-prem vLLM box
(e.g. a DGX serving Qwen) as the default model, bursting to a cloud provider
(Anthropic / Bedrock / OpenAI) when the box is saturated or unreachable; or a
cloud primary falling back to the same model on another provider during a
capacity incident.

Orthogonal to the Auto model router (`docs/design/auto-model-routing.md`):
routing decides the *desired* model (quality/cost, planner-invoked via the
`route_model` tool); fallback decides who *actually serves* when the desired
one can't (availability, harness-invoked on classified errors). They share the
apply/attribution plumbing (`AgentV2._apply_effective_model`) and the circuit
breaker feeds both: it triggers fallback skips and filters degraded providers
out of the router's candidate set.

## Layers

1. **Façade retry** (community, `app/ai/llm/llm.py`) — bounded, jittered
   backoff on transient classified errors (`rate_limit` / `network` /
   `provider_error`): 2 retries on sync `inference()`, 1 retry on
   `inference_stream_v2` and only before the first streamed event. Smooths
   blips; never masks an outage from the layers above. `quota` is deliberately
   excluded — see below.
2. **Fallback chain** (EE feature `llm_fallback`, `app/ai/llm/fallback.py`) —
   an ordered list of models tried top-to-bottom. Engaged from the agent's
   planner-retry path (`agent_v2.py`, `stream_error` handling): on an eligible
   error the agent swaps the effective model via `_apply_effective_model
   (cause="fallback")`, emits an `llm.fallback` SSE event (rendered as an
   inline notice in the chat — the substitution is never silent), resets the
   retry budget, and re-runs the planner turn. Sticky for the rest of the run;
   the completion's model badge and usage records reflect the serving model.
3. **Circuit breaker** (`app/ai/llm/fallback.py`, in-process singleton) —
   failure window + cooldown, keyed at two scopes: **model** scope for
   `rate_limit`/`provider_error` (per-model limits/capacity: an Opus 429 never
   blocks Haiku on the same provider — same-provider fallback is expected and
   supported), **provider** scope for `network`/`auth`/`quota` (endpoint- or
   account-wide). In-memory only; approximate under multi-worker deployments
   by design.
4. **Loop-level rescue** (community retry + EE fallback escalation,
   `agent_v2.py` `main_execution`) — every agent loop iteration runs inside a
   try/except. An unexpected exception (a DB hiccup, a crashed orchestration
   step — anything, not just LLM errors) no longer kills the run: the agent
   rolls the session back only if it is actually poisoned (an unnecessary
   rollback expires every ORM instance and turns the retry into a
   MissingGreenlet cascade), cancels the crashed iteration's skeleton block,
   and retries from the latest persisted context — prior blocks and tool
   executions are intact, so the next planner turn resumes mid-flight rather
   than from scratch. Budget comes from the org setting `agent_loop_retries`
   (default 2, clamped 0–10). Each retry emits a `planner.retry` SSE with
   reason `loop_error`, rendered as an inline amber notice in the chat. When
   the budget is exhausted and a fallback chain is bound, the agent walks it
   with `next_candidate(code, force=True)` — forced, because a loop error may
   classify as `unknown`, and at that point a model switch is preferable to
   killing the run; ineligible codes never trip the breaker. The switch is
   disclosed exactly like the planner-path fallback (shared helper
   `_persist_fallback_switch`: `route_model` block + `llm.fallback` SSE) and
   the retry budget resets on the new model. Only when the chain is exhausted
   (or absent) does the error surface as before. `UsageLimitExceeded` (org
   budget) and user stops are never rescued.

Not eligible for fallback: `auth` (won't heal by switching and must surface to
the admin), `context_length` (follows the conversation), `unknown` (safer to
surface). Mid-stream failures after content reached the SSE wire are not
transparently retried — they surface to the agent-level retry/fallback.

**Context overflow remediation.** A `context_length` retry is deterministic —
same inputs, same underestimate in `trim_context_to_budget` (the fast
estimator runs ~4 chars/token and undercounts JSON/code), same oversized
prompt. So both error paths call `_handle_context_overflow` before retrying:
it shrinks the run's trim-budget factor (exactly, when the provider message
carries the actual/limit numbers — Anthropic's `prompt is too long: N tokens
> M maximum`, ratio × 0.95; geometrically ×0.85 otherwise; always progress,
floor 0.2) and forces one synchronous compaction pass (`compact(force=True)`
bypasses the threshold — the provider already proved the transcript is too
big). The retried turn then provably sends fewer tokens. When the loop-level
rescue escalates a `context_length` error to the chain, the walk passes
`min_context_window` so only candidates with a strictly larger known window
are considered (a same-size model rejects the same conversation); unknown
windows fail open, and window-skips don't mark a candidate attempted — a
later non-overflow failure may still use it.

Overflow classification is covered per provider wording: Anthropic
"prompt is too long" (also passed through by Bedrock Claude), Bedrock's own
"Input is too long" (non-Claude / legacy models), Gemini's "input token
count … exceeds", plus the pre-existing OpenAI-style markers — without them,
real overflows classified as `provider_error` (façade-retried and
fallback-eligible, both wrong for a deterministic 400). The exact shrink
ratio parses all three number formats (operand order differs: Anthropic and
Gemini state actual-then-limit, OpenAI limit-then-actual). Two structural
fixes back this up: stringified botocore `ValidationException` maps to
status 400 (`str()` on a ClientError loses `ResponseMetadata`, and the
`context_length` branch requires a 400), and the provider-message extractor
matches quote pairs instead of `[^'"]+` (repr() double-quotes
apostrophe-containing text — "This model's maximum context length…" used to
truncate to "This model" and never hit a marker). Deepest of all:
`planner_v3` classifies the TYPED exception at catch time and carries the
payload in `PlannerError.details.llm_error`; the agent prefers that over
re-classifying `str(exc)`, so providers whose stringified errors carry no
parsable status (Bedrock) still classify correctly on the planner path.

## Quota exhaustion vs rate limiting

Both classes usually arrive as a 429, but they behave nothing alike: a rate
limit heals in seconds, an exhausted allowance or empty credit balance does not
heal within the run at all. `app/ai/llm/errors.py` separates them into
`rate_limit` and `quota` by matching the provider's message — no provider
offers a reliable machine-readable marker, and Google in particular reuses
`RESOURCE_EXHAUSTED` and the word "quota" for plain per-minute throttling
(markers like "per minute" / "rate limit" therefore win over the billing ones).

`quota` covers OpenAI `insufficient_quota`, Anthropic "credit balance is too
low", Google `RESOURCE_EXHAUSTED` / `FAILED_PRECONDITION` billing errors,
Azure "exceeded your assigned quota", Bedrock `ServiceQuotaExceededException`,
and the 402 "insufficient credits" that OpenRouter/DeepSeek-style endpoints
return. It behaves differently from the other eligible codes in three ways:

- **Never retried** by the façade (`_RETRYABLE_CODES`) — backoff can't fix it.
- **Provider-scope breaker**, because allowance is billed per account: an
  exhausted OpenAI key fails every OpenAI model, so the chain skips the
  siblings instead of collecting the same error three times.
- **Trips on the first failure** with a much longer cooldown
  (`sticky_cooldown_s`, 15 min default) rather than the usual threshold. A
  later transient failure on the same key can never shorten that window.

**Our own usage caps are not this.** `UsageLimitExceeded`
(`usage_policy_service`) also says "quota exceeded", but there the provider is
healthy and the *org* is out of budget — substituting a model would only spend
the budget elsewhere. Those messages are explicitly excluded from the `quota`
markers and stay `unknown`: surfaced verbatim, never retried, never eligible
for substitution.

**Provider status extraction.** Google and Bedrock used to fall through to
`unknown` for *every* failure — neither retried nor eligible for fallback —
because `_extract_status` only understood `status_code`/`status` ints and an
`Error code: NNN` string. It now also reads google-genai's int `.code` (its
`.status` is a string enum), unwraps botocore's
`ClientError.response["ResponseMetadata"]["HTTPStatusCode"]`, parses the
stringified `429 RESOURCE_EXHAUSTED` form, and name-matches the AWS exception
names (`ThrottlingException`, `ServiceQuotaExceededException`,
`ModelNotReadyException`, …) that carry no status at all. This matters beyond
quota: it is what makes Bedrock and Vertex throttling and overloads visible to
the retry and fallback layers in the first place.

**Per-user access control.** The chain is org-wide admin config, but access is
per-user: at run time `resolve_fallback_chain` filters the chain through
`user_can_use_model` (EE `llm_access_control`) for the run's requesting user,
so a fallback never serves a model the user was never granted — same principle
as routing candidates. Error posture differs from routing because nothing
re-validates a fallback on apply: if the access check itself fails,
unrestricted models stay (failing open there cannot widen access) and
restricted models are dropped. Runs with no requesting user (system/evals)
keep the full chain.

## Configuration

- **License**: `llm_fallback` in the enterprise tier (`app/ee/license.py`).
- **Toggle**: org setting `llm_fallback` (`FeatureConfig`, off by default;
  enabling without a license is rejected 402, disabling always allowed).
- **Order**: org setting `llm_fallback_order` — a bare list of `LLMModel` db
  ids, capped at 10, managed via `GET/POST /llm/fallback_order`
  (`@require_enterprise(feature="llm_fallback")` + `manage_llm` on write; read
  is permission-gated only so the UI can show locked state in community mode).
  Entries are re-validated at run time — models disabled after the list was
  saved are skipped silently.
- **UI**: `/settings/models` — "LLM fallback" panel (toggle + ordered list
  with move/remove/add; Enterprise lock badge without a license). The chat
  renders the `llm.fallback` SSE event as an amber inline notice
  ("Switched to X — Y was unavailable.") and updates the answer's model badge.

## Known limitations (v1)

- Fallback engages on the planner path (`inference_stream_v2`) and, since the
  loop-level rescue, on any error that crashes an agent loop iteration. Side
  tasks on the small model (titles, judges) get façade retries only; their
  failures are already non-fatal. Tool-internal LLM calls (create_data
  codegen) surface as error observations the planner adapts to; they do not
  walk the chain themselves.
- No capability filtering at runtime beyond enabled/breaker checks (vision /
  context-window mismatches are the admin's responsibility when ordering the
  list); the UI is the place to add warnings.
- Breaker state is per-process. A future iteration can poll vLLM's
  `num_requests_waiting` metric for a real queue-depth signal and add
  per-provider concurrency limits (proactive burst instead of error-driven).
