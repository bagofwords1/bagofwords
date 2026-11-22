# Feedback Loop — "Each group runs its own models and provider, without an org LLM admin"

An enterprise customer has two groups, each with its own lead, and two LLM
providers. Finance may use provider A, Marketing provider B. Each lead wants to
run their own team's models: pick what the team starts on, look after their
provider (key, which models are on, routing hints), and use the Auto router.
Before this change every one of those actions needed org-wide `manage_llm`.

Decided with the product owner (simplest version that covers the story):

| Action | Org LLM admin | Provider manager (own provider) | Group manager (own group) | Member |
|---|---|---|---|---|
| Provider name, API key, connection settings | ✓ | ✓ (audited) | ✗ | ✗ |
| Turn the provider's models on or off | ✓ | ✓ — a model they turn on **starts restricted** | ✗ | ✗ |
| Model settings (temperature, context, reasoning, vision, decision settings) | ✓ | ✓ | ✗ | ✗ |
| Routing hints on the provider's models | ✓ | ✓ (audited) | ✗ | ✗ |
| Group's default and small default model | ✓ (any model) | ✗ | ✓ — **only models the group can already use** | ✗ |
| Group priority | ✓ | ✗ | ✗ | ✗ |
| Add/delete providers or models, pricing, org defaults, model access, fallback order, Auto router on/off and method, naming provider managers | ✓ | ✗ | ✗ | ✗ |
| Turn off a provider that holds the org default, small default or a group default | ✓ | ✗ | — | — |

- **Routing has no per-group switch.** The Auto router is one org setting; it
  only ever offers a user the models they may use, so each group routes within
  its own provider by model access alone. A group that should not route gets
  access only to models without routing hints. (An earlier per-group switch,
  PR #1875, was closed in favour of this.)
- **Nobody but an org admin can give anyone a model they couldn't already use.**
  That is why a provider manager's newly enabled model starts restricted, and
  why a group manager's choices exclude restricted models not granted to the
  group (a group default grants `use`, so an open choice would be a back door).
- **Default:** nobody is a provider manager after upgrade; group managers gain
  only the default-model pickers for their own groups.

## Root causes / gaps (validated)

- **The decision model ignored model access.** `decide_starting_model`
  (`backend/app/ai/model_router.py`) and the MCP tool-safety judge
  (`backend/app/ai/tools/implementations/execute_mcp.py`, `_mode_judge`) loaded
  the org's decision model with `_load_decision_model`, which has no user. With
  one org-wide decision model restricted to Finance, every Marketing request and
  tool call was sent to provider A. The routing *candidates* were already
  filtered per user (`resolve_routing_candidates` → `user_can_use_model`), but
  an access-check error kept a restricted candidate.
- **All LLM administration was one org permission.** Every route in
  `backend/app/routes/llm.py` required `manage_llm`, including the group
  defaults routes; there was no per-provider grant.
- **The LLM page gated every control on `manage_llm_settings`**, a permission
  the backend never had (listed in `KNOWN_STALE_FRONTEND_PERMISSIONS`), so only
  full admins ever saw a control — an org-wide `manage_llm` holder got a
  read-only page.
- **The settings table hid restricted models from non-full-admin LLM
  managers.** It used the per-user chat list (`GET /llm/models`), which filters
  by access.
- **Toggling a provider on or off returned 500 for everyone**
  (`LLMService.toggle_provider`: joined eager load without `Result.unique()`).

## Design

- **Decision model:** `model_router.user_may_use_model` (fails open when access
  control is off; on an access-check error keeps an open model and skips a
  restricted one). `decide_starting_model(..., user=)` returns
  `decision_model_not_allowed` and the run starts small; the agent passes the
  head completion's user. The MCP judge uses the small model instead. The same
  helper now drops restricted candidates on an access-check error.
- **Group managers:** the group defaults routes use the `view_members` door
  (baseline, binds to the org) and `_group_llm_scope`: `manage_llm` → every
  group; otherwise the groups the caller manages (`managed_group_ids` is the
  source of truth) → `as_manager=True`; anyone else → audited 403.
  `LLMService.set_group_defaults(as_manager=True)` refuses a priority change
  (`llm.group_default_priority_admin_only`) and a model the group can't use
  without the default (`llm.group_default_not_available`); keeping the current
  value is allowed. "Can use without the default" is
  `permission_resolver.group_granted_model_ids`: open models, plus `use`
  grants on the group or on roles assigned to the group (a full-admin role on
  the group means every model). New `GET /llm/group-defaults/{id}/choices`.
- **Provider managers:** a `resource_grants` row (`resource_type
  "llm_provider"`, principal = one user, `["manage"]`), written only by
  `PUT/DELETE /llm/providers/{id}/managers/{user}` (org `manage_llm`, target
  must be a live org member). The generic grant and role routes refuse the type
  (422), like group-manager grants. `RESOURCE_PERM_IMPLIES["llm_provider"]` =
  `manage ⇒ manage_llm`, so provider-scoped routes declare
  `@requires_permission('manage_llm', resource_scoped=True)` and then name the
  provider with `_require_provider_manage` / `_require_model_manage`. Deleting
  the provider deletes its grants; leaving the org deletes all of a user's
  grants (`revoke_member_org_access`).
  - `toggle_model(as_manager)` / the provider dialog: enabling sets
    `is_restricted`. `toggle_provider(as_manager)` refuses to turn off a
    provider holding an org default (`llm.provider_holds_default`) or a group
    default. The provider dialog refuses new models and price changes for
    managers (`llm.provider_manager_admin_only`).
  - `GET /llm/models?manage=true` is the settings listing: every model for an
    LLM admin, the managed providers' models for a provider manager (no access
    filter), 403 for anyone else. A manager's chat picker is unchanged.
  - Audit: `llm_provider.manager_added/removed`; `llm_provider.updated` records
    `as_provider_manager`, `key_rotated` (not `credentials_*` — the audit
    sanitizer blanks credential-named keys) and `settings_changed` (key names
    only); `llm_model.toggled` records `restricted_on_enable`; every refusal
    logs `<action>.denied`.
