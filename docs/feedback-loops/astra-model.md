# Feedback Loop — Enable GPT-6 Astra

Add Astra as the OpenAI default model and verify all three inference surfaces
with the existing SDK. GPT-5.6 Luna remains the small-default model.

## Root cause (validated)

Before this change, `backend/app/ai/llm/clients/openai_responses_client.py:133`
and `:148` always sent temperature on Chat Completions, including Astra, which
rejects sampling parameters. The Responses checks at `:319` and `:345` only
recognized GPT-5 and o-series, so configured temperature leaked into Astra
requests and requested thinking settings were ignored.
`backend/app/ai/llm/llm.py:270` routed every OpenAI base URL override to Chat
Completions, although Astra tool calling requires Responses.

## Loop A — deterministic reproduction

From `backend`, with Python 3.12 and the backend dependencies installed:

```sh
BOW_DATABASE_URL=sqlite:///db/app.db TESTING=true PYTHONPATH=. python -m pytest \
  --confcutdir=tests/unit tests/unit/test_openai_reasoning_requests.py -q
```

The original client failed all 18 request-parameter cases: unexpected temperature
on sync/streaming requests and missing reasoning on Responses requests. After
the fix, all 18 pass. Two additional routing cases verify native and overridden
OpenAI endpoints select Responses and preserve the endpoint. The dated ID in the
unit test is a synthetic compatibility case, not an advertised model snapshot.

Broader focused verification:

```sh
BOW_DATABASE_URL=sqlite:///db/app.db TESTING=true PYTHONPATH=. python -m pytest \
  --confcutdir=tests/unit \
  tests/unit/test_openai_reasoning_requests.py \
  tests/unit/test_openai_client_temperature.py \
  tests/unit/test_llm_test_connection_schema.py \
  tests/unit/test_openai_family_image_attach.py \
  tests/unit/test_openai_tool_call_ids.py -q
```

Observed: **56 passed**, with existing deprecation warnings. SDK boundaries are
stubbed; the tests call the public inference methods. No database is needed.
Catalog validation also passed: 17 unique model IDs, positive prices, and no
provider with multiple default or small-default flags.

## Loop B — live confirmation

```sh
BOW_DATABASE_URL=sqlite:///db/app.db TESTING=true PYTHONPATH=. \
  python tests/integrations/astra_smoke.py
```

Supply `OPENAI_API_KEY` through the environment, or use the script's non-echoing
terminal prompt. Never put a key in this document or committed configuration.
The script sends synthetic prompts and uses a configured temperature override
to verify it is suppressed. Observed on 2026-09-05 against api.openai.com:

```text
PASS plain inference
PASS streaming inference (connection-test path)
PASS Responses reasoning + tool-result round trip
```

The tool loop retrieves a synthetic code through a function call, replays its
result through the application's message translator, and verifies the final
answer contains that code. Live gateway behavior was not tested.

## The fix

- Catalog: `gpt-6-astra`, text/image input, 1,050,000 context, 128,000 max output,
  Standard base prices $10 input / $50 output per million tokens.
- GPT-6 requests omit temperature on all three inference paths; Responses honors
  the existing low/medium/high thinking mapping.
- OpenAI providers using GPT-6 select Responses even with a base URL override.
- `llm_service.py` already syncs catalog additions on model listing; no migration
  or service change is required.

Sources checked before implementation:
[model specification and pricing](https://developers.openai.com/api/docs/models/gpt-6-astra),
[migration requirements](https://developers.openai.com/api/docs/guides/latest-model).

## Limits of verification

The live calls verify the client and API access, not a full browser flow or a
persisted console usage row. The full-stack boot was attempted but dependency
installation failed on network/DNS access to files.pythonhosted.org. Tests used
the existing local Python environment with the worktree on PYTHONPATH.

Catalog costs remain base-rate estimates under the existing accounting model;
this change does not implement Astra's cache-write pricing or the multipliers
for prompts over 272K input tokens. Async tools and mid-turn steering are outside
this enablement change.
