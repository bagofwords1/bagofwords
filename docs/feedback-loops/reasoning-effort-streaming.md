# Feedback loop — effort is ignored and reasoning summaries disappear

Planner keywords must select the same effort for code generation, and provider-supplied summaries must reach the report independently of whether a caller supplied an effort override. This change covers Anthropic, OpenAI Responses, OpenAI-compatible Chat Completions, and the Azure routes that use these adapters. It adds no effort selector.

## Validated causes and changes

- `backend/app/ai/llm/reasoning.py:26`: extracted the existing keyword policy without changing its precedence (completion override → keyword → model default → off). Adaptive configuration now retains explicit low/medium/high; budget fields remain for older adapters. Here **off means no effort override**, as before; it does not promise that a provider will stop reasoning.
- `backend/app/ai/llm/clients/anthropic_client.py:442`: adaptive Claude requests now send effort in `output_config`, not inside `thinking`. Modern default-thinking models request `display: summarized` even without an effort override. Older Claude keeps its manual budget. Model switches remap the configuration for the new model.
- `backend/app/ai/llm/clients/openai_responses_client.py:362`: summary requests no longer depend on an effort override. Supported models get `summary: auto`; explicit effort is forwarded separately. Reported reasoning-token counts survive the stream.
- `backend/app/ai/llm/clients/openai_client.py:452` and `azure_client.py:310`: Chat Completions honors effort on recognized reasoning models and forwards provider-returned `reasoning_content` / `reasoning` strings as separate events. Unknown models get no speculative reasoning parameters.
- `backend/app/ai/agents/coder/coder.py:223`: generation, inspection, CSV transformation, and legacy widget codegen use the selected effort. Summaries are forwarded separately and never concatenated into executable code. The legacy non-streaming path now uses the shared stream.
- `backend/app/ai/agent_v2.py:2991` and `backend/app/streaming/reasoning_streamer.py:6`: each tool gets its own block-scoped summary stream. Existing planner text is preserved; shared database access is locked; summary snapshots and completion persist the combined block text.
- `frontend/pages/reports/[id]/index.vue:368`: after reload, the report prefers the block's combined summary over the planner-only copy. Existing expansion/collapse and progress behavior remain.

Azure/custom deployment aliases can declare the real capability identity in `LLMModel.config.reasoning_model_id` (for example, `gpt-5.2`) while keeping their actual deployment name on the request. Custom providers can opt into Responses through `additional_config.use_responses_api: true`; Chat Completions remains their default. Existing Azure authentication/route selection is unchanged.

## Loop A — deterministic provider and stream contracts

From `backend`, using the installed Python 3.12 environment:

```sh
TESTING=true .venv/bin/python -m pytest --noconftest -q \
  tests/unit/test_reasoning_stream_contract.py \
  tests/unit/test_reasoning_text_streamer.py
```

These tests replace provider SDK request boundaries, not application logic. They exercise request mapping, legacy Claude budgets, aliases, unsupported models, returned summary fields, reasoning usage, coder keyword/override precedence, code/summary separation, parallel block isolation, and retry segments. No API keys or database setup are needed.

**Before:** the initial 18-case adapter reproduction returned **16 failed, 2 passed**. Failures showed absent Anthropic `output_config`, medium replacing low/high, missing custom effort, absent default summaries, and dropped Chat reasoning deltas.

**After:** the final focused suite returned **177 passed**, including the above contracts plus OpenAI reasoning/temperature, Azure route/stream, Anthropic caching, tool-call IDs, coder truncation/time/relative-date/return-trimming regressions. Separate routing/fallback coverage returned **35 passed**. Deprecation warnings remain. This is focused verification, not the entire backend suite.

## Loop B — bounded live API confirmation

With `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` supplied through the process environment, from `backend`:

```sh
.venv/bin/python ../tools/agent/verify_reasoning_live.py openai
.venv/bin/python ../tools/agent/verify_reasoning_live.py anthropic
```

The harness uses a synthetic arithmetic task, caps each response at 3,072 output tokens and each request at 90 seconds, and prints only settings, lengths, timings, correctness, and usage. It never prints keys, headers, summary text, or answer text. No customer inputs or remote environment are involved.

Observed on 2026-09-22:

| Client / model | Effort | Summary characters | Reported reasoning tokens | Correct answer | Seconds |
|---|---|---:|---:|---|---:|
| Anthropic / claude-sonnet-5 | low | 350 | unavailable | yes | 4.95 |
| Anthropic / claude-sonnet-5 | high | 369 | unavailable | yes | 5.82 |
| Responses / gpt-5-mini | low | 0 | 576 | yes | 10.83 |
| Responses / gpt-5-mini | high | 2,308 | 1,856 | yes | 36.64 |
| Chat Completions / gpt-5-mini | low | 0 | 512 | yes | 11.68 |
| Chat Completions / gpt-5-mini | high | 0 | 1,856 | yes | 30.11 |

All six calls succeeded and sent the selected effort. The OpenAI key tested the Chat adapter against OpenAI's own endpoint, not a third-party gateway. Azure and custom gateway compatibility were tested at the SDK boundary, **not live against an Azure deployment or external gateway**. Summary visibility does not guarantee a speedup or a summary on every request. Anthropic's zero in the shared usage object remains “not separately reported,” not evidence of zero thinking.

## Loop C — report streaming and reload

Start a local Nuxt development server on port 3100, then run from the repo root:

```sh
SHOT=before node tools/agent/verify_reasoning_ui.cjs
FLOW=1 node tools/agent/verify_reasoning_ui.cjs
```

The first command temporarily restores only the old report summary expression and restores the working file in `finally`. The second tests the changed expression, feeds synthetic token events through the real report handler, reloads the page, and verifies both planner and coder text. API responses are synthetic fixtures. This proves frontend event handling and rehydration of the combined-summary response shape; it is not a full authenticated agent/database end-to-end run. The pure stream test separately verifies the persisted snapshot callback and block isolation.

Both browser scenarios passed with **zero browser errors**. Evidence is in `media/pr/reasoning-effort-streaming/{before.png,after.png,flow.gif}`. Browser setup initially used the wrong local auth cookie and intercepted non-API module URLs; correcting the fixture resolved those setup failures. Expanded coder tests initially omitted a required context field / builder; the fixture was corrected and the final selected suites passed.

## Boundaries

- No raw private chain of thought is reconstructed; only text supplied by the provider is displayed.
- No latency benchmark or reproduction of a long production run is claimed.
- Existing planner `thinking_ms` timing semantics are unchanged; this change does not turn that metric into an exact measure of private reasoning time.
- No new provider/model catalog entries, database migration, or effort UI selector.
- No customer environment changes, deployments, or saved credentials.

API references: [OpenAI reasoning summaries](https://developers.openai.com/api/docs/guides/reasoning), [Anthropic effort](https://platform.claude.com/docs/en/build-with-claude/effort), [Anthropic thinking controls](https://platform.claude.com/docs/en/build-with-claude/thinking-steering-and-cost).
