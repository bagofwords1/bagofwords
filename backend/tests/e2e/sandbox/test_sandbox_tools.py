"""The four agent-sandbox tools, end to end on the fake compute.

Each test calls a tool the way the agent loop does: ``run_stream`` with the
runtime context of a real report, user and organization. What is checked is
what a user and the agent experience: code runs, its output streams, what it
writes under ``outputs/`` becomes a report file usable later in the same
turn, only the report's own files can be passed in, and the tools exist only
for those who may use them.
"""
import asyncio
import json
import types
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.ai.registry import ToolRegistry
from app.ai.tools.implementations._file_tool_common import resolve_session_file
from app.core.code_visibility import redact_tool_arguments
from app.ee.audit.models import AuditLog
from app.models.file import File
from app.models.organization import Organization
from app.models.report import Report
from app.models.sandbox import SandboxRun
from app.models.user import User
from app.services.sandbox import prompt as sandbox_prompt
from app.services.sandbox.catalog import sandbox_run_note
from app.services.sandbox.feature import SANDBOX_TOOL_NAMES

from .conftest import run

pytestmark = pytest.mark.e2e

PNG = "import struct, zlib\n" \
      "raw = b'\\x00\\xff\\x00\\x00'\n" \
      "def chunk(kind, data):\n" \
      "    return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))\n" \
      "png = b'\\x89PNG\\r\\n\\x1a\\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', 1, 1, 8, 2, 0, 0, 0)) + " \
      "chunk(b'IDAT', zlib.compress(raw)) + chunk(b'IEND', b'')\n" \
      "open('outputs/chart.png', 'wb').write(png)\nprint('wrote the chart')\n"


@pytest.fixture
def world(sandbox_world, enterprise_license):
    sandbox_world.add_compute("fake-1")
    sandbox_world.environment_id = sandbox_world.add_environment("Default", is_default=True)
    return sandbox_world


@pytest.fixture
def report(world, create_report):
    def _new(title=None):
        return create_report(title=title or f"Report {uuid.uuid4().hex[:6]}",
                             user_token=world.cast["admin"]["token"], org_id=world.org_id)
    return _new


@pytest.fixture(autouse=True)
def asked(monkeypatch):
    """Stands in for the person in the chat. Records what they were asked and
    answers: allow by default; set ``asked.answer`` to "deny" or "timeout"."""
    from app.ai.tools.implementations import agent_sandbox

    record = types.SimpleNamespace(cards=[], answer="allow", feedback=None)

    async def ask(runtime_ctx, tool_name, payload, decision):
        record.cards.append({"tool": tool_name, **payload})
        decision.update(refused=record.answer != "allow", timed_out=record.answer == "timeout", feedback=record.feedback)
        return
        yield  # an async generator, like the real one

    monkeypatch.setattr(agent_sandbox, "_ask", ask)
    return record


async def _ctx(world, db, report_id, user_id):
    """The runtime context the agent loop hands a tool."""
    from app.dependencies import async_session_maker

    organization = await db.get(Organization, world.org_id)
    user = await db.get(User, user_id) if user_id else None
    report = (await db.execute(
        select(Report).options(selectinload(Report.files)).where(Report.id == report_id)
    )).scalar_one()
    return {
        "db": db, "session_maker": async_session_maker, "organization": organization, "user": user,
        "report": report, "run_agents": [], "project_files": [], "agent_files": [],
        "tool_db_lock": asyncio.Lock(), "sigkill_event": asyncio.Event(), "excel_files": [],
    }


def call(world, tool_name, tool_input, report, user_id="admin", *, after=None, before=None):
    """Run one tool to its end. Returns (output, observation, events, extra)."""
    user_id = world.admin_id if user_id == "admin" else user_id

    async def _go():
        async with world.session() as db:
            ctx = await _ctx(world, db, report["id"], user_id)
            if before:
                await before(ctx)
            tool = ToolRegistry().get(tool_name)
            assert tool is not None, f"{tool_name} is not available"
            events = [event async for event in tool.run_stream(tool_input, ctx)]
            extra = await after(ctx) if after else None
            end = events[-1]
            assert end.type == "tool.end", [e.type for e in events]
            return end.payload["output"], end.payload["observation"], events, extra

    return run(_go())


# ── running code ─────────────────────────────────────────────────────────


