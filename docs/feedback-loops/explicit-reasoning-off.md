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
  tests/unit/test_opus_5_5_support.py \
  -q --tb=short --disable-warnings
```

Observed **447 passed**, no failures, 151 warnings. Existing assertions encoding omitted off/mandatory adaptive thinking were updated to the intended contract. The old Opus 5.5 private-helper assertion is replaced by the provider-boundary always-on model test. An initial run caught a misnamed schema import in the new test; corrected before final verification.

## What this proves / limits

Proves outgoing SDK request shapes, coder effort precedence, native/opaque mapping, positive efforts and the covered stream/cache paths. It does not prove browser latency, task quality, live Azure/Bedrock/Gemini acceptance or arbitrary gateway capabilities. No production settings/credentials changed. Capability metadata, UI disclosure of partial-disable/minimum fallbacks, context reduction and latency benchmarks remain separate follow-ups.

## Follow-up — small-default policy and nested calls

The five-turn benchmark exposed a second gap: `LLM.inference_stream_v2` forwarded
an omitted setting unchanged, and synchronous inference had no effort argument.
Choosing the small-default model did not enforce minimum reasoning. Parameter,
visualization and artifact helper calls could omit the run setting.

`LLM._effective_thinking` now resolves the policy for all three public inference
APIs. A model marked `is_small_default` always requests the supported minimum,
even if a caller asks for more. Other models honor an explicit call setting,
then the constructor's run setting. An unconfigured non-small call preserves
provider defaults. If the same model is both main and small-default, the
small-default policy wins as explicitly requested.

Synchronous provider clients now accept `thinking` and share translators with
their streaming implementations. The legacy text-stream facade routes policy
calls through native event streaming while retaining its text normalization and
usage recording. Judges explicitly request minimum reasoning even when their
fallback model is not marked small-default. Tool-created model clients receive
`runtime_ctx.reasoning_effort`; knowledge planning receives the run's thinking
configuration, with small-default taking precedence centrally.

The existing provider capability/configuration rules still apply: `off` becomes
`none`, disabled, or the supported minimum; custom endpoints use their configured
raw off mappings. Admin parameter-suppression mode and unknown capabilities are
not replaced with guessed request fields. Gemini retains the existing 128-token
compatibility floor rather than claiming zero reasoning on every model.

```sh
cd backend
TESTING=true BOW_DATABASE_URL=sqlite:///db/app.db PYTHONPATH=. uv run pytest --noconftest \
  tests/unit/test_small_model_reasoning.py -q --tb=short --disable-warnings
```

Initial public-facade regression: **3 failed, 3 passed** (small-default sync,
legacy stream and native stream failed). Final expanded policy regression:
**21 passed**. The earlier 11-module suite plus this module and provider-header
tests: **489 passed, 0 failed, 151 warnings**. No credentials or provider network
requests were used. Azure opaque deployment, Google and Bedrock synchronous
translations are tested at their SDK boundaries as well.

Implementation review caught a missing `TextDeltaEvent` import in the new legacy
bridge; a successful-stream test now verifies that the bridge returns text. Initial assertions
also incorrectly expected Sonnet 5 to require adaptive low and expected an
unwrapped SDK exception; corrected to match the established provider contract
and facade error behavior. Python compilation and `git diff --check` pass.

This proves request construction, not live acceptance or a measured latency gain.

## Parent merge — bounded streaming compatibility

Merged parent `267c1b4d9` (`codex/artifact-resources`). The only textual conflict
was `LLM.inference_stream`: the parent added `preserve_text`, output bounds,
explicit iterator closure and usage recording in `finally`, while this PR added
the reasoning-aware stream bridge. Both behaviors are retained. Bounded calls
use the bounded legacy adapters with the effective thinking policy; their
no-retry, truncation and cancellation behavior remains active. Unbounded calls
retain the native-stream policy bridge. Six additional facade cases cover raw
text, output caps, lowest reasoning, normal completion, truncation and closing.

The previous focused suite plus the parent's bounded-stream tests now reports
**504 passed, 0 failed, 151 warnings**. Python compilation and whitespace checks
pass. Run the verification command above with these additional modules:

```text
tests/unit/test_small_model_reasoning.py
tests/unit/test_llm_provider_headers.py
tests/unit/artifact_resources/test_bounded_streams.py
```

Parent integration findings:

- **Pre-existing defect, corrected:** parent `backend/app/errors/codes.py:24–25`
  repeats `SAML_UNAVAILABLE` and `SAML_LOGIN_FAILED`, already declared at lines
  11–12. Executing the source directly from `git show 267c1b4d9:backend/app/errors/codes.py`
  raises `TypeError: 'SAML_UNAVAILABLE' already defined`. Last touched in parent
  merge `bb00dea59`. Removed only the repeated declarations; values are unchanged.
- **Pre-existing migration blocker, not changed:** database-backed title tests
  cannot set up their fixtures because Alembic reports three heads:
  `artssomrg01`, `auditstrm01`, `mrg1003`. Migration files are identical to parent
  `267c1b4d9` (`git diff 267c1b4d9 -- backend/alembic` is empty). All ten cases
  in `test_report_title_streaming.py` are blocked at the same fixture setup.
  These are not included in the 504 passing focused tests. Initial invocation
  without conftest also lacked the required ORM/DB fixtures; rerunning with
  normal fixtures exposed the migration blocker above.
