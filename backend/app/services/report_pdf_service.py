"""Service for generating PDF exports of report artifacts using Playwright.

Rendering an artifact to PDF is NOT the same job as rendering it to a screen,
and treating it as one is what made emailed reports arrive with content
missing. Three differences drive everything in this module:

* A PDF page does not scroll. Every `overflow-x-auto` table wrapper and every
  `max-height` panel — which the dashboard codegen is explicitly told to emit —
  silently drops whatever it was hiding. `_PREPARE_PRINT_JS` expands them.
* The paper is much narrower than a desktop viewport. Laying out at 1280px and
  then printing to A4 re-flows everything into ~700px, so charts sized in
  pixels at init time (ECharts bakes the container size into the canvas) hang
  off the right edge and get cut. We lay out at the real printable width
  instead, then re-size the charts.
* Print layout has no relayout hook. `page.pdf()` does not run ResizeObserver
  callbacks, so anything that needs measuring must be measured and settled
  BEFORE the PDF call.

On top of that a dashboard is authored as one view, so it should arrive as one
page: `_fit_to_page` widens the layout until the content's aspect ratio matches
the sheet, then hands `page.pdf()` a matching scale. Past `MIN_SCALE` a single
page stops being readable, so we fall back to full-size pagination.
"""

import asyncio
import json
import logging
import math
import os
import signal
import uuid
from pathlib import Path
from typing import Any, Optional

from app.services.artifact_libs import get_inline_scripts

logger = logging.getLogger(__name__)


_CSS_PX_PER_IN = 96
# A4 portrait, in inches.
_PAPER_IN = (8.27, 11.69)
_MARGIN_IN = 0.4


def _page_geometry(landscape: bool) -> tuple[float, float, int, int]:
    """Return (paper_w_in, paper_h_in, content_w_px, content_h_px).

    The px values are the printable box in CSS pixels — the width Chromium
    lays the document out at when printing with scale 1.
    """
    w_in, h_in = _PAPER_IN
    if landscape:
        w_in, h_in = h_in, w_in
    content_w = round((w_in - 2 * _MARGIN_IN) * _CSS_PX_PER_IN)
    content_h = round((h_in - 2 * _MARGIN_IN) * _CSS_PX_PER_IN)
    return w_in, h_in, content_w, content_h