def test_python_writes_a_png_and_it_appears_as_a_report_file_usable_in_the_same_turn(world, report, get_files_by_report):
    rep = report("Sales charts")

    async def later_tools(ctx):
        """What a tool running next in the same turn would see."""
        file_id = [f.id for f in ctx["report"].files if f.filename == "chart.png"]
        resolved = resolve_session_file(ctx, file_id[0]) if file_id else None
        return {
            "resolved": resolved is not None,
            "bytes": open(resolved.path, "rb").read()[:8] if resolved is not None else None,
            "in_excel_files": any(f.filename == "chart.png" for f in ctx["excel_files"]),
        }

    output, observation, events, same_turn = call(world, "run_in_sandbox", {"language": "python", "code": PNG}, rep, after=later_tools)

    assert output["success"] is True and output["exit_code"] == 0 and output["status"] == "succeeded"
    assert output["stdout"] == "wrote the chart\n"
    assert [a["name"] for a in output["artifacts"]] == ["chart.png"]
    artifact = output["artifacts"][0]
    assert artifact["type"] == "image/png" and artifact["size"] > 50 and artifact["file_id"]
    assert "A new sandbox was created" in " ".join(output["notes"])
    assert artifact["file_id"] in observation["summary"], "the agent is told the file id"

    # Output streamed while the code ran, before the final result.
    streamed = [e for e in events if e.type == "tool.stdout"]
    assert streamed and "wrote the chart" in "".join(e.payload["text"] for e in streamed)
    assert events[0].type == "tool.start"

    # It is a report file: a tool product, linked to this report, visible through the API.
    row = world.rows(File, File.id == artifact["file_id"])[0]
    assert row.source_kind == "artifact" and row.content_type == "image/png" and row.filename == "chart.png"
    listed = get_files_by_report(rep["id"], user_token=world.cast["admin"]["token"], org_id=world.org_id)
    assert artifact["file_id"] in [f["id"] for f in listed]
    # ...and usable by the next tool of the same turn.
    assert same_turn == {"resolved": True, "bytes": b"\x89PNG\r\n\x1a\n", "in_excel_files": True}
    # The run records which files it produced.
    assert world.rows(SandboxRun, SandboxRun.id == output["run_id"])[0].artifact_file_ids == [artifact["file_id"]]


def test_a_failing_program_comes_back_as_a_result_the_agent_can_fix(world, report):
    output, observation, _, _ = call(world, "run_in_sandbox", {"language": "python", "code": "import nope_not_a_module"}, report())
    assert output["success"] is False and output["status"] == "failed" and output["exit_code"] == 1
    assert "ModuleNotFoundError" in output["stderr"] and output["error_code"] is None
    assert observation["exit_code"] == 1 and "ModuleNotFoundError" in observation["stderr"]


def test_shell_commands_extra_files_and_other_languages(world, report):
    rep = report()
    output, _, _, _ = call(world, "run_in_sandbox", {
        "language": "bash", "command": "cat notes/a.txt && sh run.sh",
        "files": [{"path": "notes/a.txt", "content": "first\n"}, {"path": "run.sh", "content": "echo second"}],
    }, rep)
    assert output["success"] and output["stdout"] == "first\nsecond\n"
    # The sandbox is the same one, with its files, on the next call.
    again, _, _, _ = call(world, "run_in_sandbox", {"language": "bash", "code": "cat notes/a.txt"}, rep)
    assert again["stdout"] == "first\n" and again["sandbox_id"] == output["sandbox_id"] and again["notes"] == []


def test_a_package_install_without_registry_access_is_refused_in_plain_words(world, report):
    output, observation, _, _ = call(world, "run_in_sandbox", {"language": "python", "code": "import requests", "packages": ["requests"]}, report())
    assert output["success"] is False and output["error_code"] == "sandbox.packages_need_network"
    assert "no internet access" in output["error_message"] and "admin" in output["error_message"]
    assert observation["success"] is False and "no internet access" in observation["summary"]


def test_a_run_is_stopped_at_its_time_limit(world, report):
    output, _, _, _ = call(world, "run_in_sandbox", {
        "language": "python", "code": "import time\nprint('started', flush=True)\ntime.sleep(60)", "timeout_seconds": 1}, report())
    assert output["status"] == "timeout" and output["success"] is False and output["stdout"] == "started\n"
    assert any("time limit" in note for note in output["notes"])


