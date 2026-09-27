# User-selectable reasoning effort — requirements map

Status: **research / planning only** (nothing implemented). Date: 2026-09-27.

Goal: let a user pick how hard the model thinks (e.g. *Fast · Balanced · Deep*)
from the prompt box model selector and the other model-picking surfaces, with
one normalized setting that every provider adapter translates correctly.

## TL;DR

- **The backend is ~70% there.** A normalized effort (`off|low|medium|high`)
  already exists end-to-end: `PromptSchema.reasoning_effort`
  (`app/schemas/completion_v2_schema.py:71`) → `AgentV2` resolution
  (`app/ai/agent_v2.py:4298-4327`) → `thinking` dict → planner + coder →
  `inference_stream_v2(thinking=…)` on every client. Resolution order today:
  per-completion → trigger words ("think hard", "ultrathink" …) →
  `LLMModel.config["reasoning_effort"]` → `off` (`app/ai/llm/reasoning.py:62`).
- **Nothing in the frontend sends it.** No UI, no persistence, no i18n.
- **Adapters are inconsistent** — the same "effort" means different things per
  provider, and two adapters are wrong (Bedrock, Google). See the matrix below.
- **No capability data.** The catalog doesn't know which models reason, which
  levels they accept, or what their default is — so the UI can't know when to
  show the control or which options to offer.
- **"off" is not off.** On OpenAI reasoning models we send nothing, so "off"
  becomes the provider default (often `medium`); on always-thinking Claude we
  send `low`; on Gemini 3 thinking can't be disabled at all.

## 1. Provider landscape (what we must translate to)

Three API shapes exist; we already have the abstraction to cover all of them.