- **Frontend:**
  - `LLMsComponent.vue`: `isLlmAdmin` (`manage_llm`) for org-wide controls,
    `canManageProvider(id)` for per-provider ones (replaces
    `manage_llm_settings`, now off the stale list); "You manage …" banner; lock
    marker on restricted models when the Access column is hidden.
  - `LLMModelCardModal.vue` / `LLMProviderModalComponent.vue`: `orgAdmin` prop
    hides default/delete/add-model/danger zone and makes prices read-only.
  - `LLMProviderManagers.vue`: the Managers section in the provider dialog
    (admins).
  - `GroupModelDefaults.vue`: in the Groups members dialog, for LLM admins and
    the group's managers.
  - `AutoRouterModal.vue`: warns when the decision model is restricted.
  - Navigation: the LLM page and the Admin entry open for provider managers
    (`useAdminNav` `resourceType`, `layouts/default.vue`,
    `pages/settings/models.vue` `anyOf`).
  - Strings in all 10 catalogs.

## Loop A — deterministic (no external services)

```sh
cd backend
export TESTING=true BOW_DATABASE_URL="sqlite:///db/app.db"
uv run pytest tests/e2e/rbac/test_decision_model_access.py \
  tests/e2e/rbac/test_group_manager_llm_defaults.py \
  tests/e2e/rbac/test_llm_provider_managers.py -m e2e --db=sqlite -q
```

- `test_decision_model_access.py` (7): allowed member is routed by the decision
  model; Marketing's prompt never reaches Finance's decision model
  (`decision_model_not_allowed`, 0 calls to the fake decision server); opening
  it routes everyone; access-check errors skip restricted decision models and
  candidates only; the MCP judge uses the small model for Marketing and the
  decision model for Finance. All 7 fail with the fix reverted.
- `test_group_manager_llm_defaults.py` (18): scoped list and choices (role
  grants count), set/clear, refusals (other provider's model, priority, other
  group, members), admin-chosen value kept but not re-picked, role ends with
  demotion/leaving, admin unchanged, audit.
- `test_llm_provider_managers.py` (41): naming managers (admin only, members
  only, generic routes refuse), scoped listings and session grant, credential
  rotation audited without values, restrict-on-enable (toggle and dialog), every
  allowed model setting and its cross-provider refusal, test-connection scoping,
  14 org-wide routes refused, no new models/prices through the dialog, can't
  turn off a provider others start on, role ends with provider deletion or
  leaving the org, audit, the provider toggle regression.

**Mutation checks** (each applied alone, then reverted):

