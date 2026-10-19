"""Instructions when cloning an agent: copy, link (share) or leave out.

Contracts:
- Only what the source uses today moves: its instructions live on main, read
  from their approved version. Global instructions are not the agent's.
- copy: new instructions on the clone only; editing a copy leaves the source
  alone; table references point at the clone's tables.
- link: the same instruction on both agents; an edit shows on both; the clone
  gets a reference to its own table next to each table reference.
- Folders and placements are mirrored onto the clone, and the primary
  instruction follows.
- Linking an instruction that is also shared with an agent the cloner doesn't
  manage is refused up front, and nothing is created; copying it is allowed.
  So is linking an instruction with a change waiting in an open build.
"""
import asyncio
import uuid

import pytest

from tests.e2e.rbac.test_agent_clone import _clone, _h, _tables, world  # noqa: F401  (fixture)

pytestmark = pytest.mark.e2e


def _instr(test_client, world, text, ds_ids, refs=(), actor="admin"):
    r = test_client.post("/api/instructions", json={
        "text": text, "title": text[:40], "data_source_ids": list(ds_ids), "references": list(refs),
        "status": "published",
    }, headers=_h(world[actor]["token"], world["org_id"]))
    assert r.status_code == 200, r.text
    return r.json()


def _list(test_client, world, ds_id):
    r = test_client.get("/api/instructions", params={"data_source_ids": ds_id, "include_global": False, "limit": 200},
                        headers=_h(world["admin"]["token"], world["org_id"]))
    assert r.status_code == 200, r.text
    body = r.json()
    rows = body.get("items", body.get("instructions", [])) if isinstance(body, dict) else body
    return {i["id"]: i for i in rows}


def _get(test_client, world, instruction_id):
    r = test_client.get(f"/api/instructions/{instruction_id}", headers=_h(world["admin"]["token"], world["org_id"]))
    assert r.status_code == 200, r.text
    return r.json()


def _edit(test_client, world, instruction_id, text):
    r = test_client.put(f"/api/instructions/{instruction_id}", json={"text": text},
                        headers=_h(world["admin"]["token"], world["org_id"]))
    assert r.status_code == 200, r.text


def _table_ref(world, ds_id, test_client):
    t = _tables(test_client, world, ds_id)[world["target"]]
    return {"object_type": "datasource_table", "object_id": t["id"], "display_text": world["target"]}


@pytest.fixture
def with_instructions(test_client, world):
    w = world
    w["ref_instr"] = _instr(test_client, w, f"Revenue excludes refunds {uuid.uuid4().hex[:4]}", [w["agent"]["id"]],
                            refs=[_table_ref(w, w["agent"]["id"], test_client)])
    w["plain_instr"] = _instr(test_client, w, f"Answer in a short table {uuid.uuid4().hex[:4]}", [w["agent"]["id"]])
    w["global_instr"] = _instr(test_client, w, f"Global rule {uuid.uuid4().hex[:4]}", [])
    return w


def test_copy_gives_the_clone_its_own_instructions(test_client, with_instructions):
    w = with_instructions
    r = _clone(test_client, w, {"instructions": "copy"})
    assert r.status_code == 201, r.text
    clone = r.json()["data_source"]
    assert r.json()["summary"]["instructions"]["copied"] == 2

    on_clone = _list(test_client, w, clone["id"])
    texts = {i["text"] for i in on_clone.values()}
    assert texts == {w["ref_instr"]["text"], w["plain_instr"]["text"]}
    assert set(on_clone).isdisjoint({w["ref_instr"]["id"], w["plain_instr"]["id"]})
    assert w["global_instr"]["text"] not in texts

    copy_id = next(i for i, v in on_clone.items() if v["text"] == w["ref_instr"]["text"])
    copy = _get(test_client, w, copy_id)
    clone_table = _tables(test_client, w, clone["id"])[w["target"]]["id"]
    assert [ref["object_id"] for ref in copy["references"] if ref["object_type"] == "datasource_table"] == [clone_table]
    assert [d["id"] for d in copy["data_sources"]] == [clone["id"]]
    # The row points at its own first version, as after a promote.
    v = test_client.get(f"/api/instructions/{copy_id}/versions/{copy['current_version_id']}",
                        headers=_h(w["admin"]["token"], w["org_id"]))
    assert v.status_code == 200, v.text
    assert v.json()["version_number"] == 1 and v.json()["data_source_ids"] == [clone["id"]]

    # Independent from now on.
    _edit(test_client, w, copy_id, "Changed on the clone only")
    assert _get(test_client, w, w["ref_instr"]["id"])["text"] == w["ref_instr"]["text"]


