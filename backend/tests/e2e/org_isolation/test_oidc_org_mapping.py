"""Operator-owned placement of people into organizations by identity-provider group.

Contract (``org_mapping`` on an OIDC provider in bow-config):
* At sign-in, every rule whose group the provider asserts places the person in
  the rule's organization with the rule's role. A mapped person needs no invite.
* A membership the mapping created ends when the person holds no group mapped
  to that organization any more. Memberships from anywhere else are left alone.
* A higher mapped role is granted on a later sign-in; a role is never lowered.
* A rule naming an unknown organization, or a full organization, is skipped
  without failing the sign-in. Groups that cannot be determined change nothing.
* Nobody is removed on doubt: while a group's display name is unknown, a rule
  keyed by display name keeps its organization's memberships.
* Deleting a rule, the last one included, ends the memberships it created.
* Group and profile sync run in every organization the person belongs to.
* With no mapping configured, sign-in behaves as before.

Only the identity provider is replaced (see ``identity_helpers``).
"""
import asyncio
import uuid

import httpx
import pytest

from app.dependencies import async_session_maker
from app.models.organization import Organization
from app.settings.config import settings as bow_settings
from tests.e2e.org_isolation.identity_helpers import (
    FakeIdentityProvider, bootstrap_admin, feature, hdr, invite, oidc_provider, unique_email, whoami,
)

G_SALES = "5d1f8a52-0c11-4d6f-9a3e-6f0f6f1a0001"
G_FINANCE = "5d1f8a52-0c11-4d6f-9a3e-6f0f6f1a0002"
G_FINANCE_LEADS = "5d1f8a52-0c11-4d6f-9a3e-6f0f6f1a0003"


@pytest.fixture
def multi_org():
    with feature("allow_multiple_organizations", True):
        yield


def _second_organization():
    """An organization other than the bootstrap admin's.

    Written directly: creating a further organization is an instance-admin
    operation with its own tests, and here only its existence matters."""
    name = f"Org {uuid.uuid4().hex[:8]}"

    async def _create():
        async with async_session_maker() as db:
            org = Organization(name=name)
            db.add(org)
            await db.commit()
            return str(org.id)

    return {"id": asyncio.run(_create()), "name": name}


def _roles(client, session):
    """organization id -> role, as the signed-in person sees it."""
    return {o["id"]: o["role"] for o in whoami(client, session)["organizations"]}


def _sign_in(idp, client, email, groups, provider="corp", **claims):
    outcome, session = idp.sign_in(client, provider, email, groups=groups, **claims)
    assert outcome == "ok", session
    return session


def _groups(client, session, org_id):
    resp = client.get(f"/api/organizations/{org_id}/groups", headers=hdr(session, org_id))
    assert resp.status_code == 200, resp.text
    return resp.json()


@pytest.mark.parametrize("form", ["objects", "compact"])
def test_first_sign_in_places_the_person_in_every_mapped_organization(test_client, monkeypatch, multi_org, form):
    admin = bootstrap_admin(test_client)
    other = _second_organization()
    rules = {
        "objects": [
            {"group": G_SALES, "organization": admin["org_id"], "role": "member"},
            {"group": G_FINANCE, "organization": other["name"], "role": "admin"},
        ],
        "compact": [f"{G_SALES}:{admin['org_id']}:member", f"{G_FINANCE}:{other['id']}:admin"],
    }[form]
    idp = FakeIdentityProvider(monkeypatch, [oidc_provider(org_mapping=rules)])

    session = _sign_in(idp, test_client, unique_email("mapped"), [G_FINANCE, G_SALES, str(uuid.uuid4())])

    assert _roles(test_client, session) == {admin["org_id"]: "member", other["id"]: "admin"}
    # The roles are real: admin-only surface opens in one organization only.
    assert test_client.get(f"/api/organizations/{other['id']}/members", headers=hdr(session, other["id"])).status_code == 200
    assert test_client.post(
        f"/api/organizations/{admin['org_id']}/members",
        json={"organization_id": admin["org_id"], "email": unique_email("x"), "role": "member"},
        headers=hdr(session, admin["org_id"]),
    ).status_code == 403


def test_person_without_a_mapped_group_still_needs_an_invitation(test_client, monkeypatch, multi_org):
    admin = bootstrap_admin(test_client)
    idp = FakeIdentityProvider(monkeypatch, [oidc_provider(org_mapping=[f"{G_SALES}:{admin['org_id']}:member"])])

    assert idp.sign_in(test_client, "corp", unique_email("stranger"), groups=[G_FINANCE]) == ("error", "invitation_required")
    assert idp.sign_in(test_client, "corp", unique_email("nogroups")) == ("error", "invitation_required")


