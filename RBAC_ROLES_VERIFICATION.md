# RBAC Sandbox — Roles Verification & Bug Report

**Date:** 2026-08-08 · **Environment:** local sandbox (backend :8000, frontend :3000), enterprise license (`custom_roles`) enabled, LLM = Claude 4.5 Haiku.
**Method:** Every role was exercised by a **real, separately-registered user** logging into the real UI (Playwright, headless Chromium). Each user's own session token drove the exact endpoints the UI calls, so allow/deny is read from real HTTP status codes (403 = denied, 2xx/4xx-non-403 = permission passed), and corroborated visually with screenshots and at the DB layer.

## What was set up (all via the UI)
- **2 built-in roles:** `admin` (full_admin_access), `member` (baseline).
- **3 custom roles:** `Agent A Manager` (resource grant `data_source.manage` on *Sales Agent*), `Connection A Manager` (resource grant `connection.manage_connection` on the *Sales* connection), `Global Builder` (org perms `create_data_source` + `manage_instructions`).
- **1 group:** `Analysts`, assigned the `Global Builder` role, with member `groupuser`.
- **6 real users**, one per scenario, plus the owner admin. Resources: 2 agents (Sales, Ops) each with its own connection.

## Verdict: RBAC enforcement is CORRECT

Every role could do exactly what it should and was denied exactly what it should not — across **global**, **per-agent**, **per-connection**, and **group-inherited** scopes. 3 issues were found around the *edges* (invite flow, error handling, UI listing), none of which is a privilege-escalation / access-control hole.

### Enforcement matrix (A = allowed, **D = denied**; all rows match the intended design)

| user (role) | list members | manage members | create agent | manage Sales agent | manage Ops agent | manage Sales conn | manage Ops conn | instr on Sales | instr on Ops | org manage connections |
|---|---|---|---|---|---|---|---|---|---|---|
| fulladmin (admin) | A | A | A | A | A | A | A | A | A | A |
| member (member) | A | **D** | **D** | **D** | **D** | **D** | **D** | **D** | **D** | **D** |
| agentmgr (Agent A Manager) | A | **D** | **D** | **A** | **D** | **D** | **D** | **A** | **D** | **D** |
| connmgr (Connection A Manager) | A | **D** | **D** | **D** | **D** | **A** | **D** | **D** | **D** | **D** |
| builder (Global Builder) | A | **D** | **A** | **D** | **D** | **D** | **D** | **A** | **A** | **D** |
| groupuser (member + Global Builder *via group*) | A | **D** | **A** | **D** | **D** | **D** | **D** | **A** | **A** | **D** |

Highlights proving fine-grained scoping:
- **agentmgr** manages *only Sales* (A) and is denied *Ops* (D); its `data_source.manage` grant does **not** leak to the connection layer (manage Sales conn = D).
- **connmgr** manages *only the Sales connection* (A), denied *Ops connection* (D), and its per-connection grant does **not** confer org-wide `manage_connections` (D).
- **builder** manages instructions on **both** agents (org-wide) but cannot fully *manage* an agent or connection — exactly what `manage_instructions` alone should give.
- **groupuser** inherits `Global Builder` purely through the **Analysts group** and behaves identically to builder — group→role→user inheritance works.

---

## Issues found

| # | Severity | Summary |
|---|---|---|
| BUG-1 | Medium | Creating a role with a duplicate name returns HTTP 500 (unhandled IntegrityError) instead of a clean 409. |
| BUG-2 | High | Inviting a *pending* user with a **custom** role silently drops the role after they register (they land on baseline `member`). |
| BUG-3 | Medium | A role with org-level `manage_instructions` can act on any agent via API/direct URL but sees **zero agents** in the Agents list, so the capability is UI-unreachable. |

---

## BUG-1 — Duplicate role name returns HTTP 500 instead of 409 (Medium)

**Where:** `POST /api/organizations/{org}/roles` (Settings → Access → Roles → New Role)

**Repro:** Create a custom role whose name already exists in the org (e.g. a second "Agent A Manager").

**Observed:** Server responds **500 Internal Server Error**. Backend log shows an unhandled DB error bubbling up:
```
sqlite3.IntegrityError: UNIQUE constraint failed: roles.organization_id, roles.name
sqlalchemy.exc.IntegrityError: (sqlite3.IntegrityError) UNIQUE constraint failed: roles.organization_id, roles.name
[SQL: INSERT INTO roles (organization_id, name, description, permissions, is_system, id, ...)]
```
The UI silently fails (no error toast; the modal stays and the role is not created).