# Runs in the page right before measuring/printing. Takes the printable page
# height in CSS px so it only marks blocks that can actually fit on a page as
# unbreakable (telling Chromium to avoid breaking inside something taller than
# the sheet just moves the overflow around).
_PREPARE_PRINT_JS = """
(pageHeightPx) => {
  const stats = {expanded: 0, unstuck: 0, unbreakable: 0};

  // 1. Un-clip anything that is actually hiding content.
  //    Only elements that BOTH clip and have content beyond the clip are
  //    touched, so decorative overflow:hidden (rounded corners, masks) and
  //    ellipsis truncation keep their intended look. Expanding one container
  //    can push content past its parent, hence the repeated passes.
  for (let pass = 0; pass < 5; pass++) {
    let changed = 0;
    const all = document.querySelectorAll('*');
    for (let i = 0; i < all.length; i++) {
      const el = all[i];
      const cs = getComputedStyle(el);
      if (cs.textOverflow === 'ellipsis') continue;
      const clipsX = cs.overflowX !== 'visible';
      const clipsY = cs.overflowY !== 'visible';
      if (!clipsX && !clipsY) continue;
      const hidesX = clipsX && el.scrollWidth > el.clientWidth + 1;
      const hidesY = clipsY && el.scrollHeight > el.clientHeight + 1;
      if (!hidesX && !hidesY) continue;
      el.style.setProperty('overflow', 'visible', 'important');
      if (cs.maxHeight !== 'none') el.style.setProperty('max-height', 'none', 'important');
      if (cs.maxWidth !== 'none') el.style.setProperty('max-width', 'none', 'important');
      if (hidesY && cs.height !== 'auto') el.style.setProperty('height', 'auto', 'important');
      changed++; stats.expanded++;
    }
    if (!changed) break;
  }

  // 2. A max-width that the content has outgrown (a centered `max-w-7xl`
  //    wrapper around a 14-column table) does not clip — the content just
  //    spills out of its card and off the sheet. Let the box grow instead.
  {
    const all = document.querySelectorAll('*');
    for (let i = 0; i < all.length; i++) {
      const el = all[i];
      if (getComputedStyle(el).maxWidth === 'none') continue;
      if (el.scrollWidth <= el.clientWidth + 1) continue;
      el.style.setProperty('max-width', 'none', 'important');
      stats.widened = (stats.widened || 0) + 1;
    }
  }

  // 3. position:sticky has no meaning on paper and makes Chromium repeat or
  //    strand headers mid-flow.
  const all = document.querySelectorAll('*');
  for (let i = 0; i < all.length; i++) {
    if (getComputedStyle(all[i]).position === 'sticky') {
      all[i].style.setProperty('position', 'static', 'important');
      stats.unstuck++;
    }
  }

  // 4. Keep cards, charts and table rows whole across page boundaries.
  const blocks = document.querySelectorAll(
    'table, tr, canvas, img, figure, [class*="card"], [class*="Card"], [class*="rounded"]'
  );
  for (let i = 0; i < blocks.length; i++) {
    const h = blocks[i].getBoundingClientRect().height;
    if (h > 0 && h <= pageHeightPx) {
      blocks[i].style.setProperty('break-inside', 'avoid', 'important');
      stats.unbreakable++;
    } else {
      // Taller than the sheet after a relayout: keeping it unbreakable would
      // only push the overflow around.
      blocks[i].style.removeProperty('break-inside');
    }
  }
  return stats;
}
"""

# ECharts sizes its canvas from the container at init and only re-reads it on
# resize; page.pdf() never fires one, so a chart laid out at another width would
# print at that width. Animations are switched off at the same time: a print is
# a single instant, and catching a chart mid-animation puts a half-drawn series
# in the export.
_RESIZE_CHARTS_JS = """
() => {
  if (typeof echarts === 'undefined') return 0;
  let n = 0;
  document.querySelectorAll('[_echarts_instance_]').forEach((el) => {
    const chart = echarts.getInstanceByDom(el);
    if (!chart) return;
    try { chart.setOption({animation: false, animationDurationUpdate: 0}, false); } catch (e) {}
    try { chart.resize(); n++; } catch (e) {}
  });
  window.dispatchEvent(new Event('resize'));
  return n;
}
"""

# scrollWidth/scrollHeight on the root under-report: content that escapes a
# centred, width-capped wrapper does not always grow the root's scrollable
# overflow, and a PDF cut off exactly there is the bug this module exists to
# fix. Fall back to the furthest painted edge of any element.
_MEASURE_JS = """
() => {
  const de = document.documentElement, b = document.body;
  let w = Math.max(de.scrollWidth, b ? b.scrollWidth : 0);
  let h = Math.max(de.scrollHeight, b ? b.scrollHeight : 0);
  const sx = window.scrollX, sy = window.scrollY;
  const all = document.querySelectorAll('body *');
  for (let i = 0; i < all.length; i++) {
    const cs = getComputedStyle(all[i]);
    if (cs.display === 'none' || cs.visibility === 'hidden' || cs.position === 'fixed') continue;
    const r = all[i].getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) continue;
    if (r.right + sx > w) w = r.right + sx;
    if (r.bottom + sy > h) h = r.bottom + sy;
  }
  return {w: Math.ceil(w), h: Math.ceil(h)};
}
"""