def test_losing_the_group_ends_only_the_membership_the_mapping_created(test_client, monkeypatch, multi_org):
    admin = bootstrap_admin(test_client)
    other = _second_organization()
    idp = FakeIdentityProvider(monkeypatch, [oidc_provider(org_mapping=[
        f"{G_SALES}:{admin['org_id']}:member", f"{G_FINANCE}:{other['id']}:member",
        f"{G_FINANCE_LEADS}:{other['id']}:admin",
    ])])
    # The other organization's administrator, there through the mapping too.
    lead = unique_email("lead")
    assert _roles(test_client, _sign_in(idp, test_client, lead, [G_FINANCE_LEADS])) == {other["id"]: "admin"}
    mapped, invited = unique_email("mapped"), unique_email("invited")
    # ``invited`` is in the first organization by an admin's invite, not by the mapping.
    invite(test_client, admin["token"], admin["org_id"], invited)

    for email in (mapped, invited):
        session = _sign_in(idp, test_client, email, [G_SALES, G_FINANCE])
        assert set(_roles(test_client, session)) == {admin["org_id"], other["id"]}

    # Still in one mapped group: only the other organization's membership ends.
    assert set(_roles(test_client, _sign_in(idp, test_client, mapped, [G_SALES]))) == {admin["org_id"]}
    # In no mapped group at all.
    assert _roles(test_client, _sign_in(idp, test_client, mapped, [])) == {}
    assert _roles(test_client, _sign_in(idp, test_client, invited, [])) == {admin["org_id"]: "member"}
    # Regaining the group restores the membership.
    assert set(_roles(test_client, _sign_in(idp, test_client, mapped, [G_FINANCE]))) == {other["id"]}
    # An organization never loses its last administrator this way.
    assert _roles(test_client, _sign_in(idp, test_client, lead, [])) == {other["id"]: "admin"}


def test_removal_runs_the_member_removal_cleanup(test_client, monkeypatch, multi_org):
    admin = bootstrap_admin(test_client)
    idp = FakeIdentityProvider(monkeypatch, [oidc_provider(org_mapping=[f"{G_SALES}:{admin['org_id']}:member"])])
    email = unique_email("mapped")
    session = _sign_in(idp, test_client, email, [G_SALES])
    key = test_client.post(
        "/api/api_keys", json={"name": "k"}, headers=hdr(session, admin["org_id"]),
    )
    assert key.status_code == 200, key.text
    api_key = key.json()["key"]
    assert test_client.get("/api/users/whoami", headers={"Authorization": f"Bearer {api_key}"}).status_code == 200

    _sign_in(idp, test_client, email, [])

    members = test_client.get(
        f"/api/organizations/{admin['org_id']}/members", headers=hdr(admin["token"], admin["org_id"])
    ).json()
    assert email not in {(m.get("user") or {}).get("email") for m in members}
    assert test_client.get("/api/users/whoami", headers={"Authorization": f"Bearer {api_key}"}).status_code in (401, 403)


def test_higher_mapped_role_is_granted_and_never_lowered(test_client, monkeypatch, multi_org):
    admin = bootstrap_admin(test_client)
    org = admin["org_id"]
    idp = FakeIdentityProvider(monkeypatch, [oidc_provider(org_mapping=[
        f"{G_FINANCE}:{org}:member", f"{G_FINANCE_LEADS}:{org}:admin",
    ])])
    email = unique_email("mapped")

    assert _roles(test_client, _sign_in(idp, test_client, email, [G_FINANCE])) == {org: "member"}
    promoted = _sign_in(idp, test_client, email, [G_FINANCE, G_FINANCE_LEADS])
    assert _roles(test_client, promoted) == {org: "admin"}
    assert test_client.get(f"/api/organizations/{org}/members", headers=hdr(promoted, org)).status_code == 200
    # Roles are only ever raised by the mapping: an admin may have set this one.
    assert _roles(test_client, _sign_in(idp, test_client, email, [G_FINANCE])) == {org: "admin"}


