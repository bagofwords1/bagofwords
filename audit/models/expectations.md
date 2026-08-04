# Expectations — /settings/models (refactored models page)

Written from handler code BEFORE the Phase-3 browser pass (ui-audit skill).
Scope: the refactored models settings page — provider chips, Add Model / Add
Provider, model list, model card modal, actions menu, delete confirmation —
plus the pre-existing provider modal reached from the new entry points.

Sandbox state at audit time: 1 provider (Anthropic, user-created, not preset),
1 model (Claude 4.5 Haiku = default + small default). Enterprise features
(llm_access_control, model_routing, llm_fallback) are licensed in this build
(the toggles render unlocked with "?" popovers). Roles: admin (qa@example.com,
full_admin_access) and member (member@example.com, member system role — no
`manage_llm`).

Sources: `frontend/components/LLMsComponent.vue` (page), `LLMModelCardModal.vue`
(card), `LLMAddModelModal.vue` (add model), `LLMProviderModalComponent.vue`
(provider modal), `frontend/pages/settings/models.vue` (route meta),
`frontend/middleware/permissions.global.ts` (guard),
`backend/app/routes/llm.py` + `backend/app/services/llm_service.py` (API).

## Route / role gating

| # | Control | Role | Expected | Source |
|---|---------|------|----------|--------|
| R1 | Navigate to /settings/models | admin | Page renders; GET /api/llm/models, /api/llm/providers, /api/organization/settings, /api/llm/fallback_order all 200 | models.vue:9 definePageMeta permissions ['manage_llm']; LLMsComponent onMounted |
| R2 | Navigate to /settings/models | member | Redirected to `/` (member lacks manage_llm) | permissions.global.ts:44-63; roles matrix (member has no manage_llm) |

## Page header (admin, models present)

| # | Control | Selector | Expected | Source |
|---|---------|----------|----------|--------|
| H1 | Provider chip (per provider) | `[data-testid="provider-chip"]` | Static pill: provider icon + name. Not clickable itself | LLMsComponent.vue chips block |
| H2 | Chip gear button | `[data-testid="provider-chip-gear"]` | Opens provider modal in EDIT view for that provider (breadcrumb "Providers / Anthropic", API-key field "Keep blank to use stored key", models list, Danger Zone). No page navigation | LLMsComponent openManageProvider → LLMProviderModalComponent watch(editProviderId) |
| H3 | Add Model button | `[data-testid="add-model-button"]` | Opens Add Model modal; GET /api/llm/available_models fires (first open); provider select pre-set to first provider; catalog list = catalog models for provider type minus installed | LLMAddModelModal watch(open), loadCatalog, catalogChoices |
| H4 | Add Provider button | `[data-testid="add-provider-button"]` | Opens provider modal directly on New Provider step (provider-type tile grid), skipping the provider list | LLMsComponent openAddProvider (startWithNewProvider=true) → LLMProviderModalComponent watch(providerModalOpen) selectOption('new_provider') |
| H5 | Search input | `input[placeholder*="Search"]` | Client-side filter of rows by model name or provider name; no network | filteredModels computed |
| H6 | Auto router toggle | `[data-testid="auto-router-toggle"] button` | PUT /api/organization/settings {model_routing}; success toast; Routing column appears; on first enable, seeds routing hints (POST /llm/models/{id}/routing_hint) for default+small-default models with empty hints | saveAutoRouter, seedDefaultRoutingHints |
| H7 | Auto router "?" | popover icon | Hover shows 4-bullet explainer panel | UPopover block |
| H8 | LLM fallback toggle | `[data-testid="llm-fallback-toggle"]` | PUT /api/organization/settings {llm_fallback}; toast; Fallback column + chain summary line appear | saveFallbackToggle |
| H9 | Fallback "?" | popover icon | Hover shows 3-bullet explainer panel | UPopover block |

## Model list rows (admin)

