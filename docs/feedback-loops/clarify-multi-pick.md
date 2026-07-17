# Feedback Loop — clarify tool: multi-pick ("select all that apply") questions

The clarify tool could only render single-choice chip questions: picking an
option replaced the previous pick, so the model had no way to ask
"which metrics should the dashboard include?" and let the user pick several.
This loop validates the feature end-to-end: schema flag → planner guidance →
SSE payload → interactive form → answer assembly → persistence → rehydration.

## Root cause (of the limitation, validated)

- `ClarifyQuestion` (`backend/app/ai/tools/schemas/clarify.py`) had only
  `text` + `options` — no selection-mode flag, so the UI could not know a
  question allows multiple answers.
- `ClarifyTool.vue` (`frontend/components/tools/ClarifyTool.vue`) kept ONE
  string per question (`selectedChips: string[]`); `selectOption` replaced the
  previous pick.
- `submit_clarify_response`
  (`backend/app/services/completion_service.py`) persisted whatever arrived,
  with no defined shape for multi-pick entries.

## The change

- Schema: `ClarifyQuestion.multi_select: bool = False`; tool metadata,
  example, and version bumped (`implementations/clarify.py`).
- Planner: `prompt_builder_v3.py` clarify protocol now tells the model when
  to set `multi_select: true`.
- Service: `selected_chips` entries are normalized — a string (single-pick)
  or a list of strings (multi-pick); non-string junk is dropped.
- UI: per-question `string | string[]` selection state, checkbox-style toggle
  with check badges, "Select all that apply" hint (`tools.clarify.multiHint`,
  en/es/he), "Other…" can coexist with other picks, answers join with ", ",
  and auto-submit is disabled for forms containing a multi-select question
  (we can't know when the user is done picking). Legacy persisted responses
  (plain strings) still rehydrate.

## Loop A — deterministic reproduction (no external services)

```bash
cd backend
export BOW_DATABASE_URL="sqlite:///db/app.db" TESTING=true
uv run pytest tests/unit/test_clarify_tool.py tests/e2e/test_clarify_response.py -m "" -q
```

Before the change (stash `schemas/clarify.py` + `completion_service.py`):
`5 failed, 3 passed` — `multi_select` rejected by the schema, absent from the
LLM-facing JSON schema and the tool.start payload, and junk persisted
verbatim. After: `8 passed`.

## Loop B — full stack, real HTTP/SSE, deterministic stub LLM

The stub plays a planner that calls `clarify` with a `multi_select` question,
a single-pick question, and a free-form question, so the whole path
(planner streaming → tool dispatch → SSE → ClarifyTool.vue → clarify_response
endpoint → rehydration) runs for real with zero API keys:

```bash
tools/agent/boot_stack.sh
cd backend && uv run python ../tools/agent/seed_org.py --demo
uv run python ../tools/agent/clarify_stub_llm.py &        # port 9099
# register an openai-type provider with base_url http://127.0.0.1:9099/v1,
# model_id "stub", and set it org default (POST /api/llm/providers + set_default)
export PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers
cd frontend && node ../tools/agent/clarify_multipick_convo.mjs ../media/pr/clarify-multi-pick
```

Observed (screenshots in `media/pr/clarify-multi-pick/`):

- `02-clarify-form.png` — form renders with the "Select all that apply" hint
  (proves `multi_select` survived LLM → tool → SSE → component).
- `03-multi-picked.png` — Revenue + Orders + Sessions selected simultaneously
  with check badges; single-pick question unaffected.
- `04-submitted.png` — locked form; the assembled turn reads
  `A: Revenue, Orders, Sessions`; agent resumed on the answers.
- `05-rehydrated.png` — after `sessionStorage.clear()` + full reload, the form
  rehydrates locked with all picks — persistence comes from the backend, whose
  stored `result_json.user_response.selected_chips` is
  `[["Revenue","Orders","Sessions"], "Last 7 days", ""]`.

## Loop C — live LLM (Claude Haiku) — BLOCKED at time of writing

The same `clarify_multipick_convo.mjs` drives the live leg unchanged: make
the Anthropic provider's Haiku model the org default and rerun. At the time
of this loop the sandbox's Anthropic key was valid (`GET /v1/models` OK) but
every `POST /v1/messages` — including Haiku — failed with
`Your credit balance is too low to access the Anthropic API`, so the live leg
could not run. Nothing in it is feature-specific; rerun when the key has
credits.

## What this proves / regression notes

- The multi-pick contract holds at every layer, including the two risky
  spots: auto-submit is suppressed for multi-select forms, and legacy
  single-string persisted responses still rehydrate (covered by
  `test_persists_single_and_multi_pick_shapes` + the component's
  normalization in `applyPersistedResponse`).
- Pre-existing, unrelated: `POST /api/llm/models` 500s
  (`LLMService.create_model` missing) and `POST /api/llm/providers` with an
  anthropic preset 500s (`MissingGreenlet`) — both reproduce without this
  change; worked around via provider-embedded model creation / direct seeding.
