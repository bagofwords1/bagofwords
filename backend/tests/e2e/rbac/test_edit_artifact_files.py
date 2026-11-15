"""create_artifact and edit_artifact attach files, and only ones the run may share.

Attaching a file makes it readable by everyone who can view the artifact, so
both tools apply one rule (_artifact_files). A <BowFile> whose file isn't
attached shows "File not found" while the render still passes, so both refuse
it before anything is saved (file_reference_errors). edit_artifact can also
detach a file (remove_file_ids): viewers can then no longer open it through
any version of the artifact, until a later edit attaches it again.
"""
import json
import asyncio
import struct
import uuid
import zlib

import pytest

from app.ai.tools.implementations.create_artifact import CreateArtifactTool
from app.ai.tools.implementations.edit_artifact import EditArtifactTool
from app.dependencies import async_session_maker
from app.models.organization import Organization
from app.models.report import Report
from app.models.user import User
from tests.fixtures.artifact import seed_artifact

CODE = "function App() { return <div><h1>Assistant</h1></div> }"


def _png() -> bytes:
    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))
    pixel = zlib.compress(b"\x00" + bytes((uuid.uuid4().bytes[0], 40, 40)))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", pixel) + chunk(b"IEND", b""))


def _h(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


@pytest.fixture
def no_render(monkeypatch):
    """Headless render validation is a separate Chromium boundary."""
    async def no_screenshot(self, *_args, **_kwargs):
        return None, []
    monkeypatch.setattr(CreateArtifactTool, "_take_preview_screenshot", no_screenshot)


@pytest.fixture
def cast(create_user, login_user, whoami, invite_user_to_org):
    admin = create_user()
    admin_token = login_user(admin["email"], admin["password"])
    org_id = whoami(admin_token)["organizations"][0]["id"]
    owner = invite_user_to_org(org_id=org_id, admin_token=admin_token)
    other = invite_user_to_org(org_id=org_id, admin_token=admin_token)
    return {"org_id": org_id, "owner": owner, "other": other}


@pytest.fixture
def chatbot(test_client, cast):
    """A conversation owned by a non-admin, and a way to seed its artifact."""
    resp = test_client.post("/api/reports", json={"title": f"r-{uuid.uuid4().hex[:6]}"},
                            headers=_h(cast["owner"]["token"], cast["org_id"]))
    assert resp.status_code == 200, resp.text
    report_id = resp.json()["id"]

    def seed(content):
        async def go():
            async with async_session_maker() as db:
                version = await seed_artifact(db, report_id=report_id, user_id=cast["owner"]["user_id"],
                                              organization_id=cast["org_id"], content=content)
                await db.commit()
                return str(version.id)
        return asyncio.run(go())

    return {"report_id": report_id, "seed": seed}


def _upload(client, token, org_id, *, report_id=None, name="logo.png", body=None, content_type="image/png"):
    data = {"report_id": report_id} if report_id else None
    resp = client.post("/api/files", files={"file": (name, body if body is not None else _png(), content_type)},
                       data=data, headers=_h(token, org_id))
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


def _edit(cast, chatbot, tool_input):
    async def go():
        async with async_session_maker() as db:
            ctx = {
                "db": db,
                "report": await db.get(Report, chatbot["report_id"]),
                "user": await db.get(User, cast["owner"]["user_id"]),
                "organization": await db.get(Organization, cast["org_id"]),
            }
            ends = [e async for e in EditArtifactTool().run_stream(tool_input, ctx) if e.type == "tool.end"]
            assert len(ends) == 1
            return ends[0].payload
    return asyncio.run(go())


def _create(cast, chatbot, tool_input):
    async def go():
        async with async_session_maker() as db:
            ctx = {
                "db": db,
                "report": await db.get(Report, chatbot["report_id"]),
                "user": await db.get(User, cast["owner"]["user_id"]),
                "organization": await db.get(Organization, cast["org_id"]),
            }
            ends = [e async for e in CreateArtifactTool().run_stream(tool_input, ctx) if e.type == "tool.end"]
            return ends[-1].payload
    return asyncio.run(go())


def _add_logo(file_id):
    return [{"find": "<h1>Assistant</h1>", "replace": f'<BowFile id="{file_id}" /><h1>Assistant</h1>'}]


def _versions(client, cast, chatbot):
    resp = client.get(f"/api/artifacts/report/{chatbot['report_id']}",
                      headers=_h(cast["owner"]["token"], cast["org_id"]))
    assert resp.status_code == 200, resp.text
    return resp.json()


def _assert_refused(payload, error_type, client, cast, chatbot):
    assert payload["output"]["success"] is False, payload
    assert payload["observation"]["error"]["type"] == error_type
    assert len(_versions(client, cast, chatbot)) == 1


def _file_ids(client, cast, version_id):
    resp = client.get(f"/api/artifacts/{version_id}", headers=_h(cast["owner"]["token"], cast["org_id"]))
    assert resp.status_code == 200, resp.text
    return {f["id"] for f in resp.json()["content"].get("files") or []}


@pytest.mark.e2e
@pytest.mark.parametrize("name,content_type", [("logo.png", "image/png"), ("guide.pdf", "application/pdf")])
def test_edit_attaches_a_new_file_and_keeps_the_existing_ones(test_client, cast, chatbot, no_render, name, content_type):
    token, org_id = cast["owner"]["token"], cast["org_id"]
    existing = _upload(test_client, token, org_id, report_id=chatbot["report_id"])
    new = _upload(test_client, token, org_id, report_id=chatbot["report_id"], name=name, content_type=content_type,
                  body=None if content_type == "image/png" else b"%PDF-1.4\n%%EOF\n")
    artifact = chatbot["seed"]({"code": CODE, "visualization_ids": [],
                                "files": [{"id": existing, "content_type": "image/png", "filename": "old.png"}]})

    payload = _edit(cast, chatbot, {"artifact_id": artifact, "edits": _add_logo(new), "file_ids": [new, new]})

    assert payload["output"]["success"] is True, payload
    assert _file_ids(test_client, cast, payload["output"]["artifact_id"]) == {existing, new}


@pytest.mark.e2e
def test_an_attached_file_reaches_the_artifacts_viewers(test_client, cast, chatbot, no_render, set_visibility):
    token, org_id = cast["owner"]["token"], cast["org_id"]
    logo = _upload(test_client, token, org_id, report_id=chatbot["report_id"])
    artifact = chatbot["seed"]({"code": CODE, "visualization_ids": []})
    set_visibility(chatbot["report_id"], "artifact", "internal", user_token=token, org_id=org_id)
    other = _h(cast["other"]["token"], org_id)
    assert test_client.get(f"/api/files/{logo}/embed_token", headers=other).status_code != 200

    payload = _edit(cast, chatbot, {"artifact_id": artifact, "edits": _add_logo(logo), "file_ids": [logo]})

    assert payload["output"]["success"] is True, payload
    assert test_client.get(f"/api/files/{logo}/embed_token", headers=other).status_code == 200


@pytest.mark.e2e
def test_edit_cannot_attach_a_file_the_run_cannot_see(test_client, cast, chatbot, no_render):
    private = _upload(test_client, cast["other"]["token"], cast["org_id"])
    artifact = chatbot["seed"]({"code": CODE, "visualization_ids": []})

    for file_id in (private, str(uuid.uuid4())):  # someone else's, and one that doesn't exist: the same answer
        payload = _edit(cast, chatbot, {"artifact_id": artifact, "edits": _add_logo(file_id), "file_ids": [file_id]})
        _assert_refused(payload, "file_not_attachable", test_client, cast, chatbot)

    embed = test_client.get(f"/api/files/{private}/embed_token", headers=_h(cast["owner"]["token"], cast["org_id"]))
    assert embed.status_code != 200


@pytest.mark.e2e
@pytest.mark.parametrize("name,content_type,body", [("data.csv", "text/csv", b"k,v\n1,2\n"),
                                                    ("page.html", "text/html", b"<p>x</p>")])
def test_edit_attaches_only_images_and_pdfs(test_client, cast, chatbot, no_render, name, content_type, body):
    file_id = _upload(test_client, cast["owner"]["token"], cast["org_id"], report_id=chatbot["report_id"],
                      name=name, content_type=content_type, body=body)
    artifact = chatbot["seed"]({"code": CODE, "visualization_ids": []})

    payload = _edit(cast, chatbot, {"artifact_id": artifact, "edits": _add_logo(file_id), "file_ids": [file_id]})

    _assert_refused(payload, "file_not_attachable", test_client, cast, chatbot)


@pytest.mark.e2e
@pytest.mark.parametrize("tag", ['<BowFile id="{}" />', "<BowFile className='h-8' id='{}'/>", '<BowFile id={{"{}"}} />',
                                 "<BowFile fit=\"cover\" id={{`{}`}} />",
                                 # A ">" inside an earlier attribute doesn't end the tag.
                                 '<BowFile onLoad={{() => x}} id="{}" />', '<BowFile alt="a > b" id="{}" />'])
def test_edit_refuses_a_bowfile_for_a_file_it_does_not_attach(test_client, cast, chatbot, no_render, tag):
    logo = _upload(test_client, cast["owner"]["token"], cast["org_id"], report_id=chatbot["report_id"])
    artifact = chatbot["seed"]({"code": CODE, "visualization_ids": []})
    edits = [{"find": "<h1>Assistant</h1>", "replace": tag.format(logo) + "<h1>Assistant</h1>"}]

    _assert_refused(_edit(cast, chatbot, {"artifact_id": artifact, "edits": edits}),
                    "file_not_attached", test_client, cast, chatbot)


@pytest.mark.e2e
@pytest.mark.parametrize("tag", ["<BowFile id={logoId} />", "<BowFile id={`${logoId}`} />",
                                 '<BowFile data-id="not-a-file" id={logoId} />', '<BowFile id={"img-" + logoId} />'])
def test_edit_does_not_refuse_a_bowfile_whose_id_is_computed(test_client, cast, chatbot, no_render, tag):
    code = CODE.replace("function App() {", "function App() { const logoId = window.ARTIFACT_DATA.files[0].id;")
    artifact = chatbot["seed"]({"code": code, "visualization_ids": []})
    edits = [{"find": "<h1>Assistant</h1>", "replace": tag + "<h1>Assistant</h1>"}]

    payload = _edit(cast, chatbot, {"artifact_id": artifact, "edits": edits})

    assert payload["output"]["success"] is True, payload


@pytest.mark.e2e
def test_an_unrelated_edit_is_not_blocked_by_an_older_broken_bowfile(test_client, cast, chatbot, no_render):
    code = CODE.replace("<h1>Assistant</h1>", f'<BowFile id="{uuid.uuid4()}" /><h1>Assistant</h1>')
    artifact = chatbot["seed"]({"code": code, "visualization_ids": []})

    payload = _edit(cast, chatbot, {"artifact_id": artifact, "edits": [{"find": "Assistant", "replace": "Helper"}]})

    assert payload["output"]["success"] is True, payload


@pytest.mark.e2e
def test_edit_attaches_a_file_named_in_another_case(test_client, cast, chatbot, no_render):
    logo = _upload(test_client, cast["owner"]["token"], cast["org_id"], report_id=chatbot["report_id"])
    artifact = chatbot["seed"]({"code": CODE, "visualization_ids": []})

    payload = _edit(cast, chatbot, {"artifact_id": artifact, "edits": _add_logo(logo), "file_ids": [f" {logo.upper()} "]})

    assert payload["output"]["success"] is True, payload
    assert _file_ids(test_client, cast, payload["output"]["artifact_id"]) == {logo}


@pytest.mark.e2e
def test_edit_refuses_a_bowfile_spelled_differently_from_the_attached_id(test_client, cast, chatbot, no_render):
    # The runtime matches <BowFile id> against files[].id exactly; another spelling shows "File not found".
    logo = _upload(test_client, cast["owner"]["token"], cast["org_id"], report_id=chatbot["report_id"])
    artifact = chatbot["seed"]({"code": CODE, "visualization_ids": []})

    payload = _edit(cast, chatbot, {"artifact_id": artifact, "edits": _add_logo(logo.upper()), "file_ids": [logo]})

    _assert_refused(payload, "file_not_attached", test_client, cast, chatbot)


@pytest.mark.e2e
def test_edit_detaches_a_file_whose_tag_it_removes(test_client, cast, chatbot, no_render):
    old = _upload(test_client, cast["owner"]["token"], cast["org_id"], report_id=chatbot["report_id"], name="old.png")
    new = _upload(test_client, cast["owner"]["token"], cast["org_id"], report_id=chatbot["report_id"], name="new.png")
    code = CODE.replace("<h1>Assistant</h1>", f'<BowFile id="{old}" /><h1>Assistant</h1>')
    artifact = chatbot["seed"]({"code": code, "visualization_ids": [],
                                "files": [{"id": old, "content_type": "image/png", "filename": "old.png"}]})

    payload = _edit(cast, chatbot, {"artifact_id": artifact, "file_ids": [new], "remove_file_ids": [old],
                                    "edits": [{"find": f'<BowFile id="{old}" />', "replace": f'<BowFile id="{new}" />'}]})

    assert payload["output"]["success"] is True, payload
    assert _file_ids(test_client, cast, payload["output"]["artifact_id"]) == {new}


@pytest.mark.e2e
def test_edit_refuses_to_detach_a_file_the_code_still_shows(test_client, cast, chatbot, no_render):
    old = _upload(test_client, cast["owner"]["token"], cast["org_id"], report_id=chatbot["report_id"])
    code = CODE.replace("<h1>Assistant</h1>", f'<BowFile id="{old}" /><h1>Assistant</h1>')
    artifact = chatbot["seed"]({"code": code, "visualization_ids": [],
                                "files": [{"id": old, "content_type": "image/png", "filename": "logo.png"}]})

    payload = _edit(cast, chatbot, {"artifact_id": artifact, "remove_file_ids": [old],
                                    "edits": [{"find": "Assistant", "replace": "Helper"}]})

    _assert_refused(payload, "file_not_attached", test_client, cast, chatbot)


@pytest.mark.e2e
def test_create_refuses_a_bowfile_for_a_file_it_does_not_attach(test_client, cast, chatbot, no_render):
    logo = _upload(test_client, cast["owner"]["token"], cast["org_id"], report_id=chatbot["report_id"])
    code = CODE.replace("<h1>Assistant</h1>", f'<BowFile onLoad={{() => x}} id="{logo}" /><h1>Assistant</h1>')

    payload = _create(cast, chatbot, {"prompt": "build", "mode": "page", "code": code, "visualization_ids": []})

    assert not payload["output"].get("success"), payload
    assert "not attached" in json.dumps(payload)
    # A failed create keeps a status="failed" row for debugging; nothing is completed.
    assert [v for v in _versions(test_client, cast, chatbot) if v.get("status") == "completed"] == []


@pytest.mark.e2e
def test_create_refuses_an_unattached_bowfile_without_vendored_libs(test_client, cast, chatbot, monkeypatch):
    """No vendored JS libs means no render check; the file check needs no render."""
    import app.services.artifact_libs as artifact_libs
    monkeypatch.setattr(artifact_libs, "_find_libs_dir", lambda *a, **k: None)
    logo = _upload(test_client, cast["owner"]["token"], cast["org_id"], report_id=chatbot["report_id"])
    code = CODE.replace("<h1>Assistant</h1>", f'<BowFile id="{logo}" /><h1>Assistant</h1>')

    payload = _create(cast, chatbot, {"prompt": "build", "mode": "page", "code": code, "visualization_ids": []})

    assert not payload["output"].get("success"), payload
    assert "not attached" in json.dumps(payload)
    assert [v for v in _versions(test_client, cast, chatbot) if v.get("status") == "completed"] == []


@pytest.mark.e2e
def test_create_attaches_a_bowfile_passed_in_file_ids(test_client, cast, chatbot, no_render):
    logo = _upload(test_client, cast["owner"]["token"], cast["org_id"], report_id=chatbot["report_id"])
    code = CODE.replace("<h1>Assistant</h1>", f'<BowFile id="{logo}" /><h1>Assistant</h1>')

    payload = _create(cast, chatbot, {"prompt": "build", "mode": "page", "code": code, "visualization_ids": [], "file_ids": [logo]})

    assert payload["output"].get("artifact_id"), payload.get("observation")
    assert [v["status"] for v in _versions(test_client, cast, chatbot)] == ["completed"]
    assert _file_ids(test_client, cast, payload["output"]["artifact_id"]) == {logo}


@pytest.mark.e2e
def test_create_attaches_only_images_and_pdfs_like_edit(test_client, cast, chatbot, no_render):
    csv = _upload(test_client, cast["owner"]["token"], cast["org_id"], report_id=chatbot["report_id"],
                  name="data.csv", body=b"k,v\n1,2\n", content_type="text/csv")

    payload = _create(cast, chatbot, {"prompt": "build", "mode": "page", "code": CODE, "visualization_ids": [], "file_ids": [csv]})

    assert not payload["output"].get("success"), payload
    assert payload["observation"]["error"]["type"] == "file_not_attachable"
    # A failed create keeps a status="failed" row for debugging; nothing is completed.
    assert [v for v in _versions(test_client, cast, chatbot) if v.get("status") == "completed"] == []


@pytest.mark.e2e
def test_a_failed_file_lookup_leaves_the_runs_session_usable(test_client, cast, chatbot):
    """A failed lookup must not poison the run's shared session: on Postgres a
    failed statement aborts the whole transaction until it is rolled back."""
    from sqlalchemy import event, select
    from app.ai.tools.implementations._artifact_files import FileLookupFailed, attachable_files

    logo = _upload(test_client, cast["owner"]["token"], cast["org_id"], report_id=chatbot["report_id"])
    title = f"kept-{uuid.uuid4().hex[:6]}"

    # Fault injection at the driver boundary: the database really rejects the
    # file lookup's statement.
    def broken_lookup(conn, cursor, statement, params, context, executemany):
        if "FROM files" in statement:
            return "SELECT no_such_column FROM files", params
        return statement, params

    async def go():
        async with async_session_maker() as db:
            report = await db.get(Report, chatbot["report_id"])
            report.title = title  # earlier, uncommitted work of the same run
            await db.flush()
            engine = db.bind.sync_engine
            event.listen(engine, "before_cursor_execute", broken_lookup, retval=True)
            try:
                with pytest.raises(FileLookupFailed):
                    await attachable_files(db, user=await db.get(User, cast["owner"]["user_id"]), report=report,
                                           organization=await db.get(Organization, cast["org_id"]), file_ids=[logo])
            finally:
                event.remove(engine, "before_cursor_execute", broken_lookup)
            return (await db.execute(select(Report.title).where(Report.id == chatbot["report_id"]))).scalar_one()

    assert asyncio.run(go()) == title


@pytest.mark.e2e
def test_a_detached_file_is_no_longer_shared_through_any_version(test_client, cast, chatbot, no_render, set_visibility):
    """remove_file_ids revokes the share: older versions still list the file,
    but viewers can't open it through them, until a later edit attaches it again."""
    token, org_id = cast["owner"]["token"], cast["org_id"]
    logo = _upload(test_client, token, org_id, report_id=chatbot["report_id"])
    artifact = chatbot["seed"]({"code": CODE, "visualization_ids": []})
    set_visibility(chatbot["report_id"], "artifact", "internal", user_token=token, org_id=org_id)
    other = _h(cast["other"]["token"], org_id)

    def viewer_can_open():
        return test_client.get(f"/api/files/{logo}/embed_token", headers=other).status_code == 200

    shown = _edit(cast, chatbot, {"artifact_id": artifact, "edits": _add_logo(logo), "file_ids": [logo]})
    assert shown["output"]["success"] is True, shown
    assert viewer_can_open()

    removed = _edit(cast, chatbot, {"artifact_id": shown["output"]["artifact_id"], "remove_file_ids": [logo],
                                    "edits": [{"find": f'<BowFile id="{logo}" />', "replace": ""}]})
    assert removed["output"]["success"] is True, removed
    assert _file_ids(test_client, cast, removed["output"]["artifact_id"]) == set()
    assert _file_ids(test_client, cast, shown["output"]["artifact_id"]) == {logo}, "the older version still lists it"
    assert not viewer_can_open(), "but no version shares it any more"

    again = _edit(cast, chatbot, {"artifact_id": removed["output"]["artifact_id"], "edits": _add_logo(logo), "file_ids": [logo]})
    assert again["output"]["success"] is True, again
    assert viewer_can_open(), "attaching it again shares it again"