def test_a_member_rule_does_not_give_an_app_user_the_workspace(test_client, monkeypatch, multi_org):
    """An app user (a member whose role withholds the workspace) is not a rank
    below member that the mapping lifts at the next sign-in: an admin took the
    workspace away, or a host application never gave it. An admin rule still
    applies."""
    admin = bootstrap_admin(test_client)
    org = admin["org_id"]
    idp = FakeIdentityProvider(monkeypatch, [oidc_provider(org_mapping=[
        f"{G_FINANCE}:{org}:member", f"{G_FINANCE_LEADS}:{org}:admin",
    ])])
    email = unique_email("appuser")
    session = _sign_in(idp, test_client, email, [G_FINANCE])
    user_id = whoami(test_client, session)["id"]
    members = test_client.get(f"/api/organizations/{org}/members", headers=hdr(admin["token"], org)).json()
    membership = next(m for m in members if m.get("user_id") == user_id)
    demoted = test_client.put(f"/api/organizations/{org}/members/{membership['id']}", json={"role": "app_user"},
                              headers=hdr(admin["token"], org))
    assert demoted.status_code == 200, demoted.text

    again = _sign_in(idp, test_client, email, [G_FINANCE])
    assert _roles(test_client, again) == {org: "app_user"}
    assert test_client.get("/api/reports", headers=hdr(again, org)).status_code == 403
    lead = _sign_in(idp, test_client, email, [G_FINANCE, G_FINANCE_LEADS])
    assert _roles(test_client, lead) == {org: "admin"}


def _provider_round_trip(test_client, idp, email, *, embed_host=None, carry=None):
    """Press a provider button (the authorization), come back from the
    provider (the callback). ``carry`` adds cookies a browser might still
    hold from an earlier authorization. Returns (session, cookies set)."""
    from urllib.parse import parse_qs, urlparse

    test_client.cookies.clear()
    started = test_client.get("/api/auth/corp/authorize", params={"embed_host": embed_host} if embed_host else None)
    assert started.status_code == 200, started.text
    state = parse_qs(urlparse(started.json()["authorization_url"]).query)["state"][0]
    cookies = {name: value for name, value in started.cookies.items()}
    test_client.cookies.clear()
    idp.claims = {"sub": f"sub-{email}", "email": email, "groups": [G_FINANCE]}
    jar = {**(carry or {}), **cookies, "oidc_corp_state": state, "oidc_corp_verifier": "verifier"}
    back = test_client.get("/api/auth/corp/callback", params={"code": "auth-code", "state": state},
                           headers={"Cookie": "; ".join(f"{k}={v}" for k, v in jar.items() if v)},
                           follow_redirects=False)
    assert back.status_code == 303, back.text
    code = parse_qs(urlparse(back.headers["location"]).query)["login_code"][0]
    return test_client.post("/api/auth/exchange", json={"login_code": code}).json()["access_token"], cookies


def _portal(test_client, admin, org):
    host = test_client.post("/api/host_applications", headers=hdr(admin["token"], org), json={
        "name": "Portal", "allowed_origins": ["https://portal.example.com"], "identity_mode": "sso"})
    assert host.status_code == 201, host.text
    return host.json()


def _member_of(test_client, admin, org, email):
    members = test_client.get(f"/api/organizations/{org}/members", headers=hdr(admin["token"], org)).json()
    return next(m for m in members if (m.get("user") or {}).get("email") == email)


@pytest.mark.parametrize("in_window", [True, False])
def test_a_provider_sign_in_started_in_an_applications_window_makes_an_app_user(test_client, monkeypatch, multi_org,
                                                                               in_window):
    """The sign-in page's provider button, inside an embedded artifact's
    sign-in window: the authorization itself names the host application
    (`?embed_host=`) and the intent is bound to that authorization's state,
    so the account the callback creates is that application's kind of
    account, however long the page sat open. The same sign-in outside a
    window makes a member."""
    admin = bootstrap_admin(test_client)
    org = admin["org_id"]
    idp = FakeIdentityProvider(monkeypatch, [oidc_provider(org_mapping=[f"{G_FINANCE}:{org}:member"])])
    host = _portal(test_client, admin, org)
    email = unique_email("button")
    session, cookies = _provider_round_trip(test_client, idp, email,
                                            embed_host=host["public_id"] if in_window else None)
    assert ("oidc_corp_embed" in cookies) is in_window

    member = _member_of(test_client, admin, org, email)  # read by the admin: what was stored
    assert member["role"] == ("app_user" if in_window else "member")
    assert member["created_via_host_application"] == ("Portal" if in_window else None)
    assert [r["name"] for r in member["roles"]] == ["app_user" if in_window else "member"]
    assert test_client.get("/api/reports", headers=hdr(session, org)).status_code == (403 if in_window else 200)


