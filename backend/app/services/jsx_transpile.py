"""Server-side JSX transpilation for standalone HTML exports.

In the app, artifact code ships to the browser untransformed inside a
`<script type="text/babel">` tag and babel-standalone compiles it on load.
That is fine for an iframe served by our own origin, but it costs 2.4MB in a
file the user downloads — more than the rest of the runtime combined — and
spends about a second of CPU every time the file is opened.

So the export transpiles ahead of time. There is no good pure-Python JSX
compiler, and adding Node to the backend image for one function is a poor
trade, so this reuses the two things already present: the vendored
babel-standalone bundle and Playwright (already a dependency of the artifact
screenshot and PDF paths). Babel runs inside a blank headless page and the
compiled source comes back as a string.

Every failure degrades rather than raises: the caller falls back to shipping
Babel inline, which is heavier but always correct.
"""

import asyncio
import hashlib
import json
import logging
import os
import re
from typing import Optional

from app.services.artifact_libs import _read_lib, _require_libs_dir

logger = logging.getLogger(__name__)

# Mirrors _ensure_app_wrapper in create_artifact.py — artifact content.code is
# stored as a complete `<script type="text/babel">...</script>` string.
_BABEL_SCRIPT_RE = re.compile(
    r'<script\s+type=["\']text/babel["\']\s*>\s*([\s\S]*?)\s*</script\s*>',
    re.IGNORECASE,
)

_TRANSPILE_TIMEOUT_SECONDS = 60

# Compiled output keyed by sha256 of the source. An artifact's code is
# immutable once saved (an edit creates a new version), so a hit is exact and
# never stale, and repeated downloads of the same dashboard skip the browser.
_cache: dict[str, str] = {}
_CACHE_MAX_ENTRIES = 64

# One in-flight transpile per source. The cache only fills once a transpile
# COMPLETES, so without this several viewers exporting the same dashboard at
# the same moment would each launch their own Chromium for identical work.
_locks: dict[str, asyncio.Lock] = {}


def extract_babel_source(code: str) -> Optional[str]:
    """Return the JSX inside a `<script type="text/babel">` wrapper.

    Returns None when the code carries no such wrapper, which means it is not
    the shape this pipeline produces and must not be transpiled blind.
    """
    if not code:
        return None
    match = _BABEL_SCRIPT_RE.search(code)
    if not match:
        return None
    inner = match.group(1).strip()
    return inner or None


def _cache_get(key: str) -> Optional[str]:
    return _cache.get(key)


def _cache_put(key: str, value: str) -> None:
    if len(_cache) >= _CACHE_MAX_ENTRIES:
        # Plain FIFO eviction; the working set is "dashboards exported lately"
        # and an LRU would not pay for its bookkeeping here.
        _cache.pop(next(iter(_cache)), None)
    _cache[key] = value


async def transpile_jsx(source: str) -> Optional[str]:
    """Compile JSX to plain ES5-compatible JS, or None if unavailable.

    None is a normal outcome (no Playwright, no browser, syntax error) and
    means "ship Babel inline instead", never "fail the export".
    """
    if not source or not source.strip():
        return None

    key = hashlib.sha256(source.encode("utf-8")).hexdigest()
    cached = _cache_get(key)
    if cached is not None:
        return cached

    lock = _locks.setdefault(key, asyncio.Lock())
    try:
        async with lock:
            # A waiter that queued behind the first transpile of this source
            # takes its result instead of launching a second browser.
            cached = _cache_get(key)
            if cached is not None:
                return cached

            try:
                result = await asyncio.wait_for(
                    _transpile_inner(source), timeout=_TRANSPILE_TIMEOUT_SECONDS
                )
            except asyncio.TimeoutError:
                logger.warning("JSX transpile exceeded %ss; falling back to inline Babel",
                               _TRANSPILE_TIMEOUT_SECONDS)
                return None
            except Exception:
                logger.exception("JSX transpile failed; falling back to inline Babel")
                return None

            if result:
                _cache_put(key, result)
            return result
    finally:
        # Dropped only after the cache is written and the lock released, so a
        # caller arriving in between still finds this lock and hits the cache
        # rather than starting a duplicate transpile. Bounded either way: one
        # entry per in-flight source.
        _locks.pop(key, None)


async def _transpile_inner(source: str) -> Optional[str]:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        logger.warning("Playwright unavailable; falling back to inline Babel")
        return None

    babel_src = _read_lib(_require_libs_dir(), "babel-standalone.min.js")

    # The source crosses into the page as a JSON argument, never by string
    # interpolation into the script body — artifact code is full of quotes,
    # backticks and `</script>` and would not survive being pasted inline.
    script = """
        (source) => {
          try {
            const out = Babel.transform(source, {
              presets: [['react', { runtime: 'classic' }]],
              sourceType: 'script',
              compact: false,
              comments: false,
            });
            return { code: out.code };
          } catch (e) {
            return { error: String((e && e.message) || e) };
          }
        }
    """

    exe = os.environ.get("BOW_CHROMIUM_EXECUTABLE") or None
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, executable_path=exe)
        try:
            page = await browser.new_page()
            await page.add_script_tag(content=babel_src)
            result = await page.evaluate(script, source)
        finally:
            await browser.close()

    if not isinstance(result, dict):
        return None
    if result.get("error"):
        # A genuine syntax error in the artifact. Inline Babel would hit the
        # same error in the browser, but it reports it in the page where the
        # existing error boundary can show it, so still prefer the fallback.
        logger.warning("Babel rejected artifact source: %s", result["error"])
        return None

    code = result.get("code")
    return code if isinstance(code, str) and code.strip() else None


def wrap_as_plain_script(compiled: str) -> str:
    """Wrap compiled JS in a script tag safe to embed in an HTML document.

    `</script>` inside a string literal in the compiled output would otherwise
    terminate the tag early and dump the remainder of the program into the DOM
    as text.
    """
    safe = compiled.replace("</script", "<\\/script")
    return f"<script>{safe}</script>"


def json_for_script(payload) -> str:
    """Serialize to JSON safe for embedding inside a <script> block.

    Escapes the `</` that would close the enclosing tag, and the U+2028/U+2029
    line separators that are valid JSON but illegal raw in a JS string literal.
    """
    encoded = json.dumps(payload, default=str, ensure_ascii=False)
    return (
        encoded.replace("</", "<\\/")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )
