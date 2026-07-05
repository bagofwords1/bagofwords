"""
Repro + regression guard: ``edit_artifact`` inflates
``artifact.content["visualization_ids"]`` with visualizations the artifact
code never uses — and the public report page ("/r/{id}") then downloads a
full ``/step`` payload for every one of them.

Reported behavior: the artifacts view (frontend/pages/r/[id]/index.vue)
should "not load data/steps/viz that are not in use in the artifact page".
The page's whole filter chain is driven by ``content.visualization_ids``
(``GET /r/{id}/queries?artifact_id=…`` → one ``/step`` fetch per returned
query), so any ID in that list that the dashboard code doesn't reference is
a wasted full-dataset download on every public page view.

Root cause (validated by this test):
``app/ai/tools/implementations/edit_artifact.py``

  1. AUTO-MERGE — lines ~665-682: every visualization in the report with
     ``created_at > artifact.created_at`` is merged into the edited
     artifact's ID list, whether or not the edit (or the code) references
     it. Any ``create_data`` call made after the artifact exists gets its
     visualization permanently attached by the next edit.
  2. NO PRUNE — nothing ever removes an ID, so the list only grows.

The MCP variant (``app/ai/tools/mcp/edit_artifact.py``) does NOT auto-merge
(existing + explicitly passed IDs only) — the leak is specific to the
planner tool.

No LLM needed: the LLM class inside edit_artifact is replaced with a fake
that returns a single surgical SEARCH/REPLACE diff renaming the dashboard
title — an edit that adds no visualizations.

Scenario (viz == query, 1:1):
  * 3 visualizations exist; an artifact is created from exactly those 3.
  * 7 more visualizations are created afterwards (never referenced).
  * edit_artifact renames the dashboard title (no visualization_ids input).

Expected (the invariant this test asserts): the edited artifact keeps only
the 3 visualizations its code uses; the public queries endpoint returns 3
queries; the /r page fetches 3 step payloads.

Observed today (why this test is xfail): the edited artifact carries all
10 IDs, the public endpoint returns 10 queries, and the page downloads
10 full step payloads — 3.3x the data actually used.

Run:
    cd backend
    BOW_DATABASE_URL=sqlite:///db/app.db \
      .venv/bin/python -m pytest tests/e2e/test_edit_artifact_viz_inflation_repro.py -v -s

Remove the xfail marker once the auto-merge/prune behavior is fixed.
"""
import asyncio
import json
import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from app.dependencies import async_session_maker
from app.models.organization import Organization
from app.models.user import User
from app.models.report import Report
from app.models.widget import Widget
from app.models.query import Query
from app.models.step import Step
from app.models.visualization import Visualization
from app.models.artifact import Artifact
from app.ai.llm.types import TextDeltaEvent


N_USED = 3      # visualizations the artifact was created from (code uses them)
N_LATER = 7     # visualizations created AFTER the artifact (never referenced)
ROWS_PER_STEP = 200


def _run(coro):
    return asyncio.run(coro)


def _make_rows(n, qi):
    return [
        {"label": f"item_{qi}_{i}", "value": round((i * 13.7 + qi) % 500, 2)}
        for i in range(n)
    ]


def _dashboard_code(used_viz_ids):
    """A realistic-looking page-mode artifact that references exactly
    `used_viz_ids` (the same lookup pattern generated dashboards use),
    padded past the 4000-char size guard in edit_artifact."""
    ids_js = ",\n  ".join(f'"{vid}"' for vid in used_viz_ids)
    filler = "// layout section — chart card grid, responsive breakpoints\n" * 60
    return f"""<script type="text/babel">
const USED_VIZ_IDS = [
  {ids_js}
];
const DASHBOARD_TITLE = "Sales Overview";

function getViz(id) {{
  return (window.ARTIFACT_DATA.visualizations || []).find(v => v.id === id);
}}

{filler}
function Dashboard() {{
  const vizs = USED_VIZ_IDS.map(getViz).filter(Boolean);
  return (
    <div className="p-6">
      <h1 className="text-xl font-bold">{{DASHBOARD_TITLE}}</h1>
      {{vizs.map(v => <pre key={{v.id}}>{{JSON.stringify(v.title)}}</pre>)}}
    </div>
  );
}}
ReactDOM.createRoot(document.getElementById('root')).render(<Dashboard />);
</script>"""


# The whole "LLM response" for the edit: one surgical diff renaming the
# title. It references no visualization — the edit is purely cosmetic.
FAKE_EDIT_DIFF = """<<<<<<< SEARCH
const DASHBOARD_TITLE = "Sales Overview";
=======
const DASHBOARD_TITLE = "Chinook Overview";
>>>>>>> REPLACE
"""


