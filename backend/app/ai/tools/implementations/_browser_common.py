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
# Placeholder grammar the model uses to reference a secret without seeing it:
# {{secret:API_TOKEN}}. Resolved server-side just before Playwright executes.
SECRET_PLACEHOLDER_RE = re.compile(r"\{\{\s*secret:([A-Za-z_][A-Za-z0-9_]{0,63})\s*\}\}")

# Values shorter than this are never redacted from outputs: substituting every
# occurrence of e.g. "a" would shred the page text without hiding anything.
_MIN_REDACTABLE_LEN = 4


class BrowserConnectionContext:
    """Everything the browser tools need from the report's browser connection:
    the allowlist, the resolved secret map (system creds with the current
    user's overlaid per-key), the proxy, and the per-user session key."""

    def __init__(self, *, connection_id: str, data_source_id: Optional[str],
                 patterns: List[str], allow_downloads: bool,
                 proxy: Optional[Dict[str, str]], secrets: Dict[str, str],
                 user_secret_keys: List[str], auth_policy: str,
                 allow_vision_after_secret_use: bool, user_id: Optional[str],
                 report_id: str):
        self.connection_id = connection_id
        self.data_source_id = data_source_id
        self.patterns = patterns
        self.allow_downloads = allow_downloads
        self.proxy = proxy
        self.secrets = secrets
        self.user_secret_keys = user_secret_keys
        self.auth_policy = auth_policy
        self.allow_vision_after_secret_use = allow_vision_after_secret_use
        self.user_id = user_id
        self.report_id = report_id

    @property
    def session_key(self) -> str:
        # Sessions carry injected credentials, so they must never be shared
        # across users or connections. The key is derived server-side from the
        # caller's identity — a session_id passed in tool input is not trusted.
        return f"{self.report_id}:{self.connection_id}:{self.user_id or 'anon'}"

    @property
    def missing_user_secrets(self) -> List[str]:
        return [k for k in (self.user_secret_keys or []) if not self.secrets.get(k)]


def _parse_conn_config(conn) -> Dict[str, Any]:
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
    return cfg if isinstance(cfg, dict) else {}


async def _load_user_secrets(runtime_ctx: Dict[str, Any], data_source_id: Optional[str]) -> Dict[str, str]:
    """The current user's saved secret map for this data source, if any."""
    db = runtime_ctx.get("db")
    user = runtime_ctx.get("user")
    if db is None or user is None or not data_source_id:
        return {}
    try:
        from sqlalchemy import select
        from app.models.user_data_source_credentials import UserDataSourceCredentials
        stmt = (
            select(UserDataSourceCredentials)
            .where(
                UserDataSourceCredentials.data_source_id == str(data_source_id),
                UserDataSourceCredentials.user_id == str(user.id),
                UserDataSourceCredentials.is_active == True,  # noqa: E712
                UserDataSourceCredentials.auth_mode == "secrets",
            )
            .order_by(UserDataSourceCredentials.is_primary.desc(), UserDataSourceCredentials.updated_at.desc())
        )
        row = (await db.execute(stmt)).scalars().first()
        if not row:
            return {}
        payload = row.decrypt_credentials() or {}
        secrets = payload.get("secrets") or {}
        return {k: v for k, v in secrets.items() if isinstance(v, str) and v}
    except Exception as e:
        logger.warning("browser: loading user secrets failed: %s", e)
        return {}


async def get_browser_connection(runtime_ctx: Dict[str, Any]) -> Optional[BrowserConnectionContext]:
    """Resolve the report's first browser connection into a context object.

    Secrets resolve in two layers: the connection's system credentials are the
    base and the current user's saved secrets overlay them per key — so an
    admin-provided shared token coexists with each member's own login fields.
    Returns None if the run has no browser connection available."""
    report = runtime_ctx.get("report")
    user = runtime_ctx.get("user")
    # Prefer the run's resolved working set (present in Auto mode and always
    # eagerly loaded); the report's own attachments are the fallback for
    # callers that don't pass one.
    sources = runtime_ctx.get("data_sources") or (getattr(report, "data_sources", None) or [])
    for ds in sources:
        for conn in (getattr(ds, "connections", None) or []):
            if getattr(conn, "type", None) != "browser":
                continue
            cfg = _parse_conn_config(conn)

            try:
                sys_creds = conn.decrypt_credentials() or {}
            except Exception:
                sys_creds = {}
            sys_secrets = sys_creds.get("secrets") or {}
            secrets: Dict[str, str] = {
                k: v for k, v in sys_secrets.items() if isinstance(v, str) and v
            }
            secrets.update(await _load_user_secrets(runtime_ctx, getattr(ds, "id", None)))

            proxy: Optional[Dict[str, str]] = None
            server = (cfg.get("proxy_server") or "").strip()
            if server:
                proxy = {"server": server}
                bypass = (cfg.get("proxy_bypass") or "").strip()
                if bypass:
                    proxy["bypass"] = bypass
                if sys_creds.get("proxy_username"):
                    proxy["username"] = sys_creds["proxy_username"]
                if sys_creds.get("proxy_password"):
                    proxy["password"] = sys_creds["proxy_password"]

            return BrowserConnectionContext(
                connection_id=str(getattr(conn, "id", "")),
                data_source_id=str(getattr(ds, "id", "")) if getattr(ds, "id", None) else None,
                patterns=list(cfg.get("url_patterns") or []),
                allow_downloads=bool(cfg.get("allow_downloads", True)),
                proxy=proxy,
                secrets=secrets,
                user_secret_keys=list(cfg.get("user_secret_keys") or []),
                auth_policy=getattr(conn, "auth_policy", None) or "system_only",
                allow_vision_after_secret_use=bool(cfg.get("allow_vision_after_secret_use", False)),
                user_id=str(getattr(user, "id", "")) if user is not None else None,
                report_id=str(getattr(report, "id", "")),
            )
    return None


