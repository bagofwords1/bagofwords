"""Agent library files reach the run live.

Files uploaded to an agent (its Files tab) used to reach a conversation only as
a snapshot copied onto the report at creation / agent switch. Auto-mode
reports (no agent picked) and files uploaded after the conversation started
never saw them, so the agent had no file ids and read_file / grep_files had
nothing to open.

These tests drive the same pieces a run uses — run-agent resolution (Auto
included), the <files> context block, read_file and grep_files — against the
real API and DB. Access is the run agents' access: a user never sees the
library of an agent they cannot use.
"""
import io
import uuid
from pathlib import Path

import pytest

_SQLITE_DB = (Path(__file__).resolve().parent.parent / "config" / "chinook.sqlite").resolve()


def _h(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _admin(create_user, login_user, whoami):
    email = f"agentfiles_{uuid.uuid4().hex[:6]}@test.com"
    create_user(email=email, password="test123")
    token = login_user(email=email, password="test123")
    me = whoami(token)
    return token, me["id"], me["organizations"][0]["id"]


def _member(test_client, create_user, login_user, whoami, admin_token, org_id):
    email = f"agentfiles_m_{uuid.uuid4().hex[:6]}@test.com"
    resp = test_client.post(
        f"/api/organizations/{org_id}/members",
        json={"organization_id": org_id, "email": email, "role": "member"},
        headers=_h(admin_token, org_id),
    )
    assert resp.status_code in (200, 201), resp.text
    create_user(email=email, password="test123")
    token = login_user(email=email, password="test123")
    return token, whoami(token)["id"]


def _agent(create_data_source, token, org_id, name="device_app"):
    return create_data_source(
        name=f"{name}_{uuid.uuid4().hex[:4]}", type="sqlite",
        config={"database": str(_SQLITE_DB)}, credentials={},
        user_token=token, org_id=org_id,
    )


def _upload_to_agent(test_client, token, org_id, ds_id, name, body: str, content_type="application/octet-stream"):
    resp = test_client.post(
        f"/api/data_sources/{ds_id}/files",
        files={"file": (name, io.BytesIO(body.encode()), content_type)},
        headers=_h(token, org_id),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


def _catalogue(marker: str, n: int = 400) -> str:
    lines = ["events:"]
    for i in range(n):
        lines += [f"  app_event_{i}:", f"    description: Generic app event number {i}."]
    lines += [f"  {marker}:", "    description: User presses the button to connect a BP monitor."]
    return "\n".join(lines)


async def _run_view(report_id: str, user_id: str, org_id: str):
    """What a run of `user` on `report` sees: its agents, the rendered <files>
    block, and a runtime_ctx staged the way the agent loop stages it."""
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from app.ai.context.agent_files import agent_library_files
    from app.ai.context.builders.files_context_builder import FilesContextBuilder
    from app.ai.tools.implementations.agent_focus_common import resolve_run_agents
    from app.dependencies import async_session_maker
    from app.models.organization import Organization
    from app.models.report import Report
    from app.models.user import User

    db = async_session_maker()
    report = (await db.execute(
        select(Report)
        .options(selectinload(Report.files), selectinload(Report.data_sources))
        .where(Report.id == report_id)
    )).scalars().first()
    user = (await db.execute(select(User).where(User.id == user_id))).scalars().first()
    org = (await db.execute(select(Organization).where(Organization.id == org_id))).scalars().first()
    agents = await resolve_run_agents(db, org, user, report)
    files_block = (await FilesContextBuilder(db, org, report, data_sources=agents).build()).render()
    ctx = {
        "db": db, "report": report, "organization": org, "user": user,
        "agent_files": agent_library_files(agents, org.id),
    }
    return db, files_block, ctx


async def _tool_end(tool, tool_input, ctx):
    end = None
    async for evt in tool.run_stream(tool_input, ctx):
        if evt.type in ("tool.end", "tool.error"):
            end = evt
    assert end is not None
    return end.payload


async def _read(file_id, ctx):
    from app.ai.tools.implementations.read_file import ReadFileTool
    return await _tool_end(ReadFileTool(), {"file_id": file_id}, ctx)


async def _grep(pattern, ctx):
    from app.ai.tools.implementations.grep_files import GrepFilesTool
    return await _tool_end(GrepFilesTool(), {"pattern": pattern}, ctx)


def _succeeded(payload) -> bool:
    out = payload.get("output") or {}
    return out.get("success") is not False and "error" not in payload


@pytest.mark.e2e
@pytest.mark.asyncio
@pytest.mark.parametrize("picked", [True, False], ids=["agent_picked", "auto"])
@pytest.mark.parametrize("uploaded_after_chat", [False, True], ids=["before_chat", "after_chat"])
async def test_agent_library_files_are_listed_readable_and_greppable(
    create_user, login_user, whoami, test_client, create_data_source, create_report,
    picked, uploaded_after_chat,
):
    token, uid, org_id = _admin(create_user, login_user, whoami)
    ds = _agent(create_data_source, token, org_id)
    marker = f"btn_prsd_connect_monitor_{uuid.uuid4().hex[:5]}"

    def upload():
        return _upload_to_agent(test_client, token, org_id, ds["id"], "devices.yaml", _catalogue(marker))

    file_id = None if uploaded_after_chat else upload()
    report = create_report(
        title="chat", user_token=token, org_id=org_id,
        data_sources=[ds["id"]] if picked else [],
    )
    if uploaded_after_chat:
        file_id = upload()

    db, files_block, ctx = await _run_view(report["id"], uid, org_id)
    try:
        # Listed with its id, marked as agent material.
        assert file_id in files_block
        assert 'source="agent"' in files_block
        # Readable by that id.
        read = await _read(file_id, ctx)
        assert _succeeded(read), read
        assert "app_event_0" in (read["output"].get("text") or "")
        # Greppable without naming a connection — the hit is past what one
        # read_file shows the model.
        grep = await _grep(marker, ctx)
        assert _succeeded(grep), grep
        assert marker in grep["observation"]["details"]
    finally:
        await db.close()


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_auto_run_never_exposes_files_of_agents_the_user_cannot_use(
    create_user, login_user, whoami, test_client, create_data_source, create_report,
):
    admin_token, admin_id, org_id = _admin(create_user, login_user, whoami)
    member_token, member_id = _member(test_client, create_user, login_user, whoami, admin_token, org_id)
    # Agents are restricted by default: the admin can use it, the member can't.
    ds = _agent(create_data_source, admin_token, org_id, name="restricted")
    marker = f"secret_event_{uuid.uuid4().hex[:5]}"
    file_id = _upload_to_agent(test_client, admin_token, org_id, ds["id"], "core.yaml", _catalogue(marker, n=5))

    member_report = create_report(title="m", user_token=member_token, org_id=org_id, data_sources=[])
    db, files_block, ctx = await _run_view(member_report["id"], member_id, org_id)
    try:
        assert file_id not in files_block
        assert not _succeeded(await _read(file_id, ctx))
        grep = await _grep(marker, ctx)
        # The pattern is echoed back; what must not appear is a matched line.
        assert marker not in str(grep["observation"].get("details") or "")
        assert not grep["output"].get("matches")
    finally:
        await db.close()

    # Same setup, the admin's Auto run does see it.
    admin_report = create_report(title="a", user_token=admin_token, org_id=org_id, data_sources=[])
    db, files_block, ctx = await _run_view(admin_report["id"], admin_id, org_id)
    try:
        assert file_id in files_block
    finally:
        await db.close()


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_file_removed_from_agent_leaves_auto_runs(
    create_user, login_user, whoami, test_client, create_data_source, create_report,
):
    token, uid, org_id = _admin(create_user, login_user, whoami)
    ds = _agent(create_data_source, token, org_id)
    file_id = _upload_to_agent(test_client, token, org_id, ds["id"], "names.tsv", "name\tdesc\nev_a\tthing\n")
    report = create_report(title="auto", user_token=token, org_id=org_id, data_sources=[])

    db, files_block, _ = await _run_view(report["id"], uid, org_id)
    await db.close()
    assert file_id in files_block

    resp = test_client.delete(f"/api/data_sources/{ds['id']}/files/{file_id}", headers=_h(token, org_id))
    assert resp.status_code in (200, 204), resp.text

    db, files_block, ctx = await _run_view(report["id"], uid, org_id)
    try:
        assert file_id not in files_block
        assert not _succeeded(await _read(file_id, ctx))
    finally:
        await db.close()


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_partial_text_read_points_the_model_at_grep(
    create_user, login_user, whoami, test_client, create_data_source, create_report,
):
    """A text read too long to show whole says it is partial and names the
    grep_files call for this file — with no connection_id, which a file in
    the conversation does not have."""
    token, uid, org_id = _admin(create_user, login_user, whoami)
    ds = _agent(create_data_source, token, org_id)
    file_id = _upload_to_agent(test_client, token, org_id, ds["id"], "devices.yaml", _catalogue("x_event", n=800))
    report = create_report(title="c", user_token=token, org_id=org_id, data_sources=[ds["id"]])

    db, _, ctx = await _run_view(report["id"], uid, org_id)
    try:
        read = await _read(file_id, ctx)
        details = read["observation"]["details"]
        trailer = details[details.rindex("\n[") :]
        assert "grep_files" in trailer and file_id in trailer
        assert "connection_id=\"" not in trailer
    finally:
        await db.close()
