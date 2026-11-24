# PII policies — per-provider policies, Presidio detection, restore in the UI

Status: agreed design, 2026-11-18. All four phases are built.
Builds on the provider-exemption change. Enterprise only (`pii_protection`).

**Scope of every rule (agreed 2026-11-18):** PII rules apply to the user's
prompt and the data the agent reads (query results, data rows, uploaded file
text, the conversation). Instructions, schemas and other agent context reach
the model as written, unless a policy turns on `redact_context`. This applies
to regex rules as well as Presidio.

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
| **Policy** | A named set of options for every rule: enabled, action, replacement (phase 2: minimum score). It also has a default action, `redact_context` (also redact instructions and schemas; off by default), and (phase 2) what to do if a detector fails. **Default** always exists. **Exempt** is built in and has no rules. |
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
custom_rules:                     # shared list; built-in and detector rules come from code
  - {id: emp, name: Employee ID, patterns: ['EMP-\d{4}'], replacement: '[EMP]'}
policies:
  default:
    name: Default
    mode: replace                 # default action for rules without one: replace | block
    redact_context: false         # true: pattern rules also apply to instructions and schemas
    builtin_overrides:            # per rule: enabled, action, replacement
      email: {action: pseudonymize}
      credit_card: {action: mask}
    custom_overrides:
      emp: {enabled: false}
    detector_overrides:           # off unless enabled; per entity: action, replacement, min_score
      PERSON: {enabled: true, min_score: 0.7}
    on_detector_error: block      # block | regex_only
  strict:
    name: Strict
    mode: block
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
- Provider ids are checked against this org's live providers, as the exemption change
  already does.
- Rule options for unknown rules are dropped.

## What happens on each LLM call