# ---------------------------------------------------------------------------
# Secret placeholders: injection and redaction
# ---------------------------------------------------------------------------
def resolve_secret_placeholders(text: str, secrets: Dict[str, str]) -> Tuple[str, List[str], List[str]]:
    """Substitute {{secret:KEY}} placeholders with real values.

    Returns (resolved_text, used_keys, missing_keys). Missing keys are left
    as-is in the text; callers should fail with a clear message rather than
    send a literal placeholder to a website."""
    used: List[str] = []
    missing: List[str] = []

    def _sub(m: re.Match) -> str:
        key = m.group(1)
        val = (secrets or {}).get(key)
        if val:
            if key not in used:
                used.append(key)
            return val
        if key not in missing:
            missing.append(key)
        return m.group(0)

    return SECRET_PLACEHOLDER_RE.sub(_sub, text or ""), used, missing


def redact_secrets(text: Optional[str], secrets: Dict[str, str]) -> Optional[str]:
    """Scrub secret VALUES out of text bound for the model, replacing each
    occurrence with its placeholder. Also catches URL-encoded occurrences so a
    token echoed back in a query string doesn't slip through."""
    if not text or not secrets:
        return text
    from urllib.parse import quote
    for key, val in secrets.items():
        if not isinstance(val, str) or len(val) < _MIN_REDACTABLE_LEN:
            continue
        placeholder = "{{secret:" + key + "}}"
        text = text.replace(val, placeholder)
        quoted = quote(val, safe="")
        if quoted != val:
            text = text.replace(quoted, placeholder)
    return text


def http_credentials_from_secrets(secrets: Dict[str, str]) -> Optional[Dict[str, str]]:
    """HTTP Basic/Digest credentials for the browser context, from the reserved
    secret names HTTP_USERNAME / HTTP_PASSWORD. Unlike form fills these answer
    the browser-level 401 challenge, so they must be set on the Playwright
    context — there is no page element to type a placeholder into. Per-user
    overlay applies like any other secret key."""
    if not secrets:
        return None
    username = secrets.get("HTTP_USERNAME")
    password = secrets.get("HTTP_PASSWORD")
    if not username and not password:
        return None
    return {"username": username or "", "password": password or ""}


def describe_available_secrets(ctx: "BrowserConnectionContext") -> str:
    """One line for tool errors/observations: which secret NAMES exist (never
    values), and which declared keys the user still has to provide."""
    names = sorted(ctx.secrets.keys())
    parts = []
    if names:
        parts.append("available secret parameters: " + ", ".join(names))
    missing = ctx.missing_user_secrets
    if missing:
        parts.append(
            "not yet provided (the user must add them via Connect on the browser connection): "
            + ", ".join(sorted(missing))
        )
    return "; ".join(parts) if parts else "no secret parameters are configured on this connection"


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
        # Resolved secret map for redaction (refreshed on every open()).
        self.secrets: Dict[str, str] = {}
        # Set once a secret value has been injected into this session (a fill
        # or a URL). Gates browser_vision: pixels can't be redacted.
        self.secret_injected = False

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
        prefix = f"{report_id}:"
        for sid in [sid for sid in self._sessions if sid == str(report_id) or sid.startswith(prefix)]:
            await self._close(sid)

    def get(self, session_id: str) -> Optional[BrowserSession]:
        s = self._sessions.get(session_id)
        if s:
            s.touch()
        return s

    async def open(self, ctx: "BrowserConnectionContext") -> BrowserSession:
        from playwright.async_api import async_playwright

        session_id = ctx.session_key
        async with self._lock:
            await self._evict_idle()
            existing = self._sessions.get(session_id)
            if existing:
                existing.touch()
                existing.patterns = ctx.patterns  # pick up config edits
                existing.allow_downloads = ctx.allow_downloads
                existing.secrets = dict(ctx.secrets)
                return existing
            if len(self._sessions) >= MAX_CONCURRENT_SESSIONS:
                # Evict the least-recently-used to honor the cap.
                lru = min(self._sessions.values(), key=lambda s: s.last_used)
                await self._close(lru.session_id)

            s = BrowserSession(session_id, ctx.patterns, ctx.allow_downloads)
            s.secrets = dict(ctx.secrets)
            s.playwright = await async_playwright().start()
            launch_kwargs: Dict[str, Any] = {"headless": True}
            exe = chromium_executable()
            if exe:
                launch_kwargs["executable_path"] = exe
            # Connection-level proxy wins; env (HTTPS_PROXY/NO_PROXY) is the
            # deployment-wide fallback. Safe at launch level because sessions
            # are keyed per (report, connection, user) — one browser, one proxy.
            proxy = ctx.proxy or _proxy_from_env()
            if proxy:
                launch_kwargs["proxy"] = proxy
            s.browser = await s.playwright.chromium.launch(**launch_kwargs)
            ctx_kwargs: Dict[str, Any] = {
                "viewport": DEFAULT_VIEWPORT,
                "accept_downloads": ctx.allow_downloads,
            }
            # HTTP Basic/Digest: reserved secret names HTTP_USERNAME /
            # HTTP_PASSWORD answer 401 challenges at the context level.
            http_creds = http_credentials_from_secrets(ctx.secrets)
            if http_creds:
                ctx_kwargs["http_credentials"] = http_creds
                s.secret_injected = True
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
