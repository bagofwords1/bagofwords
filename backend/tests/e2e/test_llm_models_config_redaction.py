"""GET /api/llm/models is readable by every member (model picker, chat), but
the nested provider's admin configuration — ``additional_config`` (custom
header values, endpoints) and ``credentials`` — is returned only to callers
with ``manage_llm``. Admin views of the same endpoint are unchanged.
"""
import asyncio
import uuid

import pytest

from app.dependencies import async_session_maker
from app.models.llm_model import LLMModel
from app.models.llm_provider import LLMProvider

SECRET_HEADER = "Bearer sk-header-secret-do-not-leak"


def _h(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


@pytest.fixture
def org(create_user, login_user, whoami, test_client):
    admin = create_user(email=f"owner_{uuid.uuid4().hex[:6]}@test.com")
    admin_token = login_user(admin["email"], admin["password"])
    org_id = whoami(admin_token)["organizations"][0]["id"]
    member_email = f"member_{uuid.uuid4().hex[:6]}@test.com"
    r = test_client.post(f"/api/organizations/{org_id}/members",
                         json={"organization_id": org_id, "email": member_email, "role": "member"},
                         headers=_h(admin_token, org_id))
    assert r.status_code == 200, r.text
    member = create_user(email=member_email)
    member_token = login_user(member["email"], member["password"])
    return {"org_id": org_id, "admin_token": admin_token, "member_token": member_token}


async def _seed(org_id):
    async with async_session_maker() as db:
        provider = LLMProvider(
            organization_id=org_id, name="custom-gw", provider_type="custom",
            is_preset=False, is_enabled=True, use_preset_credentials=False,
            additional_config={"base_url": "https://gw.internal/v1",
                               "headers": {"Authorization": SECRET_HEADER}},
        )
        db.add(provider)
        await db.flush()
        model = LLMModel(organization_id=org_id, provider_id=provider.id, name="gw-model",
                         model_id=f"gw-{uuid.uuid4().hex[:6]}", is_preset=False,
                         is_enabled=True, is_default=True, is_small_default=True)
        db.add(model)
        await db.commit()
        return str(model.id)


@pytest.mark.e2e
def test_member_gets_model_list_without_provider_config(test_client, org):
    model_id = asyncio.run(_seed(org["org_id"]))
    r = test_client.get("/api/llm/models", headers=_h(org["member_token"], org["org_id"]))
    assert r.status_code == 200
    models = {m["id"]: m for m in r.json()}
    # The picker still has what it needs.
    assert model_id in models
    m = models[model_id]
    assert m["name"] == "gw-model" and m["provider"]["name"] == "custom-gw"
    assert m["provider"]["provider_type"] == "custom"
    # ...but none of the provider's admin configuration.
    for m in r.json():
        assert not m["provider"].get("additional_config")
        assert not m["provider"].get("credentials")
    assert SECRET_HEADER not in r.text
    assert "gw.internal" not in r.text


@pytest.mark.e2e
def test_llm_admin_still_sees_provider_config(test_client, org):
    model_id = asyncio.run(_seed(org["org_id"]))
    r = test_client.get("/api/llm/models", headers=_h(org["admin_token"], org["org_id"]))
    assert r.status_code == 200
    m = next(m for m in r.json() if m["id"] == model_id)
    assert m["provider"]["additional_config"]["headers"]["Authorization"] == SECRET_HEADER
