"""Org setting `allow_public_sharing`: "anyone with the link" sharing.

Contract:
- On by default — nothing changes for existing orgs.
- When an admin turns it off:
  * no one can create a new public share (artifact or conversation, through
    the visibility endpoint or the older publish / conversation-share /
    status=published paths);
  * shares that are already public stop working for anyone outside the org:
    anonymous visitors and members of other orgs are refused, while members
    of the report's own org keep access (public acts as organization-only);
  * the stored visibility is left alone, so turning the setting back on
    restores the same links.
- Only users who can manage settings may change it.
"""
import uuid

import pytest


def _headers(token, org_id=None):
    h = {"Authorization": f"Bearer {token}"}
    if org_id:
        h["X-Organization-Id"] = str(org_id)
    return h


def _set_public_sharing(test_client, admin, enabled):
    resp = test_client.put(
        "/api/organization/settings",
        json={"config": {"allow_public_sharing": {"value": enabled}}},
        headers=_headers(admin["token"], admin["org_id"]),
    )
    assert resp.status_code == 200, resp.json()
    return resp.json()


def _put_visibility(test_client, report_id, share_type, token, org_id, **body):
    return test_client.put(
        f"/api/reports/{report_id}/visibility/{share_type}",
        json=body,
        headers=_headers(token, org_id),
    )


def _new_report(create_report, token, org_id):
    return create_report(
        title=f"Policy {uuid.uuid4().hex[:6]}", user_token=token, org_id=org_id, data_sources=[]
    )


def _assert_policy_refusal(resp):
    assert resp.status_code == 403, resp.text
    assert resp.json().get("error_code") == "sharing.public_disabled", resp.json()


@pytest.fixture
def cast(bootstrap_admin, invite_user_to_org):
    """Org admin, a regular member who owns reports, a second member, and an
    admin of a different org."""
    admin = bootstrap_admin()
    owner = invite_user_to_org(org_id=admin["org_id"], admin_token=admin["token"])
    colleague = invite_user_to_org(org_id=admin["org_id"], admin_token=admin["token"])
    outsider = bootstrap_admin("outsider")
    return {"admin": admin, "owner": owner, "colleague": colleague, "outsider": outsider}


# ---------------------------------------------------------------------------
# Setting itself
# ---------------------------------------------------------------------------

@pytest.mark.e2e
def test_public_sharing_is_allowed_by_default(test_client, create_report, cast):
    admin, owner = cast["admin"], cast["owner"]
    settings = test_client.get(
        "/api/organization/settings", headers=_headers(admin["token"], admin["org_id"])
    ).json()
    assert settings["config"]["allow_public_sharing"]["value"] is True

    report = _new_report(create_report, owner["token"], admin["org_id"])
    resp = _put_visibility(test_client, report["id"], "artifact", owner["token"], admin["org_id"], visibility="public")
    assert resp.status_code == 200, resp.text
    assert test_client.get(f"/api/r/{report['id']}").status_code == 200


@pytest.mark.e2e
def test_only_settings_managers_can_change_public_sharing(test_client, cast):
    admin, owner = cast["admin"], cast["owner"]
    resp = test_client.put(
        "/api/organization/settings",
        json={"config": {"allow_public_sharing": {"value": False}}},
        headers=_headers(owner["token"], admin["org_id"]),
    )
    assert resp.status_code == 403, resp.text

    settings = test_client.get(
        "/api/organization/settings", headers=_headers(admin["token"], admin["org_id"])
    ).json()
    assert settings["config"]["allow_public_sharing"]["value"] is True


# ---------------------------------------------------------------------------
# Existing public shares stop reaching people outside the org
# ---------------------------------------------------------------------------

