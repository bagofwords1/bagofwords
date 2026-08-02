"""
Repro: a scheduled report emailed as PDF arrives with content cut off — the
right-hand columns of a wide table are gone, long tables stop after the rows
that happened to be visible, and the tail of the dashboard is missing.

Root cause: the PDF was rendered from the live dashboard HTML unchanged. A
dashboard is built for a scrolling screen — the codegen prompt tells the model
to wrap wide tables in `overflow-x-auto`, and the built-in TableCard caps its
height with `overflow-auto` — but a PDF page does not scroll, so everything
outside those boxes is discarded. The document was also laid out at a 1280px
viewport and then printed to a much narrower A4 portrait page, so charts sized
in pixels at init time hung off the right edge.

These tests pin the behaviour that fixes it:

  * clipped containers are expanded before printing, so every column of a wide
    table and every row of a scroll-capped table lands in the PDF;
  * the page is laid out at the printable width and a dashboard that can fit
    one sheet gets one sheet;
  * embedded files reach the renderer as data URIs (the headless browser
    cannot authenticate against /files/{id}/content);
  * artifact modes with no HTML to render fail instead of attaching a blank PDF.

The heavy cases need Playwright plus the vendored JS libs (downloaded at Docker
build time by scripts/download-vendor-libs.sh) and skip when either is absent.

Run:
    cd backend
    BOW_DATABASE_URL=sqlite:///db/app.db \
      python -m pytest tests/e2e/test_report_pdf_completeness.py -v -s
"""
import json
import re
import uuid
from pathlib import Path

import pytest

from app.dependencies import async_session_maker
from app.models.artifact import Artifact
from app.models.file import File
from app.models.organization import Organization
from app.models.query import Query
from app.models.report import Report
from app.models.step import Step
from app.models.user import User
from app.models.visualization import Visualization
from app.models.widget import Widget
from app.services.report_pdf_service import ReportPdfService

pytestmark = pytest.mark.e2e

NCOLS = 14
NROWS = 30


