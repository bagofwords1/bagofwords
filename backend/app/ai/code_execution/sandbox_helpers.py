"""Optional helpers generated code can use beyond querying (org "Sandbox" settings).

    http     bulk web fetch       enable_sandbox_web_fetch, sandbox_web_fetch_max_urls
    browser  headless browser     enable_sandbox_browser, sandbox_browser_allow_mutating_requests,
                                  sandbox_browser_max_pages
    lists    Agent List writes    enable_sandbox_lists, sandbox_lists_max_rows

Everything here runs on the trusted side. The sandbox child only holds
stand-in objects whose calls come back over the RPC pipe (sandbox/child.py),
so the allowlists, limits and audit below apply however the code behaves.

`http` is built for every executor (reruns and dashboard refreshes included,
as before). `lists` exists only when the agent's own create_data run passes a
`SandboxHelpers`: a rerun must never write to a list again. `browser` is also
given to reruns (Publish, refresh, a reader's run) of code that uses it, under
the same allowlist, read-only guard and page budget (`rerun_sandbox_helpers`);
without it such a step could never be published.
"""
from __future__ import annotations

import ast
import asyncio
import logging
import threading
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def _flag(settings, key: str) -> bool:
    if settings is None:
        return False
    try:
        cfg = settings.get_config(key)
    except Exception:
        return False
    return bool(getattr(cfg, "value", False)) if cfg is not None else False


def _clamped_int(settings, key: str, default: int, lo: int, hi: int) -> int:
    value = default
    if settings is not None:
        try:
            cfg = settings.get_config(key)
            raw = getattr(cfg, "value", None) if cfg is not None else None
            if raw is not None:
                value = int(raw)
        except Exception:
            value = default
    return max(lo, min(hi, value))


@dataclass(frozen=True)
class SandboxSettings:
    web_fetch: bool = False
    max_urls: int = 1000
    browser: bool = False
    allow_mutating_requests: bool = False
    max_pages: int = 200
    lists: bool = False
    max_list_rows: int = 5000


def sandbox_settings(organization_settings) -> SandboxSettings:
    """All off when settings are missing (unit tests, legacy callers)."""
    s = organization_settings
    return SandboxSettings(
        web_fetch=_flag(s, "enable_sandbox_web_fetch"),
        max_urls=_clamped_int(s, "sandbox_web_fetch_max_urls", 1000, 1, 10_000),
        browser=_flag(s, "enable_sandbox_browser"),
        allow_mutating_requests=_flag(s, "sandbox_browser_allow_mutating_requests"),
        max_pages=_clamped_int(s, "sandbox_browser_max_pages", 200, 1, 2_000),
        lists=_flag(s, "enable_sandbox_lists"),
        max_list_rows=_clamped_int(s, "sandbox_lists_max_rows", 5000, 1, 50_000),
    )


# ---------------------------------------------------------------------------
# Page text kept for evidence verification
# ---------------------------------------------------------------------------

class PageTextStore:
    """Text of pages the code fetched in this run, so quotes in list rows can
    be verified against what was actually fetched (verify.py only knows the
    agent's own read tools). Bounded; extra pages are simply not kept."""

    MAX_TOTAL_CHARS = 20_000_000

    def __init__(self):
        self._items: List[tuple] = []
        self._chars = 0
        self._lock = threading.Lock()

    def add(self, url: Optional[str], text: Optional[str], *, final_url: Optional[str] = None) -> None:
        if not text or not isinstance(text, str):
            return
        with self._lock:
            if self._chars + len(text) > self.MAX_TOTAL_CHARS:
                return
            self._chars += len(text)
            self._items.append((url, final_url, text))

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)

    def to_sources(self) -> List[Any]:
        from app.services.agent_lists.verify import SourceText, normalize

        with self._lock:
            items = list(self._items)
        return [SourceText(None, [u or "", f or ""], normalize(t)) for (u, f, t) in items]


# ---------------------------------------------------------------------------
# http: per-run URL budget
# ---------------------------------------------------------------------------

