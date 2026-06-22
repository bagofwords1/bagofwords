"""HTTP-level API tests for the prompt catalog (through the real ASGI app).

Auth is injected via FastAPI dependency overrides + direct seeding, because the
HTTP signup route is not available under the sandbox config (pre-existing; see
docs/design/sandbox-feedback-loop.md). This still exercises the real routers,
schemas, and service wiring over HTTP.
"""
import uuid
import pytest
from fastapi.testclient import TestClient

from main import app
from app.core.auth import current_user as current_user_dep
from app.dependencies import get_current_organization
from app.dependencies import async_session_maker
from app.models.organization import Organization
from app.models.user import User
from app.models.membership import Membership


async def _seed_user_org():
    suffix = uuid.uuid4().hex[:8]
    async with async_session_maker() as db:
        org = Organization(name=f"API Org {suffix}")
        db.add(org)
        await db.flush()
        user = User(name=f"Api {suffix}", email=f"api-{suffix}@example.com",
                    hashed_password="x", is_active=True, is_verified=True)
        db.add(user)
        await db.flush()
        db.add(Membership(user_id=user.id, organization_id=org.id, role="admin"))
        await db.commit()
        return user.id, org.id


def _override(user, org):
    app.dependency_overrides[current_user_dep] = lambda: user
    app.dependency_overrides[get_current_organization] = lambda: org


def _clear_overrides():
    app.dependency_overrides.pop(current_user_dep, None)
    app.dependency_overrides.pop(get_current_organization, None)


@pytest.mark.asyncio
async def test_catalog_crud_and_subscribe_api():
    uid, oid = await _seed_user_org()
    async with async_session_maker() as db:
        user = await db.get(User, uid)
        org = await db.get(Organization, oid)
    _override(user, org)
    client = TestClient(app)
    try:
        create = client.post("/api/prompts", json={
            "title": "My Daily Digest", "text": "Summarize my day and my customers",
            "scope": "private", "status": "published", "mode": "chat",
            "default_cron": "0 9 * * 1-5", "default_channel": "smtp", "data_source_ids": [],
        })
        assert create.status_code == 200, create.text
        prompt = create.json()
        pid = prompt["id"]
        assert prompt["default_channel"] == "smtp"

        listing = client.get("/api/prompts?sort=top")
        assert listing.status_code == 200, listing.text
        assert pid in {p["id"] for p in listing.json()["prompts"]}

        got = client.get(f"/api/prompts/{pid}")
        assert got.status_code == 200
        assert got.json()["text"] == "Summarize my day and my customers"

        upd = client.put(f"/api/prompts/{pid}", json={"category": "Personal"})
        assert upd.status_code == 200 and upd.json()["category"] == "Personal"

        sub = client.post(f"/api/prompts/{pid}/subscribe", json={
            "cron_schedule": "0 9 * * 1-5", "channel": "smtp", "run_mode": "append",
        })
        assert sub.status_code == 200, sub.text
        assert sub.json()["report_id"]

        got2 = client.get(f"/api/prompts/{pid}")
        assert got2.json()["subscriber_count"] == 1
    finally:
        _clear_overrides()


@pytest.mark.asyncio
async def test_private_prompt_not_visible_to_other_user():
    uid1, oid1 = await _seed_user_org()
    uid2, oid2 = await _seed_user_org()
    async with async_session_maker() as db:
        u1 = await db.get(User, uid1); o1 = await db.get(Organization, oid1)
        u2 = await db.get(User, uid2); o2 = await db.get(Organization, oid2)

    _override(u1, o1)
    client = TestClient(app)
    try:
        r = client.post("/api/prompts", json={
            "title": "Secret", "text": "private stuff", "scope": "private",
            "status": "published", "data_source_ids": [],
        })
        assert r.status_code == 200, r.text
        pid = r.json()["id"]
    finally:
        _clear_overrides()

    # different user + org cannot fetch it
    _override(u2, o2)
    try:
        got = client.get(f"/api/prompts/{pid}")
        assert got.status_code in (403, 404), got.text
    finally:
        _clear_overrides()
