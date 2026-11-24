# PII policies per agent — strictest wins

Status: **proposal, not built.** 2026-11-24.
Builds on `docs/design/pii-policies.md` (named policies, provider assignments,
detectors, pseudonymize). Enterprise only (`pii_protection`).

That doc lists *"Policies per agent or data source"* as not in scope, because
one prompt mixes several agents' content. This proposal keeps that constraint:
an agent's policy is never applied to "its part" of a prompt. Instead, an
agent's policy applies to **every LLM call that agent takes part in**. When
several policies apply to one call, they are merged into one policy that is at
least as strict as each of them.

## Why

Today the policy is chosen only by the LLM provider a call goes to
(`provider_policies`, `backend/app/ai/llm/pii/policy.py`). Admins have asked
for the policy to also follow the data. For example:

- "Anything that touches the HR agent must have names and emails redacted, even
  on our in-VPC model."
- "The Finance agent may never send card numbers to any model."

These are guarantees about an agent's data. With provider-only policies they
can't be expressed: a provider marked Exempt sends HR data unredacted, and a
loose provider policy applies to every agent.

## Behavior in one table

| Agents on the call | Provider policy | Policy applied |
|---|---|---|
| No agent has a policy | P | P (same as today) |
| A has policy X, B has none | P | merge(P, X), applied to the **whole** prompt, including B's data |
| A has X, B has Y | P | merge(P, X, Y) |
| A has X | Exempt | X. An agent policy overrides Exempt. |
| Any | Any, PII protection off for the org | nothing (same as today) |
| Unknown (the call carries no agent scope) | P | merge(P, every policy assigned to any agent). Fails closed, see below. |

**Rule of thumb: adding an agent to a chat can only make redaction stricter.**

## Concepts

| Concept | What it is |
|---|---|
| **Agent assignment** | Agent (data source) → policy. Optional. An agent with no assignment adds nothing to a call. |
| **Agent scope** of a call | The set of agents whose data or context can be in that call's prompt. |
| **Effective policy** | The merge of the provider's policy and the policies of every agent in scope. It is compiled into a single `PiiRedactor`, so the engine itself doesn't change. |

Agents can't be assigned **Exempt**. An agent assignment can only add
redaction, never remove it.

## Data model

Stored with the rest of the PII config in
`OrganizationSettings.config["pii_protection"]`. No new tables and no
migration.

```yaml
policies: {default: {...}, strict: {...}, hr: {...}}
provider_policies: {<provider_id>: strict, <provider_id>: exempt}
agent_policies:                   # new; optional
  <data_source_id>: hr
  <data_source_id>: strict
display_policy: default
```

**Saving** follows the `provider_policies` rules:

- Agent ids are checked against this org's live data sources. Unknown ids are
  dropped.
- Values must be an existing policy id other than `exempt`.
- Deleting a policy removes its agent assignments. They are not moved to
  Default, because the agent had chosen a specific policy and an admin should
  decide again. This is audited.
- Deleting an agent drops its assignment the next time the config is normalized.

An org with no `agent_policies` produces exactly the same redaction as today.

## Merging policies

`merge(policies...) -> policy`, a pure function in `policy.py`. The output is
an ordinary policy dict that goes through the existing `compile_policy` path.

| Field | Merge rule |
|---|---|
| Rule `enabled` (built-in, custom, detector) | On if **any** policy turns it on |
| Rule `action` | The strongest of the policies that turn the rule on: `block` > `replace` > `pseudonymize` > `mask` |
| Rule `replacement` | Taken from the policy whose action won. On a tie, the first in merge order (provider policy, then agent policies by agent id) |
| Detector `min_score` | The **lowest** among the policies that turn the entity on (catches more) |
| `mode` (default action) | `block` if any policy says `block` |
| `redact_context` | On if any policy turns it on |
| `on_detector_error` | `block` if any policy says `block` |
| `name` | Joined, e.g. `Default + HR`. This is what blocked-call errors and audit entries show. |