def test_stopping_the_agent_cancels_the_run(world, report):
    async def stop_soon(ctx):
        asyncio.get_running_loop().call_later(0.7, ctx["sigkill_event"].set)

    output, _, _, _ = call(world, "run_in_sandbox", {
        "language": "python", "code": "import time\nprint('started', flush=True)\ntime.sleep(60)"}, report(), before=stop_soon)
    assert output["status"] == "cancelled" and any("cancelled" in note for note in output["notes"])


# ── input files ──────────────────────────────────────────────────────────


def test_only_files_of_the_same_report_can_be_passed_in(world, report, upload_csv_file):
    admin = world.cast["admin"]
    mine, other = report("Mine"), report("Someone else's")
    own_file = upload_csv_file(user_token=admin["token"], org_id=world.org_id, filename="q1.csv",
                               content=b"region,sales\neu,10\nus,20\n", report_id=mine["id"])
    foreign_file = upload_csv_file(user_token=admin["token"], org_id=world.org_id, filename="secret.csv",
                                   content=b"do,not,leak\n", report_id=other["id"])

    output, _, _, _ = call(world, "run_in_sandbox", {
        "language": "python", "input_file_ids": [own_file["id"]],
        "code": "import csv\nrows = list(csv.DictReader(open('inputs/q1.csv')))\nprint(sum(int(r['sales']) for r in rows))",
    }, mine)
    assert output["success"] and output["stdout"] == "30\n"
    audited = [a for a in world.rows(AuditLog, AuditLog.action == "sandbox.input_file")]
    assert [(a.details["file_id"], a.details["file_name"], a.details["title"]) for a in audited] == [(own_file["id"], "q1.csv", "Mine")]

    refused, _, _, _ = call(world, "run_in_sandbox", {
        "language": "bash", "code": "cat inputs/*", "input_file_ids": [foreign_file["id"]]}, mine)
    assert refused["success"] is False and refused["error_code"] == "sandbox.input_not_allowed"
    assert len(world.rows(SandboxRun, SandboxRun.report_id == mine["id"])) == 1, "the refused call ran nothing"
    for missing in (str(uuid.uuid4()), "not-an-id"):
        assert call(world, "run_in_sandbox", {"language": "bash", "code": "true", "input_file_ids": [missing]}, mine)[0][
            "error_code"] == "sandbox.input_not_allowed"


# ── create, status, delete ───────────────────────────────────────────────


def test_create_status_and_delete_follow_the_reports_one_sandbox(world, report):
    rep = report()
    before, _, _, _ = call(world, "sandbox_status", {}, rep)
    assert before["success"] and before["exists"] is False
    assert [e["name"] for e in before["available_environments"]] == ["Default"]
    assert "no internet access" in before["available_environments"][0]["network"]

    created, observation, _, _ = call(world, "create_sandbox", {}, rep)
    assert created["success"] and created["created"] is True and created["state"] == "ready"
    assert created["environment"] == "Default" and created["network_policy"] == "none"
    assert created["languages"] == ["python", "node", "go", "bash"] and created["limits"]["memory_mb"] == 1024
    assert "no internet access" in created["network"] and "no internet access" in observation["summary"]

    same, _, _, _ = call(world, "create_sandbox", {}, rep)
    assert same["sandbox_id"] == created["sandbox_id"] and same["created"] is False, "returns the existing sandbox"

    call(world, "run_in_sandbox", {"language": "bash", "code": "echo hi"}, rep)
    status, _, _, _ = call(world, "sandbox_status", {}, rep)
    assert status["exists"] and status["state"] == "ready" and status["sandbox_id"] == created["sandbox_id"]
    assert [r["status"] for r in status["recent_runs"]] == ["succeeded"] and status["expires_at"]

    deleted, _, _, _ = call(world, "delete_sandbox", {}, rep)
    assert deleted == {"success": True, "deleted": True, "error_code": None, "error_message": None}
    assert call(world, "sandbox_status", {}, rep)[0]["exists"] is False
    assert call(world, "delete_sandbox", {}, rep)[0]["deleted"] is False
    fresh, _, _, _ = call(world, "run_in_sandbox", {"language": "bash", "code": "echo again"}, rep)
    assert fresh["sandbox_id"] != created["sandbox_id"]


