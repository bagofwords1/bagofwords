# Feedback loop — the same screenshot looks newly uploaded after every edit

An artifact-editing conversation repeatedly treated the user's original screenshot
as fresh evidence after edits, even when newer browser output contradicted it.
This loop validates image placement in model requests, separately from the
model's interpretation of the screenshot.

## Root cause (validated)

`AgentV2._planner_images` (`backend/app/ai/agent_v2.py:2061`) keeps uploads on the
planner input while tool images travel in the transcript. Before this fix,
`PromptBuilderV3.build` forwarded those uploads through `PlannerInputV3.images`.
Provider adapters append that channel to the newest user-role message, which
after a tool call is the tool-result message. The reference therefore moved
forward on every iteration. The earlier v0.0.601 fix correctly prevents uploads
from earlier user requests from being automatically reattached; it does not
address movement within one request.

## Loop A — deterministic reproduction

Use Python 3.13 and the repository's dev dependencies. From the repository root:

```sh
cd backend
uv sync --frozen --extra dev
BOW_DATABASE_URL=sqlite:///db/app.db TESTING=true uv run pytest \
  tests/unit/test_planner_upload_origin.py \
  -q --tb=short --disable-warnings --show-capture=no
```

The regression goes through public prompt construction and each provider's
public streaming method, capturing only at the external SDK boundary. It varies
two uploads, zero/one/three tool iterations, and live history versus reconstruction.
It checks Anthropic, OpenAI Chat, Azure, OpenAI Responses, Bedrock, and Gemini.
The retained tool screenshots must stay after the original request, and each
upload must occur exactly once alongside that request.

Before the fix, the initial 18-case suite produced **12 failed, 6 passed**.
All 12 native-transcript cases failed with:

```text
AssertionError: An old reference moved onto a later tool-result message
assert [3] == [1]
```

The six passing cases preserved delivery on the legacy flat-prompt path.
The final expanded regression suite produces **23 passed**, also covering
budget accounting, image-free text previews, render isolation, and rejection
of images by models without vision support.

## The fix

- `backend/app/ai/agents/planner/transcript_bridge.py:185`: attach uploads to the
  original ask after prior history and before this run's tool calls. Label them
  as original references, not refreshed captures.
- `backend/app/ai/agents/planner/prompt_builder_v3.py:119`: clear the provider
  side channel on the native transcript path, avoiding duplicate/latest-message
  attachment. Keep legacy delivery and explain retained-image provenance there.
- `backend/app/ai/context/parts.py:65` and `transcript.py`: represent user images
  as typed parts, count their vision cost, and retain them with the ask.
- `backend/app/ai/llm/llm.py:1082`: include inline image blocks in the existing
  vision-capability check and diagnostic image count. This preserves validation
  when uploads stop using the separate image argument.
- Text prompt previews omit image bytes instead of serializing base64 as text.

## Loop B — live model, synthetic screenshot fixtures

With `ANTHROPIC_API_KEY` and `PLANNER_PROBE_MODEL` supplied through environment
variables, from `backend`:

```sh
BOW_DATABASE_URL=sqlite:///db/app.db uv run python \
  ../tools/agent/planner_image_origin_probe.py
```

The harness supplies a synthetic original chat screenshot with a table and a
newer synthetic browser capture without it. It calls the real model with the
planner's built messages and permits further edit/inspection tool calls.
Observed on `claude-haiku-5-5`: **zero tool calls**, a final response stating
the table was removed and the text answer remained. This is a live-model
continuity probe with fixture tool results, not a real browser/app end-to-end run.
No customer images or application data are sent by the harness.

## Verification and limits

**224 passed** across the regression, transcript, image retention/budget,
provider translation, image attachment, and normalization suites. **187 passed**
across LLM fallback, error classification, provider headers, and PII suites.
No failing tests in these runs; the full repository suite was not run.

Fatal-error Ruff checks pass for the changed runtime/test/probe files except
`agent_v2.py`, where this PR changes a comment only. Its two existing F821
diagnostics (`Any`, baseline lines 5220 and 6659) reproduce when linting the
unchanged main file at `14714b9af`; they are not introduced here.

This proves image provenance in requests and one live-model outcome. It does
not guarantee every model will interpret evidence correctly. Artifact version
overwrites, verification of collapsed widgets, and the other PR #1523 changes
are outside this fix. No frontend markup or rendered UI was changed.
