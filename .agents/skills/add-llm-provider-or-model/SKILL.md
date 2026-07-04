---
name: add-llm-provider-or-model
description: Add a new LLM model to the preset catalog, or a whole new LLM provider — with the mandatory pre-flight verification of model id, pricing, and context window against the provider's official docs. Use when asked to add/update LLM models, providers, or pricing.
---

# Add an LLM provider or model

## Pre-flight: verify BEFORE adding (mandatory)

Never add a model from memory — model ids and prices change and training
data goes stale. For each model, confirm from the **provider's official
docs/pricing page** (web search / fetch, cite the URL in the PR):

1. **Exact `model_id`** as the API expects it (e.g. `claude-sonnet-5`,
   `gpt-5.4-mini` — not the marketing name).
2. **Input and output cost per million tokens (USD)** — these feed real cost
   dashboards (`llm_usage_record`, console cost metrics); a wrong number
   corrupts every org's reported spend.
3. **Context window** (tokens) and **vision support**.
4. The model actually works on the provider's **current API version** used by
   our client (`backend/app/ai/llm/clients/<provider>_client.py`).

If any of these can't be verified from an official source, stop and say so —
don't guess.

## Adding a model (existing provider)

1. Append to `LLM_MODEL_DETAILS` in `backend/app/models/llm_model.py`:
   `name` (display), `model_id`, `provider_type`, `is_preset: True`,
   `is_enabled`, `supports_vision`, `context_window_tokens`,
   `input_cost_per_million_tokens_usd`, `output_cost_per_million_tokens_usd`.
   Flags to handle deliberately:
   - `is_default: True` — at most **one per provider_type** (that provider's
     flagship). Changing a default is a product decision — ask first.
   - `is_small_default: True` — the provider's cheap/fast model for
     background tasks; at most one per provider_type.
2. Preset providers **auto-sync** with this catalog
   (`llm_service.py` — "Only auto-sync preset providers with our curated
   catalog"), so existing orgs pick the model up; no migration needed.
   Custom providers keep the user's explicit selections.
3. If the model needs different request handling (new reasoning params,
   streaming quirks), extend the provider client — don't special-case in
   `agent_v2`/planner code.

## Adding a provider

1. **Client** — `backend/app/ai/llm/clients/<provider>_client.py`,
   implementing the same streaming interface as the existing clients
   (`openai_client.py` is the reference; `anthropic_client.py` for a
   non-OpenAI-shaped API). First check whether the provider is
   OpenAI-compatible — then it may just work through the existing
   **custom** provider (base_url override) and you only need presets.
2. **Dispatch** — wire the `provider_type` branch in
   `backend/app/ai/llm/llm.py` (`self.provider == "<type>"` → client).
3. **Catalog** — add to `LLM_PROVIDER_DETAILS` in
   `backend/app/models/llm_provider.py` (`type`, `name`, `description`,
   `config`, `credentials` schema) and define the `<Provider>Config` /
   `<Provider>Credentials` classes next to the existing ones. The credentials
   schema drives the settings-page form; credentials are Fernet-encrypted at
   rest (`encrypt_credentials`) — never log or return them.
4. **Models** — add its models to `LLM_MODEL_DETAILS` (pre-flight above).
5. **Frontend** — icon in `frontend/components/LLMProviderIcon.vue`; the
   provider form itself is schema-driven via `GET /llm/available_providers`.
6. **Prompt language**: conversational-vs-code agent split in
   `backend/app/ai/prompt_language.py` is provider-agnostic — no change
   needed unless the provider mishandles system prompts.

## Verification

```bash
cd backend
# Catalog sanity: unique ids; ≤1 default and ≤1 small default per provider
uv run python - <<'EOF'
from collections import Counter
from app.models.llm_model import LLM_MODEL_DETAILS as M
ids = [m["model_id"] for m in M]
assert len(ids) == len(set(ids)), "duplicate model_id"
for flag in ("is_default", "is_small_default"):
    per = Counter(m["provider_type"] for m in M if m.get(flag))
    dupes = {p: n for p, n in per.items() if n > 1}
    assert not dupes, f"multiple {flag} for provider(s): {dupes}"
assert all(m["input_cost_per_million_tokens_usd"] > 0 and m["output_cost_per_million_tokens_usd"] > 0 for m in M)
print(f"OK: {len(M)} models")
EOF
# Unit: connection-test schema still valid
uv run pytest tests/unit/test_llm_test_connection_schema.py -q
# Live round-trip (real key, env/integrations.json only — never committed)
uv run pytest tests/integrations/llm_clients.py -k "<provider>" -v
```

Then a live UI check: boot the stack, Settings → AI/LLMs → the provider and
model appear, "Test connection" passes with a real key, and one prompt
round-trips through the new model. Confirm a cost row lands in the console
usage metrics with the expected per-token math.

## Pitfalls

- Wrong price or a price in per-1K units instead of per-million — the
  catalog is per **million** tokens.
- Two `is_default`/`is_small_default` models — org defaults become
  nondeterministic.
- Adding an OpenAI-compatible provider as a full new client instead of a
  preset on the `custom` provider — double maintenance for nothing.
- CI note: `integration-llms` in `.github/workflows/e2e-tests.yml` currently
  skips `google`/`bedrock`/`openai-reasoning` cases — don't "fix" a red run
  by extending those skips; flag it instead.
