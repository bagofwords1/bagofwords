# Row-level security on custom queries — design

Phase 2 of custom queries. A custom query is materialized once with a shared
credential, so every agent that activates it sees the same rows. RLS makes that
copy safe to expose to people who should each see a slice of it, by filtering
at read time against **who is asking**.

Status: design. Depends on the custom-queries beta (`enable_custom_queries`).

Licensing: RLS is an **enterprise** feature (`rls` in `TIER_FEATURES`), while
query acceleration itself stays community. The gate sits on *authoring* —
enabling or editing a policy, the options endpoint, preview-as-user — not on
*enforcement*: a saved policy keeps filtering even if the license lapses
(fail closed, never wider), and disabling a policy needs no license so a
lapsed org isn't stuck behind its own filter.

## Why this is the unlock, not a nice-to-have

Custom queries are gated on `auth_policy == 'system_only'` today. That excludes
exactly the governed, enterprise accounts most likely to have a source that
can't take the load — the ones using per-user credentials precisely because row
visibility differs per person. RLS moves enforcement from the source's
credential to BOW, which is what lets `user_required` connections participate.

It is also valuable **without** acceleration: plenty of orgs connect through one
service account to a source with no row-level security at all, and would want
BOW-level row policies on live queries too. Scoping it as "RLS on relations"
rather than "RLS on cached relations" is the difference between a workaround and
a governance feature.

## What we already have to build on

Nothing here needs a new identity system — four surfaces already exist:

| Surface | Where | What it gives a predicate |
|---|---|---|
| **OIDC / Entra groups** | `groups` + `group_memberships`, synced from token claims by `ee/oidc/group_sync_service.sync_user_oidc_groups`; `Group.external_id` / `external_provider` keep the IdP's ID | group membership by name or external ID |
| **Entra profile attributes** | `Membership.profile_attributes` (JSON), synced on login from Graph `/me` — `jobTitle`, `department`, `companyName`, `officeLocation` | department / region / cost-centre style scoping |
| **Roles** | `roles` + `role_assignments` (`principal_type` is user **or group**) | role-based slices, and a natural "sees everything" escape hatch |
| **Org membership** | `Membership.role`, `email` | the identity the predicate binds to |

`profile_attributes` is the single most useful one: it is already per-user,
per-org, admin-curated (the org picks which attributes sync), and already
rendered into `<user_profile>` — so an admin can see exactly what values exist
before writing a rule against them.

## Model

RLS is defined per relation, on the `ConnectionTable` row that already carries
`kind='bow'`:

```
rls_enabled        bool     default false
rls_mode           String   'attribute' | 'sql'      -- how the predicate is written
rls_policy         JSON     the rule (shape below)
rls_default_deny   bool     default true             -- unresolved attribute → no rows
```

`rls_mode='attribute'` is the default and covers the common case without SQL:

```json
{
  "column": "region",
  "source": "profile.officeLocation",
  "op": "in",
  "grants": [
    {"principal_type": "group", "principal_id": "<group uuid>", "values": ["*"]},
    {"principal_type": "role",  "principal_id": "<role uuid>",  "values": ["EMEA","APAC"]}
  ]
}
```

`rls_mode='sql'` is the escape hatch — a boolean expression over the relation's
columns and bound identity parameters:

```sql
region = :user.profile.officeLocation
  OR :user.in_group('finance-admins')
```

Both compile to the same thing: a WHERE clause plus a bound parameter map.

## Enforcement — a per-session catalog, not a rewrite

Do **not** append a predicate to agent-generated SQL. Generated SQL is
arbitrary; subqueries, CTEs and unions give a dozen ways to miss one.

`FastQueryClient.connect()` already builds a throwaway in-memory DuckDB per
call, attaches artifacts read-only and registers one view per relation. RLS
slots in there: register the **filtered** view and never name the raw one.

```sql
ATTACH '<artifact>' AS a0 (ENCRYPTION_KEY '…', READ_ONLY);
CREATE VIEW revenue_summary AS
  SELECT <accessible columns> FROM a0.revenue_summary
  WHERE region = $region;            -- bound, never interpolated
SET enable_external_access = false;
SET lock_configuration = true;
```

