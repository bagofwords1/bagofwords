# LLM Model Access Control (Per-Model RBAC)

Status: Proposed
Scope: Enterprise (EE)
Author: Design doc
Related: `app/core/permission_resolver.py`, `app/models/resource_grant.py`, `app/ee/license.py`

---

## 1. Problem

Today, LLM model access is **organization-scoped only**. Any verified member of an
organization can use every enabled model. The `manage_llm` permission gates only
*administration* of providers/models (CRUD, toggling, setting defaults) — it does
**not** gate *usage*.

Enterprises need to restrict which users/groups can use which models. Examples:

- "Finance can use Anthropic Sonnet; Engineering can use GPT."
- "Only the security team may use the expensive frontier model."

We want to grant **specific users or groups** access to **specific models**.

## 2. Decisions

These were settled during design and are fixed for v1:

| # | Decision | Value |
|---|----------|-------|
| D1 | Granularity | **Per model** (not per provider). A grant targets a single `LLMModel`. |
| D2 | Default visibility | **Open by default.** A model is usable by everyone unless explicitly marked restricted. |
| D3 | Default models | The org **default** and **small-default** models (big or small) are **always accessible to every member**, regardless of restriction. |
| D4 | Principal types | A model's access list may contain **users, groups, or roles** (same as data-source grants). "Role" is a principal you grant to — not the organizing unit of the feature. |
| D5 | Licensing | This is an **Enterprise** feature, gated by a new license feature flag `llm_access_control`. Community/unlicensed installs behave exactly as today (all models open). |

