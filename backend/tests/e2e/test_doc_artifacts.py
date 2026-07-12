"""Doc artifacts (mode='doc') — create_doc / edit_doc tools + routing filters.

Contracts asserted:
- create_doc persists a mode='doc' Artifact with {markdown, visualization_ids},
  validating {{viz:<uuid>}} placeholders (exist + belong to report + success step).
- A doc with any invalid placeholder is rejected and nothing is persisted.
- edit_doc applies surgical find/replace ops atomically as a NEW version row;
  failed matches leave no new version. Full-markdown rewrite is the fallback.
- Docs never hijack dashboard routing: the planner's active-artifact lookup,
  GET /artifacts/report/{id}/latest, and default report rerun all keep binding
  to the latest DASHBOARD even when a doc is newer.
- edit_artifact refuses docs (wrong tool); read_artifact returns doc markdown.

Run:
    cd backend
    uv run pytest tests/e2e/test_doc_artifacts.py -v
"""
import asyncio
import uuid
from datetime import datetime, timedelta

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


GOOD_CODE = """
def generate_df(ds_clients, excel_files):
    import pandas as pd
    return pd.DataFrame({"artist": ["Queen", "AC/DC"], "revenue": [10, 20]})
"""


async def _seed_viz(report_id: str, n: int = 2, step_status: str = "success"):
    """Seed widget->query->step->visualization graphs directly (as the AI flow would)."""
    suffix = uuid.uuid4().hex[:8]
    now = datetime.utcnow()
    async with async_session_maker() as db:
        report = await db.get(Report, report_id)
        org_id, user_id = report.organization_id, report.user_id
        viz_ids = []
        for qi in range(n):
            widget = Widget(title=f"W{qi} {suffix}", slug=f"w{qi}-{suffix}", report_id=report_id)
            db.add(widget)
            await db.flush()
            query = Query(
                title=f"Query {qi}", report_id=report_id, widget_id=widget.id,
                organization_id=org_id, user_id=user_id,
            )
            db.add(query)
            await db.flush()
            step = Step(
                title=f"Step {qi}", slug=f"s{qi}-{suffix}", status=step_status,
                widget_id=widget.id, query_id=query.id, code=GOOD_CODE,
                data={"rows": [{"artist": "Queen", "revenue": 10}],
                      "columns": [{"field": "artist"}, {"field": "revenue"}]},
                created_at=now - timedelta(hours=1),
            )
            db.add(step)
            await db.flush()
            query.default_step_id = step.id
            viz = Visualization(
                title=f"Viz {qi}", status="success", report_id=report_id,
                query_id=query.id, view={"type": "bar_chart"},
            )
            db.add(viz)
            await db.flush()
            viz_ids.append(str(viz.id))
        await db.commit()
        return {"viz_ids": viz_ids, "org_id": str(org_id), "user_id": str(user_id)}


async def _run_tool(tool, tool_input: dict, report_id: str):
    """Drive a tool's run_stream with a real db session + report/user/org context."""
    async with async_session_maker() as db:
        report = await db.get(Report, report_id)
        user = await db.get(User, report.user_id)
        organization = await db.get(Organization, report.organization_id)
        runtime_ctx = {"db": db, "report": report, "user": user, "organization": organization}
        events = []
        async for evt in tool.run_stream(tool_input, runtime_ctx):
            events.append(evt)
        return events


def _end_payload(events):
    ends = [e for e in events if e.type == "tool.end"]
    assert ends, f"no tool.end event in {[e.type for e in events]}"
    return ends[-1].payload


async def _artifacts_for_report(report_id: str, mode: str = None):
    from sqlalchemy import select
    async with async_session_maker() as db:
        stmt = select(Artifact).where(
            Artifact.report_id == report_id, Artifact.deleted_at.is_(None)
        ).order_by(Artifact.created_at.asc())
        if mode:
            stmt = stmt.where(Artifact.mode == mode)
        res = await db.execute(stmt)
        return list(res.scalars().all())


def _make_report(create_report, create_user, login_user, whoami, title):
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)["organizations"][0]["id"]
    report = create_report(title=title, user_token=user_token, org_id=org_id, data_sources=[])
    return report, user_token, org_id


# ---------------------------------------------------------------------------
# create_doc
# ---------------------------------------------------------------------------

@pytest.mark.e2e
def test_create_doc_persists_markdown_and_valid_viz_ids(
    create_report, create_user, login_user, whoami, test_client
):
    from app.ai.tools.implementations.create_doc import CreateDocTool

    report, user_token, org_id = _make_report(create_report, create_user, login_user, whoami, "Doc report")
    seeded = _run(_seed_viz(report["id"], n=2))
    v1, v2 = seeded["viz_ids"]

    md = (
        "# Revenue Analysis\n\n## Findings\n"
        f"Revenue is concentrated (source: `invoices.total`, 2023-2025).\n\n{{{{viz:{v1}}}}}\n\n"
        f"```\nquoted example {{{{viz:{v2}}}}}\n```\n\n"
        "```mermaid\ngraph TD; A-->B\n```\n"
    )
    events = _run(_run_tool(CreateDocTool(), {"title": "Revenue Analysis", "markdown": md}, report["id"]))
    payload = _end_payload(events)
    assert payload["output"]["success"] is True, payload
    doc_id = payload["output"]["doc_id"]

    # Only the live placeholder counts; the fenced one is quoted.
    assert payload["output"]["visualization_ids"] == [v1]
    assert payload["output"]["outline"] == ["# Revenue Analysis", "## Findings"]
    assert payload["observation"]["doc_id"] == doc_id
    assert "markdown_snapshot" in payload["observation"]

    docs = _run(_artifacts_for_report(report["id"], mode="doc"))
    assert len(docs) == 1
    assert docs[0].content["markdown"] == md
    assert docs[0].content["visualization_ids"] == [v1]
    assert docs[0].version == 1
    assert docs[0].status == "completed"

    # API surfaces the doc: list includes it, schema validation accepts mode='doc'
    headers = {"Authorization": f"Bearer {user_token}", "X-Organization-Id": str(org_id)}
    listing = test_client.get(f"/api/artifacts/report/{report['id']}", headers=headers)
    assert listing.status_code == 200
    modes = {a["mode"] for a in listing.json()}
    assert "doc" in modes

    got = test_client.get(f"/api/artifacts/{doc_id}", headers=headers)
    assert got.status_code == 200
    assert got.json()["mode"] == "doc"
    assert got.json()["content"]["markdown"] == md


@pytest.mark.e2e
def test_create_doc_rejects_invalid_viz_and_persists_nothing(
    create_report, create_user, login_user, whoami
):
    from app.ai.tools.implementations.create_doc import CreateDocTool

    report, *_ = _make_report(create_report, create_user, login_user, whoami, "Bad viz doc")
    bogus = str(uuid.uuid4())
    md = f"# T\n{{{{viz:{bogus}}}}}\n"
    events = _run(_run_tool(CreateDocTool(), {"title": "T", "markdown": md}, report["id"]))
    payload = _end_payload(events)
    assert payload["output"]["success"] is False
    assert "not found" in payload["output"]["error"]
    assert _run(_artifacts_for_report(report["id"])) == []


@pytest.mark.e2e
def test_create_doc_rejects_viz_from_other_report_and_failed_steps(
    create_report, create_user, login_user, whoami
):
    from app.ai.tools.implementations.create_doc import CreateDocTool

    report_a, user_token, org_id = _make_report(create_report, create_user, login_user, whoami, "A")
    report_b = create_report(title="B", user_token=user_token, org_id=org_id, data_sources=[])
    other = _run(_seed_viz(report_b["id"], n=1))["viz_ids"][0]
    failed = _run(_seed_viz(report_a["id"], n=1, step_status="error"))["viz_ids"][0]

    for viz_id, expected in [(other, "does not belong"), (failed, "not success")]:
        events = _run(_run_tool(
            CreateDocTool(), {"title": "T", "markdown": f"# T\n{{{{viz:{viz_id}}}}}"}, report_a["id"]
        ))
        payload = _end_payload(events)
        assert payload["output"]["success"] is False
        assert expected in payload["output"]["error"], payload["output"]["error"]
    assert _run(_artifacts_for_report(report_a["id"])) == []


@pytest.mark.e2e
def test_create_doc_allows_zero_visualizations(
    create_report, create_user, login_user, whoami
):
    """A pure write-up (no charts) is a legitimate document."""
    from app.ai.tools.implementations.create_doc import CreateDocTool

    report, *_ = _make_report(create_report, create_user, login_user, whoami, "Prose only")
    events = _run(_run_tool(CreateDocTool(), {"title": "Memo", "markdown": "# Memo\nAnswer first."}, report["id"]))
    payload = _end_payload(events)
    assert payload["output"]["success"] is True
    assert payload["output"]["visualization_ids"] == []