| Provider / path | Parameter | Levels | "No thinking"? | Gotchas |
|---|---|---|---|---|
| OpenAI Responses | `reasoning: {effort, summary}` | `none·minimal·low·medium·high·xhigh·max`, **per model** (gpt-5: minimal–high; 5.1: none default; 5.5: none–xhigh, default medium; 5.6: none–max; gpt-6-astra: low–max, `none` → 400) | Only where `none` is supported | temperature/top_p must be dropped; reasoning counts toward `max_output_tokens`; `text.verbosity` separate knob |
| OpenAI Chat Completions (custom base_url, Foundry) | top-level `reasoning_effort` | same | same | gpt-5.6 / gpt-6-sol/luna: function tools only work at `none` on this endpoint → Responses API required for agent use |
| Azure OpenAI | same as OpenAI (v1 endpoint) | same; `gpt-5-pro` = `high` only | same | gpt-5.6-sol + tools on chat.completions fails even with effort omitted |
| Anthropic (≤4.5) | `thinking: {type: enabled, budget_tokens ≥1024 < max_tokens}` | budget | yes (omit) | temperature disallowed while thinking |
| Anthropic (4.6+, 5.x) | `thinking: {type: adaptive}` + `output_config: {effort}` | `low·medium·high·xhigh·max` (xhigh: Opus 4.7+/Sonnet 5/Fable; max: 4.6+) | **No** on Sonnet 5 / Opus 4.7+ / Fable 5 (adaptive-only, always on) | `enabled` → 400 on 4.7+; sampling params 400 on newest models; thinking signatures must round-trip in tool loops; changing effort invalidates message cache |
| Bedrock (Claude) | `additionalModelRequestFields: {thinking, output_config: {effort}}` | as Anthropic | as Anthropic (explicit `{"type":"disabled"}` needed on Opus/Sonnet 5) | effort must be **outside** `thinking` |
| Vertex (Claude / Gemini) | Anthropic body / Gemini `thinkingConfig` | as those | as those | — |
| Gemini 3+ | `thinkingConfig.thinkingLevel` | `minimal·low·medium·high`, per model (3.8 Flash no `minimal`; 3.1 Pro low–high; 3-pro-preview low/high) | **No** (minimal ≠ off) | sending level **and** budget → 400; thought signatures must round-trip |
| Gemini 2.5 | `thinkingConfig.thinkingBudget` | 0/‑1/N (Pro 128–32768, can't disable) | Flash only (`0`) | — |
| Ollama (via custom) | top-level `think` | `true·false·low·medium·high` (model-dependent) | yes | only reachable via OpenAI-compat `reasoning_effort` today |
| xAI / Groq / Mistral / DeepSeek (via custom) | `reasoning_effort` (+ DeepSeek `thinking`) | small, provider-specific sets | varies | DeepSeek/Mistral require reasoning content to be replayed in tool loops |

Industry practice observed in open-source gateways and chat UIs:

- **Gateways** normalize a single `reasoning_effort` string and translate per
  provider using a **per-model capability map** (`supports_reasoning`,
  per-level `supports_<level>_reasoning_effort`, `default_reasoning_effort`,
  `supports_adaptive_thinking`, `thinking_always_on`). Budget fallback for
  budget-only models: low 1024 · medium 2048 · high 4096 · xhigh 8192 ·
  max 16384 (ours: 1024 / 5000 / 15000). Unsupported levels either error or
  are dropped silently behind a "drop unsupported params" flag.
  An alternative scheme scales the budget by `max_tokens` (max 0.95, high 0.8,
  medium 0.5, low 0.2, minimal 0.1) clamped to [1024, 128000].
- **Chat UIs** expose it as a per-model "advanced param" (admin default) that
  users can override per chat in a side controls panel — commonly a **free-text
  field with no validation**, and **no control in the chat input bar**. That is
  the weak spot we should do better on: a validated, model-aware picker inline
  with the model selector.

## 2. Current code — what exists vs. gaps

### 2.1 Normalization core — `app/ai/llm/reasoning.py`
- Exists: levels `off|low|medium|high`; `_effort_to_thinking_config`
  (adaptive vs budget by **substring model-id match**, :37-43);
  `selected_effort`; `is_openai_reasoning_model` (prefix match).
- Gaps:
  - No `minimal`, `xhigh`, `max`; no `none` distinct from "unset".
  - Capability detection is hardcoded substrings/prefixes, not catalog data;
    breaks for opaque Azure deployment names unless `reasoning_model_id` is set.
  - `PromptSchema.reasoning_effort` is an unvalidated `str`; comment still says
    "Anthropic only" (`completion_v2_schema.py:68-70`, also `clients/base.py:49`).

### 2.2 Adapters — `app/ai/llm/clients/`
| Client | Today | Needed |
|---|---|---|
| `openai_responses_client.py:370-379` | sends `reasoning.effort` when set, summary auto | send explicit `none` for "off" where supported; per-model level clamp; optional `text.verbosity` |
| `openai_client.py:450-457` (chat.completions) | `reasoning_effort` when set | same + legacy `_build_chat_params` hardcodes `medium` for o1/o3 (:143) |
| `azure_client.py:310-314` | `reasoning_effort` when set | same |
| `anthropic_client.py:433-470` | adaptive + `output_config.effort` or budget; forces `low` for always-thinking when off | add xhigh/max; forward `display`; verify signature replay in multi-turn (`_translate_messages` :305 does not appear to replay `ThinkingPart`) |
| `bedrock_client.py:463-470` | **always** `{type: enabled, budget_tokens}`; ignores effort/adaptive | **bug**: adaptive-only Claude on Bedrock → 400 when effort set. Must mirror the Anthropic mapping with `output_config` beside `thinking` |
| `google_client.py:296-315` | uses `budget_tokens` only (adaptive → 1024) | **bug-ish**: Gemini 3 should get `thinking_level`; 2.5 keeps budget; never send both |
| custom / Ollama (via `OpenAi`) | `reasoning_effort` only if model id matches OpenAI prefixes | needs catalog/admin flag so custom models (Ollama, DeepSeek, xAI, Groq) can opt in |

Non-v2 paths (`LLM.inference` / `inference_stream`, `llm.py:723/804`) take no
effort at all; Google hardcodes budget 128 there.

### 2.3 Where effort flows / doesn't

Flows (planner + coder): `agent_v2.py:4298` → `planner_v3.py:205` ;
`coder.py:278-285` via `create_data`, `create_widget`, `write_csv`,
`inspect_data`. Re-mapped on routing/fallback (`agent_v2.py:3320-3334`).

Does **not** flow (always "off" → provider default / forced low):
artifact create/edit, `add_parameter`, `create_dashboard`, viz inference in
`create_data`, MCP tools, reporter/title, judge, suggest-instructions,
compaction, classifiers. Decision needed: most of these should stay on a fixed
low/off (cheap, latency-sensitive); artifacts arguably should inherit.

Entry points that build `PromptSchema` without effort (fall back to model
default): Slack/Teams (`external_platform_manager.py:775`), webhooks
(`webhook_service.py:671`), machine turns (`machine_turn.py:88`), evals
(`test_run_service.py:910/1036/1672`), queued completions
(`completion_service.py:3322`), scheduled prompts, triggers, saved prompts,
artifact chat (`routes/artifact_chat.py:123`).

### 2.4 Catalog & settings
- `LLM_MODEL_DETAILS` (`app/models/llm_model.py:16-398`): no reasoning fields.
- `LLMModel.config` JSON already holds `reasoning_effort` (model default) and
  `reasoning_model_id`, but no dedicated endpoint (only generic
  `PATCH /llm/models/{id}`); pattern to copy: `set_temperature`
  (`routes/llm.py:239`), `set_routing_hint` (:303).
- No org-level or per-user default. Patterns: `FeatureConfig` in
  `organization_settings_schema.py:362`; `Membership.default_llm_model_id`.
- Report persists `model_id` and `mode` but not effort.

### 2.5 Accounting & display
- `reasoning_tokens` recorded for OpenAI/Azure only; Anthropic, Google
  (`thoughts_token_count`), Bedrock not populated.
- Reasoning already streams and renders (thinking box, "Thought for Ns") —
  no UI work needed there.

### 2.6 Frontend surfaces
Primary: `components/prompt/PromptBoxV2.vue` model popover (:515-550; state
:1105-1162; `persistModel` :1268 → `PUT /reports/{id}`; payload
`buildSubmitPayload` :1402; `createReport` :1740; `defineExpose` :1683) and
`pages/reports/[id]/index.vue` (`onSubmitCompletion` :4889, `onQueuePrompt`
:4739, route-query hydration :5489).

Secondary (each stores its own `model_id` and would need effort alongside):
`ScheduledPromptModal.vue`, `automations/TriggersTab.vue` (`readRunSpec`
:725), saved prompts (`ModelSelector.vue` + `PromptEditModal.vue`),
evals (`monitoring/TestPromptBox.vue`, `TestCaseEditor.vue` `prompt_json`),
personal default (`UserProfileModal.vue`), share "chat model for viewers"
(`ShareModal.vue`), admin per-model card (`LLMModelCardModal.vue` — home for
the model default effort), `pages/index.vue` / `reports/new.vue` (new report).

Reusable UI: popover rows with title + hint + check (Auto row :527-537, mode
popover :297-325), section divider (:536). i18n under `prompt.*` in
`locales/*.json` (all catalogs must stay in sync).

## 3. Proposed design (for discussion)

### 3.1 Normalized levels
Keep one internal enum, widen it:
`off · minimal · low · medium · high · xhigh · max` (+ `auto` = "use model/org
default", i.e. unset). **Expose only 3–4 to users** — e.g. *Auto · Fast (low)
· Balanced (medium) · Deep (high)*, with `xhigh/max` gated behind an admin
setting (cost). Map to the nearest supported level per model (clamp, never
400) and log the effective value.

### 3.2 Capability data (single source of truth)
Add to catalog entries and expose on `/api/llm/models`:
```
reasoning: {
  supported: bool,
  levels: ["low","medium","high",...],   # what the API accepts
  default: "medium" | null,               # provider default when omitted
  can_disable: bool,                      # false for always-thinking models
  mode: "effort" | "budget" | "level" | "toggle"
}
```
Custom/Azure/Ollama models: admin-editable override in `LLMModel.config`
(like `*_override` columns). Replace substring matching in `reasoning.py` with
this lookup (keep substring as fallback for unknown ids).

### 3.3 Resolution order
`prompt.reasoning_effort` > trigger words > report/session effort > user
default > model default (`LLMModel.config`) > org default > `auto`. Explicit
choice is never overridden by triggers (today triggers only apply when unset —
keep). Auto-router: effort is per-run, clamp after a route/fallback swap
(already re-mapped at `agent_v2.py:3320`).

### 3.4 Backend work items
1. `reasoning.py`: widen enum, capability lookup, `clamp(effort, model)`;
   make "off" explicit (`none` for OpenAI where supported, `disabled` where
   Anthropic allows, `low`/`minimal` where it can't be disabled).
2. Schemas: `Literal[...]` validation on `PromptSchema.reasoning_effort`;
   fix stale comments; add effort to report (`reports.reasoning_effort`
   column, alembic migration) like `model_id`/`mode`.
3. Adapters: fix Bedrock (adaptive + `output_config`), Google (`thinking_level`
   for 3.x), OpenAI "off", custom-provider opt-in, xhigh/max passthrough.
4. Carry effort through scheduled prompts, triggers run spec, saved prompts,
   eval `prompt_json`, Slack/Teams/webhooks (use defaults).
5. Model default endpoint `POST /llm/models/{id}/set_reasoning_effort`
   (+ capability override) and optional org default in org settings.
6. Usage: populate `reasoning_tokens` for Anthropic (not separable — leave),
   Google (`thoughts_token_count`), Bedrock.
7. Tests: extend `tests/unit/test_reasoning_off_effort.py`,
   `test_openai_reasoning_requests.py`; add per-adapter request-shape tests
   for every level × provider family (follow `backend/tests/AGENTS.md`).

### 3.5 Frontend work items
1. PromptBoxV2: effort section **inside the model popover** under a divider
   (keeps the input bar uncluttered; follows the Auto-row visual), hidden when
   `reasoning.supported` is false, options filtered to model's levels. Show a
   small badge on the model button when not Auto (e.g. "Deep").
   Persist via `PUT /reports/{id}` like model; include in submit/queue
   payload, `createReport`, route query, estimate call, `defineExpose.getEffort()`.
2. Re-validate effort when the model changes (clamp or reset to Auto).
3. Secondary surfaces: ScheduledPromptModal, TriggersTab, PromptEditModal,
   TestPromptBox / TestCaseEditor, UserProfileModal (personal default),
   LLMModelCardModal (admin model default + capability override), optional
   ShareModal.
4. i18n keys in all locale catalogs; hints describe trade-off (speed/cost vs
   depth), not provider jargon.

## 4. Risks / open questions
- **Cost**: high/xhigh multiplies output tokens; quotas (`my-quota`) and
  per-role gating may be needed — should effort be an RBAC-limited option?
- **Latency on tool loops**: effort applies to every planner step; consider
  "Deep" = high on first planner step, medium thereafter?
- **Cache**: changing effort mid-report invalidates Anthropic message cache
  (and OpenAI in some modes) — acceptable, but don't auto-oscillate.
- **Signature replay**: confirm multi-turn thinking round-trip for Anthropic /
  Gemini / Bedrock before raising default efforts — otherwise tool loops 400.
- **Trigger words**: keep? They silently override "Auto" to high today.
- **Legacy paths** (`inference`, `inference_stream`): leave at fixed low or migrate?
- **Evals**: effort should be part of test case config so scores are reproducible.
