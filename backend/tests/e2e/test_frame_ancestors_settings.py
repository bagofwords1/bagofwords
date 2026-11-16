"""E2E: sites an admin allows to show BOW in an iframe (Settings → General).

Contract:
  * an admin saves plain https origins; they reach the frame-ancestors header
    of app pages (sign-in included), never the public /r/ and /c/ pages;
  * an entry that is not a plain origin is refused with a typed 400 naming it,
    and nothing is saved;
  * only manage_settings may read or change it; on a multi-organization
    instance only an instance superuser may change it;
  * every change is audited with what was added and removed.
"""
import re
import uuid

import pytest

from app.services import frame_ancestors_service
from tests.utils.user_creds import main_user


@pytest.fixture(autouse=True)
def _fresh_cache():
    # The list is cached per process; each test starts from its own database.
    frame_ancestors_service.reset_cache()
    yield
    frame_ancestors_service.reset_cache()


def _h(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _register(test_client, email, **extra):
    body = {"name": f"user_{uuid.uuid4().hex[:6]}", "email": email, "password": main_user["password"], **extra}
    r = test_client.post("/api/auth/register", json=body)
    assert r.status_code == 201, r.text


def _admin(test_client, login_user, whoami):
    email = f"owner_{uuid.uuid4().hex[:6]}@example-corp.com"
    _register(test_client, email)
    token = login_user(email, main_user["password"])
    return token, whoami(token)["organizations"][0]["id"]


def _ancestors(response) -> list:
    m = re.search(r"frame-ancestors ([^;]+)", response.headers.get("content-security-policy", ""))
    return m.group(1).split() if m else []


@pytest.mark.e2e
def test_saved_sites_reach_the_frame_policy_of_app_pages(test_client, login_user, whoami):
    token, org_id = _admin(test_client, login_user, whoami)
    r = test_client.put("/api/settings/frame-ancestors",
                        json={"origins": ["https://ERP.example-corp.com/", "https://*.intranet.example-corp.com"]},
                        headers=_h(token, org_id))
    assert r.status_code == 200, r.text
    assert r.json()["origins"] == ["https://erp.example-corp.com", "https://*.intranet.example-corp.com"]
    assert r.json()["can_edit"] is True

    sources = _ancestors(test_client.get("/api/settings"))
    assert "https://erp.example-corp.com" in sources and "https://*.intranet.example-corp.com" in sources
    assert "'self'" in sources and "https://*.office.com" in sources
    assert "frame-ancestors" not in test_client.get("/r/abc").headers["content-security-policy"]

    assert test_client.get("/api/settings/frame-ancestors", headers=_h(token, org_id)).json()["origins"] == \
        ["https://erp.example-corp.com", "https://*.intranet.example-corp.com"]


@pytest.mark.e2e
def test_clearing_the_list_removes_the_sites(test_client, login_user, whoami):
    token, org_id = _admin(test_client, login_user, whoami)
    test_client.put("/api/settings/frame-ancestors", json={"origins": ["https://erp.example-corp.com"]},
                    headers=_h(token, org_id))
    r = test_client.put("/api/settings/frame-ancestors", json={"origins": []}, headers=_h(token, org_id))
    assert r.status_code == 200 and r.json()["origins"] == []
    assert "https://erp.example-corp.com" not in _ancestors(test_client.get("/api/settings"))


@pytest.mark.e2e
@pytest.mark.parametrize("bad", [
    "https://erp.example-corp.com; script-src *", "*", "https://*.github.io",
    "http://erp.example-corp.com", "https://erp.example-corp.com/path", "'unsafe-inline'",
])
def test_an_invalid_entry_is_refused_by_name_and_nothing_is_saved(test_client, login_user, whoami, bad):
    token, org_id = _admin(test_client, login_user, whoami)
    test_client.put("/api/settings/frame-ancestors", json={"origins": ["https://keep.example-corp.com"]},
                    headers=_h(token, org_id))
    r = test_client.put("/api/settings/frame-ancestors",
                        json={"origins": ["https://ok.example-corp.com", bad]}, headers=_h(token, org_id))
    assert r.status_code == 400, r.text
    body = r.json()
    assert body["error_code"] == "frame_ancestors.invalid"
    assert bad in body["params"]["origins"]
    saved = test_client.get("/api/settings/frame-ancestors", headers=_h(token, org_id)).json()["origins"]
    assert saved == ["https://keep.example-corp.com"]
    assert "https://ok.example-corp.com" not in _ancestors(test_client.get("/api/settings"))


@pytest.mark.e2e
def test_a_member_cannot_read_or_change_the_list(test_client, login_user, whoami):
    token, org_id = _admin(test_client, login_user, whoami)
    member = f"member_{uuid.uuid4().hex[:6]}@example-corp.com"
    r = test_client.post(f"/api/organizations/{org_id}/members",
                         json={"organization_id": org_id, "email": member, "role": "member"},
                         headers=_h(token, org_id))
    assert r.status_code == 200, r.text
    from tests.fixtures.user import _pending_invite_token
    _register(test_client, member, invite_token=_pending_invite_token(member))
    member_token = login_user(member, main_user["password"])

    assert test_client.get("/api/settings/frame-ancestors", headers=_h(member_token, org_id)).status_code == 403
    r = test_client.put("/api/settings/frame-ancestors", json={"origins": ["https://evil.example.net"]},
                        headers=_h(member_token, org_id))
    assert r.status_code == 403
    assert "https://evil.example.net" not in _ancestors(test_client.get("/api/settings"))


@pytest.mark.e2e
def test_on_a_multi_org_instance_only_a_superuser_can_change_it(test_client, login_user, whoami, monkeypatch):
    from app.settings.config import settings

    token, org_id = _admin(test_client, login_user, whoami)
    monkeypatch.setattr(settings.bow_config.features, "allow_multiple_organizations", True)

    status = test_client.get("/api/settings/frame-ancestors", headers=_h(token, org_id))
    assert status.status_code == 200 and status.json()["can_edit"] is False
    r = test_client.put("/api/settings/frame-ancestors", json={"origins": ["https://other-tenant.example.net"]},
                        headers=_h(token, org_id))
    assert r.status_code == 403
    assert "https://other-tenant.example.net" not in _ancestors(test_client.get("/api/settings"))


@pytest.mark.e2e
def test_a_change_is_audited_with_what_was_added_and_removed(test_client, login_user, whoami):
    token, org_id = _admin(test_client, login_user, whoami)
    test_client.put("/api/settings/frame-ancestors", json={"origins": ["https://a.example-corp.com"]},
                    headers=_h(token, org_id))
    test_client.put("/api/settings/frame-ancestors", json={"origins": ["https://b.example-corp.com"]},
                    headers=_h(token, org_id))

    events = _audit_rows(org_id, "settings.frame_ancestors_updated")
    assert {"added": ["https://a.example-corp.com"], "removed": []} in events
    assert {"added": ["https://b.example-corp.com"], "removed": ["https://a.example-corp.com"]} in events


def _audit_rows(org_id: str, action: str) -> list:
    import asyncio

    from sqlalchemy import select

    from app.dependencies import async_session_maker
    from app.ee.audit.models import AuditLog

    async def fetch():
        async with async_session_maker() as db:
            rows = (await db.execute(select(AuditLog).where(
                AuditLog.organization_id == org_id, AuditLog.action == action,
            ))).scalars().all()
            return [r.details or {} for r in rows]
    return asyncio.run(fetch())
