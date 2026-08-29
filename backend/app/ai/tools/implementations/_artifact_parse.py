"""Fast parse gate for planner-authored artifact code.

Runs the SAME parser that judges the code at render time (the vendored
babel-standalone) in a blank Playwright page — no React, no data payload, no
networkidle — so a syntax error costs ~1-3s instead of a full ~10s validation
render. Authoritative by construction: there is no second parser whose opinion
could drift from the runtime's. Shares its mechanics with
app.services.jsx_transpile (the export-time transpiler).

On failure, ``bracket_balance_hint`` enriches Babel's often-useless column
pointer ("expected ',' at col 189" of a 280-char line) with a heuristic
"1 unclosed '(' — likely opened at line N, col M" note. The hint is ONLY ever
attached to an already-confirmed Babel failure — never used as a gate — so its
one known blind spot (bare brackets inside JSX text) can mislabel a hint but
can never reject valid code.
"""
from __future__ import annotations

import logging
import os
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

_OPENERS = {"(": ")", "[": "]", "{": "}"}
_CLOSERS = {v: k for k, v in _OPENERS.items()}


def bracket_balance_hint(code: str) -> Optional[str]:
    """Heuristic bracket-balance report for a failed parse.

    String- ('', "", ``, with escapes and ${} template holes) and comment-
    (//, /* */) aware. Does NOT understand JSX text, so a bare bracket in
    visible text can skew the count — acceptable because this only annotates
    an already-failed parse. Returns None when balanced.
    """
    stack: List[Tuple[str, int, int]] = []  # (char, line, col) 1-based
    extra: Optional[Tuple[str, int, int]] = None
    line, col = 1, 0
    state: List[str] = []  # nesting of 'sq'|'dq'|'tpl' (template holes pop back to code)
    i, n = 0, len(code)
    while i < n:
        ch = code[i]
        col += 1
        if ch == "\n":
            line += 1
            col = 0
            i += 1
            continue
        mode = state[-1] if state else None
        if mode in ("sq", "dq", "tpl"):
            if ch == "\\":
                i += 2
                col += 1
                continue
            if (mode == "sq" and ch == "'") or (mode == "dq" and ch == '"') or (mode == "tpl" and ch == "`"):
                state.pop()
            elif mode == "tpl" and ch == "$" and i + 1 < n and code[i + 1] == "{":
                state.append("code")  # template hole: brackets count again
                stack.append(("{", line, col + 1))
                i += 2
                col += 1
                continue
            i += 1
            continue
        # code mode (top level or inside a ${} hole)
        if ch == "/" and i + 1 < n and code[i + 1] == "/":
            nl = code.find("\n", i)
            if nl == -1:
                break
            i = nl
            continue
        if ch == "/" and i + 1 < n and code[i + 1] == "*":
            end = code.find("*/", i + 2)
            seg = code[i:end + 2] if end != -1 else code[i:]
            line += seg.count("\n")
            if "\n" in seg:
                col = len(seg) - seg.rfind("\n") - 1
            else:
                col += len(seg) - 1
            i = (end + 2) if end != -1 else n
            continue
        if ch == "'":
            state.append("sq")
        elif ch == '"':
            state.append("dq")
        elif ch == "`":
            state.append("tpl")
        elif ch in _OPENERS:
            stack.append((ch, line, col))
        elif ch in _CLOSERS:
            if stack and stack[-1][0] == _CLOSERS[ch]:
                popped = stack.pop()
                # closing a template-hole brace returns to template mode
                if popped[0] == "{" and state and state[-1] == "code":
                    state.pop()
            elif extra is None:
                extra = (ch, line, col)
        i += 1

    parts = []
    if stack:
        opener, oline, ocol = stack[-1]
        parts.append(
            f"{len(stack)} unclosed '{opener}'-style bracket(s) — the innermost "
            f"'{opener}' was opened at line {oline}, col {ocol} and never closed"
        )
    if extra is not None:
        ch, eline, ecol = extra
        parts.append(f"an extra '{ch}' with no matching opener at line {eline}, col {ecol}")
    if not parts:
        return None
    return "bracket-balance hint (heuristic; JSX text can skew it): " + "; ".join(parts)


async def parse_check_page_code(code: str, timeout_ms: int = 8000) -> Optional[str]:
    """Return the Babel parse error (hint-enriched) for page code, or None.

    Accepts either bare JSX or the stored `<script type="text/babel">` wrapper
    shape. Any infrastructure failure (no Playwright, browser unavailable,
    timeout) returns None — the gate silently defers to full render
    validation, same fallback philosophy as the screenshot pipeline.
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return None
    try:
        from app.services.artifact_libs import _find_libs_dir, _read_lib
        from app.services.jsx_transpile import extract_babel_source

        libs_dir = _find_libs_dir()
        if libs_dir is None:
            raise FileNotFoundError("vendored libs dir not found")
        babel_src = _read_lib(libs_dir, "babel-standalone.min.js")
    except Exception as e:
        logger.warning(f"parse gate unavailable (libs missing): {e}")
        return None

    # Stored artifact code carries a <script type="text/babel"> wrapper; the
    # parser must see only the JSX inside it (the wrapper itself would parse
    # as a stray JSX element and produce a bogus error).
    src = extract_babel_source(code) or code

    # Parse-only intent, but Babel exposes transform, not parse; presets match
    # what babel-standalone applies to text/babel blocks at render time.
    check = """
        (source) => {
          try {
            Babel.transform(source, { presets: [['react', { runtime: 'classic' }]], sourceType: 'script' });
            return null;
          } catch (e) {
            return String((e && e.message) || e).split("\\n")[0].slice(0, 500);
          }
        }
    """
    try:
        async with async_playwright() as p:
            exe = os.environ.get("BOW_CHROMIUM_EXECUTABLE") or None
            browser = await p.chromium.launch(headless=True, executable_path=exe)
            try:
                page = await browser.new_page(viewport={"width": 320, "height": 200})
                page.set_default_timeout(timeout_ms)
                await page.add_script_tag(content=babel_src)
                err = await page.evaluate(check, src)
            finally:
                await browser.close()
    except Exception as e:
        logger.warning(f"parse gate skipped (browser unavailable): {e}")
        return None
    if not err or not isinstance(err, str):
        return None
    hint = None
    try:
        hint = bracket_balance_hint(src)
    except Exception:
        hint = None
    return err + (f"\n{hint}" if hint else "")