def test_an_abandoned_window_s_authorization_does_not_reach_another_sign_in(test_client, monkeypatch, multi_org):
    """A provider button was pressed inside an app's window and the window
    abandoned. An ordinary sign-in from another tab, even with that
    authorization's cookie still in the browser, is an ordinary one: the
    intent was bound to the other authorization's state."""
    admin = bootstrap_admin(test_client)
    org = admin["org_id"]
    idp = FakeIdentityProvider(monkeypatch, [oidc_provider(org_mapping=[f"{G_FINANCE}:{org}:member"])])
    host = _portal(test_client, admin, org)
    abandoned = test_client.get("/api/auth/corp/authorize", params={"embed_host": host["public_id"]})
    left_behind = {"oidc_corp_embed": abandoned.cookies.get("oidc_corp_embed")}
    assert left_behind["oidc_corp_embed"]

    email = unique_email("othertab")
    session, cookies = _provider_round_trip(test_client, idp, email, carry=left_behind)
    assert "oidc_corp_embed" not in cookies or not cookies["oidc_corp_embed"]
    member = _member_of(test_client, admin, org, email)
    assert member["role"] == "member" and member["created_via_host_application"] is None
    assert test_client.get("/api/reports", headers=hdr(session, org)).status_code == 200


@pytest.mark.parametrize("named", ["unknown", "disabled", "malformed"])
def test_an_authorization_that_cannot_be_tied_to_its_application_does_not_start(test_client, monkeypatch, multi_org,
                                                                               named):
    """Fail closed: a sign-in that says it belongs to a host application
    either is bound to it or does not begin. Beginning it unbound would make
    an ordinary member where the application says app user."""
    admin = bootstrap_admin(test_client)
    org = admin["org_id"]
    FakeIdentityProvider(monkeypatch, [oidc_provider(org_mapping=[f"{G_FINANCE}:{org}:member"])])
    host = _portal(test_client, admin, org)
    if named == "disabled":
        off = test_client.patch(f"/api/host_applications/{host['id']}", headers=hdr(admin["token"], org),
                                json={"revision": host["revision"], "enabled": False})
        assert off.status_code == 200, off.text
    embed_host = {"unknown": "host_" + "0" * 20, "disabled": host["public_id"], "malformed": "not-a-host"}[named]
    test_client.cookies.clear()
    started = test_client.get("/api/auth/corp/authorize", params={"embed_host": embed_host})
    assert started.status_code == 403, started.text
    assert "authorization_url" not in started.text and not started.cookies.get("oidc_corp_embed")
    # The same button outside a window still works.
    assert test_client.get("/api/auth/corp/authorize").status_code == 200


def test_rule_for_an_unknown_organization_is_skipped(test_client, monkeypatch, multi_org):
    admin = bootstrap_admin(test_client)
    idp = FakeIdentityProvider(monkeypatch, [oidc_provider(org_mapping=[
        f"{G_SALES}:{uuid.uuid4()}:admin",
        f"{G_SALES}:No Such Organization {uuid.uuid4().hex[:6]}:admin",
        f"{G_SALES}:{admin['org_id']}:member",
    ])])

    session = _sign_in(idp, test_client, unique_email("mapped"), [G_SALES])

    assert _roles(test_client, session) == {admin["org_id"]: "member"}


def test_full_organization_is_skipped_without_failing_the_sign_in(test_client, monkeypatch, multi_org):
    import app.ee.license as license_module

    admin = bootstrap_admin(test_client)
    other = _second_organization()
    idp = FakeIdentityProvider(monkeypatch, [oidc_provider(org_mapping=[
        f"{G_SALES}:{admin['org_id']}:member", f"{G_SALES}:{other['id']}:member",
    ])])
    # One seat per organization: the first is taken by its admin, the other is empty.
    monkeypatch.setattr(license_module, "get_max_users", lambda: 1)
    email = unique_email("mapped")

    assert _roles(test_client, _sign_in(idp, test_client, email, [G_SALES])) == {other["id"]: "member"}

    monkeypatch.setattr(license_module, "get_max_users", lambda: -1)
    assert set(_roles(test_client, _sign_in(idp, test_client, email, [G_SALES]))) == {admin["org_id"], other["id"]}


def test_mapping_applies_only_to_the_provider_it_is_configured_on(test_client, monkeypatch, multi_org):
    admin = bootstrap_admin(test_client)
    other = _second_organization()
    idp = FakeIdentityProvider(monkeypatch, [
        oidc_provider(name="corp", org_mapping=[f"{G_SALES}:{admin['org_id']}:member"]),
        oidc_provider(name="partner", issuer="https://partner.identity-tests.example",
                      org_mapping=[f"{G_FINANCE}:{other['id']}:member"]),
    ])
    email = unique_email("mapped")

    session = _sign_in(idp, test_client, email, [G_SALES, G_FINANCE], provider="corp")

    assert set(_roles(test_client, session)) == {admin["org_id"]}


