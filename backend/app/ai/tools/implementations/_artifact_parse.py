"""Check generated page JSX with the iframe's vendored Babel compiler.

Playwright ships a Node executable even on deployments without a browser.
Artifact source is passed as data to Babel and is never executed.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
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


class ParseCheckUnavailable(RuntimeError):
    """The server could not verify the candidate source."""


_PARSE_SCRIPT = """
const fs = require('node:fs');
const context = {};
require('node:vm').runInNewContext(fs.readFileSync(process.argv[1], 'utf8'), context);
const babel = context.Babel;
const source = fs.readFileSync(0, 'utf8');
try {
    babel.transform(source, { presets: [['react', {runtime: 'classic'}]], sourceType: 'script' });
} catch (error) {
    if (error.code !== 'BABEL_PARSE_ERROR') throw error;
    process.stdout.write(String(error.message).slice(0, 1500));
    process.exitCode = 2;
}
"""


async def parse_check_page_code(code: str, timeout_ms: int = 8000) -> Optional[str]:
    """Return a syntax diagnostic or None on success. Raise on infrastructure failure."""
    from app.services.artifact_libs import _find_libs_dir
    from app.services.jsx_transpile import extract_babel_source

    src = extract_babel_source(code) or code
    libs = _find_libs_dir()
    try:
        from playwright._impl._driver import compute_driver_executable

        if libs is None:
            raise FileNotFoundError("vendored artifact libraries missing")
        node, _ = compute_driver_executable()
        proc = await asyncio.create_subprocess_exec(
            node, "--max-old-space-size=192", "-e", _PARSE_SCRIPT,
            str(Path(libs).resolve() / "babel-standalone.min.js"),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(src.encode()), timeout_ms / 1000)
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
        if proc.returncode == 0:
            return None
        if proc.returncode == 2 and out:
            hint = bracket_balance_hint(src)
            return out.decode(errors="replace") + (f"\n{hint}" if hint else "")
        raise RuntimeError(f"Babel exited {proc.returncode}: {err[:300].decode(errors='replace')}")
    except (ImportError, OSError, RuntimeError) as exc:
        logger.exception("Artifact JSX validation unavailable")
        raise ParseCheckUnavailable("Server could not run the artifact JSX compiler.") from exc
