# Feedback Loop — PII policies per LLM provider, scoped to the prompt and the data (phase 1)

The provider-exemption change let an org exempt a provider from PII redaction. Phase 1 of the PII policies plan
(design: `docs/design/pii-policies.md`) generalizes this to named policies.
Each policy is a set of options over one shared rule list, and each LLM provider
is assigned a policy: Default, a named one, or Exempt.

This phase also adds the scope rule: **PII rules apply to the user's prompt
and the data, never to instructions, schemas or other agent context.**

This loop validates four things:
- each provider receives exactly what its policy says;
- context reaches the model unredacted while the prompt and the data do not;
- a blocked request is reported clearly, audited once, and never retried or
  sent to a fallback;
- an org that never touches policies redacts exactly as before.

## How it works (file:line)

- **Policies:** `backend/app/ai/llm/pii/policy.py`.
  - `normalize` turns any stored shape (single rule set, the exemption change's
    `exempt_provider_ids`, named policies) into one canonical config.
  - `apply_update`: a policy-aware write replaces the stored policies; an older
    client's write lands on Default.
  - `validate_update` rejects writes that `normalize` would otherwise repair
    quietly.
  - `compile_policy` builds each policy through the pre-existing rule resolver,
    so Default compiles to the same rules as before.
  - `CompiledPiiConfig.for_provider` / `for_display`.
  - `diff_for_audit`.
- **Scope:** `backend/app/ai/llm/pii/context.py`.
  - `pii_context()` wraps context in two private-use Unicode markers.
  - The redactor (`redactor.py`, `PiiRedactor.scan`) skips marked regions unless
    the policy sets `redact_context`, and always strips the markers.
  - It fails closed: unmarked or unterminated text is redacted.
- **Where context is marked:** only at prompt-assembly sites, never in shared
  formatters.
  - **Planner:** `prompt_builder_v3.py` (static context, legacy user message,
    knowledge message, folder-routing instructions) and `planner_v3.py`
    (`_pii_scoped_system`, chat system prompt only).
  - **Other agents:** `coder.py`, `judge.py`, `answer.py`, `reporter.py`,
    `dashboard_designer.py`, `suggest_instructions.py`, `planner.py`,
    `prompt_builder.py`.
  - **In scope (redacted):** the user's message, conversation history, tool
    results and data rows, uploaded file text (`files_context`), notes, the
    user's profile and memory, and the sub-agent and knowledge system prompts.
- **LLM chokepoint:** `backend/app/ai/llm/llm.py`.
  - `_apply_pii` and `_apply_pii_v2` strip markers on every path, even with PII
    off, the provider exempt, or a community build.
  - `_pii_blocked` names the model and policy on a block and queues the
    `pii_protection.prompt_blocked` audit (`pii/audit.py`).
  - `generate_image` now redacts its prompt too; before this, it sent the prompt
    with no PII step.
  - Tool results whose content is a list of blocks are walked as well.
- **Errors:** `backend/app/ai/llm/errors.py` adds a `pii_blocked` code (with
  `rules` and `policy`). It is in neither the retryable nor the
  fallback-eligible codes. `backend/app/ai/agent_v2.py` ends the turn on it
  instead of retrying.
- **Save:** `backend/app/services/organization_settings_service.py`.
  - Strict validation.
  - Provider ids are checked against this org's live providers.
  - One audit entry each for `policy_created` / `policy_updated` /
    `policy_deleted` / `provider_policies_updated`.
- **Settings schema:** `PiiProtectionConfig` (`organization_settings_schema.py`)
  normalizes on read and mirrors the old single-rule-set fields from Default.
- **UI:**
  - `frontend/pages/settings/pii.vue`: provider → policy dropdowns, a policy
    picker with create, rename and delete, per-policy rule options, shared
    patterns, the "Also redact instructions and schemas" toggle, and a preview of
    the selected policy.
  - `frontend/components/LLMsComponent.vue`: a "PII: <policy>" badge on each
    provider.
  - `frontend/pages/reports/[id]/index.vue`: a translated `pii_blocked` message
    (`dir="auto"` so it reads right in RTL).
  - Strings in all 10 catalogs.

## Loop A — deterministic (pytest, provider client stubbed)

```bash
cd backend
export BOW_DATABASE_URL="sqlite:///db/app.db" TESTING=true
uv run pytest tests/unit/test_pii_policies.py tests/e2e/test_pii_protection.py --db=sqlite -q
```

- **`tests/unit/test_pii_policies.py`** (74 cases):
  - **Golden fixture** `tests/fixtures/pii_redaction_golden.json`: the
    pre-policy output for 5 configs × 8 texts, generated from the redactor
    before this change. Verified through three routes: the old shape compiled
    as is, an old client's write, and a settings round trip.
  - provider → policy resolution;
  - context left as is, and markers never surviving;
  - fail-closed segment splitting;
  - the planner prompt leaving instructions and schema but redacting the
    prompt and file text;
  - write validation;
  - the audit diff;
  - `pii_blocked` classification.
