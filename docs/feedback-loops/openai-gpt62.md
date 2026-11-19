# GPT-6.2 OpenAI defaults, reasoning and usage costs

Before the change, neither GPT-6.2 ID appeared in the OpenAI catalog. Both fell through to the generic GPT-6 reasoning rule and OpenAI cache rate. Querying the capability and pricing helpers for each ID returned an effort list containing `none` and a cached-input multiplier of `0.5`. Both APIs require at least `low` and bill cache reads at 5% of standard input.

## Local verification

From the backend directory, run the following against the isolated SQLite test database.

```sh
TESTING=true BOW_DATABASE_URL=sqlite:///db/app.db uv run pytest -q \
  tests/e2e/test_openai_gpt62_usage.py \
  tests/e2e/test_llm_providers.py::test_preset_openai_sync_migrates_to_gpt_56_models \
  tests/unit/test_reasoning_off_effort.py
```

The catalog sync test checks that a managed OpenAI provider promotes Sol and selects Luna for small tasks. The billing test creates a provider through the HTTP API, runs each model through the streaming LLM wrapper, serves an SSE response at the HTTP transport boundary, and reads the committed usage record. At 272,000 and 272,001 input tokens it checks mixed cached reads, 30-minute and 12-hour writes, and the output rate. The effort tests check the lightest supported setting on both Responses and Chat Completions.

The relevant code is in `backend/app/ai/llm/reasoning.py:129` for accepted efforts, `backend/app/ai/llm/pricing.py:245` for context and cache pricing, and `backend/app/ai/llm/clients/openai_client.py:345` for SDK usage decoding.

## Live verification

With a valid OpenAI test key exported as `OPENAI_API_KEY`, run from the backend directory.

```sh
for model in gpt-6.2-sol gpt-6.2-astra; do
  BOW_DATABASE_URL=sqlite:///db/app.db PYTHONPATH=. OPENAI_SMOKE_MODEL="$model" \
    uv run python tests/integrations/astra_smoke.py || exit 1
done
```

The smoke script exercises plain inference, streaming text and a two-turn Responses function call with a synthetic tool result. On November 18, 2026, both models passed all three checks against OpenAI. Each model also passed the authenticated BOW Settings connection endpoint’s text and tools checks. A separate live Responses call on each model produced a committed usage row in the isolated test database. Input and output costs matched the API token counts at the catalog rates. The boundary and cache-write cases above use synthetic provider usage.

[Sol specification](https://developers.openai.com/api/docs/models/gpt-6.2-sol) · [Astra specification](https://developers.openai.com/api/docs/models/gpt-6.2-astra) · [Prompt caching and usage fields](https://developers.openai.com/api/docs/guides/prompt-caching)
