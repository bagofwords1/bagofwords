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


def _find_libs_dir() -> Path | None:
    """Find the directory containing vendored JS libraries."""
    for d in _CANDIDATE_DIRS:
        if d.is_dir() and any(d.iterdir()):
            return d
    return None


@lru_cache(maxsize=1)
def _read_lib(libs_dir: Path, filename: str) -> str:
    """Read a vendored JS file and return its contents."""
    path = libs_dir / filename
    return path.read_text(encoding="utf-8")


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

    return "\n".join(parts)