- **`tests/e2e/test_pii_protection.py`** (the new tests, through the settings
  API and the real `LLM`):
  - three providers on Default / Strict / Exempt;
  - instructions and schema unredacted with the prompt redacted;
  - markers stripped when PII is off, the provider is exempt, or the build is
    community;
  - deleting a policy reassigns its providers;
  - the round trip with mirrored fields;
  - preview of the selected policy;
  - a member gets 403;
  - a block is audited once.

**Can each test fail?** Breaking the code makes the right tests fail:

| Mutation | Tests that fail |
|---|---|
| `pii_context` returns its text unmarked | 3 scope tests |
| The loader ignores the provider | 3 provider tests |
| Markers not stripped when no policy applies | 3 marker tests |

**Related suites** (org settings, audit, PII, LLM fallback and errors, planner
prompt builders, agent-loop rescue, public output parity): **437 passed**.
Locale sweep (`playwright.i18n.config.ts`): **30 passed**.

## Loop B — live stack, mock LLM, and Claude Haiku 5.5

```bash
BOW_LICENSE_KEY=... tools/agent/boot_stack.sh --dev   # pin BOW_ENCRYPTION_KEY: see notes
cd backend
uv run python ../tools/agent/seed_org.py --demo
uv run python ../tools/agent/pii_capture_llm.py &                  # mock on :9199
ANTHROPIC_KEY_LOOP=... uv run python ../tools/agent/pii_provider_exemption_setup.py
cd ..
node tools/agent/pii_provider_exemption_ui.mjs flow media/pr/pii-policies
node tools/agent/pii_provider_exemption_ui.mjs live media/pr/pii-policies   # backend with BOW_LLM_TRACE_FILE + BOW_LLM_TRACE_FULL
```

**Setup** (`pii_provider_exemption_setup.py`):
- three mock providers: In-VPC Llama, Public API (the org default) and Strict
  API (assigned the Strict policy, block mode);
- an Anthropic provider with Claude Haiku 5.5 on Default;
- the org instruction "…contact billing-escalations@musicstore.example".

**Mock flow, observed.** The same message, `Please email jane.roe@contoso.com
the Q3 summary`, sent through each provider:

```json
{ "persisted": "exempt",
  "blockedMessage": "PII protection blocked this request to gpt-strict (rule: Email address).",
  "blockedAfterReload": 1,
  "byProvider": {
    "public":  { "calls": 11, "raw_prompt_email": 0, "instruction_email": 3 },
    "trusted": { "calls": 1,  "raw_prompt_email": 1, "instruction_email": 1 } } }
```

- **Default (Public API):** the user's email was redacted in every call, and the
  instruction's email reached the model as written.
- **Exempt (In-VPC Llama):** raw values.
- **Strict:** nothing reached the provider. The chat shows the translated block
  message, before and after a reload.
- **Audit log:**
  - exactly **one** `pii_protection.prompt_blocked` entry, `outcome: denied`,
    with rule names, policy, model and report; no values;
  - `provider_policies_updated` entries with from → to;
  - `policy_created` for Strict.

**Live leg on Claude Haiku 5.5, observed.** Question: "What is the email
address of the customer Luís Gonçalves? And who should I contact about a
refund?" The trace file (post-PII requests, 3 calls to `claude-haiku-5-5`)
shows:
- the customer's real email (in query results, i.e. data) **never** reached
  Anthropic;
- the tool result carried `[REDACTED_EMAIL]` ×2;
- the org instruction's email was present in all 3 calls;
- no markers in any request.

The model answered "the email came back redacted in the query output". It
repeated the refund contact from the instruction, which the UI's display
masking (Default policy) then showed as `[REDACTED_EMAIL]`. That is the agreed
display behavior (decision 1). Restoring real values on screen is phase 3
(phase 3).

**UI, observed:**
- **Create, rename, assign, delete:** creating "Email only" (a copy of
  Default), turning off every built-in rule except email, and assigning Public
  API to it all saved with 200. Deleting it moved Public API back to Default.
- **LLM page:** each provider shows its badge, e.g. "PII: Default",
  "PII: unredacted", "PII: Strict".

**Evidence** in `media/pr/pii-policies/`:
- `after.png`, `after-he.png`, `after-providers.png`, `after-strict-policy.png`,
  `new-policy.png`
- `llm-badges.png`
- `chat-gpt-public.png`, `chat-llama-3.3-70b.png`, `chat-gpt-strict.png`,
  `chat-blocked-he.png`
- `live-haiku.png`

**Before:** `media/pr/pii-provider-exemption/after.png` (the provider-exemption page).

## Notes and regressions checked

- **Matching engine unchanged.** Today's rule-by-rule engine is kept, so
  existing orgs get identical output by construction. The position-based engine
  moves to phase 2, where Presidio needs it.
- **Encryption key.** `boot_stack.sh` does not pin `BOW_ENCRYPTION_KEY`, so a
  backend restart makes stored provider credentials undecryptable ("Failed to
  decrypt credentials"). Pin the key before restarting, or re-save each
  provider's credentials through the API, as this loop did.
- **Tests that patched `loader.build_redactor`** to simulate a failed load now
  patch `loader.compile_config`, the loader's build step.
- **Pre-existing, unrelated:**
  `tests/e2e/dashboards/test_artifact_conversations.py` errors at fixture setup
  on SQLite (`no such column: "starting_folder_ids"`). It fails the same way
  without this change.
