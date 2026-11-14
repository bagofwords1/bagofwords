# Feedback Loop — "if a decision model is available and auto routing is on, routing uses it; otherwise keep today's behavior"

**Status: EXECUTED — Loop A green, Loop B confirmed live (OpenAI + TypeSafe Jev), Loop C captured.**

Adds **decision models** (typed questions in → calibrated probabilities out; no
text) as a separate model kind that can never be used for chat, and uses one as
an optional *upfront* router: before the planner's first turn, one `choice`
question over the routing candidates (described by their routing hints) picks
the starting model. When the admin keeps "Small model" (the default), or the
decision is unconfident / fails / times out / its model is disabled, behavior is
exactly today's: start on the small model, `route_model` may escalate.

Related: `docs/design/auto-model-routing.md`,
`docs/feedback-loops/auto-model-routing.md` (the loop this extends).

---

## Background (researched 2026-11-14; wire shapes confirmed live)

Two wire formats, no single standard:

| | OpenAI "Decisions" | "System One" |
|---|---|---|
| Endpoint | `POST /v1/decisions` | `POST /v1/systemone` |
| Content | `input` | `state` |
| Questions | array, each with `name` | map keyed by name |
| Types | `predicate`, `choice` (`choices[{value,description}]`), `score` (`levels`) | `noul`, `choice` (`criteria{key:desc}`), `score` (`criteria[]`), `box` |
| Answers | array; `probabilities: [{value, probability}]`; `type:"refusal"` | map; `probabilities: {key: p}` |

Confirmed with real calls (both returned the documented shapes):
`api.openai.com/v1/decisions` with `gpt-6-luna` (`usage.output_tokens: 0`) and
`api.typesafe.ai/v1/systemone` with `jev-latest` (resolved to `jev-1.14.0`).
Others speaking System One: Microsoft Foundry Decision-1, Perplexity
pplx-decider, Cloudflare Clef, Ollama ≥0.35, vLLM ≥0.32, LiteLLM ≥1.104.2.
**Bedrock: none** (AWS ships Strands Decider as weights / SageMaker).
**Claude: none.** No decision API has temperature/top_p.

## Mechanism (validated)

- **Kind:** `llm_models.model_kind` (`chat` | `decision`), migration
  `decmodel01`. A column, because every chat-model query filters on it.
- **Catalog:** `gpt-6-luna-decisions` (`backend/app/models/llm_model.py`) with
  `api_model_id: "gpt-6-luna"` — the chat `gpt-6-luna` keeps its row; catalog
  sync adds the decision row to new and existing OpenAI providers.
- **Client:** `backend/app/ai/llm/decisions.py` — internal OpenAI shape,
  `build_request_body` / `answers_from_systemone` translate; `DecisionClient`
  is the HTTP call (egress-pinned for admin base URLs); settings resolve
  catalog defaults + `LLMModel.config` overrides (`decision_settings`).
  `LLM.decide()` (`backend/app/ai/llm/llm.py`) adds credentials, PII redaction,
  spend-cap checks and an awaited usage record that is **never** `routed`.
  `LLM.inference*` refuse decision models (`_ensure_chat_model`).
- **Hidden from chat, server-side:** `GET /llm/models` defaults to `kind=chat`
  (`?kind=decision|all`); guards in `llm_service.py` (set default/small default,
  inherited/user/report defaults, `validate_model_for_user`,
  `set_user_default_model`, fallback order, create paths),
  `completion_service._resolve_completion_models` (explicit pick → 400),
  `sub_agent_service`, `test_suite_service`, `model_router.resolve_routing_candidates`.
- **Settings:** `PUT /llm/models/{id}/decision` (format, path, wire id, max
  questions, question types, timeout, keep_alive, caching; validated),
  `POST /llm/models/{id}/test_decision`; chat-only settings (temperature,
  reasoning, routing hint) return 400 on decision models.
- **Routing setting:** org key `model_routing_decision`
  `{method: small_model|decision, decision_model_id, min_confidence}`;
  validated in `model_router.validate_routing_decision_update`.
- **Upfront routing:** `model_router.decide_starting_model` (never raises) →
  `AgentV2._apply_upfront_routing_decision` (`agent_v2.py`) reuses the
  escalation path (`_apply_routed_model`), writes a `route_model` step with
  `cause:"decision"`, confidence and probabilities.
- **Cost:** usage scope `model_routing.decision` (counts in turn cost; not in
  `_NON_TURN_USAGE_SCOPES`); `RoutingSavings.decision_calls/decision_cost_usd`
  and savings netted (`console_service._compute_routing_savings`).

## Loop A — deterministic (no external services)

Boundary stub: `backend/tests/mocks/fake_decision_server.py` — an httpx
transport serving both `/decisions` and `/systemone`, recording every request
(installed by patching `app.ai.llm.decisions.TRANSPORT_OVERRIDE`).

```bash
cd backend
export BOW_DATABASE_URL="sqlite:///db/app.db" TESTING=true
uv run pytest tests/unit/test_decision_client.py \
  tests/e2e/rbac/test_decision_models.py \
  tests/unit/test_model_router.py tests/e2e/rbac/test_auto_model_routing.py \
  tests/unit/test_runtime_head_and_routing.py --db=sqlite -q
```

