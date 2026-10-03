# Feedback Loop — reasoning off must reach the provider

Selecting off must produce an explicit disable request where supported. An unspecified adapter setting remains distinct from off. This backend-only change does not claim a measured latency improvement.

## Root cause (validated)

At base `8d25044c5`, `backend/app/ai/llm/reasoning.py` converted both off and unspecified effort to `None` in `_effort_to_thinking_config` and `selected_effort`. OpenAI adapters compensated by selecting the minimum for every unspecified request. `backend/app/ai/llm/clients/anthropic_client.py` inferred thinking defaults from temperature support and forced adaptive low even on models that accept disabled. Custom settings rejected/discarded off mappings.

Planner/coder precedence remains per-completion > prompt trigger > model default > BOW's off default. Passing `thinking=None` directly to an adapter now preserves provider defaults.

## Loop A — deterministic reproduction

Use Python 3.12 and backend dependencies (`uv sync --frozen --extra dev`). Tests replace provider SDK calls only. No network, credentials, DB or running app is needed; `--noconftest` avoids unrelated database migration fixtures.

```sh
cd backend
TESTING=true BOW_DATABASE_URL=sqlite:///db/app.db PYTHONPATH=. uv run pytest --noconftest \
  tests/unit/test_reasoning_effort_levels.py tests/unit/test_reasoning_stream_contract.py \
  -k 'explicit_off or sonnet_55_off or bedrock_off or custom_off or google_off or opaque_deployments or no_effort_preserves or reasoning_settings_accept_off' \
  -q --tb=short --disable-warnings
```

With these regression tests and production modules restored to base `8d25044c5`: **16 failed, 25 passed, 220 deselected**. Failures cover Claude disable modes, Bedrock, custom off mapping, parameter suppression, schema validation, and default-vs-off on Responses/Chat/Azure. OpenAI explicit off already passes on that base: a minimum-effort fix landed during the investigation.

With the fix: **41 passed, 220 deselected**.

## Fix

- Preserve off as `{"type":"disabled"}` internally; unspecified remains `None`.
- OpenAI Responses/Chat/Azure: explicit off selects none or the existing minimum supported effort (with a warning when full disable is unavailable). Unspecified effort is omitted. Opaque deployments use existing `like` configuration.
- Claude Messages/Bedrock: explicit disabled for supported known models; Opus 5 additionally sets low effort. Sonnet 5.5 uses between_tools + low, disabling only upfront thinking. Always-on Fable/Mythos/Opus 5.5 retain adaptive low with a warning.
- Configuration and agent-call test schemas accept off; custom raw mappings accept off with existing reserved-field safety checks.
- Admin `reasoning_mode=off` still suppresses all reasoning parameters, distinct from user effort off.
- Gemini retains the existing 128-token compatibility floor. The new marker must not accidentally enable a 1024-token budget. Full Gemini capability mapping remains a follow-up.

Reference: [Claude thinking support](https://platform.claude.com/docs/en/build-with-claude/thinking-troubleshooting). No catalog models, pricing, frontend controls or migrations change.

## Verification

```sh
cd backend
TESTING=true BOW_DATABASE_URL=sqlite:///db/app.db PYTHONPATH=. uv run pytest --noconftest \
  tests/unit/test_reasoning_effort_levels.py tests/unit/test_reasoning_off_effort.py \
  tests/unit/test_reasoning_stream_contract.py tests/unit/test_openai_reasoning_requests.py \
  tests/unit/test_openai_client_temperature.py tests/unit/test_azure_client_stream_v2.py \
  tests/unit/test_azure_foundry_endpoint.py tests/unit/test_anthropic_cache_breakpoints.py \
  tests/unit/test_anthropic_cache_ttl.py tests/unit/test_codegen_reasoning_time.py \
  -q --tb=short --disable-warnings
```

Observed **439 passed**, no failures, 117 warnings. Existing assertions encoding omitted off/mandatory adaptive thinking were updated to the intended contract. An initial run caught a misnamed schema import in the new test; corrected before final verification.

## What this proves / limits

Proves outgoing SDK request shapes, coder effort precedence, native/opaque mapping, positive efforts and the covered stream/cache paths. It does not prove browser latency, task quality, live Azure/Bedrock/Gemini acceptance or arbitrary gateway capabilities. No production settings/credentials changed. Capability metadata, UI disclosure of partial-disable/minimum fallbacks, context reduction and latency benchmarks remain separate follow-ups.
