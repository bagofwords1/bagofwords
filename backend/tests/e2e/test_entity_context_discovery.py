"""Which saved entities the planner gets to see for a turn.

- Discovery runs against the agents in play (the run's resolved agents),
  so an Auto report — no attachments — still surfaces the entities saved on
  the agents it executes against.
- A wording that shares no word with any title/description ("rock albums"
  vs "Albums by Genre" once "albums" is gone, or a follow-up like "why not
  the saved query?") falls back to the most recent entities on those agents
  instead of hiding them.
- Entities on agents outside the run never appear.

Run:
    cd backend && uv run pytest tests/e2e/test_entity_context_discovery.py -v
"""
import asyncio
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.dependencies import async_session_maker
from app.models.organization import Organization
from app.models.report import Report
from app.models.user import User
from app.ai.context.builders.entity_context_builder import EntityContextBuilder


def _hdr(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


CODE = "def generate_df(ds_clients, excel_files, params):\n    import pandas as pd\n    return pd.DataFrame({'a': [1]})\n"


def _entity(test_client, token, org_id, title, ds_ids, description=None):
    resp = test_client.post(
        "/api/entities/global",
        json={"type": "model", "title": title, "slug": f"{title.lower().replace(' ', '-')}-{uuid.uuid4().hex[:6]}",
              "description": description, "code": CODE, "data": {}, "status": "published",
              "parameters": [{"name": "genre", "type": "string", "source": "input", "default": None, "required": False}],
              "data_source_ids": ds_ids},
        headers=_hdr(token, org_id),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _titles(org_id, user_id, report_id, text, data_source_ids=None, top_k=5):
    async with async_session_maker() as db:
        org = await db.get(Organization, org_id)
        user = await db.get(User, user_id)
        report = None
        if report_id:
            report = (await db.execute(
                select(Report).options(selectinload(Report.data_sources)).where(Report.id == report_id)
            )).scalars().first()
        section = await EntityContextBuilder(db, org, report, user=user).build_for_turn(
            top_k=top_k, require_source_assoc=True, user_text=text, data_source_ids=data_source_ids,
        )
        return [i.title for i in (section.items if section else [])]


@pytest.fixture
def world(test_client, create_user, login_user, whoami, create_data_source, dynamic_sqlite_db, create_report):
    user = create_user()
    token = login_user(user["email"], user["password"])
    info = whoami(token)
    org_id = info["organizations"][0]["id"]
    admin = {"token": token, "user_id": info["id"]}

    def agent(name):
        return create_data_source(name=name, type="sqlite", config={"database": dynamic_sqlite_db},
                                  credentials={}, user_token=token, org_id=org_id)

    music = agent(f"music_{uuid.uuid4().hex[:4]}")
    other = agent(f"other_{uuid.uuid4().hex[:4]}")
    albums = _entity(test_client, token, org_id, "Albums by Genre", [music["id"]])
    _entity(test_client, token, org_id, "Payroll by Department", [other["id"]])
    attached = create_report(title="attached", user_token=token, org_id=org_id, data_sources=[music["id"]])
    auto = create_report(title="auto", user_token=token, org_id=org_id, data_sources=[])
    return {"admin": admin, "org_id": org_id, "music": music, "other": other, "albums": albums,
            "attached": attached["id"], "auto": auto["id"]}


@pytest.mark.e2e
def test_keyword_match_surfaces_the_entity_on_an_attached_report(world):
    titles = asyncio.run(_titles(world["org_id"], world["admin"]["user_id"], world["attached"], "show me all rock albums"))
    assert titles == ["Albums by Genre"]


@pytest.mark.e2e
def test_auto_report_discovers_entities_on_the_runs_agents(world):
    """No attachments on the report, but the run resolves to the music agent."""
    with_run_agents = asyncio.run(_titles(
        world["org_id"], world["admin"]["user_id"], world["auto"], "show me all rock albums",
        data_source_ids=[world["music"]["id"]],
    ))
    assert with_run_agents == ["Albums by Genre"]
    without = asyncio.run(_titles(world["org_id"], world["admin"]["user_id"], world["auto"], "show me all rock albums"))
    assert without == [], "no agents in play means no entities, never the whole org"


@pytest.mark.e2e
@pytest.mark.parametrize("text", ["which rock records do we have?", "why didnt u use the saved query?"])
def test_no_keyword_overlap_falls_back_to_recent_entities_on_those_agents(world, text):
    titles = asyncio.run(_titles(world["org_id"], world["admin"]["user_id"], world["attached"], text))
    assert titles == ["Albums by Genre"], "the planner must at least see what exists on its agents"


@pytest.mark.e2e
def test_entities_on_agents_outside_the_run_never_appear(world):
    titles = asyncio.run(_titles(world["org_id"], world["admin"]["user_id"], world["attached"], "payroll by department"))
    assert "Payroll by Department" not in titles
