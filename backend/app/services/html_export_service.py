"""Standalone, fully offline HTML export of a page-mode dashboard artifact.

The product already renders these dashboards from a nearly self-contained
document — buildArtifactIframeHtml in frontend/utils/artifactIframe.ts inlines
the data and pulls the runtime from /libs/*.js. This service closes the
remaining gap so the result survives with no server, no network and no auth:

  * every vendored library is inlined rather than linked,
  * embedded images and PDFs become data: URIs,
  * the pdf.js worker becomes a blob: URL (its hardcoded /libs/ path is the one
    absolute URL baked into the runtime),
  * JSX is transpiled ahead of time so babel-standalone can be dropped,
  * an offline host answers the param messages that would otherwise hang,
  * and anything still pointing at a remote origin is stripped, with a warning.

The output must make ZERO network requests. tests/test_html_export.py enforces
that by opening a real export in a browser with every non-local request denied.
"""

import logging
import re
from datetime import datetime, timezone
from typing import Any

from app.services.artifact_libs import (
    get_export_vendor_scripts,
    get_globals_script,
    get_offline_host_script,
    read_pdf_worker_source,
)
from app.services.artifact_payload import collect_artifact_payload, collect_params
from app.services.jsx_transpile import (
    extract_babel_source,
    json_for_script,
    transpile_jsx,
    wrap_as_plain_script,
)

logger = logging.getLogger(__name__)


class ExportUnavailable(RuntimeError):
    """The export cannot be produced (vendored libs missing, report gone)."""


# Remote references the generator is not supposed to emit but occasionally
# does. Each would be a live network request in a file that promises none, so
# they are removed rather than trusted — a missing webfont degrades to the
# system stack, which is what the export CSS asks for anyway.
_REMOTE_PATTERNS: tuple[tuple[str, re.Pattern], ...] = (
    ("stylesheet link", re.compile(r"""<link\b[^>]*\bhref=["']https?://[^"']*["'][^>]*>""", re.I)),
    ("script src", re.compile(r"""<script\b[^>]*\bsrc=["']https?://[^"']*["'][^>]*>\s*</script>""", re.I)),
    ("css @import", re.compile(r"""@import\s+(?:url\()?["']?https?://[^"')]+["']?\)?\s*;""", re.I)),
)

# Kept separate: rewriting an <img> to nothing would collapse a layout, so the
# src is blanked in place and the alt text carries the meaning.
_REMOTE_IMG = re.compile(r"""(<img\b[^>]*\bsrc=)["']https?://[^"']*["']""", re.I)

_HTML_SHELL = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title}</title>
<meta name="generator" content="Bag of Words — standalone dashboard export">
{vendor_scripts}
<style>
  html, body, #root {{ height: 100%; margin: 0; padding: 0; }}
  body {{
    font-family: system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    background: #fff;
  }}
  @media print {{ [data-bow-export-badge] {{ display: none !important; }} }}
</style>
</head>
<body>
<div id="root"></div>
{pdf_worker_script}
<script>
window.ARTIFACT_DATA = {data_json};
window.__BOW_INFO = false;
window.__BOW_OFFLINE_EXPORT_CONFIG = {config_json};
</script>
{globals_script}
{offline_host_script}
{artifact_script}
</body>
</html>
"""


def _strip_remote_references(code: str) -> tuple[str, list[str]]:
    """Remove references to remote origins from generated markup.

    Returns the cleaned code and a list of human-readable descriptions of what
    was removed, so the caller can log how often the generator does this.
    """
    removed: list[str] = []
    cleaned = code

    for label, pattern in _REMOTE_PATTERNS:
        hits = pattern.findall(cleaned)
        if hits:
            removed.append(f"{len(hits)} {label}(s)")
            cleaned = pattern.sub("", cleaned)

    img_hits = _REMOTE_IMG.findall(cleaned)
    if img_hits:
        removed.append(f"{len(img_hits)} remote image(s)")
        cleaned = _REMOTE_IMG.sub(r'\1""', cleaned)

    return cleaned, removed


def _pdf_worker_script() -> str:
    """Inline the pdf.js worker as a blob: URL and pin workerSrc to it.

    artifact-globals.js sets `GlobalWorkerOptions.workerSrc = '/libs/pdf.worker.min.js'`
    inside the <BowFile> render effect — an absolute path that resolves to
    nothing in a downloaded file. Because that assignment runs later, defining
    the property as a getter is what makes the override stick; a plain
    assignment here would simply be overwritten.
    """
    worker_json = json_for_script(read_pdf_worker_source())
    return f"""<script>
