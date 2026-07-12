"""Service for generating PDF exports of report artifacts using Playwright."""

import asyncio
import json
import logging
from pathlib import Path
from typing import Optional

from app.services.artifact_libs import get_inline_scripts

logger = logging.getLogger(__name__)


class ReportPdfService:
    """Generates PDF snapshots of artifacts using headless Chromium."""

    UPLOADS_DIR = Path(__file__).parent.parent.parent / "uploads" / "pdfs"

    def __init__(self):
        self.UPLOADS_DIR.mkdir(parents=True, exist_ok=True)

    async def generate_pdf(
        self,
        artifact_id: str,
        html_content: str,
        mode: str = "page",
    ) -> Optional[str]:
        """Render HTML to PDF via Playwright.

        Returns:
            Relative path to the PDF file (e.g. "pdfs/{id}.pdf"), or None on failure.
        """
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            logger.warning("Playwright not installed, skipping PDF generation")
            return None

        pdf_path = self.UPLOADS_DIR / f"{artifact_id}.pdf"

        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                page = await browser.new_page(viewport={"width": 1280, "height": 720})

                await page.set_content(html_content, wait_until="networkidle")

                # Wait for React to mount and render content
                try:
                    await page.wait_for_function("""
                        () => {
                            const root = document.getElementById('root');
                            if (!root || root.children.length === 0) return false;
                            const spinners = root.querySelectorAll('svg animateTransform');
                            for (let i = 0; i < spinners.length; i++) {
                                const svg = spinners[i].closest('svg');
                                if (svg && svg.offsetWidth > 0 && svg.offsetHeight > 0) return false;
                            }
                            return true;
                        }
                    """, timeout=15000)
                except Exception:
                    pass

                # Wait for charts/tables to render (canvas, svg, table elements)
                try:
                    await page.wait_for_function("""
                        () => {
                            const root = document.getElementById('root');
                            if (!root) return false;
                            const hasContent = root.querySelectorAll('canvas, svg:not([class*="spinner"]):not([class*="loading"]), table, .recharts-wrapper, [class*="chart"], [class*="viz"]').length > 0;
                            const hasText = root.innerText && root.innerText.trim().length > 20;
                            return hasContent || hasText;
                        }
                    """, timeout=30000)
                except Exception:
                    logger.warning("Timed out waiting for chart content to render for artifact %s", artifact_id)

                # Wait for chart animations to settle
                await asyncio.sleep(3)

                # Generate PDF
                pdf_bytes = await page.pdf(
                    format="A4",
                    print_background=True,
                    margin={"top": "0.5in", "bottom": "0.5in", "left": "0.5in", "right": "0.5in"},
                )

                await browser.close()

            pdf_path.write_bytes(pdf_bytes)
            return f"pdfs/{artifact_id}.pdf"

        except Exception as e:
            logger.exception(f"Failed to generate PDF for artifact {artifact_id}: {e}")
            return None

    async def generate_for_report(self, report_id: str) -> Optional[str]:
        """Generate a PDF for the latest artifact of a report.

        Returns:
            Absolute filesystem path to the PDF, or None on failure.
        """
        try:
            from app.dependencies import async_session_maker
            from app.models.artifact import Artifact
            from sqlalchemy import select

            async with async_session_maker() as db:
                stmt = (
                    select(Artifact)
                    .where(
                        Artifact.report_id == report_id,
                        Artifact.deleted_at.is_(None),
                        # Report-level PDF renders the dashboard; docs export
                        # via generate_for_artifact with the doc's id.
                        Artifact.mode.in_(("page", "slides")),
                    )
                    .order_by(Artifact.created_at.desc())
                    .limit(1)
                )
                result = await db.execute(stmt)
                artifact = result.scalar_one_or_none()

                if not artifact or not artifact.content:
                    logger.warning(f"No artifact found for report {report_id}")
                    return None

                return await self._render_artifact_pdf(db, artifact)

        except Exception as e:
            logger.exception(f"Failed to generate PDF for report {report_id}: {e}")
            return None

    async def generate_for_artifact(self, artifact_id: str) -> Optional[str]:
        """Generate a PDF for a specific artifact.

        Returns:
            Absolute filesystem path to the PDF, or None on failure.
        """
        try:
            from app.dependencies import async_session_maker
            from app.models.artifact import Artifact

            async with async_session_maker() as db:
                artifact = await db.get(Artifact, artifact_id)
                if not artifact or artifact.deleted_at is not None or not artifact.content:
                    logger.warning(f"No usable artifact found for id {artifact_id}")
                    return None

                return await self._render_artifact_pdf(db, artifact)

        except Exception as e:
            logger.exception(f"Failed to generate PDF for artifact {artifact_id}: {e}")
            return None

    async def _render_artifact_pdf(self, db, artifact) -> Optional[str]:
        """Build the artifact HTML (with its report's visualization data) and render to PDF.

        Returns the absolute filesystem path to the PDF, or None on failure.
        """
        from app.models.report import Report
        from app.models.visualization import Visualization
        from app.models.query import Query
        from app.models.step import Step
        from sqlalchemy import select
        from sqlalchemy.orm import selectinload

        report = await db.get(Report, artifact.report_id)
        if not report:
            return None

        # Get visualization data for the artifact's report
        viz_stmt = (
            select(Visualization)
            .options(selectinload(Visualization.query))
            .where(Visualization.report_id == artifact.report_id)
        )
        viz_result = await db.execute(viz_stmt)
        visualizations = viz_result.scalars().all()

        # Dashboard code addresses visualizations BY INDEX (viz[0], viz[1], ...),
        # and that index is defined by the artifact's ORDERED
        # content["visualization_ids"]. Mirror the live client
        # (frontend/components/dashboard/ArtifactFrame.vue): scope to this
        # artifact's visualizations and order them by that list, appending any
        # stragglers last. Without this, window.ARTIFACT_DATA would be a
        # report-wide, unordered superset and every index-based lookup in the
        # dashboard would bind to the wrong visualization — producing missing
        # numbers and empty chart series in the exported/emailed PDF.
        viz_ids = (artifact.content or {}).get("visualization_ids") or []
        if viz_ids:
            viz_by_id = {str(v.id): v for v in visualizations}
            ordered = [viz_by_id[vid] for vid in viz_ids if vid in viz_by_id]
            ordered_id_set = set(viz_ids)
            ordered.extend(v for v in visualizations if str(v.id) not in ordered_id_set)
            visualizations = ordered

        viz_data = []
        for viz in visualizations:
            if not viz.query_id:
                continue
            query = await db.get(Query, viz.query_id)
            if not query:
                continue

            step = None
            if query.default_step_id:
                step = await db.get(Step, query.default_step_id)
            if not step:
                step_result = await db.execute(
                    select(Step).where(Step.query_id == query.id).order_by(Step.created_at.desc()).limit(1)
                )
                step = step_result.scalar_one_or_none()

            viz_data.append({
                "id": str(viz.id),
                "title": viz.title or query.title or "Untitled",
                "view": viz.view or {},
                "rows": step.data.get("rows", []) if step and step.data else [],
                "columns": step.data.get("columns", []) if step and step.data else [],
                "dataModel": step.data_model or {} if step else {},
            })

        artifact_code = artifact.content.get("code", "")
        html_content = self._build_pdf_html(
            report_id=str(report.id),
            report_title=report.title,
            report_theme=report.theme_name,
            artifact_code=artifact_code,
            visualizations=viz_data,
            mode=artifact.mode or "page",
        )

        rel_path = await self.generate_pdf(
            artifact_id=str(artifact.id),
            html_content=html_content,
            mode=artifact.mode or "page",
        )

        if rel_path:
            return str(self.UPLOADS_DIR / f"{artifact.id}.pdf")
        return None

    def _build_pdf_html(
        self,
        report_id: str,
        report_title: Optional[str],
        report_theme: Optional[str],
        artifact_code: str,
        visualizations: list,
        mode: str = "page",
    ) -> str:
        """Build HTML for PDF rendering (same as thumbnail but full-size)."""
        artifact_data = {
            "report": {
                "id": report_id,
                "title": report_title,
                "theme": report_theme,
            },
            "visualizations": visualizations,
        }
        data_json = json.dumps(artifact_data, default=str)

        slides_scripts = get_inline_scripts(mode="slides")
        page_scripts = get_inline_scripts(mode="page")

        if mode == "slides":
            return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  {slides_scripts}
</head>
<body class="bg-slate-900">
  <script>window.ARTIFACT_DATA = {data_json};</script>
  {artifact_code}
</body>
</html>"""

        return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  {page_scripts}
  <style>html, body, #root {{ height: 100%; margin: 0; padding: 0; }}</style>
</head>
<body>
  <div id="root"></div>
  <script>
    window.ARTIFACT_DATA = {data_json};
    window.useArtifactData = function() {{ return window.ARTIFACT_DATA; }};
  </script>
  {artifact_code}
</body>
</html>"""