# ---------------------------------------------------------------------------
# edit_doc
# ---------------------------------------------------------------------------

def _create_doc(report_id, title="Doc", markdown="# Doc\n\nHello world.\n"):
    from app.ai.tools.implementations.create_doc import CreateDocTool
    events = _run(_run_tool(CreateDocTool(), {"title": title, "markdown": markdown}, report_id))
    payload = _end_payload(events)
    assert payload["output"]["success"] is True, payload
    return payload["output"]["doc_id"]


@pytest.mark.e2e
def test_edit_doc_surgical_edits_create_new_version(
    create_report, create_user, login_user, whoami
):
    from app.ai.tools.implementations.edit_doc import EditDocTool

    report, *_ = _make_report(create_report, create_user, login_user, whoami, "Edit doc")
    doc_id = _create_doc(report["id"], markdown="# Doc\n\nHello world.\n\n## Open questions\nTBD\n")

    events = _run(_run_tool(EditDocTool(), {
        "doc_id": doc_id,
        "edits": [
            {"find": "Hello world.", "replace": "Hello, music store."},
            {"find": "## Open questions\nTBD", "replace": "## Open questions\nNone — analysis complete."},
        ],
    }, report["id"]))
    payload = _end_payload(events)
    assert payload["output"]["success"] is True, payload
    assert payload["output"]["diff_applied"] is True
    assert payload["output"]["version"] == 2

    docs = _run(_artifacts_for_report(report["id"], mode="doc"))
    assert [d.version for d in docs] == [1, 2]
    assert "Hello, music store." in docs[-1].content["markdown"]
    assert "analysis complete" in docs[-1].content["markdown"]
    # v1 unchanged — version history intact
    assert "Hello world." in docs[0].content["markdown"]


@pytest.mark.e2e
def test_edit_doc_failed_match_is_atomic_no_new_version(
    create_report, create_user, login_user, whoami
):
    from app.ai.tools.implementations.edit_doc import EditDocTool

    report, *_ = _make_report(create_report, create_user, login_user, whoami, "Atomic edit")
    doc_id = _create_doc(report["id"], markdown="# Doc\n\ndup dup\n")

    # Op 1 would succeed; op 2 is ambiguous -> nothing applies, no new version.
    events = _run(_run_tool(EditDocTool(), {
        "doc_id": doc_id,
        "edits": [
            {"find": "# Doc", "replace": "# Doc v2"},
            {"find": "dup", "replace": "X"},
        ],
    }, report["id"]))
    payload = _end_payload(events)
    assert payload["output"]["success"] is False
    assert "2 times" in payload["output"]["error"]

    docs = _run(_artifacts_for_report(report["id"], mode="doc"))
    assert len(docs) == 1
    assert docs[0].content["markdown"] == "# Doc\n\ndup dup\n"


@pytest.mark.e2e
def test_edit_doc_full_rewrite_fallback(
    create_report, create_user, login_user, whoami
):
    from app.ai.tools.implementations.edit_doc import EditDocTool

    report, *_ = _make_report(create_report, create_user, login_user, whoami, "Rewrite doc")
    doc_id = _create_doc(report["id"])

    events = _run(_run_tool(EditDocTool(), {
        "doc_id": doc_id, "markdown": "# Rewritten\n\nAll new.\n", "title": "Rewritten",
    }, report["id"]))
    payload = _end_payload(events)
    assert payload["output"]["success"] is True
    assert payload["output"]["diff_applied"] is False
    assert payload["output"]["version"] == 2
    docs = _run(_artifacts_for_report(report["id"], mode="doc"))
    assert docs[-1].title == "Rewritten"


@pytest.mark.e2e
def test_edit_doc_requires_exactly_one_of_edits_or_markdown(
    create_report, create_user, login_user, whoami
):
    from app.ai.tools.implementations.edit_doc import EditDocTool

    report, *_ = _make_report(create_report, create_user, login_user, whoami, "Arg validation")
    doc_id = _create_doc(report["id"])

    for bad_input in [
        {"doc_id": doc_id},  # neither
        {"doc_id": doc_id, "markdown": "# X", "edits": [{"find": "a", "replace": "b"}]},  # both
    ]:
        events = _run(_run_tool(EditDocTool(), bad_input, report["id"]))
        payload = _end_payload(events)
        assert payload["output"]["success"] is False
        assert "exactly one" in payload["output"]["error"]


