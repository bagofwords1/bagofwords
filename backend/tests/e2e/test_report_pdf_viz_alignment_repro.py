"""
Repro: emailed / exported dashboard PDF shows missing numbers and empty charts
(axes render but no bars/lines), while the same dashboard in the bagofwords
client is correct.

Reported by a customer: "when I send a dashboard via email, the attached pdf
misses some numbers. Also, none of the charts have lines/bars in them. The
original in the bagofwords client is fine."

Root-cause hypothesis being validated here:

  Generated dashboard artifacts reference their visualizations BY INDEX
  (viz[0], viz[1], ...), matching the ORDERED list stored on the artifact at
  artifact.content["visualization_ids"].

  The live client (frontend/components/dashboard/ArtifactFrame.vue) honors that
  contract: it fetches visualizations scoped to the artifact
  (/api/queries?report_id=..&artifact_id=..) and then REORDERS them to match
  artifact.content["visualization_ids"] before injecting window.ARTIFACT_DATA.

  Before the fix, the PDF/email path (app/services/report_pdf_service.py
  _render_artifact_pdf) applied NO ordering — it injected every report
  visualization in DB order, ignoring artifact.content["visualization_ids"].
  So window.ARTIFACT_DATA.visualizations was a differently-ordered superset;
  viz[0]/viz[1] in the dashboard code pointed at the WRONG visualizations,
  whose rows lack the expected fields -> undefined / null KPI values (rendered
  as "undefined" or the em-dash from fmt(null)) and empty chart series (ECharts
  then auto-scales the value axis to its default 0..1 range and draws no
  bars/lines).

The fix orders the injected visualizations by
artifact.content["visualization_ids"] (appending any stragglers last), exactly
like the client. This test validates that:
  * the first N injected vizs match the artifact's ordered contract, so
    index-based lookups (viz[0], viz[1]) bind to the RIGHT data, and
  * any non-artifact viz is pushed AFTER the contract vizs (harmless), never
    shifting the contract indices.

No Playwright/headless browser is needed: we stub ReportPdfService.generate_pdf
to capture the exact HTML (and thus the embedded window.ARTIFACT_DATA) that
WOULD be rendered, and assert on the visualization list the renderer receives.

Run:
    cd backend
    BOW_DATABASE_URL=sqlite:///db/app.db \
      python -m pytest tests/e2e/test_report_pdf_viz_alignment_repro.py -v -s
"""
import re
import json
import uuid
import asyncio

import pytest

from app.dependencies import async_session_maker
from app.models.organization import Organization
from app.models.user import User
from app.models.report import Report
from app.models.artifact import Artifact
from app.models.visualization import Visualization
from app.models.query import Query
from app.models.step import Step
from app.models.widget import Widget
from app.services.report_pdf_service import ReportPdfService


def _run(coro):
    return asyncio.run(coro)


async def _seed():
    """Seed a report whose artifact references 2 visualizations BY INDEX, in a
    specific order, while the report also holds a 3rd (unrelated) visualization.

    Returns (report_id, artifact_id, ordered_viz_ids, extra_viz_id).
    """
    suffix = uuid.uuid4().hex[:8]
    async with async_session_maker() as db:
        org = Organization(name=f"PDF Repro Org {suffix}")
        db.add(org)
        await db.flush()

        user = User(
            name="Report Owner",
            email=f"owner-{suffix}@example.com",
            hashed_password="x",
            is_active=True,
            is_superuser=False,
            is_verified=True,
        )
        db.add(user)
        await db.flush()

        report = Report(
            title="May 2026 vs May 2025 Performance Review",
            slug=f"perf-review-{suffix}",
            organization_id=org.id,
            user_id=user.id,
        )
        db.add(report)
        await db.flush()

        # Helper to create a widget + query + success step + visualization with
        # distinct data, so a wrong-index lookup yields the wrong (or missing)
        # fields.
        async def make_viz(key, title, rows, columns, order_hint):
            widget = Widget(
                title=title,
                slug=f"w-{key}-{suffix}",
                report_id=report.id,
            )
            db.add(widget)
            await db.flush()

            step = Step(
                title=f"{title} step",
                slug=f"s-{key}-{suffix}",
                status="success",
                widget_id=widget.id,
                data={"rows": rows, "columns": columns},
                data_model={"type": "bar"},
            )
            db.add(step)
            await db.flush()

            query = Query(
                title=title,
                report_id=report.id,
                widget_id=widget.id,
                default_step_id=step.id,
            )
            db.add(query)
            await db.flush()

            step.query_id = query.id
            await db.flush()

            viz = Visualization(
                title=title,
                status="success",
                report_id=report.id,
                query_id=query.id,
                view={"type": "bar"},
            )
            db.add(viz)
            await db.flush()
            return viz

        # viz "REVENUE" — the KPI/chart the dashboard reads at a given index.
        viz_revenue = await make_viz(
            "rev",
            "Revenue (Excl. Tax)",
            rows=[
                {"month": "May 2025", "revenue": 812345},
                {"month": "May 2026", "revenue": 991200},
            ],
            columns=[{"field": "month"}, {"field": "revenue"}],
            order_hint=0,
        )
        # viz "TRANSACTIONS"
        viz_txn = await make_viz(
            "txn",
            "Transaction Count",
            rows=[{"month": "May 2025", "txn": 14032}, {"month": "May 2026", "txn": 20518}],
            columns=[{"field": "month"}, {"field": "txn"}],
            order_hint=1,
        )
        # Extra viz on the SAME report but NOT part of this artifact.
        viz_extra = await make_viz(
            "extra",
            "Unrelated Chargers Breakdown",
            rows=[{"site": "A", "chargers": 7}, {"site": "B", "chargers": 3}],
            columns=[{"field": "site"}, {"field": "chargers"}],
            order_hint=99,
        )

        # The artifact's contract: an ORDERED list of exactly its two vizs.
        ordered_viz_ids = [str(viz_txn.id), str(viz_revenue.id)]

        artifact = Artifact(
            report_id=report.id,
            user_id=user.id,
            organization_id=org.id,
            title="Dashboard",
            mode="page",
            content={
                "code": "<div id='dash'></div>",
                "visualization_ids": ordered_viz_ids,
            },
        )
        db.add(artifact)
        await db.flush()

        await db.commit()
        return (
            str(report.id),
            str(artifact.id),
            ordered_viz_ids,
            str(viz_extra.id),
        )


