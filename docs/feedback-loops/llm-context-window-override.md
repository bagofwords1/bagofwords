# Feedback Loop — "expose context size per model, because e.g. Bedrock is 100k"

Like vision, a model's context-window size must be exposed per model: the app
should still ship sane defaults (the catalog / 200k fallback), but a privileged
admin must be able to set a different size per model — the motivating case being
Bedrock deployments where the same model id is capped at 100k tokens.

The context-size plumbing already existed end-to-end on the backend
(`context_window_tokens` column → read by the agent's prompt trimmer
`trim_context_to_budget` (`backend/app/ai/context/context_hub.py:159`, default
`DEFAULT_TOKEN_BUDGET = 200_000` when unset) and by
`estimate_prompt_tokens` (`backend/app/ai/agent_v2.py:836`) /
`completion_service.py:394` for the context-usage meter). What was missing was
(a) any admin path to set it — `POST /llm/models/{id}/set_context_window`
returned **404** — and (b) durability: for preset models the catalog re-sync
clobbers any direct edit. This loop validates the new manual override,
mirroring the `supports_vision_override` design
(`docs/feedback-loops/llm-vision-toggle.md`).

## Root cause (validated)

Before this change there was **no** manual control, and the catalog would
clobber any direct edit to a preset model. `LLMService.get_models` calls
`_sync_provider_with_latest_models` for preset providers on every read, and
that sync funnels through `_apply_catalog_model_details`
(`backend/app/services/llm_service.py`), which unconditionally did:

```python
model.context_window_tokens = model_data.get("context_window_tokens")
```

So setting `context_window_tokens` directly on a preset row reverted to the
catalog value on the next `GET /llm/models`. Custom models (the Bedrock case)
never sync from a catalog, but had no endpoint or UI to change the value after
creation either — the `PATCH /llm/models/{id}` route only accepts
`name`/`config`. The durable primitive had to be a new endpoint plus an
override column, exactly like vision.

## The fix

- **New column** `llm_models.context_window_tokens_override` (nullable int):
  `NULL` = follow the catalog; a value = explicit admin choice, persisted across
  catalog re-syncs. Migration
  `c1w2o3v4r5d6_add_context_window_override_to_llm_models.py`.
  `context_window_tokens` stays the *resolved* value the agent's token budget
  reads — no inference-side changes needed.
- **Resolution helper** `LLMService._resolve_context_window(override, catalog)`
  — a non-null override always wins. Applied in `_apply_catalog_model_details`
  (the sync), and an "override wins" step in `_create_models` and
  `_update_models` (both preset and custom branches). This is what makes preset
  overrides survive re-sync.
- **New endpoint** `POST /llm/models/{id}/set_context_window?tokens=<int>` +
  `LLMService.set_context_window` (mirrors `toggle_vision`), gated by
  `@requires_permission('manage_llm')`. A positive `tokens` sets the override
  and the effective value; omitting `tokens` clears the override (preset models
  fall back to the catalog size, custom models keep their stored value);
  `tokens <= 0` is rejected with 400. Writes an audit log
  (`llm_model.context_window_set`).
- **Schema** `LLMModelBase.context_window_tokens_override` so the value
  round-trips through provider create/update payloads and model/provider
  responses.
- **Frontend**: a Context column in `LLMsComponent.vue` (settings table —
  compact "1M"/"200K" display, click-to-edit inline input, and a reset-to-
  default affordance when an override is set) and a per-model context input in
  `LLMProviderModalComponent.vue` (Integrate Models modal, threads
  `context_window_tokens`/`context_window_tokens_override`, including for
  custom/Bedrock model rows). Both gated on `manage_llm_settings`. i18n keys
  under `settings.llms.*` in all ten locales (this also backfilled the vision
  keys, which existed only in `en`).

## Loop A — deterministic reproduction (no external services)

`backend/tests/e2e/test_llm_context_window_override.py`. Seeds a preset
Anthropic provider with `claude-sonnet-4-6` (catalog
`context_window_tokens=1_000_000`) and a custom Bedrock-style provider directly
in the DB, then drives the real route → service → DB stack. No LLM calls.

```bash
cd backend
export BOW_DATABASE_URL="sqlite:///db/app.db"
uv run pytest tests/e2e/test_llm_context_window_override.py -q
```

Observed **before** the fix (the reproduction):

```
E       AssertionError: {"detail":"Not Found"}
E       assert 404 == 200
3 failed, 1 passed
```

Observed **after**:

```
4 passed
```

- `test_context_window_override_persists_across_catalog_resync` — caps the
  preset model at 100k via the endpoint, then re-reads (which re-syncs from the
  catalog). The effective value stays 100k instead of reverting to 1M, and
  clearing the override restores the catalog default. This is the regression
  the feature closes.
- `test_context_window_settable_on_custom_model` — the actual Bedrock case: a
  custom model with no catalog entry and no size gets one via the endpoint, and
  `tokens=0` is rejected without touching the row.
- `test_set_context_window_route_is_permission_gated` — asserts the endpoint
  carries the `manage_llm` gate (via the introspectable `_required_permission`
  recorded by the decorator).
- `test_context_budget_reads_model_context_window` — `trim_context_to_budget`
  leaves a ~50k-token prompt untouched under the 200k default but trims it
  under a 20k admin-set window, proving the admin-set size actually governs
  what is sent to the model.

Reproducing the *pre-fix* clobber without the 404: stash the
`_resolve_context_window` change (so `_apply_catalog_model_details` sets
`context_window_tokens` straight from the catalog) and the first test fails on
`assert model["context_window_tokens"] == 100_000` after the re-sync — the
override is ignored and the catalog wins.

## Loop B — live UI confirmation (running stack, no LLM credentials)

Booted the full stack (`tools/agent/boot_stack.sh --dev` + `seed_org.py --demo`),
seeded a preset Anthropic provider and a custom Bedrock provider/model, and
drove the real UI with Playwright. Evidence in
`media/pr/claude-model-context-size-exposure-tyhltd/`:

- `before.png` — the LLM settings table has no context-size surface at all.
- `after.png` — new **Context** column: presets show their catalog sizes
  ("200K", "1M"), the Bedrock custom model shows "—" (unset → 200k default
  budget at runtime).
- `after-edit.png` — clicking the value opens the inline editor (typed 100000).
- `after-saved.png` — Enter hits `set_context_window`; the row shows **100K**
  with a reset-to-default affordance and the success toast. The value came
  back from the real API, not local state.
- `after-modal.png` — the Integrate Models modal shows the same admin-set
  100000 in the new per-model Context input on the Bedrock model row.

## What this proves / regression notes

- The override is the single source of manual truth and **survives catalog
  re-sync** for preset models; the default behavior (catalog values, 200k
  budget fallback for sizeless models) is unchanged when no override is set.
- Custom/Bedrock models were never synced from a catalog, so their size was
  user-owned at creation only; the override gives them a post-creation admin
  path and lets the same UI govern them.
- The agent-side consumers (`trim_context_to_budget`, `estimate_prompt_tokens`,
  the context-usage meter) were already keyed off `context_window_tokens`; no
  client code needed touching.
- Companion tests `tests/e2e/test_llm_vision_toggle.py` and the keyless catalog
  sync test `test_llm_providers.py::test_preset_openai_sync_migrates_to_gpt_56_models`
  still pass with the change applied (`4 passed`).