class MeteredHttp:
    """`SafeHttpClient` with a per-run URL budget. Same surface the coder
    prompt documents (`get`, `batch_get`)."""

    def __init__(self, inner, *, max_urls: int, page_texts: Optional[PageTextStore] = None):
        self._inner = inner
        self._max = int(max_urls)
        self._used = 0
        self._lock = threading.Lock()
        self._texts = page_texts

    @property
    def used(self) -> int:
        return self._used

    def _reserve(self, n: int) -> None:
        with self._lock:
            if self._used + n > self._max:
                raise ValueError(
                    f"http: this run may fetch at most {self._max} URLs "
                    f"({self._used} already fetched, {n} more requested). "
                    "Fetch fewer pages per run."
                )
            self._used += n

    def _keep(self, pages) -> None:
        if self._texts is None:
            return
        for p in pages:
            if getattr(p, "success", False) and not getattr(p, "error", None):
                self._texts.add(getattr(p, "url", None), getattr(p, "text", None),
                                final_url=getattr(p, "final_url", None))

    def get(self, url, *, timeout=None):
        self._reserve(1)
        kw = {"timeout": timeout} if timeout is not None else {}
        page = self._inner.get(url, **kw)
        self._keep([page])
        return page

    def batch_get(self, urls, *, concurrency=None, timeout=None):
        if not isinstance(urls, (list, tuple)):
            raise TypeError("urls must be a list")
        self._reserve(len(urls))
        kw = {k: v for k, v in (("concurrency", concurrency), ("timeout", timeout)) if v is not None}
        pages = self._inner.batch_get(list(urls), **kw)
        self._keep(pages)
        return pages


# ---------------------------------------------------------------------------
# browser: a headless page the code drives
# ---------------------------------------------------------------------------

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_MAX_TEXT_CHARS = 200_000
_MAX_HTML_CHARS = 1_000_000
_MAX_ELEMENTS = 5_000
_DEFAULT_TIMEOUT_MS = 15_000
_MAX_TIMEOUT_MS = 60_000

# Called with a list of elements; returns one plain dict per element.
# `attrs`: attributes of the element itself. `fields`: {name: spec} read
# inside each element, where spec is "css" (text of the first match),
# "css@attr" (an attribute of the first match) or ["css"] / ["css@attr"]
# (one value per match, as a list).
_QUERY_ALL_JS = """
(els, [attrs, fields]) => {
  const abs = (a, v) => {
    if ((a === 'href' || a === 'src') && v) { try { return new URL(v, document.baseURI).href; } catch (e) {} }
    return v;
  };
  const read = (node, attr) => {
    if (!node) return null;
    if (!attr) return (node.innerText || node.textContent || '').trim();
    if (attr === 'html') return node.outerHTML;
    return abs(attr, node.getAttribute(attr));
  };
  const split = (spec) => {
    const i = spec.lastIndexOf('@');
    return i >= 0 ? [spec.slice(0, i).trim(), spec.slice(i + 1)] : [spec.trim(), null];
  };
  // "" (or "@attr") is the element itself; a selector the element itself
  // matches ("a@href" on an <a>) reads the element, not a descendant.
  const one = (el, css) => (!css ? el : (el.matches(css) ? el : el.querySelector(css)));
  const all = (el, css) => (!css ? [el] : (el.matches(css) ? [el] : Array.from(el.querySelectorAll(css))));
  return els.map(el => {
    const out = {text: read(el, null)};
    for (const a of attrs) { if (a !== 'text') out[a] = read(el, a); }
    for (const [name, spec] of Object.entries(fields || {})) {
      if (Array.isArray(spec)) {
        const [css, attr] = split(String(spec[0] || ''));
        out[name] = all(el, css).map(n => read(n, attr));
      } else {
        const [css, attr] = split(String(spec || ''));
        out[name] = read(one(el, css), attr);
      }
    }
    return out;
  });
}
"""


class SandboxBrowserError(RuntimeError):
    pass


