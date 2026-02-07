"""Service for generating and managing artifact thumbnails."""

import asyncio
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
                await asyncio.sleep(1.5)

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
