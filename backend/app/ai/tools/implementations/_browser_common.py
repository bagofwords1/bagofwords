"""Shared runtime for the browser tools.

One place for the things all five tools must agree on:

* the URL-pattern grammar and matching (so the connection's `test_connection`
  validation and the live `route()` interceptor enforce the same rule),
* the per-report browser session (a headless Chromium context that outlives a
  single tool call but is torn down at the end of the run),
* the request interceptor that confines every request to the allowlist and
  refuses non-public hosts that were not explicitly listed, and
* redaction — the AI snapshot and screenshots both expose input values, so
  secret-shaped fields are scrubbed before either leaves the process.

Chromium and Playwright ship with the backend already (thumbnail/PDF services);
this adds no new dependency.
"""
from __future__ import annotations

import asyncio
import glob
import ipaddress
import logging
import os
import re
import socket
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------------
NAV_TIMEOUT_MS = 30_000
ACTION_TIMEOUT_MS = 15_000
SESSION_TTL_S = 600           # idle eviction
MAX_CONCURRENT_SESSIONS = 3   # process-wide memory ceiling
DEFAULT_VIEWPORT = {"width": 1280, "height": 900}

# Fields whose values must never reach a snapshot, screenshot, or observation.
_SECRET_INPUT_SELECTOR = (
    "input[type=password], "
    "input[autocomplete*=password], input[autocomplete=one-time-code], "
    "input[name*=pass i], input[name*=secret i], input[name*=token i], "
    "input[name*=otp i], input[name*=cvv i], input[name*=cvc i], "
    "input[name*=card i], input[id*=pass i], input[id*=secret i]"
)

# Login/challenge detection — best-effort, drives the blocked_reason signal.
_AUTH_HINT_RE = re.compile(r"\b(sign in|log ?in|password|two-factor|2fa|verify your identity)\b", re.I)
_CAPTCHA_HINT_RE = re.compile(r"\b(captcha|are you a robot|verify you are human|cloudflare)\b", re.I)


# ---------------------------------------------------------------------------
# Chromium binary
# ---------------------------------------------------------------------------
def _proxy_from_env() -> Optional[Dict[str, str]]:
    """Honor HTTPS_PROXY / HTTP_PROXY so the browser works behind an egress
    proxy (the managed sandbox, and many corporate self-hosted deployments).
    NO_PROXY becomes the bypass list."""
    server = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") \
        or os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy")
    if not server:
        return None
    proxy: Dict[str, str] = {"server": server}
    bypass = os.environ.get("NO_PROXY") or os.environ.get("no_proxy")
    if bypass:
        proxy["bypass"] = bypass
    return proxy


def chromium_executable() -> Optional[str]:
    """Best-effort path to a pre-installed Chromium, or None to let Playwright
    resolve it. Honors PLAYWRIGHT_BROWSERS_PATH (set in the managed env)."""
    base = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if not base or not os.path.isdir(base):
        return None
    for pat in (
        os.path.join(base, "chromium-*/chrome-linux/chrome"),
        os.path.join(base, "chromium-*/chrome-linux/headless_shell"),
        os.path.join(base, "chromium_headless_shell-*/chrome-linux/headless_shell"),
    ):
        hits = sorted(glob.glob(pat))
        if hits:
            return hits[-1]
    return None


# ---------------------------------------------------------------------------
# URL pattern grammar
# ---------------------------------------------------------------------------
def _pattern_host(pattern: str) -> Optional[str]:
    p = urlparse(pattern)
    return (p.hostname or "").lower() or None


def validate_url_pattern(pattern: str) -> Optional[str]:
    """Return an error string if `pattern` is not an acceptable allowlist entry,
    else None. The host must NAME a host: a hostname, wildcarded subdomains
    (``*.vendor.com``), or a single literal IP. Network-spanning host wildcards
    (``10.*.*.*``, bare ``*``) are refused — that is a scan, not a target."""
    if not isinstance(pattern, str) or not pattern.strip():
        return "empty pattern"
    pattern = pattern.strip()
    parsed = urlparse(pattern)
    if parsed.scheme not in ("http", "https"):
        return "must start with http:// or https://"
    host = parsed.hostname or ""
    if not host:
        return "missing host"
    # A leading '*.' wildcard for subdomains is allowed; strip it before checking.
    core = host[2:] if host.startswith("*.") else host
    if not core or core == "*":
        return "host wildcard is too broad; name a host"
    if "*" in core:
        # Any remaining wildcard in the host portion is a network-spanning glob.
        return "host wildcards other than a leading '*.' are not allowed"
    # If the remaining host is an IP, it must be a single literal address.
    try:
        ipaddress.ip_address(core)
    except ValueError:
        pass  # a hostname — fine
    return None


