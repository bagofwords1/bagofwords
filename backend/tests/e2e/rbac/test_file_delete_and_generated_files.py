"""Deleting files, and keeping tool output from piling up in the prompt.

Reported: a daily scheduled report calling two custom API tools gained one
saved response file per call per run — 1,128 files in three months — with no
way to delete them, and every run's prompt listed all of them, so run cost
grew with the report's history.

Contract:
  * the Files page lists the user's own files only — admins included;
  * a file's creator can delete it, one at a time or in bulk; it is then gone
    everywhere: the Files page, the report, its content;
  * anyone else — an admin too — gets "not found" for that id, and the file
    stays;
  * a bulk delete reports each refused id and still deletes the rest;
  * the files the agent produced itself are listed in the prompt up to a
    fixed cap, newest first, however many earlier runs left behind; uploads
    are always listed.

The LLM boundary is stubbed; routes, services, the agent loop and the tool
gateway run real, and the tool output files come from real custom API calls.
"""
from __future__ import annotations

import json
import re
import threading
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

pytestmark = pytest.mark.e2e


class _Planner:
    """Scripted planner: replays queued tool calls, one per request, then answers."""

    def __init__(self):
        self.prompts: list[str] = []
        self.queue: list = []

    def stream(self):
        from app.ai.llm.types import (
            MessageStopEvent, TextDeltaEvent, ToolUseCompleteEvent, ToolUseStartEvent, UsageEvent,
        )
        planner = self

        async def scripted(self, *, model_id=None, messages, system=None, tools=None, **kwargs):
            planner.prompts.append(json.dumps([system, messages], default=str).replace('\\"', '"'))
            if planner.queue:
                name, args = planner.queue.pop(0)
                tid = f"tu_{uuid.uuid4().hex[:8]}"
                events = [ToolUseStartEvent(id=tid, name=name), ToolUseCompleteEvent(id=tid, name=name, input=args),
                          MessageStopEvent(stop_reason="tool_use", raw_stop_reason="tool_use")]
            else:
                events = [TextDeltaEvent(text="Done."), MessageStopEvent(stop_reason="end_turn", raw_stop_reason="end_turn")]
            for e in events + [UsageEvent(input_tokens=10, output_tokens=5)]:
                yield e

        return scripted


@pytest.fixture
def planner(monkeypatch):
    from app.ai.llm.llm import LLM
    from app.ai.tools.mcp_tool_registry import clear_usage_cache

    clear_usage_cache()
    p = _Planner()

    async def _stream(self, prompt, **kwargs):
        yield "ok"

    monkeypatch.setattr(LLM, "inference_stream_v2", p.stream())
    monkeypatch.setattr(LLM, "inference_stream", _stream)
    monkeypatch.setattr(LLM, "inference", lambda self, prompt, *a, **k: "[]")
    yield p
    clear_usage_cache()


