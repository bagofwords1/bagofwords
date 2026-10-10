# Feedback Loop — "clarify tool UI is not robust enough (multi select, and others)"

The clarify form (`frontend/components/tools/ClarifyTool.vue`) lost picks,
blocked valid answers, ignored non-English "Other" options, and sent the agent
ambiguous multi-select answers. This loop reproduces each failure live (real
LLM, real UI), then shows it fixed.

## Root causes (validated)

1. **"Other" detected by English prefix** — `isOtherOption` was `/^other/i`
   (old `ClarifyTool.vue:191`). "Other genres" (a real option) opened a
   *Describe…* box and disabled Submit until text was typed; then the typed text
   *replaced* the pick. Hebrew "אחר" / Spanish "Otro" never opened a box.
2. **Multi-select joined with `", "`** (old `:220`) — options containing commas
   became ambiguous: `A: Revenue, net, Orders, gross`.
3. **Picks wiped when the turn finished streaming** — the report page remounts
   the component, `onMounted` re-inits the arrays, and sessionStorage was only
   written on submit (old `:342`), so nothing restored them.
4. **Fire-and-forget persistence** (old `:346`) — the answer was sent to the
   agent even when saving it failed; and the backend let a second POST
   overwrite an answered clarify (`completion_service.py` `submit_clarify_response`).
5. Smaller: locked view hid the "Other" text (old `:58`), focus always went to
   the first "Other" input (`otherInputRefs.value[0]`), `:key="opt"` broke on
   duplicate options, single-pick forms auto-submitted on the last click, no
   Skip, untranslated "Describe…", no dark-mode selected style, no ARIA roles.

## Loop A — deterministic (no external services)

```bash
cd backend && export BOW_DATABASE_URL="sqlite:///db/app.db"
uv run pytest -q tests/unit/test_clarify_tool.py tests/e2e/test_clarify_response.py
```

Before the fix (backend `app/` stashed): `4 failed, 8 passed` —
`test_allow_other_defaults_off_and_is_advertised`,
`test_examples_never_put_other_in_options`,
`test_plain_text_fallback_keeps_each_option_and_the_hints`,
`test_second_answer_does_not_overwrite_the_first`.
After: `12 passed`.

## Loop B — live UI with GPT-6 Luna (real OpenAI key via env var)

```bash
tools/agent/boot_stack.sh --dev
cd backend && uv run python ../tools/agent/seed_org.py --demo
# add an OpenAI provider with $OPENAI_API_KEY, model gpt-6-luna, set as default
cd ../frontend && PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers \
  node ../docs/feedback-loops/2026-10-10-clarify-robustness.verify.mjs out/legacy legacy
```

The script asks the agent for a clarify with a multi-select `["Revenue, net",
"Orders, gross", "Other…"]`, a single-pick `["Rock", "Other genres", "Jazz"]`
and a Hebrew `["30 יום", "90 יום", "אחר"]`, clicks **immediately** while the
turn is finishing, waits 15s, then submits.

| Check | Before | After |
|---|---|---|
| Picks 15s after clicking | `- - -` (wiped) | `SELECTED SELECTED SELECTED` |
| "Other genres" | opens *Describe…*, Submit disabled | plain option |
| "אחר" | no text box | opens *Describe…* |
| Prompt sent to agent | `A: Revenue, net, Orders, gross` / `A: Blues` (pick lost) / `A: אחר` | `A:\n- Revenue, net\n- Orders, gross` / `A: Other genres` / `A: last 2 years` |
| Locked view | "Other" text hidden | shown under the pick |

Variant `natural` (agent free to shape the call): Luna sent `allow_other: true`;
the UI rendered its own localized **Other**, stored as `__other__`.

Screenshots: `media/pr/clarify-robustness/` (`before-picked.png`,
`after-legacy-picked.png`, `after-submitted.png`, `after-allow-other.png`).

## The fix

- **Backend** — `ClarifyQuestion.allow_other` (`app/ai/tools/schemas/clarify.py`);
  tool description/examples teach the flag instead of an "Other…" option
  (`implementations/clarify.py`, `prompt_builder_v3.py`); the plain-text
  fallback for non-UI channels lists one option per line with "select all
  that apply" / "Other (describe)" hints; `submit_clarify_response` returns 409
  once answered.
- **Frontend** (`ClarifyTool.vue`) — UI-owned "Other" sentinel; legacy match
  only for a bare "Other/אחר/Otro…" word; multi picks one per line; draft saved
  on every change; answer stored (awaited) before it is sent, with an inline
  error on failure; no auto-submit; Skip; per-question focus; deduped options;
  ARIA radio/checkbox roles; dark-mode selected state; new strings in
  `locales/{en,es,he}.json`.

## Regression notes

- Answers are still sent to the agent as a plain-text prompt (by design).
- Shared-artifact chat (`ArtifactChatBubble.vue`) still shows only an "Asked a
  question" label for clarify — not addressed here.
