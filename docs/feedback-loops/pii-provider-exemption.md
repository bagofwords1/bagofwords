# Feedback Loop — "PII protection: allow a per-LLM-provider override of the org policy"

PII protection was one policy per organization, applied to every model. An org
running a trusted self-hosted or in-VPC model next to a public API could not say
"redact for the public API, send real values to our own model". This loop
validates the fix: an org can mark LLM providers as **exempt**. Exempt providers
receive prompts unredacted, and every other provider keeps the org policy. What
people see in the app stays masked either way.

## Design

- **Storage:** a list on the existing org config,
  `PiiProtectionConfig.exempt_provider_ids`
  (`backend/app/schemas/organization_settings_schema.py:132`). There is no
  migration and no new column, and the per-org redactor cache is unchanged.
- **Resolution:** per LLM instance, from the provider it actually calls.
  - `LLM._pii_provider_id` (`backend/app/ai/llm/llm.py:657`) passes the provider
    to `load_redactor_for_org(..., provider_id)`
    (`backend/app/ai/llm/pii/loader.py:62`).
  - The loader returns `None` (no redaction) when `PiiRedactor.is_exempt`
    (`backend/app/ai/llm/pii/redactor.py:110`) matches.
  - Every outbound path (`inference`, `inference_stream`, `inference_stream_v2`,
    embeddings) resolves through these two methods.
  - A fallback model is a separate `LLM` instance, so the provider actually
    called decides.
- **Display masking:** display callers (`pii/display.py`,
  `completion_service`, `artifact_*`) pass no provider, so they get the org
  policy. The exemption never unmasks the UI.
- **Save validation** (`backend/app/services/organization_settings_service.py:542`):
  - A newly added id must be one of the org's live providers; anything else
    (typo, another org's provider) is rejected with a 400.
  - A stored id whose provider was deleted is dropped quietly, so it can never
    block unrelated PII saves.
  - A non-list value is a 400, and duplicates collapse.
- **Audit:** any change to the list writes its own
  `pii_protection.provider_exemptions_updated` entry, with
  `exempted_provider_ids` / `unexempted_provider_ids`
  (`organization_settings_service.py:531`). This is on top of the generic
  `settings.updated` entry.
- **Enterprise gate:** unchanged. The loader returns `None` before reading
  config on community builds, and the settings write is still a 402.
- **UI:** a "Provider exemptions" section on Settings → PII Protection
  (`frontend/pages/settings/pii.vue:47`). Each provider row has a "Send
  unredacted" toggle that autosaves.
  - The master switch description now says that exempted providers are the
    exception.
  - A change made while a save is in flight is saved right after it
    (`pii.vue:332`). Before, a quick second toggle was dropped.
  - If the provider list can't load, the stored list is kept as is, so a save
    from this page can't wipe an exemption.

## Loop A — deterministic (pytest, stubbed provider client)

```bash
cd backend
export BOW_DATABASE_URL="sqlite:///db/app.db" TESTING=true
uv run pytest tests/e2e/test_pii_protection.py -m e2e --db=sqlite -q
```

The new tests in `tests/e2e/test_pii_protection.py` go through the real
settings API and the real `LLM` + loader. Only the provider client is stubbed.

| Test | Promise |
|---|---|
| `test_exempt_provider_gets_raw_prompt_and_others_keep_org_policy` | Exempt provider receives the raw email; another provider in the same org receives it redacted; removing the exemption re-applies the policy |
| `test_exemption_does_not_relax_display_masking` | The display resolver (no provider) still gets an active redactor |
| `test_exempt_list_only_accepts_this_orgs_live_providers` | Unknown or other-org ids → 400, nothing saved; duplicates collapse; a deleted provider's stored id never blocks a later save |
| `test_changing_exemptions_is_audited_with_the_providers` | One audit entry per real change, naming added/removed providers; unchanged list → no entry |
| `test_member_cannot_change_exemptions` | A member gets 403 and nothing is saved |

Observed with the app change stashed (tests kept): **4 failed** (exempt
provider still redacted; loader has no `provider_id`; unknown ids saved; no audit
entry). The member test passes either way because the route gating already
existed. With the change: **12 passed**. Related suites (org settings, audit,
PII unit tests, LLM fallback, public output parity): **167 passed**.

## Loop B — live stack with a mock LLM (no credentials)

`tools/agent/pii_capture_llm.py` is an OpenAI-compatible mock. It records every
prompt it receives, tagged by the first path segment, so one process plays both
providers.

```bash
BOW_LICENSE_KEY=... tools/agent/boot_stack.sh --dev
cd backend
uv run python ../tools/agent/seed_org.py --demo
uv run python ../tools/agent/pii_capture_llm.py &          # :9199
uv run python ../tools/agent/pii_provider_exemption_setup.py
#   → providers "In-VPC Llama" (…/trusted/v1) and "Public API" (…/public/v1, org default),
#     PII on (replace), no exemptions
cd ..
node tools/agent/pii_provider_exemption_ui.mjs flow media/pr/pii-provider-exemption
```

The flow:
1. Turns on "Send unredacted" for In-VPC Llama in the UI.
2. Reloads the page and checks the toggle persisted.
3. Sends `Please email jane.roe@contoso.com the Q3 summary` once with each model
   picked in the composer.
4. Reads the mock's captures.

Observed:

```json
{ "persisted": "true",
  "byProvider": { "public": { "calls": 9, "raw": 0 }, "trusted": { "calls": 1, "raw": 1 } } }
```

- Public API calls carry `Please email [REDACTED_EMAIL] the Q3 summary`.
- The In-VPC Llama planner call carries the raw address.
- Side calls such as titles go to the org's small default model (Public API), so
  they stay redacted.
- The chat bubble in both reports shows `[REDACTED_EMAIL]`, so display masking is
  unchanged.
- `GET /api/enterprise/audit?action=pii_protection.provider_exemptions_updated`
  lists each toggle with the provider id.

Evidence is in `media/pr/pii-provider-exemption/`: `before.png`, `after.png`,
`after-he.png` (RTL), `after-section.png`, `chat-gpt-public.png`,
`chat-llama-3.3-70b.png`. For the Hebrew shot, run
`BOW_LOCALE=he node tools/agent/pii_provider_exemption_ui.mjs shot <out.png>`.

## Regression notes

- `tests/e2e/dashboards/test_artifact_conversations.py` errors at fixture
  setup on SQLite: `no such column: "starting_folder_ids"` during the corpus
  migration downgrade. It fails the same way with this change stashed, so it is
  pre-existing and unrelated.
- Strings: 7 new `settings.pii.*` keys, plus an updated `masterDesc`, in all 10
  catalogs. The catalog sync check's missing/extra counts are unchanged.
