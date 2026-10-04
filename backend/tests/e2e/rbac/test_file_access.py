"""Per-user file access.

`manage_files` is baseline for every org member, so it cannot decide WHICH
files a member reaches. The contract (app/services/file_access_service.py):

  a non-admin sees a file they uploaded, a file in an agent library they can
  access, a default file of a project they can view, a file attached to a
  report whose conversation they can view, or a file embedded in an artifact
  of a report they can view. Writes on a report's files are owner-only.
  Full admins see every file.

Every "cannot" below is also checked for the attach paths (report creation,
project defaults), since attaching a file is what makes it readable through
the target.
"""
import asyncio
import uuid

import pytest

from app.dependencies import async_session_maker
from tests.fixtures.artifact import seed_artifact


def _h(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _upload(client, token, org_id, *, report_id=None, data_source_id=None, body=None):
    data = {}
    if report_id:
        data["report_id"] = report_id
    if data_source_id:
        data["data_source_id"] = data_source_id
    content = body or f"k,v\nsecret,{uuid.uuid4().hex}\n".encode()
    return client.post(
        "/api/files",
        files={"file": (f"f_{uuid.uuid4().hex[:6]}.csv", content, "text/csv")},
        data=data or None,
        headers=_h(token, org_id),
    )


def _ids(resp):
    assert resp.status_code == 200, resp.text
    return {f["id"] for f in resp.json()}


def _report(client, token, org_id, **body):
    resp = client.post("/api/reports", json={"title": f"r-{uuid.uuid4().hex[:6]}", **body},
                       headers=_h(token, org_id))
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


def _can_read(client, token, org_id, file_id):
    """Both read surfaces must agree: bytes and the embed-token mint."""
    content = client.get(f"/api/files/{file_id}/content", headers=_h(token, org_id)).status_code
    embed = client.get(f"/api/files/{file_id}/embed_token", headers=_h(token, org_id)).status_code
    assert content == embed, (content, embed)
    return content == 200


@pytest.fixture
def cast(create_user, login_user, whoami, invite_user_to_org):
    admin = create_user()
    admin_token = login_user(admin["email"], admin["password"])
    org_id = whoami(admin_token)["organizations"][0]["id"]
    owner = invite_user_to_org(org_id=org_id, admin_token=admin_token)
    other = invite_user_to_org(org_id=org_id, admin_token=admin_token)
    return {"org_id": org_id, "admin": admin_token, "owner": owner, "other": other}


@pytest.mark.e2e
def test_member_runs_the_file_api_flow_with_an_api_key(test_client, cast, create_api_key):
    """A non-admin automation: create report → upload → list → read → detach."""
    org_id = cast["org_id"]
    key = create_api_key(cast["owner"]["token"], org_id)["key"]

    report_id = _report(test_client, key, org_id)
    body = f"region,revenue\nnorth,{uuid.uuid4().int % 10**6}\n".encode()
    up = _upload(test_client, key, org_id, report_id=report_id, body=body)
    assert up.status_code == 200, up.text
    file_id = up.json()["id"]

    assert file_id in _ids(test_client.get(f"/api/reports/{report_id}/files", headers=_h(key, org_id)))
    assert file_id in _ids(test_client.get("/api/files", headers=_h(key, org_id)))
    content = test_client.get(f"/api/files/{file_id}/content", headers=_h(key, org_id))
    assert content.status_code == 200 and content.content == body

    # Attach an existing upload to a new report, then detach it.
    report2 = _report(test_client, key, org_id, files=[file_id])
    assert file_id in _ids(test_client.get(f"/api/reports/{report2}/files", headers=_h(key, org_id)))
    detach = test_client.delete(f"/api/reports/{report2}/files/{file_id}", headers=_h(key, org_id))
    assert detach.status_code == 200, detach.text
    assert file_id not in _ids(test_client.get(f"/api/reports/{report2}/files", headers=_h(key, org_id)))


@pytest.mark.e2e
@pytest.mark.parametrize("attached_to_report", [True, False])
def test_member_cannot_reach_another_members_uploads(test_client, cast, attached_to_report):
    org_id, owner, other = cast["org_id"], cast["owner"]["token"], cast["other"]["token"]
    report_id = _report(test_client, owner, org_id) if attached_to_report else None
    file_id = _upload(test_client, owner, org_id, report_id=report_id).json()["id"]

    assert _can_read(test_client, owner, org_id, file_id)
    assert file_id not in _ids(test_client.get("/api/files", headers=_h(other, org_id)))
    assert not _can_read(test_client, other, org_id, file_id)
    if report_id:
        resp = test_client.get(f"/api/reports/{report_id}/files", headers=_h(other, org_id))
        assert resp.status_code == 404


@pytest.mark.e2e
def test_member_cannot_modify_another_members_report_files(test_client, cast):
    org_id, owner, other = cast["org_id"], cast["owner"]["token"], cast["other"]["token"]
    report_id = _report(test_client, owner, org_id)
    file_id = _upload(test_client, owner, org_id, report_id=report_id).json()["id"]

    detach = test_client.delete(f"/api/reports/{report_id}/files/{file_id}", headers=_h(other, org_id))
    assert detach.status_code == 403
    assert _upload(test_client, other, org_id, report_id=report_id).status_code == 404
    assert _upload(test_client, other, org_id, report_id=str(uuid.uuid4())).status_code == 404
    assert file_id in _ids(test_client.get(f"/api/reports/{report_id}/files", headers=_h(owner, org_id)))


@pytest.mark.e2e
def test_member_cannot_attach_a_file_they_cannot_see(test_client, cast, create_project):
    """Attaching makes a file readable through the target, so both attach
    paths must refuse files the caller can't already see."""
    org_id, owner, other = cast["org_id"], cast["owner"]["token"], cast["other"]["token"]
    file_id = _upload(test_client, owner, org_id).json()["id"]

    stolen = _report(test_client, other, org_id, files=[file_id])
    assert file_id not in _ids(test_client.get(f"/api/reports/{stolen}/files", headers=_h(other, org_id)))
    assert not _can_read(test_client, other, org_id, file_id)

    project = create_project(user_token=other, org_id=org_id)
    resp = test_client.put(f"/api/projects/{project['id']}/files", json={"file_ids": [file_id]},
                           headers=_h(other, org_id))
    assert resp.status_code == 404


@pytest.mark.e2e
def test_conversation_share_grants_read_not_write(test_client, cast, invite_user_to_org, set_visibility):
    org_id, owner, other = cast["org_id"], cast["owner"]["token"], cast["other"]
    outsider = invite_user_to_org(org_id=org_id, admin_token=cast["admin"])["token"]
    report_id = _report(test_client, owner, org_id)
    file_id = _upload(test_client, owner, org_id, report_id=report_id).json()["id"]

    set_visibility(report_id, "conversation", "shared", user_token=owner, org_id=org_id,
                   shared_user_ids=[other["user_id"]])
    assert _can_read(test_client, other["token"], org_id, file_id)
    assert file_id in _ids(test_client.get(f"/api/reports/{report_id}/files", headers=_h(other["token"], org_id)))
    assert test_client.delete(f"/api/reports/{report_id}/files/{file_id}",
                              headers=_h(other["token"], org_id)).status_code == 403
    assert not _can_read(test_client, outsider, org_id, file_id)

    set_visibility(report_id, "conversation", "none", user_token=owner, org_id=org_id, shared_user_ids=[])
    assert not _can_read(test_client, other["token"], org_id, file_id)


@pytest.mark.e2e
def test_shared_dashboard_exposes_only_its_embedded_files(test_client, cast, set_visibility):
    """An org-internal dashboard shows the files its artifact embeds, not every
    upload in the conversation behind it."""
    org_id, owner, other = cast["org_id"], cast["owner"], cast["other"]["token"]
    report_id = _report(test_client, owner["token"], org_id)
    chat_only = _upload(test_client, owner["token"], org_id, report_id=report_id).json()["id"]
    embedded = _upload(test_client, owner["token"], org_id, report_id=report_id).json()["id"]

    async def _seed():
        # Artifacts come from the agent run; no public CRUD API creates one.
        async with async_session_maker() as db:
            await seed_artifact(
                db, report_id=report_id, user_id=owner["user_id"], organization_id=org_id,
                content={"code": "<BowFile />", "files": [{"id": embedded, "content_type": "text/csv"}]},
            )
            await db.commit()
    asyncio.run(_seed())

    assert not _can_read(test_client, other, org_id, embedded)
    set_visibility(report_id, "artifact", "internal", user_token=owner["token"], org_id=org_id)
    assert _can_read(test_client, other, org_id, embedded)
    assert not _can_read(test_client, other, org_id, chat_only)


@pytest.mark.e2e
def test_agent_files_follow_agent_access(test_client, cast, sqlite_data_source):
    org_id, admin, other = cast["org_id"], cast["admin"], cast["other"]
    ds = sqlite_data_source(name=f"agent-{uuid.uuid4().hex[:6]}", user_token=admin, org_id=org_id)
    up = test_client.post(f"/api/data_sources/{ds['id']}/files",
                          files={"file": ("kb.txt", b"knowledge", "text/plain")}, headers=_h(admin, org_id))
    assert up.status_code == 200, up.text
    file_id = up.json()["id"]

    assert file_id not in _ids(test_client.get("/api/files", headers=_h(other["token"], org_id)))
    assert not _can_read(test_client, other["token"], org_id, file_id)
    # Uploading into an agent's library is a manage action on that agent.
    assert _upload(test_client, other["token"], org_id, data_source_id=ds["id"]).status_code == 403

    grant = test_client.post(f"/api/data_sources/{ds['id']}/members",
                             json={"principal_type": "user", "principal_id": other["user_id"]},
                             headers=_h(admin, org_id))
    assert grant.status_code == 200, grant.text
    assert file_id in _ids(test_client.get("/api/files", headers=_h(other["token"], org_id)))
    assert _can_read(test_client, other["token"], org_id, file_id)


@pytest.mark.e2e
def test_project_default_files_follow_project_access(test_client, cast, create_project, update_project):
    org_id, owner, other = cast["org_id"], cast["owner"]["token"], cast["other"]["token"]
    file_id = _upload(test_client, owner, org_id).json()["id"]
    project = create_project(user_token=owner, org_id=org_id)
    resp = test_client.put(f"/api/projects/{project['id']}/files", json={"file_ids": [file_id]},
                           headers=_h(owner, org_id))
    assert resp.status_code == 200, resp.text

    assert not _can_read(test_client, other, org_id, file_id)
    update_project(project["id"], user_token=owner, org_id=org_id, access="org")
    assert file_id in _ids(test_client.get("/api/files", headers=_h(other, org_id)))
    assert _can_read(test_client, other, org_id, file_id)


@pytest.mark.e2e
def test_admin_sees_every_file(test_client, cast):
    org_id, owner, admin = cast["org_id"], cast["owner"]["token"], cast["admin"]
    report_id = _report(test_client, owner, org_id)
    file_id = _upload(test_client, owner, org_id, report_id=report_id).json()["id"]

    assert file_id in _ids(test_client.get("/api/files", headers=_h(admin, org_id)))
    assert _can_read(test_client, admin, org_id, file_id)
    assert file_id in _ids(test_client.get(f"/api/reports/{report_id}/files", headers=_h(admin, org_id)))


# ── Agent tools ─────────────────────────────────────────────────────────────
# A file id reaching a tool comes from the model (or a prompt it read), so the
# tools must apply the same rule as the routes to the run's user. Tools run
# for real against the DB; only external connector clients are stubbed.

async def _tool_ctx(db, report_id, user_id):
    from app.models.organization import Organization
    from app.models.report import Report
    from app.models.user import User
    report = await db.get(Report, report_id)
    return {
        "db": db, "report": report,
        "user": await db.get(User, user_id),
        "organization": await db.get(Organization, report.organization_id),
    }


async def _run_tool(tool, tool_input, report_id, user_id):
    async with async_session_maker() as db:
        ctx = await _tool_ctx(db, report_id, user_id)
        events = [e async for e in tool.run_stream(tool_input, ctx)]
    ends = [e for e in events if e.type == "tool.end"]
    assert ends, [e.type for e in events]
    return ends[-1].payload["output"]


@pytest.mark.e2e
def test_doc_tools_embed_only_files_the_run_user_can_see(test_client, cast):
    from app.ai.tools.implementations.create_doc import CreateDocTool
    from app.ai.tools.implementations.edit_doc import EditDocTool

    org_id, owner, other = cast["org_id"], cast["owner"], cast["other"]
    others_file = _upload(test_client, other["token"], org_id).json()["id"]
    report_id = _report(test_client, owner["token"], org_id)
    own_file = _upload(test_client, owner["token"], org_id, report_id=report_id).json()["id"]

    md = f"# Doc\n\n{{{{file:{own_file}}}}}\n\n{{{{file:{others_file}}}}}\n"
    out = asyncio.run(_run_tool(CreateDocTool(), {"title": "D", "markdown": md}, report_id, owner["user_id"]))
    assert out["success"] is True, out

    async def _doc_file_ids():
        from sqlalchemy import select

        from app.models.artifact import ArtifactVersion
        async with async_session_maker() as db:
            row = (await db.execute(
                select(ArtifactVersion).where(ArtifactVersion.report_id == report_id)
                .order_by(ArtifactVersion.created_at.desc())
            )).scalars().first()
            return row.content.get("file_ids")

    assert asyncio.run(_doc_file_ids()) == [own_file]
    # And the other member's file is therefore not reachable through the doc.
    assert not _can_read(test_client, owner["token"], org_id, others_file)

    edited = asyncio.run(_run_tool(
        EditDocTool(), {"doc_id": out["doc_id"], "markdown": f"# Doc v2\n\n{{{{file:{others_file}}}}}\n"},
        report_id, owner["user_id"],
    ))
    assert edited["success"] is True, edited
    assert asyncio.run(_doc_file_ids()) == []


@pytest.mark.e2e
def test_email_attachments_only_files_the_sender_can_see(test_client, cast):
    from app.ai.tools.schemas.send_email import EmailAttachmentSpec
    from app.services.email_send_service import EmailSendService

    org_id, owner, other = cast["org_id"], cast["owner"], cast["other"]
    others_file = _upload(test_client, other["token"], org_id).json()["id"]
    report_id = _report(test_client, owner["token"], org_id)
    own_file = _upload(test_client, owner["token"], org_id, report_id=report_id).json()["id"]

    async def _resolve(file_id, as_user):
        async with async_session_maker() as db:
            ctx = await _tool_ctx(db, report_id, owner["user_id"])
            user = ctx["user"] if as_user else None  # None → notifications act as the report owner
            spec = EmailAttachmentSpec(ref_type="file", ref_id=file_id)
            res, att, _ = await EmailSendService().resolve_attachment(
                spec, db, ctx["report"], ctx["organization"], user=user
            )
            return res.success and att is not None

    for as_user in (True, False):
        assert asyncio.run(_resolve(own_file, as_user)) is True
        assert asyncio.run(_resolve(others_file, as_user)) is False


@pytest.mark.e2e
def test_write_file_copies_only_files_the_run_user_can_see(test_client, cast):
    from unittest.mock import AsyncMock, patch

    from app.ai.tools.implementations.write_file import WriteFileTool
    from app.data_sources.clients.base import Capability

    org_id, owner, other = cast["org_id"], cast["owner"], cast["other"]
    others_file = _upload(test_client, other["token"], org_id).json()["id"]
    report_id = _report(test_client, owner["token"], org_id)
    own_file = _upload(test_client, owner["token"], org_id, report_id=report_id).json()["id"]

    def _copy(file_id):
        client = AsyncMock()
        client.capabilities = {Capability.WRITE_FILE}
        client.awrite_file = AsyncMock(return_value={
            "id": "out/x.csv", "name": "x.csv", "path": "out/x.csv", "mime_type": "text/csv",
            "size": 1, "modified_at": None, "web_url": None,
        })
        with patch("app.ai.tools.implementations.write_file.resolve_file_client",
                   new=AsyncMock(return_value=(client, None))):
            out = asyncio.run(_run_tool(
                WriteFileTool(), {"connection_id": "c", "filename": "x.csv", "source_file_id": file_id},
                report_id, owner["user_id"],
            ))
        return out["success"], client.awrite_file.await_count

    assert _copy(own_file) == (True, 1)
    assert _copy(others_file) == (False, 0)


@pytest.mark.e2e
@pytest.mark.parametrize("key", ["files", "file_ids"])
def test_embedding_a_file_id_does_not_grant_access_to_it(test_client, cast, set_visibility, key):
    """An embed only shares what the report owner can already see. Writing
    another member's file id into your own dashboard (or doc) content — which
    the artifact routes accept verbatim — must not make it readable."""
    org_id, owner, other = cast["org_id"], cast["owner"], cast["other"]
    victim_file = _upload(test_client, other["token"], org_id).json()["id"]
    report_id = _report(test_client, owner["token"], org_id)
    embed = [{"id": victim_file, "content_type": "text/csv"}] if key == "files" else [victim_file]
    resp = test_client.post("/api/artifacts", json={
        "report_id": report_id, "mode": "page", "title": "x", "content": {"code": "", key: embed},
    }, headers=_h(owner["token"], org_id))
    assert resp.status_code == 200, resp.text
    set_visibility(report_id, "artifact", "internal", user_token=owner["token"], org_id=org_id)

    assert not _can_read(test_client, owner["token"], org_id, victim_file)
    assert _can_read(test_client, other["token"], org_id, victim_file)  # still the uploader's