# Injected into every rendered artifact. Complements _PREPARE_PRINT_JS with the
# rules that are cheaper to express as CSS than to walk the DOM for.
_PRINT_CSS = """
  html, body, #root { min-height: 100%; margin: 0; padding: 0; }
  @media print {
    /* height:100% pins the document to one viewport box; on paper the
       document has to be free to run as long as its content. */
    html, body, #root { height: auto !important; }
    body { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
    thead { display: table-header-group; }
    tfoot { display: table-footer-group; }
    tr, img, canvas, svg { break-inside: avoid; }
    /* Scroll affordances are noise on paper. */
    ::-webkit-scrollbar { display: none; }
  }
"""


def _kill_chromium_tree(marker: str) -> int:
    """SIGKILL the Chromium launched with `marker` in its argv, descendants first.

    When a render times out, the task cancellation can interrupt Playwright's
    own teardown, and even a clean teardown cannot stop a renderer whose main
    thread is spinning in JS: it never services the shutdown IPC, survives its
    parent's death, reparents to init and burns a core forever. So the process
    tree has to be walked and killed leaf-first while the parent links are
    still intact. /proc-based and best-effort: on hosts without /proc (deploys
    run on Linux) this is a silent no-op.
    """
    killed = 0
    try:
        children_by_parent: dict[int, list[int]] = {}
        marked: list[int] = []
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            pid = int(entry)
            try:
                cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().decode("utf-8", "replace")
                stat = Path(f"/proc/{pid}/stat").read_text()
            except OSError:
                continue
            try:
                # Field 4 of /proc/pid/stat, after the parenthesised comm
                # (which may itself contain spaces).
                ppid = int(stat.rsplit(")", 1)[1].split()[1])
            except (IndexError, ValueError):
                continue
            children_by_parent.setdefault(ppid, []).append(pid)
            if marker in cmdline:
                marked.append(pid)

        def _descendants(pid: int):
            for child in children_by_parent.get(pid, []):
                yield from _descendants(child)
                yield child

        for root in marked:
            for pid in [*_descendants(root), root]:
                try:
                    os.kill(pid, signal.SIGKILL)
                    killed += 1
                except OSError:
                    pass
    except OSError:
        pass
    return killed


