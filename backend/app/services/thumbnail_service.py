"""Service for generating and managing artifact thumbnails."""

import asyncio
import json
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class ThumbnailService:
    """Service for generating thumbnail screenshots of artifacts using Playwright."""

    THUMBNAIL_WIDTH = 400
    THUMBNAIL_HEIGHT = 300
    UPLOADS_DIR = Path(__file__).parent.parent.parent / "uploads" / "thumbnails"

    def __init__(self):
        self.UPLOADS_DIR.mkdir(parents=True, exist_ok=True)

    async def generate_thumbnail(
        self,
        artifact_id: str,
        html_content: str,
        mode: str = "page",
    ) -> Optional[str]:
        """Generate a thumbnail screenshot for an artifact.

        Args:
            artifact_id: The artifact ID (used for filename)
            html_content: Complete HTML to render
            mode: 'page' or 'slides'

        Returns:
            Relative path to thumbnail file (e.g. "thumbnails/{id}.png"), or None on failure
        """
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            logger.warning("Playwright not installed, skipping thumbnail generation")
            return None

        thumbnail_path = self.UPLOADS_DIR / f"{artifact_id}.png"

        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                page = await browser.new_page(viewport={"width": 1280, "height": 720})

                await page.set_content(html_content, wait_until="networkidle")

                # Wait for render to complete
                try:
                    await page.wait_for_function(
                        "window.__ARTIFACT_RENDER_COMPLETE__ === true",
                        timeout=10000,
                    )
                except Exception:
                    pass  # Timeout is acceptable for thumbnails

                # Give time for charts/React to fully render
                await asyncio.sleep(3)

                # Take screenshot
                screenshot_bytes = await page.screenshot(
                    type="png",
                    clip={"x": 0, "y": 0, "width": 1280, "height": 720},
                )

                await browser.close()

            # Save and resize using PIL
            try:
                from PIL import Image
                import io

                # Load screenshot
                img = Image.open(io.BytesIO(screenshot_bytes))

                # Resize to thumbnail dimensions while maintaining aspect ratio
                img.thumbnail((self.THUMBNAIL_WIDTH, self.THUMBNAIL_HEIGHT), Image.Resampling.LANCZOS)

                # Save thumbnail
                img.save(thumbnail_path, "PNG", optimize=True)

            except ImportError:
                # Fallback: save raw screenshot if PIL not available
                logger.warning("PIL not installed, saving full-size screenshot")
                thumbnail_path.write_bytes(screenshot_bytes)

            return f"thumbnails/{artifact_id}.png"

        except Exception as e:
            logger.exception(f"Failed to generate thumbnail for artifact {artifact_id}: {e}")
            return None

    def get_thumbnail_path(self, artifact_id: str) -> Optional[Path]:
        """Get the filesystem path to an existing thumbnail.

        Returns:
            Full filesystem path to the thumbnail, or None if it doesn't exist
        """
        path = self.UPLOADS_DIR / f"{artifact_id}.png"
        if path.exists():
            return path
        return None

    def delete_thumbnail(self, artifact_id: str) -> bool:
        """Delete a thumbnail file.

        Returns:
            True if deleted, False if not found
        """
        path = self.UPLOADS_DIR / f"{artifact_id}.png"
        if path.exists():
            path.unlink()
            return True
        return False

    def copy_thumbnail(self, source_artifact_id: str, target_artifact_id: str) -> Optional[str]:
        """Copy a thumbnail from one artifact to another.

        Returns:
            Relative path to the new thumbnail, or None if source doesn't exist
        """
        import shutil
        source_path = self.UPLOADS_DIR / f"{source_artifact_id}.png"
        if not source_path.exists():
            return None

        target_path = self.UPLOADS_DIR / f"{target_artifact_id}.png"
        shutil.copy2(source_path, target_path)
        return f"thumbnails/{target_artifact_id}.png"

    async def regenerate_for_report(self, report_id: str) -> Optional[str]:
        """Regenerate thumbnail for the latest artifact of a report.

        Loads the artifact, visualization data, and rebuilds the HTML for screenshot.
        Runs in background with its own database session.

        Returns:
            Relative path to the new thumbnail, or None on failure
        """
        try:
            from app.dependencies import async_session_maker
            from app.models.artifact import Artifact
            from app.models.report import Report
            from app.models.visualization import Visualization
            from app.models.query import Query
            from app.models.step import Step
            from sqlalchemy import select, update
            from sqlalchemy.orm import selectinload

            async with async_session_maker() as db:
                # Get the latest artifact for this report
                stmt = (
                    select(Artifact)
                    .where(Artifact.report_id == report_id, Artifact.deleted_at.is_(None))
                    .order_by(Artifact.created_at.desc())
                    .limit(1)
                )
                result = await db.execute(stmt)
                artifact = result.scalar_one_or_none()

                if not artifact or not artifact.content:
                    logger.warning(f"No artifact found for report {report_id}")
                    return None

                # Get report info
                report = await db.get(Report, report_id)
                if not report:
                    return None

                # Get visualization data for this report
                viz_stmt = (
                    select(Visualization)
                    .options(selectinload(Visualization.query))
                    .where(Visualization.report_id == report_id)
                )
                viz_result = await db.execute(viz_stmt)
                visualizations = viz_result.scalars().all()

                # Build visualization data array
                viz_data = []
                for viz in visualizations:
                    if not viz.query_id:
                        continue
                    query = await db.get(Query, viz.query_id)
                    if not query:
                        continue

                    # Get the default step for this query
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

                # Build the HTML
                artifact_code = artifact.content.get("code", "")
                html_content = self._build_thumbnail_html(
                    report_id=str(report.id),
                    report_title=report.title,
                    report_theme=report.theme_name,
                    artifact_code=artifact_code,
                    visualizations=viz_data,
                    mode=artifact.mode or "page",
                )

                # Delete old thumbnail if exists
                self.delete_thumbnail(str(artifact.id))

                # Generate new thumbnail
                thumbnail_path = await self.generate_thumbnail(
                    artifact_id=str(artifact.id),
                    html_content=html_content,
                    mode=artifact.mode or "page",
                )

                if thumbnail_path:
                    # Update artifact with new thumbnail path
                    stmt = update(Artifact).where(Artifact.id == artifact.id).values(thumbnail_path=thumbnail_path)
                    await db.execute(stmt)
                    await db.commit()

                return thumbnail_path

        except Exception as e:
            logger.exception(f"Failed to regenerate thumbnail for report {report_id}: {e}")
            return None

    def _build_thumbnail_html(
        self,
        report_id: str,
        report_title: Optional[str],
        report_theme: Optional[str],
        artifact_code: str,
        visualizations: list,
        mode: str = "page",
    ) -> str:
        """Build HTML for thumbnail screenshot."""
        artifact_data = {
            "report": {
                "id": report_id,
                "title": report_title,
                "theme": report_theme,
            },
            "visualizations": visualizations,
        }
        data_json = json.dumps(artifact_data, default=str)

        if mode == "slides":
            return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-slate-900">
  <script>window.ARTIFACT_DATA = {data_json};</script>
  {artifact_code}
</body>
</html>"""

        # Dashboard mode
        return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <script src="https://cdn.tailwindcss.com"></script>
  <script crossorigin src="https://unpkg.com/react@18/umd/react.development.js"></script>
  <script crossorigin src="https://unpkg.com/react-dom@18/umd/react-dom.development.js"></script>
  <script src="https://unpkg.com/@babel/standalone/babel.min.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/echarts@5/dist/echarts.min.js"></script>
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
