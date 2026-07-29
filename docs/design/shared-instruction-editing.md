# Shared instruction editing — authority model

## Problem

An instruction is a single mutable row attached to N agents (data sources) via
`instruction_data_source_association`. Write authority is resolved **per agent**.
That mismatch lets a manager of one agent change behavior for agents they don't
control.

Scenario: instruction1 is attached to agent1 and agent2. Admin1 owns both;
Admin2 owns only agent2. Admin2 changes instruction1 (authority granted via
agent2) and breaks agent1.

The synchronous HTTP API already blocks this — every edit/delete/approve path
checks `manage_instructions` on **every** attached agent. The hole is the
automation plane, which promotes builds org-wide based on a **single** agent's
policy.

## Rule

Editing a shared instruction requires authority over **all** attached agents:
org-level `manage_instructions` / `full_admin_access`, or per-agent `manage` on
every attached agent. Global instructions (no attached agents) stay org-admin
only.

Everyone else who reaches the instruction via some attached agent gets it
**read-only** — "used by your agent, managed elsewhere."

Under this rule the scenario is impossible: Admin2 can see instruction1 but not
edit it.

## Fixes (all in the automation plane)

The sync API already enforces the rule. These paths bypass it:

1. **`AgentReliabilityService.run_for_suggestion` / `_promote_or_pend`** — do not
   auto-promote a build touching shared or global instructions off one agent's
   policy. Only auto-promote when the build is scoped entirely to the opting
   agent; otherwise leave it in the Review queue.
2. **`BuildService.promote_build` / `approve_build`** — add the all-agents
   authority check inside the service so no call site (self-learning, training,
   git sync, metadata sync) can skip it.
3. **AI `edit_instruction` tool** — scope its instruction lookup to the current
   session's agents, so it can't stage edits to instructions nobody in that flow
   can approve.

### Status

Implemented on `claude/instruction-sharing-design-issue-v3q1ql`:

- Fixes 1–3 above: `run_for_suggestion` + `_promote_or_pend` cross-agent gate,
  and the `edit_instruction` tool scope check. Regression tests in
  `backend/tests/e2e/test_instruction_sharing_authority.py`.
- Fix 5 (UI read-only) below: the Agents explorer now shows Edit/Delete only to
  users with authority over every attached agent; others see a read-only lock.
  `useCanAll()` in `frontend/composables/usePermissions.ts`.
- Verified end-to-end with a 10-story cross-user/permission/agent harness and
  Playwright UI evidence (`media/pr/shared-instruction-authority/`).
- Feedback loop: `docs/feedback-loops/shared-instruction-authority.md`.

## Pressure valves (so associated managers aren't stuck)

Both reuse existing machinery:

- **Suggest a change** — a manager files an edit as a suggestion build that lands
  in the Review queue of people who hold all-agent authority (existing
  pending-approval flow).
- **Duplicate for my agent** — one-click agent-scoped copy the manager can edit
  freely. Uses the unused `source_instruction_id` for provenance. This is
  copy-on-write, manual and explicit.

## Edge cases

- **Attach to another agent** expands the required-authority set — an attach can
  lock out the person who could previously edit. So attaching must itself
  require edit authority over the instruction; only current editors can broaden
  scope. (Scope changes ride the same update path — audit it.)
- **Detach from own agent** — allow it as a manager-level action. Blast radius is
  exactly their own agent, so it needs no all-agent authority.

## Notification

When a shared instruction changes, notify managers of all attached agents. Even
with correct authorization, Admin2 should know agent2's behavior changed under
them — the mirror of the original complaint.

## Why this model

Lowest-complexity option that's internally consistent: it makes the ALL-agents
rule the design and conforms the rest to it. We delete permissive behavior
rather than build new machinery. It doesn't foreclose fuller copy-on-write
later — "duplicate for my agent" already is it. Main cost is admin load on
shared-instruction maintenance; the suggest + duplicate valves cover it.

## Code sites

- `backend/app/core/permission_resolver.py` — `RESOURCE_PERM_IMPLIES`
  (`manage` ⇒ `manage_instructions`), `check_resource_permissions` (all-agents
  gate).
- `backend/app/routes/instruction.py`, `backend/app/routes/build.py` — sync
  paths already enforcing the rule (`check_resource_permissions`,
  `_enforce_build_ds_access`).
- `backend/app/services/instruction_service.py` — `_can_auto_publish_build`
  (already all-agents; the model to mirror).
- `backend/app/services/agent_reliability_service.py` — `run_for_suggestion`,
  `_promote_or_pend` (fix 1).
- `backend/app/services/build_service.py` — `promote_build`, `approve_build`
  (fix 2).
- `backend/app/ai/tools/implementations/edit_instruction.py` — org-wide lookup
  (fix 3).