class _FakeLLM:
    """Stands in for app.ai.llm.LLM inside the edit_artifact module."""

    def __init__(self, *args, **kwargs):
        pass

    async def inference_stream_v2(self, *args, **kwargs):
        yield TextDeltaEvent(text=FAKE_EDIT_DIFF)


class _FakeModel:
    supports_vision = False


async def _seed():
    """Seed: org, user, published+public report, 10 widget/query/step/viz
    quadruples (viz == query, 1:1), and an artifact (v1) built from the
    FIRST 3 visualizations. The other 7 are created strictly AFTER the
    artifact's created_at — the precondition for the auto-merge."""
    suffix = uuid.uuid4().hex[:8]
    t0 = datetime.utcnow() - timedelta(minutes=10)
    t_artifact = t0 + timedelta(minutes=5)
    t_later = t0 + timedelta(minutes=8)
    columns = [{"field": "label"}, {"field": "value"}]

    async with async_session_maker() as db:
        org = Organization(name=f"Leak Org {suffix}")
        db.add(org)
        await db.flush()

        user = User(
            name="Leak User",
            email=f"leak-{suffix}@example.com",
            hashed_password="x",
            is_active=True,
            is_superuser=False,
            is_verified=True,
        )
        db.add(user)
        await db.flush()

        report = Report(
            title=f"Leak Report {suffix}",
            slug=f"leak-report-{suffix}",
            status="published",
            artifact_visibility="public",
            user_id=user.id,
            organization_id=org.id,
        )
        db.add(report)
        await db.flush()

        viz_ids, query_by_viz = [], {}
        for qi in range(N_USED + N_LATER):
            created = t0 if qi < N_USED else t_later
            widget = Widget(title=f"W{qi} {suffix}", slug=f"w{qi}-{suffix}", report_id=report.id)
            db.add(widget)
            await db.flush()

            query = Query(
                title=f"Query {qi}",
                report_id=report.id,
                widget_id=widget.id,
                organization_id=org.id,
                user_id=user.id,
            )
            db.add(query)
            await db.flush()

            step = Step(
                title=f"Step {qi}",
                slug=f"s{qi}-{suffix}",
                status="success",
                widget_id=widget.id,
                query_id=query.id,
                data={"rows": _make_rows(ROWS_PER_STEP, qi), "columns": columns},
                data_model={"type": "bar_chart", "columns": columns},
                view={"type": "bar_chart"},
            )
            db.add(step)
            await db.flush()
            query.default_step_id = step.id

            viz = Visualization(
                title=f"Viz {qi}",
                status="success",
                report_id=report.id,
                query_id=query.id,
                view={"type": "bar_chart"},
                created_at=created,
            )
            db.add(viz)
            await db.flush()
            viz_ids.append(str(viz.id))
            query_by_viz[str(viz.id)] = str(query.id)

        used_ids = viz_ids[:N_USED]
        later_ids = viz_ids[N_USED:]

        # Artifact v1 — exactly what create_artifact persists
        # (implementations/create_artifact.py: content = {code, visualization_ids})
        artifact = Artifact(
            report_id=report.id,
            user_id=user.id,
            organization_id=org.id,
            title="Sales Overview",
            mode="page",
            version=1,
            content={"code": _dashboard_code(used_ids), "visualization_ids": used_ids},
            status="completed",
            created_at=t_artifact,
        )
        db.add(artifact)
        await db.commit()

        return {
            "report_id": str(report.id),
            "artifact_id": str(artifact.id),
            "used_ids": used_ids,
            "later_ids": later_ids,
            "query_by_viz": query_by_viz,
        }


async def _run_edit(seeded, monkeypatch):
    """Invoke the real EditArtifactTool.run_stream with the LLM stubbed to a
    title-rename diff. Returns the tool.end payload."""
    import app.ai.tools.implementations.edit_artifact as ea_mod
    from app.ai.tools.implementations.create_artifact import CreateArtifactTool
    from app.ai.tools.implementations.edit_artifact import EditArtifactTool

    monkeypatch.setattr(ea_mod, "LLM", _FakeLLM)

    async def _no_thumbnail(self, **kwargs):
        return None

    monkeypatch.setattr(CreateArtifactTool, "_generate_thumbnail_background", _no_thumbnail)
    # _build_thumbnail_html inlines vendored browser libs (frontend/public/libs,
    # not in-repo) — irrelevant to the behavior under test.
    monkeypatch.setattr(
        CreateArtifactTool, "_build_thumbnail_html",
        lambda self, artifact_data, code, mode="page": "<html></html>",
    )

    async with async_session_maker() as db:
        report = (await db.execute(select(Report).where(Report.id == seeded["report_id"]))).scalar_one()
        user = (await db.execute(select(User).where(User.id == report.user_id))).scalar_one()
        org = (await db.execute(select(Organization).where(Organization.id == report.organization_id))).scalar_one()

        runtime_ctx = {
            "db": db,
            "report": report,
            "user": user,
            "organization": org,
            "model": _FakeModel(),
            "sigkill_event": None,
            "settings": None,
            "context_hub": None,
            "context_view": None,
            "head_completion": None,
        }

        tool = EditArtifactTool()
        events = []
        async for evt in tool.run_stream(
            {
                "artifact_id": seeded["artifact_id"],
                # Purely cosmetic edit: no new data, no visualization_ids input.
                "edit_prompt": "## Style changes\nRename the dashboard title to 'Chinook Overview'.",
            },
            runtime_ctx,
        ):
            events.append(evt)

        end = events[-1]
        assert end.type == "tool.end", f"unexpected last event: {end}"
        return end.payload


