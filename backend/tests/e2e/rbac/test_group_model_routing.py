"""E2E tests for the per-group Auto model router switch (Group.model_routing).

Contract (see docs/design/auto-model-routing.md, "Per-group on/off"):
- a group set to Off never routes its members, even when the org setting is on;
- a group set to On routes its members, even when the org setting is off;
- a group left on Inherit (null) follows the org setting;
- a member in several groups gets the value of the highest-priority group
  (lower ``default_llm_priority`` wins, ties by name) that has a value —
  groups on Inherit are skipped;
- an explicit model pick still bypasses routing;
- turning a group On needs the ``model_routing`` license; Off / Inherit don't;
- the console shows routing savings when any group has the router on.

Routing is asserted through ``CompletionService._resolve_completion_models``,
the method every chat path calls, plus the ``routing_enabled`` flag it hands
the agent (which decides whether ``route_model`` is advertised).
"""
import asyncio
import uuid
from types import SimpleNamespace

import pytest

from app.dependencies import async_session_maker
from app.ee import license as ee_license


def _headers(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _run(coro):
    return asyncio.run(coro)


def _set_org_routing(test_client, token, org_id, on: bool):
    r = test_client.put(
        "/api/organization/settings",
        json={"config": {"model_routing": {"value": bool(on)}}},
        headers=_headers(token, org_id),
    )
    assert r.status_code == 200, r.json()


def _put_group(test_client, token, org_id, group_id, **body):
    return test_client.put(
        f"/api/llm/group-defaults/{group_id}", json=body, headers=_headers(token, org_id)
    )


def _list_groups(test_client, token, org_id):
    r = test_client.get("/api/llm/group-defaults", headers=_headers(token, org_id))
    assert r.status_code == 200, r.json()
    return {row["group_id"]: row for row in r.json()}


def _create_report(test_client, token, org_id):
    r = test_client.post(
        "/api/reports",
        json={"title": "Group routing", "files": [], "data_sources": []},
        headers=_headers(token, org_id),
    )
    assert r.status_code == 200, r.json()
    return r.json()["id"]


def _resolve(org_id, user_id, report_id, prompt_model_id=None):
    """(model_id, routing_meta) the way the chat paths resolve a run."""
    from app.services.completion_service import CompletionService
    from app.models.organization import Organization
    from app.models.user import User
    from app.models.report import Report

    async def _go():
        async with async_session_maker() as db:
            organization = await db.get(Organization, org_id)
            user = await db.get(User, user_id)
            report = await db.get(Report, report_id)
            model, _small, meta = await CompletionService()._resolve_completion_models(
                db, organization, user, report, prompt_model_id
            )
            return str(model.id), meta

    return _run(_go())


def _routes(cast, user_key):
    """True when this member's run starts small under the router."""
    u = cast.users[user_key]
    report_id = _create_report(cast.client, u["token"], cast.org)
    model_id, meta = _resolve(cast.org, u["user_id"], report_id)
    routed = bool(meta.get("routed"))
    # The flag handed to the agent always agrees with the starting model.
    assert bool(meta.get("routing_enabled")) is routed, meta
    assert model_id == (cast.small_id if routed else cast.default_id)
    return routed


@pytest.fixture
def cast(test_client, bootstrap_admin, invite_user_to_org, create_group, add_user_to_group,
         enterprise_license, monkeypatch):
    """Org with small != default and one guided model; two groups.

    Users: g1 (group 1), g2 (group 2), both (groups 1 and 2), none (no group).
    Group names sort group 1 first on a priority tie.
    """
    # enterprise_license patches has_feature, but modules that imported it at
    # load time (the org-settings service) still run the real one against the
    # fake license, whose feature list omits model_routing — add it there too.
    info = ee_license.LicenseInfo(
        licensed=True, tier="enterprise", org_name="grprout-tests",
        features=list(ee_license.get_license_info().features or []) + ["model_routing", "llm_access_control"],
        license_id="test-group-routing",
    )
    monkeypatch.setattr(ee_license, "get_license_info", lambda force_refresh=False: info)
    monkeypatch.setattr(ee_license, "_cached_license", info)
    admin = bootstrap_admin("grprout")
    org, t = admin["org_id"], admin["token"]
    suffix = uuid.uuid4().hex[:6]
    r = test_client.post(
        "/api/llm/providers",
        json={
            "name": f"prov-{suffix}",
            "provider_type": "anthropic",
            "credentials": {"api_key": "dummy-key"},
            "models": [
                {"model_id": f"grprout-{suffix}-{i}", "name": f"GR {suffix} {i}", "is_custom": True}
                for i in range(2)
            ],
        },
        headers=_headers(t, org),
    )
    assert r.status_code == 200, r.json()
    models = test_client.get("/api/llm/models", headers=_headers(t, org)).json()
    small_id, default_id = models[0]["id"], models[1]["id"]
    for mid, small in ((small_id, True), (default_id, False)):
        r = test_client.post(
            f"/api/llm/models/{mid}/set_default?small={'true' if small else 'false'}",
            headers=_headers(t, org),
        )
        assert r.status_code == 200, r.json()
    # A guided escalation target, so routing has somewhere to go.
    r = test_client.post(
        f"/api/llm/models/{default_id}/routing_hint",
        json={"hint": "standard analysis"},
        headers=_headers(t, org),
    )
    assert r.status_code == 200, r.json()

    users = {k: invite_user_to_org(org_id=org, admin_token=t) for k in ("g1", "g2", "both", "none")}

    def _group(name):
        r = create_group(name=name, user_token=t, org_id=org)
        assert r.status_code == 200, r.json()
        return r.json()["id"]

    group1 = _group(f"a-group1-{suffix}")
    group2 = _group(f"b-group2-{suffix}")
    for key, gids in (("g1", [group1]), ("g2", [group2]), ("both", [group1, group2])):
        for gid in gids:
            r = add_user_to_group(group_id=gid, user_id=users[key]["user_id"], user_token=t, org_id=org)
            assert r.status_code in (200, 201), r.text

    return SimpleNamespace(
        client=test_client, org=org, admin_t=t, users=users,
        group1=group1, group2=group2, small_id=small_id, default_id=default_id,
    )


# ── the three values against the org setting ───────────────────────────────


@pytest.mark.e2e
@pytest.mark.parametrize("org_on", [True, False])
@pytest.mark.parametrize("group_value", [True, False, None])
def test_group_value_overrides_org_and_inherit_follows_it(cast, org_on, group_value):
    _set_org_routing(cast.client, cast.admin_t, cast.org, org_on)
    r = _put_group(cast.client, cast.admin_t, cast.org, cast.group1, model_routing=group_value)
    assert r.status_code == 200, r.json()
    assert r.json()["model_routing"] is group_value

    expected = org_on if group_value is None else group_value
    assert _routes(cast, "g1") is expected
    # Members outside the group are unaffected: they follow the org.
    assert _routes(cast, "g2") is org_on
    assert _routes(cast, "none") is org_on


# ── several groups: priority decides, Inherit is skipped ───────────────────


@pytest.mark.e2e
def test_member_in_several_groups_gets_highest_priority_value(cast):
    _set_org_routing(cast.client, cast.admin_t, cast.org, False)
    _put_group(cast.client, cast.admin_t, cast.org, cast.group1, model_routing=False, default_llm_priority=10)
    _put_group(cast.client, cast.admin_t, cast.org, cast.group2, model_routing=True, default_llm_priority=5)
    assert _routes(cast, "both") is True  # group 2 (5) beats group 1 (10)

    _put_group(cast.client, cast.admin_t, cast.org, cast.group2, default_llm_priority=20)
    assert _routes(cast, "both") is False  # now group 1 (10) wins

    # Tie on priority → name order (group 1 sorts first).
    _put_group(cast.client, cast.admin_t, cast.org, cast.group2, default_llm_priority=10)
    assert _routes(cast, "both") is False


@pytest.mark.e2e
def test_inherit_group_is_skipped_for_a_lower_priority_value(cast):
    _set_org_routing(cast.client, cast.admin_t, cast.org, False)
    # Higher-priority group 1 has no value; group 2 below it says On.
    _put_group(cast.client, cast.admin_t, cast.org, cast.group1, model_routing=None, default_llm_priority=1)
    _put_group(cast.client, cast.admin_t, cast.org, cast.group2, model_routing=True, default_llm_priority=50)
    assert _routes(cast, "both") is True


# ── explicit pick still wins ───────────────────────────────────────────────


@pytest.mark.e2e
def test_explicit_pick_bypasses_group_routing(cast):
    _set_org_routing(cast.client, cast.admin_t, cast.org, False)
    _put_group(cast.client, cast.admin_t, cast.org, cast.group1, model_routing=True)
    u = cast.users["g1"]
    report_id = _create_report(cast.client, u["token"], cast.org)
    model_id, meta = _resolve(cast.org, u["user_id"], report_id, prompt_model_id=cast.default_id)
    assert model_id == cast.default_id
    assert not meta.get("routed")


# ── API: round-trip and license gate ───────────────────────────────────────


@pytest.mark.e2e
def test_group_routing_round_trips_and_other_fields_are_untouched(cast):
    assert _list_groups(cast.client, cast.admin_t, cast.org)[cast.group1]["model_routing"] is None

    r = _put_group(cast.client, cast.admin_t, cast.org, cast.group1, model_routing=True)
    assert r.status_code == 200, r.json()
    # Sending another field alone leaves model_routing as it was.
    r = _put_group(cast.client, cast.admin_t, cast.org, cast.group1, default_llm_priority=7)
    assert r.status_code == 200, r.json()
    row = _list_groups(cast.client, cast.admin_t, cast.org)[cast.group1]
    assert (row["model_routing"], row["default_llm_priority"]) == (True, 7)

    # null = back to Inherit.
    r = _put_group(cast.client, cast.admin_t, cast.org, cast.group1, model_routing=None)
    assert r.status_code == 200, r.json()
    assert _list_groups(cast.client, cast.admin_t, cast.org)[cast.group1]["model_routing"] is None


@pytest.mark.e2e
def test_group_routing_requires_manage_llm(cast):
    member = cast.users["g1"]
    r = _put_group(cast.client, member["token"], cast.org, cast.group1, model_routing=True)
    assert r.status_code in (401, 403)


@pytest.mark.e2e
def test_turning_group_on_needs_model_routing_license(cast, monkeypatch):
    _put_group(cast.client, cast.admin_t, cast.org, cast.group1, model_routing=True)
    # Licensed for everything except the Auto router.
    monkeypatch.setattr(ee_license, "has_feature", lambda feature: feature != "model_routing")

    r = _put_group(cast.client, cast.admin_t, cast.org, cast.group2, model_routing=True)
    assert r.status_code == 402, r.json()
    # Off and Inherit stay allowed, so a lapsed license can't strand a group on.
    assert _put_group(cast.client, cast.admin_t, cast.org, cast.group2, model_routing=False).status_code == 200
    assert _put_group(cast.client, cast.admin_t, cast.org, cast.group2, model_routing=None).status_code == 200
    # And without the license a group already set On never routes.
    assert _routes(cast, "g1") is False


# ── console shows savings when only a group has the router on ──────────────


@pytest.mark.e2e
def test_console_reports_routing_enabled_when_only_a_group_is_on(cast):
    from app.services.console_service import ConsoleService
    from app.schemas.console_schema import MetricsQueryParams
    from app.models.organization import Organization

    async def _enabled():
        async with async_session_maker() as db:
            organization = await db.get(Organization, cast.org)
            metrics = await ConsoleService().get_llm_usage_metrics(db, organization, MetricsQueryParams())
            return metrics.routing.enabled

    _set_org_routing(cast.client, cast.admin_t, cast.org, False)
    assert _run(_enabled()) is False
    _put_group(cast.client, cast.admin_t, cast.org, cast.group1, model_routing=True)
    assert _run(_enabled()) is True
    _put_group(cast.client, cast.admin_t, cast.org, cast.group1, model_routing=False)
    assert _run(_enabled()) is False


# ── access-check error: restricted candidates are dropped, open ones kept ──


@pytest.mark.e2e
def test_candidate_access_check_error_drops_restricted_models(cast, monkeypatch):
    """If the access check throws, a restricted model is never offered as a
    routing target (same posture as the fallback chain); an unrestricted one
    stays, since everyone may use it anyway."""
    from app.core import permission_resolver
    from app.ai.model_router import resolve_routing_candidates
    from app.models.organization import Organization
    from app.models.user import User

    t = cast.admin_t
    r = cast.client.post(
        f"/api/llm/models/{cast.small_id}/routing_hint",
        json={"hint": "simple lookups"},
        headers=_headers(t, cast.org),
    )
    assert r.status_code == 200, r.json()
    r = cast.client.put(
        f"/api/llm/models/{cast.default_id}/restricted",
        json={"is_restricted": True},
        headers=_headers(t, cast.org),
    )
    assert r.status_code == 200, r.json()

    async def _boom(*args, **kwargs):
        raise RuntimeError("access check unavailable")

    monkeypatch.setattr(permission_resolver, "user_can_use_model", _boom)

    async def _candidates():
        async with async_session_maker() as db:
            organization = await db.get(Organization, cast.org)
            user = await db.get(User, cast.users["g1"]["user_id"])
            return {str(m.id) for m in await resolve_routing_candidates(db, organization, user)}

    assert _run(_candidates()) == {cast.small_id}
