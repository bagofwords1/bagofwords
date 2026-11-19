"""Two organizations for the boundary tests in this package.

``home`` is the organization the caller works in; ``other`` is an unrelated
one. The caller (``home`` admin) has no binding to ``other``. The ``other``
admin is also a plain member of ``home`` only because that is how a second
organization is created through the API (an existing user creates it).
"""
import uuid
from pathlib import Path

import pytest

from app.settings.config import settings as bow_settings

CHINOOK_PATH = (Path(__file__).resolve().parents[2] / "config" / "chinook.sqlite").resolve()


def h(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


@pytest.fixture
def allow_multiple_orgs():
    flags = bow_settings.bow_config.features
    saved = flags.allow_multiple_organizations
    flags.allow_multiple_organizations = True
    try:
        yield
    finally:
        flags.allow_multiple_organizations = saved


@pytest.fixture
def two_orgs(allow_multiple_orgs, create_user, login_user, whoami, test_client, create_organization):
    home_admin = create_user(email=f"home_{uuid.uuid4().hex[:6]}@test.com")
    home_token = login_user(home_admin["email"], home_admin["password"])
    home_me = whoami(home_token)
    home_org = home_me["organizations"][0]["id"]

    other_email = f"other_{uuid.uuid4().hex[:6]}@test.com"
    r = test_client.post(
        f"/api/organizations/{home_org}/members",
        json={"organization_id": home_org, "email": other_email, "role": "member"},
        headers=h(home_token, home_org),
    )
    assert r.status_code == 200, r.text
    other_admin = create_user(email=other_email)
    other_token = login_user(other_admin["email"], other_admin["password"])
    other_org = create_organization(name=f"Other {uuid.uuid4().hex[:6]}", user_token=other_token)
    return {
        "home_org": home_org, "home_token": home_token, "home_user_id": home_me["id"],
        "other_org": other_org, "other_token": other_token,
        "other_user_id": whoami(other_token)["id"],
    }


@pytest.fixture
def sqlite_agent(test_client, create_data_source):
    """A SQLite data source (agent) in the given organization."""
    if not CHINOOK_PATH.exists():
        pytest.skip(f"Missing SQLite fixture at {CHINOOK_PATH}")

    def _create(*, token, org_id, is_public=False):
        ds = create_data_source(
            name=f"agent_{uuid.uuid4().hex[:8]}", type="sqlite",
            config={"database": str(CHINOOK_PATH)}, credentials={},
            user_token=token, org_id=org_id,
        )
        r = test_client.put(f"/api/data_sources/{ds['id']}", json={"is_public": is_public},
                            headers=h(token, org_id))
        assert r.status_code == 200, r.text
        assert r.json()["is_public"] is is_public
        return ds

    return _create
