"""Rerun contract: ``prompt.file_ids`` attaches report files to a new turn.

The report chat's Rerun action re-sends an earlier prompt with the same
attachments by passing their ids in ``prompt.file_ids``. The new user message
must list those files as its attachments, for direct runs and queued prompts
alike, while the earlier message keeps its own. Ids that are not files of the
same report are ignored.

The agent loop is stubbed at AgentV2.main_execution (the LLM boundary);
routes, services and the DB run real.
"""
import base64
import uuid

import pytest

from app.ai.agent_v2 import AgentV2

# 1x1 transparent PNG
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)


def _headers(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


@pytest.fixture
def org(monkeypatch, create_user, login_user, whoami, create_llm_provider_and_models):
    monkeypatch.setenv("OPENAI_API_KEY_TEST", "sk-test-dummy")
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    create_llm_provider_and_models(token, org_id)
    return token, org_id


@pytest.fixture
def agent_succeeds(monkeypatch):
    async def fake_main_execution(self):
        self.system_completion.status = "success"
        self.db.add(self.system_completion)
        await self.db.commit()
    monkeypatch.setattr(AgentV2, "main_execution", fake_main_execution)


@pytest.fixture
def agent_hangs(monkeypatch):
    async def fake_main_execution(self):
        return None
    monkeypatch.setattr(AgentV2, "main_execution", fake_main_execution)


def _completions(test_client, report_id, token, org_id):
    r = test_client.get(f"/api/reports/{report_id}/completions?limit=50", headers=_headers(token, org_id))
    assert r.status_code == 200, r.json()
    return r.json()["completions"]


def _file_ids_of(completions, completion_id):
    c = next(c for c in completions if c["id"] == completion_id)
    return {str(f["id"]) for f in (c.get("files") or [])}


def _send(test_client, report_id, token, org_id, content, **prompt_extra):
    r = test_client.post(
        f"/api/reports/{report_id}/completions?background=true",
        json={"prompt": {"content": content, "mentions": [{}], **prompt_extra}},
        headers=_headers(token, org_id),
    )
    assert r.status_code == 200, r.json()
    return next(c for c in r.json()["completions"] if c["role"] == "user")


def _report_with_attached_image(test_client, create_report, upload_file, token, org_id, n_images=1):
    report = create_report(title=f"rerun-{uuid.uuid4().hex[:8]}", user_token=token, org_id=org_id, data_sources=[])
    images = [
        upload_file(_PNG, f"chart-{i}.png", "image/png", user_token=token, org_id=org_id, report_id=report["id"])
        for i in range(n_images)
    ]
    first = _send(test_client, report["id"], token, org_id, "what does this chart show?")
    return report, [str(i["id"]) for i in images], first


@pytest.mark.e2e
@pytest.mark.parametrize("n_images", [1, 2])
def test_rerun_attaches_the_same_files_to_the_new_turn(
    test_client, create_report, upload_file, get_files_by_report, org, agent_succeeds, n_images,
):
    token, org_id = org
    report, image_ids, first = _report_with_attached_image(
        test_client, create_report, upload_file, token, org_id, n_images=n_images
    )
    comps = _completions(test_client, report["id"], token, org_id)
    assert _file_ids_of(comps, first["id"]) == set(image_ids)

    rerun = _send(test_client, report["id"], token, org_id, "what does this chart show?", file_ids=image_ids)

    comps = _completions(test_client, report["id"], token, org_id)
    assert _file_ids_of(comps, rerun["id"]) == set(image_ids)
    # The earlier turn keeps its attachments.
    assert _file_ids_of(comps, first["id"]) == set(image_ids)
    # The report's file library is unchanged: no duplicate entries.
    listed = [str(f["id"]) for f in get_files_by_report(report["id"], user_token=token, org_id=org_id)]
    assert sorted(listed) == sorted(set(listed))
    assert set(image_ids) <= set(listed)


@pytest.mark.e2e
def test_rerun_without_file_ids_attaches_nothing(
    test_client, create_report, upload_file, org, agent_succeeds,
):
    token, org_id = org
    report, _image_ids, _first = _report_with_attached_image(test_client, create_report, upload_file, token, org_id)

    again = _send(test_client, report["id"], token, org_id, "and now?")

    comps = _completions(test_client, report["id"], token, org_id)
    assert _file_ids_of(comps, again["id"]) == set()


@pytest.mark.e2e
def test_file_ids_from_another_report_are_ignored(
    test_client, create_report, upload_file, org, agent_succeeds,
):
    token, org_id = org
    _other, foreign_ids, _ = _report_with_attached_image(test_client, create_report, upload_file, token, org_id)
    report = create_report(title=f"rerun-{uuid.uuid4().hex[:8]}", user_token=token, org_id=org_id, data_sources=[])

    sent = _send(test_client, report["id"], token, org_id, "use that chart", file_ids=foreign_ids + [str(uuid.uuid4())])

    comps = _completions(test_client, report["id"], token, org_id)
    assert _file_ids_of(comps, sent["id"]) == set()


@pytest.mark.e2e
def test_queued_rerun_carries_the_files(
    test_client, create_report, upload_file, org, agent_hangs,
):
    token, org_id = org
    report, image_ids, _first = _report_with_attached_image(test_client, create_report, upload_file, token, org_id)

    r = test_client.post(
        f"/api/reports/{report['id']}/completions",
        json={"prompt": {"content": "what does this chart show?", "mentions": [{}], "file_ids": image_ids}, "queue": True},
        headers=_headers(token, org_id),
    )
    assert r.status_code == 200, r.json()
    queued = r.json()["completions"][0]
    assert queued["status"] == "queued"

    comps = _completions(test_client, report["id"], token, org_id)
    assert _file_ids_of(comps, queued["id"]) == set(image_ids)


@pytest.mark.e2e
@pytest.mark.parametrize("original_has_image", [True, False])
def test_rerun_leaves_an_image_waiting_in_the_composer_for_the_next_message(
    test_client, create_report, upload_file, org, agent_succeeds, original_has_image,
):
    """An image uploaded but not yet sent belongs to the user's NEXT message.
    A Rerun names its own attachments (none included), so it must not take
    that image along."""
    token, org_id = org
    report, image_ids, _first = _report_with_attached_image(
        test_client, create_report, upload_file, token, org_id, n_images=1 if original_has_image else 0
    )
    staged = upload_file(_PNG, "staged.png", "image/png", user_token=token, org_id=org_id, report_id=report["id"])

    rerun = _send(test_client, report["id"], token, org_id, "what does this chart show?", file_ids=image_ids)
    comps = _completions(test_client, report["id"], token, org_id)
    assert _file_ids_of(comps, rerun["id"]) == set(image_ids)

    following = _send(test_client, report["id"], token, org_id, "and this one?")
    comps = _completions(test_client, report["id"], token, org_id)
    assert _file_ids_of(comps, following["id"]) == {str(staged["id"])}
    assert _file_ids_of(comps, rerun["id"]) == set(image_ids)
