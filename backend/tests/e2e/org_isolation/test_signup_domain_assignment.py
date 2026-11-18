"""Which organization an email domain joins is an instance-level decision.

Contract, on a deployment with several organizations enabled:
* Only an instance administrator can make an organization's sign-up policy
  admit a new domain; an organization admin gets a typed 403.
* A domain is admitted by at most one organization; a second claim is a typed 409.
* Reading the policy, removing domains, disabling it and changing its role
  stay with the organization's admins.
Single-organization deployments: the organization's admin edits the policy
exactly as before.
"""
import uuid

import pytest

from app.settings.config import settings as bow_settings
from tests.e2e.org_isolation.identity_helpers import (
    bootstrap_admin, feature, hdr, login, second_org_admin, whoami,
)

URL = "/api/organization/signup-policy"


def _domain():
    return f"{uuid.uuid4().hex[:10]}.example"


def _put(client, who, domains, enabled=True, role="member"):
    return client.put(
        URL, json={"enabled": enabled, "allowed_domains": domains, "auto_invite_role": role},
        headers=hdr(who["token"], who["org_id"]),
    )


def _get(client, who):
    resp = client.get(URL, headers=hdr(who["token"], who["org_id"]))
    assert resp.status_code == 200, resp.text
    return resp.json()


@pytest.fixture(autouse=True)
def domain_signup_licensed(monkeypatch):
    """Domain sign-up is a licensed feature the suite's license does not carry."""
    from app.ee import license as ee_license

    current = ee_license._cached_license
    monkeypatch.setattr(
        ee_license, "_cached_license",
        current.model_copy(update={"features": [*current.features, "domain_signup"]}),
    )


@pytest.fixture
def instance_admins(monkeypatch, test_client):
    """Set who the instance administrators are; it applies at their next sign-in."""

    def _set(*people):
        monkeypatch.setattr(bow_settings.bow_config.auth, "superuser_emails", [p["email"] for p in people])

    def _sign_in_again(person):
        person["token"] = login(test_client, person["email"]).json()["access_token"]
        return whoami(test_client, person["token"])["is_superuser"]

    return _set, _sign_in_again


@pytest.fixture
def two_organizations(test_client, instance_admins):
    """Organization A, whose admin is the instance administrator, and
    organization B with an ordinary admin."""
    set_admins, sign_in_again = instance_admins
    with feature("allow_multiple_organizations", True), feature("allow_uninvited_signups", True):
        root = bootstrap_admin(test_client)
        other = second_org_admin(test_client, root)
        set_admins(root)
        assert sign_in_again(root) is True
        assert sign_in_again(other) is False
        yield root, other


def test_organization_admin_cannot_assign_a_domain(test_client, two_organizations):
    _, other = two_organizations
    before = _get(test_client, other)

    refused = _put(test_client, other, [_domain()])
    assert refused.status_code == 403, refused.text
    assert refused.json()["error_code"] == "organization.signup_domains_instance_admin_required"
    assert _get(test_client, other) == before

    # Everything that admits nobody new is still theirs to change.
    assert _put(test_client, other, [], enabled=False, role="admin").status_code == 200
    assert _put(test_client, other, [_domain()], enabled=False).status_code == 200
    renamed = test_client.put(
        "/api/organization/settings", json={"config": {"general": {"ai_analyst_name": "Ada"}}},
        headers=hdr(other["token"], other["org_id"]),
    )
    assert renamed.status_code == 200, renamed.text


def test_enabling_a_policy_that_lists_domains_is_an_assignment_too(test_client, two_organizations):
    _, other = two_organizations
    domain = _domain()
    assert _put(test_client, other, [domain], enabled=False).status_code == 200

    refused = _put(test_client, other, [domain], enabled=True)

    assert refused.status_code == 403
    assert _get(test_client, other)["enabled"] is False


def test_instance_admin_assigns_and_the_organization_admin_can_only_narrow(
    test_client, two_organizations, instance_admins
):
    root, other = two_organizations
    set_admins, sign_in_again = instance_admins
    kept, dropped = _domain(), _domain()
    # The instance administrator has to act inside the organization: make B's
    # admin one for the assignment, then an ordinary admin again.
    set_admins(root, other)
    assert sign_in_again(other) is True
    saved = _put(test_client, other, [kept, dropped])
    assert saved.status_code == 200, saved.text
    assert saved.json()["allowed_domains"] == [kept, dropped]
    set_admins(root)
    assert sign_in_again(other) is False

    assert _put(test_client, other, [kept, dropped], role="admin").status_code == 200
    assert _put(test_client, other, [kept]).status_code == 200
    assert _put(test_client, other, [kept, _domain()]).status_code == 403
    assert _put(test_client, other, [kept, dropped]).status_code == 403
    assert _get(test_client, other)["allowed_domains"] == [kept]
    assert _put(test_client, other, [kept], enabled=False).status_code == 200


def test_a_domain_belongs_to_one_organization(test_client, two_organizations, instance_admins):
    root, other = two_organizations
    set_admins, sign_in_again = instance_admins
    set_admins(root, other)
    assert sign_in_again(other) is True
    shared, own = _domain(), _domain()
    assert _put(test_client, root, [shared]).status_code == 200

    refused = _put(test_client, other, [own, shared.upper()])
    assert refused.status_code == 409, refused.text
    assert refused.json()["error_code"] == "organization.signup_domain_claimed"
    assert refused.json()["params"] == {"domain": shared}
    assert _get(test_client, other)["allowed_domains"] == []

    assert _put(test_client, other, [own]).status_code == 200
    # Released by its organization, the domain can be assigned elsewhere.
    assert _put(test_client, root, [shared], enabled=False).status_code == 200
    assert _put(test_client, other, [own, shared]).status_code == 200
    assert _put(test_client, root, [shared], enabled=True).status_code == 409


def test_single_organization_admin_edits_the_policy_as_before(test_client):
    admin = bootstrap_admin(test_client)
    assert whoami(test_client, admin["token"])["is_superuser"] is False
    first, second = _domain(), _domain()

    saved = _put(test_client, admin, [first, f" {second.upper()} ", first], role="admin")

    assert saved.status_code == 200, saved.text
    assert saved.json() == {"enabled": True, "allowed_domains": [first, second], "auto_invite_role": "admin"}
    assert _put(test_client, admin, [first, _domain()]).status_code == 200
    assert _get(test_client, admin)["enabled"] is True
