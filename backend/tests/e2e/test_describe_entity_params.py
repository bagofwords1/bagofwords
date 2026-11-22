"""describe_entity with parameter VALUES materializes a saved entity without
any code generation: the SAVED code runs with the values, and the tool
output carries what the orchestrator needs to persist a parameterized step
(`parameters`, `applied_params`, the real `code`).

Run:
    cd backend && uv run pytest tests/e2e/test_describe_entity_params.py -v
"""
import asyncio
import uuid

import pytest

from app.dependencies import async_session_maker
from app.models.organization import Organization
from app.models.user import User


def _hdr(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


CODE = """
def generate_df(ds_clients, excel_files, params):
    import pandas as pd
    year = params.get("year")
    rows = [{"year": y, "total": t} for y, t in ((2021, 10), (2022, 20)) if year is None or int(y) == int(year)]
    return pd.DataFrame(rows)
"""
YEAR_SPEC = {"name": "year", "type": "number", "label": "Year", "default": None, "required": False, "source": "input"}


async def _run_tool(org_id, user_id, tool_input):
    from app.ai.tools.implementations.describe_entity import DescribeEntityTool

    async with async_session_maker() as db:
        org = await db.get(Organization, org_id)
        user = await db.get(User, user_id)
        settings = await org.get_settings(db)
        events = []
        async for evt in DescribeEntityTool().run_stream(
            tool_input, {"db": db, "organization": org, "user": user, "settings": settings},
        ):
            events.append(evt)
        return events[-1].payload


@pytest.fixture
def admin(create_user, login_user, whoami):
    user = create_user()
    token = login_user(user["email"], user["password"])
    info = whoami(token)
    return {"token": token, "org_id": info["organizations"][0]["id"], "user_id": info["id"]}


def _create_entity(test_client, admin):
    resp = test_client.post(
        "/api/entities",
        json={
            "type": "model", "title": f"Sales by year {uuid.uuid4().hex[:4]}",
            "slug": f"sales-{uuid.uuid4().hex[:8]}", "code": CODE,
            "data": {"rows": [{"year": 2021, "total": 10}, {"year": 2022, "total": 20}], "columns": [{"field": "year"}, {"field": "total"}]},
            "applied_params": {"year": None},
            "status": "published", "parameters": [YEAR_SPEC], "data_source_ids": [],
        },
        headers=_hdr(admin["token"], admin["org_id"]),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


@pytest.mark.e2e
def test_values_run_materializes_the_entity_for_those_values(test_client, admin):
    ent = _create_entity(test_client, admin)
    year = 2022
    payload = asyncio.run(_run_tool(
        admin["org_id"], admin["user_id"],
        {"name_or_id": ent["title"], "should_create": True, "params": {"year": year}},
    ))
    out, obs = payload["output"], payload["observation"]
    assert out["success"], out["errors"]
    assert [r["year"] for r in out["data"]["rows"]] == [year]
    # The planner reads the same budgeted preview create_data emits: a small
    # result comes through whole, with stats — no second read_query needed.
    assert obs["data_preview"]["rows"] == out["data"]["rows"]
    assert obs["data_preview"]["row_count"] == 1 and obs["data_preview"]["truncated"] is False
    assert "stats" in obs and "data_profile" not in obs
    assert out["data_preview"]["rows"] == out["data"]["rows"]
    # What the orchestrator persists onto the created query/step.
    assert out["code"].strip() == CODE.strip()
    assert [p["name"] for p in out["parameters"]] == ["year"]
    assert out["applied_params"]["year"] == year
    assert obs["applied_params"]["year"] == year
    assert [p["name"] for p in obs["parameters"]] == ["year"]


@pytest.mark.e2e
def test_without_values_the_shared_snapshot_is_served(test_client, admin):
    ent = _create_entity(test_client, admin)
    payload = asyncio.run(_run_tool(
        admin["org_id"], admin["user_id"], {"name_or_id": ent["id"], "should_create": True},
    ))
    out = payload["output"]
    assert out["success"], out["errors"]
    assert len(out["data"]["rows"]) == 2
    assert out["applied_params"] == {"year": None}


@pytest.mark.e2e
def test_bad_values_fail_instead_of_serving_the_wrong_slice(test_client, admin):
    ent = _create_entity(test_client, admin)
    payload = asyncio.run(_run_tool(
        admin["org_id"], admin["user_id"],
        {"name_or_id": ent["id"], "should_create": True, "params": {"quarter": 3}},
    ))
    out = payload["output"]
    assert not out["success"]
    assert not (out.get("data") or {}).get("rows"), "must not fall back to the default snapshot for other values"


# Three rows whatever the values: a plain column sum the observation can pin.
SUMS_CODE = """
def generate_df(ds_clients, excel_files, params):
    import pandas as pd
    return pd.DataFrame({"year": [2024, 2025, 2026], "total": [120, 340, 560]})
"""


@pytest.mark.e2e
def test_observation_carries_sums_for_a_run(test_client, admin):
    import pandas as pd

    from app.ai.persisted_summary import build_tool_context_summary, tool_context_for_replay
    from tests.unit.test_data_preview_sums import _assert_k_clean, _dicts_with_key

    _assert_k_clean(pd.DataFrame({"total": [120, 340, 560]}), ["total"])
    resp = test_client.post(
        "/api/entities",
        json={
            "type": "model", "title": f"Totals by year {uuid.uuid4().hex[:4]}",
            "slug": f"totals-{uuid.uuid4().hex[:8]}", "code": SUMS_CODE,
            "data": {"rows": [{"year": 2024, "total": 120}], "columns": [{"field": "year"}, {"field": "total"}]},
            "applied_params": {"year": None},
            "status": "published", "parameters": [YEAR_SPEC], "data_source_ids": [],
        },
        headers=_hdr(admin["token"], admin["org_id"]),
    )
    assert resp.status_code == 200, resp.text
    payload = asyncio.run(_run_tool(
        admin["org_id"], admin["user_id"],
        {"name_or_id": resp.json()["id"], "should_create": True, "params": {"year": 2025}},
    ))
    out, obs = payload["output"], payload["observation"]
    assert out["success"], out["errors"]

    # 1020 / 560 = 1.82: no total-row signature. year is a calendar column.
    assert obs["data_preview"]["column_sums"] == {
        "note": "sum = sum of all 3 rows", "sum": {"total": 1020},
    }
    # The output (UI, SSE, result_json) keeps the plain preview.
    assert out["data_preview"]["rows"] == obs["data_preview"]["rows"]
    assert _dicts_with_key(out, "column_sums") == []
    # describe_entity persists its observation as its context projection, so
    # prior-turn replay shows the same block.
    replayed = tool_context_for_replay(
        build_tool_context_summary("describe_entity", out, observation=obs)
    )
    assert replayed["data_preview"]["column_sums"] == obs["data_preview"]["column_sums"]
