"""A dashboard's share link can carry a readable name: /r/{slug}.

Invariants under test:
- A name resolves to its dashboard (report_id + artifact_id) and is listed
  with it; the long link is unaffected.
- The name opens exactly what the long link opens: a private dashboard is
  not found for anyone but the owner, an org-wide one asks anonymous callers
  to sign in, a public one opens without a sign-in.
- Names are unique across organizations, case-insensitive, and must be
  3-80 lowercase letters/digits/single hyphens, not reserved, not UUID-shaped.
- Renaming keeps the old name pointing at the dashboard (resolving reports
  the current name); re-claiming an own old name moves it back; another
  dashboard can't take an old name while it still redirects.
- Clearing releases every name of the dashboard, and so does deleting the
  dashboard: the names stop resolving and only the same organization may
  claim them again (a link already sent is never taken over by another org).
  Archiving the conversation ("delete" in the UI) releases nothing, since
  its shared dashboards stay served on the long link.
- Only the owner names a dashboard; a fork does not carry the name.
"""
import uuid

import pytest


def _headers(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _name():
    return f"sales-{uuid.uuid4().hex[:8]}"


def _create_dashboard(test_client, *, report_id, token, org_id, title):
    resp = test_client.post(
        "/api/artifacts",
        json={"report_id": report_id, "title": title, "mode": "page",
              "content": {"code": f"<div>{title}</div>"}},
        headers=_headers(token, org_id),
    )
    assert resp.status_code == 200, resp.json()
    return resp.json()


def _set_slug(test_client, *, report_id, artifact_id, token, org_id, slug):
    return test_client.put(
        f"/api/reports/{report_id}/artifacts/{artifact_id}/slug",
        json={"slug": slug},
        headers=_headers(token, org_id),
    )


def _share(test_client, *, report_id, artifact_id, token, org_id, visibility):
    resp = test_client.put(
        f"/api/reports/{report_id}/artifacts/{artifact_id}/visibility",
        json={"visibility": visibility},
        headers=_headers(token, org_id),
    )
    assert resp.status_code == 200, resp.json()


def _resolve(test_client, slug, headers=None):
    return test_client.get(f"/api/artifact-links/{slug}", headers=headers or {})


@pytest.fixture
def owner_setup(test_client, create_user, login_user, whoami, invite_user_to_org, create_report):
    """An owner's report with two dashboards, plus a plain member of the org."""
    owner = create_user()
    token = login_user(owner["email"], owner["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    member = invite_user_to_org(org_id=org_id, admin_token=token)
    report = create_report(title=f"Slugs {uuid.uuid4().hex[:6]}", user_token=token, org_id=org_id, data_sources=[])
    sales = _create_dashboard(test_client, report_id=report["id"], token=token, org_id=org_id, title="Sales")
    payroll = _create_dashboard(test_client, report_id=report["id"], token=token, org_id=org_id, title="Payroll")
    return {"token": token, "org_id": org_id, "member": member, "report": report,
            "sales": sales, "payroll": payroll}


@pytest.fixture
def other_org(create_user, login_user, create_organization, create_report, test_client):
    """A second organization with its own dashboard."""
    user = create_user(email=f"other_org_{uuid.uuid4().hex[:8]}@test.com")
    token = login_user(user["email"], user["password"])
    org_id = create_organization(name=f"Other org {uuid.uuid4().hex[:6]}", user_token=token)
    report = create_report(title="Other org", user_token=token, org_id=org_id, data_sources=[])
    dash = _create_dashboard(test_client, report_id=report["id"], token=token, org_id=org_id, title="Theirs")
    return {"token": token, "org_id": org_id, "report": report, "dash": dash}


def _name_dashboard(test_client, c, dash_key, slug, expect=200):
    resp = _set_slug(test_client, report_id=c["report"]["id"], artifact_id=c[dash_key]["artifact_id"],
                     token=c["token"], org_id=c["org_id"], slug=slug)
    assert resp.status_code == expect, resp.json()
    return resp.json()


@pytest.mark.e2e
def test_name_resolves_to_its_dashboard_and_is_listed(test_client, owner_setup):
    c = owner_setup
    rid, sales = c["report"]["id"], c["sales"]
    slug = _name()
    assert _name_dashboard(test_client, c, "sales", slug.upper())["slug"] == slug  # stored lowercase

    owner = _headers(c["token"], c["org_id"])
    resp = _resolve(test_client, slug, owner)
    assert resp.status_code == 200, resp.json()
    assert resp.json() == {"report_id": rid, "artifact_id": sales["artifact_id"], "slug": slug}
    assert _resolve(test_client, slug.upper(), owner).status_code == 200

    listed = {a["artifact_id"]: a["slug"] for a in test_client.get(f"/api/artifacts/report/{rid}", headers=owner).json()}
    assert listed == {sales["artifact_id"]: slug, c["payroll"]["artifact_id"]: None}
    sharing = test_client.get(f"/api/reports/{rid}/artifacts/{sales['artifact_id']}/sharing", headers=owner).json()
    assert sharing["slug"] == slug

    # The long link still opens the dashboard.
    assert test_client.get(f"/api/r/{rid}/artifacts/{sales['id']}", headers=owner).status_code == 200


@pytest.mark.e2e
def test_name_opens_only_what_the_dashboard_allows(test_client, owner_setup):
    c = owner_setup
    slug = _name()
    _name_dashboard(test_client, c, "sales", slug)
    member = _headers(c["member"]["token"], c["org_id"])
    share = dict(report_id=c["report"]["id"], artifact_id=c["sales"]["artifact_id"],
                 token=c["token"], org_id=c["org_id"])

    # Private: hidden from everyone but the owner, as on the long link.
    assert _resolve(test_client, slug).status_code == 404
    assert _resolve(test_client, slug, member).status_code == 404

    # Shared with specific people (not this member): sign-in needed / denied.
    _share(test_client, visibility="shared", **share)
    assert _resolve(test_client, slug).status_code == 401
    assert _resolve(test_client, slug, member).status_code == 403

    _share(test_client, visibility="internal", **share)
    assert _resolve(test_client, slug).status_code == 401
    assert _resolve(test_client, slug, member).status_code == 200

    _share(test_client, visibility="public", **share)
    assert _resolve(test_client, slug).status_code == 200


@pytest.mark.e2e
def test_unknown_name_is_not_found(test_client, owner_setup):
    c = owner_setup
    assert _resolve(test_client, _name(), _headers(c["token"], c["org_id"])).status_code == 404


@pytest.mark.e2e
@pytest.mark.parametrize("bad", [
    "ab",                                   # too short
    "a" * 81,                               # too long
    "Sales 2026",                           # space
    "sales_2026",                           # underscore
    "-sales", "sales-", "sales--2026",      # hyphen placement
    "מכירות",                               # non-ASCII
    "api", "settings",                      # reserved
    "41b31a83-e25c-4162-a03e-3b77af6ce785", # UUID-shaped (fixed: xdist workers
    "41b31a83e25c4162a03e3b77af6ce785",     # must collect identical ids), undashed
])
def test_invalid_names_are_rejected(test_client, owner_setup, bad):
    c = owner_setup
    resp = _set_slug(test_client, report_id=c["report"]["id"], artifact_id=c["sales"]["artifact_id"],
                     token=c["token"], org_id=c["org_id"], slug=bad)
    assert resp.status_code == 422, resp.json()
    assert resp.json()["error_code"] == "artifact.slug_invalid"


@pytest.mark.e2e
def test_name_is_unique_across_organizations(test_client, owner_setup, other_org):
    c, o = owner_setup, other_org
    slug = _name()
    _name_dashboard(test_client, c, "sales", slug)

    resp = _name_dashboard(test_client, c, "payroll", slug, expect=409)
    assert resp["error_code"] == "artifact.slug_taken"
    resp = _set_slug(test_client, report_id=o["report"]["id"], artifact_id=o["dash"]["artifact_id"],
                     token=o["token"], org_id=o["org_id"], slug=slug)
    assert resp.status_code == 409, resp.json()

    # Saving the same name again is a no-op, not a conflict.
    assert _name_dashboard(test_client, c, "sales", slug)["slug"] == slug


@pytest.mark.e2e
def test_old_name_keeps_pointing_at_the_dashboard(test_client, owner_setup):
    c = owner_setup
    owner = _headers(c["token"], c["org_id"])
    old, new = _name(), _name()
    _name_dashboard(test_client, c, "sales", old)
    _name_dashboard(test_client, c, "sales", new)

    resp = _resolve(test_client, old, owner)
    assert resp.status_code == 200, resp.json()
    assert resp.json()["artifact_id"] == c["sales"]["artifact_id"]
    assert resp.json()["slug"] == new

    # Another dashboard can't take a name that still redirects.
    assert _name_dashboard(test_client, c, "payroll", old, expect=409)["error_code"] == "artifact.slug_taken"

    # Moving back to the old name makes it current again.
    _name_dashboard(test_client, c, "sales", old)
    assert _resolve(test_client, old, owner).json()["slug"] == old
    assert _resolve(test_client, new, owner).json()["slug"] == old


@pytest.mark.e2e
def test_clearing_frees_every_name(test_client, owner_setup):
    c = owner_setup
    owner = _headers(c["token"], c["org_id"])
    old, new = _name(), _name()
    _name_dashboard(test_client, c, "sales", old)
    _name_dashboard(test_client, c, "sales", new)

    assert _name_dashboard(test_client, c, "sales", None)["slug"] is None
    assert _resolve(test_client, old, owner).status_code == 404
    assert _resolve(test_client, new, owner).status_code == 404
    _name_dashboard(test_client, c, "payroll", old)
    assert _name_dashboard(test_client, c, "sales", "  ")["slug"] is None  # blank clears too


@pytest.mark.e2e
def test_deleting_the_dashboard_frees_its_names(test_client, owner_setup):
    c = owner_setup
    owner = _headers(c["token"], c["org_id"])
    old, new = _name(), _name()
    _name_dashboard(test_client, c, "sales", old)
    _name_dashboard(test_client, c, "sales", new)
    assert test_client.delete(f"/api/artifacts/{c['sales']['id']}", headers=owner).status_code == 200

    assert _resolve(test_client, new, owner).status_code == 404
    assert _resolve(test_client, old, owner).status_code == 404
    _name_dashboard(test_client, c, "payroll", new)
    _name_dashboard(test_client, c, "payroll", old)
    assert _resolve(test_client, old, owner).json()["artifact_id"] == c["payroll"]["artifact_id"]


@pytest.mark.e2e
def test_archived_conversation_keeps_its_names(test_client, owner_setup, other_org, delete_report):
    # Deleting a conversation archives it; its shared dashboards stay served
    # on the long link, so their names keep working too.
    c, o = owner_setup, other_org
    slug = _name()
    _name_dashboard(test_client, c, "sales", slug)
    _share(test_client, report_id=c["report"]["id"], artifact_id=c["sales"]["artifact_id"],
           token=c["token"], org_id=c["org_id"], visibility="public")
    delete_report(c["report"]["id"], user_token=c["token"], org_id=c["org_id"])

    long_link = test_client.get(f"/api/r/{c['report']['id']}/artifacts/{c['sales']['id']}")
    assert long_link.status_code == 200
    assert _resolve(test_client, slug).status_code == 200
    resp = _set_slug(test_client, report_id=o["report"]["id"], artifact_id=o["dash"]["artifact_id"],
                     token=o["token"], org_id=o["org_id"], slug=slug)
    assert resp.status_code == 409, resp.json()


@pytest.mark.e2e
def test_only_the_owner_names_a_dashboard(test_client, owner_setup):
    c = owner_setup
    _share(test_client, report_id=c["report"]["id"], artifact_id=c["sales"]["artifact_id"],
           token=c["token"], org_id=c["org_id"], visibility="internal")
    resp = _set_slug(test_client, report_id=c["report"]["id"], artifact_id=c["sales"]["artifact_id"],
                     token=c["member"]["token"], org_id=c["org_id"], slug=_name())
    assert resp.status_code == 403, resp.json()

    # A dashboard of another report can't be named through this one.
    resp = _set_slug(test_client, report_id=c["report"]["id"], artifact_id=str(uuid.uuid4()),
                     token=c["token"], org_id=c["org_id"], slug=_name())
    assert resp.status_code == 404, resp.json()


@pytest.mark.e2e
def test_fork_does_not_carry_the_name(test_client, owner_setup, publish_report, fork_report):
    c = owner_setup
    slug = _name()
    _name_dashboard(test_client, c, "sales", slug)
    publish_report(c["report"]["id"], user_token=c["token"], org_id=c["org_id"])
    fork = fork_report(c["report"]["id"], user_token=c["token"], org_id=c["org_id"]).json()

    owner = _headers(c["token"], c["org_id"])
    forked = test_client.get(f"/api/artifacts/report/{fork['id']}", headers=owner).json()
    assert forked and all(a["slug"] is None for a in forked)
    assert _resolve(test_client, slug, owner).json()["report_id"] == c["report"]["id"]


@pytest.mark.e2e
def test_released_names_stay_with_their_organization(test_client, owner_setup, other_org):
    c, o = owner_setup, other_org
    owner = _headers(c["token"], c["org_id"])
    cleared, deleted = _name(), _name()
    _name_dashboard(test_client, c, "sales", cleared)
    _name_dashboard(test_client, c, "sales", None)
    _name_dashboard(test_client, c, "payroll", deleted)
    assert test_client.delete(f"/api/artifacts/{c['payroll']['id']}", headers=owner).status_code == 200

    for slug in (cleared, deleted):
        assert _resolve(test_client, slug, owner).status_code == 404
        resp = _set_slug(test_client, report_id=o["report"]["id"], artifact_id=o["dash"]["artifact_id"],
                         token=o["token"], org_id=o["org_id"], slug=slug)
        assert resp.status_code == 409, resp.json()
        assert resp.json()["error_code"] == "artifact.slug_taken"

    # The organization that held them may take them again.
    _name_dashboard(test_client, c, "sales", deleted)
    assert _resolve(test_client, deleted, owner).json()["artifact_id"] == c["sales"]["artifact_id"]