def _browser_stack_available() -> bool:
    """Can this machine actually render an artifact?

    Three separate things have to be present, and CI has only the first: the
    Playwright package (a backend dependency), a downloaded Chromium, and the
    vendored JS libs — which are gitignored and fetched at Docker build time by
    scripts/download-vendor-libs.sh. So check for the files themselves; the
    libs directory exists in a checkout but holds only artifact-globals.js.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False

    from app.services.artifact_libs import (
        _GLOBALS_FILENAME,
        _PAGE_LIBS,
        _find_libs_dir,
    )

    libs_dir = _find_libs_dir()
    if libs_dir is None:
        return False
    if not all((libs_dir / name).is_file() for name in (*_PAGE_LIBS, _GLOBALS_FILENAME)):
        return False

    try:
        with sync_playwright() as p:
            return Path(p.chromium.executable_path).exists()
    except Exception:
        return False


requires_browser = pytest.mark.skipif(
    not _browser_stack_available(),
    reason="needs Playwright + a downloaded Chromium + the vendored JS libs "
           "(scripts/download-vendor-libs.sh)",
)


# A dashboard written the way the codegen prompt asks for one: fluid outer
# container, wide table inside an overflow-x-auto wrapper, long table inside a
# height-capped scroll panel.
DASHBOARD_CODE = """<script type="text/babel">
function App() {
  const data = useArtifactData();
  if (!data) return <div className="h-screen"><LoadingSpinner /></div>;
  const viz = data.visualizations;
  const wide = viz[0];
  const long = viz[1];
  const cols = wide.columns.map(c => c.field);
  return (
    <div className="w-full min-h-full bg-slate-50 p-6">
      <div className="max-w-7xl mx-auto space-y-6">
        <h1 className="text-2xl font-bold">Coverage</h1>
        <SectionCard title="Wide table" viz={wide}>
          <div className="overflow-x-auto">
            <table className="min-w-full text-sm whitespace-nowrap">
              <thead><tr>{cols.map(c => <th key={c} className="px-4 py-2">{c}</th>)}</tr></thead>
              <tbody>{wide.rows.map((r, i) =>
                <tr key={i}>{cols.map(c => <td key={c} className="px-4 py-2">{r[c]}</td>)}</tr>)}
              </tbody>
            </table>
          </div>
        </SectionCard>
        <SectionCard title="Long table" viz={long}>
          <div className="overflow-y-auto" style={{ maxHeight: 240 }}>
            <table className="min-w-full text-sm">
              <tbody>{long.rows.map((r, i) =>
                <tr key={i}><td className="px-4 py-1">{r.label}</td></tr>)}
              </tbody>
            </table>
          </div>
        </SectionCard>
        <p className="text-xs">TAIL_OF_DASHBOARD</p>
      </div>
    </div>
  );
}
ReactDOM.createRoot(document.getElementById('root')).render(<App />);
</script>"""


async def _seed(mode: str = "page", code: str = DASHBOARD_CODE, files=None):
    """Seed a report with a wide viz and a long viz, plus an artifact."""
    suffix = uuid.uuid4().hex[:8]
    async with async_session_maker() as db:
        org = Organization(name=f"PDF Coverage Org {suffix}")
        db.add(org)
        await db.flush()

        user = User(
            name="Report Owner",
            email=f"coverage-{suffix}@example.com",
            hashed_password="x",
            is_active=True,
            is_superuser=False,
            is_verified=True,
        )
        db.add(user)
        await db.flush()

        report = Report(
            title="Coverage Report",
            slug=f"coverage-{suffix}",
            organization_id=org.id,
            user_id=user.id,
        )
        db.add(report)
        await db.flush()

        async def make_viz(key, title, rows, columns):
            widget = Widget(title=title, slug=f"w-{key}-{suffix}", report_id=report.id)
            db.add(widget)
            await db.flush()
            step = Step(
                title=f"{title} step", slug=f"s-{key}-{suffix}", status="success",
                widget_id=widget.id, data={"rows": rows, "columns": columns},
                data_model={"type": "table"},
            )
            db.add(step)
            await db.flush()
            query = Query(title=title, report_id=report.id, widget_id=widget.id,
                          default_step_id=step.id)
            db.add(query)
            await db.flush()
            step.query_id = query.id
            await db.flush()
            viz = Visualization(title=title, status="success", report_id=report.id,
                                query_id=query.id, view={"type": "table"})
            db.add(viz)
            await db.flush()
            return viz

        wide = await make_viz(
            "wide", "Wide table",
            rows=[{f"metric_{j:02d}": f"R{r}C{j:02d}" for j in range(NCOLS)} for r in range(3)],
            columns=[{"field": f"metric_{j:02d}"} for j in range(NCOLS)],
        )
        long = await make_viz(
            "long", "Long table",
            rows=[{"label": f"ROW_{i:03d}"} for i in range(NROWS)],
            columns=[{"field": "label"}],
        )

        content = {"code": code, "visualization_ids": [str(wide.id), str(long.id)]}
        if files is not None:
            content["files"] = files

        artifact = Artifact(
            report_id=report.id, user_id=user.id, organization_id=org.id,
            title="Dashboard", mode=mode, content=content,
        )
        db.add(artifact)
        await db.flush()
        await db.commit()
        return str(report.id), str(artifact.id), org, user


def _pdf_text(path):
    """(text, page_count, first_page_size) read back out of the rendered PDF."""
    import pypdfium2

    doc = pypdfium2.PdfDocument(path)
    text = "\n".join(page.get_textpage().get_text_range() for page in doc)
    return text, len(doc), doc[0].get_size()


@requires_browser
@pytest.mark.asyncio
async def test_scrollable_content_is_not_lost_in_the_pdf(tmp_path):
    """Every column of an overflow-x-auto table and every row of a height-capped
    scroll panel must appear in the export."""
    report_id, artifact_id, _, _ = await _seed()

    service = ReportPdfService()
    service.UPLOADS_DIR = tmp_path
    path = await service.generate_for_report(report_id)
    assert path, "PDF generation returned None"

    text, page_count, (page_w, page_h) = _pdf_text(path)

    missing_cols = [j for j in range(NCOLS) if f"metric_{j:02d}" not in text]
    missing_cells = [j for j in range(NCOLS) if f"R0C{j:02d}" not in text]
    missing_rows = [i for i in range(NROWS) if f"ROW_{i:03d}" not in text]

    assert not missing_cols, f"wide-table headers missing from PDF: {missing_cols}"
    assert not missing_cells, f"wide-table cells missing from PDF: {missing_cells}"
    assert not missing_rows, f"scroll-panel rows missing from PDF: {missing_rows}"
    assert "TAIL_OF_DASHBOARD" in text, "the end of the dashboard never made it in"

    # Landscape sheet: a dashboard is a wide grid, and the page is chosen to suit it.
    assert page_w > page_h, "dashboard PDF should be landscape"


@requires_browser
@pytest.mark.asyncio
async def test_dashboard_that_can_fit_gets_a_single_page(tmp_path):
    """A dashboard authored as one view arrives as one page when it can be
    scaled to fit above the legibility floor."""
    report_id, _, _, _ = await _seed()

    service = ReportPdfService()
    service.UPLOADS_DIR = tmp_path
    path = await service.generate_for_report(report_id)
    assert path

    text, page_count, _ = _pdf_text(path)
    assert page_count == 1, (
        f"expected the dashboard to fit one page, got {page_count}"
    )
    assert "TAIL_OF_DASHBOARD" in text


@pytest.mark.asyncio
async def test_embedded_files_are_inlined_for_the_headless_render(monkeypatch, tmp_path):
    """<BowFile> content is fetched over an authenticated route the headless
    browser cannot use, so it has to be inlined as a data URI."""
    raw = b"\x89PNG\r\n\x1a\nfake-png-bytes"
    uploads = tmp_path / "uploads" / "files"
    uploads.mkdir(parents=True)
    (uploads / "embedded.png").write_bytes(raw)
    monkeypatch.chdir(tmp_path)

    report_id, _, org, user = await _seed()
    async with async_session_maker() as db:
        f = File(filename="embedded.png", path="uploads/files/embedded.png",
                 content_type="image/png", user_id=str(user.id),
                 organization_id=str(org.id))
        db.add(f)
        await db.commit()
        file_id = str(f.id)

    report_id, _, _, _ = await _seed(
        files=[{"id": file_id, "content_type": "image/png", "filename": "embedded.png"}]
    )

    captured = {}

    async def fake_generate_pdf(self, artifact_id, html_content, mode="page"):
        captured["html"] = html_content
        return f"pdfs/{artifact_id}.pdf"

    monkeypatch.setattr(ReportPdfService, "generate_pdf", fake_generate_pdf)
    import app.services.report_pdf_service as pdf_mod
    monkeypatch.setattr(pdf_mod, "get_inline_scripts", lambda mode="page": "")

    service = ReportPdfService()
    assert await service.generate_for_report(report_id)

    m = re.search(r"window\.ARTIFACT_DATA\s*=\s*(\{.*?\});", captured["html"], re.S)
    data = json.loads(m.group(1))
    files = data.get("files") or []
    assert len(files) == 1, "embedded files must be injected into ARTIFACT_DATA"
    assert files[0]["dataUri"].startswith("data:image/png;base64,"), (
        "embedded file must be inlined as a data URI, not left for an authenticated fetch"
    )


@pytest.mark.asyncio
async def test_doc_artifact_is_refused_rather_than_exported_blank():
    """A doc's body is markdown rendered client-side; the HTML renderer has no
    code to run for it and would emit an empty page."""
    _, artifact_id, _, _ = await _seed(mode="doc", code="")
    async with async_session_maker() as db:
        artifact = await db.get(Artifact, artifact_id)
        artifact.content = {"markdown": "# Title\n\nBody", "visualization_ids": []}
        await db.commit()

    assert await ReportPdfService().generate_for_artifact(artifact_id) is None


@pytest.mark.asyncio
async def test_slides_artifact_without_a_deck_is_refused(monkeypatch, tmp_path):
    """A slides artifact stores python-pptx source, not HTML. With no .pptx on
    disk there is nothing to convert — and feeding the Python to the HTML
    renderer produced a blank page."""
    _, artifact_id, _, _ = await _seed(mode="slides", code="prs = Presentation()")

    service = ReportPdfService()
    service.UPLOADS_DIR = tmp_path
    service.PPTX_DIR = tmp_path / "pptx"
    service.PPTX_DIR.mkdir()

    called = {}

    async def fail_generate_pdf(self, *a, **k):
        called["html_renderer"] = True
        return None

    monkeypatch.setattr(ReportPdfService, "generate_pdf", fail_generate_pdf)

    assert await service.generate_for_artifact(artifact_id) is None
    assert "html_renderer" not in called, (
        "slides must never be routed through the HTML renderer"
    )
