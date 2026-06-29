# Sandbox Feedback Loop — Agent-manager RBAC (`manage` grant as a superset)

Validates the user stories behind giving the per-agent **`manage`** grant the
ability to fully manage *that* agent — so a non-admin who creates an agent (or
is granted `manage` on one) can edit its instructions, entities, evals, tables
and members, **scoped to that agent only**, without being able to touch other
agents or org-wide/global resources.

This is the runnable feedback loop used to confirm the behavior in a fresh
sandbox.

---

## User stories (the contract)

Setup: *an admin invites N users across M groups; a group is assigned a role
whose only permission is `create_data_source`, so its members can create
agents.* A member who creates an agent becomes its **owner** and receives a
per-resource `manage` grant on it (`data_source_service.create_data_source` →
`_create_memberships(..., permissions=["manage"])`).

1. A group member who creates an agent **can create and edit instructions on
   that agent**.
2. They **cannot** add org-wide **global** instructions, and **cannot** edit
   instructions on agents they don't manage.
3. A user scoped to specific agents **cannot see other agents**.
4. A user **can add entities** in an agent they manage.
5. A user who manages a specific agent **cannot see other agents in `/agents`**
   (the `/data_sources/active` selector backing that page).

Expanded (added during implementation):

6. Group members can create agents; a member **outside** the group cannot
   (no `create_data_source`).
7. Owning/managing an agent does **not** grant the org-level ability to create
   *new* agents — that stays the separate `create_data_source` role.
8. `manage` also covers the agent's **tables** and **membership**, scoped to
   that agent (denied on others).
9. A manager **cannot** author a **global entity** (no data source) — that
   stays an org-level (`create_entities`) capability.

---

## Root design (what the fix changes)

Before: a `manage` data-source grant was *not* a superset. The resolver matched
permissions exactly, so an agent owner holding only `manage` was **denied**
`manage_instructions` / `create_entities` / `manage_evals` on their own agent —
those required either a separate per-DS grant or an org-wide `manage_*` role.

After: a single, centralized implication makes `manage` the agent-manager tier.

- `app/core/permission_resolver.py`
  - `RESOURCE_PERM_IMPLIES = {"data_source": {"manage": {manage_instructions,
    create_entities, manage_evals, manage_members, view, view_schema}}}`.
  - `has_resource_permission` consults it (new tier, below the org-perm
    implications and above the explicit-grant check).
  - `has_any_resource_permission(perm)` — honours the same implication; used by
    the `resource_scoped` route pre-filter so an agent manager isn't rejected
    at the decorator door before the per-resource body check runs.
- `app/core/permissions_decorator.py`
  - The `resource_scoped` fallback now calls `has_any_resource_permission`
    instead of a raw literal membership scan.
  - New `require_org_permission(...)` helper.
- `app/routes/instruction.py`, `app/routes/entity.py`
  - The **global** create routes (`/instructions/global`, `/entities/global`)
    now require **org-level** `manage_instructions` / `create_entities` when no
    `data_source_ids` are given — closing the hole where a manager could author
    org-wide resources via the resource-scoped path (story 2 / 9).

Frontend mirror (so the UI shows the same affordances):

- `frontend/composables/usePermissions.ts` — mirrors `RESOURCE_PERM_IMPLIES`;
  `useCan` / `useCanAny` resolve `manage` ⇒ the management sub-permissions.
- `frontend/components/AgentSettingsPanel.vue` — a **"Your access"** block shows
  the current user's effective role on the agent (Admin / Owner / Manager /
  Member) and the capabilities `manage` confers; the **owner** is badged in the
  members list. (Org-settings members page already lists each member's roles in
  `MembersComponent.vue`.)

> Scoping guarantee preserved: `manage` only ever implies these on the **same**
> data source. It is NOT an org-wide permission — unlike the `manage_*` org
> roles, a manager is confined to their own agents. Stories 3/5 (list scoping)
> are unaffected by the implication and continue to hold.