@pytest.mark.e2e
def test_edit_doc_rejects_dashboard_artifacts(
    create_report, create_user, login_user, whoami
):
    from app.ai.tools.implementations.edit_doc import EditDocTool

    report, *_ = _make_report(create_report, create_user, login_user, whoami, "Wrong tool")

    async def _seed_dashboard():
        async with async_session_maker() as db:
            r = await db.get(Report, report["id"])
            a = Artifact(
                report_id=report["id"], user_id=r.user_id, organization_id=r.organization_id,
                title="Dash", mode="page", version=1, status="completed",
                content={"code": "function App() {}", "visualization_ids": []},
            )
            db.add(a)
            await db.commit()
            await db.refresh(a)
            return str(a.id)

    dash_id = _run(_seed_dashboard())
    events = _run(_run_tool(EditDocTool(), {"doc_id": dash_id, "markdown": "# X"}, report["id"]))
    payload = _end_payload(events)
    assert payload["output"]["success"] is False
    assert "edit_artifact" in payload["output"]["error"]


# ---------------------------------------------------------------------------
# Routing filters — docs must not hijack dashboard surfaces
# ---------------------------------------------------------------------------

@pytest.mark.e2e
def test_docs_do_not_hijack_latest_artifact_or_rerun(
    create_report, create_user, login_user, whoami, test_client, rerun_report
):
    """Dashboard first, newer doc second: /latest, rerun and the planner's
    active-artifact lookup must all still resolve to the dashboard."""
    report, user_token, org_id = _make_report(create_report, create_user, login_user, whoami, "Hijack guard")
    seeded = _run(_seed_viz(report["id"], n=1))
    dash_viz = seeded["viz_ids"][0]

    async def _seed_dashboard():
        async with async_session_maker() as db:
            r = await db.get(Report, report["id"])
            a = Artifact(
                report_id=report["id"], user_id=r.user_id, organization_id=r.organization_id,
                title="Dashboard", mode="page", version=1, status="completed",
                content={"code": "function App() {}", "visualization_ids": [dash_viz]},
                created_at=datetime.utcnow() - timedelta(minutes=5),
            )
            db.add(a)
            await db.commit()
            return str(a.id)

    dash_id = _run(_seed_dashboard())
    doc_id = _create_doc(report["id"], title="Newer doc", markdown="# Newer doc\nProse.")

    headers = {"Authorization": f"Bearer {user_token}", "X-Organization-Id": str(org_id)}

    # /latest returns the dashboard even though the doc is newer
    latest = test_client.get(f"/api/artifacts/report/{report['id']}/latest", headers=headers)
    assert latest.status_code == 200
    assert latest.json()["id"] == dash_id
    assert latest.json()["mode"] == "page"

    # The artifacts list still exposes BOTH (docs are first-class in the list)
    listing = test_client.get(f"/api/artifacts/report/{report['id']}", headers=headers)
    ids = {a["id"] for a in listing.json()}
    assert {dash_id, doc_id} <= ids

    # Default rerun follows the dashboard's queries (1 step), not the doc
    body = rerun_report(report["id"], user_token=user_token, org_id=org_id)
    assert body["steps_total"] == 1

    # Planner active-artifact lookup: doc excluded by mode filter
    async def _active_artifact_mode():
        from sqlalchemy import select
        async with async_session_maker() as db:
            res = await db.execute(
                select(Artifact).where(
                    Artifact.report_id == report["id"],
                    Artifact.status == "completed",
                    Artifact.mode.in_(("page", "slides")),
                ).order_by(Artifact.created_at.desc()).limit(1)
            )
            return res.scalar_one_or_none()

    active = _run(_active_artifact_mode())
    assert active is not None and str(active.id) == dash_id


@pytest.mark.e2e
def test_edit_artifact_refuses_docs_and_read_artifact_returns_markdown(
    create_report, create_user, login_user, whoami
):
    from app.ai.tools.implementations.edit_artifact import EditArtifactTool
    from app.ai.tools.implementations.read_artifact import ReadArtifactTool

    report, *_ = _make_report(create_report, create_user, login_user, whoami, "Doc tool guards")
    doc_id = _create_doc(report["id"], markdown="# Guarded\nExact text to quote.")

    events = _run(_run_tool(
        EditArtifactTool(),
        {"artifact_id": doc_id, "edit_prompt": "## Style changes\nMake it blue"},
        report["id"],
    ))
    payload = _end_payload(events)
    assert payload["output"]["success"] is False
    assert "edit_doc" in payload["output"]["error"]

    events = _run(_run_tool(ReadArtifactTool(), {"artifact_id": doc_id}, report["id"]))
    payload = _end_payload(events)
    assert "Exact text to quote." in payload["output"]["code"]


