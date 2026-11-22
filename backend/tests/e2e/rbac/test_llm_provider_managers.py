"""LLM provider managers: an org LLM admin lets a member run one provider.

Story: two providers, A and B. Alice runs provider A, Bob runs provider B;
neither is an org LLM admin. Each may change their own provider's name,
credentials and connection settings, turn its models on or off (a model they
turn on starts restricted), and set those models' settings and routing hints.
Everything org-wide stays with ``manage_llm``: defaults, model access,
pricing, fallback, the Auto router, adding or removing providers or models.
"""
import types
import uuid

import pytest


def _h(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


@pytest.fixture
def cast(test_client, bootstrap_admin, invite_user_to_org, create_group, add_user_to_group, enterprise_license):
    admin = bootstrap_admin("provmgr")
    t, org = admin["token"], admin["org_id"]
    sfx = uuid.uuid4().hex[:6]

    def call(actor, method, path, **kw):
        return test_client.request(method, f"/api/{path}", headers=_h(actor["token"], org), **kw)

    def provider(name, n, ptype="anthropic", creds=None):
        r = test_client.post("/api/llm/providers", json={
            "name": f"{name}-{sfx}", "provider_type": ptype,
            "credentials": creds or {"api_key": "dummy-key"},
            "models": [{"model_id": f"{name}-{sfx}-{i}", "name": f"{name} {i}", "is_custom": True}
                       for i in range(n)],
        }, headers=_h(t, org))
        assert r.status_code == 200, r.json()
        return r.json()["id"]

    pa = provider("prova", 3)
    pb = provider("provb", 2)
    models = test_client.get("/api/llm/models", params={"kind": "all"}, headers=_h(t, org)).json()
    by_name = {m["name"]: m["id"] for m in models}
    m = types.SimpleNamespace(a0=by_name["prova 0"], a1=by_name["prova 1"], a2=by_name["prova 2"],
                              b0=by_name["provb 0"], b1=by_name["provb 1"])
    # a2 starts switched off; Alice will turn it on.
    assert test_client.post(f"/api/llm/models/{m.a2}/toggle?enabled=false", headers=_h(t, org)).status_code == 200

    people = {k: invite_user_to_org(org_id=org, admin_token=t) for k in ("alice", "bob", "fiona")}
    for who, pid in (("alice", pa), ("bob", pb)):
        r = test_client.put(f"/api/llm/providers/{pid}/managers/{people[who]['user_id']}", headers=_h(t, org))
        assert r.status_code == 200, r.text
    group = create_group(name=f"Finance-{sfx}", user_token=t, org_id=org).json()["id"]
    return types.SimpleNamespace(call=call, org=org, admin=admin, pa=pa, pb=pb, m=m, group=group,
                                 client=test_client, **people)


def _model(c, mid):
    rows = c.call(c.admin, "GET", "llm/models", params={"kind": "all", "manage": "true"}).json()
    return next(x for x in rows if x["id"] == mid)


# ── who may name a manager ─────────────────────────────────────────────────

@pytest.mark.e2e
def test_admin_lists_and_removes_managers(cast):
    r = cast.call(cast.admin, "GET", f"llm/providers/{cast.pa}/managers")
    assert r.status_code == 200 and [u["user_id"] for u in r.json()] == [cast.alice["user_id"]]
    assert cast.call(cast.admin, "DELETE", f"llm/providers/{cast.pa}/managers/{cast.alice['user_id']}").status_code == 200
    assert cast.call(cast.admin, "GET", f"llm/providers/{cast.pa}/managers").json() == []
    assert cast.call(cast.alice, "PUT", f"llm/providers/{cast.pa}", json={"name": "x"}).status_code == 403


@pytest.mark.e2e
@pytest.mark.parametrize("method,suffix", [("GET", ""), ("PUT", "/{fiona}"), ("DELETE", "/{bob}")])
def test_managers_cannot_name_or_remove_managers(cast, method, suffix):
    path = f"llm/providers/{cast.pa}/managers" + suffix.format(fiona=cast.fiona["user_id"], bob=cast.bob["user_id"])
    assert cast.call(cast.alice, method, path).status_code == 403
    assert cast.call(cast.bob, "PUT", f"llm/providers/{cast.pa}/managers/{cast.bob['user_id']}").status_code == 403


@pytest.mark.e2e
def test_only_org_members_can_be_named(cast):
    r = cast.call(cast.admin, "PUT", f"llm/providers/{cast.pa}/managers/{uuid.uuid4()}")
    assert r.status_code == 400 and r.json().get("error_code") == "llm.provider_manager_not_member"


@pytest.mark.e2e
def test_provider_grants_cannot_be_minted_through_generic_routes(cast):
    c = cast
    r = c.call(c.admin, "POST", f"organizations/{c.org}/resource-grants", json={
        "resource_type": "llm_provider", "resource_id": c.pb, "principal_type": "user",
        "principal_id": c.fiona["user_id"], "permissions": ["manage"],
    })
    assert r.status_code == 422, r.text
    r = c.call(c.admin, "POST", f"organizations/{c.org}/roles", json={
        "name": f"sneaky-{uuid.uuid4().hex[:4]}", "permissions": ["view_reports"],
        "resource_grants": [{"resource_type": "llm_provider", "resource_id": c.pb, "permissions": ["manage"]}],
    })
    assert r.status_code == 422, r.text


# ── what a manager sees ────────────────────────────────────────────────────

@pytest.mark.e2e
def test_manager_sees_their_provider_and_its_models_only(cast):
    c = cast
    provs = c.call(c.alice, "GET", "llm/providers")
    assert provs.status_code == 200 and [p["id"] for p in provs.json()] == [c.pa]
    rows = c.call(c.alice, "GET", "llm/models", params={"kind": "all", "manage": "true"}).json()
    assert {r["id"] for r in rows} >= {c.m.a0, c.m.a1, c.m.a2}
    assert {r["provider"]["id"] for r in rows} == {c.pa}
    assert c.call(c.fiona, "GET", "llm/models", params={"manage": "true"}).status_code == 403
    assert c.call(c.fiona, "GET", "llm/providers").status_code == 403


@pytest.mark.e2e
def test_managed_provider_connection_settings_are_shown_to_its_manager_only(cast):
    c = cast
    rows = c.call(c.alice, "GET", "llm/models", params={"kind": "all"}).json()
    for r in rows:
        if r["provider"]["id"] == c.pb:
            assert r["provider"]["additional_config"] is None


@pytest.mark.e2e
def test_session_exposes_the_provider_grant(cast, whoami):
    me = whoami(cast.alice["token"])
    org = next(o for o in me["organizations"] if o["id"] == cast.org)
    perms = org.get("resource_permissions") or {}
    assert any(k.endswith(cast.pa) and "manage" in v for k, v in perms.items()), perms
    assert not any(k.endswith(cast.pb) for k in perms), perms


# ── what a manager may change ──────────────────────────────────────────────

@pytest.mark.e2e
def test_manager_updates_credentials_and_it_is_audited_without_values(cast):
    c = cast
    r = c.call(c.alice, "PUT", f"llm/providers/{c.pa}", json={"credentials": {"api_key": "new-secret-key"}})
    assert r.status_code == 200, r.text
    body = c.call(c.admin, "GET", "enterprise/audit?limit=50").json()
    items = body.get("items", body if isinstance(body, list) else [])
    upd = next(e for e in items if e["action"] == "llm_provider.updated" and e.get("resource_id") == c.pa)
    assert upd["details"]["as_provider_manager"] is True
    assert upd["details"]["key_rotated"] is True
    assert "new-secret-key" not in str(upd)


@pytest.mark.e2e
def test_a_model_the_manager_turns_on_starts_restricted(cast):
    c = cast
    assert c.call(c.alice, "POST", f"llm/models/{c.m.a2}/toggle?enabled=true").status_code == 200
    a2 = _model(c, c.m.a2)
    assert a2["is_enabled"] and a2["is_restricted"]
    # Fiona (no grant) can't use it; the admin decides who may.
    assert c.m.a2 not in {x["id"] for x in c.call(c.fiona, "GET", "llm/models").json()}


@pytest.mark.e2e
def test_turning_on_through_the_provider_dialog_also_starts_restricted(cast):
    c = cast
    rows = [x for x in c.call(c.alice, "GET", "llm/models", params={"kind": "all", "manage": "true"}).json()
            if x["provider"]["id"] == c.pa]
    payload = [{"id": x["id"], "model_id": x["model_id"], "name": x["name"],
                "is_enabled": True if x["id"] == c.m.a2 else x["is_enabled"]} for x in rows]
    r = c.call(c.alice, "PUT", f"llm/providers/{c.pa}", json={"models": payload})
    assert r.status_code == 200, r.text
    assert _model(c, c.m.a2)["is_restricted"] is True


@pytest.mark.e2e
def test_an_admin_turning_a_model_on_keeps_todays_behaviour(cast):
    c = cast
    assert c.call(c.admin, "POST", f"llm/models/{c.m.a2}/toggle?enabled=true").status_code == 200
    assert _model(c, c.m.a2)["is_restricted"] is False


@pytest.mark.e2e
@pytest.mark.parametrize("method,path,kw", [
    ("POST", "llm/models/{a0}/set_temperature?temperature=0.3", {}),
    ("POST", "llm/models/{a0}/set_context_window?tokens=64000", {}),
    ("POST", "llm/models/{a0}/toggle_vision?enabled=true", {}),
    ("PATCH", "llm/models/{a0}", {"json": {"config": {"note": "x"}}}),
    ("POST", "llm/models/{a0}/routing_hint", {"json": {"hint": "Quick lookups"}}),
    ("POST", "llm/providers/{pa}/toggle?enabled=true", {}),
])
def test_manager_may_change_their_models_settings(cast, method, path, kw):
    c = cast
    r = c.call(c.alice, method, path.format(a0=c.m.a0, pa=c.pa), **kw)
    assert r.status_code == 200, r.text
    # The same call on Bob's provider is refused.
    r = c.call(c.alice, method, path.format(a0=c.m.b0, pa=c.pb), **kw)
    assert r.status_code == 403, r.text


@pytest.mark.e2e
def test_test_connection_needs_the_provider(cast, monkeypatch):
    c = cast
    from app.routes import llm as llm_routes

    async def _fake(*a, **k):
        return {"success": True, "message": "stubbed"}
    monkeypatch.setattr(llm_routes.llm_service, "test_connection", _fake)
    base = {"provider_type": "anthropic", "name": "x", "credentials": {"api_key": ""}}
    assert c.call(c.alice, "POST", "llm/test_connection", json={**base, "provider_id": c.pa}).status_code == 200
    assert c.call(c.alice, "POST", "llm/test_connection", json={**base, "provider_id": c.pb}).status_code == 403
    # A new provider's credentials: admin only (managers can't create providers).
    assert c.call(c.alice, "POST", "llm/test_connection", json={**base, "credentials": {"api_key": "k"}}).status_code == 403


# ── what a manager may not change ──────────────────────────────────────────

@pytest.mark.e2e
@pytest.mark.parametrize("method,path,kw", [
    ("POST", "llm/models/{a0}/set_default?small=false", {}),
    ("POST", "llm/models/{a0}/pricing", {"json": {"input_cost_per_million_tokens_usd": 0.01}}),
    ("PUT", "llm/models/{a0}/restricted", {"json": {"is_restricted": False}}),
    ("GET", "llm/models/{a0}/access", {}),
    ("POST", "llm/models/{a0}/access", {"json": {"principal_type": "user", "principal_id": "{fiona}"}}),
    ("DELETE", "llm/models/{a0}", {}),
    ("DELETE", "llm/providers/{pa}", {}),
    ("POST", "llm/providers", {"json": {"name": "new", "provider_type": "anthropic",
                                         "credentials": {"api_key": "k"}, "models": []}}),
    ("POST", "llm/models", {"json": {"provider_id": "{pa}", "model_id": "extra", "name": "Extra"}}),
    ("GET", "llm/fallback_order", {}),
    ("POST", "llm/fallback_order", {"json": {"model_ids": ["{a0}"]}}),
    ("GET", "llm/default-coverage", {}),
    ("GET", "llm/model-access/by-principal", {"params": {"principal_type": "user", "principal_id": "{fiona}"}}),
    ("PUT", "organization/settings", {"json": {"config": {"model_routing": {"value": True}}}}),
])
def test_manager_cannot_change_org_wide_settings(cast, method, path, kw):
    c = cast
    fill = dict(a0=c.m.a0, pa=c.pa, fiona=c.fiona["user_id"])
    kw = {k: _fill(v, fill) for k, v in kw.items()}
    r = c.call(c.alice, method, path.format(**fill), **kw)
    assert r.status_code == 403, f"{method} {path}: {r.status_code} {r.text}"


def _fill(v, fill):
    if isinstance(v, str):
        return v.format(**fill)
    if isinstance(v, list):
        return [_fill(x, fill) for x in v]
    if isinstance(v, dict):
        return {k: _fill(x, fill) for k, x in v.items()}
    return v


@pytest.mark.e2e
def test_manager_cannot_add_models_or_change_prices_through_the_provider_dialog(cast):
    c = cast
    rows = [x for x in c.call(c.alice, "GET", "llm/models", params={"kind": "all", "manage": "true"}).json()
            if x["provider"]["id"] == c.pa]
    base = [{"id": x["id"], "model_id": x["model_id"], "name": x["name"], "is_enabled": x["is_enabled"],
             "input_cost_per_million_tokens_usd": x.get("input_cost_per_million_tokens_usd")} for x in rows]
    # Echoing the current values back is fine.
    assert c.call(c.alice, "PUT", f"llm/providers/{c.pa}", json={"models": base}).status_code == 200
    added = base + [{"model_id": "brand-new", "name": "Brand new", "is_enabled": True, "is_custom": True}]
    r = c.call(c.alice, "PUT", f"llm/providers/{c.pa}", json={"models": added})
    assert r.status_code == 403 and r.json().get("error_code") == "llm.provider_manager_admin_only"
    priced = [dict(x, input_cost_per_million_tokens_usd=999.0) if x["id"] == c.m.a0 else x for x in base]
    r = c.call(c.alice, "PUT", f"llm/providers/{c.pa}", json={"models": priced})
    assert r.status_code == 403 and r.json().get("error_code") == "llm.provider_manager_admin_only"


@pytest.mark.e2e
def test_manager_cannot_switch_off_a_provider_others_start_on(cast):
    c = cast
    # Org small default on provider A → Alice can't turn A off.
    assert c.call(c.admin, "POST", f"llm/models/{c.m.a0}/set_default?small=true").status_code == 200
    r = c.call(c.alice, "POST", f"llm/providers/{c.pa}/toggle?enabled=false")
    assert r.status_code == 403 and r.json().get("error_code") == "llm.provider_holds_default"
    # A group default on provider B → Bob can't turn B off.
    assert c.call(c.admin, "PUT", f"llm/group-defaults/{c.group}", json={"default_llm_model_id": c.m.b0}).status_code == 200
    r = c.call(c.bob, "POST", f"llm/providers/{c.pb}/toggle?enabled=false")
    assert r.status_code == 400 and r.json().get("error_code") == "llm.model_is_group_default"


# ── the role ends ──────────────────────────────────────────────────────────

@pytest.mark.e2e
def test_deleting_the_provider_ends_the_role(cast):
    c = cast
    assert c.call(c.admin, "DELETE", f"llm/providers/{c.pb}").status_code == 200
    from app.dependencies import async_session_maker
    from app.models.resource_grant import ResourceGrant
    from sqlalchemy import select
    import asyncio

    async def _rows():
        async with async_session_maker() as db:
            return (await db.execute(select(ResourceGrant).where(
                ResourceGrant.resource_type == "llm_provider", ResourceGrant.resource_id == c.pb,
            ))).scalars().all()
    assert asyncio.run(_rows()) == []
    assert c.call(c.bob, "GET", "llm/providers").status_code == 403


@pytest.mark.e2e
def test_leaving_the_org_ends_the_role(cast):
    c = cast
    me = c.client.get("/api/organizations/" + c.org + "/members", headers=_h(c.admin["token"], c.org))
    assert me.status_code == 200, me.text
    member = next(x for x in me.json() if (x.get("user") or {}).get("id") == c.alice["user_id"]
                  or x.get("user_id") == c.alice["user_id"])
    r = c.client.delete(f"/api/organizations/{c.org}/members/{member['id']}", headers=_h(c.admin["token"], c.org))
    assert r.status_code in (200, 204), r.text
    assert c.call(c.admin, "GET", f"llm/providers/{c.pa}/managers").json() == []


@pytest.mark.e2e
def test_manager_changes_and_denials_are_audited(cast):
    c = cast
    assert c.call(c.alice, "POST", f"llm/models/{c.m.a2}/toggle?enabled=true").status_code == 200
    assert c.call(c.alice, "POST", f"llm/models/{c.m.b0}/toggle?enabled=false").status_code == 403
    body = c.call(c.admin, "GET", "enterprise/audit?limit=100").json()
    items = body.get("items", body if isinstance(body, list) else [])
    actions = [e["action"] for e in items]
    assert "llm_provider.manager_added" in actions
    toggled = next(e for e in items if e["action"] == "llm_model.toggled")
    assert toggled["details"]["as_provider_manager"] is True and toggled["details"]["restricted_on_enable"] is True
    assert "llm_model.toggled.denied" in actions


@pytest.mark.e2e
def test_admin_can_switch_a_provider_off_and_on(cast):
    """Regression: toggling a provider used to 500 for everyone (the joined
    eager load of its models needs Result.unique())."""
    c = cast
    assert c.call(c.admin, "POST", f"llm/providers/{c.pb}/toggle?enabled=false").status_code == 200
    assert c.call(c.admin, "POST", f"llm/providers/{c.pb}/toggle?enabled=true").status_code == 200


@pytest.mark.e2e
def test_managing_a_provider_does_not_mean_chatting_with_its_restricted_models(cast):
    """The settings listing shows a manager every model on their provider, but
    their chat picker still follows model access like everyone else's."""
    c = cast
    assert c.call(c.alice, "POST", f"llm/models/{c.m.a2}/toggle?enabled=true").status_code == 200
    picker = {x["id"] for x in c.call(c.alice, "GET", "llm/models").json()}
    assert c.m.a2 not in picker
    managed = {x["id"] for x in c.call(c.alice, "GET", "llm/models", params={"kind": "all", "manage": "true"}).json()}
    assert c.m.a2 in managed


# ── review round 1 ─────────────────────────────────────────────────────────

def _openai_provider(c, name, models, manager):
    """An OpenAI provider (it takes a base_url) with ``manager`` as its manager."""
    r = c.call(c.admin, "POST", "llm/providers", json={
        "name": f"{name}-{uuid.uuid4().hex[:6]}", "provider_type": "openai",
        "credentials": {"api_key": "sk-org-secret"}, "models": models,
    })
    assert r.status_code == 200, r.text
    pid = r.json()["id"]
    assert c.call(c.admin, "PUT", f"llm/providers/{pid}/managers/{manager['user_id']}").status_code == 200
    return pid


def _dialog_models(c, actor, pid):
    """The model list the provider dialog sends back (no prices)."""
    prov = next(p for p in c.call(actor, "GET", "llm/providers").json() if p["id"] == pid)
    return prov, [{"id": m["id"], "model_id": m["model_id"], "name": m["name"], "is_enabled": m["is_enabled"],
                   "is_custom": m.get("is_custom", False), "is_preset": m.get("is_preset", False)}
                  for m in prov["models"]]


@pytest.mark.e2e
def test_manager_moving_the_destination_must_reenter_the_key(cast):
    """The stored key is bound to where it was saved for: a manager can't
    point the provider elsewhere and have the org's key sent there."""
    c = cast
    pid = _openai_provider(c, "oai", [], c.alice)
    moved = {"credentials": {"base_url": "https://collector.example.com/v1"}}
    r = c.call(c.alice, "PUT", f"llm/providers/{pid}", json=moved)
    assert r.status_code == 400 and r.json().get("error_code") == "llm.provider_key_required_for_new_destination", r.text
    prov = next(p for p in c.call(c.admin, "GET", "llm/providers").json() if p["id"] == pid)
    assert not (prov.get("additional_config") or {}).get("base_url"), "the refused change must not be saved"
    # With a key of their own for the new destination, it's fine.
    r = c.call(c.alice, "PUT", f"llm/providers/{pid}",
               json={"credentials": {"base_url": "https://collector.example.com/v1", "api_key": "sk-their-own"}})
    assert r.status_code == 200, r.text
    # Unchanged for org LLM admins.
    assert c.call(c.admin, "PUT", f"llm/providers/{pid}",
                  json={"credentials": {"base_url": "https://other.example.com/v1"}}).status_code == 200


@pytest.mark.e2e
def test_manager_reenabling_a_provider_restricts_its_models(cast):
    c = cast
    assert c.call(c.admin, "POST", f"llm/providers/{c.pb}/toggle?enabled=false").status_code == 200
    assert c.call(c.bob, "POST", f"llm/providers/{c.pb}/toggle?enabled=true").status_code == 200
    for mid in (c.m.b0, c.m.b1):
        assert _model(c, mid)["is_restricted"] is True
    assert c.m.b0 not in {x["id"] for x in c.call(c.fiona, "GET", "llm/models").json()}


@pytest.mark.e2e
def test_an_admin_reenabling_a_provider_keeps_its_models_open(cast):
    c = cast
    assert c.call(c.admin, "POST", f"llm/providers/{c.pb}/toggle?enabled=false").status_code == 200
    assert c.call(c.admin, "POST", f"llm/providers/{c.pb}/toggle?enabled=true").status_code == 200
    assert _model(c, c.m.b0)["is_restricted"] is False


@pytest.mark.e2e
def test_grants_confer_nothing_without_the_license_and_admins_can_still_revoke(cast, monkeypatch):
    c = cast
    from app.ee import license as ee_license
    monkeypatch.setattr(ee_license, "has_feature", lambda feature: feature != "llm_access_control")
    assert c.call(c.alice, "PUT", f"llm/providers/{c.pa}", json={"credentials": {"api_key": "x"}}).status_code == 403
    assert c.call(c.alice, "POST", f"llm/models/{c.m.a0}/set_temperature?temperature=0.5").status_code == 403
    assert c.call(c.alice, "GET", "llm/models", params={"manage": "true"}).status_code == 403
    r = c.call(c.admin, "GET", f"llm/providers/{c.pa}/managers")
    assert r.status_code == 200 and [u["user_id"] for u in r.json()] == [c.alice["user_id"]]
    assert c.call(c.admin, "DELETE", f"llm/providers/{c.pa}/managers/{c.alice['user_id']}").status_code == 200
    assert c.call(c.admin, "GET", f"llm/providers/{c.pa}/managers").json() == []


@pytest.mark.e2e
def test_soft_deleted_models_do_not_block_a_managers_save(cast):
    c = cast
    assert c.call(c.admin, "DELETE", f"llm/models/{c.m.a1}").status_code == 200
    prov, models = _dialog_models(c, c.alice, c.pa)
    assert c.m.a1 not in {m["id"] for m in prov["models"]}, "deleted models are not returned"
    # Even a stale dialog that still carries the deleted model saves.
    stale = models + [{"id": c.m.a1, "model_id": "gone", "name": "gone", "is_enabled": True}]
    r = c.call(c.alice, "PUT", f"llm/providers/{c.pa}", json={"credentials": {"api_key": "rotated"}, "models": stale})
    assert r.status_code == 200, r.text
    # An id from another provider is still refused.
    foreign = models + [{"id": c.m.b0, "model_id": "x", "name": "x", "is_enabled": True}]
    assert c.call(c.alice, "PUT", f"llm/providers/{c.pa}", json={"models": foreign}).status_code == 403


@pytest.mark.e2e
def test_a_managers_save_keeps_an_admin_set_price(cast):
    """A custom model whose id matches a catalog entry used to get the catalog
    price written over the stored one when the dialog (which sends no prices)
    was saved."""
    c = cast
    pid = _openai_provider(c, "priced", [{"model_id": "gpt-6.2-sol", "name": "Sol custom", "is_custom": True}], c.alice)
    mid = next(m["id"] for m in c.call(c.admin, "GET", "llm/models", params={"kind": "all", "manage": "true"}).json()
               if m["provider"]["id"] == pid and m["model_id"] == "gpt-6.2-sol")
    r = c.call(c.admin, "POST", f"llm/models/{mid}/pricing",
               json={"input_cost_per_million_tokens_usd": 0.5, "output_cost_per_million_tokens_usd": 1.5})
    assert r.status_code == 200, r.text
    _prov, models = _dialog_models(c, c.alice, pid)
    r = c.call(c.alice, "PUT", f"llm/providers/{pid}", json={"credentials": {"api_key": "sk-rotated"}, "models": models})
    assert r.status_code == 200, r.text
    after = _model(c, mid)
    assert after["input_cost_per_million_tokens_usd"] == 0.5 and after["output_cost_per_million_tokens_usd"] == 1.5


# ── review round 2 ─────────────────────────────────────────────────────────

def _stored(pid):
    """(api_key, api_secret, additional_config) as stored for a provider."""
    import asyncio
    from app.dependencies import async_session_maker
    from app.models.llm_provider import LLMProvider

    async def _go():
        async with async_session_maker() as db:
            p = await db.get(LLMProvider, pid)
            try:
                key, secret = p.decrypt_credentials()
            except Exception:
                key, secret = None, None
            return key, secret, dict(p.additional_config or {})
    return asyncio.run(_go())


@pytest.mark.e2e
def test_moving_the_destination_never_keeps_the_omitted_stored_secret(cast):
    """Submitting only the *other* secret slot used to pass the check while the
    merge kept the stored API key, which then went to the new host."""
    c = cast
    pid = _openai_provider(c, "oai2", [], c.alice)
    key_before, _, _ = _stored(pid)
    assert key_before == "sk-org-secret"
    r = c.call(c.alice, "PUT", f"llm/providers/{pid}",
               json={"credentials": {"base_url": "https://collector.example.com/v1", "api_secret": "decoy"}})
    assert r.status_code == 400 and r.json().get("error_code") == "llm.provider_key_required_for_new_destination", r.text
    key, _secret, cfg = _stored(pid)
    assert key == "sk-org-secret" and not cfg.get("base_url"), "a refused move changes nothing"
    # A real key for the new host: saved, and only the submitted secrets remain.
    r = c.call(c.alice, "PUT", f"llm/providers/{pid}",
               json={"credentials": {"base_url": "https://collector.example.com/v1", "api_key": "sk-new-host"}})
    assert r.status_code == 200, r.text
    key, secret, cfg = _stored(pid)
    assert key == "sk-new-host" and secret is None and cfg["base_url"] == "https://collector.example.com/v1"


@pytest.mark.e2e
def test_azure_entra_client_secret_counts_as_the_credential_for_a_move(cast):
    c = cast
    r = c.call(c.admin, "POST", "llm/providers", json={
        "name": f"az-{uuid.uuid4().hex[:6]}", "provider_type": "azure",
        "credentials": {"endpoint_url": "https://old-resource.openai.azure.com/", "auth_mode": "entra_client_secret",
                        "tenant_id": "t-1", "client_id": "c-1", "client_secret": "org-client-secret"},
        "models": [],
    })
    assert r.status_code == 200, r.text
    pid = r.json()["id"]
    assert c.call(c.admin, "PUT", f"llm/providers/{pid}/managers/{c.alice['user_id']}").status_code == 200
    move = {"endpoint_url": "https://new-resource.openai.azure.com/", "auth_mode": "entra_client_secret",
            "tenant_id": "t-1", "client_id": "c-1"}
    # No secret, or an API key (not what Entra auth uses): refused.
    for extra in ({}, {"api_key": "not-the-client-secret"}):
        r = c.call(c.alice, "PUT", f"llm/providers/{pid}", json={"credentials": {**move, **extra}})
        assert r.status_code == 400, (extra, r.text)
    assert _stored(pid)[1] == "org-client-secret"
    # A new client secret: accepted, and it is what's stored.
    r = c.call(c.alice, "PUT", f"llm/providers/{pid}", json={"credentials": {**move, "client_secret": "new-client-secret"}})
    assert r.status_code == 200, r.text
    _key, secret, cfg = _stored(pid)
    assert secret == "new-client-secret" and cfg["endpoint_url"] == "https://new-resource.openai.azure.com/"


@pytest.mark.e2e
def test_a_refused_save_leaves_models_and_endpoint_untouched(cast):
    """The model writes used to commit before the destination check, so a 400
    still left a model turned on (and restricted)."""
    c = cast
    pid = _openai_provider(c, "atomic", [{"model_id": "atomic-a", "name": "Atomic A", "is_custom": True}], c.alice)
    mid = next(m["id"] for m in c.call(c.admin, "GET", "llm/models", params={"kind": "all", "manage": "true"}).json()
               if m["provider"]["id"] == pid)
    assert c.call(c.admin, "POST", f"llm/models/{mid}/toggle?enabled=false").status_code == 200
    _prov, models = _dialog_models(c, c.alice, pid)
    models = [dict(m, is_enabled=True) if m["id"] == mid else m for m in models]
    r = c.call(c.alice, "PUT", f"llm/providers/{pid}",
               json={"credentials": {"base_url": "https://collector.example.com/v1"}, "models": models})
    assert r.status_code == 400, r.text
    after = _model(c, mid)
    assert after["is_enabled"] is False and after["is_restricted"] is False
    assert not _stored(pid)[2].get("base_url")


@pytest.mark.e2e
def test_a_stale_dialog_never_reenables_a_deleted_model(cast):
    c = cast
    assert c.call(c.admin, "DELETE", f"llm/models/{c.m.a1}").status_code == 200
    _prov, models = _dialog_models(c, c.alice, c.pa)
    stale = models + [{"id": c.m.a1, "model_id": "gone", "name": "gone", "is_enabled": True}]
    for actor in (c.alice, c.admin):
        assert c.call(actor, "PUT", f"llm/providers/{c.pa}", json={"models": stale}).status_code == 200

    import asyncio
    from app.dependencies import async_session_maker
    from app.models.llm_model import LLMModel

    async def _row():
        async with async_session_maker() as db:
            m = await db.get(LLMModel, c.m.a1)
            return m.deleted_at is not None, m.is_enabled
    deleted, enabled = asyncio.run(_row())
    assert deleted and enabled is False