def _main_base_version(instruction_id):
    """What main's change to the instruction was written against (the
    reviewer's diff baseline). Not exposed by the API."""
    from sqlalchemy import select

    from app.dependencies import async_session_maker
    from app.models.build_content import BuildContent
    from app.models.instruction_build import InstructionBuild

    async def _read():
        async with async_session_maker() as db:
            return (await db.execute(
                select(BuildContent.base_version_id)
                .join(InstructionBuild, InstructionBuild.id == BuildContent.build_id)
                .where(InstructionBuild.is_main.is_(True), BuildContent.instruction_id == instruction_id)
            )).scalar()
    return asyncio.run(_read())


def test_link_shares_the_same_instructions_with_the_clone(test_client, with_instructions):
    w = with_instructions
    h = _h(w["admin"]["token"], w["org_id"])
    old_version_id = _get(test_client, w, w["ref_instr"]["id"])["current_version_id"]
    old_number = test_client.get(f"/api/instructions/{w['ref_instr']['id']}/versions/{old_version_id}",
                                 headers=h).json()["version_number"]
    r = _clone(test_client, w, {"instructions": "link"})
    assert r.status_code == 201, r.text
    clone = r.json()["data_source"]
    assert r.json()["summary"]["instructions"]["linked"] == 2

    on_clone = _list(test_client, w, clone["id"])
    assert {w["ref_instr"]["id"], w["plain_instr"]["id"]} <= set(on_clone)
    shared = _get(test_client, w, w["ref_instr"]["id"])
    assert {d["id"] for d in shared["data_sources"]} == {w["agent"]["id"], clone["id"]}
    table_refs = {ref["object_id"] for ref in shared["references"] if ref["object_type"] == "datasource_table"}
    assert table_refs == {_tables(test_client, w, w["agent"]["id"])[w["target"]]["id"],
                          _tables(test_client, w, clone["id"])[w["target"]]["id"]}

    # The live version (what the agent context reads) names the clone and its table.
    v = test_client.get(f"/api/instructions/{shared['id']}/versions/{shared['current_version_id']}",
                        headers=_h(w["admin"]["token"], w["org_id"]))
    assert v.status_code == 200, v.text
    assert clone["id"] in v.json()["data_source_ids"]
    assert v.json()["version_number"] == old_number + 1
    assert _main_base_version(shared["id"]) == old_version_id
    assert _tables(test_client, w, clone["id"])[w["target"]]["id"] in {
        ref["object_id"] for ref in v.json()["references_json"] or []}

    _edit(test_client, w, w["ref_instr"]["id"], "Edited once, seen by both")
    assert _list(test_client, w, clone["id"])[w["ref_instr"]["id"]]["text"] == "Edited once, seen by both"


def test_none_leaves_the_clone_without_the_source_instructions(test_client, with_instructions):
    w = with_instructions
    clone = _clone(test_client, w, {"instructions": "none"}).json()["data_source"]
    assert _list(test_client, w, clone["id"]) == {}
    assert w["ref_instr"]["id"] in _list(test_client, w, w["agent"]["id"])


def test_an_unapproved_edit_does_not_travel_with_the_copy(test_client, with_instructions):
    """The copy takes the approved (main) version. Direct write: the row-level
    pending edit is what a suggester's change leaves behind, which the API only
    produces through an LLM-driven review flow."""
    from sqlalchemy import update

    from app.dependencies import async_session_maker
    from app.models.instruction import Instruction

    w = with_instructions

    async def _pending():
        async with async_session_maker() as db:
            await db.execute(update(Instruction).where(Instruction.id == w["plain_instr"]["id"])
                             .values(text="NOT APPROVED YET"))
            await db.commit()
    asyncio.run(_pending())
    clone = _clone(test_client, w, {"instructions": "copy"}).json()["data_source"]
    texts = {i["text"] for i in _list(test_client, w, clone["id"]).values()}
    assert w["plain_instr"]["text"] in texts and "NOT APPROVED YET" not in texts


def test_linking_syncs_the_row_to_the_live_version_like_a_promote(test_client, with_instructions):
    """Link gives the instruction a new live version; the row follows it, as
    promote_build does, so readers of the row and of the version agree."""
    from sqlalchemy import update

    from app.dependencies import async_session_maker
    from app.models.instruction import Instruction

    w = with_instructions

    async def _drift():
        async with async_session_maker() as db:
            await db.execute(update(Instruction).where(Instruction.id == w["plain_instr"]["id"])
                             .values(text="ROW ONLY", title="ROW ONLY"))
            await db.commit()
    asyncio.run(_drift())
    assert _clone(test_client, w, {"instructions": "link"}).status_code == 201
    row = _get(test_client, w, w["plain_instr"]["id"])
    v = test_client.get(f"/api/instructions/{row['id']}/versions/{row['current_version_id']}",
                        headers=_h(w["admin"]["token"], w["org_id"])).json()
    assert row["text"] == v["text"] == w["plain_instr"]["text"]
    assert row["title"] == v["title"] == w["plain_instr"]["title"]


