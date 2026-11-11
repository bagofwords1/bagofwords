"""Code visibility of a queued run that an MCP turn dispatches.

Contract: a prompt queued in BOW runs, when the queue dispatches it, with the
code visibility of the prompt's author. The live stream serializes tool code
by the ambient decision (``code_visible_now``): an in-app run inherits it
from the route that started it. A run the dispatcher starts after an MCP turn
has no such route, so the dispatcher sets it from the author's permissions:
code for an author who may view code, none for an author whose role lacks
``view_code``.

The queued prompt is set up in-app by the member: a run is in progress
(hanging stub), a prompt sent with queue_if_busy waits behind it, and the
member stops the run, which pauses the queue. The member's MCP turn on the
report then dispatches it. Stubbed: AgentV2.main_execution only; the stub
records the decision the stream serializer would read. Real: /api/mcp, the
MCP tools, the completion routes and services, the dispatcher, RBAC.
"""
import asyncio
import time
import uuid

import pytest

from app.ai.agent_v2 import AgentV2
from app.core.code_visibility import code_visible_now


def _hdr(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


@pytest.fixture
def live_client():
    """Keep background tasks on one event loop for queue dispatch tests."""
    from fastapi.testclient import TestClient

    from main import app

    with TestClient(app) as client:
        yield client


def _strip_code_role(test_client, admin, member, create_role, assign_role):
    """Leave the member holding only a role without view_code (RBAC unions
    roles, so the default member assignment has to go)."""
    org_id = admin["org_id"]
    role = create_role(
        name=f"no-code-{uuid.uuid4().hex[:6]}",
        permissions=["view_reports", "create_reports", "manage_files", "view_members"],
        user_token=admin["token"], org_id=org_id,
    )
    assert role.status_code == 200, role.text
    existing = test_client.get(
        f"/api/organizations/{org_id}/role-assignments", headers=_hdr(admin["token"], org_id),
        params={"principal_type": "user", "principal_id": member["user_id"]},
    )
    assert existing.status_code == 200, existing.text
    for assignment in existing.json():
        test_client.delete(
            f"/api/organizations/{org_id}/role-assignments/{assignment['id']}",
            headers=_hdr(admin["token"], org_id),
        )
    assigned = assign_role(
        role_id=role.json()["id"], principal_type="user", principal_id=member["user_id"],
        user_token=admin["token"], org_id=org_id,
    )
    assert assigned.status_code in (200, 201), assigned.text


def _org_permissions(whoami, token, org_id):
    org = next(o for o in whoami(token)["organizations"] if o["id"] == org_id)
    return set(org["permissions"])


def _stub_hang(monkeypatch):
    async def fake_main_execution(self):
        return None
    monkeypatch.setattr(AgentV2, "main_execution", fake_main_execution)


def _stub_success_recording_code_visibility(monkeypatch):
    """The run ends successfully; each run records (head id, the code
    visibility the stream serializer would read)."""
    seen = []

    async def fake_main_execution(self):
        seen.append((str(self.head_completion.id), code_visible_now()))
        self.system_completion.status = "success"
        self.db.add(self.system_completion)
        await self.db.commit()
    monkeypatch.setattr(AgentV2, "main_execution", fake_main_execution)
    return seen


def _get_completions(client, report_id, token, org_id):
    r = client.get(f"/api/reports/{report_id}/completions?limit=50", headers=_hdr(token, org_id))
    assert r.status_code == 200, r.text
    return r.json()["completions"]


def _wait_until(predicate, timeout=15.0, interval=0.3):
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(interval)
    return None


def _settle_queue_work(portal, timeout=30.0):
    """Wait until no dispatcher or dispatched-run task is left on the live loop."""
    names = ("CompletionService.start_next_queued_if_idle", "CompletionService._run_dispatched_agent")

    async def _wait():
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            pending = [t for t in asyncio.all_tasks()
                       if t is not asyncio.current_task() and not t.done()
                       and t.get_coro().__qualname__.endswith(names)]
            if not pending:
                return
            await asyncio.wait(pending, timeout=deadline - asyncio.get_running_loop().time())
        raise AssertionError("queue work did not settle")

    portal.call(_wait)


def _queue_prompt_behind_a_stopped_run(monkeypatch, client, create_report, token, org_id):
    _stub_hang(monkeypatch)
    report = create_report(title=f"mcp-codevis-{uuid.uuid4().hex[:8]}", user_token=token,
                           org_id=org_id, data_sources=[])
    first = client.post(
        f"/api/reports/{report['id']}/completions?background=true",
        json={"prompt": {"content": "a run the member will stop", "mentions": [{}]}},
        headers=_hdr(token, org_id),
    )
    assert first.status_code == 200, first.text
    running = next(c for c in first.json()["completions"] if c["role"] == "system")
    queued = client.post(
        f"/api/reports/{report['id']}/completions",
        json={"queue_if_busy": True, "prompt": {"content": "waiting in line", "mentions": [{}]}},
        headers=_hdr(token, org_id),
    )
    assert queued.status_code == 200, queued.text
    head = queued.json()["completions"][0]
    assert head["role"] == "user" and head["status"] == "queued", queued.json()
    stop = client.post(f"/api/completions/{running['id']}/sigkill", headers=_hdr(token, org_id))
    assert stop.status_code == 200, stop.text
    return report["id"], head["id"]


@pytest.mark.e2e
@pytest.mark.parametrize("may_view_code", [True, False], ids=["code_viewer", "no_view_code"])
def test_queued_run_after_an_mcp_turn_sees_code_as_its_author_may(
    may_view_code, monkeypatch, test_client, live_client, bootstrap_admin, invite_user_to_org,
    create_role, assign_role, enterprise_license, whoami, create_report,
    create_llm_provider_and_models, enable_mcp, create_api_key,
):
    monkeypatch.setenv("OPENAI_API_KEY_TEST", "sk-test-dummy")
    admin = bootstrap_admin()
    org_id = admin["org_id"]
    create_llm_provider_and_models(admin["token"], org_id)
    enable_mcp(user_token=admin["token"], org_id=org_id)
    member = invite_user_to_org(org_id=org_id, admin_token=admin["token"])
    if not may_view_code:
        _strip_code_role(test_client, admin, member, create_role, assign_role)
    assert ("view_code" in _org_permissions(whoami, member["token"], org_id)) is may_view_code
    api_key = create_api_key(user_token=member["token"], org_id=org_id)["key"]

    report_id, queued_id = _queue_prompt_behind_a_stopped_run(
        monkeypatch, live_client, create_report, member["token"], org_id)
    _settle_queue_work(live_client.portal)
    assert any(c["id"] == queued_id and c["status"] == "queued"
               for c in _get_completions(live_client, report_id, member["token"], org_id)), \
        "precondition: the stopped run leaves the prompt queued"

    seen = _stub_success_recording_code_visibility(monkeypatch)
    r = live_client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": "get_context", "arguments": {"report_id": report_id}}},
        headers={"X-API-Key": api_key},
    )
    assert r.status_code == 200, r.text
    assert r.json()["result"]["isError"] is False, r.json()

    def _dispatched():
        comps = _get_completions(live_client, report_id, member["token"], org_id)
        runs = [c for c in comps if c["role"] == "system" and c["parent_id"] == queued_id]
        return runs if runs and runs[0]["status"] == "success" else None

    assert _wait_until(_dispatched), "the queued prompt was not dispatched after the MCP turn"
    _settle_queue_work(live_client.portal)

    assert seen == [(queued_id, may_view_code)], (
        f"the queued run's code visibility does not follow its author "
        f"(may view code: {may_view_code}): {seen}"
    )
