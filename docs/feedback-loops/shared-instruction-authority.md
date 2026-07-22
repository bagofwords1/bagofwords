# Feedback Loop — "Admin2 changes instruction1 (granted via agent2) and breaks agent1"

An instruction can be attached to several agents at once. Instruction1 is shared
by agent1 and agent2. Admin1 owns both; Admin2 owns only agent2. The report:
Admin2, acting with authority over agent2 alone, changes instruction1 and thereby
changes agent1's behavior — an agent Admin2 does not control. This loop validates
the escalation and the fix.

## Root cause (validated)

A shared instruction is a **single mutable row** (`instructions`) linked to N
agents via `instruction_data_source_association`
(`backend/app/models/instruction.py:8`). What is *live* is the row's current
version, flipped org-wide by `BuildService.promote_build`
(`backend/app/services/build_service.py:851`). Write authority is resolved
**per agent** — a per-agent `manage` grant implies `manage_instructions` on that
agent only (`backend/app/core/permission_resolver.py:47`).

The synchronous HTTP API already resolves this conflict safely: every
edit/delete/approve path requires `manage_instructions` on **every** attached
agent via `check_resource_permissions`, which fails on the first miss
(`backend/app/routes/instruction.py:558`,
`backend/app/core/permissions_decorator.py:350`). So Admin2 editing instruction1
directly → 403.

The hole is the **automation plane**. `AgentReliabilityService.run_for_suggestion`
promotes a suggestion build org-wide based on a **single** agent's Self-Learning
policy (`backend/app/services/agent_reliability_service.py:826`):

- The auto-approve path promoted whenever *any* affected agent's policy was
  `auto_approve` — an affected-but-non-consenting agent1 (mode `off`) was simply
  skipped, not treated as a blocker.
- The eval path promoted on green when all *eval-gating* agents were `eval_auto`,
  ignoring other affected agents entirely.

Admin2 controls agent2's policy alone (`PATCH /data_sources/{id}/automation`,
gated only by `manage` on that one agent —
`backend/app/routes/agent_reliability.py:118`). Setting agent2 to `auto_approve`
made any suggestion touching the shared instruction1 go live for agent1 too. The
training loop's `_promote_or_pend`
(`backend/app/services/agent_reliability_service.py:334`) had the same one-agent
gate.

## Loop A — deterministic reproduction (no external services)

`backend/tests/e2e/test_instruction_sharing_authority.py` seeds an org with two
agents, stages a shared suggestion build via the real `InstructionService`
(`auto_finalize=False`, so it is staged not promoted), then calls
`run_for_suggestion` and asserts whether the build reached `is_main`.

```bash
cd backend
export BOW_DATABASE_URL="sqlite:///db/app.db"; mkdir -p db
# Reproduce on unfixed code (revert just the service):
git stash push -- app/services/agent_reliability_service.py
uv run pytest tests/e2e/test_instruction_sharing_authority.py -q -p no:cacheprovider
git stash pop
```

Observed FAIL (escalation present) — agent2=`auto_approve`, agent1=`off`:

```
FAILED ... ::test_shared_instruction_not_autopromoted_when_one_agent_dissents
FAILED ... ::test_global_instruction_not_autopromoted_off_one_agent
2 failed, 2 passed
```

The two control cases (scoped-to-one-agent, and all-agents-consent) passed even
before the fix, confirming the reproduction isolates the escalation, not
over-blocking.

## The fix

`backend/app/services/agent_reliability_service.py`:

- New `_dissenting_agents(db, org, build_id)` — affected agents whose policy is not
  an auto-promoting mode (`auto_approve` / `eval_auto`).
- `run_for_suggestion`: compute `autopromote_allowed = not dissenting`; both the
  eval-green and no-eval auto-approve branches withhold promotion and submit for
  human approval when any affected agent dissents.
- `_promote_or_pend`: same cross-agent gate on the training loop's candidate
  build.

`backend/app/ai/tools/implementations/edit_instruction.py`: in knowledge/
post-analysis mode, refuse to edit an instruction attached to agents outside the
session's data-source scope (defense in depth — stops the tool generating
un-approvable cross-agent suggestions).

Re-run Loop A on fixed code — flips to PASS:

```
tests/e2e/test_instruction_sharing_authority.py .... [100%]
4 passed
```

## What this proves / regression notes

- The escalation is closed at the promotion decision, not just the sync route:
  a shared or global instruction cannot go live org-wide unless every affected
  agent independently consents; otherwise the build waits for someone with
  authority over all affected agents (the ALL-agents publish gate).
- Single-agent and all-consent promotion paths are unchanged.
- Full regression run (green): `test_agent_reliability_loop.py` (8),
  `rbac/test_rbac_instructions.py` (5), `test_instruction*.py` + `test_build.py`
  (115), `test_training_multi_instruction_accept.py` + overfit/evidence (18).

Design writeup: `docs/design/shared-instruction-editing.md`.