Why this order of actions:

- `block`: the model never sees the call.
- `replace`: the model sees a fixed token, with no link between values.
- `pseudonymize`: the model sees a stable token, so it can tell that two values
  are equal. Values are restored for people.
- `mask`: the model sees part of the value (`****1234`).

**Invariant, enforced by a property test:** for every policy `p` in the input,
a text that `p` would block is blocked by the merge, and any span that `p`
hides is also hidden by the merge, with an action at least as strong.

**Cost.** Merged redactors are cached per (org, sorted tuple of policy ids) in
the loader's existing per-org cache, and cleared on every settings save
(`loader.invalidate`). Orgs have at most 20 policies, so there are few combos,
and the steady state needs no DB reads.

## Agent scope: which agents count for a call

Agent scope is the part that can go wrong silently, so it is built to **fail
closed**.

**Source.** Add `data_source_ids: list[str]` to `UsageAttribution`
(`backend/app/ai/llm/usage_attribution.py`). AgentV2 already stamps this
contextvar at the start of a run (`backend/app/ai/agent_v2.py`, around
`set_usage_attribution`). Today it carries a single `data_source_id` and only
when the report has exactly one. The new field carries the full set. The
chokepoint (`_aget_pii_redactor` / `_get_pii_redactor_sync` in
`backend/app/ai/llm/llm.py`) reads it next to the provider id.

**What goes into the set for a report:**

1. Every agent attached to the report (`report.data_sources`), whether or not
   it has been queried yet. Its schema and instructions may already be in the
   prompt.
2. **Sticky:** every agent whose data has entered this report before, even if
   it was detached later. Its rows are still in the conversation history.
   Sources: the data source ids on the report's steps and queries.
   *Open question 1.*

**No scope.** If a call has no agent scope (the contextvar is unset or the set
is empty) and the org has any `agent_policies`, the effective policy is
merge(provider policy, **every policy assigned to any agent**). We can't prove
which agents' data is in the prompt, so we assume all of them. This is the same
fail-closed stance `pii/context.py` takes for unmarked text.

A call site that has been checked and carries no agent data (e.g. a generic
title generation with no data in it) can opt out explicitly by setting the
scope to an empty set with `scope_verified=True`. Anything not marked stays
strict.

**Entry points to cover.** Each LLM entry point has to stamp an agent scope or
deliberately rely on the fallback:

| Entry point | Scope |
|---|---|
| AgentV2 runs: web chat, Slack/Teams/WhatsApp, scheduled automations | Report agents + sticky set, stamped at run start |
| Sub-agents and context compaction inside a run | Inherit the run's contextvar (to verify with a test) |
| Artifact-page chat and resources (`backend/app/routes/artifact_resources.py`) | The report's set |
| Agent indexing and onboarding LLM calls (`use_llm_sync`, schema descriptions) | The single agent being indexed |
| MCP tools | The agents named in the tool call, else the fallback |
| Instruction suggestions, judge/evals, anything else | Audit each one; the fallback applies until it's marked |

Phase 2 adds a test that lists every `LLM(...)` construction site and fails
when a new one has neither a stamped scope nor an explicit opt-out. That
catches new prompt paths at review time.

## What happens on each LLM call (changes marked ★)

```
LLM call ─► provider policy = provider_policies[provider] or Default (Exempt → none)
        ★─► agent scope = attribution.data_source_ids (or "unknown")
        ★─► agent policies = agent_policies[a] for a in scope
              (unknown scope → all assigned agent policies)
        ★─► effective = merge(provider policy, *agent policies)   [cached]
              (an agent policy overrides an Exempt provider)
        ─► rest unchanged: context split, detectors, overlap resolution,
           block | replace | mask | pseudonymize, restore on the way back
```

The vault, pseudonym restore, context markers, display masking and the engine
in `redactor.py` don't change.

## UI