Open policy questions are tracked in [§8](#8-open-questions); v1 ships with the
recommended defaults noted there.

## 3. Approach (summary)

Reuse the existing RBAC primitives rather than introducing a new authorization
mechanism:

- Store access as `ResourceGrant` rows with `resource_type = "llm_model"`,
  `permissions = ["use"]`. This is the same generic grant table and resolver
  already used for data sources.
- Add a per-model `is_restricted` boolean (default `False`). When `False`, the
  model is open to all members (today's behavior). When `True`, only principals
  with a grant (plus full admins and the org defaults) may use it.
- Enforce at the **two** points where models are surfaced and chosen: the
  listing path (`get_models`) **and** the selection path (`get_model_by_id`,
  used by the completion service). The selection path is the security boundary —
  filtering only the list would let a user pass a `model_id` directly and bypass
  it.
- Gate the **write/configuration** surface behind the EE license. The enforcement
  read path is always on but is a no-op when nothing is restricted, so community
  behavior is unchanged.

```
ResourceGrant(resource_type="llm_model", resource_id=<model.id>,
              principal_type=user|group|role, principal_id=<id>,
              permissions=["use"])
                         │
                         ▼
resolve_permissions() ──► get_accessible_model_ids(user, org) ──► (is_admin, [model_ids])
                         │
        ┌────────────────┴─────────────────┐
        ▼                                   ▼
get_models()  (list / picker)        get_model_by_id()  (completion)
   filter to accessible                 reject if not accessible
```

## 4. Data model

### 4.1 `LLMModel.is_restricted`

Add a column to `app/models/llm_model.py`:

```python
is_restricted = Column(Boolean, nullable=False, default=False)
```

- `False` → open to all org members (current behavior).
- `True` → usable only by full admins, the org default/small-default bypass (D3),
  and principals holding a `use` grant.

Alembic migration: add the column with `server_default="0"` / `false` so existing
rows backfill as open. No data migration of grants is needed (open-by-default).

### 4.2 `ResourceGrant` (no schema change)

`app/models/resource_grant.py` is already generic over `resource_type`. We extend
the set of valid values to include `"llm_model"`. The doc comment on
`resource_type` should be updated:

```python
resource_type = Column(String, nullable=False)  # "data_source" | "connection" | "llm_model"
```

Grant shape for this feature:

```
resource_type  = "llm_model"
resource_id    = <LLMModel.id>
principal_type = "user" | "group" | "role"
principal_id   = <id>
permissions    = ["use"]
```

The existing `uq_resource_grant` unique constraint
(`resource_type, resource_id, principal_type, principal_id`) already prevents
duplicate grants.

## 5. Backend

### 5.1 Resolver helper

Add to `app/core/permission_resolver.py`, mirroring
`get_accessible_data_source_ids`:

```python
async def get_accessible_model_ids(
    db: AsyncSession, user_id: str, org_id: str,
) -> tuple[bool, list[str]]:
    """Returns (is_admin, model_ids_granted_to_user).

    is_admin=True (full_admin_access) means callers should not filter.
    model_ids are LLMModel ids the user holds an explicit `use` grant for
    (direct, via group, or via role). Does NOT include unrestricted models
    or org defaults — those are handled by the caller / accessibility check.
    """
    resolved = await resolve_permissions(db, str(user_id), str(org_id))
    if FULL_ADMIN in resolved.org_permissions:
        return True, []
    return False, [
        rid for (rtype, rid), perms in resolved.resource_permissions.items()
        if rtype == "llm_model" and "use" in perms
    ]
```

And a single-model capability check used by the completion path:

```python
async def user_can_use_model(db, user_id, org_id, model) -> bool:
    if not model.is_restricted:
        return True
    if model.is_default or model.is_small_default:   # D3: defaults always open
        return True
    is_admin, granted = await get_accessible_model_ids(db, user_id, org_id)
    return is_admin or str(model.id) in set(granted)
```

The resolver already unions grants across direct/user/group/role assignments and
caches per request (`get_resolved_permissions`), so no new caching is needed.

### 5.2 Enforcement points

Both live in `app/services/llm_service.py`:

1. **`get_models(...)`** — after loading the org's enabled models, filter to:

   ```
   {m for m in models if not m.is_restricted
                       or m.is_default or m.is_small_default
                       or str(m.id) in granted_ids
                       or is_admin}
   ```

   This drives the `/llm/models` API and therefore the model picker UI.

2. **`get_model_by_id(...)`** — after the org-scoped lookup, call
   `user_can_use_model(...)` and return `None` (or raise 403) if the user lacks
   access. **This is the security boundary** — the completion service
   (`completion_service.py`) selects models by id and must not be bypassable via
   a direct `model_id`.

3. **Default fallback** — the completion service falls back to
   `get_default_model(...)` when no `model_id` is supplied. Per D3 the default and
   small-default are always accessible, so the existing fallback remains valid for
   every user. No change needed beyond ensuring `user_can_use_model` honors the
   default bypass.

### 5.3 Routes (grant management)

Reuse the **existing** generic resource-grant endpoints in `app/routes/rbac.py`:

- `GET    /organizations/{org}/resource-grants?resource_type=llm_model&resource_id=<id>`
- `POST   /organizations/{org}/resource-grants`  (create `use` grant)
- `PUT    /organizations/{org}/resource-grants/{grant_id}`
- `DELETE /organizations/{org}/resource-grants/{grant_id}`

Add EE gating + a permission check on the LLM-model write paths. The cleanest
option is a thin set of model-scoped convenience endpoints under the LLM router
(parallel to the data-source `members` endpoints), e.g.:

```python
GET    /llm/models/{model_id}/access          # list principals + is_restricted
POST   /llm/models/{model_id}/access          # add user/group/role grant
DELETE /llm/models/{model_id}/access/{principal_id}
PUT    /llm/models/{model_id}/restricted      # toggle is_restricted
```

Each decorated:

```python
@require_enterprise(feature="llm_access_control")
@requires_permission("manage_llm")
async def ...
```

(Stacking matches the `custom_roles` + `manage_members` pattern in `rbac.py`.)
Internally these wrap the same `ResourceGrant` CRUD used by data sources, so the
resolver picks them up automatically.

### 5.4 Audit

Audit logging already exists for `llm_model.*` actions via `audit_service`. Add:

- `llm_model.access_granted` / `llm_model.access_revoked`
- `llm_model.restriction_changed`
- A denial log when a restricted model is requested without access (the resolver
  already audits resolution failures; this is an explicit allow/deny event).

## 6. Frontend

### 6.1 Model table — Access column

In `frontend/components/LLMsComponent.vue`, add one **Access** column that is a
summary + entry point (no inline editing):

| State | Cell |
|-------|------|
| Not restricted | badge **"Everyone"** |
| Restricted | chip **"3 users · 1 group"** |
| Default / small-default | **"Everyone (default)"**, locked |

### 6.2 Manage-access panel

Clicking the cell opens a slide-over / modal (denser than the data-source inline
expand in `AgentSettingsPanel.vue`, but the same building blocks):

- **"Restricted access"** toggle at top → `PUT /llm/models/{id}/restricted`.
  - Off = open to all (today).
  - On = only listed principals.
  - **Disabled** for default/small-default models, with hint *"Default models are
    available to all members."* (mirrors D3 server-side so admins aren't confused.)
- Principal list with **Add** (search users / groups / roles) and per-row remove,
  backed by `POST` / `DELETE /llm/models/{id}/access` (`permissions=["use"]`).

This reuses the existing principal-picker and grant-row UX from the data-source
members panel.

### 6.3 EE gating

Gate with the existing enterprise composable:

```ts
const { hasFeature } = useEnterprise()
const canManageModelAccess = computed(() => hasFeature('llm_access_control') && useCan('manage_llm'))
```

- Without the feature: the Access column renders read-only as **"Everyone"** (or
  is hidden), and the manage panel shows `UpgradeBanner.vue` linking to pricing.
- With the feature: full editing as above.

## 7. Licensing

Add the feature flag in `app/ee/license.py`:

- Add `"llm_access_control"` to the **enterprise** tier in `TIER_FEATURES`.
- Backend: `@require_enterprise(feature="llm_access_control")` on all model-access
  write/config endpoints. The enforcement *read* path (`get_accessible_model_ids`,
  the `get_models` filter, `get_model_by_id` check) stays always-on but is a no-op
  when no model is `is_restricted` and no grants exist — so community installs are
  unaffected.
- Frontend: `useEnterprise().hasFeature('llm_access_control')` as in §6.3.

## 8. Open questions

Tracked decisions that ship with a recommended default in v1:

1. **License expiry / downgrade behavior.** If an enterprise license lapses while
   models are restricted, do restricted models **fail open** (everyone regains
   access) or **fail closed** (admins only)?
   - **Recommendation: fail open**, and log it. Matches D2 (open by default) and
     avoids locking an org out of its own models on a billing lapse. Implement by
     having the enforcement path treat `is_restricted` as `False` when
     `has_feature("llm_access_control")` is `False`.

2. **Live revocation via role/group.** Because the resolver unions *live* grants,
   removing a user from a granting group/role (or deleting the grant) drops their
   model access **immediately** on the next request.
   - **Recommendation: keep this behavior** — it's consistent with how
     data-source grants already work and is the expected RBAC semantic.

3. **Restricting the current default.** D3 keeps defaults open to everyone, so
   marking a default model restricted has no usage effect until it stops being the
   default. v1 surfaces this by **disabling** the restriction toggle for default
   models (§6.2) rather than allowing a confusing no-op state.

## 9. Backward compatibility

- New column defaults to `is_restricted = False` → every existing model stays open.
- No license / community tier → write paths return 402, enforcement is a no-op,
  behavior identical to today.
- Existing `manage_llm` admins gain the new access-management UI; non-admins see no
  change.

## 10. Implementation checklist

- [ ] Migration: add `LLMModel.is_restricted` (`server_default false`).
- [ ] Update `resource_type` comment to include `"llm_model"`.
- [ ] `permission_resolver.py`: `get_accessible_model_ids`, `user_can_use_model`.
- [ ] `llm_service.get_models`: filter by accessibility.
- [ ] `llm_service.get_model_by_id`: enforce `user_can_use_model` (security boundary).
- [ ] Routes: `/llm/models/{id}/access` (GET/POST/DELETE) + `/restricted` (PUT),
      decorated with `@require_enterprise("llm_access_control")` + `@requires_permission("manage_llm")`.
- [ ] Audit events: granted / revoked / restriction_changed / denied.
- [ ] `app/ee/license.py`: add `"llm_access_control"` to enterprise `TIER_FEATURES`.
- [ ] Frontend `LLMsComponent.vue`: Access column + manage-access panel + EE gating.
- [ ] Tests: resolver unit (user/group/role grants, default bypass, admin bypass),
      enforcement on `get_model_by_id`, license-off no-op, expiry fail-open.
