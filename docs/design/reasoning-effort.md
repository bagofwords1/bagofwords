# Reasoning effort

Status: **implemented** (branch `claude/effort-level-research-4e9yuf`).

Users pick how hard the model thinks — **Default · Low · Medium · High · Max** —
right in the model picker. One normalized level travels with the model choice;
each provider client translates it into what that model's API accepts, so a
pick never becomes a 400. **Default sends nothing new**: cost and latency are
unchanged unless someone chooses a level.

## Levels and what they run as

The backend vocabulary is `none · minimal · low · medium · high · xhigh · max`
(`app/utils/reasoning_effort.py`). The product offers `low · medium · high ·
max`; "max" means *the strongest this model has* (max, else xhigh, else its top
level). Any other level snaps to the nearest accepted one
(`clamp_effort`, `app/ai/llm/reasoning.py`).

Per-model capability lives in one table (`_FAMILIES` in `reasoning.py`),
matched on the model id after stripping gateway / Bedrock / Vertex prefixes.
Every row below was verified live (Sep 2026) unless marked.

| Model family | Accepted efforts | Max runs as |
|---|---|---|
| gpt-6-astra | low – max | max |
| gpt-6-sol / luna, gpt-5.6-* | none, low – max | max (xhigh on Chat Completions) |
| gpt-5.2 – 5.5, gpt-5.4-mini | none, low – xhigh | xhigh |
| gpt-5.1 | none, low – high | high |
| gpt-5 | minimal – high | high |
| o1 / o3 / o4 | low – high | high |
| Claude Sonnet 5, Opus 4.7+, Fable, Mythos | low – max (adaptive) | max |
| Claude Opus / Sonnet 4.6 | low, medium, high, max (no xhigh) | max |
| Claude ≤ 4.5 (incl. Haiku 4.5) | token budget 1,024 / 5,000 / 15,000 / 31,999 | 31,999 budget |
| Gemini 3.x (verified on Vertex) | low – high (`thinking_level`) | high |
| Gemini 2.5 (verified on Vertex) | token budget (capped 24,576) | 24,576 budget |
| gpt-4.x, gpt-image, unknown ids | — (no effort) | — |

## Provider translation

| Client | Request shape |
|---|---|
| OpenAI Responses (`openai_responses_client.py`) | `reasoning: {effort, summary}` |
| Chat Completions (`openai_client.py`, `azure_client.py`, via `chat_effort.py`) | `reasoning_effort`; no `max` on this API. If the endpoint rejects tools + effort, the call is retried once with `none` and logged. |
| Anthropic (`anthropic_client.py`) | adaptive: `thinking: {type: adaptive, display: summarized}` + `output_config.effort`; ≤4.5: `thinking.budget_tokens` |
| Bedrock (`bedrock_client.py`) | `additionalModelRequestFields: {thinking: {type: adaptive}, output_config: {effort}}` — effort **beside** thinking; ≤4.5 keeps a budget |
| Gemini (`google_client.py`) | 3.x `thinking_level`, 2.5 `thinking_budget`, never both |

Routing: GPT-5.6 / GPT-6 go through the **Responses API** on OpenAI gateways
(custom base URL) and on Azure with key auth — on Chat Completions their
function tools only work at effort `none`, and GPT-6 fails there even at its
default (`needs_responses_for_tools`, `app/ai/llm/llm.py`).

## Admin settings per model

On the model card (`LLMModelReasoningSection.vue` →
`POST /api/llm/models/{id}/reasoning`, `manage_llm`), stored in
`LLMModel.config`:

| Mode (`reasoning_mode`) | Meaning |
|---|---|
| `auto` (default) | capability from the model id |
| `like` + `reasoning_model_id` | an opaque deployment / ARN / alias that behaves like a known model |
| `generic` | an OpenAI-compatible endpoint that takes `reasoning_effort` low/medium/high |
| `custom` | send only the admin's raw fields |
| `off` | never send reasoning parameters |

- `reasoning_effort` — the model's default level (used when the user leaves Default).
- `reasoning_params` — **raw request fields per level**, JSON merged into the
  provider request when that level runs (`merge_raw_params`): keys the request
  already sets are deep-merged, others pass through (`extra_body` for SDK
  clients, the body itself for Anthropic/Bedrock, `GenerateContentConfig`
  names for Gemini). Keys that would replace the conversation, tools or model
  (`model`, `messages`, `input`, `tools`, `system`, …) are rejected; ≤ 8 KB.
- **Test** (`POST /api/llm/models/{id}/test_reasoning`) sends one
  tool-calling request at a level with the saved settings and reports what it
  ran as, which API served it and reasoning tokens — or the provider's own
  error text.
- **Test Connection** on a provider now also sends a tool-calling request:
  a plain text reply passed for endpoints that then failed every agent turn.

## Where the level is stored

Effort is stored wherever a model override is stored, as a pair
(null = Default):

| Surface | Model | Effort |
|---|---|---|
| Report (prompt box, sticky) | `reports.model_id` | `reports.reasoning_effort` |
| Each turn (audit) | `completions.prompt.model_id` | `completions.prompt.reasoning_effort` |
| Scheduled tasks | `scheduled_prompts.prompt` JSON | same JSON |
| Triggers / webhooks | `webhooks.model_id` | `webhooks.reasoning_effort` |
| Saved prompts | `prompts.model_id` | `prompts.reasoning_effort` |
| Eval test cases | `prompt_json.model_id` | `prompt_json.reasoning_effort` |
| Admin model default | — | `llm_models.config.reasoning_effort` |

Migration `reasoneffort01` adds the three columns. Values are validated on
write (`normalize_effort`); stored prompt JSON is normalized too.

## Resolution at run time

`turn level > report level > trigger words ("think hard", …) > model default > off`
— the report level is written onto the turn's prompt when the turn has none
(`CompletionService._inherit_report_reasoning_effort`); the agent resolves the
rest (`AgentV2`, `_resolve_reasoning_effort`). After an auto-route or fallback
swap, the level is re-mapped for the new model. Side calls (titles, judge,
compaction, classifiers) never inherit the user's level.

## UI

`components/prompt/ModelPickerPanel.vue` — one picker for the prompt box,
scheduled tasks, triggers, saved prompts and eval cases: search, models
sorted by provider, keyboard navigation, and an effort
bar for the selected model with "runs as …" hints. The trigger button shows the
level as a badge. Strings are in every locale catalog (`prompt.effort.*`,
`settings.llms.reasoning.*`).

## Known limits / follow-ups

- Gemini on Vertex was verified live (3.1 Pro, 3.6 / 3.8 Flash, 2.5 Pro / Flash:
  every level accepted). Claude on Vertex was not: the test project had no
  quota for it (429). Gemini through the Google AI API was not live-tested.
- Reasoning token counts are recorded for OpenAI/Azure only (Anthropic folds
  thinking into output tokens; Google/Bedrock not read yet).
- Named presets ("Analyst – Deep") that pair a base model with a level would
  be a separate table resolving to `{model_id, effort}` — not model rows.
- Chat Completions routes that reject tools + effort fall back to `none`; the
  admin can switch the provider to the Responses API to honor the level.
