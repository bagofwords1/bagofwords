"""App users: members who have the apps shared with them, not the workspace.

The contract, through the HTTP surface:

* ``access_workspace`` is what separates a workspace member from an app user.
  Every role that existed before keeps it (nobody loses the workspace on
  upgrade); the built-in ``app_user`` role is the one without it, and a custom
  role without it makes an app user too. Roles add up: any role or group that
  carries it gives the workspace.
* An app user's BOW session reaches their account, the launcher and the
  published artifacts shared with them, and is refused (typed, 403) by the rest
  of the API. An embedded session keeps working exactly as before.
* The role is decided where the account is created: a host application gives
  the people its sign-in creates its new-user role (app user by default), in
  every identity mode including the SSO popup. Someone who already has an
  account, or who was invited, keeps the role they have; opening more apps
  never changes it. Giving or taking the workspace is a role change, effective
  on the next request.
* The launcher ("Shared with you") lists exactly the published artifacts the
  reader can open -- apps, dashboards and docs, each marked with its kind and
  filterable by it -- and an app user can ask the people who manage members
  for the workspace.
"""
from __future__ import annotations

import base64
import hashlib
import secrets
import uuid

import pytest

from tests.e2e.dashboards.dashboard_setup import as_, own, publish, share
from tests.e2e.dashboards.test_embed_identity_sources import (  # noqa: F401  (provider is a fixture)
    _count, _domain_signup, _host, _invite, _link, _session, provider,
)

pytestmark = pytest.mark.e2e

DASH = "conv_three_dash/dash_sales"
DECK = "conv_three_dash/deck"
ONE = "conv_one_dash/dash"
DOC = "doc_only/doc"
OTHER_DOC = "doc_after_page/doc"
ERP = "https://erp.example.com"
REFUSED = "workspace.access_required"

# What the workspace is made of (a sample across routers and verbs) ...
WORKSPACE = [
    ("GET", "/api/reports"), ("POST", "/api/reports"), ("GET", "/api/artifacts"), ("GET", "/api/data_sources"),
    ("GET", "/api/notifications/count"), ("GET", "/api/llm/models"), ("GET", "/api/organization/members"),
    ("PUT", "/api/organization/locale"), ("POST", "/api/organizations"), ("GET", "/api/users/me/default_model"),
    ("GET", "/api/instructions"), ("GET", "/api/api_keys"),
]
# ... and what an app user keeps.
# (whoami and the organizations list are covered in tests/e2e/rbac/test_app_user_session.py)
OPEN = ["/api/organization/locale", "/api/organization/settings", "/api/apps", "/api/apps/available",
        "/api/users/me/viewer_context", "/api/settings"]


# ── helpers ───────────────────────────────────────────────────────────────

class People:
    def __init__(self, client, corpus):
        self.client, self.corpus = client, corpus
        self.org = corpus.cast.org_id
        self.admin = as_(corpus, "admin")

    def members(self):
        r = self.client.get(f"/api/organizations/{self.org}/members", headers=self.admin)
        assert r.status_code == 200, r.text
        return r.json()

    def member(self, user_id):
        return next(m for m in self.members() if m.get("user_id") == user_id)

    def set_role(self, person, role):
        membership = self.member(self.corpus.cast.id(person))
        r = self.client.put(f"/api/organizations/{self.org}/members/{membership['id']}", json={"role": role},
                            headers=self.admin)
        assert r.status_code == 200, r.text
        return r.json()

    def roles(self):
        r = self.client.get(f"/api/organizations/{self.org}/roles", headers=self.admin)
        assert r.status_code == 200, r.text
        return {role["name"]: role for role in r.json()}

    def assign(self, role_id, *, user=None, group=None):
        body = {"role_id": role_id, "principal_type": "user" if user else "group",
                "principal_id": self.corpus.cast.id(user) if user else group}
        r = self.client.post(f"/api/organizations/{self.org}/role-assignments", json=body, headers=self.admin)
        assert r.status_code in (200, 201), r.text
        return r.json()

    def has_workspace(self, headers):
        return self.client.get("/api/reports", headers=headers).status_code == 200


@pytest.fixture
def people(test_client, corpus):
    return People(test_client, corpus)


@pytest.fixture
def app_user(people):
    """The corpus member, made an app user by an admin."""
    assert people.set_role("member", "app_user")["role"] == "app_user"
    return as_(people.corpus, "member")


def _host_app(client, corpus, **overrides):
    body = {"name": f"Portal {uuid.uuid4().hex[:6]}", "allowed_origins": [ERP], "identity_mode": "sso"}
    body.update(overrides)
    r = client.post("/api/host_applications", json=body, headers=as_(corpus, "admin"))
    assert r.status_code == 201, r.text
    return r.json()


def _allow(client, corpus, artifact_id, *hosts, by="owner"):
    r = client.put(f"/api/artifacts/{artifact_id}/embedding", json={"host_application_ids": [h["id"] for h in hosts]},
                   headers=as_(corpus, by))
    assert r.status_code == 200, r.text


def _app(client, corpus, corpus_rows, key, host, visibility="internal", users=None, groups=None):
    """A corpus artifact, published, shared and (with a ``host``) allowed in it."""
    aid = own(corpus_rows, corpus, key)
    publish(aid)
    assert share(client, corpus, aid, visibility, users=users, groups=groups).status_code == 200
    if host is not None:
        _allow(client, corpus, aid, host)
    return aid