The unfiltered relation is not in the session catalog, so there is nothing to
bypass — the same structural property that already makes an unactivated relation
unnameable. Column projection lands here too, which is also how the existing
`UserConnectionColumn` overlay should be honoured on a cached relation.

**Identity must be a required argument.** `construct_clients` currently accepts
`current_user=None` for trusted system contexts. With RLS live, a `None`
identity must mean *no rows*, not *all rows*, on any relation with
`rls_enabled`. Scheduled reports and other background paths then need an
explicit run-as identity — which is correct, and worth surfacing in the
schedule UI rather than inferring.

> The precedent to avoid: `org-row-limit-ignored-on-refresh.md` documents five
> services that built executors without `organization_settings`, so the row cap
> was silently skipped on every rerun path. The identical bug here **leaks rows
> instead of truncating them**. Every construction site must pass identity, and
> it must not have a permissive default.

## Authoring

A fourth tab in the custom query modal, alongside Query / Cache / Danger.

- **Attribute mode**: pick a column, pick an identity source (a
  `profile_attributes` key, a group, or a role), pick an operator. Attribute
  keys are offered from what the org actually syncs, with live sample values —
  the same affordance the Entra profile sync settings page already uses.
- **SQL mode**: an expression box with the bindable identity variables listed.
- **Preview as user** (required, not optional): pick a member, see the rows they
  would get and the effective view SQL. This is the single best safeguard, and
  it doubles as the conformance-check primitive below.
- Changing a policy is a security-relevant assertion: audit-log it with the
  author, as `connection.custom_query.rls_changed`.

## Trust — the part that decides whether anyone adopts it

Writing a predicate makes BOW the RLS authority for that relation. The danger
isn't day one; it's month six, when the customer changes the source policy and
the predicate doesn't follow.

**Conformance check.** For a sample of users, run the relation live under their
own credentials and diff the row set against what the policy returns from the
cached copy. Divergence fails the check and disables acceleration for that
relation until an admin reviews it. Runs on a schedule, and the result is
something an auditor can be shown. Without it, "we enforce RLS" is a claim; with
it, it's a test.

This is also what makes `user_required` connections safe to unlock: the check
compares BOW's answer against the source's own answer for a real user.

## Phasing

1. **Attribute mode + filtered catalog + preview-as-user.** Covers
   department/region scoping from `profile_attributes` and group membership,
   which is most of the demand, with no SQL to get wrong.
2. **SQL mode**, once the attribute vocabulary shows where it's insufficient.
3. **Conformance check**, then unlock `user_required`.
4. **Per-group materialization** for sources whose policies can't be expressed
   as a predicate at all: one artifact per RLS role, extracted with that role's
   credential, users routed by group membership. Storage scales with role count,
   not user count, so a dozen roles is tractable.

## What this deliberately does not do

- **Not a substitute for source RLS.** Where the source enforces per-user
  visibility and the org wants that to remain the authority, the answer stays
  `per_user`: no shared copy, no acceleration.
- **Not row-level security on the file.** The artifact on disk still holds every
  row; filtering happens at read. Encryption at rest and container access
  control carry real weight here — which is already true and already the reason
  artifacts are encrypted rather than Parquet.
- **No per-user artifacts.** One copy per relation (or per role in phase 4).
  Per-user copies defeat the point of caching.

## Open questions

- **Stale attributes.** `profile_attributes` syncs on login. A user who changes
  department keeps the old value until they sign in again — a predicate bound to
  it is stale in the meantime. Do we re-sync on a schedule, or bound the policy's
  trust to a max attribute age?
- **Group claim size.** Entra omits the `groups` claim above a threshold and
  falls back to a Graph call. `group_sync_service` handles this for sync; a
  predicate evaluated at query time must not depend on a claim that may be
  absent.
- **Multi-connection agents.** A relation is per connection, but a user's
  identity is per org. Nothing blocks that, but the preview-as-user UI has to be
  clear about which relation it is previewing.