```
LLM call ─► policy = provider_policies[provider of this call] or Default
            (a fallback model is its own LLM instance → its own provider decides)
        ─► split the prompt into context (marked with pii_context at prompt assembly:
           instructions, schemas, roster, skills, resources, saved-query definitions,
           the chat system prompt) and everything else (prompt + data); unmarked text
           is in scope, so a new prompt path fails closed
        ─► detectors run on the prompt + data only (and on context too with redact_context);
           markers are stripped from every outbound request, PII on or off
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
| `detectors/` | `Detector.detect(text, entities) -> spans`; registry, content cache, chunking. Implementations: `presidio_inproc`, `presidio_http` |
| `context.py` | Phase 1. `pii_context()` marks context regions at prompt assembly; `split_segments` / `strip_markers` (fail-closed) |
| `redactor.py` | The engine: block rules on the original text, pattern rules in order with position tracking, then detector spans; `mask` and `pseudonymize` |
| `vault.py` | Per-call token ↔ value map; tokens are an HMAC of the value keyed per report, so nothing is stored. `StreamRestorer` holds back a partial token at a chunk edge |

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
| `pii_protection.provider_policies_updated` | Assignments change | Replaces the exemption change's `provider_exemptions_updated` |
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

## Phases (all built)

Loop docs with observed results: `docs/feedback-loops/pii-policies.md`
(phase 1) and `docs/feedback-loops/pii-detectors-and-tokens.md` (phases 2–4).

### Phase 1 — named policies, prompt + data scope

- `policy.py`: every stored shape normalizes to one config; a policy-aware
  write replaces the stored policies, an older client's write lands on Default.
- `context.py`: prompt builders mark platform context; only the prompt and the
  data are redacted. `redact_context` per policy.
- Provider → policy at the LLM chokepoint; `pii_blocked` error (no retry, no
  fallback); audit; settings page; LLM page badge.

**Upgrade rule.** Before policies, an enabled rule set redacted the whole
prompt. A stored single rule set that is **enabled** therefore converts to a
Default policy with `redact_context: true`: it keeps its coverage until an
admin turns that off. Policies created later, and protection enabled for the
first time, default to prompt + data.

**The text cannot mark itself.** User messages and tool results are
interpolated into the same strings as the marked context, so the marker
characters alone prove nothing. A region is `START content END hmac`, where the
HMAC covers the exact content and is made with a key that lives only in the
process's memory. A region without a valid HMAC — reserved characters typed by
a user or returned by a tool, a replayed HMAC on other content, a region edited
or truncated after marking — is scanned like any other text. Structured
segments (carrying origin as metadata and flattening after redaction) would
give the same guarantee, but every prompt in the app is assembled as one
f-string in nine builders; the authenticated marker reaches the same property
without rewriting them, and still fails closed.

### Phase 2 — detectors (Presidio in-process)

- **Engine** (`redactor.py`): pattern rules still substitute one after another
  in rule order — the previous behavior, proven by a differential test — while
  the engine tracks positions, so detector spans found on the original text
  survive the substitutions. A pattern rule wins over a detector span it
  overlaps; among detector spans the longest wins, then the highest score.
- **Detector interface** (`detectors/base.py`): `detect(text, entities) ->
  spans`. Results are cached by content (the planner re-sends the whole
  conversation every step) and long text is analyzed in chunks.
- **In-process Presidio** (`detectors/presidio_inproc.py`): the optional `pii`
  extra (`presidio-analyzer` + `en_core_web_sm`). The model loads on first use;
  scanning runs in a worker thread.
- **Per policy:** `detector_overrides` (entities are off until enabled; action,
  replacement, `min_score`, default 0.7) and `on_detector_error`
  (`block` default | `regex_only`, audited as
  `pii_protection.detector_failed`, at most once a minute per org and policy).
- **Scope:** a detector never runs on platform context, even when the policy
  sets `redact_context`: a table or column called "Jordan" must reach the
  model. Display masking is pattern rules only.
- Turning a detector rule on is refused (400) when the instance has no
  detector.

**Model choice.** Measured with `tools/agent/pii_detector_eval.py` (synthetic
BoW-style text; numbers in the loop doc):

| | English | Hebrew | False positives on analytics text | Speed | Memory |
|---|---|---|---|---|---|
| spaCy `en_core_web_sm` | good on names, weaker on places | none | high | ~12 ms | lowest |
| spaCy `en_core_web_lg` | a little better | none | high | ~12 ms | ~1 GB |
| GLiNER `gliner_multi_pii-v1` | best | good | low at 0.8 | ~120 ms CPU | ~1.8 GB |

- The in-process default is `en_core_web_sm`: small, no extra dependencies.
- GLiNER cannot run in-process: its tokenizer needs a protobuf version that
  conflicts with the backend's pinned one. It runs behind `pii.presidio_url`
  (phase 4). It is the only option here that reads Hebrew.
- The false positives on analytics text are why detectors never run on
  context.

### Phase 3 — hide from the model, restore for people (`pseudonymize`)

- A value reaches the model as a token such as `<EMAIL_3fa9c21d>`: an HMAC of
  the value keyed per report. The same value always gets the same token in a
  report, tokens from different reports can't be correlated, and parallel calls
  agree without coordination.
- **No stored vault.** The original plan stored token ↔ value per report. It
  isn't needed: the conversation is stored with real values, so every call
  re-derives the same tokens for what it sends and restores what the model
  writes back in that same call (`vault.py`). There is no second copy of the
  values at rest, nothing to delete with the report, no migration.
- **Restore happens once, at the LLM chokepoint** (`llm.py`), so every provider
  client is covered: streamed text (a token split across chunks is held until
  complete), reasoning, streamed tool arguments (JSON-escaped), final tool
  arguments, web-search queries.
- Tools therefore run with real values. Their results, and the replayed tool
  calls, are tokenized again on the way out.
- The model is told once (appended to the system prompt when a token was
  produced) to copy placeholders exactly.
- **Display:** rules with this action do not mask the UI; anyone who can open
  the report sees real values (decision 3).
- `mask` (keep the last four characters) was added alongside.

### Phase 4 — Presidio as a service

- `detectors/presidio_http.py`: `POST {url}/analyze`, the official analyzer's
  API. Any failure (unreachable, timeout, non-200, malformed body) is a
  detector outage and follows the policy.
- `pii.presidio_url` in bow-config or `BOW_PII_PRESIDIO_URL`; when set it wins
  over the in-process detector.
- Helm: `pii.presidio.enabled` (bundled official image) or `pii.presidio.url`.
  docker-compose: a commented `presidio` service. Dockerfile:
  `--build-arg INSTALL_PII_EXTRA=true` for the in-process option.
- `tools/agent/presidio_service.py`: the same API serving spaCy or GLiNER, for
  loops and as a reference for a multilingual deployment.

### Phase 5 — validated identifiers from Presidio

- `detectors/catalog.py` now has two kinds of detector entity: `ner` (names,
  places, organizations, groups) and `pattern`: 21 Presidio recognizers that
  confirm a match with a **checksum** (credit card/Luhn, IBAN/mod-97, ABA
  routing, Bitcoin address, US NPI, US DEA, UK NHS, ES NIF/NIE, IT fiscal code,
  DE tax ID, PL PESEL, FI HETU, SE personnummer, TR national ID, AU
  TFN/ABN/Medicare, IN Aadhaar, KR RRN, ZA ID). A valid match scores 1.0, a
  broken checksum is dropped, so the default `min_score` of 0.7 holds.
  Recognizers without a checksum (US passport, driver's license, bank
  account…) are left out: they score 0.05–0.5 and pass only with context.
- Why next to the built-in regex rules: on random 13–16 digit IDs the built-in
  `credit_card` rule matches every one; Presidio's `CREDIT_CARD` about one in
  ten (those that pass Luhn). Both can be on; an overlapping built-in match wins.
- In-process: the pattern recognizers are added to the registry with either
  engine (no model needed). HTTP: the entities offered are the ones the service
  lists at `GET /supportedentities` (the official image disables several by
  default in `default_recognizers.yaml`); a service without the endpoint is
  treated as NER-only.
- Settings page: a separate **Validated identifiers** group, off until enabled.

## Not in scope

- Policies per agent or data source: one prompt mixes several agents' content
  (see the provider-exemption feedback loop). A "strictest wins" variant that
  avoids this is proposed in `docs/design/pii-agent-policies.md`.
- Per-user policies.
- Applying PII rules to stored data at rest.