def test_an_environment_can_be_chosen_by_name_and_an_unknown_one_is_refused(world, report):
    world.add_environment("With registries", network_policy="registries")
    chosen, _, _, _ = call(world, "create_sandbox", {"environment": "With registries"}, report())
    assert chosen["environment"] == "With registries" and "package registries" in chosen["network"]
    unknown, _, _, _ = call(world, "create_sandbox", {"environment": "No such"}, report())
    assert unknown["success"] is False and unknown["error_code"] == "sandbox.environment_not_allowed"


# ── who is offered the tools, and who can call them ──────────────────────


def _member(world, invite_user_to_org, create_role, assign_role, *permissions):
    admin = world.cast["admin"]
    member = invite_user_to_org(org_id=world.org_id, admin_token=admin["token"])
    if permissions:
        role = create_role(name=f"r-{uuid.uuid4().hex[:6]}", permissions=list(permissions), user_token=admin["token"], org_id=world.org_id).json()
        assign_role(role_id=role["id"], principal_type="user", principal_id=member["user_id"], user_token=admin["token"], org_id=world.org_id)
    return member


def _note(world, rep, user_id):
    async def _go(db):
        ctx = await _ctx(world, db, rep["id"], user_id)
        return await sandbox_run_note(db, ctx["organization"], ctx["report"], ctx["user"], [])
    return world.call(_go)


def _catalog_after_filter(world, rep, user_id):
    """The agent's own per-user filter, applied to a catalog holding the sandbox tools and one other."""
    from app.ai.agent_v2 import AgentV2
    from app.schemas.ai.planner import ToolDescriptor

    async def _go(db):
        ctx = await _ctx(world, db, rep["id"], user_id)
        catalog = [ToolDescriptor(name=name, description=f"{name} description") for name in sorted(SANDBOX_TOOL_NAMES) + ["create_data"]]
        agent = types.SimpleNamespace(
            planner=types.SimpleNamespace(tool_catalog=catalog), db=db, organization=ctx["organization"],
            report=ctx["report"], head_completion=types.SimpleNamespace(user=ctx["user"]), data_sources=[],
        )
        await AgentV2._apply_agent_sandbox_filter(agent)
        return {t.name: t.description for t in agent.planner.tool_catalog}
    return world.call(_go)


def test_the_tools_are_offered_with_a_note_about_network_access(world, report):
    rep = report()
    catalog = _catalog_after_filter(world, rep, world.admin_id)
    assert SANDBOX_TOOL_NAMES <= set(catalog) and "create_data" in catalog
    for name in ("run_in_sandbox", "create_sandbox"):
        assert "no sandbox yet" in catalog[name] and "'Default' (default)" in catalog[name]
        assert "no internet access" in catalog[name]
    assert catalog["create_data"] == "create_data description", "other tools are not touched"

    call(world, "run_in_sandbox", {"language": "bash", "code": "true"}, rep)
    after = _catalog_after_filter(world, rep, world.admin_id)["run_in_sandbox"]
    assert "sandbox exists (ready)" in after and "environment 'Default'" in after and "no internet access" in after


def test_the_tools_are_hidden_and_refuse_without_the_permission_or_an_environment(world, report, invite_user_to_org,
                                                                                create_role, assign_role):
    rep = report()
    plain = _member(world, invite_user_to_org, create_role, assign_role)
    permitted = _member(world, invite_user_to_org, create_role, assign_role, "use_agent_sandbox")

    for user in (plain, permitted):  # no permission; permission but no environment granted
        assert _note(world, rep, user["user_id"]) is None
        assert set(_catalog_after_filter(world, rep, user["user_id"])) == {"create_data"}
    refused, _, _, _ = call(world, "run_in_sandbox", {"language": "bash", "code": "true"}, rep, user_id=plain["user_id"])
    assert refused["success"] is False and refused["error_code"] == "sandbox.forbidden"
    no_env, _, _, _ = call(world, "run_in_sandbox", {"language": "bash", "code": "true"}, rep, user_id=permitted["user_id"])
    assert no_env["error_code"] == "sandbox.no_environment"
    for tool in ("create_sandbox", "sandbox_status", "delete_sandbox"):
        assert call(world, tool, {}, rep, user_id=plain["user_id"])[0]["error_code"] == "sandbox.forbidden"
    assert world.sandbox_of(rep) is None

    from app.services.sandbox import access as access_module
    from app.services.sandbox import environments as environment_service

    async def grant(db):
        env = await environment_service.get_environment(db, world.org_id, world.environment_id)
        await access_module.set_environment_grants(db, world.org_id, world.admin_id, env,
                                                   [{"principal_type": "user", "principal_id": permitted["user_id"]}])
    world.call(grant)
    assert SANDBOX_TOOL_NAMES <= set(_catalog_after_filter(world, rep, permitted["user_id"]))
    ok, _, _, _ = call(world, "run_in_sandbox", {"language": "bash", "code": "echo mine"}, rep, user_id=permitted["user_id"])
    assert ok["success"] and world.rows(SandboxRun, SandboxRun.id == ok["run_id"])[0].user_id == permitted["user_id"]


