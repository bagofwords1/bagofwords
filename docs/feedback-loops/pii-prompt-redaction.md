# Feedback Loop — "add PII block/replace for prompts sent to the LLM"

Redact personal data (PII) from prompts **before** they are sent to any LLM
provider, with prebuilt + custom regex rules (a rule may hold several patterns),
a replace-with-token mode and a hard block mode. The feature is **enterprise-
gated** (`pii_protection`) and must not activate on a community build. This loop
validates the redaction engine, the LLM chokepoint wiring, the license gate, and
the settings UI end-to-end.

## What was built (file:line)

- Rule engine: `backend/app/ai/llm/pii/redactor.py` — compile, `scan`, `apply`
  (replace + block), no-raw-values audit summary; `validate_pattern` for save-time
  regex validation.
- Prebuilt rules: `backend/app/ai/llm/pii/builtin_rules.py` — email, credit card,
  US SSN, phone, IPv4, IBAN, AWS key (each with one or more patterns).
- Per-org loader + cache with the **enterprise gate**:
  `backend/app/ai/llm/pii/loader.py:load_redactor_for_org` (returns `None` unless
  `has_feature("pii_protection")`).
- LLM chokepoint: `backend/app/ai/llm/llm.py` — redaction applied in `inference`
  (`llm.py:270`), `inference_stream` (`:335`) and `inference_stream_v2` (`:452`,
  via `_apply_pii_v2`). Loaded lazily from `model.organization_id`, so it is
  on-by-construction at all ~29 `LLM(...)` call sites.
- License tier: `backend/app/ee/license.py` — `pii_protection` added to the
  `enterprise` tier.
- Config schema: `backend/app/schemas/organization_settings_schema.py`
  (`PiiRule`, `PiiProtectionConfig`); validation + cache invalidation in
  `backend/app/services/organization_settings_service.py`.
- API: `backend/app/routes/organization_settings.py` — builtin-rule catalogue +
  dry-run `/organization/pii/test`.
- UI: `frontend/pages/settings/pii.vue` + tab gating in
  `frontend/layouts/settings.vue` (hidden unless `hasFeature('pii_protection')`).

## Root cause / design (validated)

The single place every agent reaches a provider is the `LLM` class
(`backend/app/ai/llm/llm.py`) — planner, coder, judge, answer, tools all go
through `inference*`. Redacting there covers the *entire assembled prompt*
(user message + schema samples + data previews + uploaded file text) by
construction. Because a missed call site on a privacy control is a silent leak,
the redactor is resolved *inside* `LLM` from `model.organization_id` (cached,
enterprise-gated) rather than threaded through every constructor.

## Loop A — deterministic reproduction (no external services)

Pure-logic + API/DB, LLM provider stubbed. Runs in a clean sandbox on SQLite.

```bash
cd backend
export TESTING=true BOW_DATABASE_URL="sqlite:///db/app.db" TEST_DATABASE_URL="sqlite:///db/agenttest.db"
uv run pytest tests/unit/test_pii_redactor.py tests/e2e/test_pii_protection.py -q --db=sqlite
```

Observed: **26 passed**. Key assertions:

- `tests/e2e/test_pii_protection.py::test_inference_redacts_before_reaching_provider`
  — a prompt with `john@acme.com` / `415-555-1234` reaches a stub client with the
  PII already replaced (proves the chokepoint redacts before the provider call).
- `test_inference_stream_v2_block_mode_refuses` — block mode raises
  `PiiPromptBlockedError` before any provider call on the native tool-use path.
- `test_pii_write_refused_without_license` — PUT returns **402** when
  `has_feature` is False.
- `test_redactor_loader_is_enterprise_gated` — with an **enabled** config
  persisted, `load_redactor_for_org` returns a live redactor when licensed and
  **None** on a community instance. This is the guarantee the feature can't
  activate without a license.

To prove the loop can fail: stash the redaction call in `LLM.inference`
(`prompt = self._apply_pii(...)`) and `test_inference_redacts_before_reaching_provider`
fails because the raw email reaches the stub.

## Loop B — live UI end-to-end (Playwright, seeded stack)

```bash
# Enterprise: boot WITH the license key
BOW_LICENSE_KEY=... tools/agent/boot_stack.sh --dev
cd backend && BOW_DATABASE_URL="sqlite:///db/agent.db" uv run python ../tools/agent/seed_org.py --demo
# then drive frontend/pii_e2e.mjs  (login, enable, add multi-pattern rule, preview)
```

Observed (licensed): PII tab visible, master toggle + Replace/Block modes,
7 built-in rules, a custom "Employee ID" rule with two patterns
(`EMP-\d{4}`, `E\d{6}`). Live preview of
`Email john.doe@acme.com, call 415-555-1234, SSN 078-05-1120, badge EMP-4821 and E123456.`
→ `Email [REDACTED_EMAIL], call [REDACTED_PHONE], SSN [REDACTED_SSN], badge [EMPLOYEE_ID] and [EMPLOYEE_ID].`
(Matched: Email 1, SSN 1, Phone 1, Employee ID 2 — both custom patterns fired.)

Community leg: reboot the stack **without** `BOW_LICENSE_KEY`. Observed: the PII
tab is absent from `/settings`, `/settings/pii` shows the "Enterprise feature"
gate (no toggles), and `PUT /api/organization/settings` for `pii_protection`
returns **402**.

Screenshots: `media/pr/pii-block-replace-llm/`.

## What this proves

- PII is redacted from the full outbound prompt at the one chokepoint every
  agent shares, in both replace and block modes.
- One rule can carry multiple regex patterns; prebuilt and custom rules compose.
- The feature is strictly enterprise-gated — inactive and refused on community,
  active only under a valid enterprise license — verified via API, the loader,
  and the UI.
