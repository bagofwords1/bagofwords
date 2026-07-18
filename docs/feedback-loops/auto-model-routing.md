# Feedback Loop — "automatically route to a cheap or expensive model"

Validates the Auto model router: when a user picks no model and the org has
routing on, a run STARTS on the small model, the agent may escalate to a
stronger model via the `route_model` tool, the choice PROPAGATES to code
generation and other tools, and realized savings surface on the cost console.

Design: `docs/design/auto-model-routing.md`.

## Root cause / mechanism (validated)

- Model resolution for a completion happens in
  `CompletionService._resolve_completion_models` (`backend/app/services/completion_service.py`).
  Routing engages only when nothing is picked (no `prompt.model_id`, no
  `report.model_id`), `model_routing` org setting is on, a small model exists
  and differs from the default, and ≥1 *guided* candidate exists.
- Escalation: `route_model` tool → `RoutingController.apply` →
  `AgentV2._apply_routed_model` swaps `self.model` and rebuilds the planner LLM
  (`backend/app/ai/model_router.py`, `backend/app/ai/agent_v2.py`).
- Propagation: `runtime_ctx` is rebuilt from the live `self.model` on every
  tool dispatch (`agent_v2.py:~3529`), so codegen/viz/artifacts follow the
  routed model automatically. `small_model` is now also passed in so
  viz-inference always runs small.
- Savings: every LLM call during a routed run is stamped `routed=True` +
  `baseline_model_id` (usage attribution → `llm_usage_records`); the console
  computes baseline-priced tokens − actual cost.

## Loop A — deterministic (no external services)

```bash
cd backend
export BOW_DATABASE_URL="sqlite:///db/app.db" TESTING=true
uv run pytest tests/unit/test_model_router.py \
  tests/e2e/rbac/test_auto_model_routing.py -m "e2e or not e2e" --db=sqlite -q
```

Observed: **18 passed**. Covers:
- schema enum built only from guided candidates; controller swaps the agent
  model on valid escalation and rejects unknown models (propagation mechanism);
- savings math (baseline − actual, nets escalation overhead);
- the decision: nothing-picked + routing on → starts small with baseline
  stamped; explicit pick / report-pinned / routing off / no-candidate → default;
- routing-hint endpoint (manage_llm) and the savings KPI on the usage metrics.

To prove the test can fail, stash the resolver change and re-run
`test_nothing_picked_and_routing_on_starts_on_small_with_baseline` — it fails
because the run starts on the default model.

## Loop B — live confirmation (real OpenAI key)

Boot the stack, seed the demo data source, register a real OpenAI provider
(the seeded catalog IDs are fictional), set gpt-4o-mini as small default and
gpt-4o as default, add routing hints to both, and turn routing on. Then run
two prompts through `POST /api/reports/{id}/completions` (non-stream) and read
`llm_usage_records`.

Observed (13 LLM calls across both prompts):

```
scope                     model        routed  baseline
planner                   gpt-4o-mini    1      <gpt-4o id>
create_data.code_gen      gpt-4o-mini    1      <gpt-4o id>
create_data.viz_infer     gpt-4o-mini    1      <gpt-4o id>
create_artifact           gpt-4o-mini    1      <gpt-4o id>
judge.instructions_context gpt-4o-mini   1      <gpt-4o id>
report.follow_ups         gpt-4o-mini    1      <gpt-4o id>
```

- **Propagation proven**: planner, codegen, viz-infer, and artifact generation
  all ran on the small model with `routed=1` and the gpt-4o baseline stamped.
- Cost console shows **"Saved by auto-routing: $0.43 · 13 routed calls (100%)"**
  (`media/pr/auto-model-routing/06-dashboard-savings.png`).

Known limitation observed live: gpt-4o-mini did NOT call `route_model` to
escalate the dashboard prompt — small models under-escalate (the documented
risk). The escalation *mechanism* is proven by Loop A's controller tests; live
escalation reliability is a prompt/model-calibration matter, and judge scores
on small-resolved runs are the audit for it.

## UI evidence

`media/pr/auto-model-routing/`: settings with routing on (Routing column +
guidance) / off (column hidden), Actions dropdown (freeze fixed), and the
dashboard savings KPI.

## What this proves

The router starts cheap, propagates a routed model across the whole run,
respects explicit/pinned picks, and reports honest net savings — all covered
by tests that can fail, plus a live end-to-end run against real OpenAI.
