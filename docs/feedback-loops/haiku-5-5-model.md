# Claude Haiku 5.5 preset model

## Change

- `backend/app/models/llm_model.py` — new Anthropic preset `claude-haiku-5-5`:
  1M context, 128K output, vision, $0.10 / $0.50 per MTok (≤100K-token prompt
  tier; $0.50 / $2.50 above is not modelled). Not default / small default.
- `backend/app/ai/llm/reasoning.py` — `claude-haiku-5` joins the adaptive
  family (efforts low..max), gets adaptive thinking instead of `budget_tokens`,
  and "off" sends `thinking: disabled` with `effort: low` (disabled is only
  accepted at high or below).
- `backend/app/ai/llm/clients/anthropic_client.py` — `haiku-5` added to
  `_NO_SAMPLING_PARAM_TAGS` (non-default `temperature` is a 400).
- Cache pricing: standard 0.1x read / 1.25x 5m write / 2x 1h write — no override.
- Frontend: `claude-haiku-5-5` added to the "behaves like" suggestions.

## Why the client changes are required (live API, before the fix)

```
thinking {type: enabled, budget_tokens: 1024} -> 400 "thinking.type.enabled" is not supported for this model
temperature: 0                                -> 400 `temperature` is deprecated for this model.
thinking {type: disabled} + effort low         -> 200
```

## Sandbox loop (real backend, real Anthropic API)

1. Boot backend on `db/sandbox.db`, seed org + Music Store demo via
   `tools/agent/seed_org.py`.
2. `GET /api/llm/available_models` lists Claude Haiku 5.5 with 0.10 / 0.50,
   1,000,000 ctx, vision.
3. Create an Anthropic provider with only Haiku 5.5, set default + small default.
4. `POST /api/llm/models/{id}/test_reasoning` for off / low / medium / high / max:
   all 200 "OK" against the live API.
5. Report on Music Store, prompt "customers per country, top 5": completion
   `success`, `model=claude-haiku-5-5`, table produced; 10/10 Anthropic calls 200.
6. `llm_usage_records` cost math matches the catalog exactly, e.g. planner
   turn 2: 978 in × $0.10 + 60,673 cache-read × $0.01 + 4,142 5m-write × $0.125
   = $0.00122228; 1h writes bill at $0.20/MTok (2x).

Unit: `backend/tests/unit/test_haiku_5_5_support.py`.

Source: Anthropic model docs (Claude Haiku 5.5: `claude-haiku-5-5`, 1M / 128K,
$0.10 / $0.50 ≤100K prompt, cache 0.1x / 1.25x / 2x, adaptive thinking only,
effort low–max default medium, non-default sampling params 400) —
https://platform.claude.com/docs/en/about-claude/pricing
