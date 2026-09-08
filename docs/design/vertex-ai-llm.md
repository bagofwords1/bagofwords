# Google Cloud Vertex AI as an LLM provider — Analysis

**Status:** Research / analysis only — no implementation.
**Goal:** Decide whether and how Bag of Words should support Google Cloud
Vertex AI (Google now brands the model surface "Gemini Enterprise Agent
Platform"; the API host is still `aiplatform.googleapis.com`) as an LLM
provider, next to the existing OpenAI / Anthropic / Azure / Google (Gemini
Developer API) / Bedrock / custom providers.

---

## 0. Bottom line up front

1. **Do it as one new provider type, `vertex`, that routes by model family**
   — Gemini ids go to the existing `google-genai` client with `vertexai=True`,
   Claude ids go to the Anthropic SDK's `AnthropicVertex` client. This mirrors
   what the Azure provider already does (Anthropic deployments → Messages
   API, everything else → OpenAI-shaped routes, `app/ai/llm/llm.py`).
2. **No new SDKs.** Both pinned dependencies already carry the Vertex code
   paths: `google-genai 1.75.0` (`genai.Client(vertexai=True, project=,
   location=, credentials=)`) and `anthropic 0.40.0`
   (`anthropic.lib.vertex.AnthropicVertex`, needs `google-auth`, which is
   already a direct dependency at 2.55).
3. **Auth is the real design work, not the request/response plumbing.**
   Vertex authenticates with Google Cloud OAuth (service-account JSON or
   Application Default Credentials); the "express mode" API key exists but is
   Gemini-only and quota-limited. Model the auth modes the way Bedrock and
   Azure do (`auth_mode` in `additional_config`, secret in the encrypted
   `api_key` slot).
4. **The cheap alternative — pointing the `custom` OpenAI-compatible provider
   at Vertex's `/endpoints/openapi` — does not work** for real deployments:
   that endpoint only accepts a Google OAuth access token, which expires
   after ~1 hour, and the custom provider stores a static key.
5. **Effort:** roughly a Bedrock-sized change. ~10 backend files, 2 frontend
   files (the provider modal and the onboarding page duplicate their
   auth-mode blocks), tests, docs. Two to four focused days including live
   verification against a GCP project.

Nobody has filed a GitHub issue asking for Vertex; the only in-repo mention is
`docs/design/llm-fallback.md`, whose error classifier already anticipates
"Bedrock and Vertex throttling" because the `google-genai` error shapes are
shared between the Developer API and Vertex.

---

## 1. What Vertex AI offers that the existing providers don't

| Need | Today | With `vertex` |
| --- | --- | --- |
| Gemini under an enterprise GCP contract (billing, IAM, audit logs, data residency, VPC-SC, CMEK) | `google` provider = Gemini Developer API with an AI Studio key; consumer-grade terms | Gemini billed to the customer's GCP project, governed by their IAM |
| Claude without an Anthropic account | Anthropic direct, Azure Foundry, or AWS Bedrock | Claude billed through GCP (Fable 5.1, Opus 5, Sonnet 5, Haiku 4.5 are all listed in Model Garden) |
| Keyless auth on GCP (GKE Workload Identity, Cloud Run service identity) | Not possible for Google models; Bedrock IAM and Azure Entra exist for the other clouds | ADC mode, zero stored secrets |
| Regional / multi-region data residency | n/a for Gemini | `location` = `global`, `us`, `eu`, or a specific region |

Customers already on GCP are the target. For anyone else, the existing
`google` and `anthropic` providers are simpler and cheaper to run.

---

## 2. Verified facts (checked September 2026)

### 2.1 Gemini on Vertex via `google-genai` (already pinned at 1.75.0)

From `.venv/.../google/genai/_api_client.py`:

- `genai.Client(vertexai=True, project=..., location=..., credentials=...)`.
  The SDK is renaming `vertexai` to `enterprise`; `vertexai` is kept as a
  legacy alias and both are accepted.
- `credentials` takes any `google.auth.credentials.Credentials` (so a
  service-account JSON can be turned into one with
  `google.oauth2.service_account.Credentials.from_service_account_info(...,
  scopes=["https://www.googleapis.com/auth/cloud-platform"])`). If omitted,
  the SDK walks ADC (`GOOGLE_APPLICATION_CREDENTIALS`, workload identity,
  gcloud login).
- `location` may be `global` (base URL `https://aiplatform.googleapis.com/`),
  a multi-region (`us` / `eu` → `aiplatform.<loc>.rep.googleapis.com`), or a
  regional id (`<region>-aiplatform.googleapis.com`). Omitting it defaults to
  `global`.
- `genai.Client(vertexai=True, api_key=...)` is **express mode**: a Google
  Cloud API key, no project/location, Gemini only, 90-day trial quotas until
  billing is enabled. Useful for trial signups, not for the enterprise case
  above.
- `http_options=HttpOptions(headers=...)` still works, so the existing custom
  header / identity-forwarding feature (`app/ai/llm/header_injection.py`)
  carries over unchanged.
- The rest of `app/ai/llm/clients/google_client.py` — thinking budget floor,
  `thought_signature` replay, tool-schema scrubbing, `Part.from_uri` for
  images — is model behaviour, not endpoint behaviour, and applies as-is.

### 2.2 Claude on Vertex via the Anthropic SDK (already pinned at 0.40.0)

From `.venv/.../anthropic/lib/vertex/_client.py` and the
[Claude on Google Cloud](https://platform.claude.com/docs/en/build-with-claude/claude-on-vertex-ai)
docs:

- `AnthropicVertex(project_id=, region=, credentials=|access_token=)` and its
  async twin `AsyncAnthropicVertex`. Same `messages.create` surface, so the
  repo's `Anthropic` client (`anthropic_client.py`) only needs an alternate
  constructor path — its streaming, tool-use, image and reasoning handling is
  untouched.
- Request shape differences are handled by the SDK: `model` moves into the
  URL (`.../publishers/anthropic/models/<model>:rawPredict` or
  `:streamRawPredict`) and `anthropic_version: vertex-2023-10-16` goes in the
  body.
- **Pin caveat:** 0.40.0 builds the base URL as
  `https://{region}-aiplatform.googleapis.com/v1` for every region, so
  `region="global"` produces a wrong host. Current SDK main special-cases
  `global`, `us` and `eu`. Either bump the pin (latest is 1.4.0 — a major
  version, and this repo pins `<0.41`, so that is its own change) or pass
  `base_url=` explicitly, which 0.40.0 already accepts. Passing `base_url`
  is the low-risk option.
- Model ids on Vertex: current models are unversioned (`claude-fable-5-1`,
  `claude-opus-5`, `claude-sonnet-5`, `claude-sonnet-4-6`); older ones carry
  an `@date` suffix (`claude-haiku-4-5@20251001`,
  `claude-sonnet-4-5@20250929`). Specific regional endpoints only serve
  Sonnet 4.6 and earlier; newer models need `global`, `us` or `eu`.
- Supported: Messages API, streaming, tool use, thinking, prompt caching,
  structured outputs, 1M context on the 5.x / 4.6+ models. Not supported:
  Files API / URL image sources, Message Batches, server-side tools. None of
  the unsupported items are used by this codebase.
- Claude must be enabled per project in Model Garden, and the caller needs
  the Vertex AI User role (`roles/aiplatform.user`).

### 2.3 OpenAI-compatible endpoint (the "custom provider" shortcut)

Vertex serves `https://aiplatform.googleapis.com/v1/projects/<p>/locations/<l>/endpoints/openapi`
with model ids like `google/gemini-3.5-flash`. Per Google's docs, **only
Google Cloud OAuth is accepted** — the `api_key` field must hold a short-lived
access token minted from `cloud-platform`-scoped credentials. The `custom`
provider stores one static key, so a customer can make it work for about an
hour and then it breaks. Not a shippable path; at most a tip for people who
front Vertex with their own gateway (LiteLLM etc.), which already works today.

### 2.4 Pricing

- Gemini on Vertex global endpoints matches the Gemini Developer API
  per-token prices, so the existing `google` catalog rows
  (`gemini-3.6-flash` $1.50 / $7.50, `gemini-3.5-flash-lite` $0.30 / $2.50,
  `gemini-3.1-pro-preview` $2 / $12 under 200k) can be reused verbatim.
  Note the Developer API price page currently lists an introductory
  $0.75 / $3.75 for 3.6 Flash through 2026-12-31; the catalog carries the
  standard rate, which is the safe choice for cost dashboards.
- Claude on Vertex global endpoints matches Anthropic direct pricing;
  regional and multi-region endpoints add a 10% premium (Sonnet 4.5 and
  newer). The per-model pricing override that already exists
  (`tests/unit/test_llm_pricing_override.py`) lets an admin on a regional
  endpoint correct for that without a catalog change.
- Follow `.agents/skills/add-llm-provider-or-model/SKILL.md`: every id and
  price gets re-verified against the official page at implementation time
  and cited in the PR.

---

## 3. Recommended design

### 3.1 Provider shape

One provider type, `vertex`, display name "Google Cloud Vertex AI".

Credentials schema (`backend/app/schemas/llm_schema.py`), modelled on
`BedrockCredentials` / `AzureCredentials`:

| Field | Storage | Notes |
| --- | --- | --- |
| `project_id` (required) | `additional_config` | GCP project |
| `location` (default `global`) | `additional_config` | `global`, `us`, `eu`, or a region |
| `auth_mode` (default `adc`) | `additional_config` | `adc`, `service_account`, `api_key` |
| `service_account_json` | encrypted `api_key` slot | Whole JSON document; `Text` column, Fernet-encrypted, never echoed |
| `api_key` | encrypted `api_key` slot | Express-mode key; Gemini only |

`adc` uses the environment (workload identity, `GOOGLE_APPLICATION_CREDENTIALS`)
and stores no secret, so `LLM.__init__`'s soft-fail branch for missing
credentials (the one that already exempts Bedrock IAM and Azure Entra) must
also exempt `vertex` + `adc`.

A single shared credentials object should be built once per `LLM` instance
and handed to whichever SDK client is chosen, the same way
`_build_entra_token_provider` works for Azure. `google-auth` refreshes tokens
itself, so long-lived clients stay valid past the one-hour token lifetime.

### 3.2 Dispatch (`backend/app/ai/llm/llm.py`)

```
elif self.provider == "vertex":
    creds = self._build_vertex_credentials(auth_mode, additional_config)
    if _is_anthropic_model_id(self.model_id):
        client = Anthropic(vertex=VertexTarget(project, location, creds), ...)
    else:
        client = Google(vertex=VertexTarget(project, location, creds), ...)
```

`_is_anthropic_model_id` already exists for Azure and matches `claude` /
`anthropic` in the id. `api_key` (express) mode with a Claude id should fail
fast with a clear message rather than reach the Anthropic SDK, which has no
API-key path on Vertex.

Both clients need a constructor branch: `Google` builds
`genai.Client(vertexai=True, project=, location=, credentials=|api_key=,
http_options=...)`; `Anthropic` builds `AnthropicVertex` /
`AsyncAnthropicVertex` with an explicit `base_url` computed for `global` /
`us` / `eu` / regional (see the 0.40.0 caveat above). Everything after
construction is shared with the existing providers.

### 3.3 Catalog

- `LLM_PROVIDER_DETAILS` gains the `vertex` entry; `EDITABLE_MODEL_ID_PROVIDER_TYPES`
  gains `vertex` (Claude ids carry `@date` suffixes and admins enable models
  per project, so ids are admin-owned like Azure deployment names).
- Presets in `LLM_MODEL_DETAILS` with `provider_type: "vertex"`: the three
  Gemini chat rows copied from the `google` presets, plus the Claude rows
  available on Vertex (`claude-sonnet-5` as `is_default`, `claude-haiku-4-5@20251001`
  as `is_small_default`, Fable 5.1 / Opus 5 as opt-in). Preset auto-sync
  (`llm_service.py`) then surfaces them to every org with the provider.
  Keep the Gemini image model (`gemini-*-image`) out of the first cut.
- Because catalog entries are keyed by `provider_type`, the Gemini rows are
  duplicated rather than shared. That is the existing pattern (Azure has no
  presets at all, Bedrock none) and keeps the pricing/vision/context data in
  one file.

### 3.4 Service layer (`backend/app/services/llm_service.py`)

`_set_provider_credentials` gets a `vertex` block next to the Bedrock one:
validate `auth_mode`, copy `project_id` / `location` / `auth_mode` into
`additional_config`, map `service_account_json` or `api_key` into the
encrypted `api_key` slot, and reject a JSON blob that does not parse or lacks
`client_email` / `private_key` before it is stored. `test_connection` works
unchanged once presets exist (it picks the provider's `is_default` catalog
model when the payload carries none).

### 3.5 Frontend

- `frontend/components/LLMProviderModalComponent.vue` and
  `frontend/pages/onboarding/llm.vue` both hand-code the Bedrock and Azure
  auth-mode toggles and the `canTestConnection` rules. `vertex` needs the same
  treatment in both: project id, location (text input with `global` default),
  a three-way auth toggle, a textarea for the service-account JSON, and the
  "uses Application Default Credentials" hint for `adc`.
- `frontend/utils/llmBrand.ts`: add `vertex` to `LlmBrand` mapping to the
  `google` brand as the fallback; model-name-first detection already brands
  Claude models correctly on any host.
- Icon in `frontend/components/LLMProviderIcon.vue`.
- Locale keys in all three catalogs (`locales/{en,es,he}.json`) — the sync
  check fails on key drift.

### 3.6 Tests and CI

- Unit: credential mapping and dispatch (pattern:
  `tests/unit/test_llm_provider_headers.py`, `test_llm_test_connection_schema.py`),
  plus the `global` / `us` / regional base-URL computation for the Anthropic
  path — that is the one place a silent bug would ship.
- Integration: a `vertex-gemini` and `vertex-claude` case in
  `tests/integrations/llm_clients.py` behind env credentials
  (`GOOGLE_APPLICATION_CREDENTIALS` or a JSON blob in `integrations.json`).
  `.github/workflows/e2e-tests.yml` already skips `google` and `bedrock` for
  lack of CI credentials; `vertex` joins that skip list until a CI service
  account exists. Flag it in the PR rather than hide it.
- E2E: a fixture next to `create_bedrock_provider_and_models` in
  `tests/fixtures/llm.py` so the RBAC and catalog-sync suites cover the new
  type.

### 3.7 Docs and release

- docs.bagofwords.com provider page via the `docs-update` skill; README
  provider table (`README.md` ~line 139) gains a Vertex row.
- `CHANGELOG.md` entry and `VERSION` bump via the `release-notes` skill.

---

## 4. Alternatives considered

| Option | Verdict |
| --- | --- |
| **A. One `vertex` provider, family-routed (recommended)** | Matches the Azure precedent, one credential set, one settings card. |
| **B. Two providers, `vertex-gemini` and `vertex-anthropic`** | Simpler dispatch but the admin enters the same project / service account twice; brand detection is already model-first so the split buys nothing in the UI. |
| **C. Extend the existing `google` provider with an "enterprise" toggle** | Tempting because the SDK is the same, but the credential shape (service-account JSON, project, location) and the Claude routing don't fit a provider whose schema is one API key; preset auto-sync would also have to special-case which rows apply. |
| **D. Custom OpenAI-compatible provider pointed at `/endpoints/openapi`** | Breaks after the one-hour OAuth token expiry; no Claude; drops Gemini-specific handling (thinking budget, thought signatures). Not viable. |
| **E. Do nothing, recommend a gateway (LiteLLM / Portkey) in front of Vertex** | Works today through `custom`, but pushes the auth problem onto the customer and loses native reasoning / signature handling for Gemini. Fine as an interim answer for a prospect. |

---

## 5. Risks and open questions

1. **Anthropic SDK pin.** 0.40.0 works with an explicit `base_url`; a bump to
   1.x is a separate, larger change (the 1.x line moved to httpx 2 and has
   its own breaking changes) and should not be bundled into this work.
2. **Express-mode API key.** Confirm on a live key whether Claude is callable
   at all (expected: no) and whether `global` is the only location. If it
   only complicates the form, drop `api_key` mode and ship `adc` +
   `service_account` first.
3. **Thinking budget floor.** The Google client forces `thinking_budget >= 128`
   because newer Gemini models reject 0. Same models on Vertex, so the same
   floor applies; verify once on the live path.
4. **Image URLs.** `Part.from_uri` with `https://` sources is accepted by the
   Developer API; Vertex historically preferred `gs://`. The repo already
   normalises uploads to base64 bytes for other providers, so this is a
   verification item, not a blocker.
5. **Regional pricing premium (Claude +10%).** Catalog carries global prices;
   document the override for regional deployments rather than add a second
   catalog row per model.
6. **Product decision:** whether `claude-sonnet-5` or `gemini-3.6-flash`
   should be the provider's `is_default`. Recommendation: Gemini Flash, since
   it needs no Model Garden enablement and works on every project the moment
   the Vertex API is turned on; Claude requires the extra enablement step.
7. **Error classification.** `app/ai/llm/errors.py` already reads
   `google-genai`'s `.code` and the stringified `429 RESOURCE_EXHAUSTED`
   form, and the Anthropic SDK raises the same classes on Vertex, so retry
   and fallback should work without changes. Verify with one forced 429.

---

## 6. Sources

- Claude on Google Cloud (Anthropic docs): https://platform.claude.com/docs/en/build-with-claude/claude-on-vertex-ai
- Claude models on Agent Platform (Google docs): https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/partner-models/claude
- OpenAI compatibility on Agent Platform: https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/start/openai
- Express mode overview: https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/start/express-mode/overview
- Gemini Developer API pricing: https://ai.google.dev/gemini-api/docs/pricing
- Vertex AI generative pricing: https://cloud.google.com/vertex-ai/generative-ai/pricing
- `google-genai` client source (installed 1.75.0): `.venv/lib/python3.12/site-packages/google/genai/_api_client.py`
- `anthropic` Vertex client source (installed 0.40.0): `.venv/lib/python3.12/site-packages/anthropic/lib/vertex/_client.py`; current main: https://github.com/anthropics/anthropic-sdk-python/blob/main/src/anthropic/lib/vertex/_client.py