(function() {{
  if (typeof pdfjsLib === 'undefined') return;
  var src = URL.createObjectURL(new Blob([{worker_json}], {{ type: 'text/javascript' }}));
  try {{
    Object.defineProperty(pdfjsLib.GlobalWorkerOptions, 'workerSrc', {{
      get: function() {{ return src; }},
      set: function() {{}},
      configurable: true
    }});
  }} catch (e) {{
    pdfjsLib.GlobalWorkerOptions.workerSrc = src;
  }}
}})();
</script>"""


def _escape_title(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _has_pdf(files: list[dict[str, Any]]) -> bool:
    return any(
        str(f.get("content_type") or "").lower().startswith("application/pdf")
        for f in files or []
    )


async def build_standalone_html(db, artifact) -> str:
    """Render a page-mode artifact as one self-contained, offline HTML document.

    Raises:
        ExportUnavailable: vendored libs missing, or the artifact has no report
            or no code to render.
    """
    if (artifact.mode or "page") != "page":
        raise ExportUnavailable("Only page-mode artifacts can be exported as HTML")

    raw_code = (artifact.content or {}).get("code") or ""
    if not raw_code.strip():
        raise ExportUnavailable("This dashboard has no code to export")

    try:
        payload = await collect_artifact_payload(db, artifact)
    except FileNotFoundError as e:
        raise ExportUnavailable(str(e)) from e
    if payload is None:
        raise ExportUnavailable("The report backing this dashboard no longer exists")

    payload["params"] = await collect_params(db, artifact)

    code, removed = _strip_remote_references(raw_code)
    if removed:
        logger.warning(
            "HTML export for artifact %s stripped remote references: %s",
            artifact.id, ", ".join(removed),
        )

    artifact_script = await _build_artifact_script(code)

    try:
        include_pdf = _has_pdf(payload.get("files") or [])
        vendor_scripts = get_export_vendor_scripts(include_pdf=include_pdf)
        globals_script = get_globals_script()
        offline_host_script = get_offline_host_script()
        pdf_worker_script = _pdf_worker_script() if include_pdf else ""
    except FileNotFoundError as e:
        # No CDN fallback on purpose: a "standalone" file that silently reaches
        # the network is worse than a refused export.
        raise ExportUnavailable(
            "Dashboard export is unavailable because the vendored browser "
            "libraries are missing from this deployment."
        ) from e

    title = (artifact.title or "").strip() or (payload["report"].get("title") or "Dashboard")

    config = {
        "exportedAt": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "showBadge": True,
    }

    return _HTML_SHELL.format(
        title=_escape_title(title),
        vendor_scripts=vendor_scripts,
        pdf_worker_script=pdf_worker_script,
        data_json=json_for_script(payload),
        config_json=json_for_script(config),
        globals_script=globals_script,
        offline_host_script=offline_host_script,
        artifact_script=artifact_script,
    )


async def _build_artifact_script(code: str) -> str:
    """The artifact's own code, pre-transpiled when possible.

    Falls back to shipping the original `<script type="text/babel">` block plus
    an inlined babel-standalone. That is 2.4MB heavier but always correct, so a
    deployment without a usable headless browser still gets a working export.
    """
    source = extract_babel_source(code)
    if source is None:
        # Not the shape this pipeline produces — pass through untouched rather
        # than transpiling something we do not understand.
        logger.info("Artifact code carries no text/babel wrapper; embedding as-is")
        return code

    compiled = await transpile_jsx(source)
    if compiled is not None:
        return wrap_as_plain_script(compiled)

    from app.services.artifact_libs import _read_lib, _require_libs_dir

    logger.info("Falling back to inline babel-standalone for this export")
    babel = _read_lib(_require_libs_dir(), "babel-standalone.min.js")
    return f"<script>{babel}</script>\n{code}"


def suggested_filename(artifact) -> str:
    """A safe, descriptive download filename for this artifact.

    Unicode is preserved — the product ships RTL and Cyrillic locales, and the
    Content-Disposition RFC 5987 form carries these intact. See
    ascii_fallback_filename for the companion the plain `filename=` uses.
    """
    base = (artifact.title or "dashboard").strip()
    base = re.sub(r"[^\w\s-]", "", base, flags=re.UNICODE).strip()
    base = re.sub(r"[\s_]+", "-", base) or "dashboard"
    return f"{base[:80]}.html"


def ascii_fallback_filename(filename: str) -> str:
    """The plain `filename=` companion for a possibly non-Latin filename.

    A raw non-ASCII byte is invalid in an HTTP header, so the plain form has to
    be transliterated. Dropping non-ASCII from "דשבורד-מכירות.html" leaves
    "-.html" — punctuation the browser would save as a hidden, extensionless
    file — so a stem with no letters or digits left is replaced outright.
    """
    stem = filename[:-len(".html")] if filename.endswith(".html") else filename
    stem = stem.encode("ascii", "ignore").decode("ascii").strip()
    if not re.search(r"[A-Za-z0-9]", stem):
        stem = "dashboard"
    return f"{stem}.html"
