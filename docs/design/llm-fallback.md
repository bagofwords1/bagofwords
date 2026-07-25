# LLM Fallback (Enterprise)

Availability-driven model substitution. When the effective model fails with a
rate limit, provider overload, or network error, the agent swaps to the next
model in the org's admin-configured fallback order for the rest of the run —
instead of failing the whole turn. Typical deployment: an on-prem vLLM box
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
   blips; never masks an outage from the layers above.
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
   `rate_limit`/`provider_error` (per-model quotas/capacity: an Opus 429 never
   blocks Haiku on the same provider — same-provider fallback is expected and
   supported), **provider** scope for `network`/`auth` (endpoint-wide).
   In-memory only; approximate under multi-worker deployments by design.

Not eligible for fallback: `auth` (won't heal by switching and must surface to
the admin), `context_length` (follows the conversation), `unknown` (safer to
surface). Mid-stream failures after content reached the SSE wire are not
transparently retried — they surface to the agent-level retry/fallback.

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

- Fallback engages on the planner path (`inference_stream_v2`). Side tasks on
  the small model (titles, judges) get façade retries only; their failures are
  already non-fatal.
- No capability filtering at runtime beyond enabled/breaker checks (vision /
  context-window mismatches are the admin's responsibility when ordering the
  list); the UI is the place to add warnings.
- Breaker state is per-process. A future iteration can poll vLLM's
  `num_requests_waiting` metric for a real queue-depth signal and add
  per-provider concurrency limits (proactive burst instead of error-driven).