@pytest.mark.e2e
def test_disabling_cuts_off_existing_public_dashboard_outside_the_org(test_client, create_report, cast):
    admin, owner, colleague, outsider = cast["admin"], cast["owner"], cast["colleague"], cast["outsider"]
    org_id = admin["org_id"]
    report = _new_report(create_report, owner["token"], org_id)
    assert _put_visibility(test_client, report["id"], "artifact", owner["token"], org_id, visibility="public").status_code == 200

    # Every /r/ surface the shared page loads goes through the same gate.
    surfaces = [
        f"/api/r/{report['id']}",
        f"/api/r/{report['id']}/artifacts",
        f"/api/r/{report['id']}/queries",
    ]
    for url in surfaces:
        assert test_client.get(url).status_code == 200, url
        assert test_client.get(url, headers=_headers(outsider["token"])).status_code == 200, url

    _set_public_sharing(test_client, admin, False)

    for url in surfaces:
        anon = test_client.get(url)
        assert anon.status_code == 401, (url, anon.text)
        foreign = test_client.get(url, headers=_headers(outsider["token"]))
        assert foreign.status_code == 403, (url, foreign.text)
        # Org members (not just the owner) keep access: public now means org-only.
        assert test_client.get(url, headers=_headers(colleague["token"])).status_code == 200, url
        assert test_client.get(url, headers=_headers(owner["token"])).status_code == 200, url

    # The stored setting is untouched — it is the policy, not the data, that changed.
    fetched = test_client.get(f"/api/reports/{report['id']}", headers=_headers(owner["token"], org_id)).json()
    assert fetched["artifact_visibility"] == "public"
    assert fetched["public_sharing_allowed"] is False


@pytest.mark.e2e
def test_disabling_cuts_off_existing_public_conversation_outside_the_org(test_client, create_report, cast):
    admin, owner, colleague, outsider = cast["admin"], cast["owner"], cast["colleague"], cast["outsider"]
    org_id = admin["org_id"]
    report = _new_report(create_report, owner["token"], org_id)
    resp = _put_visibility(test_client, report["id"], "conversation", owner["token"], org_id, visibility="public")
    assert resp.status_code == 200, resp.text
    token = resp.json()["conversation_share_token"]
    assert test_client.get(f"/api/c/{token}").status_code == 200

    _set_public_sharing(test_client, admin, False)

    assert test_client.get(f"/api/c/{token}").status_code == 401
    assert test_client.get(f"/api/c/{token}", headers=_headers(outsider["token"])).status_code == 403
    assert test_client.get(f"/api/c/{token}", headers=_headers(colleague["token"])).status_code == 200


@pytest.mark.e2e
def test_reenabling_restores_existing_public_links(test_client, create_report, cast):
    admin, owner = cast["admin"], cast["owner"]
    org_id = admin["org_id"]
    report = _new_report(create_report, owner["token"], org_id)
    assert _put_visibility(test_client, report["id"], "artifact", owner["token"], org_id, visibility="public").status_code == 200

    _set_public_sharing(test_client, admin, False)
    assert test_client.get(f"/api/r/{report['id']}").status_code == 401

    _set_public_sharing(test_client, admin, True)
    assert test_client.get(f"/api/r/{report['id']}").status_code == 200


@pytest.mark.e2e
def test_policy_does_not_affect_other_org_or_non_public_shares(test_client, create_report, cast):
    """Turning it off in one org leaves another org's public links alone, and
    leaves internal / specific-people shares in the same org unchanged."""
    admin, owner, colleague, outsider = cast["admin"], cast["owner"], cast["colleague"], cast["outsider"]
    org_id = admin["org_id"]

    foreign_report = _new_report(create_report, outsider["token"], outsider["org_id"])
    assert _put_visibility(test_client, foreign_report["id"], "artifact", outsider["token"], outsider["org_id"], visibility="public").status_code == 200

    internal = _new_report(create_report, owner["token"], org_id)
    assert _put_visibility(test_client, internal["id"], "artifact", owner["token"], org_id, visibility="internal").status_code == 200
    shared = _new_report(create_report, owner["token"], org_id)
    assert _put_visibility(
        test_client, shared["id"], "artifact", owner["token"], org_id,
        visibility="shared", shared_user_ids=[colleague["user_id"]],
    ).status_code == 200

    _set_public_sharing(test_client, admin, False)

    assert test_client.get(f"/api/r/{foreign_report['id']}").status_code == 200
    assert test_client.get(f"/api/r/{internal['id']}", headers=_headers(colleague["token"])).status_code == 200
    assert test_client.get(f"/api/r/{shared['id']}", headers=_headers(colleague["token"])).status_code == 200


