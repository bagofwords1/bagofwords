"""Steps with a stored full result must serve it, bounded, through the API.

Contract (routes/query.py):
  - GET /queries/{id}/default_step advertises `full_result` when the step's
    complete payload lives in the result store, so clients know the embedded
    preview is not everything.
  - GET /queries/{id}/default_step/data pages the FULL stored result with
    limit/offset (server-capped), and falls back to the preview rows for steps
    that never spilled — the artifact page can rely on one endpoint either way.
  - Step.data itself stays bounded (the preview), so report/artifact payloads
    never balloon with the full dataset.

Run:
    cd backend
    uv run pytest tests/e2e/test_result_file_step_serving.py -v
"""
import asyncio
import os
import uuid

import pandas as pd
import pytest

from app.dependencies import async_session_maker
from app.models.report import Report
from app.models.widget import Widget
from app.models.query import Query
from app.models.step import Step

FULL_ROWS = 4321
PREVIEW_ROWS = 100


def _auth(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


async def _seed_query_with_step(org_id: str, user_id: str, *, spill: bool, store_dir: str):
    """Report -> widget -> query -> success default step. When `spill`, the
    step keeps only a preview in Step.data and the full frame goes to the
    result store (the exact shape create_data produces above the cap)."""
    from app.services.result_store import ResultStore, LocalDirStorage

    suffix = uuid.uuid4().hex[:8]
    os.environ["BOW_RESULT_STORE_PATH"] = store_dir

    full_df = pd.DataFrame(
        {
            "idx": range(FULL_ROWS),
            "service": ["payments" if i % 2 else "auth" for i in range(FULL_ROWS)],
            "msg": [("ERROR boom" if i % 1000 == 0 else f"ok {i}") for i in range(FULL_ROWS)],
        }
    )
    preview_rows = full_df.head(PREVIEW_ROWS).to_dict(orient="records")
    columns = [{"field": c, "headerName": c} for c in full_df.columns]

    async with async_session_maker() as db:
        report = Report(
            title=f"RF {suffix}", slug=f"rf-{suffix}", user_id=user_id,
            organization_id=org_id, status="draft",
        )
        db.add(report)
        await db.flush()
        widget = Widget(title=f"W {suffix}", slug=f"w-{suffix}", report_id=report.id)
        db.add(widget)
        await db.flush()
        query = Query(
            title="Q", report_id=report.id, widget_id=widget.id,
            organization_id=org_id, user_id=user_id,
        )
        db.add(query)
        await db.flush()
        step = Step(
            title="S", slug=f"s-{suffix}", status="success",
            widget_id=widget.id, query_id=query.id,
            code="def generate_df(ds_clients, excel_files): ...",
            data={
                "rows": preview_rows,
                "columns": columns,
                "info": {"total_rows": FULL_ROWS if spill else PREVIEW_ROWS},
            },
            data_model={"type": "table"},
            view={"type": "table"},
        )
        db.add(step)
        await db.flush()
        query.default_step_id = step.id

        if spill:
            svc = ResultStore(storage=LocalDirStorage(root=store_dir))
            spill_result = await svc.spill_dataframe(
                full_df, organization_id=org_id, producer="create_data",
            )
            await svc.persist_handle(
                db,
                spill_result,
                organization_id=org_id,
                report_id=str(report.id),
                step_id=str(step.id),
                query_id=str(query.id),
                commit=False,
            )
        await db.commit()
        return {"query_id": str(query.id), "step_id": str(step.id)}


def _org_and_user_via_api(create_user, login_user, whoami):
    user = create_user()
    token = login_user(user["email"], user["password"])
    me = whoami(token)
    return me["organizations"][0]["id"], me["id"], token


@pytest.mark.e2e
def test_default_step_advertises_and_pages_full_result(
    test_client, create_user, login_user, whoami, tmp_path
):
    org_id, user_id, token = _org_and_user_via_api(create_user, login_user, whoami)
    seeded = asyncio.run(
        _seed_query_with_step(org_id, user_id, spill=True, store_dir=str(tmp_path / "results"))
    )

    # default_step advertises the stored full result but its embedded data
    # stays the bounded preview.
    r = test_client.get(f"/api/queries/{seeded['query_id']}/default_step", headers=_auth(token, org_id))
    assert r.status_code == 200
    step = r.json()["step"]
    assert step["full_result"]["available"] is True
    assert step["full_result"]["row_count"] == FULL_ROWS
    assert step["full_result"]["result_file_id"]
    assert len(step["data"]["rows"]) == PREVIEW_ROWS

    # /data pages the full payload deterministically...
    r = test_client.get(
        f"/api/queries/{seeded['query_id']}/default_step/data?limit=50&offset=200",
        headers=_auth(token, org_id),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["source"] == "full_result"
    assert body["row_count"] == 50
    assert body["total_rows"] == FULL_ROWS
    assert body["truncated"] is True
    assert body["rows"][0]["idx"] == 200

    # ...including beyond the preview boundary (the rows that used to be lost).
    r = test_client.get(
        f"/api/queries/{seeded['query_id']}/default_step/data?limit=10&offset={FULL_ROWS - 10}",
        headers=_auth(token, org_id),
    )
    body = r.json()
    assert body["row_count"] == 10
    assert body["truncated"] is False
    assert body["rows"][-1]["idx"] == FULL_ROWS - 1


@pytest.mark.e2e
def test_default_step_data_falls_back_to_preview_rows(
    test_client, create_user, login_user, whoami, tmp_path
):
    org_id, user_id, token = _org_and_user_via_api(create_user, login_user, whoami)
    seeded = asyncio.run(
        _seed_query_with_step(org_id, user_id, spill=False, store_dir=str(tmp_path / "results"))
    )

    r = test_client.get(f"/api/queries/{seeded['query_id']}/default_step", headers=_auth(token, org_id))
    assert r.status_code == 200
    assert "full_result" not in r.json()["step"]

    r = test_client.get(
        f"/api/queries/{seeded['query_id']}/default_step/data?limit=30&offset=10",
        headers=_auth(token, org_id),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["source"] == "preview_rows"
    assert body["row_count"] == 30
    assert body["total_rows"] == PREVIEW_ROWS
    assert body["rows"][0]["idx"] == 10


@pytest.mark.e2e
def test_default_step_data_requires_authorization(
    test_client, create_user, login_user, whoami, tmp_path
):
    org_id, user_id, token = _org_and_user_via_api(create_user, login_user, whoami)
    seeded = asyncio.run(
        _seed_query_with_step(org_id, user_id, spill=True, store_dir=str(tmp_path / "results"))
    )
    r = test_client.get(f"/api/queries/{seeded['query_id']}/default_step/data")
    assert r.status_code in (401, 403)