**Red first (before the change):** 28 decision tests — unit file fails to
import (`app.ai.llm.decisions` missing); e2e:
`27 × TypeError: 'model_kind' is an invalid keyword argument for LLMModel`,
`AssertionError: OpenAI providers get the decisions catalog entry automatically`.

**Green (after):** `77 passed` (new + existing routing suites).

Coverage: both formats round-trip every question type (parametrized option
counts); refusals; question limits/types rejected before sending; catalog row
with wire id coexisting with chat `gpt-6-luna`; Bedrock rejects decision
models; default list excludes them, `kind=all` includes them; cannot be
default/small default/fallback/user default/report model/explicit completion
model/routing candidate; every decision setting persists and reaches the wire
(stub body); out-of-range settings → 400/422; temperature/routing hint rejected
and never sent; settings need `manage_llm`; method defaults to small_model;
decision method needs an enabled decision model; small_model method makes 0
decision calls; confident pick starts on the chosen model (two targets);
low confidence / small pick keep small; error/timeout/refusal/disabled fall
back; the decision call is unrouted usage on its own scope; decision cost nets
out of savings.

**Regression:** the LLM/settings/console/routing/usage e2e subset —
`462 passed, 5 skipped`. Full `tests/unit`: `202 failed, 16854 passed` — the
**same 202** fail on the untouched base commit (`202 failed, 256 passed` for
those 14 files on both trees: SharePoint, OBO login recovery, connector
dialect/egress tests); none touch LLM code.

## Loop B — live confirmation (real OpenAI + TypeSafe keys)

Keys from env only (never printed). Stack: `tools/agent/boot_stack.sh --dev`,
`seed_org.py --demo`, then:

```bash
cd backend && uv run python ../tools/agent/decision_routing_live.py --decision openai
cd backend && uv run python ../tools/agent/decision_routing_live.py --decision jev
```

Observed:

```
decision models: [('GPT-6 Luna Decisions', 'openai', 'gpt-6-luna'), ('TypeSafe Jev', 'systemone', 'jev-latest')]
chat list: 11 models, none of kind=decision ✓
test_decision  GPT-6 Luna Decisions 200 probability 1.0  latency 1121 ms
test_decision  TypeSafe Jev         200 probability 0.99 latency  394 ms

--decision openai
  simple: completions model gpt-6-luna; route_model steps: none
          usage ('model_routing.decision', 'gpt-6-luna-decisions', routed=0, 173 in, 0 out, $0.000017)
  hard:   completions model gpt-5.6-sol
          route_model[decision]: model=gpt-5.6-sol confidence=0.99
            probs=[gpt-5.6-sol 0.99, gpt-6-luna 0.01] by=GPT-6 Luna Decisions
          planner/create_data.code_gen all on gpt-5.6-sol from the first turn
--decision jev (System One)
  simple: stays on gpt-6-luna; usage ('model_routing.decision', 'jev-latest', routed=0, 375 in, 48 out)
  hard:   route_model[decision]: model=gpt-5.6-sol confidence=1.0 by=TypeSafe Jev
cost console routing: decision_calls 4, decision_cost_usd 0.00007, savings net of it
```

The hard prompt built the full dashboard on GPT-5.6 Sol (judge: instructions
4/5, response 4/5).

## Loop C — UI evidence

`media/pr/decision-model-routing/` (deviceScaleFactor 2; en + he):
LLM table with the Decision badge and router summary; the Auto-router modal on
turn-on (small model preselected) and with Decision selected; decision model
card (Decision settings, live test "99% yes in 348 ms", input-only cost) vs a
chat model card; prompt-box model picker without decision models; chat line
"Routed to GPT-5.6 Sol · 99% match" + tooltip; trace modal timeline with the
`route_model.decision` step; cost page "4 upfront decisions". "Before" shots
are the base commit's frontend against the same backend.

Caught by Loop C: the first render of `/settings/models` crashed with
"Cannot access 'addModelModalOpen' before initialization" (a `watch` placed
above the ref it watches in `LLMsComponent.vue`); fixed before capture.

## What this proves / regression notes

- Decision models can never be selected for, or leak into, chat — enforced on
  the server, so every current and future picker inherits it.
- With method "small_model" or no usable decision model, routing makes zero
  decision calls and behaves exactly as before.
- The decision only changes the starting model; escalation still works, and
  the decision's own cost is visible in turn cost, the trace, the cost page and
  net savings.

Follow-up noticed (separate change): image-generation models appear not to be
filtered from several pickers (`UserProfileModal`, `TriggersTab`,
`ScheduledPromptModal`, `DashboardSettingsModal`, `TestCaseEditor`); the
server-side `kind` filter is the pattern to fix it.

Not covered by an automated test: the agent-side wiring
(`_apply_upfront_routing_decision` writing the step) — verified in Loop B and
the trace modal; the routing decision itself is covered by Loop A.