def test_folders_and_the_primary_instruction_follow_the_clone(test_client, with_instructions):
    w = with_instructions
    h = _h(w["admin"]["token"], w["org_id"])
    parent = test_client.post("/api/instructions/directories", json={"name": "Finance", "data_source_id": w["agent"]["id"]},
                              headers=h).json()
    child = test_client.post("/api/instructions/directories",
                             json={"name": "Refunds", "data_source_id": w["agent"]["id"], "parent_id": parent["id"]},
                             headers=h).json()
    r = test_client.put(f"/api/instructions/{w['ref_instr']['id']}/directory", json={"directory_id": child["id"]}, headers=h)
    assert r.status_code == 200, r.text
    r = test_client.put(f"/api/data_sources/{w['agent']['id']}", json={"primary_instruction_id": w["plain_instr"]["id"]}, headers=h)
    assert r.status_code == 200, r.text

    for mode in ("copy", "link"):
        clone = _clone(test_client, w, {"instructions": mode}).json()["data_source"]
        tree = test_client.get("/api/instructions/directories", params={"data_source_id": clone["id"]}, headers=h).json()
        dirs = {d["name"]: d for d in tree["directories"]}
        assert set(dirs) == {"Finance", "Refunds"} and dirs["Refunds"]["parent_id"] == dirs["Finance"]["id"]
        placed = {p["instruction_id"] for p in tree["placements"] if p["directory_id"] == dirs["Refunds"]["id"]}
        on_clone = _list(test_client, w, clone["id"])
        expected = next(i for i, v in on_clone.items() if v["text"] == w["ref_instr"]["text"])
        assert placed == {expected}
        primary = test_client.get(f"/api/data_sources/{clone['id']}", headers=h).json()["primary_instruction_id"]
        assert on_clone[primary]["text"] == w["plain_instr"]["text"]
        # The source's folders are untouched.
        src_tree = test_client.get("/api/instructions/directories", params={"data_source_id": w["agent"]["id"]}, headers=h).json()
        assert {d["id"] for d in src_tree["directories"]} == {parent["id"], child["id"]}


def test_linking_an_instruction_shared_with_an_agent_you_dont_manage_is_refused(
    test_client, with_instructions, grant_resource, sqlite_data_source,
):
    w = with_instructions
    other = sqlite_data_source(name=f"Legal {uuid.uuid4().hex[:4]}", user_token=w["admin"]["token"], org_id=w["org_id"])
    shared = _instr(test_client, w, f"Shared with legal {uuid.uuid4().hex[:4]}", [w["agent"]["id"], other["id"]])
    # The manager may clone (manage on the agent + build on its connections)…
    for conn in (w["sql"], w["api"]):
        r = grant_resource(resource_type="connection", resource_id=conn, principal_type="user",
                           principal_id=w["manager"]["user_id"], permissions=["create_data_sources"],
                           user_token=w["admin"]["token"], org_id=w["org_id"])
        assert r.status_code in (200, 201), r.text
    before = test_client.get("/api/data_sources", headers=_h(w["admin"]["token"], w["org_id"])).json()

    # …but does not manage Legal, which also uses one of the instructions.
    r = _clone(test_client, w, {"instructions": "link"}, actor="manager")
    assert r.status_code == 403, r.text
    assert shared["title"] in str(r.json())
    after = test_client.get("/api/data_sources", headers=_h(w["admin"]["token"], w["org_id"])).json()
    assert {d["id"] for d in after} == {d["id"] for d in before}
    assert {d["id"] for d in _get(test_client, w, shared["id"])["data_sources"]} == {w["agent"]["id"], other["id"]}

    r = _clone(test_client, w, {"instructions": "copy"}, actor="manager")
    assert r.status_code == 201, r.text
    assert r.json()["summary"]["instructions"]["copied"] == 3


@pytest.mark.parametrize("submitted", [False, True])
def test_linking_an_instruction_with_a_change_waiting_in_a_build_is_refused(test_client, with_instructions, submitted):
    """Linking gives the instruction a new version; a waiting change was made on
    the old one and would drop the clone when promoted. Refused up front, by
    name, and nothing is created; copying is still allowed."""
    w = with_instructions
    h = _h(w["admin"]["token"], w["org_id"])
    build = test_client.post("/api/builds", json={"source": "user"}, headers=h)
    assert build.status_code == 200, build.text
    build_id = build.json()["id"]
    r = test_client.put(f"/api/instructions/{w['plain_instr']['id']}",
                        json={"text": "Answer in a long table", "target_build_id": build_id}, headers=h)
    assert r.status_code == 200, r.text
    if submitted:
        assert test_client.post(f"/api/builds/{build_id}/submit", headers=h).status_code == 200
    before = {d["id"] for d in test_client.get("/api/data_sources", headers=h).json()}

    r = _clone(test_client, w, {"instructions": "link"})
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["code"] == "instructions_pending"
    assert detail["instructions"] == [w["plain_instr"]["title"]]
    assert {d["id"] for d in test_client.get("/api/data_sources", headers=h).json()} == before

    r = _clone(test_client, w, {"instructions": "copy"})
    assert r.status_code == 201, r.text
