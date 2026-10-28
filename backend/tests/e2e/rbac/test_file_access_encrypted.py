"""File access through an artifact whose content is encrypted at rest.

The embed check in file_access_service prefilters ``ArtifactVersion.content``
with a SQL substring match on the file id. Under enterprise ``data_encryption``
that column is ciphertext, so encrypted versions are routed to the Python
structural check instead; a shared dashboard must expose its embedded file
exactly as a plaintext one does (tests/e2e/rbac/test_file_access.py).
"""
import asyncio
import uuid

import pytest

from app.dependencies import async_session_maker
from app.ee.encryption import types as enc
from tests.fixtures.artifact import seed_artifact


def _h(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


@pytest.fixture
def encrypting(monkeypatch):
    monkeypatch.setattr(enc, "encryption_active", lambda: True)
    return enc


@pytest.mark.e2e
def test_shared_dashboard_with_encrypted_content_still_exposes_its_embedded_file(
    test_client, create_user, login_user, whoami, invite_user_to_org, set_visibility, encrypting
):
    admin = create_user()
    admin_token = login_user(admin["email"], admin["password"])
    org_id = whoami(admin_token)["organizations"][0]["id"]
    owner = invite_user_to_org(org_id=org_id, admin_token=admin_token)
    other = invite_user_to_org(org_id=org_id, admin_token=admin_token)["token"]

    report = test_client.post("/api/reports", json={"title": "dash"}, headers=_h(owner["token"], org_id))
    assert report.status_code == 200, report.text
    report_id = report.json()["id"]

    def _upload():
        resp = test_client.post(
            "/api/files",
            files={"file": (f"f_{uuid.uuid4().hex[:6]}.csv", b"k,v\nsecret,1\n", "text/csv")},
            data={"report_id": report_id},
            headers=_h(owner["token"], org_id),
        )
        assert resp.status_code == 200, resp.text
        return resp.json()["id"]

    embedded, chat_only = _upload(), _upload()

    async def _seed():
        async with async_session_maker() as db:
            await seed_artifact(
                db, report_id=report_id, user_id=owner["user_id"], organization_id=org_id,
                content={"code": "<BowFile />", "files": [{"id": embedded, "content_type": "text/csv"}]},
            )
            await db.commit()
    asyncio.run(_seed())

    def _can_read(file_id):
        return test_client.get(f"/api/files/{file_id}/content", headers=_h(other, org_id)).status_code == 200

    assert not _can_read(embedded)
    set_visibility(report_id, "artifact", "internal", user_token=owner["token"], org_id=org_id)
    assert _can_read(embedded)
    assert not _can_read(chat_only)