| # | Control | Selector | Expected | Source |
|---|---------|----------|----------|--------|
| L1 | Row click | `[data-testid="model-row"]` | Opens model card modal for that model (admin only; no-op for roles without manage_llm_settings — they can't reach the page anyway) | openModelCard |
| L2 | Status toggle | row UToggle | POST /api/llm/models/{id}/toggle?enabled=…; toast; list refresh. DISABLED for default/small-default model. Click must NOT also open the model card (td has @click.stop) | toggleModel; backend toggle_model 400s for defaults |
| L3 | Cost cell | `[data-testid="llm-cost-cell"]` | Read-only display (Input/Output $/M, "—" when unset). Clicking it opens the model card via row click (no stop on this td) | template cost cell |
| L4 | Access cell (EE) | `[data-testid="llm-access-cell"]` | Opens LLMModelAccessModal; label "Everyone (default)" for default models, else Everyone/Restricted; must NOT open the model card | openAccess; td @click.stop |
| L5 | Actions "⋮" | row dropdown trigger | Opens menu: Make Default, Make Small Default, Manage Provider / Delete (red, trash icon). Delete DISABLED for default/small-default model. Must not open model card | dropdownItemsByModel, buildDropdownItems |
| L6 | Make Default | menu item | POST /api/llm/models/{id}/set_default; Default badge moves to this row; toast "Model updated" | setDefaultModel(small=false) |
| L7 | Make Small Default | menu item | POST .../set_default?small=true; Small default badge moves; toast | setDefaultModel(small=true) |
| L8 | Manage Provider | menu item | Opens provider modal in EDIT view for the row's provider (same as H2) | openManageProvider |
| L9 | Delete (menu) | menu item | Opens confirmation modal naming the model; nothing deleted yet | deleteTarget ref → UModal |
| L10 | Delete confirm → Delete | `[data-testid="delete-model-confirm-button"]` | DELETE /api/llm/models/{id}; toast "Model deleted"; row disappears; modal closes. Backend soft-deletes (deleted_at set, is_enabled=0) | confirmDeleteModel; llm_service.delete_model |
| L11 | Delete confirm → Cancel | ghost button | Modal closes, nothing deleted | deleteConfirmOpen setter |
| L12 | Routing cell (router on) | `[data-testid="llm-routing-cell"]` | Enabled models: dotted "Add routing guidance…" → click opens textarea; ✓ saves POST /llm/models/{id}/routing_hint; ✗/Escape cancels. Disabled models: "not routable". Must not open model card | startHintEdit/saveHint; td @click.stop |
| L13 | Fallback cell (fallback on) | `[data-testid="llm-fallback-cell"]` | Default model: "primary" label. Other enabled models: priority select → POST /api/llm/fallback_order; chain summary updates. Must not open model card | setFallbackPriority; td @click.stop |

## Model card modal (admin)

| # | Control | Selector | Expected | Source |
|---|---------|----------|----------|--------|
| C1 | Enabled toggle | `[data-testid="card-enabled-toggle"]` | POST /llm/models/{id}/toggle immediately; DISABLED for default/small-default (tooltip explains) | LLMModelCardModal toggleEnabled |
| C2 | Vision toggle | `[data-testid="card-vision-toggle"]` | POST /llm/models/{id}/toggle_vision immediately; persists override in DB | toggleVision; llm_service.toggle_vision |
| C3 | Image gen toggle | `[data-testid="card-imagegen-toggle"]` | POST /llm/models/{id}/toggle_image_generation immediately. NOTE backend side effect: enabling on the default model CLEARS its default/small-default flags (image models can't be defaults) — org can be left with no default. Watch for this | toggleImageGeneration; llm_service.toggle_image_generation:904-907 |
| C4 | Context input + Save | `[data-testid="card-context-input"]`, `card-save-button` | Save enabled only when dirty; POST /llm/models/{id}/set_context_window?tokens=N (floor, must be >0, else client-side error toast); "Model updated" toast; modal closes | save() |
| C5 | Context reset ↺ | arrow button (visible only when override set) | POST set_context_window with no tokens → clears override, preset models return to catalog value; card stays open, value re-syncs | resetContextWindow |
| C6 | Cost inputs + Save | `card-cost-input`, `card-cost-output` | POST /llm/models/{id}/pricing with both values; negative → client-side error toast; toast + close on success | save() |
| C7 | Save (not dirty) | `card-save-button` | Disabled | isDirty computed |
| C8 | Cancel / X / Escape | buttons | Close without applying drafts (toggles already applied — they're instant) | open setter |
| C9 | Delete | `[data-testid="card-delete-button"]` | DISABLED for default/small-default with tooltip. Else swaps footer to inline confirm ("Delete "name"? This cannot be undone.") | confirmingDelete |
| C10 | Inline confirm → Delete | `card-delete-confirm-button` | DELETE /api/llm/models/{id}; toast; card closes; row gone | deleteModel |
| C11 | Inline confirm → Cancel | ghost button | Footer returns to normal, nothing deleted | confirmingDelete=false |

## Add Model modal (admin)

| # | Control | Selector | Expected | Source |
|---|---------|----------|----------|--------|
| A1 | Provider select | `[data-testid="add-model-provider-select"]` | Lists org providers; changing it clears checked catalog selections | watch(selectedProviderId) |
| A2 | Catalog row / checkbox | rows in modal | Click row or checkbox toggles selection; installed models are absent from list; if all installed, italic empty message | catalogChoices, toggleChoice |
| A3 | Custom model input | `[data-testid="add-model-custom-input"]` | Free-text model id; enables Add | canSubmit |
| A4 | Add | `[data-testid="add-model-submit"]` | Disabled with no selection and empty custom. Else POST /api/llm/models per catalog pick (with catalog vision/context/cost) + one for custom (is_custom). Success: toast "Model added", modal closes, rows appear. Duplicate id → 409 with error toast, modal stays open | submit(); llm_service.create_model dup check |
| A5 | Cancel / X | buttons | Close, nothing created | open setter |

## Provider modal (pre-existing flow, reached from new entry points)

| # | Control | Selector | Expected | Source |
|---|---------|----------|----------|--------|
| P1 | Gear-opened edit view | — | Shows API key (blank = keep stored), models with checkbox/vision/context, Add Custom Model, Danger Zone → Delete Provider, Test Connection, Update Provider | LLMProviderModalComponent edit branch |
| P2 | Test Connection (edit) | button | POST /api/llm/test_connection; toast success/fail | testConnection |
| P3 | Update Provider | submit | PUT /api/llm/providers/{id}; toast; modal closes; chips/models refresh (getModels+getProviders on close) | updateProvider; handleProviderModalClose |
| P4 | Danger Zone → Delete Provider | red button | window.confirm; DELETE /api/llm/providers/{id}; blocked with error toast if provider holds default model | deleteProvider; llm_service.delete_provider guard |
| P5 | Add Provider view ("Providers" back link) | breadcrumb | Returns to provider list view (chip-less list + "New Provider" option) | goBackToProviderList |

## Empty state (no models)

| # | Control | Expected | Source |
|---|---------|----------|--------|
| E1 | Empty-state Add Provider | Same as H4 — modal opens on New Provider step | template empty state @click openAddProvider |

## Addendum — mid-audit UI revision (user feedback, same day)

The design changed after the table above was committed; revised rows,
re-derived from the updated code and re-clicked:

| # | Control | Selector | Expected | Source |
|---|---------|----------|----------|--------|
| H1r | Provider chip | `[data-testid="provider-chip"]` | Click FILTERS the table to that provider (active style: blue border/bg); second click clears. Gear inside still opens provider settings without triggering the filter (`@click.stop`) | toggleProviderFilter, filteredModels |
| L5r | Actions menu | dropdown | First item is now "Model Settings" (opens the model card), then Make Default, Make Small Default, Manage Provider / Delete | buildDropdownItems |
| A1r | Provider select | `[data-testid="add-model-provider-select"]` | Custom select button with provider ICON + name; opens option list (icon + name + check); picking closes it | LLMAddModelModal template |
| A2r | Add Model modal body | — | Card-style rows (same layout as model card): Model ID input, Vision toggle, Image gen toggle, Context input, Cost inputs. NO catalog list | LLMAddModelModal |
| A4r | Add | `[data-testid="add-model-submit"]` | Disabled until provider + model id present. POST /api/llm/models with is_custom + chosen vision/image-gen/context(+override)/costs; client-side duplicate check shows error toast and keeps modal open | submit() |

## Phase-3 outcomes

All rows above matched their expectation except: C3 (image-gen on default
silently un-defaults the org — fixed, toggle now disabled with tooltip),
C5 (context override dropped by create_model — fixed, override persisted),
and H6 (double routing-hint seed when one model holds both defaults —
recorded as P4, unchanged). Full report:
`docs/feedback-loops/ui-audit-2026-08-04-models.md`.