def _extract_artifact_data(html: str) -> dict:
    """Pull the window.ARTIFACT_DATA = {...}; JSON blob out of the built HTML."""
    m = re.search(r"window\.ARTIFACT_DATA\s*=\s*(\{.*?\});", html, re.S)
    assert m, "window.ARTIFACT_DATA not found in generated HTML"
    return json.loads(m.group(1))


@pytest.mark.asyncio
async def test_pdf_visualizations_aligned_to_artifact_contract(monkeypatch):
    report_id, artifact_id, ordered_viz_ids, extra_viz_id = await _seed()

    # Stub the headless render: capture the HTML the renderer WOULD receive.
    captured = {}

    async def fake_generate_pdf(self, artifact_id, html_content, mode="page"):
        captured["html"] = html_content
        captured["mode"] = mode
        return f"pdfs/{artifact_id}.pdf"

    monkeypatch.setattr(ReportPdfService, "generate_pdf", fake_generate_pdf)

    # The vendored JS libs (react/echarts/tailwind) are downloaded at Docker
    # build time and absent from the repo; they're irrelevant to this data-
    # alignment repro, so stub the inline-scripts loader.
    import app.services.report_pdf_service as pdf_mod
    monkeypatch.setattr(pdf_mod, "get_inline_scripts", lambda mode="page": "")

    service = ReportPdfService()
    result = await service.generate_for_artifact(artifact_id)
    assert result is not None, "PDF generation returned None"
    assert "html" in captured, "generate_pdf was never called"

    data = _extract_artifact_data(captured["html"])
    pdf_vizs = data.get("visualizations", [])
    pdf_ids = [v["id"] for v in pdf_vizs]

    print(f"[contract] artifact.visualization_ids (ordered) = {ordered_viz_ids}")
    print(f"[pdf]      visualizations injected (in order)   = {pdf_ids}")
    print(f"[pdf]      extra (non-artifact) viz present?    = {extra_viz_id in pdf_ids}")

    # --- ORDER. The dashboard code reads viz[0], viz[1] by index and relies on
    # the injected order matching artifact.content['visualization_ids']. After
    # the fix, the first N injected vizs equal the artifact's ordered ids, so
    # index-based lookups bind to the RIGHT data (correct numbers + populated
    # chart series).
    assert pdf_ids[: len(ordered_viz_ids)] == ordered_viz_ids, (
        f"Injected viz order must match the artifact contract; "
        f"got {pdf_ids} vs contract {ordered_viz_ids}"
    )
    assert pdf_ids[0] == ordered_viz_ids[0], "viz[0] must equal contract viz[0]"

    # --- SCOPE. Any non-artifact report viz is appended AFTER the contract
    # vizs (mirrors ArtifactFrame.vue), so it never shifts the contract indices.
    if extra_viz_id in pdf_ids:
        assert pdf_ids.index(extra_viz_id) >= len(ordered_viz_ids), (
            "A non-artifact viz must not occupy a contract index"
        )

    print(
        "[fixed] PDF renderer now receives visualizations ordered by the "
        "artifact contract, so index-based dashboard code binds to the correct "
        "data (numbers present, chart series populated)."
    )
