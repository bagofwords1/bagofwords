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


_GLOBALS_FILENAME = "artifact-globals.js"


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
