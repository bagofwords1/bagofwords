"""E2E: the pre-built skill catalog.

Installing a catalog entry copies its body into a normal ``kind='skill'``
instruction, so the promise is twofold: the install/uninstall lifecycle behaves
(idempotent, versioned, reversible), and what lands actually reaches the agent
the way a skill is supposed to — advertised in ``<available_skills>`` with its
body withheld until ``read_instruction``.
"""
import uuid
from types import SimpleNamespace

import pytest

from app.ai.skills.catalog import get_prebuilt_skill, list_prebuilt_skills


def _auth(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _new_admin(create_user, login_user, whoami):
    email = f"skillcat_{uuid.uuid4().hex[:8]}@test.com"
    create_user(email=email, password="test123")
    token = login_user(email=email, password="test123")
    me = whoami(token)
    return token, me["organizations"][0]["id"]


def _catalog(test_client, token, org_id):
    resp = test_client.get("/api/instructions/skill-catalog", headers=_auth(token, org_id))
    assert resp.status_code == 200, resp.json()
    return {e["key"]: e for e in resp.json()}


def _install(test_client, token, org_id, key):
    resp = test_client.post(
        f"/api/instructions/skill-catalog/{key}/install", headers=_auth(token, org_id)
    )
    assert resp.status_code == 200, resp.json()
    return resp.json()


def _a_chat_skill_key() -> str:
    """A catalog entry that is offered in chat — unscoped, or scoped to chat."""
    return next(
        s.key for s in list_prebuilt_skills() if not s.modes or "chat" in s.modes
    )


def _a_training_only_skill_key():
    return next((s.key for s in list_prebuilt_skills() if s.modes == ("training",)), None)


@pytest.mark.e2e
def test_catalog_lists_every_prebuilt_skill_as_uninstalled(
    create_user, login_user, whoami, test_client
):
    token, org_id = _new_admin(create_user, login_user, whoami)
    entries = _catalog(test_client, token, org_id)

    assert set(entries) == {s.key for s in list_prebuilt_skills()}
    for entry in entries.values():
        # A fresh org has nothing installed — enabling is a deliberate act.
        assert entry["installed"] is False
        assert entry["instruction_id"] is None
        # The listing carries what the UI renders a card from.
        assert entry["title"] and entry["description"] and entry["version"]


@pytest.mark.e2e
def test_install_creates_a_published_skill_instruction(
    create_user, login_user, whoami, test_client
):
    token, org_id = _new_admin(create_user, login_user, whoami)
    key = _a_chat_skill_key()
    skill = get_prebuilt_skill(key)

    result = _install(test_client, token, org_id, key)
    assert result["installed"] is True
    assert result["installed_version"] == skill.version
    assert result["update_available"] is False
    assert result["is_customized"] is False

    row = test_client.get(
        f"/api/instructions/{result['instruction_id']}", headers=_auth(token, org_id)
    ).json()
    assert row["kind"] == "skill"
    assert row["status"] == "published"
    # Skills are never force-loaded, whatever the caller asked for.
    assert row["load_mode"] == "intelligent"
    assert row["text"].strip() == skill.body.strip()
    assert row["description"] == skill.description


@pytest.mark.e2e
def test_install_is_idempotent(create_user, login_user, whoami, test_client):
    """A double-click must not advertise the same playbook twice."""
    token, org_id = _new_admin(create_user, login_user, whoami)
    key = _a_chat_skill_key()

    first = _install(test_client, token, org_id, key)
    second = _install(test_client, token, org_id, key)
    assert second["instruction_id"] == first["instruction_id"]

    listed = test_client.get(
        "/api/instructions", params={"kind": "skill", "limit": 200},
        headers=_auth(token, org_id),
    ).json()
    rows = listed["items"] if isinstance(listed, dict) else listed
    matching = [r for r in rows if r.get("catalog_key") == key]
    assert len(matching) == 1


@pytest.mark.e2e
def test_uninstall_removes_the_skill_and_is_repeatable(
    create_user, login_user, whoami, test_client
):
    token, org_id = _new_admin(create_user, login_user, whoami)
    key = _a_chat_skill_key()
    installed = _install(test_client, token, org_id, key)

    resp = test_client.delete(
        f"/api/instructions/skill-catalog/{key}", headers=_auth(token, org_id)
    )
    assert resp.status_code == 200, resp.json()
    assert resp.json()["installed"] is False
    assert _catalog(test_client, token, org_id)[key]["installed"] is False

    # The underlying instruction is gone...
    gone = test_client.get(
        f"/api/instructions/{installed['instruction_id']}", headers=_auth(token, org_id)
    )
    assert gone.status_code == 404

    # ...and uninstalling again is a no-op, not an error.
    again = test_client.delete(
        f"/api/instructions/skill-catalog/{key}", headers=_auth(token, org_id)
    )
    assert again.status_code == 200
    assert again.json()["installed"] is False

    # Re-installing after an uninstall works and yields a fresh row.
    reinstalled = _install(test_client, token, org_id, key)
    assert reinstalled["installed"] is True
    assert reinstalled["instruction_id"] != installed["instruction_id"]


@pytest.mark.e2e
def test_unknown_key_is_rejected(create_user, login_user, whoami, test_client):
    token, org_id = _new_admin(create_user, login_user, whoami)
    for method, path in (
        ("post", "/api/instructions/skill-catalog/not-a-skill/install"),
        ("post", "/api/instructions/skill-catalog/not-a-skill/update"),
        ("delete", "/api/instructions/skill-catalog/not-a-skill"),
    ):
        resp = getattr(test_client, method)(path, headers=_auth(token, org_id))
        assert resp.status_code == 404, (method, path, resp.status_code)


@pytest.mark.e2e
def test_editing_an_installed_skill_is_reported_as_customized(
    create_user, login_user, whoami, test_client
):
    """The UI must be able to warn before an update overwrites local edits."""
    token, org_id = _new_admin(create_user, login_user, whoami)
    key = _a_chat_skill_key()
    installed = _install(test_client, token, org_id, key)
    assert _catalog(test_client, token, org_id)[key]["is_customized"] is False

    edited = test_client.put(
        f"/api/instructions/{installed['instruction_id']}",
        json={"text": "Our own house rules for this playbook."},
        headers=_auth(token, org_id),
    )
    assert edited.status_code == 200, edited.json()

    assert _catalog(test_client, token, org_id)[key]["is_customized"] is True


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_update_resyncs_body_and_version(create_user, login_user, whoami, test_client):
    token, org_id = _new_admin(create_user, login_user, whoami)
    key = _a_chat_skill_key()
    skill = get_prebuilt_skill(key)
    installed = _install(test_client, token, org_id, key)

    # Simulate a row installed from an older product version. The API has no
    # way to set the provenance stamp (it is not user-editable content), so
    # this state can only be produced directly.
    from sqlalchemy import select
    from app.dependencies import async_session_maker
    from app.models.instruction import Instruction

    async with async_session_maker() as db:
        row = (await db.execute(
            select(Instruction).where(Instruction.id == installed["instruction_id"])
        )).scalar_one()
        row.catalog_version = "0.1"
        row.text = "stale body from an older release"
        await db.commit()

    stale = _catalog(test_client, token, org_id)[key]
    assert stale["installed_version"] == "0.1"
    assert stale["update_available"] is True

    resp = test_client.post(
        f"/api/instructions/skill-catalog/{key}/update", headers=_auth(token, org_id)
    )
    assert resp.status_code == 200, resp.json()

    after = _catalog(test_client, token, org_id)[key]
    assert after["installed_version"] == skill.version
    assert after["update_available"] is False
    assert after["is_customized"] is False

    row = test_client.get(
        f"/api/instructions/{installed['instruction_id']}", headers=_auth(token, org_id)
    ).json()
    assert row["text"].strip() == skill.body.strip()


@pytest.mark.e2e
def test_update_requires_an_installed_skill(create_user, login_user, whoami, test_client):
    token, org_id = _new_admin(create_user, login_user, whoami)
    resp = test_client.post(
        f"/api/instructions/skill-catalog/{_a_chat_skill_key()}/update",
        headers=_auth(token, org_id),
    )
    assert resp.status_code == 404


# ──────────────────────────────────────────────────────────────────────
# What actually reaches the agent
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_installed_skill_is_advertised_not_force_loaded(
    create_user, login_user, whoami, test_client
):
    """An installed skill must behave exactly like a hand-authored one."""
    from app.dependencies import async_session_maker
    from app.ai.context.builders.instruction_context_builder import InstructionContextBuilder

    token, org_id = _new_admin(create_user, login_user, whoami)
    key = _a_chat_skill_key()
    skill = get_prebuilt_skill(key)
    installed = _install(test_client, token, org_id, key)

    async with async_session_maker() as db:
        builder = InstructionContextBuilder(db, SimpleNamespace(id=org_id), mode="chat")
        section = await builder.build(query=None)

    assert installed["instruction_id"] in {s.id for s in section.skills}
    # Never force-loaded — that is the whole point of a skill.
    assert installed["instruction_id"] not in {i.id for i in section.items}

    rendered = section.render()
    assert "<available_skills>" in rendered
    assert installed["instruction_id"][:8] in rendered
    assert skill.title in rendered
    assert skill.description in rendered
    # The body stays out of the prompt until read_instruction pulls it.
    body_tail = skill.body.strip().splitlines()[-1]
    assert body_tail not in rendered


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_uninstalled_skill_leaves_the_prompt(
    create_user, login_user, whoami, test_client
):
    from app.dependencies import async_session_maker
    from app.ai.context.builders.instruction_context_builder import InstructionContextBuilder

    token, org_id = _new_admin(create_user, login_user, whoami)
    key = _a_chat_skill_key()
    installed = _install(test_client, token, org_id, key)
    test_client.delete(
        f"/api/instructions/skill-catalog/{key}", headers=_auth(token, org_id)
    )

    async with async_session_maker() as db:
        builder = InstructionContextBuilder(db, SimpleNamespace(id=org_id), mode="chat")
        section = await builder.build(query=None)

    assert installed["instruction_id"] not in {s.id for s in section.skills}


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_mode_scoped_skill_is_only_advertised_in_its_mode(
    create_user, login_user, whoami, test_client
):
    """A training-only playbook must not burn a catalog slot on chat requests."""
    from app.dependencies import async_session_maker
    from app.ai.context.builders.instruction_context_builder import InstructionContextBuilder

    training_key = _a_training_only_skill_key()
    if training_key is None:
        pytest.skip("no mode-scoped skill in the catalog")

    token, org_id = _new_admin(create_user, login_user, whoami)
    chat_key = _a_chat_skill_key()
    training_id = _install(test_client, token, org_id, training_key)["instruction_id"]
    chat_id = _install(test_client, token, org_id, chat_key)["instruction_id"]

    async def advertised(mode):
        async with async_session_maker() as db:
            builder = InstructionContextBuilder(db, SimpleNamespace(id=org_id), mode=mode)
            section = await builder.build(query=None)
            return {s.id for s in section.skills}

    in_chat = await advertised("chat")
    in_training = await advertised("training")

    assert training_id not in in_chat
    assert training_id in in_training
    # A chat-scoped (or unscoped) skill stays visible in chat — the filter must
    # not over-reach beyond the entries that actually excluded this mode.
    assert chat_id in in_chat


@pytest.mark.e2e
def test_scoping_an_installed_skill_counts_as_customized(
    create_user, login_user, whoami, test_client
):
    """is_customized must cover everything an update overwrites, not just the
    body — otherwise the UI skips its confirmation and the update silently
    resets the admin's scoping."""
    token, org_id = _new_admin(create_user, login_user, whoami)
    key = _a_chat_skill_key()
    installed = _install(test_client, token, org_id, key)
    assert _catalog(test_client, token, org_id)[key]["is_customized"] is False

    # Narrow the skill to one channel, leaving the text untouched.
    scoped = test_client.put(
        f"/api/instructions/{installed['instruction_id']}",
        json={"applicable_channels": ["slack"]},
        headers=_auth(token, org_id),
    )
    assert scoped.status_code == 200, scoped.json()

    assert _catalog(test_client, token, org_id)[key]["is_customized"] is True

    # And an update restores the shipped scoping, clearing the flag.
    assert test_client.post(
        f"/api/instructions/skill-catalog/{key}/update", headers=_auth(token, org_id)
    ).status_code == 200
    after = _catalog(test_client, token, org_id)[key]
    assert after["is_customized"] is False


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_uninstall_clears_duplicate_rows(create_user, login_user, whoami, test_client):
    """Install is check-then-act with no unique constraint behind it, so two
    concurrent installs can both land. One Disable must clear all of them —
    a leftover row keeps the playbook advertised after it was disabled."""
    from sqlalchemy import select
    from app.dependencies import async_session_maker
    from app.models.instruction import Instruction

    token, org_id = _new_admin(create_user, login_user, whoami)
    key = _a_chat_skill_key()
    first = _install(test_client, token, org_id, key)

    # A second row for the same key is only reachable by racing the check, so
    # produce it directly.
    async with async_session_maker() as db:
        original = (await db.execute(
            select(Instruction).where(Instruction.id == first["instruction_id"])
        )).scalar_one()
        db.add(Instruction(
            text=original.text, title=original.title, description=original.description,
            category=original.category, kind="skill", status="published",
            load_mode="intelligent", organization_id=org_id,
            catalog_key=key, catalog_version=original.catalog_version,
        ))
        await db.commit()

    assert _catalog(test_client, token, org_id)[key]["duplicate_count"] == 1

    assert test_client.delete(
        f"/api/instructions/skill-catalog/{key}", headers=_auth(token, org_id)
    ).status_code == 200

    entry = _catalog(test_client, token, org_id)[key]
    assert entry["installed"] is False
    assert entry["duplicate_count"] == 0

    async with async_session_maker() as db:
        live = (await db.execute(
            select(Instruction).where(
                Instruction.organization_id == org_id,
                Instruction.catalog_key == key,
                Instruction.deleted_at.is_(None),
            )
        )).scalars().all()
    assert live == []


@pytest.mark.e2e
def test_update_resets_local_edits_without_a_version_bump(
    create_user, login_user, whoami, test_client
):
    """Reset path: an edited skill can be put back to the shipped text even
    when no new catalog version exists — otherwise local edits are one-way."""
    token, org_id = _new_admin(create_user, login_user, whoami)
    key = _a_chat_skill_key()
    skill = get_prebuilt_skill(key)
    installed = _install(test_client, token, org_id, key)

    edited = test_client.put(
        f"/api/instructions/{installed['instruction_id']}",
        json={"text": "Our own take on this playbook."},
        headers=_auth(token, org_id),
    )
    assert edited.status_code == 200, edited.json()

    before = _catalog(test_client, token, org_id)[key]
    assert before["is_customized"] is True
    # No version bump — the only reason to re-sync is the local edit itself.
    assert before["update_available"] is False

    assert test_client.post(
        f"/api/instructions/skill-catalog/{key}/update", headers=_auth(token, org_id)
    ).status_code == 200

    after = _catalog(test_client, token, org_id)[key]
    assert after["is_customized"] is False
    row = test_client.get(
        f"/api/instructions/{installed['instruction_id']}", headers=_auth(token, org_id)
    ).json()
    assert row["text"].strip() == skill.body.strip()