**Settings → PII Protection.** Add an "Agents" section below "Providers": one
row per agent, with a policy dropdown ("No agent policy" plus every policy
except Exempt). A short explainer under the heading: *"An agent's policy
applies to every request that includes the agent, including the other agents in
the same chat. When several apply, the strictest option for each rule wins."*

**Agent page (`frontend/pages/agents/[...slug].vue`).** A read-only "PII:
<policy>" badge that links to the PII settings. It follows the same
license/permission gating as the provider badge on Settings → LLM.

**Test your rules.** Add an optional "as if these agents were in the chat"
multi-select, which runs the merged policy. Admins can then see the effective
policy without guessing.

**Blocked calls.** Same `pii_blocked` error. The policy name is the merged one
(`Default + HR`), so users can see why a call was blocked when it wasn't
blocked yesterday.

Strings go into every locale catalog, including RTL checks for `he`.

## Audit

| Action | When | Details |
|---|---|---|
| `pii_protection.agent_policies_updated` | Assignments change, including removals caused by a policy delete | agent id → old/new policy |
| `pii_protection.prompt_blocked` (extended) | A call is blocked | Adds `agent_ids` in scope, `policy_ids` merged, and `scope: known \| unknown` |
| `pii_protection.detector_failed` (extended) | Same | Same additions |

## Permissions

Agent assignments are part of the org PII config, so `manage_settings` is
required, the same as editing policies. *Open question 3* asks whether agent
owners should be able to tighten their own agent.

## Testing

- **Unit (`backend/tests/`, follow `backend/tests/AGENTS.md`):** the merge
  table above, field by field. The strictness invariant as a property test
  over random policies. Config normalization (unknown agent dropped, `exempt`
  rejected, deleting a policy drops its assignments). An org with no
  `agent_policies` gives the same output as today, as a regression test.
- **Chokepoint:** with attribution set, an Exempt provider plus an agent policy
  redacts. With unknown scope, the union applies. With a verified empty scope,
  only the provider policy applies.
- **E2E (sandbox-feedback-loop):** reuse `tools/agent/pii_capture_llm.py`.
  Two agents, one with an HR policy that turns on `PERSON`, the provider
  Exempt. Send one message in a chat with both agents and one in a chat with
  only the other agent, then check the mock's captures: the name is redacted in
  the first and raw in the second.
- **UI evidence (ui-evidence skill):** settings section, agent badge, `he`.

## Phases

1. **Core:** `agent_policies` config, `merge`, loader resolution,
   `data_source_ids` in attribution from AgentV2 (attached agents only),
   unknown-scope fallback, audit. API only.
2. **Coverage:** sticky agent set, the other entry points from the table, the
   call-site guard test.
3. **UI:** settings section, agent badge, "Test your rules" with agents, docs
   (docs-update skill), changelog (release-notes skill).

Phase 1 is safe to ship alone: the fallback makes every uncovered path stricter,
never looser.

## Open questions

1. **Sticky scope:** should an agent that was detached keep its policy on the
   report for good? Recommended: yes, since its rows stay in history. The
   alternative is clearing the sticky set when history is compacted past its
   last step.
2. **Unknown scope:** is "every assigned agent policy" too strict for orgs with
   many agents? It over-redacts calls that touch no data at all. The opt-out
   marking covers known-safe sites, but the scale is unknown until phase 2 has
   audited the entry points.
3. **Who can assign:** org admins only, or agent owners too? Tightening is
   harmless, but a policy with `block` rules can make an agent unusable in
   shared chats, so it's an org-level decision by default.
4. **Display policy:** should the display masking for an agent's data follow
   its policy too? Out of scope here, because display uses one org-wide policy
   and restore follows the report's sharing settings.

## Not in scope

- Applying a policy only to the part of a prompt that came from one agent. See
  the opening section: provenance doesn't survive prompt assembly, history or
  compaction, and losing it would mean redacting less.
- An agent loosening redaction (agent-level Exempt).
- Per-user policies, or applying PII rules to stored data at rest (unchanged
  from `pii-policies.md`).
