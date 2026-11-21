"""read_resources only searches the metadata of the run's own data sources.

Contract: the tool returns indexed resources (dbt / LookML / docs) solely for
data sources that (a) are attached to the current run, (b) belong to the run's
organization and (c) the running user may access. A data_source_id filter that
names anything else matches nothing, and a run with no data sources finds
none of them.

Resources indexed from an organization-level repository have no data source.
They belong to their organization (their own organization_id, else their
indexing job's) and are returned to every run of that organization, with or
without run data sources — and to no other organization's run.

MetadataResource rows are written by Git indexing; they are seeded directly.
The tool is called through its public run_stream with a real session.
"""
import asyncio
import uuid

import pytest
from sqlalchemy import select

from app.ai.tools.implementations.read_resources import ReadResourcesTool
from app.dependencies import async_session_maker
from app.models.data_source import DataSource
from app.models.metadata_indexing_job import MetadataIndexingJob
from app.models.metadata_resource import MetadataResource
from app.models.organization import Organization
from app.models.user import User

from .agent_surface_fixtures import allow_multiple_orgs, h, sqlite_agent, two_orgs  # noqa: F401


@pytest.fixture
def indexed(two_orgs, sqlite_agent):
    """home: an attached agent, an unattached one and a private one;
    other: one public agent. Each has one indexed model."""
    o = two_orgs
    agents = {
        "attached": sqlite_agent(token=o["home_token"], org_id=o["home_org"], is_public=True),
        "unattached": sqlite_agent(token=o["home_token"], org_id=o["home_org"], is_public=True),
        "private": sqlite_agent(token=o["home_token"], org_id=o["home_org"], is_public=False),
        "foreign": sqlite_agent(token=o["other_token"], org_id=o["other_org"], is_public=True),
    }
    names = {k: f"{k}_model_{uuid.uuid4().hex[:8]}" for k in agents}

    async def _seed():
        async with async_session_maker() as db:
            for key, ds in agents.items():
                db.add(MetadataResource(
                    name=names[key], resource_type="dbt_model", path=f"models/{names[key]}.sql",
                    sql_content=f"select '{key}'", data_source_id=ds["id"], is_active=True,
                ))
            await db.commit()

    asyncio.run(_seed())
    return {**o, "agents": agents, "names": names}


def _search(ctx, *, as_user, run_keys, tool_input=None):
    """Run the tool as `as_user` with the agents `run_keys` attached; return
    the tool's final output dict."""
    async def _run():
        async with async_session_maker() as db:
            org = (await db.execute(
                select(Organization).where(Organization.id == ctx["home_org"]))).unique().scalar_one()
            user = await db.get(User, as_user)
            run_agents = [await db.get(DataSource, ctx["agents"][k]["id"]) for k in run_keys]
            runtime_ctx = {"db": db, "organization": org, "user": user, "run_agents": run_agents}
            output = None
            async for ev in ReadResourcesTool().run_stream(tool_input or {"query": [".*_model_.*"]}, runtime_ctx):
                if ev.type == "tool.end":
                    output = ev.payload["output"]
            return output

    return asyncio.run(_run())


def _found(ctx, output):
    return {k for k, name in ctx["names"].items() if name in (output["resources_excerpt"] or "")}


def test_search_returns_only_the_runs_data_sources(indexed):
    out = _search(indexed, as_user=indexed["home_user_id"], run_keys=["attached"])
    assert _found(indexed, out) == {"attached"}
    assert out["errors"] == []


def test_a_data_source_of_another_organization_is_never_searched(indexed):
    # Even if it were handed to the run, or named explicitly in the filter.
    out = _search(indexed, as_user=indexed["home_user_id"], run_keys=["attached", "foreign"])
    assert _found(indexed, out) == {"attached"}

    out = _search(indexed, as_user=indexed["home_user_id"], run_keys=["attached"],
                  tool_input={"query": [".*_model_.*"], "data_source_id": indexed["agents"]["foreign"]["id"]})
    assert _found(indexed, out) == set()
    assert out["errors"] == []


def test_data_source_filter_outside_the_run_matches_nothing(indexed):
    out = _search(indexed, as_user=indexed["home_user_id"], run_keys=["attached"],
                  tool_input={"query": [".*_model_.*"], "data_source_id": indexed["agents"]["unattached"]["id"]})
    assert _found(indexed, out) == set()

    out = _search(indexed, as_user=indexed["home_user_id"], run_keys=["attached", "unattached"],
                  tool_input={"query": [".*_model_.*"], "data_source_id": indexed["agents"]["unattached"]["id"]})
    assert _found(indexed, out) == {"unattached"}


def test_run_without_data_sources_finds_no_data_source_resources(indexed):
    out = _search(indexed, as_user=indexed["home_user_id"], run_keys=[])
    assert _found(indexed, out) == set()
    assert out["searched_resources_est"] == 0


def test_a_member_does_not_read_metadata_of_an_agent_they_cannot_access(indexed):
    # `other` admin is a plain member of home: public agents yes, the private one no.
    member = indexed["other_user_id"]
    out = _search(indexed, as_user=member, run_keys=["attached", "private"])
    assert _found(indexed, out) == {"attached"}
    # The home admin, who can access it, does.
    out = _search(indexed, as_user=indexed["home_user_id"], run_keys=["attached", "private"])
    assert _found(indexed, out) == {"attached", "private"}


@pytest.fixture
def org_level(indexed):
    """Organization-level resources (no data source) in both organizations:
    one placed by its own organization_id, one only by its indexing job."""
    names = {k: f"{k}_model_{uuid.uuid4().hex[:8]}" for k in
             ("home_own", "home_job", "other_own", "other_job")}

    async def _seed():
        async with async_session_maker() as db:
            for side, org_id in (("home", indexed["home_org"]), ("other", indexed["other_org"])):
                job = MetadataIndexingJob(organization_id=org_id, status="completed")
                db.add(job)
                await db.flush()
                db.add(MetadataResource(name=names[f"{side}_own"], resource_type="dbt_model",
                                        path=f"models/{names[f'{side}_own']}.sql", is_active=True,
                                        organization_id=org_id))
                db.add(MetadataResource(name=names[f"{side}_job"], resource_type="dbt_model",
                                        path=f"models/{names[f'{side}_job']}.sql", is_active=True,
                                        metadata_indexing_job_id=job.id))
            await db.commit()

    asyncio.run(_seed())
    return {**indexed, "names": {**indexed["names"], **names}}


@pytest.mark.parametrize("run_keys,expected", [
    ([], {"home_own", "home_job"}),
    (["attached"], {"attached", "home_own", "home_job"}),
])
def test_organization_level_resources_reach_every_run_of_their_organization(org_level, run_keys, expected):
    # For the admin and for a plain member alike.
    for user in (org_level["home_user_id"], org_level["other_user_id"]):
        out = _search(org_level, as_user=user, run_keys=run_keys)
        assert _found(org_level, out) == expected
        assert out["errors"] == []


def test_data_source_filter_excludes_organization_level_resources(org_level):
    out = _search(org_level, as_user=org_level["home_user_id"], run_keys=["attached"],
                  tool_input={"query": [".*_model_.*"], "data_source_id": org_level["agents"]["attached"]["id"]})
    assert _found(org_level, out) == {"attached"}