def test_a_run_nobody_triggered_in_person_acts_as_the_report_owner(world, report):
    rep = report()
    output, _, _, _ = call(world, "run_in_sandbox", {"language": "bash", "code": "echo scheduled"}, rep, user_id=None)
    assert output["success"]
    assert world.rows(SandboxRun, SandboxRun.id == output["run_id"])[0].user_id == world.admin_id


def test_with_the_organization_switch_off_the_tools_are_not_offered_and_refuse(world, report):
    rep = report()
    world.configure(enabled=False)
    assert _note(world, rep, world.admin_id) is None
    assert call(world, "run_in_sandbox", {"language": "bash", "code": "true"}, rep)[0]["error_code"] == "sandbox.disabled"
    assert call(world, "sandbox_status", {}, rep)[0]["error_code"] == "sandbox.disabled"


# ── the deployment flag ──────────────────────────────────────────────────


def test_with_the_flag_off_the_tools_do_not_exist(sandbox_cast, agent_sandboxes_off):
    """Not in any catalog, not executable, and the prompt does not mention them."""
    registry = ToolRegistry()
    assert not {m.name for m in registry.list_tools()} & SANDBOX_TOOL_NAMES
    for mode in ("chat", "training", "knowledge", None):
        for plan in ("action", "research"):
            names = {t["name"] for t in registry.get_catalog_for_plan_type(plan, None, mode=mode)}
            assert not names & SANDBOX_TOOL_NAMES, (mode, plan)
    assert all(registry.get(name) is None for name in SANDBOX_TOOL_NAMES)
    assert {m.name for m in registry.list_tools()} >= {"create_data", "inspect_data"}, "the other tools are untouched"


def test_with_the_flag_on_the_tools_are_listed_for_reports_only(sandbox_cast, agent_sandboxes_on):
    registry = ToolRegistry()
    chat = {t["name"] for plan in ("action", "research") for t in registry.get_catalog_for_plan_type(plan, None, mode="chat")}
    assert SANDBOX_TOOL_NAMES <= chat
    for mode in ("training", "knowledge", "subagent"):
        names = {t["name"] for plan in ("action", "research") for t in registry.get_catalog_for_plan_type(plan, None, mode=mode)}
        assert not names & SANDBOX_TOOL_NAMES, mode
    run_tool = registry.get_metadata("run_in_sandbox")
    assert run_tool.timeout_seconds >= 3600, "a run may take as long as an environment allows"
    assert run_tool.max_retries == 0, "a run has side effects and is never repeated automatically"
    assert all(registry.get_metadata(n).required_permissions == ["use_agent_sandbox"] for n in SANDBOX_TOOL_NAMES)


def test_the_prompt_mentions_the_sandbox_only_when_its_tools_are_in_the_catalog():
    from app.ai.agents.planner.prompt_builder import PromptBuilder
    from app.schemas.ai.planner import ToolDescriptor

    with_tools = types.SimpleNamespace(tool_catalog=[ToolDescriptor(name="run_in_sandbox"), ToolDescriptor(name="create_data")])
    without = types.SimpleNamespace(tool_catalog=[ToolDescriptor(name="create_data")])
    assert "agent_sandbox_guidance" in PromptBuilder._render_agent_sandbox_block(with_tools)
    assert "input_file_ids" in sandbox_prompt.prompt_section(with_tools.tool_catalog)
    assert PromptBuilder._render_agent_sandbox_block(without) == "" == sandbox_prompt.prompt_section(without.tool_catalog)
    assert sandbox_prompt.prompt_section(None) == ""


# ── code visibility ──────────────────────────────────────────────────────


