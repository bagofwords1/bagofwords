"""Precompiled artifact JSX must be byte-identical to babel-standalone's output.

The in-app artifact iframe can skip babel-standalone when the backend hands it
precompiled code (app.services.artifact_precompile). That is only safe if the
precompiled code is EXACTLY what Babel would have generated in the iframe for
the same <script type="text/babel"> — same presets, same filename, same
inline source map. These tests run the real vendored babel-standalone's
transformScriptTags in Chromium and compare.

They also pin the code-matching key shared with the frontend
(frontend/tests/unit/artifactPrecompiled.mjs asserts the same vectors) and the
"only one plain text/babel script" shape rule.
"""
import asyncio
import os

import pytest

from app.services import artifact_precompile as ap

try:
    from app.services.artifact_libs import _find_libs_dir
    LIBS = _find_libs_dir()
    HAVE_BABEL = bool(LIBS) and (LIBS / "babel-standalone.min.js").exists()
except Exception:  # pragma: no cover
    LIBS, HAVE_BABEL = None, False

try:
    import playwright  # noqa: F401
    HAVE_PLAYWRIGHT = True
except ImportError:  # pragma: no cover
    HAVE_PLAYWRIGHT = False

requires_browser_babel = pytest.mark.skipif(
    not (HAVE_BABEL and HAVE_PLAYWRIGHT),
    reason="needs vendored babel-standalone (scripts/download-vendor-libs.sh) and Playwright",
)


def wrap(src: str) -> str:
    return f'<script type="text/babel">{src}</script>'


# Shared with frontend/tests/unit/artifactPrecompiled.mjs — both sides must
# compute the same key or the host silently never uses precompiled code.
KEY_VECTORS = [
    ("", "0:811c9dc5"),
    ("abc", "3:1a47e90b"),
    ('<script type="text/babel">\nconst x = <div/>;\n</script>', "54:904bcd58"),
    ("ünïcødé — 😀 \r\n end", "19:699069e9"),
]


@pytest.mark.parametrize("code,key", KEY_VECTORS)
def test_source_key_matches_the_frontend_vectors(code, key):
    assert ap.source_key(code) == key


def test_source_key_differs_for_any_edit():
    base = wrap("\nfunction App(){ return <b>hi</b>; }\n")
    keys = {ap.source_key(base), ap.source_key(base + " "), ap.source_key(base.replace("hi", "ho"))}
    assert len(keys) == 3


@pytest.mark.parametrize("code", [
    "",
    "<div>not a script</div>",
    '<script type="module">import x from "y"</script>',
    '<script>plain()</script>',
    wrap("a") + wrap("b"),                      # two scripts
    "<style>.x{}</style>" + wrap("a"),          # extra markup
    wrap("const s = '<!-- x';"),                # tokenizer escape states
    wrap("   "),                                # empty
])
def test_only_a_single_plain_babel_script_is_precompilable(code):
    assert ap.extract_single_babel_script(code) is None


def test_inner_text_is_normalized_like_the_html_parser():
    code = '\n  <script type="text/babel">\r\nconst a = 1;\r\n</script>\n'
    assert ap.extract_single_babel_script(code) == "\nconst a = 1;\n"


SOURCES = [
    # JSX + hooks + template literals
    """
function App() {
  const [n, setN] = useState(0);
  return <div className={`p-${n}`} onClick={() => setN(n + 1)}>{n ?? 'none'}</div>;
}
ReactDOM.createRoot(document.getElementById('root')).render(<App />);
""",
    # classes with private fields/static, generators, async iteration, spread, optional chaining
    """
class Stats { static of(v) { return new Stats(v); } #v; constructor(v) { this.#v = [...v]; } get sum() { return this.#v.reduce((a, b) => a + b, 0); } }
function* pairs(r) { for (let i = 1; i < r.length; i++) yield [r[i - 1], r[i]]; }
async function f() { for await (const x of (async function* () { yield 1; })()) console.log(x?.y); }
const { a = 1, ...rest } = window.ARTIFACT_DATA ?? {};
""",
    # import lines (artifact-globals strips them after compilation) and unicode
    """
import React, { useState as useLocal } from 'react';
const label = "ünïcødé — 😀";
function App() { return <p title="&quot;x&quot;">{label}</p>; }
""",
]


