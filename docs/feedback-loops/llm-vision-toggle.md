# Feedback Loop — "manually manage LLM model and toggle vision on/off"

Admins want to turn a model's image (vision) support on or off by hand, and have
that choice **stick** — including for preset models like Claude, whose flags are
otherwise re-synced from the hardcoded catalog on every settings load.

The vision plumbing already existed end-to-end on the backend (`supports_vision`
column → enforced at inference by `_validate_vision_support`). What was missing
was (a) a durable way to override the catalog per model and (b) any UI to do it.
This loop validates the new manual override.

## Root cause (validated)

Before this change there was **no** manual control, and the catalog would clobber
any direct edit to a preset model. `LLMService.get_models` calls
`_sync_provider_with_latest_models` for preset providers on every read, and that
sync funnels through `_apply_catalog_model_details`
(`backend/app/services/llm_service.py:1415`), which unconditionally did:

```python
model.supports_vision = model_data.get("supports_vision", False)
```

So setting `supports_vision` directly on a preset row would revert to the catalog
value on the next `GET /llm/models`. There was also no `create_model`/`update_model`
service method behind the `PATCH /llm/models/{id}` route (those routes are dead),
so widening `LLMModelUpdate` was a non-starter — the durable primitive had to be a
new toggle plus an override column.

## The fix

- **New column** `llm_models.supports_vision_override` (nullable): `NULL` = follow
  the catalog; `True`/`False` = explicit admin choice. Migration
  `v1s2v3o4t5g6_add_supports_vision_override_to_llm_models.py`.
  `supports_vision` stays the *resolved* value inference reads.
- **Resolution helper** `LLMService._resolve_supports_vision(override, catalog)` —
  a non-null override always wins. Applied everywhere the effective flag is set:
  `_apply_catalog_model_details` (the sync), `_create_models`, `_update_models`.
  This is what makes preset overrides survive re-sync.
- **New endpoint** `POST /llm/models/{id}/toggle_vision?enabled=<bool>` +
  `LLMService.toggle_vision` (mirrors the existing `toggle_model`), gated by
  `@requires_permission('manage_llm')`. It sets the override and the effective flag,
  and writes an audit log (`llm_model.vision_toggled`).
- **Schema** `LLMModelBase.supports_vision_override` so the flag round-trips through
  the provider create/update payloads and the model/provider responses.
- **Frontend**: a Vision toggle column in `LLMsComponent.vue` (settings table,
  calls `toggle_vision`) and a per-model Vision toggle in
  `LLMProviderModalComponent.vue` (Integrate Models modal, threads
  `supports_vision_override`). Both gated on `manage_llm_settings`. i18n keys under
  `settings.llms.*`.

## Loop A — deterministic reproduction (no external services)

`backend/tests/e2e/test_llm_vision_toggle.py`. Seeds a preset Anthropic provider
with `claude-sonnet-4-6` (catalog `supports_vision=True`) directly in the DB, then
drives the real route → service → DB stack. No LLM calls.

```bash
cd backend
export BOW_DATABASE_URL="sqlite:///db/app.db"
uv run pytest tests/e2e/test_llm_vision_toggle.py -q
```

Observed:

```
3 passed
```

- `test_vision_toggle_persists_across_catalog_resync` — turns vision OFF via the
  endpoint, then re-reads (which re-syncs from the catalog). **Before the override:
  `supports_vision` reverted to `True`.** After: it stays `False`
  (`supports_vision_override == False`), and toggling back ON survives another
  re-sync. This is the regression the feature closes.
- `test_toggle_vision_route_is_permission_gated` — asserts the endpoint carries the
  `manage_llm` gate (via the introspectable `_required_permission` the decorator now
  records).
- `test_inference_gate_reads_supports_vision` — `LLM._validate_vision_support`
  raises `"does not support images"` when `supports_vision` is False and passes when
  True, proving the manual flag actually governs inference.

Reproducing the *pre-fix* clobber: stash the `_resolve_supports_vision` change (so
`_apply_catalog_model_details` sets `supports_vision` straight from the catalog) and
the first test fails on `assert model["supports_vision"] is False` after the re-sync
— the override is ignored and the catalog wins.

## Loop B — live confirmation (real ANTHROPIC credentials)

Proves the toggle governs a **real** provider call, not just a DB flag. Secrets come
from `ANTHROPIC_KEY` in the environment (never committed); the Anthropic SDK routes
through `ANTHROPIC_BASE_URL`. Save as `backend/loopb_vision.py` and run from
`backend/`:

```python
import base64, io, os
from PIL import Image
import main  # noqa: F401 -- registers every ORM model so mappers configure
from app.models.llm_model import LLMModel
from app.models.llm_provider import LLMProvider
from app.ai.llm.llm import LLM
from app.ai.llm.types import ImageInput

def image():
    img = Image.new("RGB", (96, 96), (30, 120, 220))  # solid blue
    buf = io.BytesIO(); img.save(buf, format="PNG")
    return ImageInput(data=base64.b64encode(buf.getvalue()).decode(),
                      media_type="image/png", source_type="base64")

def llm(supports_vision):
    p = LLMProvider(name="A", provider_type="anthropic", organization_id="o",
                    is_preset=True, is_enabled=True)
    p.encrypt_credentials(os.environ["ANTHROPIC_KEY"], None)
    m = LLMModel(name="Haiku", model_id="claude-haiku-4-5-20251001",
                 organization_id="o", provider=p, is_preset=True, is_enabled=True,
                 supports_vision=supports_vision, supports_vision_override=supports_vision)
    return LLM(m)

img = image()
try:
    llm(False).inference("What color?", images=[img], should_record=False)
    print("FAIL: gate did not fire")
except ValueError as e:
    print("OFF -> gated:", e)
out = llm(True).inference("What color is this image? One word.", images=[img], should_record=False)
print("ON  -> live response:", out.strip())
assert "blue" in out.lower()
```

```bash
cd backend
export BOW_DATABASE_URL="sqlite:///db/app.db"
uv run python loopb_vision.py
```

Observed (real `POST https://api.anthropic.com/v1/messages "200 OK"`):

```
OFF -> gated: Model 'claude-haiku-4-5-20251001' does not support images. ...
ON  -> live response: Blue
```

Vision OFF is refused before any network call; vision ON reaches Anthropic and the
model correctly reads the blue image. The manual flag drives real behavior.

## What this proves / regression notes

- The override is the single source of manual truth and **survives catalog re-sync**
  for preset models — the core requirement.
- Custom/Bedrock models were never synced from a catalog, so their vision value was
  already user-owned; the override makes them consistent and lets the same UI toggle
  govern them.
- The inference gate is unchanged and already keyed off `supports_vision`; no client
  code needed touching.
- Pre-existing unrelated failures in `tests/e2e/test_llm_providers.py` come from
  `OPENAI_API_KEY_TEST`/`ANTHROPIC_API_KEY_TEST` not being set in this sandbox
  (the fixtures `pytest.fail` without them) — they reproduce with this change stashed.
  The catalog-sync test that does not need a key
  (`test_preset_openai_sync_migrates_to_gpt_56_models`) passes.
