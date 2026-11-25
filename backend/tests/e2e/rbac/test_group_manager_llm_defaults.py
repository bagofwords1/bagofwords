"""Group managers pick their group's default and small default model.

Story: Finance and Marketing each have their own manager (Alice, Bob) and
their own provider. Finance may use provider A, Marketing provider B. An org
admin sets up access once; after that each manager picks the models their
team starts on, without org-wide LLM admin rights.

The rule that keeps this safe: a group's default is usable by its members even
when restricted, so a manager may only pick a model the group can *already*
use — open to everyone, or granted to the group (directly or through a role
assigned to the group). The priority decides between groups and stays
admin-only.
"""
import asyncio
import types
import uuid

import pytest

from app.dependencies import async_session_maker


def _h(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def cast(test_client, bootstrap_admin, invite_user_to_org, create_group, add_user_to_group,
         grant_resource, create_role, assign_role, enterprise_license):
    admin = bootstrap_admin("gmllm")
    t, org = admin["token"], admin["org_id"]
    sfx = uuid.uuid4().hex[:6]

    def call(actor, method, path, **kw):
        return test_client.request(method, f"/api/{path}", headers=_h(actor["token"], org), **kw)

    def provider(name, n):
        r = test_client.post("/api/llm/providers", json={
            "name": f"{name}-{sfx}", "provider_type": "anthropic", "credentials": {"api_key": "dummy-key"},
            "models": [{"model_id": f"{name}-{sfx}-{i}", "name": f"{name} {i}", "is_custom": True} for i in range(n)],
        }, headers=_h(t, org))
        assert r.status_code == 200, r.json()
        return r.json()["id"]

    provider("openprov", 1)
    pa = provider("prova", 2)
    pb = provider("provb", 3)
    models = test_client.get("/api/llm/models", headers=_h(t, org)).json()
    by_name = {m["name"]: m["id"] for m in models}
    m = types.SimpleNamespace(
        open=by_name["openprov 0"], a0=by_name["prova 0"], a1=by_name["prova 1"],
        b0=by_name["provb 0"], b1=by_name["provb 1"], b_role=by_name["provb 2"],
    )
    for mid in (m.a0, m.a1, m.b0, m.b1, m.b_role):
        assert test_client.put(f"/api/llm/models/{mid}/restricted", json={"is_restricted": True},
                               headers=_h(t, org)).status_code == 200

    finance = create_group(name=f"Finance-{sfx}", user_token=t, org_id=org).json()["id"]
    marketing = create_group(name=f"Marketing-{sfx}", user_token=t, org_id=org).json()["id"]
    people = {k: invite_user_to_org(org_id=org, admin_token=t) for k in ("alice", "fiona", "bob", "mark", "nate")}
    for gid, keys in ((finance, ("alice", "fiona")), (marketing, ("bob", "mark"))):
        for k in keys:
            assert add_user_to_group(group_id=gid, user_id=people[k]["user_id"], user_token=t,
                                     org_id=org).status_code in (200, 201)
    for gid, k in ((finance, "alice"), (marketing, "bob")):
        r = test_client.put(f"/api/organizations/{org}/groups/{gid}/managers/{people[k]['user_id']}",
                            headers=_h(t, org))
        assert r.status_code == 204, r.text

    # Finance may use provider A; Marketing provider B (b0, b1 granted to the
    # group, b_role through a role assigned to the group).
    for mid, gid in ((m.a0, finance), (m.a1, finance), (m.b0, marketing), (m.b1, marketing)):
        g = grant_resource(resource_type="llm_model", resource_id=mid, principal_type="group",
                           principal_id=gid, permissions=["use"], user_token=t, org_id=org)
        assert g.status_code in (200, 201), g.json()
    role = test_client.post(f"/api/organizations/{org}/roles", json={
        "name": f"mkt-models-{sfx}", "permissions": ["view_reports"],
        "resource_grants": [{"resource_type": "llm_model", "resource_id": m.b_role, "permissions": ["use"]}],
    }, headers=_h(t, org))
    assert role.status_code in (200, 201), role.json()
    role_id = role.json()["id"]
    r = assign_role(role_id=role_id, principal_type="group", principal_id=marketing, user_token=t, org_id=org)
    assert r.status_code in (200, 201), r.json()

    return types.SimpleNamespace(
        call=call, org=org, admin=admin, m=m, finance=finance, marketing=marketing, **people,
    )


def _put(c, actor, gid, **body):
    return c.call(actor, "PUT", f"llm/group-defaults/{gid}", json=body)


def _choices(c, actor, gid):
    return c.call(actor, "GET", f"llm/group-defaults/{gid}/choices")


def _default_for(org_id, user_id, *, is_small=False):
    from app.models.organization import Organization
    from app.models.user import User
    from app.services.llm_service import LLMService

    async def _go():
        async with async_session_maker() as db:
            org = await db.get(Organization, org_id)
            user = await db.get(User, user_id)
            model = await LLMService().get_default_model(db, org, user, is_small=is_small)
            return str(model.id) if model is not None else None
    return _run(_go())


# ── what a manager sees ────────────────────────────────────────────────────

@pytest.mark.e2e
def test_manager_lists_only_the_groups_they_manage(cast):
    r = cast.call(cast.alice, "GET", "llm/group-defaults")
    assert r.status_code == 200, r.text
    assert [g["group_id"] for g in r.json()] == [cast.finance]
    admin_rows = cast.call(cast.admin, "GET", "llm/group-defaults").json()
    assert {cast.finance, cast.marketing} <= {g["group_id"] for g in admin_rows}


@pytest.mark.e2e
def test_manager_choices_are_the_models_the_group_can_already_use(cast):
    m = cast.m
    # Open models are offered to everyone; of the restricted ones, only the
    # group's own grants are.
    finance = _choices(cast, cast.alice, cast.finance).json()
    assert m.open in {c["id"] for c in finance}
    assert {c["id"] for c in finance if c["is_restricted"]} == {m.a0, m.a1}
    marketing = _choices(cast, cast.bob, cast.marketing).json()
    assert {c["id"] for c in marketing if c["is_restricted"]} == {m.b0, m.b1, m.b_role}, \
        "a role assigned to the group counts"
    admin = {c["id"] for c in _choices(cast, cast.admin, cast.finance).json()}
    assert {m.open, m.a0, m.a1, m.b0, m.b1, m.b_role} <= admin, "an LLM admin may pick anything"


# ── what a manager may change ──────────────────────────────────────────────

@pytest.mark.e2e
def test_manager_sets_their_groups_default_and_members_get_it(cast):
    m = cast.m
    r = _put(cast, cast.alice, cast.finance, default_llm_model_id=m.a1, small_default_llm_model_id=m.a0)
    assert r.status_code == 200, r.text
    assert r.json()["default_llm_model_id"] == m.a1
    assert _default_for(cast.org, cast.fiona["user_id"]) == m.a1
    assert _default_for(cast.org, cast.fiona["user_id"], is_small=True) == m.a0
    # Marketing is untouched.
    assert _default_for(cast.org, cast.mark["user_id"]) != m.a1


@pytest.mark.e2e
def test_manager_can_pick_a_model_granted_through_a_role_on_the_group(cast):
    assert _put(cast, cast.bob, cast.marketing, default_llm_model_id=cast.m.b_role).status_code == 200


@pytest.mark.e2e
def test_manager_can_clear_the_default(cast):
    assert _put(cast, cast.alice, cast.finance, default_llm_model_id=cast.m.a1).status_code == 200
    r = _put(cast, cast.alice, cast.finance, default_llm_model_id=None)
    assert r.status_code == 200 and r.json()["default_llm_model_id"] is None


# ── what a manager may not change ──────────────────────────────────────────

@pytest.mark.e2e
@pytest.mark.parametrize("target", ["b0", "b_role"])
def test_manager_cannot_give_their_group_a_model_it_cannot_use(cast, target):
    for field in ("default_llm_model_id", "small_default_llm_model_id"):
        r = _put(cast, cast.alice, cast.finance, **{field: getattr(cast.m, target)})
        assert r.status_code == 403, r.text
        assert r.json().get("error_code") == "llm.group_default_not_available"
    # And Finance members still can't see the model.
    seen = {x["id"] for x in cast.call(cast.fiona, "GET", "llm/models").json()}
    assert getattr(cast.m, target) not in seen


@pytest.mark.e2e
def test_manager_cannot_change_the_priority(cast):
    r = _put(cast, cast.alice, cast.finance, default_llm_priority=1)
    assert r.status_code == 403, r.text
    assert r.json().get("error_code") == "llm.group_default_priority_admin_only"
    # Sending the current value back (as a form does) is fine.
    assert _put(cast, cast.alice, cast.finance, default_llm_priority=100,
                default_llm_model_id=cast.m.a0).status_code == 200


@pytest.mark.e2e
def test_manager_keeps_an_admin_chosen_default_but_cannot_set_it_back_after_changing(cast):
    m = cast.m
    # The admin gives Finance a provider-B model as its default (admins may).
    assert _put(cast, cast.admin, cast.finance, default_llm_model_id=m.b1).status_code == 200
    # Alice changes only the small model, or re-sends the current default: fine.
    assert _put(cast, cast.alice, cast.finance, small_default_llm_model_id=m.a0).status_code == 200
    assert _put(cast, cast.alice, cast.finance, default_llm_model_id=m.b1).status_code == 200
    # Once she moves away from it, she can't put it back.
    assert _put(cast, cast.alice, cast.finance, default_llm_model_id=m.a1).status_code == 200
    assert _put(cast, cast.alice, cast.finance, default_llm_model_id=m.b1).status_code == 403


@pytest.mark.e2e
@pytest.mark.parametrize("who", ["alice", "fiona", "nate"])
def test_others_cannot_touch_marketing(cast, who):
    actor = getattr(cast, who)
    assert _put(cast, actor, cast.marketing, default_llm_model_id=cast.m.open).status_code == 403
    assert _choices(cast, actor, cast.marketing).status_code == 403


@pytest.mark.e2e
@pytest.mark.parametrize("who", ["fiona", "nate"])
def test_plain_members_cannot_list_group_defaults(cast, who):
    assert cast.call(getattr(cast, who), "GET", "llm/group-defaults").status_code == 403


@pytest.mark.e2e
def test_manager_cannot_use_the_admin_only_llm_routes(cast):
    a = cast.alice
    assert cast.call(a, "GET", "llm/default-coverage").status_code == 403
    assert cast.call(a, "POST", f"llm/models/{cast.m.a0}/set_default?small=false").status_code == 403
    assert cast.call(a, "PUT", f"llm/models/{cast.m.b0}/restricted", json={"is_restricted": False}).status_code == 403


@pytest.mark.e2e
def test_the_role_ends_with_demotion_or_leaving(cast, test_client):
    t, org = cast.admin["token"], cast.org
    r = test_client.delete(f"/api/organizations/{org}/groups/{cast.finance}/managers/{cast.alice['user_id']}",
                           headers=_h(t, org))
    assert r.status_code == 204, r.text
    assert _put(cast, cast.alice, cast.finance, default_llm_model_id=cast.m.a0).status_code == 403
    assert cast.call(cast.alice, "GET", "llm/group-defaults").status_code == 403

    r = test_client.delete(f"/api/organizations/{org}/groups/{cast.marketing}/members/{cast.bob['user_id']}",
                           headers=_h(t, org))
    assert r.status_code == 204, r.text
    assert _put(cast, cast.bob, cast.marketing, default_llm_model_id=cast.m.b0).status_code == 403


# ── admin unchanged, audit ─────────────────────────────────────────────────

@pytest.mark.e2e
def test_admin_still_sets_any_model_and_the_priority(cast):
    r = _put(cast, cast.admin, cast.finance, default_llm_model_id=cast.m.b0, default_llm_priority=5)
    assert r.status_code == 200, r.text
    assert r.json()["default_llm_priority"] == 5


@pytest.mark.e2e
def test_manager_changes_and_denials_are_audited(cast):
    assert _put(cast, cast.alice, cast.finance, default_llm_model_id=cast.m.a0).status_code == 200
    assert _put(cast, cast.alice, cast.marketing, default_llm_model_id=cast.m.open).status_code == 403
    body = cast.call(cast.admin, "GET", "enterprise/audit?limit=100").json()
    items = body.get("items", body if isinstance(body, list) else [])
    changed = [e for e in items if e["action"] == "group.llm_defaults_changed"]
    assert any((e.get("details") or {}).get("as_group_manager") is True for e in changed)
    assert any(e["action"] == "group.llm_defaults_changed.denied" for e in items)