**Expected:** A 409 Conflict (or 400) with a clear message like "A role named 'X' already exists", surfaced to the user in the modal. The unique constraint should be checked before insert / the IntegrityError caught and mapped.

**Impact:** Confusing UX; a 500 leaks an internal DB error and gives the operator no actionable feedback.

---

## BUG-2 — Inviting a pending user with a CUSTOM role silently drops the role (High)

**Where:** Invite flow — Settings → Access → Members → Add Member → pick a **custom** role in the "Role" dropdown → the invitee registers via the invite link.
Backend: `OrganizationService.add_member` + `UserManager._attach_open_memberships` (`app/core/auth.py:243-272`).

**Repro:**
1. As admin, invite `agentmgr@acme.io` and select the custom role **"Agent A Manager"** in the invite dropdown. (Members list then shows the role badge "Agent A Manager", Pending.)
2. The invitee registers through the invite link.
3. Inspect the invitee's effective permissions (`GET /api/users/whoami`).

**Observed:** The registered user has **no** custom role. `whoami` returns `roles: []`, `resource_permissions: {}`, and only the baseline member permissions. Verified for all three custom-role invitees:
- `agentmgr` (invited "Agent A Manager") → `roles: []`, `resource_permissions: {}` (no `manage` on Sales Agent).
- `connmgr` (invited "Connection A Manager") → `roles: []`, `resource_permissions: {}` (no `manage_connection`).
- `builder` (invited "Global Builder", an **org-level** role) → permissions are just the baseline; `create_data_source` / `manage_instructions` are **missing**.

**Root cause:** For a *pending* invite (`user_id is None`), `add_member` records the role **only** as the legacy `Membership.role` string and does **not** create a membership-principal `RoleAssignment` (the `_assign_system_role` call is gated on `user_id` being set). At registration, `_attach_open_memberships` reconstructs a `RoleAssignment` from `membership.role` **only when it matches a *system* role** (`Role.is_system == True`, `organization_id IS NULL`). A custom role name never matches, and `_materialize_pending_rbac` has nothing to rewrite, so the assignment is silently lost. The permission resolver's legacy fallback (`permission_resolver.py:371-383`) recognizes only `"admin"`/`"member"`, so the user lands on baseline member.

**Expected:** A user invited with a custom role holds that role after registering (proper `RoleAssignment` created), exactly as when the same role is assigned from the Members table to an already-registered user (which works — it POSTs `/role-assignments` by `role_id`).

**Impact:** High. Admins assigning custom roles at invite time get a silent, incorrect result — the member appears to have the role (badge shown) but has none of its permissions. Only re-assigning the role after registration fixes it.

**Workaround used for the rest of this run:** re-assign each custom role from the Members-table role dropdown after the user registered (this path assigns by `role_id` and works).

---

## BUG-3 — Org-level `manage_instructions` (etc.) grants the action but lists no agents in the UI (Medium)

**Where:** `GET /api/data_sources` filter (`data_source_service.get_data_sources`, `app/services/data_source_service.py:1354-1358`) vs. the instruction/entity/eval permission checks.

**Repro:**
1. Give a user only the org-level permission **`manage_instructions`** (custom role "Global Builder"), with no per-agent grant.
2. Log in as that user → open **Agents**.

**Observed:** The Agents list is **empty** — no agent is shown (`GET /data_sources` returns `[]`). Yet the same user can successfully create/manage instructions on any agent: `POST /instructions {data_source_ids:[<any agent>]}` → **200**, and opening the agent by **direct URL** (`/agents/<id>`) renders its instructions and lets them manage it. So the permission is enforced correctly server-side, but there is no way to reach any agent through normal UI navigation.

**Root cause:** `get_data_sources` lists only agents that are **public** or that the caller is an explicit **member of** (has a per-`data_source` resource grant). `show_all` is honored only for `full_admin_access` / `manage_connections`. Org-level `manage_instructions` / `manage_entities` / `manage_evals` put nothing in the list, so a role built around those permissions sees zero agents.

**Expected:** A user who can manage instructions/entities/evals org-wide should see the agents they can act on in the Agents list (or get the same "show all" affordance), so the capability is reachable without hand-crafting a URL.

**Impact:** Medium (usability / capability-reachability; not a security hole — enforcement is correct). A "Global Builder"-style role is functional via API but effectively unusable through the UI. May be partly by design (admins are deliberately not flooded), but the instruction-manager case looks like an oversight.

---
