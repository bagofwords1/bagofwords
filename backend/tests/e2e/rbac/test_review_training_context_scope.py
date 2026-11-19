"""E2E (RBAC): the "Improve in training mode" examples are other people's
conversations, so /api/review/{id}/training-context shows only what the caller
could already open in Monitoring: a report whose every agent they manage."""
import asyncio
import uuid
from datetime import datetime

import pytest

from app.dependencies import async_session_maker
from app.models.review_item import TYPE_LOW_CONFIDENCE, SEVERITY_WARNING

pytestmark = pytest.mark.e2e


def _h(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


async def _emit(org_id, ds_id, type_):
    from app.services.review_service import review_service
    async with async_session_maker() as db:
        it = await review_service.emit(db, organization_id=org_id, type=type_, data_source_id=ds_id,
                                       severity=SEVERITY_WARNING, title=type_, group_key=f"{type_}:{ds_id}",
                                       subject={"kind": type_})
        return str(it.id)



async def _low_scored_question(report_id, text):
    from app.models.completion import Completion
    async with async_session_maker() as db:
        db.add(Completion(report_id=report_id, role="user", prompt={"content": text}, completion={},
                          response_score=1, sigkill=datetime.utcnow()))
        await db.commit()


def test_training_context_only_shows_conversations_the_manager_can_open(
    test_client, bootstrap_admin, invite_user_to_org, sqlite_data_source, grant_resource, create_report,
):
    """Same rule as a Monitoring trace: a manager of agent A sees a report's
    questions only when every agent behind it is one they manage."""
    admin = bootstrap_admin("trainctx")
    org_id = admin["org_id"]
    a = sqlite_data_source(name=f"a_{uuid.uuid4().hex[:6]}", user_token=admin["token"], org_id=org_id)
    b = sqlite_data_source(name=f"b_{uuid.uuid4().hex[:6]}", user_token=admin["token"], org_id=org_id)
    manager = invite_user_to_org(org_id=org_id, admin_token=admin["token"])
    g = grant_resource(resource_type="data_source", resource_id=a["id"], principal_type="user",
                       principal_id=manager["user_id"], permissions=["manage"],
                       user_token=admin["token"], org_id=org_id)
    assert g.status_code in (200, 201), g.json()
    only_a = create_report(title="A", user_token=admin["token"], org_id=org_id, data_sources=[a["id"]])
    joint = create_report(title="A+B", user_token=admin["token"], org_id=org_id, data_sources=[a["id"], b["id"]])
    asyncio.run(_low_scored_question(only_a["id"], "question on A only"))
    asyncio.run(_low_scored_question(joint["id"], "question on A and B"))
    item_id = asyncio.run(_emit(org_id, a["id"], TYPE_LOW_CONFIDENCE))

    def questions(token):
        r = test_client.get(f"/api/review/{item_id}/training-context", headers=_h(token, org_id))
        assert r.status_code == 200, r.text
        return sorted(e["question"] for e in r.json()["examples"])

    assert questions(admin["token"]) == ["question on A and B", "question on A only"]
    assert questions(manager["token"]) == ["question on A only"], "a report that also uses agent B stays hidden"

