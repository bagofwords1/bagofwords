"""Objects that live on a report follow the report's read rules.

A query (its stored code, saved rows and the viewer-mode "Run" that
re-executes it) and a report's connector file references are readable iff the
parent report is: its owner, a full admin, or anyone the report's
visibility (shared / internal / public) or project admits. Changing a report's
file references is a report edit: owner (or full admin) only.

Holding org-wide ``view_reports`` is not enough on its own: that is every
member's baseline, so it cannot decide WHICH reports a member reaches.
"""
import asyncio
import uuid
from datetime import datetime

import pytest

from app.dependencies import async_session_maker
from app.models.query import Query
from app.models.report import Report
from app.models.step import Step
from app.models.widget import Widget


def _h(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


CODE = """
def generate_df(ds_clients, excel_files, params):
    import pandas as pd
    return pd.DataFrame({"year": [params["Year"]]})
"""

PARAM = {
    "name": "Year", "type": "string", "label": "Year", "default": None,
    "required": False, "source": "input", "identity_binding": None,
    "options": None, "options_source": None, "strict_options": False,
}


async def _seed_query(report_id):
    """The widget/query/step graph an AI-built dashboard produces. There is no
    public CRUD API that creates it, so it is written directly (same as
    test_query_viewer_run_with_data_source.py)."""
    suffix = uuid.uuid4().hex[:8]
    async with async_session_maker() as db:
        report = await db.get(Report, report_id)
        widget = Widget(title=f"W {suffix}", slug=f"w-{suffix}", report_id=report_id)
        db.add(widget)
        await db.flush()
        query = Query(title=f"Q {suffix}", report_id=report_id, widget_id=widget.id,
                      organization_id=report.organization_id, user_id=report.user_id,
                      parameters=[PARAM])
        db.add(query)
        await db.flush()
        step = Step(title="Step", slug=f"s-{suffix}", status="success", widget_id=widget.id,
                    query_id=query.id, code=CODE,
                    data={"rows": [{"year": "2023"}], "columns": [{"field": "year"}]},
                    applied_params={"Year": "2023"}, created_at=datetime.utcnow())
        db.add(step)
        await db.flush()
        query.default_step_id = step.id
        await db.commit()
        return str(query.id)


@pytest.fixture
def report_world(test_client, bootstrap_admin, invite_user_to_org):
    admin = bootstrap_admin("admin")
    org = admin["org_id"]
    owner = invite_user_to_org(org_id=org, admin_token=admin["token"])
    other = invite_user_to_org(org_id=org, admin_token=admin["token"])
    r = test_client.post("/api/reports", json={"title": f"private-{uuid.uuid4().hex[:6]}",
                                               "data_sources": []},
                         headers=_h(owner["token"], org))
    assert r.status_code == 200, r.text
    report_id = r.json()["id"]
    return {"org": org, "admin": admin, "owner": owner, "other": other,
            "report_id": report_id, "query_id": asyncio.run(_seed_query(report_id))}


def _share(test_client, w, share_type, visibility):
    r = test_client.put(f"/api/reports/{w['report_id']}/visibility/{share_type}",
                        json={"visibility": visibility}, headers=_h(w["owner"]["token"], w["org"]))
    assert r.status_code == 200, r.text


def _query_surfaces(test_client, w, token, year):
    """Status codes of every read/run surface of the query, plus the listing."""
    h, qid = _h(token, w["org"]), w["query_id"]
    listed = test_client.get("/api/queries", params={"report_id": w["report_id"]}, headers=h)
    assert listed.status_code == 200, listed.text
    return {
        "get": test_client.get(f"/api/queries/{qid}", headers=h).status_code,
        "default_step": test_client.get(f"/api/queries/{qid}/default_step", headers=h).status_code,
        "run": test_client.post(f"/api/queries/{qid}/run",
                                json={"mode": "viewer", "params": {"Year": year}},
                                headers=h).status_code,
        "listed": qid in {q["id"] for q in listed.json()},
    }


READABLE = {"get": 200, "default_step": 200, "run": 200, "listed": True}
HIDDEN = {"get": 404, "default_step": 404, "run": 404, "listed": False}


@pytest.mark.e2e
def test_private_report_queries_are_hidden_from_other_members(test_client, report_world):
    w = report_world
    assert _query_surfaces(test_client, w, w["other"]["token"], "2031") == HIDDEN
    # Owner and full admin keep every surface.
    assert _query_surfaces(test_client, w, w["owner"]["token"], "2032") == READABLE
    assert _query_surfaces(test_client, w, w["admin"]["token"], "2033") == READABLE


@pytest.mark.e2e
@pytest.mark.parametrize("share_type,visibility", [
    ("artifact", "internal"),      # published dashboard, whole org
    ("artifact", "public"),        # public link
    ("conversation", "internal"),  # shared conversation
])
def test_viewers_of_a_visible_report_can_still_run_its_queries(test_client, report_world, share_type, visibility):
    w = report_world
    _share(test_client, w, share_type, visibility)
    assert _query_surfaces(test_client, w, w["other"]["token"], "2041") == READABLE


@pytest.mark.e2e
def test_query_run_result_reaches_a_viewer_of_a_shared_report(test_client, report_world):
    w = report_world
    _share(test_client, w, "artifact", "internal")
    r = test_client.post(f"/api/queries/{w['query_id']}/run",
                         json={"mode": "viewer", "params": {"Year": "2042"}},
                         headers=_h(w["other"]["token"], w["org"]))
    assert r.status_code == 200, r.text
    assert str(r.json()["data"]["rows"][0]["year"]) == "2042"


# ── File references ─────────────────────────────────────────────────────


def _make_reference(test_client, w, connection_id):
    r = test_client.post(f"/api/reports/{w['report_id']}/file_references",
                         json={"connection_id": connection_id, "external_file_id": f"f-{uuid.uuid4().hex[:6]}",
                               "name": "budget.xlsx"},
                         headers=_h(w["owner"]["token"], w["org"]))
    assert r.status_code == 200, r.text
    return r.json()["id"]


@pytest.fixture
def public_connection(test_client, report_world, create_connection):
    """A connection the owner can reach: it backs a public agent."""
    from pathlib import Path
    chinook = (Path(__file__).resolve().parents[2] / "config" / "chinook.sqlite").resolve()
    if not chinook.exists():
        pytest.skip(f"Missing SQLite fixture at {chinook}")
    w = report_world
    c = create_connection(name=f"files-{uuid.uuid4().hex[:6]}", type="sqlite",
                          config={"database": str(chinook)}, credentials={},
                          user_token=w["admin"]["token"], org_id=w["org"])
    a = test_client.post("/api/data_sources", json={
        "name": f"public-{uuid.uuid4().hex[:6]}", "connection_ids": [c["id"]], "is_public": True,
        "generate_summary": False, "generate_conversation_starters": False, "generate_ai_rules": False,
    }, headers=_h(w["admin"]["token"], w["org"]))
    assert a.status_code == 200, a.text
    return c["id"]


@pytest.mark.e2e
def test_file_references_follow_report_read_and_owner_write(test_client, report_world, public_connection):
    w = report_world
    url = f"/api/reports/{w['report_id']}/file_references"
    other_h, owner_h = _h(w["other"]["token"], w["org"]), _h(w["owner"]["token"], w["org"])

    ref_id = _make_reference(test_client, w, public_connection)

    # Owner reads their references.
    mine = test_client.get(url, headers=owner_h)
    assert mine.status_code == 200, mine.text
    assert {r["id"] for r in mine.json()} == {ref_id}
    assert set(mine.json()[0]) == {"id", "report_id", "connection_id", "external_file_id", "name", "mime"}

    # A member who cannot read the report cannot list, add or delete.
    assert test_client.get(url, headers=other_h).status_code == 404
    add = test_client.post(url, json={"connection_id": public_connection, "external_file_id": "x"},
                           headers=other_h)
    assert add.status_code == 404
    assert test_client.delete(f"/api/file_references/{ref_id}", headers=other_h).status_code == 404

    # Once the report is shared they can read, but writes stay owner-only.
    _share(test_client, w, "conversation", "internal")
    shared = test_client.get(url, headers=other_h)
    assert shared.status_code == 200, shared.text
    assert {r["id"] for r in shared.json()} == {ref_id}
    assert test_client.delete(f"/api/file_references/{ref_id}", headers=other_h).status_code == 403
    add = test_client.post(url, json={"connection_id": public_connection, "external_file_id": "y"},
                           headers=other_h)
    assert add.status_code == 403
    assert {r["id"] for r in test_client.get(url, headers=owner_h).json()} == {ref_id}

    # Owner can still delete their own reference.
    gone = test_client.delete(f"/api/file_references/{ref_id}", headers=owner_h)
    assert gone.status_code == 200, gone.text
    assert test_client.get(url, headers=owner_h).json() == []


@pytest.mark.e2e
def test_full_admin_can_manage_any_reports_file_references(test_client, report_world, public_connection):
    w = report_world
    ref_id = _make_reference(test_client, w, public_connection)
    admin_h = _h(w["admin"]["token"], w["org"])
    listed = test_client.get(f"/api/reports/{w['report_id']}/file_references", headers=admin_h)
    assert listed.status_code == 200 and {r["id"] for r in listed.json()} == {ref_id}
    assert test_client.delete(f"/api/file_references/{ref_id}", headers=admin_h).status_code == 200
