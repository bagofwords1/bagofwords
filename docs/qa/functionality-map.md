# Functionality map

Living inventory of user-facing flows (see `docs/qa/README.md`). Seeded
2026-08-04 by a change-scoped QA pass over the **Settings → LLM (models)**
area; other areas are enumerated at the screen level only and still need
their flow rows filled in by a future full pass.

## Settings → LLM (models page)

Routes: `/settings/models` (meta permissions: `manage_llm`).
API: `backend/app/routes/llm.py` (`/api/llm/*`).
Guards: `frontend/middleware/permissions.global.ts` (org-perm gate),
`onboarding` redirect until dismissed.
Automated coverage: `backend/tests/e2e/rbac/test_rbac_llm_models.py` (API-level
RBAC), `frontend/tests/onboarding/onboarding-wizard.spec.ts` (onboarding LLM
step only — the settings page itself has no Playwright spec).

| Area | Flow | Route(s) | API | Automated coverage | Last QA | Status |
|------|------|----------|-----|--------------------|---------|--------|
| LLM | Add a provider (Add Provider → New Provider step → test connection → save) and its models appear | /settings/models | POST /api/llm/providers, POST /api/llm/test_connection | none (UI) | 2026-08-04 | PASS |
| LLM | Provider chip filters the model list; gear opens provider settings (key/URL/models/danger zone) | /settings/models | GET /api/llm/providers, PUT /api/llm/providers/{id} | none | 2026-08-04 | PASS |
| LLM | Add a model to an existing provider (Add Model: provider icon select, model id, vision/image-gen/context/cost) and it appears in the list | /settings/models | POST /api/llm/models | none | 2026-08-04 | PASS |
| LLM | Duplicate model id is rejected with a visible error and the modal stays open | /settings/models | POST /api/llm/models (409) | none | 2026-08-04 | PASS |
| LLM | Open model card (row click or Actions → Model Settings), edit context/cost, save; values persist | /settings/models | POST /api/llm/models/{id}/set_context_window, /pricing | none | 2026-08-04 | PASS |
| LLM | Toggle enabled/vision/image-gen from the model card; default model's enabled + image-gen toggles are blocked with explanation | /settings/models | POST /api/llm/models/{id}/toggle, /toggle_vision, /toggle_image_generation | none | 2026-08-04 | PASS (image-gen block added this pass) |
| LLM | Make default / make small default from Actions; badges move | /settings/models | POST /api/llm/models/{id}/set_default | test_rbac_llm_models (API) | 2026-08-04 | PASS |
| LLM | Delete a model (Actions → Delete → confirm, or card → Delete → inline confirm); row disappears; default model cannot be deleted | /settings/models | DELETE /api/llm/models/{id} | none | 2026-08-04 | PASS |
| LLM | Delete a provider from Danger Zone; its chip and models disappear; blocked while it holds the default model | /settings/models | DELETE /api/llm/providers/{id} | none | 2026-08-04 | PASS |
| LLM | Auto router toggle shows Routing column, seeds hints, guidance editable per model | /settings/models | PUT /api/organization/settings, POST /api/llm/models/{id}/routing_hint | none | 2026-08-04 | PASS |
| LLM | LLM fallback toggle shows Fallback column + chain; priorities editable | /settings/models | POST /api/llm/fallback_order | none | 2026-08-04 | PASS |
| LLM | Member (no manage_llm) is redirected away from /settings/models | /settings/models | — | none | 2026-08-04 | PASS |
| LLM | Per-model access modal opens from Access column (EE) | /settings/models | GET/POST /api/llm/models/{id}/access | test_rbac_llm_models (API) | 2026-08-04 | PASS (modal open only; grant editing not exercised) |

## Other areas (screens enumerated, flows not yet mapped)

From `frontend/pages/**`: home/reports (`/`, `/reports/[id]`), agents
(`/agents/**`), dashboards, automations (`/automations`, scheduled prompts),
prompts, queries, monitoring, projects, settings (access/members, AI settings,
PII, general, channels, audit, identity provider, SMTP, license,
instructions, integrations), onboarding wizard (`/onboarding/**`), auth
(`/users/sign-in`, `/users/sign-up`, verify), share links (`/r/`, `/c/`).
A future full QA pass should expand these into behavioral flow rows.
