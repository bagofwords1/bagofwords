"""Helper for loading vendored JS libraries for artifact rendering in headless browser.

In airgapped deployments, CDN URLs are not available. This module reads the
vendored JS files from disk and returns them as inline <script> tags for use
with Playwright's page.set_content() (which renders at about:blank and cannot
resolve relative paths).
"""

import logging
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

# Paths where vendored libs may be found (checked in order):
# 1. Nuxt build output (production Docker image)
# 2. Frontend public dir (local development / Docker with public copied)
_CANDIDATE_DIRS = [
    Path(__file__).parent.parent.parent.parent / "frontend" / ".output" / "public" / "libs",
    Path(__file__).parent.parent.parent.parent / "frontend" / "public" / "libs",
]

# Libraries needed for dashboard (page) mode artifacts
_PAGE_LIBS = [
    "tailwindcss-3.4.16.js",
    "react-18.development.js",
    "react-dom-18.development.js",
    "babel-standalone.min.js",
    "echarts-5.min.js",
]

# Libraries needed for slides mode artifacts
_SLIDES_LIBS = [
    "tailwindcss-3.4.16.js",
]

# Libraries for the standalone HTML export. Two deliberate differences from
# _PAGE_LIBS: React ships as the PRODUCTION build (10KB + 132KB instead of
# 110KB + 1.1MB — the dev builds only exist for readable in-app errors), and
# babel-standalone (2.4MB) is absent because the export pre-transpiles the
# artifact's JSX server-side. Together that takes a download from ~4MB to
# ~1.6MB before data.
_EXPORT_LIBS = [
    "tailwindcss-3.4.16.js",
    "react-18.production.min.js",
    "react-dom-18.production.min.js",
    "echarts-5.min.js",
]

# Only inlined when the artifact actually embeds a PDF — 320KB of viewer plus
# a 1MB worker is not worth carrying in every export.
_PDF_LIB = "pdf.min.js"
_PDF_WORKER_LIB = "pdf.worker.min.js"


_GLOBALS_FILENAME = "artifact-globals.js"
_OFFLINE_HOST_FILENAME = "artifact-offline-host.js"


def _read_globals() -> str:
    """Read the shared artifact-globals.js from the vendored libs directory.

    The runtime surface (useParams, useCurrentUser, ...) must match what the
    generation prompt teaches, so this always reads the FRESHEST copy — see
    _find_libs_dir. Cached per (path, mtime) so a rebuild is picked up without
    restarting the backend.
    """
    libs_dir = _find_libs_dir()
    if libs_dir is None:
        raise FileNotFoundError(
            "Vendored JS libs directory not found. "
            "Run scripts/download-vendor-libs.sh during Docker build."
        )
    path = libs_dir / _GLOBALS_FILENAME
    return _read_file_cached(str(path), path.stat().st_mtime)


def _find_libs_dir() -> Path | None:
    """Find the directory containing vendored JS libraries.

    When several candidates exist, prefer the one whose artifact-globals.js is
    NEWEST. A developer checkout often carries a stale frontend/.output build
    next to a current public/libs — validating against the stale runtime made
    every artifact using a newer global (e.g. useParams) fail with a phantom
    "X is not defined" and burn the whole repair loop on correct code.
    """
    candidates = [d for d in _CANDIDATE_DIRS if d.is_dir() and any(d.iterdir())]
    if not candidates:
        return None

    def _globals_mtime(d: Path) -> float:
        try:
            return (d / _GLOBALS_FILENAME).stat().st_mtime
        except OSError:
            return -1.0

    return max(candidates, key=_globals_mtime)


@lru_cache(maxsize=32)
def _read_file_cached(path_str: str, mtime: float) -> str:
    """Read a file, cached by (path, mtime) so updated files are re-read."""
    return Path(path_str).read_text(encoding="utf-8")


def _read_lib(libs_dir: Path, filename: str) -> str:
    """Read a vendored JS file and return its contents."""
    path = libs_dir / filename
    return _read_file_cached(str(path), path.stat().st_mtime)