# ---------------------------------------------------------------------------
# No new public shares while off
# ---------------------------------------------------------------------------

@pytest.mark.e2e
@pytest.mark.parametrize("share_type", ["artifact", "conversation"])
def test_cannot_make_a_report_public_while_disabled(test_client, create_report, cast, share_type):
    admin, owner = cast["admin"], cast["owner"]
    org_id = admin["org_id"]
    _set_public_sharing(test_client, admin, False)

    for actor in (owner, admin):  # the owner of the report, and an org admin on their own report
        report = _new_report(create_report, actor["token"], org_id)
        _assert_policy_refusal(
            _put_visibility(test_client, report["id"], share_type, actor["token"], org_id, visibility="public")
        )
        fetched = test_client.get(f"/api/reports/{report['id']}", headers=_headers(actor["token"], org_id)).json()
        assert fetched[f"{share_type}_visibility"] == "none"

        # Every other level still works.
        for level in ("internal", "none"):
            resp = _put_visibility(test_client, report["id"], share_type, actor["token"], org_id, visibility=level)
            assert resp.status_code == 200, resp.text


@pytest.mark.e2e
def test_older_publish_paths_are_blocked_while_disabled(test_client, create_report, cast):
    admin, owner = cast["admin"], cast["owner"]
    org_id = admin["org_id"]
    _set_public_sharing(test_client, admin, False)
    h = _headers(owner["token"], org_id)

    report = _new_report(create_report, owner["token"], org_id)
    _assert_policy_refusal(test_client.post(f"/api/reports/{report['id']}/publish", headers=h))
    _assert_policy_refusal(test_client.put(f"/api/reports/{report['id']}", json={"status": "published"}, headers=h))
    _assert_policy_refusal(test_client.post(f"/api/reports/{report['id']}/conversation-share", headers=h))

    fetched = test_client.get(f"/api/reports/{report['id']}", headers=h).json()
    assert fetched["artifact_visibility"] == "none"
    assert fetched["conversation_visibility"] == "none"
    assert test_client.get(f"/api/r/{report['id']}").status_code == 404


@pytest.mark.e2e
def test_existing_public_share_can_still_be_edited_or_turned_off(test_client, create_report, cast):
    """A report made public before the switch can still have its display
    settings changed (the modal re-sends the unchanged visibility) and can be
    turned off through every path."""
    admin, owner = cast["admin"], cast["owner"]
    org_id = admin["org_id"]
    h = _headers(owner["token"], org_id)

    artifact_report = _new_report(create_report, owner["token"], org_id)
    assert _put_visibility(test_client, artifact_report["id"], "artifact", owner["token"], org_id, visibility="public").status_code == 200
    conv = test_client.post(f"/api/reports/{artifact_report['id']}/conversation-share", headers=h)
    assert conv.status_code == 200, conv.text
    published = _new_report(create_report, owner["token"], org_id)
    assert test_client.post(f"/api/reports/{published['id']}/publish", headers=h).status_code == 200

    _set_public_sharing(test_client, admin, False)

    resp = _put_visibility(
        test_client, artifact_report["id"], "artifact", owner["token"], org_id,
        visibility="public", include_data_tab=False,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["include_data_tab"] is False

    # Turn everything off through each path.
    assert test_client.post(f"/api/reports/{artifact_report['id']}/conversation-share", headers=h).status_code == 200
    assert _put_visibility(test_client, artifact_report["id"], "artifact", owner["token"], org_id, visibility="none").status_code == 200
    assert test_client.post(f"/api/reports/{published['id']}/publish", headers=h).status_code == 200

    for rid in (artifact_report["id"], published["id"]):
        fetched = test_client.get(f"/api/reports/{rid}", headers=h).json()
        assert fetched["artifact_visibility"] == "none"
        assert fetched["conversation_visibility"] == "none"