def _canonical(s: str) -> str:
    """Canonical form for matching, used for BOTH URLs and patterns so they
    compare on identical rules: lowercase scheme+host (case games and
    `user@evil.com` userinfo tricks can't slip past — urlparse drops userinfo),
    but PRESERVE path/query case (URL paths are case-sensitive, e.g. `/CARELINE`).
    A leading `*.` in a pattern host and `*` globs in the path survive intact."""
    try:
        p = urlparse(s)
    except Exception:
        return s
    scheme = (p.scheme or "").lower()
    host = (p.hostname or "").lower()
    port = f":{p.port}" if p.port else ""
    path = p.path or ""
    query = f"?{p.query}" if p.query else ""
    return f"{scheme}://{host}{port}{path}{query}"


def _normalize_for_match(url: str) -> str:
    return _canonical(url)


def _host_glob_to_url_glob(pattern: str) -> str:
    return _canonical(pattern)


def _glob_match(text: str, pattern: str) -> bool:
    """Match with ONLY ``*`` as a wildcard (any run of characters). Every other
    character — including ``?``, ``.``, ``:`` — is literal. This matters for URL
    patterns: a user who pastes a real URL with a query string
    (``…/c/b_425?q=:brand:x``) means the ``?`` literally, not as fnmatch's
    single-char wildcard. ``**`` and ``*`` behave the same here."""
    parts = pattern.split("*")
    rx = ".*".join(re.escape(p) for p in parts)
    return re.fullmatch(rx, text) is not None


def url_matches_patterns(url: str, patterns: List[str]) -> bool:
    """True if `url` matches any allowlist pattern. `*.vendor.com` also matches
    the apex `vendor.com`."""
    norm = _normalize_for_match(url)
    host = urlparse(norm).hostname or ""
    for pat in patterns or []:
        gl = _host_glob_to_url_glob(pat)
        if _glob_match(norm, gl):
            return True
        # Let a leading-subdomain wildcard also cover the apex domain.
        ph = _pattern_host(pat) or ""
        if ph.startswith("*."):
            apex = ph[2:]
            if host == apex:
                # rebuild the pattern with the apex host and retest
                gl2 = gl.replace(ph, apex, 1)
                if _glob_match(norm, gl2):
                    return True
    return False


def _host_is_public(hostname: str, cache: Dict[str, bool]) -> bool:
    """Resolve `hostname` and return True only if every address is public.
    Mirrors web_fetch._is_safe_host; cached per session to bound DNS cost."""
    if hostname in cache:
        return cache[hostname]
    ok = True
    lowered = (hostname or "").lower()
    if not lowered or lowered == "localhost" or lowered.endswith(".localhost"):
        ok = False
    else:
        try:
            infos = socket.getaddrinfo(hostname, None)
        except socket.gaierror:
            ok = False
        else:
            for info in infos:
                try:
                    ip = ipaddress.ip_address(info[4][0])
                except ValueError:
                    ok = False
                    break
                if (ip.is_private or ip.is_loopback or ip.is_link_local
                        or ip.is_multicast or ip.is_reserved or ip.is_unspecified):
                    ok = False
                    break
    cache[hostname] = ok
    return ok


def _is_link_local_literal(hostname: str) -> bool:
    """169.254.0.0/16 (incl. cloud metadata 169.254.169.254) — always refused."""
    try:
        return ipaddress.ip_address(hostname).is_link_local
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Connection resolution
# ---------------------------------------------------------------------------
def get_browser_connection(runtime_ctx: Dict[str, Any]) -> Optional[Tuple[List[str], bool]]:
    """Return (url_patterns, allow_downloads) from the report's first browser
    connection, or None if the report has none attached."""
    report = runtime_ctx.get("report")
    for ds in (getattr(report, "data_sources", None) or []):
        for conn in (getattr(ds, "connections", None) or []):
            if getattr(conn, "type", None) != "browser":
                continue
            cfg = getattr(conn, "config", None) or {}
            # Connection.config may be a dict, a JSON string, or (depending on the
            # create path) a JSON string that itself decodes to a JSON string.
            import json as _json
            for _ in range(2):
                if isinstance(cfg, str):
                    try:
                        cfg = _json.loads(cfg)
                    except Exception:
                        cfg = {}
                        break
                else:
                    break
            if not isinstance(cfg, dict):
                cfg = {}
            patterns = list(cfg.get("url_patterns") or [])
            allow_dl = bool(cfg.get("allow_downloads", True))
            return patterns, allow_dl
    return None


# ---------------------------------------------------------------------------
# Session manager
# ---------------------------------------------------------------------------
class BrowserSession:
    def __init__(self, session_id: str, patterns: List[str], allow_downloads: bool):
        self.session_id = session_id
        self.patterns = patterns
        self.allow_downloads = allow_downloads
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None
        self.last_used = time.monotonic()
        self._host_cache: Dict[str, bool] = {}
        self.pending_downloads: List[dict] = []

    def touch(self):
        self.last_used = time.monotonic()


class BrowserSessionManager:
    """Process-wide registry of report browser sessions. One context per report;
    idle sessions are evicted and there is a hard concurrency cap."""

    def __init__(self):
        self._sessions: Dict[str, BrowserSession] = {}
        self._lock = asyncio.Lock()

    async def _evict_idle(self):
        now = time.monotonic()
        stale = [sid for sid, s in self._sessions.items() if now - s.last_used > SESSION_TTL_S]
        for sid in stale:
            await self._close(sid)

    async def _close(self, session_id: str):
        s = self._sessions.pop(session_id, None)
        if not s:
            return
        for closer in (getattr(s, "context", None), getattr(s, "browser", None)):
            try:
                if closer:
                    await closer.close()
            except Exception:
                pass
        try:
            if s.playwright:
                await s.playwright.stop()
        except Exception:
            pass

    async def close_report(self, report_id: str):
        await self._close(str(report_id))

    def get(self, session_id: str) -> Optional[BrowserSession]:
        s = self._sessions.get(session_id)
        if s:
            s.touch()
        return s

    async def open(self, report_id: str, patterns: List[str], allow_downloads: bool) -> BrowserSession:
        from playwright.async_api import async_playwright

        session_id = str(report_id)
        async with self._lock:
            await self._evict_idle()
            existing = self._sessions.get(session_id)
            if existing:
                existing.touch()
                existing.patterns = patterns  # pick up config edits
                existing.allow_downloads = allow_downloads
                return existing
            if len(self._sessions) >= MAX_CONCURRENT_SESSIONS:
                # Evict the least-recently-used to honor the cap.
                lru = min(self._sessions.values(), key=lambda s: s.last_used)
                await self._close(lru.session_id)

            s = BrowserSession(session_id, patterns, allow_downloads)
            s.playwright = await async_playwright().start()
            launch_kwargs: Dict[str, Any] = {"headless": True}
            exe = chromium_executable()
            if exe:
                launch_kwargs["executable_path"] = exe
            proxy = _proxy_from_env()
            if proxy:
                launch_kwargs["proxy"] = proxy
            s.browser = await s.playwright.chromium.launch(**launch_kwargs)
            ctx_kwargs: Dict[str, Any] = {
                "viewport": DEFAULT_VIEWPORT,
                "accept_downloads": allow_downloads,
            }
            # Sandbox/dev only: trust a MITM proxy's cert. Never set in prod.
            if os.environ.get("BOW_BROWSER_IGNORE_HTTPS_ERRORS", "").lower() in ("1", "true", "yes"):
                ctx_kwargs["ignore_https_errors"] = True
            s.context = await s.browser.new_context(**ctx_kwargs)
            await self._install_guard(s)
            s.page = await s.context.new_page()
            self._sessions[session_id] = s
            return s

    async def _install_guard(self, s: BrowserSession):
        """Confine every request to the allowlist; refuse link-local always and
        non-public hosts that were not explicitly allowlisted."""
        async def handler(route, request):
            url = request.url
            host = urlparse(url).hostname or ""
            try:
                if _is_link_local_literal(host):
                    await route.abort()
                    return
                allowed = url_matches_patterns(url, s.patterns)
                if allowed:
                    await route.continue_()
                    return
                # Not allowlisted: permit only if it is a public host (lets a
                # page pull public CDN assets) and never for the main document.
                if request.resource_type == "document":
                    await route.abort()
                    return
                if await asyncio.to_thread(_host_is_public, host, s._host_cache):
                    await route.continue_()
                else:
                    await route.abort()
            except Exception:
                try:
                    await route.abort()
                except Exception:
                    pass

        await s.context.route("**/*", handler)

    async def track_downloads(self, s: BrowserSession, runtime_ctx: Dict[str, Any]):
        """Wire a download handler that saves files into the report's file store."""
        if not s.allow_downloads:
            return

        async def on_download(download):
            try:
                path = await download.path()
                if not path:
                    return
                with open(path, "rb") as fh:
                    raw = fh.read()
                fname = download.suggested_filename or "download"
                db_file = await save_bytes(runtime_ctx, raw, fname, _guess_content_type(fname))
                if db_file is not None:
                    s.pending_downloads.append({"file_id": str(db_file.id), "filename": fname})
            except Exception as e:
                logger.warning("browser download capture failed: %s", e)

        s.page.on("download", lambda d: asyncio.create_task(on_download(d)))


# Module singleton.
session_manager = BrowserSessionManager()


# ---------------------------------------------------------------------------
# File persistence helpers
# ---------------------------------------------------------------------------
import mimetypes


def _guess_content_type(filename: str) -> str:
    ct, _ = mimetypes.guess_type(filename)
    return ct or "application/octet-stream"


async def save_bytes(runtime_ctx: Dict[str, Any], data: bytes, filename: str, content_type: str):
    """Persist bytes as a report File, tagged to the current completion (so a
    screenshot shows inline in chat rather than in the composer tray)."""
    try:
        from app.services.file_service import FileService
        report = runtime_ctx.get("report")
        system = runtime_ctx.get("system_completion")
        return await FileService().save_bytes_as_file(
            db=runtime_ctx.get("db"),
            content=data,
            filename=filename,
            content_type=content_type,
            current_user=runtime_ctx.get("user"),
            organization=runtime_ctx.get("organization"),
            report_id=str(report.id) if report else None,
            completion_id=str(system.id) if system is not None else None,
        )
    except Exception as e:
        logger.warning("browser: save file failed: %s", e)
        return None


# ---------------------------------------------------------------------------
# Snapshot + redaction
# ---------------------------------------------------------------------------
async def _blank_secret_inputs(page) -> Any:
    """Temporarily blank secret-shaped inputs; returns a token to restore them.
    Runs entirely in-page so there is no window where a value is exposed to us."""
    js = """
    (sel) => {
      const nodes = Array.from(document.querySelectorAll(sel));
      const saved = nodes.map((n, i) => { const v = n.value; n.value = ''; n.setAttribute('data-bow-redacted','1'); return v; });
      window.__bowSecretNodes = nodes;
      return saved.length;
    }
    """
    try:
        return await page.evaluate(js, _SECRET_INPUT_SELECTOR)
    except Exception:
        return 0


async def _restore_secret_inputs(page, saved_values):
    js = """
    (vals) => {
      const nodes = window.__bowSecretNodes || [];
      nodes.forEach((n, i) => { if (vals && vals[i] !== undefined) n.value = vals[i]; n.removeAttribute('data-bow-redacted'); });
      window.__bowSecretNodes = null;
    }
    """
    try:
        await page.evaluate(js, saved_values)
    except Exception:
        pass


async def build_snapshot(page, *, full: bool = False, max_chars: int = 8000) -> Tuple[str, bool]:
    """Return (snapshot_text, truncated). Secret-shaped input values are blanked
    for the duration of the snapshot so they never appear in the tree."""
    saved = None
    try:
        saved = await page.evaluate(
            """(sel) => { const ns=[...document.querySelectorAll(sel)]; window.__bowS=ns; return ns.map(n=>{const v=n.value; n.value=''; return v;}); }""",
            _SECRET_INPUT_SELECTOR,
        )
    except Exception:
        saved = None
    try:
        snap = await page.locator("body").aria_snapshot(mode="ai")
    except Exception:
        try:
            snap = await page.locator("body").aria_snapshot()
        except Exception:
            snap = ""
    finally:
        if saved is not None:
            try:
                await page.evaluate(
                    """(vals) => { const ns=window.__bowS||[]; ns.forEach((n,i)=>{ if(vals&&vals[i]!==undefined) n.value=vals[i]; }); window.__bowS=null; }""",
                    saved,
                )
            except Exception:
                pass

    if not full:
        # Drop pure structural containers with no accessible name to shrink the tree.
        lines = []
        for ln in snap.splitlines():
            stripped = ln.strip()
            if re.match(r'^-\s+(generic|group|paragraph|list|listitem)\s+\[ref=', stripped) and '"' not in stripped:
                continue
            lines.append(ln)
        snap = "\n".join(lines)

    truncated = False
    if len(snap) > max_chars:
        snap = snap[:max_chars] + "\n… (snapshot truncated)"
        truncated = True
    return snap, truncated


async def mask_secrets_style(page):
    """Apply visual masking to secret inputs before a screenshot."""
    try:
        await page.add_style_tag(content=(
            _SECRET_INPUT_SELECTOR
            + " { -webkit-text-security: disc !important; text-security: disc !important; }"
        ))
    except Exception:
        pass


async def detect_block(page) -> Optional[str]:
    """Best-effort: is the current page an auth/captcha wall?"""
    try:
        text = (await page.locator("body").inner_text(timeout=2000))[:4000]
    except Exception:
        return None
    if _CAPTCHA_HINT_RE.search(text):
        return "captcha"
    # Require both a password-ish field AND login language to avoid false positives
    try:
        has_pwd = await page.locator("input[type=password]").count() > 0
    except Exception:
        has_pwd = False
    if has_pwd and _AUTH_HINT_RE.search(text):
        return "authentication"
    return None