---

## Environment setup (fresh sandbox)

The app targets **Python 3.12**.

```bash
cd backend
pip install uv
uv sync --frozen --extra dev

# Required by bow-config.dev.yaml (database.url: ${BOW_DATABASE_URL})
export BOW_DATABASE_URL="sqlite:///db/app.db"
mkdir -p db
```

Tests run on SQLite; the autouse `run_migrations` fixture builds the schema per
test (`tests/conftest.py`).

---

## Loop A — Resolver unit logic (fast, no HTTP)

Pure checks on the implication tiers — the quickest iteration point.

```bash
cd backend
export BOW_DATABASE_URL="sqlite:///db/app.db"
uv run --extra dev pytest tests/unit/test_permission_resolver.py -q
```

**Observed (PASS):** 18 passed.

> One unrelated test, `test_org_perm_implies_resource_map_targets_are_valid_resource_perms`,
> fails in this sandbox: `ORG_PERM_IMPLIES_RESOURCE` maps `manage_connections →
> connection`, but `RESOURCE_PERMISSIONS` defines no `connection` resource type.
> This is **pre-existing and unrelated** — it reproduces with this change
> stashed and is not part of the agent-manager work.

Iterate here: edit `RESOURCE_PERM_IMPLIES` / `has_resource_permission` and
re-run.

---

## Loop B — End-to-end user stories (HTTP, in-process SQLite)

Seeds the world (admin, group with a `create_data_source` role, two group
members m1/m2 who each create an agent, an outsider) and asserts every story.

```bash
cd backend
export BOW_DATABASE_URL="sqlite:///db/app.db"
uv run --extra dev pytest tests/e2e/rbac/test_rbac_agent_manager_stories.py -v
```

**Observed (PASS):** 7 passed —

```
test_group_members_can_create_agents                              # story 6
test_story1_manager_can_edit_own_agent_instructions               # story 1
test_story2_manager_cannot_add_global_or_edit_others              # story 2
test_story3_and_5_manager_only_sees_own_agents                    # stories 3 & 5
test_story4_manager_can_add_entities_to_own_agent_only            # stories 4 & 9
test_expansion_manager_can_edit_tables_and_members_of_own_agent   # story 8
test_expansion_manage_does_not_grant_create_agent                 # story 7
```

Regression guard (existing RBAC behavior must not change):

```bash
uv run --extra dev pytest \
  tests/e2e/rbac/test_rbac_instructions.py \
  tests/e2e/rbac/test_rbac_entities.py \
  tests/e2e/rbac/test_rbac_evals.py \
  tests/e2e/rbac/test_rbac_data_sources.py -q
```

**Observed (PASS):** 21 passed.

---

## Loop C — Frontend permission logic

The UI gating mirrors the backend. The implication algorithm is validated
directly (no Nuxt build required):

```bash
node -e '...'   # see commit; asserts manage ⇒ {manage_instructions, create_entities,
                # manage_evals, view_schema, manage}; view-only ⇏ management; explicit grants intact
```

**Observed (PASS):** ALL FRONTEND IMPLICATION CHECKS PASSED.

In the live app this surfaces as:
- An agent owner/manager sees the edit controls (instructions, entities, evals,
  tables, members) on agents they manage, and read-only on agents they don't.
- `/agents/[id]/settings` shows a **"Your access"** block (Admin / Owner /
  Manager / Member + capabilities) and badges the **Owner** in the member list.

---

## What this proves

- A `manage` grant now fully manages **its** agent — instructions, entities,
  evals, tables, members — for non-admin owners/managers (stories 1, 4, 8).
- That power is **scoped**: managers can't see or touch other agents (stories
  3, 5, 8) and can't create org-wide global instructions/entities (stories 2,
  9) or new agents without `create_data_source` (story 7).
- Existing per-DS-grant and org-level behavior is unchanged (21 regression
  tests pass).
