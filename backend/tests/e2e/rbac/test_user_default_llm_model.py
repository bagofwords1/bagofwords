"""
E2E tests for the per-user default LLM model (memberships.default_llm_model_id).

Behavior under test:
- any member can set/clear their own default from the models they can use
  (`PUT /users/me/default_model`), and `GET /llm/models` marks it via
  `is_user_default` so clients preselect it;
- the preference is per-user (one user's default never leaks into another's
  model list);
- setting is strict about access (restricted models require a grant — user or
  group), while resolution is lenient: a preference that later became stale
  (model disabled, grant revoked) silently falls back to the org default.

Uses the RBAC cast fixtures; providers are created with a dummy key —
provider creation does not test the connection, so no LLM access is needed.
"""
import asyncio
import uuid

import pytest

from app.dependencies import async_session_maker


def _headers(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _run(coro):
    return asyncio.run(coro)


# ── helpers ──────────────────────────────────────────────────────────────


def _create_provider_with_models(test_client, token, org_id, n_models=3):
    suffix = uuid.uuid4().hex[:6]
    models = [
        {"model_id": f"claude-test-{suffix}-{i}", "name": f"Test Model {suffix} {i}", "is_custom": True}
        for i in range(n_models)
    ]
    resp = test_client.post(
        "/api/llm/providers",
        json={
            "name": f"prov-{suffix}",
            "provider_type": "anthropic",
            "credentials": {"api_key": "dummy-key"},
            "models": models,
        },
        headers=_headers(token, org_id),
    )
    assert resp.status_code == 200, resp.json()
    return resp.json()


def _list_models(test_client, token, org_id):
    resp = test_client.get("/api/llm/models", headers=_headers(token, org_id))
    assert resp.status_code == 200, resp.json()
    return resp.json()


def _get_default(test_client, token, org_id):
    resp = test_client.get("/api/users/me/default_model", headers=_headers(token, org_id))
    assert resp.status_code == 200, resp.json()
    return resp.json()


def _put_default(test_client, token, org_id, model_id):
    return test_client.put(
        "/api/users/me/default_model",
        json={"model_id": model_id},
        headers=_headers(token, org_id),
    )


def _restrict(test_client, token, org_id, model_id, value=True):
    return test_client.put(
        f"/api/llm/models/{model_id}/restricted",
        json={"is_restricted": value},
        headers=_headers(token, org_id),
    )


def _add_access(test_client, token, org_id, model_id, principal_type, principal_id):
    return test_client.post(
        f"/api/llm/models/{model_id}/access",
        json={"principal_type": principal_type, "principal_id": principal_id},
        headers=_headers(token, org_id),
    )


async def _resolve_for(org_id, user_id):
    """Resolve the effective default via the same service call chat uses."""
    from app.services.llm_service import LLMService
    from app.models.organization import Organization
    from app.models.user import User

    async with async_session_maker() as db:
        organization = await db.get(Organization, org_id)
        user = await db.get(User, user_id)
        return await LLMService().get_default_model_for_user(db, organization, user)


@pytest.fixture
def cast(test_client, bootstrap_admin, invite_user_to_org):
    """Admin + regular member in one org, plus a provider with 3 models."""
    admin = bootstrap_admin("userdef")
    member = invite_user_to_org(org_id=admin["org_id"], admin_token=admin["token"])
    _create_provider_with_models(test_client, admin["token"], admin["org_id"])
    models = _list_models(test_client, admin["token"], admin["org_id"])
    non_default = next(
        m for m in models if not m["is_default"] and not m["is_small_default"]
    )
    return {
        "admin": admin,
        "member": member,
        "org_id": admin["org_id"],
        "models": models,
        "org_default_id": next(m["id"] for m in models if m["is_default"]),
        "non_default_id": non_default["id"],
    }


# ── set / clear / marking ────────────────────────────────────────────────


@pytest.mark.e2e
def test_member_sets_and_clears_personal_default(test_client, cast):
    member_t, org = cast["member"]["token"], cast["org_id"]
    target = cast["non_default_id"]

    # No preference initially.
    assert _get_default(test_client, member_t, org)["model_id"] is None
    assert all(not m["is_user_default"] for m in _list_models(test_client, member_t, org))

    # Set → persisted and marked in the model list.
    resp = _put_default(test_client, member_t, org, target)
    assert resp.status_code == 200, resp.json()
    assert _get_default(test_client, member_t, org)["model_id"] == target
    marked = [m["id"] for m in _list_models(test_client, member_t, org) if m["is_user_default"]]
    assert marked == [target]

    # The effective default resolves to the preference.
    resolved = _run(_resolve_for(org, cast["member"]["user_id"]))
    assert str(resolved.id) == target

    # Clear → falls back to org default everywhere.
    resp = _put_default(test_client, member_t, org, None)
    assert resp.status_code == 200, resp.json()
    assert _get_default(test_client, member_t, org)["model_id"] is None
    assert all(not m["is_user_default"] for m in _list_models(test_client, member_t, org))
    resolved = _run(_resolve_for(org, cast["member"]["user_id"]))
    assert str(resolved.id) == cast["org_default_id"]


@pytest.mark.e2e
def test_preference_is_per_user(test_client, cast):
    """One user's preference must not leak into another user's list/resolution."""
    member_t, admin_t, org = cast["member"]["token"], cast["admin"]["token"], cast["org_id"]
    target = cast["non_default_id"]

    assert _put_default(test_client, member_t, org, target).status_code == 200

    # Admin sees no personal default and still resolves to the org default.
    assert all(not m["is_user_default"] for m in _list_models(test_client, admin_t, org))
    resolved = _run(_resolve_for(org, cast["admin"]["user_id"]))
    assert str(resolved.id) == cast["org_default_id"]


@pytest.mark.e2e
def test_set_rejects_unknown_and_disabled_models(test_client, cast):
    member_t, admin_t, org = cast["member"]["token"], cast["admin"]["token"], cast["org_id"]

    assert _put_default(test_client, member_t, org, str(uuid.uuid4())).status_code == 404

    # Disable a non-default model, then try to pick it.
    target = cast["non_default_id"]
    resp = test_client.post(
        f"/api/llm/models/{target}/toggle?enabled=false", headers=_headers(admin_t, org)
    )
    assert resp.status_code == 200, resp.json()
    assert _put_default(test_client, member_t, org, target).status_code == 400


# ── access control (restricted models, group grants) ────────────────────


@pytest.mark.e2e
def test_restricted_model_requires_grant_group_grant_allows(
    enterprise_license, test_client, cast, create_group, add_user_to_group
):
    """A member cannot pick a restricted model as their default until a grant
    (here via group membership) makes it usable."""
    member, admin_t, org = cast["member"], cast["admin"]["token"], cast["org_id"]
    target = cast["non_default_id"]

    assert _restrict(test_client, admin_t, org, target, True).status_code == 200

    # No grant → cannot set as personal default.
    denied = _put_default(test_client, member["token"], org, target)
    assert denied.status_code == 403, denied.json()

    # Grant through a group → allowed.
    grp = create_group(name=f"grp-{uuid.uuid4().hex[:6]}", user_token=admin_t, org_id=org)
    group_id = grp.json()["id"] if hasattr(grp, "json") else grp["id"]
    add_user_to_group(group_id=group_id, user_id=member["user_id"], user_token=admin_t, org_id=org)
    assert _add_access(test_client, admin_t, org, target, "group", group_id).status_code == 200

    allowed = _put_default(test_client, member["token"], org, target)
    assert allowed.status_code == 200, allowed.json()
    assert _get_default(test_client, member["token"], org)["model_id"] == target
    resolved = _run(_resolve_for(org, member["user_id"]))
    assert str(resolved.id) == target


@pytest.mark.e2e
def test_stale_preference_falls_back_silently(
    enterprise_license, test_client, cast, create_group, add_user_to_group
):
    """A preference that later becomes unusable (model restricted after the
    fact, grant revoked) must fall back to the org default — never break chat
    and never leak a restricted model."""
    member, admin_t, org = cast["member"], cast["admin"]["token"], cast["org_id"]
    target = cast["non_default_id"]

    # Member picks an open model, then the admin restricts it with no grant.
    assert _put_default(test_client, member["token"], org, target).status_code == 200
    assert _restrict(test_client, admin_t, org, target, True).status_code == 200

    # Raw preference still stored, but resolution falls back...
    assert _get_default(test_client, member["token"], org)["model_id"] == target
    resolved = _run(_resolve_for(org, member["user_id"]))
    assert str(resolved.id) == cast["org_default_id"]
    # ...and the (now invisible) model is not marked in the member's list.
    member_models = _list_models(test_client, member["token"], org)
    assert target not in {m["id"] for m in member_models}
    assert all(not m["is_user_default"] for m in member_models)

    # Un-restricting makes the stored preference effective again — no re-set needed.
    assert _restrict(test_client, admin_t, org, target, False).status_code == 200
    resolved = _run(_resolve_for(org, member["user_id"]))
    assert str(resolved.id) == target
