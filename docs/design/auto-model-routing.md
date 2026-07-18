# Auto model routing

Route each request to the cheapest model that can handle it, without the user
ever having to think about models. Off by default; enabled per organization.

## Principles

- **Explicit user choice always wins.** The router only acts when the user
  picked nothing — no `prompt.model_id`, no `report.model_id`. Router
  decisions are per-run, never persisted into any default.
- **Route down on evidence, up on need.** Start small only where failure is
  detectable or recoverable; escalation is always available and one-way.
- **Extremely simple for the user.** The model picker gains no new options.
  "Default (let the system pick)" simply becomes smarter; the answer shows
  which model actually ran.
- **Admin-guided, not config-sprawl.** Three labels and a free-text hint per
  model — no task×model matrix.

## How routing decides (small-first + escalation tool)

When the org toggle is on and the precedence ladder
(`prompt.model_id` > `report.model_id` > user default > org default) falls
through to a default:

1. **Deterministic pre-checks** in `CompletionService` (no LLM): thinking
   triggers (`THINKING_TRIGGERS` / explicit `reasoning_effort`) → start on the
   resolved default ("strong"); estimated prompt tokens exceed the small
   model's `context_window_tokens` → strong; previous turn in this report
   escalated and this prompt continues it → strong.
2. Otherwise the **planner starts on the small-labeled model**.
   `prompt_builder_v3` instructs it: *before doing anything user-visible*,
   call `route_model(...)` if the task needs a stronger model.
3. **`route_model` tool** — deterministic, no LLM inside. Its input schema is
   built per request: an enum of the org's eligible models (enabled, passes
   `permission_resolver.user_can_use_model`, context fits), each described by
   the admin's routing hint. The server validates the choice, swaps the model
   for all remaining planner turns (one-way, sticky — a mid-run switch
   discards the provider prompt cache, so never oscillate), and stamps the
   final model on the completion.

The "difficulty classifier" is the small planner itself reading the admin's
hints — merged into the first planner turn, so easy requests (most BI
traffic) pay zero routing overhead. The known risk is under-escalation
(small models don't know what they don't know); judge scores on every
completion are the audit for it (see Measurement).

The "strong" candidate is whatever the ladder resolved — so a user's
personal default stays meaningful: their easy questions run small, their
hard ones run *their* chosen model.

### Error-driven fallback (independent of the toggle)

Hooked on `app/ai/llm/errors.py` classification: `context_length` → retry one
tier up; `rate_limit` → retry on the cheapest same-tier sibling.

## Sub-task model assignment

Rule: *is the sub-call the deliverable, or plumbing?*

| Call | Model | Why |
|---|---|---|
| Planner loop | routed model | the routing decision itself |
| `create_data` codegen | follows planner, unless admin pinned a **Coding** model | code correctness = answer correctness; the executor is an objective verifier — a failed cheap attempt escalates its retry |
| Artifact create/edit | follows planner / Coding model | deliverable, but **no server-side verifier** — hence opt-in Coding pin, never a silent cheap default |
| Viz-inference, title, judge, follow-ups | always small | bounded classification with deterministic guardrails downstream |

Today `runtime_ctx` carries only `"model": self.model` (agent_v2 ~:1197,
~:3529), so tool-internal calls — including viz-inference at
`create_data.py:845` — run on the main model. Passing `small_model` through
`runtime_ctx` and switching viz-inference to it is a standalone, risk-free
saving that ships first.

## Admin surface (LLM settings page)

- **"Auto router" toggle** at the top of the LLM tab (manage-LLM permission
  only). Value stored in `organization_settings` as a `FeatureConfig`
  (`model_routing: off | auto`), surfaced here rather than in AI Settings —
  the admin needs the model list, costs, and hints in view when flipping it.
- **Table rework**: `Model | Routing | Cost | Context | Access | Status | ⋮`.
  Provider column dropped (icon suffices), Vision demoted to an icon, Cost
  ($/M in/out — already on `LLMModel`) added.
- **Routing column**: chips `Default` / `Small` / `Coding` (optional) — the
  existing `is_default` / `is_small_default` flags plus one new coding flag —
  and a free-text **routing hint** per model (stored in `LLMModel.config`),
  e.g. "use for simple lookups and follow-ups". Hints feed the `route_model`
  enum descriptions verbatim. Hidden/collapsed when the toggle is off.
  Non-routable models (disabled / access-restricted) show a muted state so
  admins don't write hints the router can never use.

## User surface (PromptBoxV2)

- Picker unchanged. First option remains "Default (let the system pick)";
  with routing on it gains a one-word signal ("Default · Auto" or a ⚡ with
  tooltip).
- The completion displays the model that actually answered. Dislike it →
  pick a model; the pick persists on the report (`reports.model_id`) and the
  router never touches that report again.
- The router **never writes** to `reports.model_id`, user, or org defaults —
  those are user/admin-owned. Cross-turn stickiness comes from the
  pre-checks reading the previous turn's outcome, not stored state.

## Measurement & cost-savings attribution

Captured at decision time (unknowable retroactively): on the completion,
`routed: bool` and `baseline_model_id` — the model the ladder would have used
with routing off. Savings per completion:

```
saved ≈ (baseline model rates × tokens actually used) − actual cost
```

using per-call tokens/cost already in `LLMUsageRecord` and rates on
`LLMModel`. Escalated runs naturally report routing *overhead*, so console
numbers are net. Everything else needed for evaluation already exists:
`Completion.model` (final effective model — updated at run end once
escalation exists, since it is currently stamped at creation), judge scores
(`response_score`, `judge_json`), human feedback, `sigkill`. "Did
small-routed requests score fine, and what did we save" is a SQL query, not
a new subsystem.

## Build order

1. **Plumbing** — `small_model` into both `runtime_ctx` dicts; viz-inference
   → small. Shippable alone.
2. **Org toggle + eligibility** — `model_routing` FeatureConfig; routing hint
   field; eligible-model resolution (labels + access control + context).
3. **The router** — pre-checks in `CompletionService`; `route_model` tool +
   dynamic enum + prompt-builder instruction; model swap for remaining
   turns; completion stamping + `routed`/`baseline_model_id`.
4. **Settings UI** — toggle, Routing column, Cost column, hint editing;
   PromptBoxV2 "· Auto" signal + answer badge; console savings query.
5. **Coding label** — third chip; codegen/artifacts honor it.

## Explicitly deferred (menu, not plan)

Add only if judge/savings data justifies: history-based routing (kNN over
past prompts + `TableStats` difficulty priors), an outside difficulty
classifier in front of the planner, cheapest-in-tier selection across all
org models, exploration/bandit de-confounding, learned routers
(RouteLLM-style). The `routed`/`baseline` logging and hints field are the
only groundwork they need, and all are additive.

## Non-goals

- No external routing services/proxies — they can't see org model catalogs,
  EE access control, judge scores, or table history.
- No cascade-everything (double cost + visible latency in streaming UX);
  cascading only where execution objectively verifies (codegen retries).
- No per-task model matrix beyond the three labels.
- No router writes to any default, ever.
