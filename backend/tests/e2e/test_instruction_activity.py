"""The instruction changelog, and listing instructions the live build dropped.

A build is a commit: author, source, timestamp, and contents that differ from
the build it forked from. `GET /instructions/activity` reads that back as a feed
with each change's net effect, so a build that removed hundreds of instructions
is one legible row instead of something you go looking for.

`GET /instructions?live=false` is its counterpart on the list: the instructions
the org still holds that the live build isn't carrying. Before it, those rows
were unreachable through the API at all — the list only ever returned what the
live build contained, which is why a set of instructions could stop being used
with nothing able to show it.
"""

import asyncio
import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select, text

from app.models.build_content import BuildContent
from app.models.instruction_build import InstructionBuild


def _run(fn):
    from app.settings.database import create_async_session_factory

    async def _w():
        factory = create_async_session_factory()
        async with factory() as db:
            return await fn(db)
    return asyncio.run(_w())


@pytest.fixture
def org_with_instructions(create_user, login_user, whoami, create_data_source,
                          create_instruction):
    def _make(n=4):
        user = create_user()
        token = login_user(user["email"], user["password"])
        me = whoami(token)
        org_id = str(me["organizations"][0]["id"])
        ds = create_data_source(
            name=f"agent-{uuid.uuid4().hex[:6]}", type="sqlite",
            config={"database": ":memory:"}, credentials={},
            user_token=token, org_id=org_id,
        )
        ids = [
            create_instruction(text=f"rule {i}: reconcile against the closed period",
                               user_token=token, org_id=org_id, status="published",
                               data_source_ids=[ds["id"]])["id"]
            for i in range(n)
        ]
        return {
            "org_id": org_id, "ds_id": ds["id"], "user_id": str(me["id"]),
            "instruction_ids": ids, "count": n,
            "headers": {"Authorization": f"Bearer {token}", "X-Organization-Id": org_id},
        }
    return _make


async def _live_build_id(db, org_id):
    return (await db.execute(
        select(InstructionBuild.id).where(
            InstructionBuild.organization_id == org_id,
            InstructionBuild.is_main == True,  # noqa: E712
            InstructionBuild.deleted_at == None,  # noqa: E711
        )
    )).scalar()


def _drop_from_live(org_id, keep_id):
    """Reproduce a promotion that failed to carry instructions over: the live
    build ends up holding only `keep_id`, every other instruction untouched."""
    async def _do(db):
        live = await _live_build_id(db, org_id)
        await db.execute(text(
            "DELETE FROM build_contents WHERE build_id = :b AND instruction_id != :k"
        ), {"b": live, "k": keep_id})
        await db.commit()
        return live
    return _run(_do)


@pytest.mark.e2e
def test_activity_reports_each_change_with_its_net_effect(test_client, org_with_instructions):
    ctx = org_with_instructions(3)
    r = test_client.get("/api/instructions/activity", headers=ctx["headers"])
    assert r.status_code == 200, r.text[:400]
    body = r.json()

    assert body["total"] >= 1
    assert body["items"], "creating instructions produced no changelog entries"

    # Every entry carries the shape the feed renders.
    for e in body["items"]:
        for field in ("build_id", "build_number", "status", "source", "created_at",
                      "added", "modified", "removed", "actor"):
            assert field in e, f"entry missing {field}"
        assert isinstance(e["added"], int)

    # The instructions were created by a person, so the change is attributed.
    authored = [e for e in body["items"] if e["source"] == "user"]
    assert authored, "no user-sourced entries"
    assert any(e["actor"] and e["actor"]["id"] == ctx["user_id"] for e in authored)

    # Adding instructions shows up as additions, not as edits or removals.
    assert sum(e["added"] for e in body["items"]) >= ctx["count"]


@pytest.mark.e2e
def test_a_change_that_drops_instructions_reads_as_a_removal(test_client, org_with_instructions):
    """The shape of the incident this feed exists to make visible."""
    ctx = org_with_instructions(4)
    keep = ctx["instruction_ids"][-1]

    def _promote_without_carrying_over(db):
        async def _do(db):
            live = await _live_build_id(db, ctx["org_id"])
            new_id = str(uuid.uuid4())
            bn = (await db.execute(text(
                "SELECT COALESCE(MAX(build_number),0)+1 FROM instruction_builds "
                "WHERE organization_id = :o"), {"o": ctx["org_id"]})).scalar()
            db.add(InstructionBuild(
                id=new_id, build_number=bn, status="approved", source="git",
                is_main=False, organization_id=ctx["org_id"], base_build_id=live,
                created_by_user_id=ctx["user_id"], total_instructions=0,
            ))
            await db.flush()
            kept_version = (await db.execute(
                select(BuildContent.instruction_version_id).where(
                    BuildContent.build_id == live, BuildContent.instruction_id == keep)
            )).scalar()
            db.add(BuildContent(build_id=new_id, instruction_id=keep,
                                instruction_version_id=kept_version))
            await db.commit()
            return new_id
        return _run(_do)

    new_build = _promote_without_carrying_over(None)

    body = test_client.get("/api/instructions/activity",
                           headers=ctx["headers"]).json()
    entry = next((e for e in body["items"] if e["build_id"] == new_build), None)
    assert entry is not None, "the change is absent from the changelog"
    assert entry["removed"] == ctx["count"] - 1, entry
    assert entry["added"] == 0

    # Expanding it names what it dropped.
    detail = test_client.get(f"/api/instructions/activity/{new_build}",
                             headers=ctx["headers"])
    assert detail.status_code == 200, detail.text[:300]
    d = detail.json()
    assert d["removed_total"] == ctx["count"] - 1
    assert len(d["removed"]) == ctx["count"] - 1
    assert all(item["label"] for item in d["removed"]), "removals rendered without labels"