| Mutation | Tests that go red |
|---|---|
| Group manager may pick any model | 4 |
| No restrict-on-enable | 4 |
| A provider manager may touch any provider's model | 6 |
| Provider dialog unchecked for managers | 1 |
| Decision model / judge access check removed | 7 |

## Loop B — live app, real HTTP and real browser (fake EE license, no LLM)

```sh
cd backend
uv run python ../tools/agent/llm_delegation_live.py serve &   # :8000
uv run python ../tools/agent/llm_delegation_live.py seed
(cd ../frontend && yarn dev) &                                 # :3000
uv run python ../tools/agent/llm_delegation_live.py check      # what each person gets
uv run python ../tools/agent/llm_delegation_live.py attack     # 33 HTTP rows
cd ../frontend && node ../tools/agent/llm_delegation_ui.cjs flow <outDir> [he]
```

**`check` after seeding** — each group sees only its own provider's models;
Bianca (both groups) sees both; Nate (no group) only the shared ones:

```
alice  chat: Haiku 5.5, Sonnet 5.5, GPT-6 Luna, GPT-6 Sol   manages: OpenAI (Finance), group Finance
fiona  chat: Haiku 5.5, Sonnet 5.5, GPT-6 Luna, GPT-6 Sol
bob    chat: Haiku 5.5, Sonnet 5.5, Gemini 3.6 Flash, Gemini 4 Argon   manages: Gemini (Marketing), group Marketing
mark   chat: Haiku 5.5, Sonnet 5.5, Gemini 3.6 Flash, Gemini 4 Argon
bianca chat: all six
nate   chat: Haiku 5.5, Sonnet 5.5
```

**Attack table**, real sessions — 33/33 as expected:

```
PASS  alice lists providers / her models / hints GPT-6 Sol / sets temperature / rotates key   200
PASS  alice hints, turns on, edits, turns off Bob's provider or its models                  403
PASS  alice makes herself Bob's provider manager                                            403
PASS  alice: org default, open to everyone, grant nate, zero price, delete/create provider,
      fallback order, Auto router off                                                       403
PASS  alice / admin mint an llm_provider grant through the generic route                    422
PASS  alice lists group defaults (Finance only), sets Finance → GPT-6 Sol / GPT-6 Luna       200
PASS  alice gives Finance Gemini 4 Argon / bumps priority / edits Marketing                 403
PASS  bob sets Marketing → Gemini 4 Argon / 3.6 Flash; reads Finance's choices              200 / 403
PASS  fiona / nate: group defaults, manage listing, hints, providers                        403
PASS  alice turns on GPT-6.1 Terra → enabled and restricted                                 200
```

**Browser flow** (`llm_delegation_ui.cjs`, 25 checks, en and he both 25/25):

```
PASS  alice: Admin entry opens the LLM page; sees only OpenAI (Finance); every row is hers
PASS  alice: banner; no Add provider/model, no Access column, router switch locked, no group table
PASS  alice: saved a routing hint on GPT-6 Sol in the table
PASS  alice: model card has no Make default / Delete; price read-only
PASS  alice: provider dialog has no Add Custom Model / Danger Zone / Managers
PASS  alice: Finance choices are Finance's models (no Gemini); set default to GPT-6 Luna
PASS  bob: sees only Gemini (Marketing)
PASS  fiona: no Admin entry; /settings/models sends her away
PASS  admin: Gemini lists Bob; added and removed Mark; router dialog warns decision model is restricted
```

Before the change (frontend stashed, same backend): Alice's Admin entry opens
Groups, `/settings/models` sends her home, and the Finance dialog has no
default-model section.

Evidence: `media/pr/claude-affectionate-franklin-0huizc/` (`before-*`,
`after-*`, Hebrew `*-he`).

## What this proves / regression notes

- The rule "only an org admin widens model access" holds on every write path a
  delegate can reach: the toggle route, the provider dialog, group defaults, and
  the generic grant/role routes.
- The settings listing and the chat list are now separate: managing a provider
  never puts restricted models in the manager's own chat picker.
- Not run here: the PostgreSQL leg (no Docker). No migration is involved —
  provider managers reuse `resource_grants`.
- Follow-ups, not done here: per-group decision models (only if asked); a
  managers column on the provider chips; letting group managers set their
  group's priority within a band.
