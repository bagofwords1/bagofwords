"""
Reproduction: scheduled/refresh reruns of an "Auto"-agent report fail with a
KeyError and still report success.

PromptBoxV2 stores the Auto agent selector as an EMPTY agent list — the report
row gets no report_data_source associations. Interactive runs treat that as a
mode ("resolve the running user's accessible agents at run time", see
agent_focus_common.resolve_run_agents), so saved step code happily references
`ds_clients["<Agent>:<Connection>"]` for an agent that was never attached.

Report reruns (rerun_report_steps / viewer_rerun_report_steps in
report_service.py) instead iterate `report.data_sources` directly. Under Auto
that list is empty, so `db_clients == {}`, every step KeyErrors on its client,
yet the endpoint returns HTTP 200 and advances report.last_run_at — the shared
artifact shows stale data with a fresh timestamp.

This test asserts the DESIRED behavior (rerun resolves Auto the same way the
interactive path does), so it FAILS while the bug is present.

Run:
    cd backend
    uv run pytest tests/e2e/test_repro_auto_agent_rerun.py -v
"""
import asyncio
import uuid

import pytest

from app.dependencies import async_session_maker
from app.models.artifact import Artifact
from app.models.organization import Organization
from app.models.query import Query
from app.models.report import Report
from app.models.step import Step
from app.models.user import User
from app.models.visualization import Visualization
from app.models.widget import Widget


def _run(coro):
    return asyncio.run(coro)


async def _interactive_client_keys(report_id: str):
    """What the INTERACTIVE path resolves for this report — Auto resolves to
    the running user's accessible agents and builds their clients."""
    from app.ai.tools.implementations.agent_focus_common import (
        prepare_run_agents, report_selection_is_auto,
    )
    async with async_session_maker() as db:
        report = await db.get(Report, report_id)
        org = await db.get(Organization, str(report.organization_id))
        user = await db.get(User, str(report.user_id))
        assert report_selection_is_auto(report), "expected an Auto (unattached) report"
        agents, clients = await prepare_run_agents(db, org, user, report)
        return [a.name for a in agents], sorted(clients.keys())


async def _seed_artifact_step(report_id: str, step_code: str):
    """One-query artifact dashboard whose default step runs `step_code`
    (mirrors tests/e2e/test_report_rerun_artifact.py seeding)."""
    suffix = uuid.uuid4().hex[:8]
    async with async_session_maker() as db:
        report = await db.get(Report, report_id)
        org_id, user_id = report.organization_id, report.user_id

        widget = Widget(title=f"W {suffix}", slug=f"w-{suffix}", report_id=report_id)
        db.add(widget)
        await db.flush()

        query = Query(title="Q", report_id=report_id, widget_id=widget.id,
                      organization_id=org_id, user_id=user_id)
        db.add(query)
        await db.flush()

        step = Step(title="Default", slug=f"default-{suffix}", status="success",
                    widget_id=widget.id, query_id=query.id, code=step_code, data=None)
        db.add(step)
        await db.flush()
        query.default_step_id = step.id

        viz = Visualization(title="Viz", status="success", report_id=report_id,
                            query_id=query.id, view={"type": "bar_chart"})
        db.add(viz)
        await db.flush()

        db.add(Artifact(report_id=report_id, user_id=user_id, organization_id=org_id,
                        title="Dashboard", mode="page", version=1, status="completed",
                        content={"code": "function App() {}",
                                 "visualization_ids": [str(viz.id)]}))
        await db.commit()
        return {"query_id": str(query.id), "step_id": str(step.id)}


async def _read_step(step_id: str):
    async with async_session_maker() as db:
        step = await db.get(Step, step_id)
        return {"status": step.status, "data": step.data}


@pytest.mark.e2e
def test_rerun_resolves_auto_agents_like_the_interactive_path(
    create_report, create_user, login_user, whoami, rerun_report, get_report,
    install_demo_data_source,
):
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    # The org has an agent the user can access...
    install_demo_data_source(demo_id="chinook", user_token=token, org_id=org_id)

    # ...but the report is created with the Auto selector: data_sources=[]
    # (exactly what PromptBoxV2 submits for Auto).
    report = create_report(title="Auto report", user_token=token, org_id=org_id,
                           data_sources=[])

    # Interactive resolution finds the agent and builds its clients — this is
    # the context the saved step code was generated in.
    agent_names, client_keys = _run(_interactive_client_keys(report["id"]))
    assert agent_names and client_keys, (
        "sanity: interactive Auto resolution should find the demo agent")
    ds_key = client_keys[0]  # "<Agent>:<Connection>" — same shape as 'Reservations-V2:QVD_Input'

    # Saved step code references that client by key, like production coder output.
    step_code = f'''
def generate_df(ds_clients, excel_files):
    client = ds_clients["{ds_key}"]
    return client.execute_query("SELECT Name AS genre FROM Genre ORDER BY GenreId LIMIT 3")
'''
    seeded = _run(_seed_artifact_step(report["id"], step_code))

    body = rerun_report(report["id"], user_token=token, org_id=org_id)

    # DESIRED: the rerun resolves Auto exactly like the interactive path and
    # the step succeeds. BUG TODAY: db_clients is {}, the step fails with
    # KeyError '<Agent>:<Connection>', yet HTTP 200 + last_run_at advance.
    step_after = _run(_read_step(seeded["step_id"]))
    assert body["steps_succeeded"] == 1, (
        f"rerun response: {body!r}; step after rerun: {step_after!r} "
        f"(interactive path resolved clients {client_keys!r} for the same report/user)")
    assert (step_after["data"] or {}).get("rows"), "step data was not refreshed"

    refreshed = get_report(report["id"], user_token=token, org_id=org_id)
    assert refreshed["last_run_at"] is not None
