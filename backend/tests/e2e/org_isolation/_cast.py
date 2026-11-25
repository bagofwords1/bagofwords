"""Two organizations on one instance, built through the real API.

Not a conftest on purpose: test modules import the fixtures they need, so
this file never collides with fixtures other modules in the directory define.
"""
import uuid
from pathlib import Path

import pytest

from app.settings.config import settings as bow_settings

CHINOOK_PATH = (Path(__file__).resolve().parents[2] / "config" / "chinook.sqlite").resolve()


def h(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


@pytest.fixture
def two_orgs(monkeypatch, test_client, create_user, login_user, whoami):
    """Org A (admin + plain member) and an unrelated org B (its own admin).

    ``b_token`` is the "other organization": nothing of B may be reachable
    with A's tokens, and B's people may not be named in A's requests.
    """
    flags = bow_settings.bow_config.features
    monkeypatch.setattr(flags, "allow_uninvited_signups", True)
    monkeypatch.setattr(flags, "allow_multiple_organizations", True)

    def _signup(prefix):
        email = f"{prefix}_{uuid.uuid4().hex[:10]}@test.com"
        create_user(email=email, password="Test1234!")
        token = login_user(email, "Test1234!")
        return email, token

    _, a_token = _signup("a_admin")
    a_me = whoami(a_token)
    a_org = a_me["organizations"][0]["id"]

    member_email = f"a_member_{uuid.uuid4().hex[:10]}@test.com"
    invite = test_client.post(
        f"/api/organizations/{a_org}/members",
        json={"organization_id": a_org, "email": member_email, "role": "member"},
        headers=h(a_token, a_org),
    )
    assert invite.status_code == 200, invite.text
    create_user(email=member_email, password="Test1234!")
    a_member_token = login_user(member_email, "Test1234!")

    _, b_token = _signup("b_admin")
    b_me = whoami(b_token)
    if b_me.get("organizations"):
        b_org = b_me["organizations"][0]["id"]
    else:
        created = test_client.post(
            "/api/organizations", json={"name": f"org_b_{uuid.uuid4().hex[:8]}"},
            headers={"Authorization": f"Bearer {b_token}"},
        )
        assert created.status_code == 200, created.text
        b_org = created.json()["id"]
    assert b_org != a_org

    return {
        "a_org": a_org, "a_token": a_token, "a_id": a_me["id"],
        "a_member_token": a_member_token, "a_member_id": whoami(a_member_token)["id"],
        "b_org": b_org, "b_token": b_token, "b_id": b_me["id"],
    }


@pytest.fixture
def chinook_agent(create_data_source, refresh_schema):
    """A SQLite data source over the bundled chinook file, with its tables
    indexed, in the given org."""
    if not CHINOOK_PATH.exists():
        pytest.skip(f"Missing SQLite fixture at {CHINOOK_PATH}")

    def _create(*, user_token, org_id, name=None):
        ds = create_data_source(
            name=name or f"chinook_{uuid.uuid4().hex[:6]}", type="sqlite",
            config={"database": str(CHINOOK_PATH)}, credentials={},
            user_token=user_token, org_id=org_id,
        )
        refresh_schema(data_source_id=ds["id"], user_token=user_token, org_id=org_id)
        return ds

    return _create