class ReportPdfService:
    """Generates PDF snapshots of artifacts using headless Chromium."""

    UPLOADS_DIR = Path(__file__).parent.parent.parent / "uploads" / "pdfs"
    PPTX_DIR = Path(__file__).parent.parent.parent / "uploads" / "pptx"

    # Below this the one-page fit is too small to read, so we stop shrinking
    # and let the dashboard paginate at full size instead.
    MIN_SCALE = 0.4
    # How many widen-and-remeasure rounds the fit search gets.
    MAX_FIT_PASSES = 4
    # Hard cap on the layout box. Content wider than this is scaled down rather
    # than laid out at an absurd width.
    MAX_LAYOUT_WIDTH = 4000
    # Breathing room added when content forces the layout wider than the page.
    OVERFLOW_GUTTER_PX = 32
    # Seconds to let ECharts re-render after each layout change.
    SETTLE_SECONDS = 1.2
    # Hard wall-clock cap on one Chromium render. The individual Playwright
    # calls each have their own timeout, but page.pdf() does not — a wedged
    # renderer (huge canvas at device_scale_factor=2, OOMing tab) would
    # otherwise hang the awaiting request forever, and with it every caller
    # queued on the per-report export lock.
    RENDER_TIMEOUT_SECONDS = 150

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
            from playwright.async_api import async_playwright  # noqa: F401
        except ImportError:
            logger.warning("Playwright not installed, skipping PDF generation")
            return None

        # Unique argv marker so a timed-out render's Chromium tree can be
        # found and killed — cancellation interrupts Playwright's teardown,
        # and a wedged renderer survives even a clean one (see
        # _kill_chromium_tree). Chromium ignores unknown switches.
        marker = f"--bow-pdf-export={uuid.uuid4().hex}"
        try:
            return await asyncio.wait_for(
                self._generate_pdf_inner(artifact_id, html_content, marker),
                timeout=self.RENDER_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            logger.error(
                "PDF render for artifact %s exceeded %ss; aborting",
                artifact_id, self.RENDER_TIMEOUT_SECONDS,
            )
            killed = await asyncio.to_thread(_kill_chromium_tree, marker)
            if killed:
                logger.info(
                    "Killed %s leftover Chromium processes for artifact %s",
                    killed, artifact_id,
                )
            return None
        except Exception as e:
            logger.exception(f"Failed to generate PDF for artifact {artifact_id}: {e}")
            await asyncio.to_thread(_kill_chromium_tree, marker)
            return None

    async def _generate_pdf_inner(
        self, artifact_id: str, html_content: str, marker: str
    ) -> Optional[str]:
        """The actual render. Callers go through generate_pdf, which bounds this
        with RENDER_TIMEOUT_SECONDS and turns any failure into None."""
        from playwright.async_api import async_playwright

        pdf_path = self.UPLOADS_DIR / f"{artifact_id}.pdf"

        # Dashboards are authored as wide grids, so a landscape sheet needs the
        # least shrink to hold one. (Only page-mode artifacts reach this path;
        # decks convert from their .pptx and docs are refused.)
        paper_w_in, paper_h_in, content_w, content_h = _page_geometry(landscape=True)

        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True, args=[marker])
                # Lay out at the printable width from the start: responsive
                # breakpoints and every JS-measured chart size are then computed
                # for the paper, not for a desktop window that does not exist.
                # device_scale_factor=2 keeps canvas-based charts crisp — they
                # are rasters in the PDF, unlike the text around them.
                page = await browser.new_page(
                    viewport={"width": content_w, "height": content_h},
                    device_scale_factor=2,
                )
                await page.emulate_media(media="print")

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

                layout_w, scale = await self._fit_to_page(
                    page, artifact_id, content_w, content_h
                )

                pdf_bytes = await page.pdf(
                    width=f"{paper_w_in}in",
                    height=f"{paper_h_in}in",
                    print_background=True,
                    scale=scale,
                    margin={
                        "top": f"{_MARGIN_IN}in", "bottom": f"{_MARGIN_IN}in",
                        "left": f"{_MARGIN_IN}in", "right": f"{_MARGIN_IN}in",
                    },
                )

                await browser.close()

            pdf_path.write_bytes(pdf_bytes)
            logger.info(
                "Rendered PDF for artifact %s: layout_width=%spx scale=%.3f",
                artifact_id, layout_w, scale,
            )
            return f"pdfs/{artifact_id}.pdf"

        except Exception as e:
            logger.exception(f"Failed to generate PDF for artifact {artifact_id}: {e}")
            return None

    async def _layout_at(self, page, width: int, content_h: int) -> tuple[int, dict]:
        """Settle the document at a layout width and report what it measures.

        If the content turns out to need MORE width than it was given — an
        un-clipped nowrap table, an image with an intrinsic size — it is given
        that width and re-settled rather than left to be cut at the page edge.
        The re-settle matters: charts read their container size once, so a
        width the charts never saw would print them at the wrong size.
        """
        measured: dict[str, Any] = {"w": width, "h": content_h}
        for _ in range(3):
            width = max(1, min(width, self.MAX_LAYOUT_WIDTH))
            await page.set_viewport_size({"width": width, "height": content_h})
            await self._settle(page, content_h)
            measured = await page.evaluate(_MEASURE_JS)
            if measured["w"] <= width + 1 or width >= self.MAX_LAYOUT_WIDTH:
                break
            # A block does not apply its padding to content that overflows it,
            # so a table given exactly its own width ends up with its last
            # column border flush against the card edge and the page margin —
            # it reads as cut even though nothing is missing. Add a gutter.
            width = measured["w"] + self.OVERFLOW_GUTTER_PX
        return width, measured

    async def _fit_to_page(
        self, page, artifact_id: str, content_w: int, content_h: int
    ) -> tuple[int, float]:
        """Un-clip the document and find a layout width + scale that fits a page.

        Chromium lays a printed page out at `content_w / scale` CSS px, so
        choosing a scale is the same as choosing a layout width — and a wider
        layout is what buys height, because responsive grids reflow into more
        columns and get shorter. Search over layout width, then report the
        matching scale.

        Returns (layout_width_px, scale).
        """
        max_fit_w = round(content_w / self.MIN_SCALE)
        layout_w = content_w
        fits = False

        for attempt in range(1, self.MAX_FIT_PASSES + 1):
            layout_w, measured = await self._layout_at(page, layout_w, content_h)

            # A document H tall and W wide fits one sheet at layout width L
            # when H <= content_h * L / content_w and W <= L. Solve for L.
            # If the layout reflows with width, H shrinks as L grows and the
            # next pass converges downward; if it does not (fixed-height charts,
            # long tables), this is already the exact answer.
            needed_w = max(measured["w"], content_w * measured["h"] / content_h)
            fits = needed_w <= layout_w + 1
            logger.debug(
                "PDF fit pass %s for %s: layout_w=%s doc=%sx%s needed_w=%.0f fits=%s",
                attempt, artifact_id, layout_w, measured["w"], measured["h"],
                needed_w, fits,
            )
            if fits or needed_w > max_fit_w:
                break
            if abs(needed_w - layout_w) / layout_w < 0.02:
                # Converged without quite fitting; take it rather than oscillate.
                layout_w = math.ceil(needed_w)
                layout_w, measured = await self._layout_at(page, layout_w, content_h)
                fits = measured["w"] <= layout_w + 1 and (
                    measured["h"] <= content_h * layout_w / content_w + 1
                )
                break
            layout_w = math.ceil(needed_w)

        if not fits:
            # One page would mean shrinking past legibility. Go back to full
            # size and let it paginate — still full-width, still nothing
            # clipped, just more than one sheet.
            logger.info(
                "Artifact %s does not fit one page above scale %.2f; "
                "paginating at full size", artifact_id, self.MIN_SCALE,
            )
            layout_w, _ = await self._layout_at(page, content_w, content_h)

        scale = max(0.1, min(1.0, content_w / layout_w))
        return layout_w, round(scale, 4)

    async def _settle(self, page, content_h: int) -> None:
        """Prepare the DOM for paper and give the charts time to re-render."""
        try:
            stats = await page.evaluate(_PREPARE_PRINT_JS, content_h)
            logger.debug("PDF print prep: %s", stats)
        except Exception as e:
            logger.warning("PDF print prep failed: %s", e)
        try:
            await page.evaluate(_RESIZE_CHARTS_JS)
        except Exception:
            pass
        await asyncio.sleep(self.SETTLE_SECONDS)

    async def generate_for_report(self, report_id: str) -> Optional[str]:
        """Generate a PDF for the latest artifact of a report.

        Returns:
            Absolute filesystem path to the PDF, or None on failure.
        """
        try:
            from app.dependencies import async_session_maker
            from app.models.artifact import Artifact
            from app.services.viewer_data_policy import report_snapshot_withheld
            from sqlalchemy import select

            async with async_session_maker() as db:
                # A report-level PDF renders the shared Step.data snapshot with
                # no user in scope. In viewer-identity mode on user-scoped
                # connections that snapshot is credential-differentiated creator
                # data — refuse (callers skip the attachment). Owner-initiated
                # per-artifact exports use generate_for_artifact, not this path.
                if await report_snapshot_withheld(db, str(report_id)):
                    logger.info(
                        "Refusing report PDF for %s: snapshot withheld "
                        "(viewer-identity mode on user-scoped connections)", report_id,
                    )
                    return None

                # Prefer the dashboard. A deck can still be exported (via its
                # .pptx, see _render_slides_pdf), but a report that has both
                # should send the dashboard: that is what the report view shows.
                artifact = None
                for wanted_mode in ("page", "slides"):
                    stmt = (
                        select(Artifact)
                        .where(
                            Artifact.report_id == report_id,
                            Artifact.deleted_at.is_(None),
                            # Docs export via generate_for_artifact with the
                            # doc's id, never as the report-level PDF.
                            Artifact.mode == wanted_mode,
                        )
                        .order_by(Artifact.created_at.desc())
                        .limit(1)
                    )
                    result = await db.execute(stmt)
                    artifact = result.scalar_one_or_none()
                    if artifact is not None and artifact.content:
                        break
                    artifact = None

                if not artifact:
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
        """Dispatch to the renderer that matches the artifact's mode.

        Returns the absolute filesystem path to the PDF, or None on failure.
        """
        mode = (artifact.mode or "page").lower()

        if mode == "slides":
            # A slides artifact's content['code'] is python-pptx source, not
            # HTML — feeding it to the HTML renderer produced a blank page.
            # The deck itself is the artifact; convert that.
            return await self._render_slides_pdf(artifact)

        if mode == "doc":
            # Docs are markdown + {{viz:...}} placeholders rendered client-side
            # by DocViewer.vue; there is no server-side renderer for them, and
            # the HTML path below would emit an empty page (docs have no
            # content['code']). Fail instead of attaching a blank PDF.
            logger.warning(
                "PDF export is not supported for doc artifact %s "
                "(no server-side markdown renderer)", artifact.id,
            )
            return None

        return await self._render_page_pdf(db, artifact)

    async def _render_slides_pdf(self, artifact) -> Optional[str]:
        """Convert a slides artifact's generated .pptx to PDF via LibreOffice."""
        from app.data_sources.clients._office_convert import office_to_pdf_bytes

        src = None
        if artifact.pptx_path:
            candidate = Path(artifact.pptx_path)
            if candidate.is_file():
                src = candidate
        if src is None:
            candidate = self.PPTX_DIR / f"{artifact.id}.pptx"
            if candidate.is_file():
                src = candidate
        if src is None:
            logger.warning("No .pptx on disk for slides artifact %s", artifact.id)
            return None

        try:
            raw = src.read_bytes()
        except OSError as e:
            logger.warning("Could not read deck for slides artifact %s: %s", artifact.id, e)
            return None

        # Blocking subprocess — off the event loop. The shared helper already
        # handles the per-call LibreOffice profile (concurrent conversions
        # otherwise block each other) and every failure mode.
        data = await asyncio.to_thread(office_to_pdf_bytes, raw, src.name)
        if not data:
            logger.warning(
                "LibreOffice could not convert the deck for slides artifact %s; "
                "sending without a PDF attachment", artifact.id,
            )
            return None

        dest = self.UPLOADS_DIR / f"{artifact.id}.pdf"
        dest.write_bytes(data)
        return str(dest)

    async def _render_page_pdf(self, db, artifact) -> Optional[str]:
        """Build the dashboard HTML (with its report's data) and render to PDF.

        Returns the absolute filesystem path to the PDF, or None on failure.
        """
        from app.services.artifact_payload import collect_artifact_payload

        payload = await collect_artifact_payload(db, artifact)
        if payload is None:
            return None

        html_content = self._build_pdf_html(
            artifact_data=payload,
            artifact_code=artifact.content.get("code", ""),
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
        artifact_data: dict,
        artifact_code: str,
        mode: str = "page",
    ) -> str:
        """Build the standalone HTML the headless browser renders for print.

        artifact_data comes from services.artifact_payload.collect_artifact_payload
        — shared with the standalone HTML export so both stay in step. PDF
        exports are shareable documents, so it renders anonymously
        (current_user is None).
        """
        data_json = json.dumps(artifact_data, default=str)

        page_scripts = get_inline_scripts(mode="page")

        return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  {page_scripts}
  <style>{_PRINT_CSS}</style>
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