def test_run_code_is_hidden_from_viewers_without_view_code():
    arguments = {"language": "python", "code": "print('secret logic')", "command": "python x.py --key abc",
                 "files": [{"path": "x.py", "content": "more logic"}], "packages": ["pandas"], "timeout_seconds": 30}
    hidden = redact_tool_arguments(arguments, False, "run_in_sandbox")
    assert hidden["code"] is None and hidden["command"] is None and hidden["files"] is None
    assert hidden["language"] == "python" and hidden["packages"] == ["pandas"] and hidden["timeout_seconds"] == 30
    assert "secret logic" not in json.dumps(hidden) and "more logic" not in json.dumps(hidden)
    assert redact_tool_arguments(arguments, True, "run_in_sandbox") == arguments
    # Tools that are not code-producing keep their arguments as before.
    assert redact_tool_arguments({"code": "x"}, False, "create_artifact") == {"code": "x"}
    assert redact_tool_arguments({"code": "x", "command": "y"}, False, "write_csv") == {"code": None, "command": "y"}


# ── asking the person in the chat first ──────────────────────────────────


def test_deleting_the_sandbox_always_asks_and_a_no_keeps_it(world, report, asked):
    rep = report()
    created, _, _, _ = call(world, "run_in_sandbox", {"language": "bash", "code": "echo keep > f.txt"}, rep)
    assert asked.cards == [], "a plain run in an environment that does not ask is not held up"

    asked.answer, asked.feedback = "deny", "I still need it"
    refused, observation, _, _ = call(world, "delete_sandbox", {}, rep)
    assert refused["success"] is False and refused["error_code"] == "sandbox.declined" and refused["deleted"] is False
    assert "declined" in observation["summary"] and "I still need it" in observation["summary"]
    assert asked.cards[-1]["tool"] == "delete_sandbox" and "Files already saved to the report are kept" in asked.cards[-1]["reason"]
    assert call(world, "run_in_sandbox", {"language": "bash", "code": "cat f.txt"}, rep)[0]["stdout"] == "keep\n"

    asked.answer = "timeout"
    late, observation, _, _ = call(world, "delete_sandbox", {}, rep)
    assert late["deleted"] is False and "not confirmed in time" in observation["summary"]

    asked.answer = "allow"
    assert call(world, "delete_sandbox", {}, rep)[0]["deleted"] is True
    before = len(asked.cards)
    assert call(world, "delete_sandbox", {}, rep)[0]["deleted"] is False
    assert len(asked.cards) == before, "nothing to delete, nothing to ask"


@pytest.mark.parametrize("policy, network, packages, asks", [
    ("never", "full", ["requests"], False),
    ("always", "none", [], True),
    ("full_network", "full", [], True),
    ("full_network", "registries", ["requests"], False),
    ("packages", "registries", ["requests"], True),
    ("packages", "registries", [], False),
])
def test_an_environment_decides_when_a_run_is_confirmed_first(world, report, asked, policy, network, packages, asks):
    world.add_environment("Asks", confirm_runs=policy, network_policy=network)
    rep = report()
    call(world, "create_sandbox", {"environment": "Asks"}, rep)
    asked.answer = "deny"
    output, observation, _, _ = call(world, "run_in_sandbox", {"language": "bash", "code": "echo ran", "packages": packages}, rep)
    if asks:
        assert output["error_code"] == "sandbox.declined" and "declined the run" in observation["summary"]
        card = asked.cards[-1]
        assert (card["tool"], card["environment"], card["network_policy"], card["packages"]) == ("run_in_sandbox", "Asks", network, packages)
        assert world.rows(SandboxRun, SandboxRun.report_id == rep["id"]) == [], "nothing ran"
    else:
        assert asked.cards == [] and output.get("error_code") != "sandbox.declined"


def test_the_confirmation_card_shows_the_code_only_to_someone_who_may_see_code(world, report, asked):
    from app.core.code_visibility import set_code_visibility

    world.add_environment("Asks", confirm_runs="always")
    rep = report()
    call(world, "create_sandbox", {"environment": "Asks"}, rep)

    call(world, "run_in_sandbox", {"language": "python", "code": "print('secret logic')"}, rep)
    assert asked.cards[-1]["code"] is None and asked.cards[-1]["language"] == "python"

    async def allowed(ctx):
        set_code_visibility(True)
    out, _, _, _ = call(world, "run_in_sandbox", {"language": "python", "code": "print('shown')"}, rep, before=allowed)
    assert asked.cards[-1]["code"] == "print('shown')" and out["stdout"] == "shown\n"