def get_inline_scripts(mode: str = "page") -> str:
    """Return inline <script> tags with vendored JS library contents.

    Args:
        mode: 'page' for React/Babel/ECharts dashboard, 'slides' for Tailwind-only.

    Returns:
        HTML string with <script>...</script> tags containing the library code.

    Raises:
        FileNotFoundError: If vendored libs directory or individual files are missing.
            In airgapped deployments there is no CDN to fall back to, so missing
            vendored files must fail loudly.
    """
    libs_dir = _find_libs_dir()

    if libs_dir is None:
        raise FileNotFoundError(
            "Vendored JS libs directory not found. "
            "Run scripts/download-vendor-libs.sh during Docker build."
        )

    lib_files = _PAGE_LIBS if mode == "page" else _SLIDES_LIBS
    parts = []

    for filename in lib_files:
        content = _read_lib(libs_dir, filename)  # raises FileNotFoundError if missing
        parts.append(f"<script>{content}</script>")

    # Add global setup for page mode (hooks, EChart wrapper, filters, etc.)
    if mode == "page":
        parts.append(f"<script>{_read_globals()}</script>")

    return "\n".join(parts)


def _require_libs_dir() -> Path:
    """Locate the vendored libs dir or raise the standard airgap error."""
    libs_dir = _find_libs_dir()
    if libs_dir is None:
        raise FileNotFoundError(
            "Vendored JS libs directory not found. "
            "Run scripts/download-vendor-libs.sh during Docker build."
        )
    return libs_dir


def get_export_vendor_scripts(include_pdf: bool = False) -> str:
    """Inline <script> tags for the standalone HTML export's vendor libs.

    Unlike get_inline_scripts, this deliberately does NOT append
    artifact-globals.js: the export has to set window.ARTIFACT_DATA *between*
    the vendor libs and the globals, because artifact-globals.js ingests
    ARTIFACT_DATA into the param store at load time. The caller sequences the
    parts via get_globals_script / get_offline_host_script.

    Raises:
        FileNotFoundError: if the vendored libs are missing. There is no CDN
            fallback — a "standalone" export that reaches the network is worse
            than no export.
    """
    libs_dir = _require_libs_dir()

    files = list(_EXPORT_LIBS)
    if include_pdf:
        files.append(_PDF_LIB)

    return "\n".join(f"<script>{_read_lib(libs_dir, f)}</script>" for f in files)


def get_globals_script() -> str:
    """The shared artifact runtime (useArtifactData, useFilters, EChart, ...)."""
    return f"<script>{_read_globals()}</script>"


def get_offline_host_script() -> str:
    """The offline params host — stands in for the authenticated parent page.

    In the app the artifact runs in an iframe and posts param changes to
    ArtifactFrame.vue, which re-runs the queries server-side. A standalone
    export has no such host, so this shim answers those messages in-browser.
    See frontend/public/libs/artifact-offline-host.js.

    Unlike the other readers this scans EVERY candidate dir rather than only
    the preferred one. _find_libs_dir ranks by artifact-globals.js mtime, so a
    developer checkout carrying a frontend/.output build from before this file
    existed would win the ranking and take the export down with a missing-file
    error, even though a perfectly good copy sits in frontend/public/libs.
    """
    _require_libs_dir()  # keep the standard airgap error when nothing is set up

    for candidate in sorted(
        (d for d in _CANDIDATE_DIRS if (d / _OFFLINE_HOST_FILENAME).is_file()),
        key=lambda d: (d / _OFFLINE_HOST_FILENAME).stat().st_mtime,
        reverse=True,
    ):
        path = candidate / _OFFLINE_HOST_FILENAME
        return f"<script>{_read_file_cached(str(path), path.stat().st_mtime)}</script>"

    raise FileNotFoundError(
        f"{_OFFLINE_HOST_FILENAME} not found in any vendored libs directory. "
        "It is committed to the repo (see frontend/public/libs/.gitignore); "
        "rebuild the frontend so it reaches the Nuxt output."
    )


def read_pdf_worker_source() -> str:
    """Raw pdf.js worker source, for inlining as a blob: URL in the export."""
    return _read_lib(_require_libs_dir(), _PDF_WORKER_LIB)
