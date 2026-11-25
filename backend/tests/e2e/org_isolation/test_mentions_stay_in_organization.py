"""A mention only ever resolves to an object of the conversation's organization.

Mention ids are supplied by the client. Contract:
* on write, a mentioned data source / table / entity / instruction that is not
  the organization's is not stored (dropped silently, like an unviewable file);
* on read, a stored mention whose object is not the organization's contributes
  nothing to the prompt — neither to the mentions section nor to the history.

Completions and Mention rows are what the agent loop writes, so they are seeded
in the database; every mentioned object is created through the API.
"""
import asyncio
import uuid
from datetime import datetime

import pytest
from sqlalchemy import select

from app.ai.context.builders.mention_context_builder import MentionContextBuilder
from app.ai.context.builders.message_context_builder import MessageContextBuilder
from app.dependencies import async_session_maker
from app.models.completion import Completion
from app.models.datasource_table import DataSourceTable
from app.models.mention import Mention, MentionType
from app.models.organization import Organization
from app.models.report import Report
from app.models.user import User
from app.services.mention_service import MentionService

from .agent_surface_fixtures import allow_multiple_orgs, h, sqlite_agent, two_orgs  # noqa: F401


@pytest.fixture
def mentionables(test_client, two_orgs, sqlite_agent, create_report, create_global_instruction):
    """One data source, table, entity and instruction in each organization."""
    o = two_orgs

    def _side(token, org_id, tag):
        ds = sqlite_agent(token=token, org_id=org_id, is_public=True)
        entity = test_client.post("/api/entities/global", headers=h(token, org_id), json={
            "type": "model", "title": f"{tag}-entity-{uuid.uuid4().hex[:8]}",
            "slug": f"{tag}-{uuid.uuid4().hex[:8]}", "description": None, "tags": [],
            "code": f"select '{tag}' as marker", "data": {}, "status": "published",
            "data_source_ids": [],
        })
        assert entity.status_code == 200, entity.text
        instruction = create_global_instruction(
            text=f"{tag}-rule-{uuid.uuid4().hex[:8]}", user_token=token, org_id=org_id, status="published",
        )
        return {"ds": ds, "entity": entity.json(), "instruction": instruction}

    home = _side(o["home_token"], o["home_org"], "home")
    other = _side(o["other_token"], o["other_org"], "other")

    async def _tables():
        # A catalog row per agent: schema indexing is a background job, so the
        # row is written directly to have a known table id on each side.
        async with async_session_maker() as db:
            rows = {}
            for key, side in (("home", home), ("other", other)):
                t = DataSourceTable(name=f"{key}_tbl", datasource_id=side["ds"]["id"], is_active=True,
                                    columns=[{"name": "id", "dtype": "Integer"}], pks=[], fks=[])
                db.add(t)
                await db.flush()
                rows[key] = str(t.id)
            await db.commit()
            return rows

    table_ids = asyncio.run(_tables())
    home["table_id"], other["table_id"] = table_ids["home"], table_ids["other"]
    report = create_report(title="mentions", user_token=o["home_token"], org_id=o["home_org"])
    return {**o, "home": home, "other": other, "report_id": report["id"]}


def _groups(*sides):
    return [
        {"name": "DATA SOURCES", "items": [{"id": s["ds"]["id"], "name": s["ds"]["name"]} for s in sides]},
        {"name": "TABLES", "items": [{"id": s["table_id"], "name": "t"} for s in sides]},
        {"name": "ENTITIES", "items": [{"id": s["entity"]["id"], "title": s["entity"]["title"]} for s in sides]},
        {"name": "INSTRUCTIONS", "items": [{"id": s["instruction"]["id"], "name": "rule"} for s in sides]},
    ]


def _ids(side):
    return {side["ds"]["id"], side["table_id"], side["entity"]["id"], side["instruction"]["id"]}


async def _user_turn(db, m, prompt):
    completion = Completion(report_id=m["report_id"], role="user", prompt=prompt, completion={},
                            sigkill=datetime.utcnow(), user_id=m["home_user_id"], status="success")
    db.add(completion)
    await db.commit()
    await db.refresh(completion)
    return completion


def test_only_own_organization_mentions_are_stored(mentionables):
    m = mentionables

    async def _run():
        async with async_session_maker() as db:
            completion = await _user_turn(
                db, m, {"content": "q", "mentions": _groups(m["home"], m["other"])})
            await MentionService().create_completion_mentions(db, completion)
            rows = (await db.execute(
                select(Mention.object_id).where(Mention.completion_id == str(completion.id))
            )).scalars().all()
            return {str(r) for r in rows}

    stored = asyncio.run(_run())
    assert stored == _ids(m["home"])
    assert not stored & _ids(m["other"])


_STORED_TYPES = (
    (MentionType.DATA_SOURCE, lambda s: s["ds"]["id"]),
    (MentionType.TABLE, lambda s: s["table_id"]),
    (MentionType.ENTITY, lambda s: s["entity"]["id"]),
    (MentionType.INSTRUCTION, lambda s: s["instruction"]["id"]),
)


def test_stored_mentions_of_another_organization_never_reach_the_prompt(mentionables):
    m = mentionables

    async def _run():
        async with async_session_maker() as db:
            completion = await _user_turn(db, m, {"content": "what does this mean?"})
            for side in (m["home"], m["other"]):
                for mtype, pick in _STORED_TYPES:
                    db.add(Mention(type=mtype, report_id=m["report_id"], object_id=pick(side),
                                   mention_content="mentioned", completion_id=str(completion.id)))
            await db.commit()

            org = (await db.execute(
                select(Organization).where(Organization.id == m["home_org"]))).unique().scalar_one()
            report = (await db.execute(
                select(Report).where(Report.id == m["report_id"]))).unique().scalar_one()
            user = await db.get(User, m["home_user_id"])

            section = await MentionContextBuilder(db, org, report, completion, user=user).build()
            history = await MessageContextBuilder(db, org, report, user=user).build(
                completion_ids=[str(completion.id)])
            return section, section.render(), history.render()

    section, mentions_text, history_text = asyncio.run(_run())
    home, other = m["home"], m["other"]

    assert [i.id for i in section.data_sources] == [home["ds"]["id"]]
    assert [i.id for i in section.entities] == [home["entity"]["id"]]
    assert [i.id for i in section.instructions] == [home["instruction"]["id"]]
    # The legitimate mentions still carry their content...
    assert home["instruction"]["text"] in mentions_text
    assert home["entity"]["title"] in mentions_text
    assert home["entity"]["title"] in history_text
    assert home["ds"]["name"] in history_text
    # ...and nothing of the other organization's objects is rendered anywhere.
    for text in (mentions_text, history_text):
        assert other["instruction"]["text"] not in text
        assert other["entity"]["title"] not in text
        assert other["ds"]["name"] not in text
        assert "other_tbl" not in text
