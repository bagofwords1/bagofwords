"""Ahead-of-time compilation of page-artifact JSX for the in-app iframe.

The in-app artifact iframe used to ship artifact code untransformed inside
`<script type="text/babel">` and download + run babel-standalone (2.3MB raw,
~575KB gzipped) on EVERY view just to compile it. This module produces the
exact JavaScript that babel-standalone would have produced in the browser, so
the iframe can skip Babel entirely.

"Exact" is the contract — not "equivalent". The output must be what
babel-standalone's transformScriptTags generates for the same inline script:

  * same compiler: the vendored babel-standalone.min.js the iframe loads
  * same options: transformScriptTags' defaults for a classic inline script —
    presets [["react", {runtime: "classic"}], "env"], no plugins,
    filename/sourceFileName "Inline Babel script", inline source map,
    targets {browsers: undefined}
  * same input: the script element's text exactly as the HTML parser sees it
    (not stripped)

The host (frontend/utils/artifactIframe.ts) then executes the result the way
Babel does: on DOMContentLoaded, as a new <script> appended to <head> — so
timing, scope and artifact-globals' appendChild import-stripping hook are all
unchanged.

Every failure degrades: no cached output simply means the host keeps shipping
babel-standalone, which is always correct. Compilation never runs on the
request path — reads only consult the cache and schedule a background compile
on a miss; artifact writes schedule one eagerly.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import time
from collections import OrderedDict
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Bump when the compile options below change, so stale disk entries are ignored.
_OPTIONS_VERSION = "1"

# The whole artifact code must be ONE classic text/babel inline script and
# nothing else. Anything more (extra scripts, markup, a type="module" script)
# goes through the browser path unchanged.
_SINGLE_BABEL_SCRIPT_RE = re.compile(
    r'\A\s*<script\s+type=["\']text/babel["\']\s*>([\s\S]*?)</script\s*>\s*\Z',
    re.IGNORECASE,
)

_MEM_MAX = 256
_mem: "OrderedDict[str, str]" = OrderedDict()
# Sources Babel rejected (syntax errors): never retried — the browser path
# reports the error. Bounded like the memory cache.
_rejected: "OrderedDict[str, None]" = OrderedDict()
# When the compiler itself is unavailable (no Playwright/Chromium), back off
# instead of retrying — and logging — on every artifact view.
_UNAVAILABLE_BACKOFF_SECONDS = 600
_unavailable_until = 0.0
_inflight: dict[str, asyncio.Task] = {}
_bg_tasks: set[asyncio.Task] = set()
# Each compile launches a headless Chromium; keep at most two at a time.
_sem: Optional[asyncio.Semaphore] = None

_COMPILE_SCRIPT = """
(source) => {
  try {
    var filename = 'Inline Babel script';
    var out = Babel.transform(source, {
      filename: filename,
      presets: [['react', { runtime: 'classic' }], 'env'],
      plugins: undefined,
      sourceMaps: 'inline',
      sourceFileName: filename,
      targets: { browsers: undefined },
    });
    return { code: out.code };
  } catch (e) {
    return { error: String((e && e.message) || e) };
  }
}
"""


def extract_single_babel_script(code: Optional[str]) -> Optional[str]:
    """The inline script text if `code` is exactly one text/babel script."""
    if not code:
        return None
    m = _SINGLE_BABEL_SCRIPT_RE.match(code)
    if not m:
        return None
    inner = m.group(1)
    # Mirror the HTML parser that hands Babel the script text: it normalizes
    # CR/CRLF to LF. `<!--` can switch the tokenizer into the escaped
    # script-data states (changing where the script ends) and NUL is replaced
    # — both are vanishingly rare, so just leave those to the browser path.
    if "</script" in inner.lower() or "<!--" in inner or "\x00" in inner or not inner.strip():
        return None
    return inner.replace("\r\n", "\n").replace("\r", "\n")


def source_key(code: str) -> str:
    """FNV-1a (32-bit) over the UTF-16 code units of `code`, plus its length.

    The frontend computes the same value over the code string it is about to
    render (artifactIframe.ts: precompiledKey) and only uses the compiled
    output when the two match — so a compiled payload can never be paired with
    different code (an edit in flight, a version preview, a stale response).
    """
    h = 0x811C9DC5
    data = code.encode("utf-16-le", "surrogatepass")
    for i in range(0, len(data), 2):
        h ^= data[i] | (data[i + 1] << 8)
        h = (h * 0x01000193) & 0xFFFFFFFF
    return f"{len(data) // 2}:{h:08x}"


def _libs_fingerprint() -> Optional[str]:
    try:
        from app.services.artifact_libs import _find_libs_dir
        libs = _find_libs_dir()
        if not libs:
            return None
        p = Path(libs) / "babel-standalone.min.js"
        st = p.stat()
        return f"{st.st_size}:{int(st.st_mtime)}"
    except Exception:
        return None


def _cache_key(inner: str) -> Optional[str]:
    fp = _libs_fingerprint()
    if fp is None:
        return None
    h = hashlib.sha256()
    h.update(_OPTIONS_VERSION.encode())
    h.update(b"\0")
    h.update(fp.encode())
    h.update(b"\0")
    h.update(inner.encode("utf-8", "surrogatepass"))
    return h.hexdigest()


def _disk_dir() -> Path:
    override = os.environ.get("BOW_ARTIFACT_JS_CACHE_DIR")
    if override:
        return Path(override)
    return Path(__file__).resolve().parent.parent.parent / "uploads" / "artifact_js"


def _disk_get(key: str) -> Optional[str]:
    try:
        return (_disk_dir() / f"{key}.js").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _disk_put(key: str, value: str) -> None:
    try:
        d = _disk_dir()
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / f"{key}.{os.getpid()}.tmp"
        tmp.write_text(value, encoding="utf-8")
        os.replace(tmp, d / f"{key}.js")
    except OSError:
        logger.debug("artifact precompile: disk cache write failed", exc_info=True)


def _mem_get(key: str) -> Optional[str]:
    v = _mem.get(key)
    if v is not None:
        _mem.move_to_end(key)
    return v


def _mem_put(key: str, value: str) -> None:
    _mem[key] = value
    _mem.move_to_end(key)
    while len(_mem) > _MEM_MAX:
        _mem.popitem(last=False)


def get_cached(code: Optional[str]) -> Optional[str]:
    """Compiled JS for `code` if already compiled; never compiles."""
    inner = extract_single_babel_script(code)
    if inner is None:
        return None
    key = _cache_key(inner)
    if key is None:
        return None
    v = _mem_get(key)
    if v is None:
        v = _disk_get(key)
        if v is not None:
            _mem_put(key, v)
    return v


class _CompilerUnavailable(Exception):
    pass


async def _compile_inner(inner: str) -> tuple[Optional[str], bool]:
    """(compiled, rejected) — rejected means Babel refused the source."""
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise _CompilerUnavailable("playwright not installed") from exc
    from app.services.artifact_libs import _read_lib, _require_libs_dir

    babel_src = _read_lib(_require_libs_dir(), "babel-standalone.min.js")
    exe = os.environ.get("BOW_CHROMIUM_EXECUTABLE") or None
    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(headless=True, executable_path=exe)
        except Exception as exc:
            raise _CompilerUnavailable(str(exc)[:300]) from exc
        try:
            page = await browser.new_page()
            await page.add_script_tag(content=babel_src)
            result = await page.evaluate(_COMPILE_SCRIPT, inner)
        finally:
            await browser.close()
    if not isinstance(result, dict) or result.get("error"):
        # A syntax error: the browser path reports it through the iframe's
        # error boundary exactly as before, so leave it uncompiled.
        return None, True
    code = result.get("code")
    return (code if isinstance(code, str) and code.strip() else None), False


async def compile_code(code: Optional[str]) -> Optional[str]:
    """Compile (or fetch from cache) the artifact code; None when not possible."""
    inner = extract_single_babel_script(code)
    if inner is None:
        return None
    key = _cache_key(inner)
    if key is None:
        return None
    cached = get_cached(code)
    if cached is not None:
        return cached
    if key in _rejected or time.monotonic() < _unavailable_until:
        return None
    task = _inflight.get(key)
    if task is None:
        task = asyncio.ensure_future(_compile_and_store(key, inner))
        _inflight[key] = task
        task.add_done_callback(lambda _t, k=key: _inflight.pop(k, None))
    try:
        return await asyncio.shield(task)
    except Exception:
        return None


async def _compile_and_store(key: str, inner: str) -> Optional[str]:
    global _sem, _unavailable_until
    if _sem is None:
        _sem = asyncio.Semaphore(2)
    async with _sem:
        try:
            out, rejected = await asyncio.wait_for(_compile_inner(inner), timeout=60)
        except _CompilerUnavailable as exc:
            _unavailable_until = time.monotonic() + _UNAVAILABLE_BACKOFF_SECONDS
            logger.warning("artifact precompile unavailable (%s); artifacts compile in the browser", exc)
            return None
        except Exception:
            logger.warning("artifact precompile failed; the browser keeps compiling", exc_info=True)
            return None
    if rejected:
        _rejected[key] = None
        while len(_rejected) > _MEM_MAX:
            _rejected.popitem(last=False)
        return None
    if out:
        _mem_put(key, out)
        _disk_put(key, out)
    return out


def _scheduling_enabled() -> bool:
    if os.environ.get("BOW_ARTIFACT_PRECOMPILE", "1").lower() in ("0", "false", "no"):
        return False
    # The test suite creates thousands of artifacts; launching a background
    # Chromium for each would only slow it down. Tests of this module call
    # compile_code() directly.
    return not os.environ.get("PYTEST_CURRENT_TEST")


def schedule_compile(code: Optional[str]) -> None:
    """Fire-and-forget compile for `code` (no-op if cached or not compilable)."""
    if not _scheduling_enabled():
        return
    if extract_single_babel_script(code) is None or get_cached(code) is not None:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    t = loop.create_task(compile_code(code))
    _bg_tasks.add(t)
    t.add_done_callback(_bg_tasks.discard)


def attach_compiled(schema) -> None:
    """Set compiled_js / compiled_js_key on an ArtifactSchema for page mode.

    Read paths call this: a hit serves the compiled output, a miss schedules a
    background compile so the next view gets it. Never raises.
    """
    try:
        if os.environ.get("BOW_ARTIFACT_PRECOMPILE", "1").lower() in ("0", "false", "no"):
            return
        if getattr(schema, "mode", None) != "page":
            return
        content = getattr(schema, "content", None) or {}
        code = content.get("code") if isinstance(content, dict) else None
        if not isinstance(code, str):
            return
        compiled = get_cached(code)
        if compiled is None:
            schedule_compile(code)
            return
        schema.compiled_js = compiled
        schema.compiled_js_key = source_key(code)
    except Exception:
        logger.debug("attach_compiled failed", exc_info=True)