async def _browser_babel_output(src: str) -> str:
    """The code babel-standalone's transformScriptTags appends for an inline
    text/babel script — captured the way the iframe would run it."""
    from playwright.async_api import async_playwright

    babel = (LIBS / "babel-standalone.min.js").read_text(encoding="utf-8")
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, executable_path=os.environ.get("BOW_CHROMIUM_EXECUTABLE") or None)
        try:
            page = await browser.new_page()
            await page.set_content("<html><head></head><body></body></html>")
            await page.add_script_tag(content=babel)
            # Capture (without executing) the script Babel appends.
            await page.evaluate("""() => {
              window.__captured = [];
              const orig = Node.prototype.appendChild;
              Node.prototype.appendChild = function (n) {
                if (n && n.tagName === 'SCRIPT' && !n.src) { window.__captured.push(n.text); return n; }
                return orig.call(this, n);
              };
            }""")
            await page.evaluate("""(src) => {
              const s = document.createElement('script');
              s.type = 'text/babel';
              s.text = src;
              document.body.insertBefore(s, null);
              Babel.transformScriptTags();
            }""", src)
            captured = await page.evaluate("() => window.__captured")
        finally:
            await browser.close()
    assert len(captured) == 1
    return captured[0]


@requires_browser_babel
@pytest.mark.asyncio
@pytest.mark.parametrize("src", SOURCES)
async def test_precompiled_output_is_byte_identical_to_in_browser_babel(src, tmp_path, monkeypatch):
    monkeypatch.setenv("BOW_ARTIFACT_JS_CACHE_DIR", str(tmp_path))
    compiled = await ap.compile_code(wrap(src))
    assert compiled, "a valid artifact compiles"
    assert compiled == await _browser_babel_output(src)


@requires_browser_babel
@pytest.mark.asyncio
async def test_a_syntax_error_is_left_to_the_browser(tmp_path, monkeypatch):
    monkeypatch.setenv("BOW_ARTIFACT_JS_CACHE_DIR", str(tmp_path))
    code = wrap("function App() { return <div>{oops</div>; }")
    assert await ap.compile_code(code) is None
    assert ap.get_cached(code) is None


@requires_browser_babel
@pytest.mark.asyncio
async def test_compiled_output_is_served_only_for_the_code_it_was_compiled_from(tmp_path, monkeypatch):
    monkeypatch.setenv("BOW_ARTIFACT_JS_CACHE_DIR", str(tmp_path))
    ap._mem.clear()  # earlier tests compiled the same source into memory
    from types import SimpleNamespace

    code_a = wrap(SOURCES[0])
    code_b = wrap(SOURCES[0].replace("p-", "q-"))
    assert await ap.compile_code(code_a)

    a = SimpleNamespace(mode="page", content={"code": code_a}, compiled_js=None, compiled_js_key=None)
    ap.attach_compiled(a)
    assert a.compiled_js == ap.get_cached(code_a) and a.compiled_js_key == ap.source_key(code_a)

    b = SimpleNamespace(mode="page", content={"code": code_b}, compiled_js=None, compiled_js_key=None)
    ap.attach_compiled(b)
    assert b.compiled_js is None and b.compiled_js_key is None

    # Survives a process restart through the disk cache.
    ap._mem.clear()
    assert ap.get_cached(code_a) == a.compiled_js


def test_non_page_artifacts_never_get_compiled_code():
    from types import SimpleNamespace
    for mode, content in [("slides", {"code": wrap("x")}), ("doc", {"markdown": "# x"}), ("page", {})]:
        a = SimpleNamespace(mode=mode, content=content, compiled_js=None, compiled_js_key=None)
        ap.attach_compiled(a)
        assert a.compiled_js is None


@pytest.mark.skipif(not HAVE_BABEL, reason="needs vendored babel-standalone to key the cache")
@pytest.mark.asyncio
async def test_an_unavailable_compiler_backs_off_instead_of_retrying_every_view(tmp_path, monkeypatch, caplog):
    """No Chromium on this deployment: artifacts keep compiling in the browser,
    and views do not each pay (and log) a failed launch."""
    import logging
    monkeypatch.setenv("BOW_ARTIFACT_JS_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("BOW_CHROMIUM_EXECUTABLE", str(tmp_path / "no-such-chrome"))
    monkeypatch.setattr(ap, "_unavailable_until", 0.0)
    ap._mem.clear()
    caplog.set_level(logging.WARNING, logger=ap.__name__)
    for i in range(4):
        assert await ap.compile_code(wrap(f"const v{i} = <b>{i}</b>;")) is None
    unavailable = [r for r in caplog.records if "unavailable" in r.getMessage()]
    assert len(unavailable) == 1


@requires_browser_babel
@pytest.mark.asyncio
async def test_a_rejected_source_is_not_recompiled_on_every_view(tmp_path, monkeypatch):
    import time as _time
    monkeypatch.setenv("BOW_ARTIFACT_JS_CACHE_DIR", str(tmp_path))
    code = wrap("function App() { return <div>{nope</div>; }")
    assert await ap.compile_code(code) is None
    started = _time.monotonic()
    for _ in range(3):
        assert await ap.compile_code(code) is None
    # Three browser launches would take seconds; a remembered rejection is instant.
    assert _time.monotonic() - started < 0.5
