"""File tools follow the run's agents, in Auto and in a pinned selection.

Contract:
  * the file tools (list_files / read_file / search_files ...) are offered when
    an agent the run executes against has a file source — "the run's agents"
    being the pinned agents, or in Auto every agent the RUNNING user can
    access. An Auto report has no attachments, which used to hide every file
    tool (the model answered "I have no read_file tool") even with a
    ServiceNow / SharePoint / network-folder agent one click away;
  * read_file resolves a file source in that working set, by connection id,
    agent id or agent name;
  * a file source outside the working set is neither offered nor readable,
    for admins and members alike.

The LLM boundary (``LLM.inference_stream_v2`` / ``LLM.inference``) is stubbed;
routes, services, the agent loop, the tool gateway and the network-folder
client (over a temp directory) run real. Each planner request is recorded with
the tool names it offered and every tool result it saw.
"""
from __future__ import annotations

import json
import uuid

import pytest

pytestmark = pytest.mark.e2e

FILE_TOOLS = {"list_files", "read_file", "search_files"}


class _Planner:
    def __init__(self):
        self.tool_names: list[set] = []
        self.force_tool: tuple | None = None
        self.script: list[tuple] = []  # one forced call per planner request, in order
        self.tool_results: list[str] = []

    def stream(self):
        from app.ai.llm.types import (
            MessageStopEvent,
            TextDeltaEvent,
            ToolUseCompleteEvent,
            ToolUseStartEvent,
            UsageEvent,
        )
        planner = self

        async def scripted(self, *, model_id=None, messages, system=None, tools=None, **kwargs):
            names = {getattr(t, "name", None) or (t.get("name") if isinstance(t, dict) else None) for t in (tools or [])}
            names.discard(None)
            results = [
                json.dumps(b.get("content"))
                for m in messages or []
                for b in ((m.get("content") if isinstance(m, dict) else getattr(m, "content", None)) or [])
                if isinstance(b, dict) and b.get("type") == "tool_result"
            ]
            planner.tool_names.append(names)
            forced = None
            if planner.script:
                forced = planner.script.pop(0)
            elif not results and planner.force_tool:
                forced, planner.force_tool = planner.force_tool, None
            planner.tool_results = results
            if forced:
                name, args = forced
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

    p = _Planner()

    async def _stream(self, prompt, **kwargs):
        yield "ok"

    monkeypatch.setattr(LLM, "inference_stream_v2", p.stream())
    monkeypatch.setattr(LLM, "inference_stream", _stream)
    monkeypatch.setattr(LLM, "inference", lambda self, prompt, *a, **k: "[]")
    return p


@pytest.fixture
def org(tmp_path, test_client, bootstrap_admin, invite_user_to_org, create_llm_provider_and_models,
        create_data_source, update_data_source, dynamic_sqlite_db, monkeypatch):
    """Admin + member; a public and an admin-only file agent, plus a query-only agent."""
    monkeypatch.setenv("OPENAI_API_KEY_TEST", "dummy-local-provider-boundary")
    admin = bootstrap_admin("filescope")
    token, org_id = admin["token"], admin["org_id"]
    create_llm_provider_and_models(user_token=token, org_id=org_id)
    auth = {"user_token": token, "org_id": org_id}

    agents = {}
    for label, public in (("shared", True), ("private", False)):
        root = tmp_path / label
        root.mkdir()
        marker = f"{label.upper()}_MARKER_{uuid.uuid4().hex[:6]}"
        (root / f"{label}-procedure.txt").write_text(f"Procedure text. {marker}\n")
        name = f"{label} docs {uuid.uuid4().hex[:4]}"
        ds = create_data_source(name=name, type="network_dir", config={"root_path": str(root)},
                                credentials={"auth_type": "none"}, **auth)
        if public:
            update_data_source(data_source_id=ds["id"], payload={"is_public": True}, **auth)
        conn_id = str((ds.get("connections") or [{}])[0].get("id") or ds.get("connection_id"))
        agents[label] = {"name": name, "ds_id": str(ds["id"]), "conn_id": conn_id,
                         "file": f"{label}-procedure.txt", "marker": marker}

    query = create_data_source(name=f"orders {uuid.uuid4().hex[:4]}", type="sqlite",
                               config={"database": dynamic_sqlite_db}, credentials={}, **auth)
    agents["query"] = {"ds_id": str(query["id"])}

    member = invite_user_to_org(org_id=org_id, admin_token=token)
    return {"admin": admin, "member": member, "org_id": org_id, "agents": agents}