# ---------------------------------------------------------------------------
# User-facing doc_edit route (TipTap save path)
# ---------------------------------------------------------------------------

@pytest.mark.e2e
def test_doc_edit_route_saves_new_version_owner_only(
    create_report, create_user, login_user, whoami, test_client
):
    report, user_token, org_id = _make_report(create_report, create_user, login_user, whoami, "Doc edit route")
    doc_id = _create_doc(report["id"], markdown="# Doc\n\nOriginal text.\n")
    headers = {"Authorization": f"Bearer {user_token}", "X-Organization-Id": str(org_id)}

    r = test_client.post(f"/api/artifacts/{doc_id}/doc_edit", headers=headers,
                         json={"markdown": "# Doc\n\nEdited by owner.\n"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["version"] == 2
    assert body["mode"] == "doc"
    assert body["content"]["markdown"] == "# Doc\n\nEdited by owner.\n"

    # Empty markdown rejected
    r = test_client.post(f"/api/artifacts/{doc_id}/doc_edit", headers=headers, json={"markdown": "  "})
    assert r.status_code == 400

    # Invalid placeholder rejected
    r = test_client.post(f"/api/artifacts/{doc_id}/doc_edit", headers=headers,
                         json={"markdown": f"# Doc\n{{{{viz:{uuid.uuid4()}}}}}\n"})
    assert r.status_code == 400
    assert "placeholder" in r.json()["detail"].lower() or "not found" in r.json()["detail"].lower()


@pytest.mark.e2e
def test_doc_edit_route_rejects_non_docs_and_locks_during_runs(
    create_report, create_user, login_user, whoami, test_client
):
    from app.models.completion import Completion

    report, user_token, org_id = _make_report(create_report, create_user, login_user, whoami, "Doc run lock")
    doc_id = _create_doc(report["id"], markdown="# Doc\n\nText.\n")
    headers = {"Authorization": f"Bearer {user_token}", "X-Organization-Id": str(org_id)}

    # Non-doc artifact rejected
    async def _seed_dashboard():
        async with async_session_maker() as db:
            r = await db.get(Report, report["id"])
            a = Artifact(
                report_id=report["id"], user_id=r.user_id, organization_id=r.organization_id,
                title="Dash", mode="page", version=1, status="completed",
                content={"code": "function App() {}", "visualization_ids": []},
            )
            db.add(a)
            await db.commit()
            await db.refresh(a)
            return str(a.id)

    dash_id = _run(_seed_dashboard())
    r = test_client.post(f"/api/artifacts/{dash_id}/doc_edit", headers=headers, json={"markdown": "# X"})
    assert r.status_code == 400
    assert "not a document" in r.json()["detail"]

    # Run-lock: an in_progress completion on the report blocks the save with 409
    async def _seed_running_completion():
        async with async_session_maker() as db:
            rep = await db.get(Report, report["id"])
            comp = Completion(
                report_id=report["id"],
                user_id=rep.user_id,
                status="in_progress",
                role="system",
                prompt={"content": ""},
                completion={"content": ""},
                model="test",
            )
            db.add(comp)
            await db.commit()
            await db.refresh(comp)
            return str(comp.id)

    comp_id = _run(_seed_running_completion())
    r = test_client.post(f"/api/artifacts/{doc_id}/doc_edit", headers=headers,
                         json={"markdown": "# Doc\n\nBlocked edit.\n"})
    assert r.status_code == 409

    # Once the run finishes, the save goes through
    async def _finish_completion():
        async with async_session_maker() as db:
            comp = await db.get(Completion, comp_id)
            comp.status = "success"
            await db.commit()

    _run(_finish_completion())
    r = test_client.post(f"/api/artifacts/{doc_id}/doc_edit", headers=headers,
                         json={"markdown": "# Doc\n\nUnblocked edit.\n"})
    assert r.status_code == 200
    assert r.json()["version"] == 2
