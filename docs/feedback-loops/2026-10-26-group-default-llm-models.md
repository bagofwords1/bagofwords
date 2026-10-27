# Feedback Loop — "Group 1 and group 2 each get their own default model, and neither can use the other's"

Feature request: let each group have its own default LLM, allow restricting
models even when they are a default, so that group 1's default is invisible
and unusable for group 2 (and vice versa).

Before this change it was impossible: there was one org-wide default, the org
default (and small default) were always open to every member (design D3 in
`docs/design/llm-model-access-control.md`), and restricting a default was
refused (`llm_service.py` `set_model_restricted`, "Default models are
available to all members and cannot be restricted.").

## Root cause (validated)

- One default per org: `LLMService.get_default_model` read only the
  `LLMModel.is_default` / `is_small_default` flags; the chain was
  report model → personal default → org default.
- Defaults bypassed restriction: `permission_resolver.model_open_to_all`
  returned True for any `is_default`/`is_small_default` model, and
  `get_models` had the same bypass inline.
- No place to store a group's choice: `Group` had no model columns.

## Loop A — deterministic (no external services)

Seeded through the real API with the RBAC fixtures; providers use a dummy key
(provider creation does not call the LLM).

```bash
cd backend
export TESTING=true BOW_DATABASE_URL="sqlite:///db/app.db"
uv run pytest tests/e2e/rbac/test_group_default_llm_models.py -m e2e --db=sqlite -q
```

Cast: `g1` (group 1), `g2` (group 2), `both` (groups 1+2), `none` (no group);
models A (group 1 default), B (group 2 default) — both restricted with **no**
explicit grants — plus the org default and an open model C.

On `main` the file fails at fixture setup (`PUT /llm/group-defaults/...` → 404).
Mutation checks on this branch (each change applied alone, then reverted):

| Mutation | Tests that go red |
|---|---|
| Group default no longer grants `use` | 7 (each-group default, no cross-access, report fallback, both-groups, restricted org default, small default fallback, members-cannot-manage) |
| Old D3 rule (defaults always open) restored | 2 (restricted org default, restricted small default) |
| Group step skipped in resolution | 7 |
| Priority ignored | 1 (both-groups priority) |

After the change: **15 passed**. Related suites still green:
`test_rbac_llm_models.py`, `test_user_default_llm_model.py`,
`test_report_default_llm_model.py`, `test_sub_agent_model_access.py`,
`test_auto_model_routing.py` (61 passed) and `tests/unit/test_llm_fallback.py`
(26 passed). `test_cannot_restrict_default_model` was replaced by
`test_restricted_default_model_is_restricted` — the old assertion encoded the
rule this feature removes.

## Loop B — live app (real UI, fake EE license, no LLM calls)

```bash
cd backend
uv run python ../tools/agent/group_llm_defaults_live.py serve &   # :8000, EE license faked in-process
cd ../frontend && yarn dev &                                       # :3000
cd ../backend && uv run python ../tools/agent/group_llm_defaults_live.py seed
```

`seed` creates Finance (default Claude Opus 5.5) and Marketing (default
GPT-6 Sol), restricts both models, adds fiona@ (Finance), mark@ (Marketing),
bianca@ (both), nate@ (none) — password `Password123!` — and prints what each
user gets. Observed:

| User | Default | Can see Opus 5.5 | Can see GPT-6 Sol |
|---|---|---|---|
| fiona (Finance) | Claude Opus 5.5 (group) | yes | **no** |
| mark (Marketing) | GPT-6 Sol (group) | **no** | yes |
| bianca (both) | Claude Opus 5.5 (Finance wins the name tie) | yes | yes |
| nate (no group) | Claude Sonnet 5.5 (org) | no | no |

"Before" run: the same script against a `main` worktree with
`BOW_SANDBOX_PORT=8001 … seed --legacy` (group *grants* instead of group
defaults, since the feature doesn't exist there): every user gets the org
default, Claude Sonnet 5.5.

Then, as admin, restricting the org default (Claude Sonnet 5.5) in the access
modal: Fiona and Mark keep their group defaults, Nate falls back to the first
open model (Claude Haiku 5.5) and the Settings → LLM page warns
"Members without a default model: 1 — Nate NoGroup — falls back to Claude Haiku 5.5".

Evidence: `media/pr/feat-group-default-llm-models/`.

## The fix

- `backend/alembic/versions/grpllm01_group_default_llm_models.py`: group
  columns; un-restricts any model that was both restricted and an org default
  (keeps existing orgs' effective access unchanged).
- `app/core/permission_resolver.py`: group defaults add an implicit
  `llm_model` `use` grant (both resolvers); `model_open_to_all` = not restricted.
- `app/services/llm_service.py`: `resolve_inherited_default` (group → org →
  fallback, small chain), model-list markers, group-default CRUD, coverage
  report, delete/disable guards; restricting a default is allowed.
- `app/routes/llm.py`: `/llm/group-defaults`, `/llm/group-defaults/{id}`,
  `/llm/default-coverage`.
- Frontend: `LLMGroupDefaults.vue` (table + coverage warning),
  `LLMsComponent.vue` (badges, access label), `LLMModelAccessModal.vue`
  (default can be restricted, "Default for groups"), prompt boxes and profile
  picker preselect the inherited default. Strings in all 10 locale catalogs.

## What this proves / regression notes

- The completion path (`get_default_model_for_user`,
  `get_default_model_for_report`, `get_default_model(is_small=True)`) resolves
  per group, and the security boundary (`get_model_by_id`) rejects the other
  group's model with 403.
- Not run here: the Postgres leg (no Docker in this sandbox). The migration
  uses bound booleans and an idempotent column add, like its neighbours.
- Pre-existing, unrelated: the locale catalogs already had key drift before
  this change (e.g. `es`/`he` miss `onboarding.llm.vertex*`; `fr`/`sv`/… miss
  `agentsPage.queries`). The new keys were added to all 10 catalogs.