def _ask(test_client, token, org_id, data_sources, mode="chat"):
    h = {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}
    r = test_client.post("/api/reports", headers=h,
                         json={"title": "file scope", "files": [], "data_sources": data_sources, "mode": mode})
    assert r.status_code == 200, r.text
    assert bool(r.json().get("data_sources")) == bool(data_sources)
    r = test_client.post(f"/api/reports/{r.json()['id']}/completions", headers=h,
                         params={"background": False},
                         json={"prompt": {"content": "Read the procedure document.", "mode": mode}})
    assert r.status_code == 200, r.text


@pytest.mark.parametrize("who", ["admin", "member"])
def test_auto_offers_file_tools_when_an_accessible_agent_has_a_file_source(test_client, org, planner, who):
    _ask(test_client, org[who]["token"], org["org_id"], [])
    assert planner.tool_names, "the planner was never called"
    assert planner.tool_names[0] >= FILE_TOOLS, sorted(planner.tool_names[0])


@pytest.mark.parametrize("who", ["admin", "member"])
@pytest.mark.parametrize("address", ["conn_id", "ds_id", "name"])
def test_auto_read_file_resolves_a_file_source_in_the_working_set(test_client, org, planner, who, address):
    shared = org["agents"]["shared"]
    planner.force_tool = ("read_file", {"connection_id": shared[address], "file_id": shared["file"],
                                        "title": "Reading the procedure"})
    _ask(test_client, org[who]["token"], org["org_id"], [])
    assert any(shared["marker"] in r for r in planner.tool_results), planner.tool_results


def test_auto_member_cannot_read_a_file_agent_they_cannot_access(test_client, org, planner):
    private = org["agents"]["private"]
    planner.force_tool = ("read_file", {"connection_id": private["conn_id"], "file_id": private["file"],
                                        "title": "Reading the procedure"})
    _ask(test_client, org["member"]["token"], org["org_id"], [])
    assert planner.tool_results, "the forced read_file never produced a result"
    assert not any(private["marker"] in r for r in planner.tool_results), planner.tool_results


def test_admin_auto_reaches_every_file_agent(test_client, org, planner):
    private = org["agents"]["private"]
    planner.force_tool = ("read_file", {"connection_id": private["conn_id"], "file_id": private["file"],
                                        "title": "Reading the procedure"})
    _ask(test_client, org["admin"]["token"], org["org_id"], [])
    assert any(private["marker"] in r for r in planner.tool_results), planner.tool_results


def test_pinned_query_only_selection_offers_no_file_tools(test_client, org, planner):
    """A pinned selection is a hard scope: no file agent in it, no file tools —
    even though file agents exist in the org."""
    _ask(test_client, org["admin"]["token"], org["org_id"], [org["agents"]["query"]["ds_id"]])
    assert not (FILE_TOOLS & planner.tool_names[0]), sorted(planner.tool_names[0])


def test_pinned_selection_cannot_read_a_file_agent_outside_it(test_client, org, planner):
    shared = org["agents"]["shared"]
    planner.force_tool = ("read_file", {"connection_id": shared["conn_id"], "file_id": shared["file"],
                                        "title": "Reading the procedure"})
    _ask(test_client, org["admin"]["token"], org["org_id"], [org["agents"]["private"]["ds_id"]])
    assert planner.tool_results, "the forced read_file never produced a result"
    assert not any(shared["marker"] in r for r in planner.tool_results), planner.tool_results


def test_file_agent_attached_mid_run_brings_its_file_tools_to_the_next_step(test_client, org, planner):
    """create_agent attaches a new agent to the report mid-run: from the next
    planner step on, its file tools are offered and resolve — not only on the
    next turn."""
    shared = org["agents"]["shared"]
    planner.script = [
        ("create_agent", {"name": f"procedures {uuid.uuid4().hex[:4]}", "connection_ids": [shared["conn_id"]]}),
        ("read_file", {"connection_id": shared["conn_id"], "file_id": shared["file"],
                       "title": "Reading the procedure"}),
    ]
    _ask(test_client, org["admin"]["token"], org["org_id"], [org["agents"]["query"]["ds_id"]], mode="training")
    assert len(planner.tool_names) >= 3, planner.tool_names
    assert not (FILE_TOOLS & planner.tool_names[0]), "no file agent before create_agent"
    assert "create_agent" in planner.tool_names[0], sorted(planner.tool_names[0])
    assert planner.tool_names[1] >= FILE_TOOLS, sorted(planner.tool_names[1])
    assert any(shared["marker"] in r for r in planner.tool_results), planner.tool_results