@pytest.mark.e2e
@pytest.mark.xfail(
    reason="Leak: edit_artifact auto-merges every viz created after the artifact "
    "and never prunes (implementations/edit_artifact.py auto-merge block); "
    "see docs/feedback-loops/edit-artifact-viz-inflation.md",
    strict=False,
)
def test_edit_artifact_keeps_only_visualizations_the_code_uses(test_client, monkeypatch):
    seeded = _run(_seed())
    payload = _run(_run_edit(seeded, monkeypatch))

    output = payload["output"]
    assert output.get("artifact_id"), f"edit failed: {payload}"
    assert payload["observation"].get("diff_applied", output.get("diff_applied")), (
        f"surgical diff did not apply: {payload['observation']}"
    )
    new_artifact_id = output["artifact_id"]
    new_code = output["code"]
    assert 'DASHBOARD_TITLE = "Chinook Overview"' in new_code

    async def _fetch_content_ids():
        async with async_session_maker() as db:
            art = (await db.execute(select(Artifact).where(Artifact.id == new_artifact_id))).scalar_one()
            return list((art.content or {}).get("visualization_ids", []))

    v2_ids = _run(_fetch_content_ids())
    referenced_in_code = [vid for vid in v2_ids if vid in new_code]
    unreferenced = [vid for vid in v2_ids if vid not in new_code]

    print(f"\n[inflation] artifact v1 visualization_ids: {len(seeded['used_ids'])} (all referenced by code)")
    print(f"[inflation] artifact v2 visualization_ids: {len(v2_ids)} "
          f"({len(referenced_in_code)} referenced by code, {len(unreferenced)} never referenced)")
    print(f"[inflation] later (auto-merged) vizs attached: "
          f"{sum(1 for vid in seeded['later_ids'] if vid in v2_ids)}/{len(seeded['later_ids'])}")

    # -- Public page cost: replay what frontend/pages/r/[id]/index.vue does --
    r = test_client.get(f"/api/r/{seeded['report_id']}/queries?artifact_id={new_artifact_id}")
    assert r.status_code == 200, r.text
    public_queries = r.json()

    step_bytes_total = 0
    for q in public_queries:
        sr = test_client.get(f"/api/r/{seeded['report_id']}/queries/{q['id']}/step")
        assert sr.status_code == 200, sr.text
        step_bytes_total += len(sr.content)

    used_query_ids = {seeded["query_by_viz"][vid] for vid in seeded["used_ids"]}
    used_bytes = 0
    for qid in used_query_ids:
        used_bytes += len(test_client.get(f"/api/r/{seeded['report_id']}/queries/{qid}/step").content)

    print(f"[inflation] public /queries?artifact_id= returned {len(public_queries)} queries "
          f"(dashboard uses {len(used_query_ids)})")
    print(f"[inflation] /step payload downloaded by /r page: {step_bytes_total / 1e3:.1f} kB "
          f"(needed: {used_bytes / 1e3:.1f} kB — "
          f"{step_bytes_total / max(used_bytes, 1):.1f}x)")

    # ---- The invariant (fails today — this is the leak) ----
    # A cosmetic edit that adds no visualizations must not grow the
    # artifact's visualization set…
    assert set(v2_ids) == set(seeded["used_ids"]), (
        f"edit_artifact attached {len(set(v2_ids) - set(seeded['used_ids']))} visualization(s) "
        f"the artifact code never references (auto-merge leak)"
    )
    # …and the public page must therefore only load the steps in use.
    assert len(public_queries) == len(used_query_ids), (
        f"public /r page will fetch {len(public_queries)} step payloads; "
        f"the artifact only uses {len(used_query_ids)}"
    )
