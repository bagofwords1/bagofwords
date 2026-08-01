"""E2E tests: instruction directories (cosmetic per-agent folders).

Directories organize instructions in the KnowledgeExplorer tree. They carry no
AI semantics beyond a light topical hint: an instruction's folder path is
surfaced on its <instruction> element as a `path` attribute, but placement never
changes which instructions load or their text. Deleting a folder re-parents its
children and drops placements — instructions are never deleted.
"""
import uuid
from types import SimpleNamespace

import pytest


def _auth(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _new_admin(create_user, login_user, whoami):
    email = f"dir_{uuid.uuid4().hex[:6]}@test.com"
    create_user(email=email, password="test123")
    token = login_user(email=email, password="test123")
    me = whoami(token)
    return token, me["organizations"][0]["id"]


def _create_instruction(test_client, token, org_id, **fields):
    resp = test_client.post(
        "/api/instructions/global",
        json={"status": "published", "load_mode": "always", **fields},
        headers=_auth(token, org_id),
    )
    assert resp.status_code == 200, resp.json()
    return resp.json()


def _mkdir(test_client, token, org_id, name, parent_id=None):
    resp = test_client.post(
        "/api/instructions/directories",
        json={"name": name, "data_source_id": None, "parent_id": parent_id},
        headers=_auth(token, org_id),
    )
    assert resp.status_code == 200, resp.json()
    return resp.json()


def _place(test_client, token, org_id, instruction_id, directory_id):
    resp = test_client.put(
        f"/api/instructions/{instruction_id}/directory",
        json={"directory_id": directory_id, "data_source_id": None},
        headers=_auth(token, org_id),
    )
    assert resp.status_code == 200, resp.json()
    return resp.json()


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_nested_folder_path_renders_on_instruction(create_user, login_user, whoami, test_client):
    """An instruction filed in a nested folder gets a full `path` attribute
    ("Finance/Definitions") on its rendered <instruction>; an unfiled one does not."""
    from app.dependencies import async_session_maker
    from app.ai.context.builders.instruction_context_builder import InstructionContextBuilder

    token, org_id = _new_admin(create_user, login_user, whoami)
    filed = _create_instruction(test_client, token, org_id, text="Revenue = gross minus refunds.", title="Revenue")
    unfiled = _create_instruction(test_client, token, org_id, text="Report amounts in USD.", title="Currency")

    finance = _mkdir(test_client, token, org_id, "Finance")
    definitions = _mkdir(test_client, token, org_id, "Definitions", parent_id=finance["id"])
    _place(test_client, token, org_id, filed["id"], definitions["id"])

    async with async_session_maker() as db:
        builder = InstructionContextBuilder(db, SimpleNamespace(id=org_id))
        section = await builder.build(query=None)

    by_id = {it.id: it for it in section.items}
    assert by_id[filed["id"]].path == "Finance/Definitions"
    assert by_id[unfiled["id"]].path is None

    rendered = section.render()
    assert 'path="Finance/Definitions"' in rendered
    # The unfiled instruction's element carries no path attribute.
    assert 'title="Currency"' in rendered


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_delete_folder_reparents_children_and_frees_instructions(create_user, login_user, whoami, test_client):
    """Deleting a folder re-parents its subfolders up one level and drops its
    placements (instructions fall back to root) — nothing is deleted."""
    token, org_id = _new_admin(create_user, login_user, whoami)
    inst = _create_instruction(test_client, token, org_id, text="Fiscal year starts in February.", title="FY")

    finance = _mkdir(test_client, token, org_id, "Finance")
    definitions = _mkdir(test_client, token, org_id, "Definitions", parent_id=finance["id"])
    _place(test_client, token, org_id, inst["id"], definitions["id"])

    # Delete the parent folder.
    resp = test_client.delete(
        f"/api/instructions/directories/{finance['id']}", headers=_auth(token, org_id)
    )
    assert resp.status_code == 200, resp.json()

    tree = test_client.get(
        "/api/instructions/directories", headers=_auth(token, org_id)
    ).json()
    names = {d["name"]: d for d in tree["directories"]}
    # Finance is gone; Definitions survived and re-parented to the root.
    assert "Finance" not in names
    assert "Definitions" in names
    assert names["Definitions"]["parent_id"] in (None, "")
    # Its placement (in Definitions) is untouched — the instruction still exists.
    assert any(p["instruction_id"] == inst["id"] for p in tree["placements"])

    # And the instruction itself was not deleted.
    got = test_client.get(f"/api/instructions/{inst['id']}", headers=_auth(token, org_id))
    assert got.status_code == 200


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_move_replaces_placement_one_per_scope(create_user, login_user, whoami, test_client):
    """Placement is one-per-scope: moving an instruction to another folder
    replaces its prior placement rather than accumulating."""
    token, org_id = _new_admin(create_user, login_user, whoami)
    inst = _create_instruction(test_client, token, org_id, text="Be concise.", title="Tone")

    a = _mkdir(test_client, token, org_id, "Alpha")
    b = _mkdir(test_client, token, org_id, "Beta")
    _place(test_client, token, org_id, inst["id"], a["id"])
    tree = _place(test_client, token, org_id, inst["id"], b["id"])

    places = [p for p in tree["placements"] if p["instruction_id"] == inst["id"]]
    assert len(places) == 1
    assert places[0]["directory_id"] == b["id"]