@pytest.fixture
def cost_api():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps({"rows": [{"user": "a", "cost": 1.25}, {"user": "b", "cost": 2.5}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


@pytest.fixture
def org(test_client, bootstrap_admin, invite_user_to_org, create_llm_provider_and_models, enable_mcp,
        create_custom_api_connection, create_domain_from_connection, wait_for_connection_indexing,
        cost_api, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY_TEST", "dummy-local-provider-boundary")
    admin = bootstrap_admin("filedel")
    token, org_id = admin["token"], str(admin["org_id"])
    create_llm_provider_and_models(user_token=token, org_id=org_id)
    enable_mcp(user_token=token, org_id=org_id)
    slug = f"billing{uuid.uuid4().hex[:4]}"
    conn = create_custom_api_connection(
        name=slug, base_url=cost_api, user_token=token, org_id=org_id,
        endpoints=[{"name": "cost_report", "method": "GET", "path": "/cost", "description": "Cost report",
                    "parameters": [{"name": "days", "in": "query", "type": "integer", "description": "Window"}]}],
    )
    h = _h(admin["token"], org_id)
    ds = create_domain_from_connection(name=f"{slug} agent", connection_id=conn["id"], user_token=token,
                                       org_id=org_id, is_public=True)
    # Creation starts a background discovery run that writes the tool catalog
    # with the fixture's mock client; let it finish before the real refresh.
    wait_for_connection_indexing(connection_id=conn["id"], user_token=token, org_id=org_id)
    assert test_client.post(f"/api/connections/{conn['id']}/refresh-tools", headers=h).status_code == 200
    member = invite_user_to_org(org_id=org_id, admin_token=token)
    return {"admin": admin, "member": member, "org_id": org_id, "tool": f"mcp__{slug}__cost_report",
            "ds_id": str(ds["id"])}


def _h(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _report(test_client, user, org):
    r = test_client.post("/api/reports", headers=_h(user["token"], org["org_id"]),
                         json={"title": "costs", "files": [], "data_sources": [org["ds_id"]]})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _run(test_client, user, org, report_id, calls: int, planner):
    planner.queue = [(org["tool"], {"days": d}) for d in range(1, calls + 1)]
    r = test_client.post(f"/api/reports/{report_id}/completions", headers=_h(user["token"], org["org_id"]),
                         params={"background": False}, json={"prompt": {"content": "Pull the cost report."}})
    assert r.status_code == 200, r.text


def _report_files(test_client, user, org, report_id):
    r = test_client.get(f"/api/reports/{report_id}/files", headers=_h(user["token"], org["org_id"]))
    assert r.status_code == 200, r.text
    return r.json()


def _org_file_ids(test_client, user, org):
    r = test_client.get("/api/files", headers=_h(user["token"], org["org_id"]))
    assert r.status_code == 200, r.text
    return {f["id"] for f in r.json()}


def _tool_outputs(test_client, user, org, report_id):
    return [f for f in _report_files(test_client, user, org, report_id) if f.get("source_kind") == "artifact"]


def _upload(test_client, user, org, report_id, name="notes.txt"):
    r = test_client.post("/api/files", headers=_h(user["token"], org["org_id"]),
                         files={"file": (name, b"hello", "text/plain")}, data={"report_id": report_id})
    assert r.status_code == 200, r.text
    return r.json()["id"]


@pytest.mark.parametrize("role", ["admin", "member"])
def test_a_creator_deletes_their_tool_output_in_bulk_and_it_is_gone_everywhere(test_client, org, planner, role):
    user = org[role]
    report_id = _report(test_client, user, org)
    _run(test_client, user, org, report_id, calls=3, planner=planner)
    outputs = _tool_outputs(test_client, user, org, report_id)
    assert len(outputs) == 3, outputs
    keep = _upload(test_client, user, org, report_id, "email template.html")

    r = test_client.post("/api/files/delete", headers=_h(user["token"], org["org_id"]),
                         json={"file_ids": [f["id"] for f in outputs]})
    assert r.status_code == 200, r.text
    assert set(r.json()["deleted"]) == {f["id"] for f in outputs}
    assert r.json()["failed"] == []

    remaining = {f["id"] for f in _report_files(test_client, user, org, report_id)}
    assert remaining == {keep}
    assert not ({f["id"] for f in outputs} & _org_file_ids(test_client, user, org))
    for f in outputs:
        c = test_client.get(f"/api/files/{f['id']}/content", headers=_h(user["token"], org["org_id"]))
        assert c.status_code == 404, c.text


def test_a_single_delete_removes_one_file(test_client, org, planner):
    user = org["member"]
    report_id = _report(test_client, user, org)
    a = _upload(test_client, user, org, report_id, "a.txt")
    b = _upload(test_client, user, org, report_id, "b.txt")

    r = test_client.delete(f"/api/files/{a}", headers=_h(user["token"], org["org_id"]))
    assert r.status_code == 204, r.text
    assert {f["id"] for f in _report_files(test_client, user, org, report_id)} == {b}
    again = test_client.delete(f"/api/files/{a}", headers=_h(user["token"], org["org_id"]))
    assert again.status_code == 404
    assert again.json()["error_code"] == "file.not_found"


@pytest.mark.parametrize("deleter,owner", [("member", "admin"), ("admin", "member")])
def test_only_the_creator_can_delete_a_file_whatever_the_role(test_client, org, planner, deleter, owner):
    owners_file = _upload(test_client, org[owner], org, _report(test_client, org[owner], org))

    r = test_client.delete(f"/api/files/{owners_file}", headers=_h(org[deleter]["token"], org["org_id"]))
    assert r.status_code == 404
    assert r.json()["error_code"] == "file.not_found"
    bulk = test_client.post("/api/files/delete", headers=_h(org[deleter]["token"], org["org_id"]),
                            json={"file_ids": [owners_file]})
    assert bulk.status_code == 200 and bulk.json()["deleted"] == []
    assert owners_file in _org_file_ids(test_client, org[owner], org)


@pytest.mark.parametrize("viewer,owner", [("member", "admin"), ("admin", "member")])
def test_the_files_page_lists_only_your_own_files_whatever_the_role(test_client, org, planner, viewer, owner):
    mine = _upload(test_client, org[viewer], org, _report(test_client, org[viewer], org), "mine.txt")
    theirs = _upload(test_client, org[owner], org, _report(test_client, org[owner], org), "theirs.txt")

    listed = {f["id"]: f for f in test_client.get("/api/files", headers=_h(org[viewer]["token"], org["org_id"])).json()}
    assert mine in listed and listed[mine]["can_delete"] is True
    assert theirs not in listed


def test_a_bulk_delete_reports_refused_ids_and_deletes_the_rest(test_client, org, planner):
    admin, member = org["admin"], org["member"]
    mine = [_upload(test_client, member, org, _report(test_client, member, org), f"m{i}.txt") for i in range(2)]
    theirs = _upload(test_client, admin, org, _report(test_client, admin, org))
    unknown = str(uuid.uuid4())

    r = test_client.post("/api/files/delete", headers=_h(member["token"], org["org_id"]),
                         json={"file_ids": mine + [theirs, unknown]})
    assert r.status_code == 200, r.text
    assert set(r.json()["deleted"]) == set(mine)
    assert {f["id"] for f in r.json()["failed"]} == {theirs, unknown}
    assert {f["error_code"] for f in r.json()["failed"]} == {"file.not_found"}
    assert theirs in _org_file_ids(test_client, admin, org)


def test_a_file_from_another_org_cannot_be_deleted(test_client, org, planner, bootstrap_admin):
    other = bootstrap_admin("otherorg")
    r = test_client.post("/api/reports", headers=_h(other["token"], other["org_id"]),
                         json={"title": "x", "files": [], "data_sources": []})
    other_file = test_client.post("/api/files", headers=_h(other["token"], other["org_id"]),
                                  files={"file": ("x.txt", b"x", "text/plain")},
                                  data={"report_id": r.json()["id"]}).json()["id"]

    r = test_client.delete(f"/api/files/{other_file}", headers=_h(org["admin"]["token"], org["org_id"]))
    assert r.status_code == 404
    assert other_file in _org_file_ids(test_client, other, {"org_id": other["org_id"]})


def _listed_file_ids(prompt: str) -> set:
    return set(re.findall(r'<file id="([0-9a-f-]{36})"', prompt))


@pytest.mark.parametrize("cap", [2, 3])
def test_the_prompt_lists_a_bounded_number_of_tool_outputs_however_many_runs_left(
        test_client, org, planner, monkeypatch, cap):
    from app.ai.context.builders import files_context_builder

    monkeypatch.setattr(files_context_builder, "GENERATED_FILES_MAX", cap)
    user = org["admin"]
    report_id = _report(test_client, user, org)
    upload = _upload(test_client, user, org, report_id)
    for _ in range(3):  # three earlier "scheduled" runs, two tool calls each
        _run(test_client, user, org, report_id, calls=2, planner=planner)
    outputs = _tool_outputs(test_client, user, org, report_id)
    assert len(outputs) == 6

    planner.prompts.clear()
    _run(test_client, user, org, report_id, calls=0, planner=planner)
    listed = _listed_file_ids(planner.prompts[0])
    listed_outputs = listed & {f["id"] for f in outputs}
    assert len(listed_outputs) == cap, planner.prompts[0][:2000]
    newest = {f["id"] for f in sorted(outputs, key=lambda f: f["created_at"], reverse=True)[:cap]}
    assert listed_outputs == newest
    assert upload in listed
    assert f'omitted="{6 - cap}"' in planner.prompts[0]
