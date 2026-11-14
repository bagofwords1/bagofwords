# Feedback Loop — "image-generation models show up in chat model pickers"

Image-generation models (`supports_image_generation`, e.g. GPT Image 2.5 Flare /
Sunburst) produce images and cannot run a chat. The prompt-box picker hid them
client-side, but four other pickers listed them and the server accepted them
wherever a chat model is chosen. A user could make one their personal default
(or a report / dashboard / trigger model) and every chat on it would then be
sent to a model that can't answer.

## Root cause (validated)

The "is this a chat model?" rule existed only as client filters
(`PromptBoxV2.vue`, `ModelSelector.vue`, `TestPromptBox.vue`,
`LLMGroupDefaults.vue`) and in a few server queries (org default/small default,
`_live_chat_model`, fallback resolution, sub-agents). It was missing from:

- `LLMService.get_model_by_id` — the explicit-pick path for completions,
  triggers (`webhook_service`), eval runs and artifact live completions;
- `LLMService.validate_model_for_user` — report model, artifact chat model,
  dashboard settings;
- `LLMService.set_user_default_model` — personal default;
- `get_default_model_for_user` / `get_default_model_for_report` — resolution of
  saved preferences (a stale image pick was used as-is);
- `set_fallback_order`, `model_router.resolve_routing_candidates`,
  `TestSuiteService.get_test_catalog` (judge model options);
- pickers: `UserProfileModal.vue`, `automations/TriggersTab.vue`,
  `dashboard/DashboardSettingsModal.vue`, `monitoring/TestCaseEditor.vue`.

(`ScheduledPromptModal.vue` only looks up a model's name for display — not a
picker; left unfiltered so labels resolve.)

## Loop A — deterministic reproduction

```bash
cd backend
export BOW_DATABASE_URL="sqlite:///db/app.db" TESTING=true
uv run pytest tests/e2e/rbac/test_image_models_not_chat.py --db=sqlite -q
```

Uses an OpenAI provider with a dummy key; the catalog sync adds both chat and
image models. No provider calls.

**Before the fix (observed): `9 failed`** — personal default and report model
saved with `200` (expected 400), explicit completion pick `DID NOT RAISE`,
stale user/report preference resolved to a model with
`supports_image_generation=True`, fallback order accepted the image model,
routing candidates and judge options contained the image models.

**After the fix (observed): `9 passed`.**

## The fix

- `_reject_image_model` (400 "Image-generation models cannot be used for chat")
  in `get_model_by_id`, `validate_model_for_user`, `set_user_default_model`.
- Saved-preference resolution skips image models, so a pick saved before this
  change silently falls back to the next default instead of breaking chat.
- `set_fallback_order` rejects image models; routing candidates and judge
  options exclude them.
- The four pickers filter `!m.supports_image_generation`, matching the prompt box.

## Regression

LLM / model / report / webhook / trigger / eval / test-run / artifact-resource /
fallback / default / scheduled e2e files + `test_image_generation.py` +
`test_model_router.py`: **697 passed, 16 skipped, 0 failed** (SQLite).

## UI evidence

`media/pr/image-models-not-chat/` — the Account Settings default-model picker
before (lists GPT Image 2.5 Flare / Sunburst) and after (chat models only).
Seeded sandbox data, OpenAI provider with a dummy key.

## Notes

- `generate_image` selects image models through its own query and is
  unaffected.
- PR #1721 (decision models) adds a server-side `kind` filter to
  `GET /llm/models`; once both land, image models can move onto that same
  mechanism and the client filters can go.
