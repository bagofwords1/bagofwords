"""
E2E tests for group-level default LLM models (EE llm_access_control).

Main use case: group 1 and group 2 each get their own default model, both
models are restricted, and neither group can see or use the other's model.
Also covers: members in several groups (priority), members in no group, a
restricted org default, small defaults, stale/removed membership, the
coverage warning, and the admin guardrails.

Cast is seeded through the real API with the shared RBAC fixtures. Providers
use a dummy key — provider creation does not test the connection, so no
network/LLM access is needed.
"""
import asyncio
import uuid
from types import SimpleNamespace

import pytest

from app.dependencies import async_session_maker


def _headers(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _run(coro):
    return asyncio.run(coro)


def _create_provider_with_models(test_client, token, org_id, n_models):
    suffix = uuid.uuid4().hex[:6]
    models = [
        {"model_id": f"claude-grp-{suffix}-{i}", "name": f"Grp Model {suffix} {i}", "is_custom": True}
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


def _list_models(test_client, token, org_id):
    resp = test_client.get("/api/llm/models", headers=_headers(token, org_id))
    assert resp.status_code == 200, resp.json()
    return resp.json()


def _visible_ids(test_client, token, org_id):
    return {m["id"] for m in _list_models(test_client, token, org_id)}


def _inherited(test_client, token, org_id):
    """(model id, source, group name) the caller inherits, per GET /llm/models."""
    marked = [m for m in _list_models(test_client, token, org_id) if m.get("is_inherited_default")]
    assert len(marked) <= 1, marked
    if not marked:
        return None, None, None
    m = marked[0]
    return m["id"], m["inherited_default_source"], m["inherited_default_group_name"]


def _restrict(test_client, token, org_id, model_id, value=True):
    r = test_client.put(
        f"/api/llm/models/{model_id}/restricted",
        json={"is_restricted": value},
        headers=_headers(token, org_id),
    )
    return r


def _set_group_defaults(test_client, token, org_id, group_id, **body):
    return test_client.put(
        f"/api/llm/group-defaults/{group_id}", json=body, headers=_headers(token, org_id)
    )


def _resolve(org_id, user_id, *, is_small=False, personal=False, report_model_id=None):
    """Resolve the model the completion path would pick for this user."""
    from app.services.llm_service import LLMService
    from app.models.organization import Organization
    from app.models.user import User

    async def _go():
        svc = LLMService()
        async with async_session_maker() as db:
            organization = await db.get(Organization, org_id)
            user = await db.get(User, user_id) if user_id else None
            if report_model_id is not None:
                m = await svc.get_default_model_for_report(
                    db, organization, user, SimpleNamespace(model_id=report_model_id)
                )
            elif personal:
                m = await svc.get_default_model_for_user(db, organization, user)
            else:
                m = await svc.get_default_model(db, organization, user, is_small=is_small)
            return str(m.id) if m is not None else None

    return _run(_go())


@pytest.fixture
def cast(test_client, bootstrap_admin, invite_user_to_org, create_group, add_user_to_group, enterprise_license):
    """Two groups with their own restricted default model.

    Users: g1 (group 1), g2 (group 2), both (groups 1 and 2), none (no group).
    Models: the org default, A (group 1 default), B (group 2 default), and an
    open spare model C. A and B are restricted with no explicit grants — the
    group default alone must grant access.
    """
    admin = bootstrap_admin("grpdef")
    org, admin_t = admin["org_id"], admin["token"]
    _create_provider_with_models(test_client, admin_t, org, 4)
    models = _list_models(test_client, admin_t, org)
    org_default = next(m["id"] for m in models if m["is_default"])
    others = [
        m["id"] for m in models
        if not m["is_default"] and not m["is_small_default"] and not m.get("supports_image_generation")
    ]
    assert len(others) >= 3, models
    model_a, model_b, model_c = others[:3]

    users = {k: invite_user_to_org(org_id=org, admin_token=admin_t) for k in ("g1", "g2", "both", "none")}

    def _group(name):
        r = create_group(name=name, user_token=admin_t, org_id=org)
        assert r.status_code == 200, r.json()
        return r.json()["id"]

    # Names chosen so that, on a priority tie, group 1 sorts first.
    group1 = _group(f"a-group1-{uuid.uuid4().hex[:6]}")
    group2 = _group(f"b-group2-{uuid.uuid4().hex[:6]}")
    for key, gids in (("g1", [group1]), ("g2", [group2]), ("both", [group1, group2])):
        for gid in gids:
            r = add_user_to_group(group_id=gid, user_id=users[key]["user_id"], user_token=admin_t, org_id=org)
            assert r.status_code in (200, 201), r.text

    for mid in (model_a, model_b):
        assert _restrict(test_client, admin_t, org, mid).status_code == 200
    assert _set_group_defaults(test_client, admin_t, org, group1, default_llm_model_id=model_a).status_code == 200
    assert _set_group_defaults(test_client, admin_t, org, group2, default_llm_model_id=model_b).status_code == 200

    return SimpleNamespace(
        admin=admin, org=org, admin_t=admin_t, users=users,
        group1=group1, group2=group2,
        org_default=org_default, a=model_a, b=model_b, c=model_c,
    )


# ── main use case: each group has its own default, no cross-access ─────────


@pytest.mark.e2e
def test_each_group_member_inherits_their_group_default(test_client, cast):
    for key, expected, gid in (("g1", cast.a, cast.group1), ("g2", cast.b, cast.group2)):
        u = cast.users[key]
        mid, source, _group_name = _inherited(test_client, u["token"], cast.org)
        assert (mid, source) == (expected, "group"), key
        # The completion path picks the same model, with and without the
        # personal-default layer on top.
        assert _resolve(cast.org, u["user_id"]) == expected
        assert _resolve(cast.org, u["user_id"], personal=True) == expected


@pytest.mark.e2e
def test_groups_cannot_see_or_use_each_others_models(test_client, cast):
    from fastapi import HTTPException
    from app.services.llm_service import LLMService
    from app.models.organization import Organization
    from app.models.user import User

    g1, g2 = cast.users["g1"], cast.users["g2"]
    g1_ids = _visible_ids(test_client, g1["token"], cast.org)
    g2_ids = _visible_ids(test_client, g2["token"], cast.org)
    assert cast.a in g1_ids and cast.b not in g1_ids
    assert cast.b in g2_ids and cast.a not in g2_ids

    async def _check():
        svc = LLMService()
        async with async_session_maker() as db:
            organization = await db.get(Organization, cast.org)
            for user_id, other in ((g1["user_id"], cast.b), (g2["user_id"], cast.a)):
                user = await db.get(User, user_id)
                with pytest.raises(HTTPException) as exc:
                    await svc.get_model_by_id(db, organization, user, other)
                assert exc.value.status_code == 403

    _run(_check())

    # Neither can pick the other group's model as a personal default.
    for u, other in ((g1, cast.b), (g2, cast.a)):
        r = test_client.put(
            "/api/users/me/default_model", json={"model_id": other}, headers=_headers(u["token"], cast.org)
        )
        assert r.status_code == 403, r.text


@pytest.mark.e2e
def test_report_pinned_to_other_groups_model_falls_back_to_own_group_default(cast):
    """A report a group-2 teammate pinned to B, run by a group-1 member, uses A."""
    assert _resolve(cast.org, cast.users["g1"]["user_id"], report_model_id=cast.b) == cast.a
    assert _resolve(cast.org, cast.users["g2"]["user_id"], report_model_id=cast.b) == cast.b


@pytest.mark.e2e
def test_member_in_both_groups_uses_priority_then_name(test_client, cast):
    both = cast.users["both"]
    ids = _visible_ids(test_client, both["token"], cast.org)
    assert {cast.a, cast.b} <= ids

    # Equal priority → tie broken by group name (group 1 sorts first).
    assert _inherited(test_client, both["token"], cast.org)[0] == cast.a

    # Lower number wins.
    r = _set_group_defaults(test_client, cast.admin_t, cast.org, cast.group2, default_llm_priority=10)
    assert r.status_code == 200, r.json()
    assert _inherited(test_client, both["token"], cast.org)[0] == cast.b
    assert _resolve(cast.org, both["user_id"]) == cast.b


@pytest.mark.e2e
def test_member_in_no_group_gets_org_default_and_no_group_models(test_client, cast):
    none = cast.users["none"]
    ids = _visible_ids(test_client, none["token"], cast.org)
    assert cast.a not in ids and cast.b not in ids
    mid, source, _ = _inherited(test_client, none["token"], cast.org)
    assert (mid, source) == (cast.org_default, "org")


@pytest.mark.e2e
def test_removing_member_from_group_drops_default_and_access(test_client, cast):
    g1 = cast.users["g1"]
    r = test_client.delete(
        f"/api/organizations/{cast.org}/groups/{cast.group1}/members/{g1['user_id']}",
        headers=_headers(cast.admin_t, cast.org),
    )
    assert r.status_code == 204, r.text
    assert cast.a not in _visible_ids(test_client, g1["token"], cast.org)
    assert _resolve(cast.org, g1["user_id"]) == cast.org_default


@pytest.mark.e2e
def test_clearing_group_default_restores_org_default(test_client, cast):
    g2 = cast.users["g2"]
    r = _set_group_defaults(test_client, cast.admin_t, cast.org, cast.group2, default_llm_model_id=None)
    assert r.status_code == 200, r.json()
    assert r.json()["default_llm_model_id"] is None
    assert _resolve(cast.org, g2["user_id"]) == cast.org_default
    # The default was B's only source of access for group 2.
    assert cast.b not in _visible_ids(test_client, g2["token"], cast.org)


# ── restricted org default ────────────────────────────────────────────────


@pytest.mark.e2e
def test_restricted_org_default_hidden_from_groups_without_access(test_client, cast):
    """The org default can be restricted: grouped members keep their group
    default, ungrouped members fall back, and coverage reports them."""
    r = _restrict(test_client, cast.admin_t, cast.org, cast.org_default, True)
    assert r.status_code == 200, r.json()

    for key, expected in (("g1", cast.a), ("g2", cast.b)):
        u = cast.users[key]
        assert cast.org_default not in _visible_ids(test_client, u["token"], cast.org)
        assert _resolve(cast.org, u["user_id"]) == expected

    none = cast.users["none"]
    assert cast.org_default not in _visible_ids(test_client, none["token"], cast.org)
    mid, source, _ = _inherited(test_client, none["token"], cast.org)
    assert source == "fallback"
    assert mid not in (cast.org_default, cast.a, cast.b)

    # Admin keeps the org default (full admin bypass).
    assert _resolve(cast.org, cast.admin["user_id"]) == cast.org_default

    cov = test_client.get("/api/llm/default-coverage", headers=_headers(cast.admin_t, cast.org))
    assert cov.status_code == 200, cov.json()
    uncovered = {u["user_id"] for u in cov.json()["members_without_default"]}
    assert none["user_id"] in uncovered
    assert not uncovered & {cast.users[k]["user_id"] for k in ("g1", "g2", "both")}
    assert cast.admin["user_id"] not in uncovered

    # Granting the ungrouped member access closes the gap.
    g = test_client.post(
        f"/api/llm/models/{cast.org_default}/access",
        json={"principal_type": "user", "principal_id": none["user_id"]},
        headers=_headers(cast.admin_t, cast.org),
    )
    assert g.status_code == 200, g.json()
    cov = test_client.get("/api/llm/default-coverage", headers=_headers(cast.admin_t, cast.org)).json()
    assert none["user_id"] not in {u["user_id"] for u in cov["members_without_default"]}


@pytest.mark.e2e
def test_no_coverage_gap_while_org_default_is_open(test_client, cast):
    cov = test_client.get("/api/llm/default-coverage", headers=_headers(cast.admin_t, cast.org))
    assert cov.status_code == 200, cov.json()
    assert cov.json()["members_without_default"] == []


# ── small defaults ───────────────────────────────────────────────────────


@pytest.mark.e2e
def test_group_small_default(test_client, cast):
    g1, g2 = cast.users["g1"], cast.users["g2"]
    org_small = _resolve(cast.org, g2["user_id"], is_small=True)
    assert org_small is not None

    r = _set_group_defaults(test_client, cast.admin_t, cast.org, cast.group1, small_default_llm_model_id=cast.c)
    assert r.status_code == 200, r.json()
    assert _resolve(cast.org, g1["user_id"], is_small=True) == cast.c
    # Group 2 has no small default → unchanged.
    assert _resolve(cast.org, g2["user_id"], is_small=True) == org_small


@pytest.mark.e2e
def test_restricted_org_small_default_falls_back_to_group_default(test_client, cast):
    org_small = _resolve(cast.org, None, is_small=True)
    r = _restrict(test_client, cast.admin_t, cast.org, org_small, True)
    assert r.status_code == 200, r.json()
    assert _resolve(cast.org, cast.users["g1"]["user_id"], is_small=True) == cast.a
    assert _resolve(cast.org, cast.users["g2"]["user_id"], is_small=True) == cast.b
    # System jobs (no user) still get the org small default.
    assert _resolve(cast.org, None, is_small=True) == org_small


# ── admin API & guardrails ────────────────────────────────────────────────


@pytest.mark.e2e
def test_list_group_defaults_and_access_view(test_client, cast):
    r = test_client.get("/api/llm/group-defaults", headers=_headers(cast.admin_t, cast.org))
    assert r.status_code == 200, r.json()
    rows = {row["group_id"]: row for row in r.json()}
    assert rows[cast.group1]["default_llm_model_id"] == cast.a
    assert rows[cast.group2]["default_llm_model_id"] == cast.b
    assert rows[cast.group1]["default_model"]["id"] == cast.a

    acc = test_client.get(f"/api/llm/models/{cast.a}/access", headers=_headers(cast.admin_t, cast.org))
    assert acc.status_code == 200, acc.json()
    assert [g["group_id"] for g in acc.json()["default_for_groups"]] == [cast.group1]


@pytest.mark.e2e
def test_group_default_model_cannot_be_disabled_or_deleted(test_client, cast):
    h = _headers(cast.admin_t, cast.org)
    r = test_client.post(f"/api/llm/models/{cast.a}/toggle", params={"enabled": False}, headers=h)
    assert r.status_code == 400, r.text
    assert r.json().get("error_code") == "llm.model_is_group_default"

    r = test_client.delete(f"/api/llm/models/{cast.a}", headers=h)
    assert r.status_code == 400, r.text
    assert r.json().get("error_code") == "llm.model_is_group_default"

    # Once no group uses it, it can be disabled.
    _set_group_defaults(test_client, cast.admin_t, cast.org, cast.group1, default_llm_model_id=None)
    r = test_client.post(f"/api/llm/models/{cast.a}/toggle", params={"enabled": False}, headers=h)
    assert r.status_code == 200, r.text


@pytest.mark.e2e
def test_group_default_must_be_a_usable_chat_model(test_client, cast):
    r = _set_group_defaults(test_client, cast.admin_t, cast.org, cast.group1, default_llm_model_id=str(uuid.uuid4()))
    assert r.status_code == 400, r.json()
    assert r.json().get("error_code") == "llm.group_default_invalid"

    r = test_client.put(
        f"/api/llm/group-defaults/{uuid.uuid4()}", json={"default_llm_priority": 1},
        headers=_headers(cast.admin_t, cast.org),
    )
    assert r.status_code == 404


@pytest.mark.e2e
def test_members_cannot_manage_group_defaults(test_client, cast):
    u = cast.users["g1"]
    h = _headers(u["token"], cast.org)
    assert test_client.get("/api/llm/group-defaults", headers=h).status_code == 403
    assert test_client.get("/api/llm/default-coverage", headers=h).status_code == 403
    r = test_client.put(f"/api/llm/group-defaults/{cast.group2}", json={"default_llm_model_id": cast.a}, headers=h)
    assert r.status_code == 403
    # Nothing changed for group 2.
    assert _resolve(cast.org, cast.users["g2"]["user_id"]) == cast.b
