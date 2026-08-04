# UI audit — Settings → LLM (models page), 2026-08-04

Scope: the refactored `/settings/models` page (provider chips + filter, Add
Model / Add Provider, model list, model card modal, actions menu, delete
confirmations) plus the pre-existing provider modal reached from the new entry
points. Roles: admin (`full_admin_access`) and member (system `member` role,
no `manage_llm`). Method per `.agents/skills/ui-audit`: expectations written
from handler code and committed before the browser pass
(`audit/models/expectations.md`), then Phase-1 sweep
(`scripts/sweep.mjs`, admin + member) and Phase-3 click-through against a live
sandbox (backend :8000 sqlite, frontend :3000, real Anthropic provider).

Mid-audit revision: after the expectations were committed, the Add Model
modal was redesigned on request (card-style, provider icon select, no catalog
list) and chips became filters with a "Model Settings" menu item added.
Revised rows were re-derived and re-clicked; the expectations file carries an
addendum.

## Findings

Mismatch count: 2 confirmed (both fixed in this branch), 1 recorded as-is.

### F1 — P3 (fixed): enabling Image gen on the default model silently un-defaults the org

- **Symptom**: In the model card, switching Image gen ON for the model that is
  Default (and/or Small default) succeeds (200) and both badges vanish — the
  org is left with **no default model**. No warning, no toast about the side
  effect. Evidence: audit obs `C3` — "Default badges left in table=0".
- **Repro**: model card of the default model → toggle Image gen on.
- **Root cause**: `backend/app/services/llm_service.py:toggle_image_generation`
  intentionally clears `is_default`/`is_small_default` (image models can't be
  chat defaults), but the UI offered the toggle on a default model with no
  guard — the same class of footgun the enabled-toggle and delete already
  guard against.
- **Fix (this branch)**: `LLMModelCardModal.vue` disables the Image gen toggle
  for default/small-default models with tooltip
  `settings.llms.imageGenDefaultBlocked` (mirrors the backend `set_default`
  guard "image-generation models cannot be set as default"). Verified: toggle
  renders disabled on the default model's card.

### F2 — P3 (fixed): context window set in Add Model was stored without its override

- **Symptom**: A model added via Add Model with a context window did not show
  the override-reset arrow in its card, and the value would not survive a
  catalog re-sync the way an admin override should. Evidence: audit obs `C5` —
  "reset visible=false" despite the override being sent.
- **Root cause**: `LLMService.create_model` dropped
  `context_window_tokens_override` from the payload (it persisted only the
  plain value).
- **Fix (this branch)**: persist `context_window_tokens_override` in
  `create_model`. Verified: newly added model with context 123000 shows the
  reset arrow in its card.

### F3 — P4 (recorded, not fixed): auto-router seeds the same model twice when it holds both defaults

- **Symptom**: First enable of Auto router with one model being both Default
  and Small default fires `POST /routing_hint` twice against the same model;
  the second (default-seed) hint overwrites the first (small-seed) hint.
  Evidence: audit obs `H6` — two POSTs to the same model id.
- **Root cause**: `LLMsComponent.vue:seedDefaultRoutingHints` iterates the two
  seeds without refreshing `models` between them, so `modelHint(m)` still
  reads empty on the second pass. Pre-existing logic, unchanged by the
  refactor; harmless beyond the hint text choice.

### Pre-existing defects fixed en route (found in Phase 2, before clicking)

- **P2 (fixed)**: `DELETE /api/llm/models/{id}` and `PATCH /api/llm/models/{id}`
  called `LLMService.delete_model` / `update_model`, which did not exist —
  every call 500'd with `AttributeError`. Implemented with guards (no deleting
  default/small-default; no deleting preset-catalog models of a preset
  provider, which the catalog sync would resurrect).

## Coverage (expected = actual everywhere else)

- Phase-1 sweep: admin — 0 routes flagged, no console errors, no failed API
  calls on load; member — redirected to `/` as the permission matrix says
  (`manage_llm` gate). Role diff shows only expected gating (plus the
  known user-menu identity false-positives).
- Clicked rows R1–R2, H1–H9, L1–L13, C1–C11, A1–A5, E1, P1–P5 (see
  `audit/models/expectations.md`): provider chips filter and clear; gear and
  Actions → Manage Provider open the provider editor; Add Provider lands on
  the New Provider step; Add Model creates with vision/image-gen/context/cost
  and rejects duplicates visibly; search filters client-side; auto-router and
  fallback toggles flip org settings, reveal their columns, and their per-row
  editors save (routing hint POST 200, fallback order POST 200 with chain
  update); status toggle 200s and is disabled on defaults; access modal opens
  without triggering the row's model card; both delete paths confirm before
  DELETE and cancel cleanly; default model's card blocks disable/delete;
  Save is disabled until dirty; provider delete removes chip + models and is
  refused while it holds the default model (400 verified at the API).