class SandboxBrowser:
    """One headless page per code run, driven from the sandbox's RPC thread.

    Playwright lives on the app's event loop; each call is scheduled there
    with `run_coroutine_threadsafe`, which is safe because the code itself
    runs on the code-exec pool, never on the loop. The page is confined by
    the browser connection's URL allowlist (the same guard the agent's
    browser tools use). Unless the org allows it, any request that is not
    GET/HEAD/OPTIONS is aborted, so clicks and typing can only read.
    """

    OPS = frozenset({"goto", "click", "fill", "press", "select", "scroll",
                     "wait_for", "text", "html", "query_all", "url"})

    def __init__(self, loop: asyncio.AbstractEventLoop, *, patterns: List[str],
                 allow_mutating_requests: bool, max_pages: int,
                 page_texts: Optional[PageTextStore] = None):
        self._loop = loop
        self._patterns = list(patterns or [])
        self._allow_mutating = bool(allow_mutating_requests)
        self._max_pages = int(max_pages)
        self._texts = page_texts
        self._session = None
        self._session_id: Optional[str] = None
        self.pages_loaded = 0  # main-frame navigations: goto and clicks that load a page
        self.blocked_requests = 0

    # ── thread side ─────────────────────────────────────────────────────
    def call(self, op: str, args: Optional[list] = None, kwargs: Optional[dict] = None) -> Any:
        if op not in self.OPS:
            raise SandboxBrowserError(f"browser.{op} is not available")
        coro = getattr(self, f"_op_{op}")(*(args or []), **(kwargs or {}))
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return fut.result(timeout=_MAX_TIMEOUT_MS / 1000 + 30)
        except Exception:
            fut.cancel()
            raise

    def close(self) -> None:
        if self._session_id is None:
            return
        fut = asyncio.run_coroutine_threadsafe(self._close(), self._loop)
        try:
            fut.result(timeout=30)
        except Exception as e:  # never fail a run on cleanup
            logger.warning("sandbox browser: close failed: %s", e)

    # ── loop side ───────────────────────────────────────────────────────
    async def _page(self):
        if self._session is None:
            from app.ai.tools.implementations._browser_common import session_manager

            self._session_id = f"sandbox-{uuid.uuid4()}"
            self._session = await session_manager.open(self._session_id, self._patterns, False)
            if not self._allow_mutating:
                await self._session.context.route("**/*", self._block_mutating)
            page = self._session.page
            page.set_default_timeout(_DEFAULT_TIMEOUT_MS)
            page.on("framenavigated", lambda frame: self._count_navigation(page, frame))
        self._session.touch()
        return self._session.page

    def _count_navigation(self, page, frame) -> None:
        if frame == page.main_frame:
            self.pages_loaded += 1

    def _check_budget(self) -> None:
        if self.pages_loaded >= self._max_pages:
            raise SandboxBrowserError(
                f"browser: this run may load at most {self._max_pages} pages; load fewer pages per run"
            )

    async def _block_mutating(self, route, request):
        # Registered after the allowlist guard, so it runs first (Playwright
        # runs routes in reverse registration order); fallback() hands an
        # allowed request on to the guard.
        if request.method.upper() in _SAFE_METHODS:
            await route.fallback()
            return
        self.blocked_requests += 1
        await route.abort()

    async def _close(self) -> None:
        from app.ai.tools.implementations._browser_common import session_manager

        sid, self._session_id, self._session = self._session_id, None, None
        if sid:
            await session_manager.close_session(sid)

    @staticmethod
    def _timeout(timeout) -> int:
        try:
            ms = int(float(timeout) * 1000) if timeout is not None else _DEFAULT_TIMEOUT_MS
        except (TypeError, ValueError):
            ms = _DEFAULT_TIMEOUT_MS
        return max(1_000, min(_MAX_TIMEOUT_MS, ms))

    async def _op_goto(self, url, timeout=None):
        self._check_budget()
        page = await self._page()
        try:
            resp = await page.goto(str(url), wait_until="domcontentloaded", timeout=self._timeout(timeout))
        except Exception as e:
            raise SandboxBrowserError(
                f"browser.goto({url!r}) failed: {e}. The URL must match the browser connection's allowlist."
            ) from None
        return {"url": page.url, "status": resp.status if resp is not None else None}

    async def _op_click(self, selector, timeout=None):
        self._check_budget()
        page = await self._page()
        await page.locator(str(selector)).first.click(timeout=self._timeout(timeout))
        await self._settle(page)
        return {"url": page.url}

    async def _op_fill(self, selector, text, timeout=None):
        page = await self._page()
        await page.locator(str(selector)).first.fill(str(text), timeout=self._timeout(timeout))
        return {"url": page.url}

    async def _op_press(self, key, selector=None, timeout=None):
        self._check_budget()
        page = await self._page()
        if selector:
            await page.locator(str(selector)).first.press(str(key), timeout=self._timeout(timeout))
        else:
            await page.keyboard.press(str(key))
        await self._settle(page)
        return {"url": page.url}

    async def _op_select(self, selector, value, timeout=None):
        self._check_budget()
        page = await self._page()
        chosen = await page.locator(str(selector)).first.select_option(str(value), timeout=self._timeout(timeout))
        await self._settle(page)
        return {"url": page.url, "selected": chosen}

    async def _op_scroll(self, pixels=2000):
        page = await self._page()
        await page.mouse.wheel(0, int(pixels))
        await self._settle(page)
        return {"url": page.url}

    async def _op_wait_for(self, selector, timeout=None, state="visible"):
        page = await self._page()
        st = state if state in ("attached", "detached", "visible", "hidden") else "visible"
        await page.locator(str(selector)).first.wait_for(state=st, timeout=self._timeout(timeout))
        return {"url": page.url}

    async def _op_text(self, selector=None):
        page = await self._page()
        text = await page.locator(str(selector or "body")).first.inner_text()
        text = (text or "")[:_MAX_TEXT_CHARS]
        if self._texts is not None:
            self._texts.add(page.url, text)
        return text

    async def _op_html(self, selector=None):
        page = await self._page()
        if selector:
            html = await page.locator(str(selector)).first.evaluate("el => el.outerHTML")
        else:
            html = await page.content()
        return (html or "")[:_MAX_HTML_CHARS]

    async def _op_query_all(self, selector, attrs=None, fields=None):
        page = await self._page()
        attrs = [str(a) for a in (attrs or [])]
        if fields is not None and not isinstance(fields, dict):
            raise SandboxBrowserError("browser.query_all: fields must be a dict of {name: css selector}")
        fields = {str(k): ([str(v[0])] if isinstance(v, (list, tuple)) and v else str(v))
                  for k, v in (fields or {}).items()}
        items = await page.locator(str(selector)).evaluate_all(_QUERY_ALL_JS, [attrs, fields])
        items = list(items or [])[:_MAX_ELEMENTS]
        if self._texts is not None and items:
            self._texts.add(page.url, "\n".join(str(i.get("text") or "") for i in items))
        return items

    async def _op_url(self):
        page = await self._page()
        return page.url

    @staticmethod
    async def _settle(page) -> None:
        """Give a click/keypress a moment to navigate or re-render."""
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=5_000)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Container handed to the executor
# ---------------------------------------------------------------------------

