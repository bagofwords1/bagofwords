# Roles and permissions — what to audit each page as

A page audited only as admin is a page audited in its easiest state. Most of this app's UI is
permission-gated, so a control's correctness is not "does it work" but "does it work, and is
it visible, for the right people".

## Contents

- [The matrix](#the-matrix) — where truth lives, and the built-in roles
- [Three layers of gating](#three-layers-of-gating) — org perms, resource grants, groups
- [The drift risk](#the-drift-risk-why-this-phase-exists) — why role auditing finds real bugs
- [Seeding roles](#seeding-the-roles-you-need) — verified recipes
- [What to compare](#what-to-compare-across-roles)

## The matrix

`backend/app/core/permission_resolver.py` is the authority. Do not infer the matrix from the
UI — the UI is the thing under test.

Two built-in system roles ship (`roles` rows with `organization_id IS NULL`, `is_system=1`):

| Role | Permissions |
|---|---|
| `admin` | `["full_admin_access"]` — the `FULL_ADMIN` wildcard, passes every org-level check |
| `member` | `view_reports`, `create_reports`, `update_reports`, `delete_reports`, `publish_reports`, `manage_files`, `view_members` |

Read them live rather than trusting this table, which will age:

```bash
cd backend && uv run python -c "
import sqlite3; [print(r) for r in sqlite3.connect('db/agent.db').execute(
  'select name, is_system, organization_id, permissions from roles order by is_system desc, name')]"
```

Orgs can also define **custom roles** (`is_system=0`, org-scoped) with any permission subset,
via `rbac_service`. A custom role holding exactly one permission is the sharpest audit
instrument available — it isolates a single gate.

## Three layers of gating

A user's effective permissions are the union of every role assigned directly or through a
group, then expanded by two implication tables. All four layers can hide or show a control:

1. **Org-level role permissions** — e.g. `manage_settings`, `manage_llm`, `view_audit_logs`.
   Routes declare these in `definePageMeta({ permissions: [...] })`.
2. **Resource grants** (`ResourceGrant`) — per data source / connection, e.g.
   `data_source:<id> → ["manage"]`. Routes declare these via `meta.resourcePermissionAny`.
3. **Groups** (`Group`, `GroupMembership`) — roles assigned to a group flow to its members.
   A user with no direct role can still hold permissions. Easy to forget when seeding.
4. **Implications** — `ORG_PERM_IMPLIES_RESOURCE` (holding `manage_instructions` at org level
   grants `manage_instructions` on *every* data source) and `RESOURCE_PERM_IMPLIES` (a
   `manage` grant on a data source is the owner tier, implying `manage_instructions`,
   `create_entities`, `manage_evals`, `manage_members`, `view`, `view_schema`).

The implication rules are where intuition fails. A user with no admin role and a single
`manage` grant on one agent sees most of that agent's management UI — and must see none of it
on any other agent. That asymmetry is worth a row in the expectations table.

## The drift risk (why this phase exists)

`frontend/composables/usePermissions.ts` **hand-mirrors** both implication tables from
`permission_resolver.py`. Its own comments say so: *"Mirror of backend
ORG_PERM_IMPLIES_RESOURCE in app/core/permission_resolver.py."*

Two hand-maintained copies of one matrix drift. When they do, you get one of two bugs, and
only a role-aware audit finds either:

- **Visible but forbidden** — the UI's copy is more generous, so it renders a control the
  backend rejects. The user clicks and gets a 403, usually silently. This is the serious
  direction: it looks like a broken button, and it is really a permission bug.
- **Hidden but permitted** — the UI's copy is stricter, so a capability the user legitimately
  holds is unreachable. Nobody files this one, because the affordance was never seen.

So when a control 403s for a non-admin, do not stop at "permission denied, working as
intended". Ask why the UI offered it at all — that is the finding.

## Seeding the roles you need

**Admin** comes from `seed_org.py` (`admin@example.com` / `Password123!`).

**Member** — `seed_org.py --invite` is currently broken: it posts
`{"email", "role_id"}` but `MembershipCreate`
(`backend/app/schemas/organization_schema.py:27-32`) requires `organization_id` and names the
field `role`, so the invite 422s. Until that is fixed, invite by hand:

```bash
cd backend && uv run python - <<'PY'
import httpx, sqlite3
c = httpx.Client(base_url="http://localhost:8000", timeout=30)
tok = c.post("/api/auth/jwt/login",
             data={"username": "admin@example.com", "password": "Password123!"}).json()["access_token"]
org = c.get("/api/organizations", headers={"Authorization": f"Bearer {tok}"}).json()[0]["id"]
H = {"Authorization": f"Bearer {tok}", "X-Organization-Id": org}
r = c.post(f"/api/organizations/{org}/members",
           json={"organization_id": org, "email": "member@example.com", "role": "member"}, headers=H)
print("invite:", r.status_code, r.text[:200])
row = sqlite3.connect("db/agent.db").execute(
    "select invite_token from memberships order by rowid desc limit 1").fetchone()
token = row[0] if row else None
print("register:", c.post("/api/auth/register", json={
    "email": "member@example.com", "password": "Password123!",
    "name": "Member", "invite_token": token}).status_code)
PY
```

**A custom role** isolating one permission — create it through `/api/organizations/{org}/roles`
(see `backend/app/routes/rbac.py`) and assign it instead of `member`. Use this when a finding
depends on exactly one gate; `member` holds seven permissions and confounds the result.

Then sweep as each identity:

```bash
BOW_EMAIL=member@example.com BOW_PASSWORD='Password123!' \
  node .agents/skills/ui-audit/scripts/sweep.mjs --routes /agents --login --out audit/agents-member
```

`--login` caches per `--out` directory, so give each role its own directory or the second run
silently reuses the first role's session — which produces a confident, entirely wrong role
comparison.

## What to compare across roles

Diff the control inventories, not the screenshots. For each control present for admin:

| Present for admin | Present for role X | Read as |
|---|---|---|
| yes | yes, and it works | Fine — but confirm X *should* have it |
| yes | yes, and it 403s | **Finding.** UI offered what the backend forbids |
| yes | no | Expected gating — confirm against the matrix, not against intuition |
| yes | yes, but disabled with no explanation | P3 at least: the user cannot tell whether it is broken or forbidden |

The reverse direction matters too: a control that appears for a **lower**-privileged role and
not for admin is almost always a bug in the gate's condition.

Record the role in every expectations row where the answer differs by role. A row that reads
the same for all roles does not need one.

### Reading the diff without being fooled

**Expect two false positives from the user menu.** Controls are matched by accessible name, so
the signed-in user's own name (`A Agent Admin` vs `M Member`) always appears as a difference in
both directions. Ignore that pair; it is the identity, not a gate.

**A big count difference is usually one modal, not many gates.** A page that auto-opens a
dialog for one role and not the other shifts dozens of controls at once. Find the dialog before
concluding the page is wildly different — and then ask why it opened, because "this role sees a
list filtered to empty, and empty is treated as *nothing exists yet*" is a bug pattern this app
repeats.

**The diff cannot see the worst case.** A control present for both roles that renders fine and
then 403s on click looks identical in both inventories. Only Phase 3, run as the lower role,
finds it. Treat the diff as a map of where to click, not as the result.
