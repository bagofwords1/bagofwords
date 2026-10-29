"""A saved entity attached to SEVERAL agents runs with parameter values on the
agent it is run from, through every path a user reaches it from: the entity
run API (entity page) and the describe_entity tool (chat). Each run builds
clients for that one agent, under the caller's credentials, so its rows are
that agent's alone; a member needs access to that agent, not to all of them.

Run:
    cd backend && uv run pytest tests/e2e/test_entity_multi_agent_params.py -v
"""
import asyncio
import os
import sqlite3
import tempfile
import uuid

import pytest

from app.dependencies import async_session_maker
from app.models.organization import Organization
from app.models.user import User


def _hdr(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


# Reaches every client it is given without naming an agent, and binds the
# parameter in each query: run on one agent, it must see only that agent's.
CODE_TEMPLATE = """
def generate_df(ds_clients, excel_files, params):
    import pandas as pd
    year = params.get("year")
    frames = []
    for key in sorted(ds_clients):
        df = ds_clients[key].execute_query(
            "SELECT store, year, SUM(amount) AS total FROM sales WHERE (:year IS NULL OR year = :year) GROUP BY store, year",
            params={"year": year},
        )
        df["agent"] = key
        frames.append(df)
    return pd.concat(frames, ignore_index=True)
"""
YEAR_SPEC = {"name": "year", "type": "number", "label": "Year", "default": None, "required": False, "source": "input"}


def _sales_db(store):
    fd, path = tempfile.mkstemp(suffix=".sqlite")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE sales (store TEXT, year INTEGER, amount REAL)")
    conn.executemany("INSERT INTO sales VALUES (?, ?, ?)", [
        (store, 2021, 10.0), (store, 2022, 20.0), (store, 2022, 5.0), (store, 2023, 30.0),
    ])
    conn.commit(); conn.close()
    return path


@pytest.fixture
def two_agents(bootstrap_admin, sqlite_data_source, invite_user_to_org, grant_resource):
    admin = bootstrap_admin("admin")
    org_id = admin["org_id"]
    paths = [_sales_db("EU"), _sales_db("US")]
    eu = sqlite_data_source(name=f"eu_{uuid.uuid4().hex[:4]}", user_token=admin["token"], org_id=org_id, database=paths[0])
    us = sqlite_data_source(name=f"us_{uuid.uuid4().hex[:4]}", user_token=admin["token"], org_id=org_id, database=paths[1])
    member = invite_user_to_org(org_id=org_id, admin_token=admin["token"])
    yield {"admin": admin, "org_id": org_id, "eu": eu, "us": us, "member": member, "grant": grant_resource}
    for p in paths:
        try: os.unlink(p)
        except OSError: pass


def _entity(test_client, w):
    resp = test_client.post(
        "/api/entities/global",
        json={
            "type": "model", "title": f"Sales by store {uuid.uuid4().hex[:4]}", "slug": f"sales-{uuid.uuid4().hex[:8]}",
            "code": CODE_TEMPLATE, "data": {}, "status": "published",
            "parameters": [YEAR_SPEC], "data_source_ids": [w["eu"]["id"], w["us"]["id"]],
        },
        headers=_hdr(w["admin"]["token"], w["org_id"]),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert {d["id"] for d in body["data_sources"]} == {w["eu"]["id"], w["us"]["id"]}
    return body


def _agents_in(rows):
    return {r["agent"].split(":")[0] for r in rows}


@pytest.mark.e2e
def test_entity_run_api_binds_values_on_the_agent_it_is_run_from(test_client, two_agents):
    w = two_agents
    ent = _entity(test_client, w)
    year = 2022
    for ds, store in ((w["eu"], "EU"), (w["us"], "US")):
        resp = test_client.post(
            f"/api/entities/{ent['id']}/run",
            json={"params": {"year": year}, "data_source_id": ds["id"]},
            headers=_hdr(w["admin"]["token"], w["org_id"]),
        )
        assert resp.status_code == 200, resp.text
        rows = resp.json()["data"]["rows"]
        assert {r["store"] for r in rows} == {store}, rows
        assert all(int(r["year"]) == year for r in rows)
        assert all(float(r["total"]) == 25.0 for r in rows)
        assert _agents_in(rows) == {ds["name"]}
        assert resp.json()["applied_params"]["year"] == year


@pytest.mark.e2e
def test_describe_entity_binds_values_on_the_agent_asked_for(test_client, two_agents):
    from app.ai.tools.implementations.describe_entity import DescribeEntityTool

    w = two_agents
    ent = _entity(test_client, w)
    year = 2023

    async def run(agent_name):
        async with async_session_maker() as db:
            org = await db.get(Organization, w["org_id"])
            user = await db.get(User, w["admin"]["user_id"])
            settings = await org.get_settings(db)
            events = [e async for e in DescribeEntityTool().run_stream(
                {"name_or_id": ent["id"], "should_create": True, "params": {"year": year}, "agent": agent_name},
                {"db": db, "organization": org, "user": user, "settings": settings},
            )]
            return events[-1].payload["output"]

    for ds, store in ((w["eu"], "EU"), (w["us"], "US")):
        out = asyncio.run(run(ds["name"]))
        assert out["success"], out["errors"]
        rows = out["data"]["rows"]
        assert {r["store"] for r in rows} == {store}, rows
        assert all(int(r["year"]) == year for r in rows)
        assert _agents_in(rows) == {ds["name"]}
        assert out["applied_params"]["year"] == year
        assert out["agent"] == ds["name"]


@pytest.mark.e2e
def test_member_runs_values_on_the_agent_they_can_reach(test_client, two_agents):
    w = two_agents
    ent = _entity(test_client, w)
    h = _hdr(w["member"]["token"], w["org_id"])

    w["grant"](resource_type="data_source", resource_id=w["eu"]["id"], principal_type="user",
               principal_id=w["member"]["user_id"], permissions=["access"], user_token=w["admin"]["token"], org_id=w["org_id"])
    reached = test_client.post(f"/api/entities/{ent['id']}/run", json={"params": {"year": 2021}, "data_source_id": w["eu"]["id"]}, headers=h)
    assert reached.status_code == 200, reached.text
    assert {r["store"] for r in reached.json()["data"]["rows"]} == {"EU"}

    unreached = test_client.post(f"/api/entities/{ent['id']}/run", json={"params": {"year": 2021}, "data_source_id": w["us"]["id"]}, headers=h)
    assert unreached.status_code in (403, 404), unreached.text