def test_groups_that_cannot_be_determined_remove_nobody(test_client, monkeypatch, multi_org):
    admin = bootstrap_admin(test_client)
    idp = FakeIdentityProvider(monkeypatch, [oidc_provider(org_mapping=[f"{G_SALES}:{admin['org_id']}:member"])])
    email = unique_email("mapped")
    _sign_in(idp, test_client, email, [G_SALES])

    # The provider signals "too many groups to list" and the directory lookup
    # (answered 404 by the fake provider) fails.
    outcome, session = idp.sign_in(test_client, "corp", email, _claim_names={"groups": "src1"})

    assert outcome == "ok"
    assert _roles(test_client, session) == {admin["org_id"]: "member"}


class _Directory(FakeIdentityProvider):
    """Also answers the directory's group-name lookups.

    ``names`` maps group id -> display name (None = the directory will not
    name it); ``names = None`` makes every lookup fail."""

    names = {}
    # Groups the person is in directly (None = all of ``names``). The rest of
    # ``names`` are reached through a nested group.
    direct = None

    def _respond(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "graph.microsoft.com" not in url:
            return super()._respond(request)
        if self.names is None:
            return httpx.Response(503, json={}, request=request)
        if url.endswith("/directoryObjects/getByIds"):
            import json
            asked = json.loads(request.content)["ids"]
            found = [{"id": g, "displayName": self.names[g]} for g in asked if self.names.get(g)]
            return httpx.Response(200, json={"value": found}, request=request)
        if "/me/transitiveMemberOf" in url or "/me/memberOf" in url:
            listed = self.names
            if "/me/memberOf" in url and self.direct is not None:
                listed = {g: n for g, n in self.names.items() if g in self.direct}
            groups = [
                {"@odata.type": "#microsoft.graph.group", "id": g, "displayName": n} for g, n in listed.items()
            ]
            return httpx.Response(200, json={"value": groups}, request=request)
        return httpx.Response(404, json={}, request=request)


def _use_rules(monkeypatch, rules, **options):
    """What the operator's bow-config holds from now on."""
    monkeypatch.setattr(bow_settings.bow_config, "oidc_providers", [oidc_provider(org_mapping=rules, **options)])


def _api_key(client, session, org_id):
    created = client.post("/api/api_keys", json={"name": "k"}, headers=hdr(session, org_id))
    assert created.status_code == 200, created.text
    return created.json()["key"]


def _key_works(client, key):
    return client.get("/api/users/whoami", headers={"Authorization": f"Bearer {key}"}).status_code == 200


def _oidc_groups_of(client, admin, user_id):
    return {
        g["external_id"] for g in _groups(client, admin["token"], admin["org_id"])
        if g["external_provider"] == "oidc" and user_id in g["member_user_ids"]
    }


G_OTHER = "5d1f8a52-0c11-4d6f-9a3e-6f0f6f1a0009"


@pytest.mark.parametrize("failure", ["lookup_fails", "nothing_named", "one_group_unnamed"])
def test_rule_by_display_name_removes_nobody_while_a_name_is_unknown(test_client, monkeypatch, multi_org, failure):
    admin = bootstrap_admin(test_client)
    org = admin["org_id"]
    idp = _Directory(monkeypatch, [oidc_provider(
        resolve_group_names=True, sync_groups=True, org_mapping=[f"Sales Team:{org}:admin"],
    )])
    idp.names = {G_SALES: "Sales Team", G_OTHER: "Everyone"}
    email = unique_email("mapped")
    session = _sign_in(idp, test_client, email, [G_SALES, G_OTHER])
    assert _roles(test_client, session) == {org: "admin"}
    user_id = whoami(test_client, session)["id"]
    key = _api_key(test_client, session, org)

    # Still in the group, but the directory cannot say what the groups are called.
    idp.names = {"lookup_fails": None, "nothing_named": {}, "one_group_unnamed": {G_OTHER: "Everyone"}}[failure]
    for _ in range(2):
        session = _sign_in(idp, test_client, email, [G_SALES, G_OTHER])
        assert _roles(test_client, session) == {org: "admin"}
        assert test_client.get(f"/api/organizations/{org}/members", headers=hdr(session, org)).status_code == 200
        assert _key_works(test_client, key)
        assert _oidc_groups_of(test_client, admin, user_id) == {G_SALES, G_OTHER}

    # Names resolve again and the person has left the group: now it is known.
    idp.names = {G_SALES: "Sales Team", G_OTHER: "Everyone"}
    session = _sign_in(idp, test_client, email, [G_OTHER])
    assert _roles(test_client, session) == {}
    assert not _key_works(test_client, key)


def test_rule_by_group_id_does_not_depend_on_names(test_client, monkeypatch, multi_org):
    admin = bootstrap_admin(test_client)
    other = _second_organization()
    idp = _Directory(monkeypatch, [oidc_provider(resolve_group_names=True, org_mapping=[
        f"{G_SALES}:{admin['org_id']}:member", f"Finance Team:{other['id']}:member",
        f"{G_FINANCE_LEADS}:{other['id']}:admin",
    ])])
    idp.names = {G_SALES: "Sales Team", G_FINANCE: "Finance Team", G_OTHER: "Everyone", G_FINANCE_LEADS: "Leads"}
    _sign_in(idp, test_client, unique_email("lead"), [G_FINANCE_LEADS])
    email = unique_email("mapped")
    assert set(_roles(test_client, _sign_in(idp, test_client, email, [G_SALES, G_FINANCE]))) == {admin["org_id"], other["id"]}

    # The lookup fails and the person is in neither group any more. The id rule
    # is decided by the ids; the name rule cannot be, so its organization waits.
    idp.names = None
    assert set(_roles(test_client, _sign_in(idp, test_client, email, [G_OTHER]))) == {other["id"]}
    # A failed lookup never stands in the way of granting by id either.
    assert set(_roles(test_client, _sign_in(idp, test_client, email, [G_SALES, G_OTHER]))) == {admin["org_id"], other["id"]}

    idp.names = {G_OTHER: "Everyone"}
    assert _roles(test_client, _sign_in(idp, test_client, email, [G_OTHER])) == {}


def test_name_rule_without_name_resolution_never_removes_while_groups_are_opaque(test_client, monkeypatch, multi_org):
    admin = bootstrap_admin(test_client)
    org = admin["org_id"]
    # The provider sends object ids and is not asked to resolve names.
    idp = _Directory(monkeypatch, [oidc_provider(org_mapping=[f"{G_SALES}:{org}:member", f"Sales Team:{org}:member"])])
    email = unique_email("mapped")
    assert _roles(test_client, _sign_in(idp, test_client, email, [G_SALES])) == {org: "member"}

    # Any of the remaining opaque groups could be the one called "Sales Team".
    assert _roles(test_client, _sign_in(idp, test_client, email, [G_OTHER])) == {org: "member"}
    # In no group at all, nothing is left to doubt.
    assert _roles(test_client, _sign_in(idp, test_client, email, [])) == {}


def test_readable_group_values_are_their_own_names(test_client, monkeypatch, multi_org):
    admin = bootstrap_admin(test_client)
    org = admin["org_id"]
    # Providers that put group names straight in the claim need no lookup.
    idp = FakeIdentityProvider(monkeypatch, [oidc_provider(org_mapping=[f"sales-team:{org}:member"])])
    email = unique_email("mapped")
    assert _roles(test_client, _sign_in(idp, test_client, email, ["sales-team", "everyone"])) == {org: "member"}
    assert _roles(test_client, _sign_in(idp, test_client, email, ["everyone"])) == {}


def test_group_names_that_differ_only_by_case_are_different_groups(test_client, monkeypatch, multi_org):
    admin = bootstrap_admin(test_client)
    org = admin["org_id"]
    idp = FakeIdentityProvider(monkeypatch, [oidc_provider(
        org_mapping=[f"Finance:{org}:admin", f"everyone:{org}:member"],
    )])

    # In "finance", which is not the mapped "Finance".
    lower = _sign_in(idp, test_client, unique_email("lower"), ["finance", "everyone"])
    assert _roles(test_client, lower) == {org: "member"}

    exact = _sign_in(idp, test_client, unique_email("exact"), ["Finance", "everyone"])
    assert _roles(test_client, exact) == {org: "admin"}


def test_resolved_display_names_match_exactly_too(test_client, monkeypatch, multi_org):
    admin = bootstrap_admin(test_client)
    org = admin["org_id"]
    idp = _Directory(monkeypatch, [oidc_provider(
        resolve_group_names=True, org_mapping=[f"Finance:{org}:admin", f"Everyone:{org}:member"],
    )])
    idp.names = {G_SALES: "finance", G_OTHER: "Everyone"}

    session = _sign_in(idp, test_client, unique_email("mapped"), [G_SALES, G_OTHER])

    assert _roles(test_client, session) == {org: "member"}


def test_group_object_ids_still_match_in_any_letter_case(test_client, monkeypatch, multi_org):
    admin = bootstrap_admin(test_client)
    org = admin["org_id"]
    idp = FakeIdentityProvider(monkeypatch, [oidc_provider(org_mapping=[f"{G_SALES.upper()}:{org}:member"])])

    session = _sign_in(idp, test_client, unique_email("mapped"), [G_SALES.lower()])

    assert _roles(test_client, session) == {org: "member"}


def test_too_many_groups_to_list_keeps_a_membership_held_through_a_nested_group(
    test_client, monkeypatch, multi_org,
):
    admin = bootstrap_admin(test_client)
    org = admin["org_id"]
    idp = _Directory(monkeypatch, [oidc_provider(org_mapping=[f"{G_SALES}:{org}:admin"])])
    # The person is directly in G_OTHER only; G_SALES contains it.
    idp.names = {G_SALES: "Sales Team", G_OTHER: "Sales EMEA"}
    idp.direct = {G_OTHER}
    email = unique_email("mapped")
    # While the token can list the groups it lists the nested one as well.
    session = _sign_in(idp, test_client, email, [G_SALES, G_OTHER])
    assert _roles(test_client, session) == {org: "admin"}
    key = _api_key(test_client, session, org)

    outcome, session = idp.sign_in(test_client, "corp", email, _claim_names={"groups": "src1"})

    assert outcome == "ok", session
    assert _roles(test_client, session) == {org: "admin"}
    assert _key_works(test_client, key)


def test_too_many_groups_to_list_uses_the_directory_and_its_gaps_remove_nobody(test_client, monkeypatch, multi_org):
    admin = bootstrap_admin(test_client)
    org = admin["org_id"]
    idp = _Directory(monkeypatch, [oidc_provider(org_mapping=[f"Sales Team:{org}:member"])])
    email = unique_email("mapped")
    overage = {"_claim_names": {"groups": "src1"}}

    def sign_in():
        outcome, session = idp.sign_in(test_client, "corp", email, **overage)
        assert outcome == "ok", session
        return _roles(test_client, session)

    idp.names = {G_SALES: "Sales Team", G_OTHER: "Everyone"}
    assert sign_in() == {org: "member"}
    idp.names = {G_SALES: None, G_OTHER: "Everyone"}
    assert sign_in() == {org: "member"}
    idp.names = {G_OTHER: "Everyone"}
    assert sign_in() == {}


def test_deleting_rules_ends_the_memberships_they_created(test_client, monkeypatch, multi_org):
    admin = bootstrap_admin(test_client)
    other = _second_organization()
    sales, finance = f"{G_SALES}:{admin['org_id']}:member", f"{G_FINANCE}:{other['id']}:member"
    lead_rule = f"{G_FINANCE_LEADS}:{other['id']}:admin"
    idp = FakeIdentityProvider(monkeypatch, [oidc_provider(org_mapping=[sales, finance, lead_rule])])
    lead, mapped, invited = unique_email("lead"), unique_email("mapped"), unique_email("invited")
    _sign_in(idp, test_client, lead, [G_FINANCE_LEADS])
    invite(test_client, admin["token"], admin["org_id"], invited)
    groups = [G_SALES, G_FINANCE]
    for email in (mapped, invited):
        assert set(_roles(test_client, _sign_in(idp, test_client, email, groups))) == {admin["org_id"], other["id"]}
    key = _api_key(test_client, _sign_in(idp, test_client, mapped, groups), admin["org_id"])

    # One rule deleted: its organization goes, the other rule still holds.
    _use_rules(monkeypatch, [sales, lead_rule])
    assert set(_roles(test_client, _sign_in(idp, test_client, mapped, groups))) == {admin["org_id"]}
    assert _roles(test_client, _sign_in(idp, test_client, invited, groups)) == {admin["org_id"]: "member"}
    assert _key_works(test_client, key)

    # The last rule deleted: what the mapping created goes, the invite stays.
    _use_rules(monkeypatch, [])
    assert _roles(test_client, _sign_in(idp, test_client, mapped, groups)) == {}
    assert not _key_works(test_client, key)
    assert _roles(test_client, _sign_in(idp, test_client, invited, groups)) == {admin["org_id"]: "member"}
    # The organization's only administrator is kept even so.
    assert _roles(test_client, _sign_in(idp, test_client, lead, [G_FINANCE_LEADS])) == {other["id"]: "admin"}


def test_with_no_rules_left_removal_does_not_wait_for_the_groups(test_client, monkeypatch, multi_org):
    admin = bootstrap_admin(test_client)
    rule = f"{G_SALES}:{admin['org_id']}:member"
    idp = FakeIdentityProvider(monkeypatch, [oidc_provider(org_mapping=[rule])])
    email = unique_email("mapped")
    _sign_in(idp, test_client, email, [G_SALES])
    unknown_groups = {"_claim_names": {"groups": "src1"}}

    outcome, session = idp.sign_in(test_client, "corp", email, **unknown_groups)
    assert outcome == "ok" and set(_roles(test_client, session)) == {admin["org_id"]}

    _use_rules(monkeypatch, [])
    outcome, session = idp.sign_in(test_client, "corp", email, **unknown_groups)
    assert outcome == "ok" and _roles(test_client, session) == {}


class _GoogleWorkspace(FakeIdentityProvider):
    """Also answers the profile lookup made with the sign-in token."""

    profile_name = "Dana Example"

    def _respond(self, request: httpx.Request) -> httpx.Response:
        if "oauth2/v1/userinfo" in str(request.url):
            return httpx.Response(200, json={"name": self.profile_name}, request=request)
        return super()._respond(request)


def _enable_profile_sync(client, session, org_id):
    resp = client.put(
        "/api/organization/identity/google-profile-sync",
        json={"enabled": True, "fields": ["displayName"]}, headers=hdr(session, org_id),
    )
    assert resp.status_code == 200, resp.text


def _synced_profile(client, session, org_id):
    resp = client.get("/api/users/me/instructions", headers=hdr(session, org_id))
    assert resp.status_code == 200, resp.text
    return resp.json().get("profile_attributes") or {}


def test_groups_and_profile_sync_into_every_organization_of_the_person(test_client, monkeypatch, multi_org):
    admin = bootstrap_admin(test_client)
    first, second = admin["org_id"], _second_organization()["id"]
    idp = _GoogleWorkspace(monkeypatch, [oidc_provider(
        issuer="https://accounts.google.com", sync_groups=True, org_mapping=[f"{G_FINANCE}:{second}:admin"],
    )])
    groups = ["analysts", "finance-leads", G_FINANCE]

    # The instance's first admin, an admin of both organizations after sign-in.
    session = _sign_in(idp, test_client, admin["email"], groups)
    assert _roles(test_client, session) == {first: "admin", second: "admin"}

    for org_id in (first, second):
        synced = {g["external_id"]: g for g in _groups(test_client, session, org_id) if g["external_provider"] == "oidc"}
        assert set(synced) == set(groups)
        assert all(admin["id"] in g["member_user_ids"] for g in synced.values())

    # Profile sync is each organization's own switch.
    _enable_profile_sync(test_client, session, second)
    session = _sign_in(idp, test_client, admin["email"], groups)
    assert _synced_profile(test_client, session, second) == {"displayName": idp.profile_name}
    assert _synced_profile(test_client, session, first) == {}

    _enable_profile_sync(test_client, session, first)
    idp.profile_name = "Dana Renamed"
    session = _sign_in(idp, test_client, admin["email"], groups[:1] + [G_FINANCE])
    for org_id in (first, second):
        assert _synced_profile(test_client, session, org_id) == {"displayName": "Dana Renamed"}
        mine = {g["external_id"] for g in _groups(test_client, session, org_id) if admin["id"] in g["member_user_ids"]}
        assert mine == {"analysts", G_FINANCE}


@pytest.mark.parametrize("mapping", [[], None])
def test_without_a_mapping_sign_in_and_group_sync_are_unchanged(test_client, monkeypatch, mapping):
    admin = bootstrap_admin(test_client)
    other = _second_organization()
    options = {"sync_groups": True}
    if mapping is not None:
        options["org_mapping"] = mapping
    idp = FakeIdentityProvider(monkeypatch, [oidc_provider(**options)])
    member = unique_email("member")
    invite(test_client, admin["token"], admin["org_id"], member)

    # Uninvited people are refused whatever groups they carry.
    assert idp.sign_in(test_client, "corp", unique_email("stranger"), groups=[G_SALES]) == ("error", "invitation_required")

    session = _sign_in(idp, test_client, member, [G_SALES, other["id"], other["name"]])
    assert _roles(test_client, session) == {admin["org_id"]: "member"}
    synced = {
        g["external_id"]: g["member_user_ids"]
        for g in _groups(test_client, admin["token"], admin["org_id"]) if g["external_provider"] == "oidc"
    }
    assert set(synced) == {G_SALES, other["id"], other["name"]}
    assert all(whoami(test_client, session)["id"] in ids for ids in synced.values())

    # Leaving every group changes group membership only, never the organization.
    session = _sign_in(idp, test_client, member, [])
    assert _roles(test_client, session) == {admin["org_id"]: "member"}
