# PII policies — per-provider policies, Presidio detection, restore in the UI

Status: agreed design, 2026-11-18. Builds on BOW-152 (provider exemptions,
shipped on `claude/stoic-meitner-fq7v0t`). Enterprise only (`pii_protection`).

## Why

PII protection started as one org-wide rule set applied to every model. Orgs
mix trusted models (self-hosted, in-VPC) with public APIs. They need:

- a different set of rules and options for each kind of provider,
- detection of things regex can't find (names, places), and
- redaction that only the model sees: users of the app still see the real data.

## Concepts

| Concept | What it is |
|---|---|
| **Rule** | One detector. Comes from one of three sources: **built-in** (regex in code), **custom** (regex the org defines), or **Presidio** (an entity such as `PERSON`, from Presidio's catalog). One shared list per org. |
| **Policy** | A named set of options for every rule: enabled, action, replacement, minimum score. It also has a default action, a scope, and what to do if a detector fails. **Default** always exists. **Exempt** is built in and has no rules. |
| **Assignment** | Provider → policy. A provider with no assignment uses Default. |
| **Action** | `replace` (fixed token), `mask` (`****1234`), `block` (refuse the call), `pseudonymize` (numbered token, restored afterwards). Stored as a string so more can be added. |
| **Display policy** | The policy that masks what people see when values are not restored. Default. |

### Why one shared rule list (not rules per policy)

Patterns are the hard part, and the options are what differ between policies.

- A shared list means a fixed regex reaches every policy at once.
- Policies stay comparable side by side.
- It matches how built-in rules already work: patterns live in code and the org
  stores options.

A rule a policy doesn't need is simply turned off there.

## Data model

Stored where it is today, in `OrganizationSettings.config["pii_protection"]`.
No new tables.

```yaml
enabled: true
custom_rules:                     # shared list; built-in + Presidio rules come from code / catalog
  - {id: emp, name: Employee ID, patterns: ['EMP-\d{4}']}
policies:
  default:
    name: Default
    mode: replace                 # default action for rules without one
    scope: [user, data]           # where Presidio runs; regex runs everywhere
    on_detector_error: block      # block | regex_only
    rule_options:                 # open-ended per rule, so detector options need no schema change
      email: {enabled: true, action: replace, replacement: '[EMAIL]'}
      presidio.PERSON: {enabled: false}
  strict:
    name: Strict
    mode: block
    rule_options: {presidio.PERSON: {enabled: true, min_score: 0.7}}
provider_policies: {<provider_id>: strict, <provider_id>: exempt}
display_policy: default
```

**Reading today's format.** It is converted on read:

| Today's field | Becomes |
|---|---|
| `mode`, `builtin_overrides`, each custom rule's `action` / `replacement` / `enabled` | `policies.default` |
| `custom_rules` (`id`, `name`, `patterns`) | the shared list |
| `exempt_provider_ids` | `provider_policies[id] = exempt` |

Saves always write the new format. A client still sending the old fields keeps
working.

**Saving.**

- Policy ids are slugs.
- `default` and `exempt` cannot be deleted or renamed.
- Deleting a policy moves its providers back to Default.
- Provider ids are checked against this org's live providers, as BOW-152
  already does.
- Rule options for unknown rules are dropped.

## What happens on each LLM call

```
LLM call ─► policy = provider_policies[provider of this call] or Default
            (a fallback model is its own LLM instance → its own provider decides)
        ─► split the prompt into segments: system/schema | user message | tool results / data rows
        ─► detectors: regex on every segment; Presidio only on segments in policy.scope
        ─► collect all matches by position; resolve overlaps:
           block wins, then the longest match, then the highest score
        ─► apply actions once: block → PiiPromptBlockedError | replace | mask | pseudonymize
        ─► send to the provider
        ◄─ reply: restore pseudonym tokens in streamed text, tool arguments and generated titles
        ─► the tool runs with real values ─► its result is redacted with the same tokens before the next call
```

The chokepoint in `backend/app/ai/llm/llm.py` keeps its shape:

- `_aget_pii_redactor` / `_get_pii_redactor_sync` resolve the compiled policy
  for `_pii_provider_id()` instead of an org redactor.
- `_apply_pii` / `_apply_pii_v2` call the engine.
- Display masking (`backend/app/ai/llm/pii/display.py`) calls the same engine
  with the display policy.

## Components (`backend/app/ai/llm/pii/`)

| Module | Job |
|---|---|
| `rules.py` | The rule list: built-in catalog + the org's custom rules + Presidio entities (when installed) |
| `policy.py` | Policy schema; conversion from today's format; provider → policy lookup; compiled policy cache per (org, policy), cleared on save |
| `detectors/` | `Detector.detect(text) -> list[Match(start, end, rule_id, score)]`. Implementations: `regex` (phase 1), `presidio_inproc` (phase 2), `presidio_http` (phase 4) |
| `engine.py` | Collects matches by position, resolves overlaps, applies actions. Replaces today's rule-by-rule `re.sub` |
| `vault.py` | Phase 3. Token ↔ value map per report: encrypted with the existing key, deleted with the report, never sent to a model |
| `restore.py` | Phase 3. Restores tokens in streamed text (holds back a partial token at a chunk edge), tool arguments and titles |

`loader.py` and `redactor.py` stay as the public entry points, so callers don't
change.

## UI

**Settings → PII Protection**

- A policy picker with create (copies Default), rename and delete.
- The shared rule list shows the selected policy's options for each rule.
- Custom rule patterns are edited once, for every policy.
- "Test your rules" runs against the selected policy.
- The provider section has a policy dropdown per provider. It replaces today's
  "Send unredacted" toggle; Exempt is one of the options.

**Settings → LLM:** a read-only "PII: <policy>" badge on each provider. It shows
only when the instance is licensed and protection is on, and links to the PII
page. It stays hidden for users who can't read org settings.

**Blocked calls** surface as a typed `pii_blocked` error (`app/errors/codes.py`
plus an `errors.pii_blocked` key in every catalog). The message names the
matched rules and the model, e.g. "PII protection blocked this request to
gpt-public (rule: US SSN)". The same error covers a fallback that lands on a
blocking policy and a Presidio failure under `on_detector_error: block`.

## Audit

| Action | When | Details |
|---|---|---|
| `pii_protection.policy_created` / `policy_updated` / `policy_deleted` | Policy edits | Which rules / options changed, never patterns |
| `pii_protection.provider_policies_updated` | Assignments change | Replaces BOW-152's `provider_exemptions_updated` |
| `pii_protection.prompt_blocked` | A call is blocked | Rule ids and counts, provider, model; never values |
| `pii_protection.detector_failed` | Presidio couldn't run | Policy, provider, outcome (blocked / fell back) |

## Decisions (agreed 2026-11-18)

1. **Display policy:** Default.
2. **Block on fallback:** shown as the PII error (`pii_blocked`), naming rules
   and model.
3. **Restored values** (phase 3): anyone who can open the report sees real
   values. Access is left to the app's sharing settings, and **public share
   links and embeds restore too**.
4. **Presidio scope default:** user message and data rows. Schemas, system
   prompt and SQL get regex only, so query generation doesn't break.
5. **Presidio failure default:** block. Each policy can switch to
   `regex_only`, which is audited.
6. **Licensing:** Enterprise (`pii_protection`), including Presidio.

## Phases

### Phase 1 — named policies + position-based engine

Scope:

- `policy.py` and the conversion from today's format.
- `engine.py`: collect matches by position, resolve overlaps, apply once.
  Output must be identical to today's for every existing rule.
- `detectors/regex.py`.
- Provider → policy lookup at the chokepoint.
- Saving and validation.
- The `pii_blocked` typed error.
- The audit actions above.
- PII settings page with the policy picker and provider dropdowns.
- LLM page badge.
- Strings in all 10 catalogs.

Tests:

- Conversion: an existing org gets byte-identical redaction after upgrading.
- Two providers on different policies receive different redaction.
- Fallback across policies.
- Deleting a policy reassigns its providers.
- Overlap resolution.
- A member gets 403.
- The community build stays inert.
- Loop: extend `tools/agent/pii_capture_llm.py` and the UI script to cover
  three providers on Default / Strict / Exempt.

### Phase 2 — Presidio in-process

Scope:

- Optional `pii` extra (`presidio-analyzer` + one model).
- `detectors/presidio_inproc.py`: lazy load, run in a thread pool.
- Presidio entities added to the rule list.
- Per-policy `scope`, `min_score`, `on_detector_error`.
- Scan time per call recorded in traces.

Before choosing the default model:

- Run a `presidio-research` evaluation on BoW-style prompts, English + Hebrew.
- Compare spaCy `en_core_web_sm`, GLiNER `urchade/gliner_multi_pii-v1`, and a
  transformers model.
- Pick on measured precision and recall.

### Phase 3 — pseudonymize + restore

Scope:

- `vault.py` and `restore.py`.
- Numbered tokens (`<EMAIL_1>`), stable per report.
- Tool arguments restored before execution.
- Tool results redacted with the same vault.
- A system prompt line asking the model to copy tokens verbatim.
- Real values in the UI for anyone with access, public links included.

### Phase 4 — Presidio as a separate service (when needed)

Scope:

- `detectors/presidio_http.py` and `pii.presidio_url` in `bow-config`.
- A container in compose and Helm.

When to move:

- model RAM × workers stops fitting (`start.sh` runs up to 4 workers), or
- p95 scan time exceeds ~300 ms, or
- a large model or GPU is needed, or
- the install runs many replicas.

## Not in scope

- Policies per agent or data source: one prompt mixes several agents' content
  (see BOW-152).
- Per-user policies.
- Applying PII rules to stored data at rest.