@pytest.mark.e2e
def test_activity_filters(test_client, org_with_instructions):
    ctx = org_with_instructions(2)
    h = ctx["headers"]

    by_agent = test_client.get("/api/instructions/activity",
                               params={"agent_ids": ctx["ds_id"]}, headers=h)
    assert by_agent.status_code == 200
    assert by_agent.json()["items"], "agent filter dropped its own agent's changes"

    other_agent = test_client.get("/api/instructions/activity",
                                  params={"agent_ids": str(uuid.uuid4())}, headers=h)
    assert other_agent.json()["items"] == []

    by_person = test_client.get("/api/instructions/activity",
                                params={"user_ids": ctx["user_id"]}, headers=h)
    assert by_person.json()["items"], "person filter dropped their own changes"
    assert test_client.get("/api/instructions/activity",
                           params={"user_ids": str(uuid.uuid4())},
                           headers=h).json()["items"] == []

    by_source = test_client.get("/api/instructions/activity",
                                params={"sources": "user"}, headers=h)
    assert all(e["source"] == "user" for e in by_source.json()["items"])
    assert test_client.get("/api/instructions/activity",
                           params={"sources": "git"}, headers=h).json()["items"] == []

    future = (datetime.utcnow() + timedelta(days=1)).isoformat()
    assert test_client.get("/api/instructions/activity",
                           params={"since": future}, headers=h).json()["items"] == []

    assert test_client.get("/api/instructions/activity",
                           params={"sources": "telepathy"}, headers=h).status_code >= 400


@pytest.mark.e2e
def test_activity_paginates(test_client, org_with_instructions):
    ctx = org_with_instructions(5)
    h = ctx["headers"]
    full = test_client.get("/api/instructions/activity",
                           params={"limit": 100}, headers=h).json()
    if full["total"] < 2:
        pytest.skip("needs at least two changelog entries")
    first = test_client.get("/api/instructions/activity",
                            params={"limit": 1}, headers=h).json()
    second = test_client.get("/api/instructions/activity",
                             params={"limit": 1, "skip": 1}, headers=h).json()
    assert len(first["items"]) == 1 and len(second["items"]) == 1
    assert first["items"][0]["build_id"] != second["items"][0]["build_id"]
    assert first["total"] == full["total"]


@pytest.mark.e2e
@pytest.mark.parametrize("view", ["full", "light"])
def test_live_false_lists_instructions_the_live_build_dropped(test_client,
                                                              org_with_instructions, view):
    ctx = org_with_instructions(4)
    keep = ctx["instruction_ids"][-1]

    def _list(**params):
        q = {"include_own": "true", "include_drafts": "true", "limit": 200, "view": view}
        q.update(params)
        return test_client.get("/api/instructions", params=q, headers=ctx["headers"]).json()

    assert _list()["total"] == ctx["count"]
    assert _list(live="false")["total"] == 0, "nothing should be missing yet"

    _drop_from_live(ctx["org_id"], keep)

    live = _list(live="true")
    dropped = _list(live="false")

    assert live["total"] == 1
    assert [i["id"] for i in live["items"]] == [keep]

    assert dropped["total"] == ctx["count"] - 1, "the dropped rows are still unreachable"
    assert keep not in [i["id"] for i in dropped["items"]]
    # They are intact — this is a visibility state, not deletion.
    assert all(i["id"] in ctx["instruction_ids"] for i in dropped["items"])


@pytest.mark.e2e
def test_live_true_is_the_default(test_client, org_with_instructions):
    """Existing callers keep seeing exactly what the agent uses."""
    ctx = org_with_instructions(3)
    _drop_from_live(ctx["org_id"], ctx["instruction_ids"][0])
    q = {"include_own": "true", "include_drafts": "true", "limit": 200}
    default = test_client.get("/api/instructions", params=q, headers=ctx["headers"]).json()
    explicit = test_client.get("/api/instructions", params={**q, "live": "true"},
                               headers=ctx["headers"]).json()
    assert default["total"] == explicit["total"] == 1

@pytest.mark.e2e
def test_activity_filters_accept_several_values(test_client, org_with_instructions):
    """Filters are multi-select: several values in one facet widen it (OR),
    while separate facets narrow together (AND)."""
    ctx = org_with_instructions(2)
    h = ctx["headers"]
    stranger = str(uuid.uuid4())

    def n(**params):
        return len(test_client.get("/api/instructions/activity",
                                   params=params, headers=h).json()["items"])

    # A real value OR'd with an unrelated one returns the same as the real one
    # alone — not the intersection, and not nothing.
    assert n(agent_ids=f"{ctx['ds_id']},{stranger}") == n(agent_ids=ctx["ds_id"]) > 0
    assert n(user_ids=f"{ctx['user_id']},{stranger}") == n(user_ids=ctx["user_id"]) > 0
    assert n(sources="user,git") == n(sources="user") > 0

    # Across facets it narrows: a real agent with nobody's changes is empty.
    assert n(agent_ids=ctx["ds_id"], user_ids=stranger) == 0

    # Whitespace and empty segments are tolerated rather than treated as a value.
    assert n(sources=" user , ") == n(sources="user")
    assert n(agent_ids="") == n()

    # One bad value in a list is still rejected.
    assert test_client.get("/api/instructions/activity",
                           params={"sources": "user,telepathy"},
                           headers=h).status_code >= 400
