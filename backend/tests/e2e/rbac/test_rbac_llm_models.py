"""
E2E tests for per-model LLM access control (RBAC).

Covers the enterprise "restrict a model to specific users/groups/roles"
feature end-to-end through the HTTP API, plus the service-level security
boundary (`get_model_by_id`) and the fail-open behavior when the license
feature is absent.

Cast is built with the shared RBAC fixtures (bootstrap_admin,
invite_user_to_org, create_group, ...). Providers are created with a dummy
key — provider creation does not test the connection, so no network/LLM
access is needed.
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
    """Create an anthropic provider with N models (dummy key, no conn test)."""
    suffix = uuid.uuid4().hex[:6]
    models = [
        {"model_id": f"claude-test-{suffix}-{i}", "name": f"Test Model {i}", "is_custom": True}
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


def _model_ids_by_name(models):
    return {m["name"]: m["id"] for m in models}


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


def _get_access(test_client, token, org_id, model_id):
    return test_client.get(
        f"/api/llm/models/{model_id}/access", headers=_headers(token, org_id)
    )


def _list_by_principal(test_client, token, org_id, principal_type, principal_id):
    return test_client.get(
        f"/api/llm/model-access/by-principal",
        params={"principal_type": principal_type, "principal_id": principal_id},
        headers=_headers(token, org_id),
    )


@pytest.fixture
def cast(test_client, bootstrap_admin, invite_user_to_org):
    """Admin + a regular member in the same org, plus a provider w/ 3 models."""
    admin = bootstrap_admin("llmacl")
    member = invite_user_to_org(org_id=admin["org_id"], admin_token=admin["token"])
    _create_provider_with_models(test_client, admin["token"], admin["org_id"])
    models = _list_models(test_client, admin["token"], admin["org_id"])
    by_name = _model_ids_by_name(models)
    # A non-default model we can safely restrict.
    restrictable = next(
        m for m in models if not m["is_default"] and not m["is_small_default"]
    )
    return {
        "admin": admin,
        "member": member,
        "org_id": admin["org_id"],
        "models": models,
        "by_name": by_name,
        "restrictable_id": restrictable["id"],
    }


# ── visibility (GET /llm/models) ─────────────────────────────────────────


@pytest.mark.e2e
def test_unrestricted_model_visible_to_member(enterprise_license, test_client, cast):
    member_models = _list_models(test_client, cast["member"]["token"], cast["org_id"])
    ids = {m["id"] for m in member_models}
    assert cast["restrictable_id"] in ids
    # And nothing is restricted by default.
    assert all(m["is_restricted"] is False for m in member_models)


@pytest.mark.e2e
def test_restrict_hides_from_member_admin_still_sees(enterprise_license, test_client, cast):
    mid = cast["restrictable_id"]
    r = _restrict(test_client, cast["admin"]["token"], cast["org_id"], mid, True)
    assert r.status_code == 200, r.json()

    # Member no longer sees it.
    member_ids = {m["id"] for m in _list_models(test_client, cast["member"]["token"], cast["org_id"])}
    assert mid not in member_ids

    # Admin (full_admin_access) still sees it.
    admin_ids = {m["id"] for m in _list_models(test_client, cast["admin"]["token"], cast["org_id"])}
    assert mid in admin_ids


@pytest.mark.e2e
def test_user_grant_restores_and_revoke_removes(enterprise_license, test_client, cast):
    mid = cast["restrictable_id"]
    admin_t, org = cast["admin"]["token"], cast["org_id"]
    _restrict(test_client, admin_t, org, mid, True)

    # Grant the member directly.
    g = _add_access(test_client, admin_t, org, mid, "user", cast["member"]["user_id"])
    assert g.status_code == 200, g.json()
    grant_id = g.json()["grant_id"]

    member_ids = {m["id"] for m in _list_models(test_client, cast["member"]["token"], org)}
    assert mid in member_ids

    # Revoke.
    d = test_client.delete(
        f"/api/llm/models/{mid}/access/{grant_id}", headers=_headers(admin_t, org)
    )
    assert d.status_code == 200, d.text
    member_ids = {m["id"] for m in _list_models(test_client, cast["member"]["token"], org)}
    assert mid not in member_ids


@pytest.mark.e2e
def test_group_grant(enterprise_license, test_client, cast, create_group, add_user_to_group):
    mid = cast["restrictable_id"]
    admin_t, org = cast["admin"]["token"], cast["org_id"]
    _restrict(test_client, admin_t, org, mid, True)

    grp = create_group(name=f"grp-{uuid.uuid4().hex[:6]}", user_token=admin_t, org_id=org)
    group_id = grp.json()["id"] if hasattr(grp, "json") else grp["id"]
    add_user_to_group(group_id=group_id, user_id=cast["member"]["user_id"], user_token=admin_t, org_id=org)

    g = _add_access(test_client, admin_t, org, mid, "group", group_id)
    assert g.status_code == 200, g.json()

    member_ids = {m["id"] for m in _list_models(test_client, cast["member"]["token"], org)}
    assert mid in member_ids


@pytest.mark.e2e
def test_role_grant(enterprise_license, test_client, cast, get_system_role):
    mid = cast["restrictable_id"]
    admin_t, org = cast["admin"]["token"], cast["org_id"]
    _restrict(test_client, admin_t, org, mid, True)

    member_role = get_system_role("member", user_token=admin_t, org_id=org)
    g = _add_access(test_client, admin_t, org, mid, "role", member_role["id"])
    assert g.status_code == 200, g.json()

    # The member holds the system 'member' role → gains access.
    member_ids = {m["id"] for m in _list_models(test_client, cast["member"]["token"], org)}
    assert mid in member_ids


@pytest.mark.e2e
def test_list_models_for_principal_role(enterprise_license, test_client, cast, get_system_role):
    """The role-centric editor lists restricted models with a granted flag."""
    mid = cast["restrictable_id"]
    admin_t, org = cast["admin"]["token"], cast["org_id"]
    member_role = get_system_role("member", user_token=admin_t, org_id=org)

    # Nothing restricted yet → empty list (open models aren't grantable).
    r = _list_by_principal(test_client, admin_t, org, "role", member_role["id"])
    assert r.status_code == 200, r.json()
    assert all(m["model_id"] != mid for m in r.json())

    # Restrict it → now listed, not yet granted to the role.
    _restrict(test_client, admin_t, org, mid, True)
    r = _list_by_principal(test_client, admin_t, org, "role", member_role["id"])
    row = next(m for m in r.json() if m["model_id"] == mid)
    assert row["granted"] is False and row["grant_id"] is None

    # Grant via the model endpoint → role-centric view reflects it.
    _add_access(test_client, admin_t, org, mid, "role", member_role["id"])
    r = _list_by_principal(test_client, admin_t, org, "role", member_role["id"])
    row = next(m for m in r.json() if m["model_id"] == mid)
    assert row["granted"] is True and row["grant_id"]


@pytest.mark.e2e
def test_list_by_principal_member_denied(enterprise_license, test_client, cast, get_system_role):
    """by-principal is manage_llm-gated — members get 403."""
    admin_t, org = cast["admin"]["token"], cast["org_id"]
    member_role = get_system_role("member", user_token=admin_t, org_id=org)
    r = _list_by_principal(test_client, cast["member"]["token"], org, "role", member_role["id"])
    assert r.status_code == 403


# ── guardrails ───────────────────────────────────────────────────────────


@pytest.mark.e2e
def test_cannot_restrict_default_model(enterprise_license, test_client, cast):
    default = next(m for m in cast["models"] if m["is_default"])
    r = _restrict(test_client, cast["admin"]["token"], cast["org_id"], default["id"], True)
    assert r.status_code == 400


@pytest.mark.e2e
def test_member_cannot_manage_access(enterprise_license, test_client, cast):
    mid = cast["restrictable_id"]
    member_t, org = cast["member"]["token"], cast["org_id"]
    # manage_llm gated — members are denied.
    assert _restrict(test_client, member_t, org, mid, True).status_code == 403
    assert _get_access(test_client, member_t, org, mid).status_code == 403
    assert _add_access(test_client, member_t, org, mid, "user", cast["member"]["user_id"]).status_code == 403


@pytest.mark.e2e
def test_access_endpoints_require_enterprise(test_client, cast):
    """Without an enterprise license the access endpoints are 402-gated."""
    mid = cast["restrictable_id"]
    admin_t, org = cast["admin"]["token"], cast["org_id"]
    # No enterprise_license fixture here → community → 402.
    assert _restrict(test_client, admin_t, org, mid, True).status_code == 402
    assert _get_access(test_client, admin_t, org, mid).status_code == 402


# ── service-level security boundary: get_model_by_id ─────────────────────


@pytest.mark.e2e
def test_get_model_by_id_enforces_access(enterprise_license, test_client, cast):
    """The completion path selects models by id — a restricted model the user
    has no grant for must be rejected (not silently returned)."""
    from fastapi import HTTPException
    from app.services.llm_service import LLMService
    from app.models.organization import Organization
    from app.models.user import User

    mid = cast["restrictable_id"]
    admin_t, org = cast["admin"]["token"], cast["org_id"]
    _restrict(test_client, admin_t, org, mid, True)

    default_id = next(m["id"] for m in cast["models"] if m["is_default"])
    member_uid = cast["member"]["user_id"]
    admin_uid = cast["admin"]["user_id"]

    async def _check():
        svc = LLMService()
        async with async_session_maker() as db:
            organization = await db.get(Organization, org)
            member = await db.get(User, member_uid)
            admin = await db.get(User, admin_uid)

            # Member denied on the restricted model.
            with pytest.raises(HTTPException) as exc:
                await svc.get_model_by_id(db, organization, member, mid)
            assert exc.value.status_code == 403

            # Admin allowed (full_admin_access bypass).
            assert await svc.get_model_by_id(db, organization, admin, mid) is not None

            # Member still allowed on the default model (defaults always open).
            assert await svc.get_model_by_id(db, organization, member, default_id) is not None

    _run(_check())


@pytest.mark.e2e
def test_fail_open_when_feature_absent(enterprise_license, test_client, cast, monkeypatch):
    """If the license loses the feature, restricted models behave as open
    (fail-open) — no lock-out from a billing lapse."""
    from app.services.llm_service import LLMService
    from app.models.organization import Organization
    from app.models.user import User
    from app.ee import license as ee_license

    mid = cast["restrictable_id"]
    admin_t, org = cast["admin"]["token"], cast["org_id"]
    _restrict(test_client, admin_t, org, mid, True)

    # Member can't see it while the feature is active.
    member_ids = {m["id"] for m in _list_models(test_client, cast["member"]["token"], org)}
    assert mid not in member_ids

    # Now drop the feature → enforcement fails open.
    monkeypatch.setattr(ee_license, "has_feature", lambda feature: False)
    member_uid = cast["member"]["user_id"]

    async def _check():
        svc = LLMService()
        async with async_session_maker() as db:
            organization = await db.get(Organization, org)
            member = await db.get(User, member_uid)
            # Visible again via the list path.
            models = await svc.get_models(db, organization, member)
            assert any(str(m.id) == mid for m in models)
            # And usable via the selection path.
            assert await svc.get_model_by_id(db, organization, member, mid) is not None

    _run(_check())