def _apps(client, headers):
    r = client.get("/api/apps", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def _all_apps(client, headers, **query):
    """Every page of the launcher, as the page's "Show more" walks them."""
    cards, after = [], 0
    while after is not None:
        r = client.get("/api/apps", headers=headers, params={**query, "after": after})
        assert r.status_code == 200, r.text
        cards += r.json()["apps"]
        after = r.json()["next"]
    return cards


def _pkce():
    verifier = secrets.token_urlsafe(48)
    return verifier, base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


def _intent(client, host):
    """What the sign-up form does as it submits inside ``host``'s sign-in
    window: asks for an intent, to send with that one request."""
    r = client.post(f"/api/embed/{host['public_id']}/sso/intent")
    assert r.status_code == 200, r.text
    return {"X-Embed-Signup": r.json()["token"]}


def _register(client, email, intent=None, password="Password123!"):
    return client.post("/api/auth/register", headers=intent or {},
                       json={"name": f"Person {email.split('@')[0]}", "email": email, "password": password})


def _sign_up(client, email, intent=None, password="Password123!"):
    """BOW's ordinary sign-up and sign-in, as the SSO popup runs them."""
    r = _register(client, email, intent, password)
    assert r.status_code == 201, r.text
    login = client.post("/api/auth/jwt/login", data={"username": email, "password": password})
    assert login.status_code == 200, login.text
    return {"Authorization": f"Bearer {login.json()['access_token']}"}, r.json()["id"]


# ── the permission ────────────────────────────────────────────────────────

@pytest.mark.parametrize("person", ["admin", "owner", "member"])
def test_everyone_who_had_the_workspace_keeps_it(people, person):
    assert people.has_workspace(as_(people.corpus, person))
    assert people.member(people.corpus.cast.id(person))["role"] == ("admin" if person == "admin" else "member")


def test_the_built_in_roles_are_admin_member_and_app_user(people):
    roles = people.roles()
    assert {"admin", "member", "app_user"} <= set(roles)
    assert "access_workspace" in roles["member"]["permissions"]
    assert "access_workspace" not in roles["app_user"]["permissions"]
    assert "full_admin_access" not in roles["app_user"]["permissions"]


def test_the_role_editor_can_grant_the_workspace(people):
    r = people.client.get("/api/permissions/registry", headers=people.admin)
    if r.status_code == 404:
        r = people.client.get(f"/api/organizations/{people.org}/permissions/registry", headers=people.admin)
    assert r.status_code == 200, r.text
    offered = [p for group in r.json()["categories"].values() for p in group]
    assert "access_workspace" in offered


# ── the gate ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("method,path", WORKSPACE)
def test_the_workspace_is_closed_to_an_app_user(people, app_user, method, path):
    r = people.client.request(method, path, headers=app_user, json={} if method != "GET" else None)
    assert r.status_code == 403, (method, path, r.status_code, r.text)
    assert r.json()["error_code"] == REFUSED


@pytest.mark.parametrize("method,path", WORKSPACE)
def test_the_workspace_gate_does_not_touch_a_member(people, method, path):
    """The same calls by a workspace member are answered by the route itself."""
    r = people.client.request(method, path, headers=as_(people.corpus, "owner"), json={} if method != "GET" else None)
    assert r.status_code != 401
    assert not (r.status_code == 403 and r.json().get("error_code") == REFUSED), (method, path, r.text)


@pytest.mark.parametrize("path", OPEN)
def test_an_app_user_keeps_their_account_and_the_launcher(people, app_user, path):
    r = people.client.get(path, headers=app_user)
    assert r.status_code == 200, (path, r.status_code, r.text)


def test_an_app_user_opens_what_is_shared_with_them_and_nothing_else(people, app_user, test_client, corpus,
                                                                     corpus_rows):
    host = _host_app(test_client, corpus)
    shared = _app(test_client, corpus, corpus_rows, DASH, host, "shared", users=["member"])
    private = _app(test_client, corpus, corpus_rows, DECK, host, "shared", users=["direct_share"])
    assert test_client.get(f"/api/d/{shared}", headers=app_user).status_code == 200
    assert test_client.get(f"/api/d/{private}", headers=app_user).status_code in (403, 404)
    # The artifact's runtime answers them as it answers any reader.
    policy = test_client.get(f"/api/artifacts/{shared}/runtime/conversations/policy", headers=app_user)
    assert policy.status_code == 200, policy.text


def test_the_workspace_follows_the_role_on_the_next_request(people):
    headers = as_(people.corpus, "member")
    assert people.has_workspace(headers)
    people.set_role("member", "app_user")
    assert not people.has_workspace(headers)
    assert people.set_role("member", "member")["role"] == "member"
    assert people.has_workspace(headers)
    assert people.member(people.corpus.cast.id("member"))["role"] == "member"


def test_only_someone_who_manages_members_changes_it(people, app_user):
    """An app user cannot give themselves the workspace, and neither can a peer."""
    membership = people.member(people.corpus.cast.id("member"))
    for person in ("member", "owner"):
        r = people.client.put(f"/api/organizations/{people.org}/members/{membership['id']}", json={"role": "member"},
                              headers=as_(people.corpus, person))
        assert r.status_code == 403, (person, r.status_code, r.text)
    assert not people.has_workspace(app_user)


def test_roles_add_up_so_any_workspace_role_gives_the_workspace(people, app_user):
    roles = people.roles()
    assignment = people.assign(roles["member"]["id"], user="member")
    assert people.has_workspace(app_user)
    member = people.member(people.corpus.cast.id("member"))
    assert member["role"] == "member"
    # Taking it away again leaves the app_user role, which carries nothing.
    r = people.client.delete(f"/api/organizations/{people.org}/role-assignments/{assignment['id']}",
                             headers=people.admin)
    assert r.status_code in (200, 204), r.text
    assert not people.has_workspace(app_user)
    assert people.member(people.corpus.cast.id("member"))["role"] == "app_user"


def test_a_group_role_gives_its_app_users_the_workspace(people, app_user, enterprise_license):
    """How a directory promotes people: a synced group that carries a workspace role."""
    org = people.org
    group = people.client.post(f"/api/organizations/{org}/groups", json={"name": "BOW Full Users"},
                               headers=people.admin)
    assert group.status_code in (200, 201), group.text
    gid = group.json()["id"]
    people.assign(people.roles()["member"]["id"], group=gid)
    assert not people.has_workspace(app_user)  # not in the group yet
    added = people.client.post(f"/api/organizations/{org}/groups/{gid}/members",
                               json={"user_id": people.corpus.cast.id("member")}, headers=people.admin)
    assert added.status_code in (200, 201), added.text
    assert people.has_workspace(app_user)
    assert people.member(people.corpus.cast.id("member"))["role"] == "member"
    removed = people.client.delete(
        f"/api/organizations/{org}/groups/{gid}/members/{people.corpus.cast.id('member')}", headers=people.admin)
    assert removed.status_code in (200, 204), removed.text
    assert not people.has_workspace(app_user)


@pytest.mark.parametrize("permissions,workspace", [
    (["manage_instructions"], False),
    (["access_workspace", "manage_instructions"], True),
])
def test_a_custom_role_gives_the_workspace_only_if_it_carries_it(people, app_user, enterprise_license, permissions,
                                                                workspace):
    created = people.client.post(f"/api/organizations/{people.org}/roles", headers=people.admin,
                                 json={"name": f"Custom {uuid.uuid4().hex[:6]}", "permissions": permissions})
    assert created.status_code in (200, 201), created.text
    people.assign(created.json()["id"], user="member")
    assert people.has_workspace(app_user) is workspace
    assert people.member(people.corpus.cast.id("member"))["role"] == ("member" if workspace else "app_user")


def test_an_embedded_session_serves_an_app_user_as_before(people, app_user, test_client, corpus, corpus_rows):
    """The frame's own session is not the BOW session: it never had the workspace."""
    from tests.e2e.dashboards.test_embed_host_applications import Embed, bearer

    embed = Embed(test_client, corpus)
    host = embed.create()
    aid = _app(test_client, corpus, corpus_rows, DASH, host)
    embed.attribute("member", "E-1")
    token = embed.sign_in(host, "E-1")
    assert test_client.get(f"/api/d/{aid}", headers=bearer(token)).status_code == 200
    assert test_client.get("/api/reports", headers=bearer(token)).status_code == 401


# ── where the role comes from ─────────────────────────────────────────────

def _newcomer(provider, oid="oid-new-1", email="newcomer@fresh.example"):
    return lambda: provider.token("x", oid=oid, email=email, name="New Comer")


@pytest.mark.parametrize("new_user_role,workspace", [(None, False), ("app_user", False), ("member", True)])
def test_a_host_application_gives_the_people_it_creates_its_role(provider, people, corpus, corpus_rows, test_client,
                                                                new_user_role, workspace):
    _domain_signup(corpus_rows, corpus, "fresh.example")
    body = {"name": "Back office (Entra)", "allowed_origins": [ERP], "identity_mode": "idp_token",
            "identity_source": "oidc:entra", "audience": "api://chinook-backoffice", "create_users": True}
    if new_user_role:
        body["new_user_role"] = new_user_role
    host = test_client.post("/api/host_applications", json=body, headers=people.admin).json()
    assert host["new_user_role"] == (new_user_role or "app_user")

    first = _session(test_client, host, _newcomer(provider)())
    assert first.status_code == 200, first.text
    member = people.member(first.json()["user"]["id"])
    assert member["role"] == ("member" if workspace else "app_user")
    assert member["created_via_host_application"] == host["name"]
    assert [r["name"] for r in member["roles"]] == ["member" if workspace else "app_user"]
    # Coming back changes nothing, whatever the application now says.
    patched = test_client.patch(f"/api/host_applications/{host['id']}", headers=people.admin,
                                json={"revision": host["revision"], "new_user_role": "member"})
    assert patched.status_code == 200, patched.text
    again = _session(test_client, patched.json(), _newcomer(provider)())
    assert again.status_code == 200 and again.json()["user"]["id"] == first.json()["user"]["id"]
    assert people.member(first.json()["user"]["id"])["role"] == member["role"]


def test_an_invitation_keeps_the_role_it_grants(provider, people, corpus, test_client):
    """An admin invited them as a member: arriving through an app does not undo that."""
    host = _host(test_client, corpus, create_users=True)
    _invite(test_client, corpus, "invited@fresh.example")
    r = _session(test_client, host, provider.token("x", oid="oid-inv", email="invited@fresh.example", name="In Vited"))
    assert r.status_code == 200, r.text
    member = people.member(r.json()["user"]["id"])
    assert member["role"] == "member" and member["created_via_host_application"] is None


def test_someone_who_already_has_an_account_keeps_their_role(provider, people, corpus, corpus_rows, test_client):
    host = _host(test_client, corpus, create_users=True)
    _link(corpus_rows, corpus, "member", "oid-member")
    r = _session(test_client, host, provider.token("x", oid="oid-member"))
    assert r.status_code == 200 and r.json()["user"]["id"] == corpus.cast.id("member")
    member = people.member(corpus.cast.id("member"))
    assert member["role"] == "member" and member["created_via_host_application"] is None
    assert people.has_workspace(as_(corpus, "member"))


def test_the_sso_popup_gives_an_account_it_creates_the_hosts_role(people, corpus, corpus_rows, test_client):
    _domain_signup(corpus_rows, corpus, "popup.example")
    host = _host_app(test_client, corpus)
    headers, user_id = _sign_up(test_client, "ada@popup.example", _intent(test_client, host))
    headers["X-Organization-Id"] = people.org
    _, challenge = _pkce()
    code = test_client.post(f"/api/embed/{host['public_id']}/sso/code", json={"code_challenge": challenge},
                            headers=headers)
    assert code.status_code == 200, code.text
    member = people.member(user_id)
    assert member["role"] == "app_user" and member["created_via_host_application"] == host["name"]
    assert not people.has_workspace(headers)
    assert _apps(test_client, headers)["workspace_access"] is False


def test_signing_up_to_bow_itself_makes_a_member(people, corpus, corpus_rows, test_client):
    """No popup, no host application: the account is an ordinary one, and
    opening an app afterwards does not take the workspace away."""
    _domain_signup(corpus_rows, corpus, "direct.example")
    host = _host_app(test_client, corpus)
    headers, user_id = _sign_up(test_client, "bo@direct.example")
    headers["X-Organization-Id"] = people.org
    _, challenge = _pkce()
    assert test_client.post(f"/api/embed/{host['public_id']}/sso/code", json={"code_challenge": challenge},
                            headers=headers).status_code == 200
    member = people.member(user_id)
    assert member["role"] == "member" and member["created_via_host_application"] is None
    assert people.has_workspace(headers)


def test_the_popup_never_changes_an_existing_members_role(people, corpus, test_client):
    host = _host_app(test_client, corpus)
    intent = _intent(test_client, host)
    _, challenge = _pkce()
    for person in ("member", "admin"):
        # Signing in again inside the window, intent attached, then its code.
        again = test_client.post("/api/auth/jwt/login", headers=intent, data={
            "username": corpus.cast.people[person].email, "password": "Password123!"})
        assert again.status_code in (200, 400), again.text
        r = test_client.post(f"/api/embed/{host['public_id']}/sso/code", json={"code_challenge": challenge},
                             headers={**as_(corpus, person), **intent})
        assert r.status_code == 200, r.text
    assert people.member(corpus.cast.id("member"))["role"] == "member"
    assert people.member(corpus.cast.id("admin"))["role"] == "admin"


def test_the_role_is_the_application_s_whose_window_they_signed_up_in(people, corpus, corpus_rows, test_client):
    _domain_signup(corpus_rows, corpus, "other.example")
    started_in = _host_app(test_client, corpus)
    signed_into = _host_app(test_client, corpus, new_user_role="member")
    headers, user_id = _sign_up(test_client, "cy@other.example", _intent(test_client, started_in))
    headers["X-Organization-Id"] = people.org
    _, challenge = _pkce()
    assert test_client.post(f"/api/embed/{signed_into['public_id']}/sso/code", json={"code_challenge": challenge},
                            headers=headers).status_code == 200
    member = people.member(user_id)
    assert member["role"] == "app_user" and member["created_via_host_application"] == started_in["name"]


@pytest.mark.parametrize("first_request", ["/api/reports", "/api/users/me/viewer_context", "/api/apps"])
def test_a_sign_up_window_that_never_finishes_still_makes_an_app_user(people, corpus, corpus_rows, test_client,
                                                                      first_request):
    """They registered in the window and closed it (or wandered off): the
    window never asked for its code. Whatever they do next with that session,
    they do it as an app user -- there is no request on which the workspace
    was theirs."""
    _domain_signup(corpus_rows, corpus, "closed.example")
    host = _host_app(test_client, corpus)
    headers, user_id = _sign_up(test_client, "eve@closed.example", _intent(test_client, host))
    headers["X-Organization-Id"] = people.org

    first = test_client.get(first_request, headers=headers)
    assert first.status_code == (403 if first_request == "/api/reports" else 200), first.text
    refused = test_client.get("/api/reports", headers=headers)
    assert refused.status_code == 403 and refused.json()["error_code"] == REFUSED
    member = people.member(user_id)
    assert member["role"] == "app_user" and member["created_via_host_application"] == host["name"]


def test_the_role_is_stored_when_the_account_is_registered(people, corpus, corpus_rows, test_client):
    """Before the new account has a session at all: nothing it could do
    first, and no later request, is needed for it to be an app user."""
    _domain_signup(corpus_rows, corpus, "atonce.example")
    host = _host_app(test_client, corpus)
    r = _register(test_client, "gal@atonce.example", _intent(test_client, host))
    assert r.status_code == 201, r.text
    member = people.member(r.json()["id"])
    assert member["role"] == "app_user" and member["created_via_host_application"] == host["name"]
    assert [role["name"] for role in member["roles"]] == ["app_user"]
    # Recorded once, for that account and that application.
    assert _count(corpus_rows, "select count(*) from audit_logs where action = 'embed.user_created' "
                               "and user_id = :u and resource_id = :h", u=r.json()["id"], h=host["id"]) == 1
    # Using the account does not record it again.
    login = test_client.post("/api/auth/jwt/login", data={"username": "gal@atonce.example",
                                                          "password": "Password123!"})
    headers = {"Authorization": f"Bearer {login.json()['access_token']}", "X-Organization-Id": people.org}
    assert test_client.get("/api/apps", headers=headers).status_code == 200
    assert _count(corpus_rows, "select count(*) from audit_logs where action = 'embed.user_created' "
                               "and user_id = :u", u=r.json()["id"]) == 1


def test_an_abandoned_window_does_not_follow_the_browser(people, corpus, corpus_rows, test_client):
    """Someone opens an app's sign-in window, leaves it, and signs up to BOW
    itself in the same browser. Nothing of the window travels with them: an
    intent is sent with one request, it is not a cookie."""
    _domain_signup(corpus_rows, corpus, "tabs.example")
    host = _host_app(test_client, corpus)
    opened = test_client.post(f"/api/embed/{host['public_id']}/sso/intent")
    assert opened.status_code == 200 and not opened.cookies and "set-cookie" not in opened.headers
    headers, user_id = _sign_up(test_client, "kai@tabs.example")  # the other tab: no intent
    headers["X-Organization-Id"] = people.org
    member = people.member(user_id)
    assert member["role"] == "member" and member["created_via_host_application"] is None
    assert people.has_workspace(headers)


@pytest.mark.parametrize("supplied", ["expired", "forged", "garbage", "for_another_transaction"])
def test_a_supplied_intent_that_is_not_valid_refuses_the_registration(people, corpus, corpus_rows, test_client,
                                                                      monkeypatch, supplied):
    """A request that says it belongs to a host application and cannot prove
    which is refused before any account exists. It is never run as an
    ordinary sign-up (that would make a member where the application says
    app user). The form asks again as it resubmits, and that one goes
    through; a sign-up with no intent at all is still an ordinary one."""
    import time

    import jwt

    from app.core import workspace_access

    _domain_signup(corpus_rows, corpus, "slow.example")
    host = _host_app(test_client, corpus)
    if supplied == "expired":
        monkeypatch.setattr(workspace_access, "INTENT_LIFETIME", -10)  # issued already expired
        header = _intent(test_client, host)
        monkeypatch.setattr(workspace_access, "INTENT_LIFETIME", 600)
    elif supplied == "forged":
        now = int(time.time())
        header = {"X-Embed-Signup": jwt.encode({
            "host": host["public_id"], "hid": host["id"], "org": people.org, "role": "member", "started": now - 1,
            "for": "form", "aud": "bow:embed-signup", "exp": now + 600}, "not-the-key", algorithm="HS256")}
    elif supplied == "garbage":
        header = {"X-Embed-Signup": "x"}
    else:  # a real token, issued for an OIDC authorization's state rather than a form
        from types import SimpleNamespace

        header = {"X-Embed-Signup": workspace_access.issue_signup_intent(SimpleNamespace(
            public_id=host["public_id"], id=host["id"], organization_id=people.org, new_user_role="app_user"),
            "some-oidc-state")}

    refused = _register(test_client, "hal@slow.example", header)
    assert refused.status_code == 403, refused.text
    assert refused.json()["error_code"] == "embed.not_allowed"
    assert _count(corpus_rows, "select count(*) from users where lower(email) = 'hal@slow.example'") == 0
    assert not [m for m in people.members() if "hal@slow.example" in str(m)]
    login = test_client.post("/api/auth/jwt/login", headers=header,
                             data={"username": "hal@slow.example", "password": "Password123!"})
    assert login.status_code == 403  # the same for a sign-in that could create a directory account

    again = _register(test_client, "hal@slow.example", _intent(test_client, host))  # the form resubmits
    assert again.status_code == 201, again.text
    assert people.member(again.json()["id"])["role"] == "app_user"
    plain = _register(test_client, "ivy@slow.example")  # no window at all
    assert plain.status_code == 201 and people.member(plain.json()["id"])["role"] == "member"


def test_a_disabled_application_opens_no_sign_up_window(people, corpus, corpus_rows, test_client):
    _domain_signup(corpus_rows, corpus, "late.example")
    host = _host_app(test_client, corpus, enabled=False)
    assert test_client.post(f"/api/embed/{host['public_id']}/sso/intent").status_code == 403
    headers, user_id = _sign_up(test_client, "flo@late.example")
    headers["X-Organization-Id"] = people.org
    assert people.has_workspace(headers)
    assert people.member(user_id)["role"] == "member"


def test_no_workspace_membership_survives_a_failed_role_write(people, corpus, corpus_rows, test_client, monkeypatch):
    """The role is part of the write that creates the membership. If it
    cannot be set, there is no membership at all — never a member's one that
    a later sign-in without the window's cookie would find."""
    from app.core import workspace_access

    _domain_signup(corpus_rows, corpus, "broken.example")
    host = _host_app(test_client, corpus)
    intent = _intent(test_client, host)

    def refuse(*args, **kwargs):
        raise RuntimeError("the role could not be written")

    monkeypatch.setattr(workspace_access, "stamp_membership", refuse)
    try:
        _register(test_client, "jo@broken.example", intent)
    except RuntimeError:
        pass  # the test client re-raises what the server would answer 500 for
    monkeypatch.undo()

    held = [m for m in people.members() if ((m.get("user") or {}).get("email") or m.get("email")) == "jo@broken.example"]
    assert all(m["role"] != "member" and not m.get("user_id") for m in held), held
    login = test_client.post("/api/auth/jwt/login", data={"username": "jo@broken.example", "password": "Password123!"})
    if login.status_code == 200:  # the account may exist; it belongs to nothing here
        headers = {"Authorization": f"Bearer {login.json()['access_token']}", "X-Organization-Id": people.org}
        assert test_client.get("/api/reports", headers=headers).status_code in (401, 403)


def test_more_apps_never_change_the_role(people, app_user, test_client, corpus, corpus_rows):
    """App 1, then app 2 in another host application: still an app user, both listed."""
    first, second = _host_app(test_client, corpus), _host_app(test_client, corpus, new_user_role="member")
    one = _app(test_client, corpus, corpus_rows, DASH, first, "shared", users=["member"])
    assert [a["id"] for a in _apps(test_client, app_user)["apps"]] == [one]
    two = _app(test_client, corpus, corpus_rows, ONE, second, "shared", users=["member"])
    _, challenge = _pkce()
    assert test_client.post(f"/api/embed/{second['public_id']}/sso/code", json={"code_challenge": challenge},
                            headers=app_user).status_code == 200
    listing = _apps(test_client, app_user)
    assert {a["id"] for a in listing["apps"]} == {one, two}
    assert listing["workspace_access"] is False
    assert people.member(corpus.cast.id("member"))["role"] == "app_user"


# ── the launcher ──────────────────────────────────────────────────────────

def _cards(client, headers, **query):
    return {a["id"]: a for a in _all_apps(client, headers, **query)}


@pytest.mark.parametrize("as_app_user", [True, False])
def test_the_launcher_lists_everything_the_reader_can_open(people, test_client, corpus, corpus_rows, as_app_user):
    """Apps, dashboards and docs alike: shared by name, by group or with the
    whole organization. Each card opens, and what is not listed would not."""
    host = _host_app(test_client, corpus)
    retired = _host_app(test_client, corpus, enabled=False)
    app = _app(test_client, corpus, corpus_rows, DASH, host, "shared", users=["member"])
    dashboard = _app(test_client, corpus, corpus_rows, ONE, None, "internal")
    doc = _app(test_client, corpus, corpus_rows, DOC, None, "shared", users=["member"])
    someone_elses = _app(test_client, corpus, corpus_rows, DECK, host, "shared", users=["direct_share"])
    unpublished = own(corpus_rows, corpus, OTHER_DOC)  # its conversation is shared with the organization
    if as_app_user:
        people.set_role("member", "app_user")
    reader = as_(corpus, "member")

    listing = _apps(test_client, reader)
    assert listing["workspace_access"] is (not as_app_user)
    cards = _cards(test_client, reader)
    assert {app, dashboard, doc} <= set(cards)
    assert not {someone_elses, unpublished} & set(cards)
    assert {cards[app]["kind"], cards[dashboard]["kind"], cards[doc]["kind"]} == {"app", "dashboard", "doc"}
    assert cards[app]["kind"] == "app" and cards[app]["applications"] == [host["name"]]
    assert cards[dashboard]["kind"] == "dashboard" and cards[dashboard]["applications"] == []
    assert cards[doc]["kind"] == "doc"
    # Every card opens; what is left out would be refused.
    for card in cards.values():
        assert test_client.get(f"/api/d/{card['ref']}", headers=reader).status_code == 200, card
    for refused in (someone_elses, unpublished):
        assert test_client.get(f"/api/d/{refused}", headers=reader).status_code in (403, 404)

    # Only an enabled host application makes an artifact an app, whatever its mode.
    _allow(test_client, corpus, dashboard, retired)
    assert _cards(test_client, reader)[dashboard]["kind"] == "dashboard"
    _allow(test_client, corpus, doc, host)
    assert _cards(test_client, reader)[doc]["kind"] == "app"
    # The publisher sees their own list, by the same rule.
    assert {app, dashboard, doc, someone_elses} <= set(_cards(test_client, as_(corpus, "owner")))


def test_a_group_share_reaches_an_app_users_launcher(people, test_client, corpus, corpus_rows):
    doc = _app(test_client, corpus, corpus_rows, DOC, None, "shared", groups=["analysts"])
    people.set_role("group_share", "app_user")
    reader = as_(corpus, "group_share")
    assert _apps(test_client, reader)["workspace_access"] is False
    assert _cards(test_client, reader)[doc]["kind"] == "doc"
    assert doc not in _cards(test_client, as_(corpus, "direct_share"))


def test_the_launcher_filters_by_kind(people, app_user, test_client, corpus, corpus_rows):
    host = _host_app(test_client, corpus)
    made = {
        "app": _app(test_client, corpus, corpus_rows, DASH, host, "shared", users=["member"]),
        "dashboard": _app(test_client, corpus, corpus_rows, ONE, None, "shared", users=["member"]),
        "doc": _app(test_client, corpus, corpus_rows, DOC, None, "internal"),
    }
    # A doc some host application may show is an app, and only that.
    doc_app = _app(test_client, corpus, corpus_rows, OTHER_DOC, host, "shared", users=["member"])
    everything = _cards(test_client, app_user)
    by_kind = {}
    for kind, aid in made.items():
        cards = _cards(test_client, app_user, kind=kind)
        assert aid in cards and all(card["kind"] == kind for card in cards.values()), (kind, cards)
        by_kind[kind] = set(cards)
    assert doc_app in by_kind["app"] and doc_app not in by_kind["doc"]
    # The filters split the whole list: nothing in two, nothing in none.
    assert set().union(*by_kind.values()) == set(everything)
    assert sum(len(ids) for ids in by_kind.values()) == len(everything)
    found = _cards(test_client, app_user, kind="doc", q=everything[made["doc"]]["title"])
    assert made["doc"] in found and made["app"] not in found
    assert test_client.get("/api/apps", headers=app_user, params={"kind": "report"}).status_code == 422


def test_an_unpublished_or_unshared_app_leaves_the_launcher(people, app_user, test_client, corpus, corpus_rows):
    host = _host_app(test_client, corpus)
    aid = _app(test_client, corpus, corpus_rows, DASH, host, "shared", users=["member"])
    assert [a["id"] for a in _apps(test_client, app_user)["apps"]] == [aid]
    assert share(test_client, corpus, aid, "shared", users=[]).status_code == 200
    assert _apps(test_client, app_user)["apps"] == []
    assert test_client.get("/api/apps/available", headers=app_user).json() == {"available": True}


def test_the_launcher_is_for_members(test_client, corpus):
    assert test_client.get("/api/apps").status_code == 401
    listing = _apps(test_client, as_(corpus, "admin"))
    assert listing["workspace_access"] is True and listing["access_requested"] is False


def _inbox(client, headers):
    r = client.get("/api/notifications", headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    rows = body.get("items", body) if isinstance(body, dict) else body
    return [n for n in rows if n.get("type") == "workspace_access_request"]


def test_an_app_user_asks_the_member_managers_for_the_workspace(people, app_user, test_client, corpus):
    assert _apps(test_client, app_user)["access_requested"] is False
    for _ in range(2):  # asking twice is one open ask
        r = test_client.post("/api/apps/access-request", headers=app_user)
        assert r.status_code == 200 and r.json() == {"access_requested": True, "workspace_access": False}
    assert _apps(test_client, app_user)["access_requested"] is True
    asks = _inbox(test_client, people.admin)
    assert len(asks) == 1 and asks[0]["link"] == "/settings/members"
    assert _inbox(test_client, as_(corpus, "owner")) == []  # a peer manages nobody

    people.set_role("member", "member")
    after = _apps(test_client, app_user)
    assert after["workspace_access"] is True and after["access_requested"] is False
    assert all(n.get("resolved_at") for n in _inbox(test_client, people.admin))


def test_a_member_with_the_workspace_has_nothing_to_ask_for(people, test_client, corpus):
    r = test_client.post("/api/apps/access-request", headers=as_(corpus, "member"))
    assert r.status_code == 200 and r.json() == {"access_requested": False, "workspace_access": True}
    assert _inbox(test_client, people.admin) == []


def test_a_card_pictures_the_app_only_for_those_who_may_see_the_publishers_data(people, app_user, test_client,
                                                                                corpus, corpus_rows, monkeypatch):
    """The thumbnail is a picture of the publisher's run. Where each viewer
    gets their own data (a connection people sign in to), it is theirs only."""
    from app.services import viewer_data_policy

    host = _host_app(test_client, corpus)
    aid = own(corpus_rows, corpus, "thumbnail/dash")
    release = publish(aid)
    assert share(test_client, corpus, aid, "internal").status_code == 200
    _allow(test_client, corpus, aid, host)
    live = f"/thumbnails/release-{release}.png"

    def card(headers):
        return next(a for a in _apps(test_client, headers)["apps"] if a["id"] == aid)

    assert card(app_user)["thumbnail_url"] == live

    async def yes(*args, **kwargs):
        return True

    monkeypatch.setattr(viewer_data_policy, "_any_user_scoped_connection", yes)
    assert card(app_user)["thumbnail_url"] is None          # still listed, without the picture
    assert card(as_(corpus, "owner"))["thumbnail_url"] == live  # their own run


# ── an app user is a reader, never an editor ──────────────────────────────

def test_an_app_user_reads_what_is_published_not_the_draft_of_what_they_own(people, test_client, corpus,
                                                                            corpus_rows):
    """Demoted while owning a dashboard with unpublished changes: the draft,
    and anything else an editor gets, is the workspace."""
    host = _host_app(test_client, corpus)
    aid = _app(test_client, corpus, corpus_rows, DASH, host, "internal")
    private = own(corpus_rows, corpus, ONE)
    publish(private)  # published, shared with nobody: only its owner's
    owner = as_(corpus, "owner")
    draft_paths = [f"/api/d/{aid}/draft", f"/api/d/{aid}/draft/content"]
    assert test_client.get(draft_paths[0], headers=owner).status_code == 200
    assert test_client.get(f"/api/d/{private}", headers=owner).status_code == 200

    people.set_role("owner", "app_user")
    for path in draft_paths:
        assert test_client.get(path, headers=owner).status_code in (401, 403, 404), path
    assert test_client.post(f"/api/d/{aid}/fork", headers=owner, json={}).status_code in (401, 403, 404)
    published = test_client.get(f"/api/d/{aid}", headers=owner)
    assert published.status_code == 200
    # Owning it is no longer a way in: sharing is.
    assert test_client.get(f"/api/d/{private}", headers=owner).status_code in (403, 404)
    assert [a["id"] for a in _all_apps(test_client, owner)] == [aid]

    people.set_role("owner", "member")
    assert test_client.get(draft_paths[0], headers=owner).status_code == 200
    assert test_client.get(f"/api/d/{private}", headers=owner).status_code == 200


# ── every app is reachable ────────────────────────────────────────────────

def test_the_launcher_pages_and_searches_past_what_the_reader_may_not_open(people, app_user, test_client, corpus,
                                                                           corpus_rows):
    """Three artifacts of mixed kinds sort before the reader's (an app and a
    doc), and are not theirs. A page of one still finds theirs, the pages add
    up to the whole list, in each kind too, and search runs over all of it."""
    host = _host_app(test_client, corpus)
    theirs = [_app(test_client, corpus, corpus_rows, DASH, host, "shared", users=["member"]),
              _app(test_client, corpus, corpus_rows, DOC, None, "shared", users=["member"])]
    others = [_app(test_client, corpus, corpus_rows, key, host if index else None, "shared", users=["direct_share"])
              for index, key in enumerate((OTHER_DOC, "thumbnail/dash", "conv_three_dash/dash_stock"))]
    for index, aid in enumerate(others):
        corpus_rows.update("artifacts", aid, title=f"AAA {index}")
    corpus_rows.update("artifacts", theirs[0], title="Zebra crossing")
    corpus_rows.update("artifacts", theirs[1], title="Yak shaving")

    first = test_client.get("/api/apps", headers=app_user, params={"limit": 1}).json()
    assert [a["title"] for a in first["apps"]] == ["Yak shaving"] and first["next"] is not None
    assert [a["title"] for a in _all_apps(test_client, app_user, limit=1)] == ["Yak shaving", "Zebra crossing"]
    assert test_client.get("/api/apps", headers=app_user).json()["next"] is None
    assert [a["title"] for a in _all_apps(test_client, app_user, kind="doc", limit=1)] == ["Yak shaving"]
    assert [a["title"] for a in _all_apps(test_client, app_user, kind="app", limit=1)] == ["Zebra crossing"]

    found = test_client.get("/api/apps", headers=app_user, params={"q": "zebra", "limit": 1}).json()
    assert [a["id"] for a in found["apps"]] == [theirs[0]] and found["next"] is None
    assert test_client.get("/api/apps", headers=app_user, params={"q": "AAA"}).json()["apps"] == []
    assert test_client.get("/api/apps", headers=app_user, params={"q": "%"}).json()["apps"] == []


# ── the migration ─────────────────────────────────────────────────────────

def test_replaying_the_migration_leaves_app_user_without_the_workspace(people, corpus_rows):
    """The dashboard corpus (and a half-applied upgrade) runs appuser01 more
    than once. The second run must not backfill the role the first one made."""
    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext
    from sqlalchemy import create_engine

    from tests.fixtures.dashboard_corpus.alembic_at import script_directory
    from tests.fixtures.dashboard_corpus.rows import sync_test_url

    engine = create_engine(sync_test_url())
    try:
        for _ in range(2):
            with engine.begin() as conn:
                with Operations.context(MigrationContext.configure(conn)):
                    script_directory().get_revision("appuser01").module.upgrade()
    finally:
        engine.dispose()
    roles = people.roles()
    assert roles["app_user"]["permissions"] == []
    assert "access_workspace" in roles["member"]["permissions"]
    assert len([r for r in people.client.get(f"/api/organizations/{people.org}/roles", headers=people.admin).json()
                if r["name"] == "app_user"]) == 1


# ── acting in an organization takes its workspace ─────────────────────────

def test_a_custom_role_cannot_fork_without_the_workspace(people, app_user, test_client, corpus, corpus_rows,
                                                         enterprise_license):
    """A fork is a new report. `create_reports` in a role that withholds the
    workspace is not enough, in the page's answer and in the write alike."""
    host = _host_app(test_client, corpus)
    aid = _app(test_client, corpus, corpus_rows, DASH, host, "internal")
    created = people.client.post(f"/api/organizations/{people.org}/roles", headers=people.admin,
                                 json={"name": f"Forker {uuid.uuid4().hex[:6]}", "permissions": ["create_reports"]})
    assert created.status_code in (200, 201), created.text
    people.assign(created.json()["id"], user="member")
    assert not people.has_workspace(app_user)

    page = test_client.get(f"/api/d/{aid}", headers=app_user)
    assert page.status_code == 200
    assert page.json()["fork_eligibility"]["can_fork"] is False
    before = people.client.get("/api/reports", headers=people.admin).json()
    refused = test_client.post(f"/api/d/{aid}/fork", headers=app_user, json={})
    assert refused.status_code in (401, 403, 404), refused.text
    assert people.client.get("/api/reports", headers=people.admin).json() == before


def test_fork_eligibility_needs_the_workspace_of_the_dashboards_organization(people, corpus, corpus_rows,
                                                                            test_client, enterprise_license):
    """The fork route names no organization, so the account-level gate lets
    through anyone with a workspace *somewhere*. The rule is asked of the
    dashboard's own organization (dashboard_lifecycle.fork_eligibility)."""
    import asyncio

    from app.dependencies import async_session_maker
    from app.models.artifact import Artifact
    from app.models.user import User
    from app.services.dashboard_access import published_release
    from app.services.dashboard_lifecycle import fork_eligibility

    host = _host_app(test_client, corpus)
    aid = _app(test_client, corpus, corpus_rows, DASH, host, "internal")
    created = people.client.post(f"/api/organizations/{people.org}/roles", headers=people.admin,
                                 json={"name": f"Forker {uuid.uuid4().hex[:6]}", "permissions": ["create_reports"]})
    assert created.status_code in (200, 201), created.text

    async def eligibility():
        async with async_session_maker() as db:
            artifact = await db.get(Artifact, aid)
            release = await published_release(db, artifact)
            return await fork_eligibility(db, artifact, release, await db.get(User, corpus.cast.id("member")))

    reason_as_member = asyncio.run(eligibility()).get("reason")
    assert reason_as_member != "no_permission"
    people.set_role("member", "app_user")
    people.assign(created.json()["id"], user="member")
    assert asyncio.run(eligibility()) == {"can_fork": False, "reason": "no_permission"}