class SandboxHelpers:
    """What the agent's create_data run adds to the sandbox. One instance per
    tool call; the executor calls `begin_attempt` / `end_attempt` around
    every attempt so a retried attempt starts clean."""

    def __init__(self, *, lists=None, browser: Optional[SandboxBrowser] = None,
                 page_texts: Optional[PageTextStore] = None):
        self.lists = lists
        self.browser = browser
        self.page_texts = page_texts or PageTextStore()
        self.urls_fetched = 0

    def begin_attempt(self) -> None:
        if self.lists is not None:
            self.lists.reset()

    def end_attempt(self) -> None:
        if self.browser is not None:
            self.browser.close()

    def summary(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        if self.urls_fetched:
            out["urls_fetched"] = self.urls_fetched
        if self.browser is not None and self.browser.pages_loaded:
            out["browser_pages_loaded"] = self.browser.pages_loaded
            if self.browser.blocked_requests:
                out["browser_requests_blocked"] = self.browser.blocked_requests
        return out


# ---------------------------------------------------------------------------
# browser outside the agent's own run
# ---------------------------------------------------------------------------

class BrowserUnavailableError(RuntimeError):
    """Code drives `browser`, but this run has none. Raised before the code
    runs, so the reason is readable instead of `'NoneType' object has no
    attribute 'goto'`."""


def code_uses_browser(code: Optional[str]) -> bool:
    """Does `code` call the `browser` helper (``browser.goto(...)`` etc.)?
    A local variable that happens to be named `browser` does not count."""
    if not code or "browser" not in code:
        return False
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return False
    used = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "browser":
            used = True
        elif isinstance(node, ast.Name) and node.id == "browser" and isinstance(node.ctx, (ast.Store, ast.Del)):
            return False  # the code defines its own `browser`
        elif isinstance(node, ast.alias) and (node.asname or node.name) == "browser":
            return False
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name != "generate_df":
            # generate_df(..., browser=None) receives the helper; any other
            # function's `browser` argument is the code's own value.
            args = node.args
            if "browser" in [a.arg for a in args.posonlyargs + args.args + args.kwonlyargs]:
                return False
    return used


def browser_unavailable_reason(organization_settings) -> str:
    if not sandbox_settings(organization_settings).browser:
        return "the organization's 'Browser in code' setting is off"
    return "no browser connection is attached to the data sources this run uses"


def rerun_sandbox_helpers(code: Optional[str], data_sources, organization_settings) -> Optional[SandboxHelpers]:
    """A `browser` for a rerun of saved code (Publish, refresh, a reader's
    run), or None when the code does not use one or none is allowed. Same
    guards as the agent's run; never `lists`. Call on the event loop: the
    browser schedules its Playwright calls there."""
    if not code_uses_browser(code):
        return None
    sb = sandbox_settings(organization_settings)
    if not sb.browser:
        return None
    from app.ai.tools.implementations._browser_common import browser_connection_for

    conn = browser_connection_for(data_sources)
    if conn is None:
        return None
    patterns, _allow_downloads = conn
    page_texts = PageTextStore()
    browser = SandboxBrowser(
        asyncio.get_running_loop(), patterns=patterns,
        allow_mutating_requests=sb.allow_mutating_requests,
        max_pages=sb.max_pages, page_texts=page_texts,
    )
    return SandboxHelpers(browser=browser, page_texts=page_texts)
