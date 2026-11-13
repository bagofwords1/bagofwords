"""Time-bounded regex matching for model-written patterns.

Tool inputs such as search_agents / describe_tables query terms are written by
the LLM and matched synchronously on the event loop. A backtracking pattern
("(\\w+_?)+#") against table names can take seconds per name with stdlib `re`,
which has no timeout — the single web worker then stops answering requests and
health checks. These helpers run patterns through the `regex` package with a
per-search timeout instead.
"""
import logging
from collections.abc import Iterable
from dataclasses import dataclass

import regex

logger = logging.getLogger(__name__)

# Per-search budget. Table names and descriptions are short, so a sane pattern
# finishes in microseconds; anything that hits this is runaway backtracking.
SEARCH_TIMEOUT_S = 0.05


class BoundedPattern:
    """A compiled pattern whose `search` can't run longer than the timeout.

    A pattern that times out once is dropped (it would time out again on every
    remaining name and add up), so its cost is bounded per search call, not per
    name.
    """

    def __init__(self, compiled):
        self._rx = compiled
        self.dead = False

    @property
    def pattern(self) -> str:
        return self._rx.pattern

    def search(self, text: str) -> bool:
        if self.dead:
            return False
        try:
            return self._rx.search(text or "", timeout=SEARCH_TIMEOUT_S) is not None
        except TimeoutError:
            self.dead = True
            logger.warning("regex pattern %r timed out; ignoring it", self._rx.pattern)
            return False


MAX_PATTERN_LENGTH = 200
MAX_REPEAT_COUNT = 100
MAX_NESTED_REPEAT_PRODUCT = 1000
_COUNTED_REPEAT = regex.compile(r"\{(\d*)(?:,(\d*))?\}")


def _quantifier(pattern: str, i: int) -> tuple[int, bool, int]:
    """(largest count, unbounded, end) of the quantifier at `i`; (1, False, i) if none.

    `*`, `+` and `{n,}` are unbounded; a trailing lazy `?` or possessive `+` is
    part of the quantifier.
    """
    if i < len(pattern) and pattern[i] in "*+?":
        bound, unbounded, end = 1, pattern[i] != "?", i + 1
    else:
        m = _COUNTED_REPEAT.match(pattern, i)
        if not m:
            return 1, False, i
        low, high = m.groups()
        counts = [int(g) for g in (low, high) if g]
        bound, unbounded, end = max(counts) if counts else 1, high == "", m.end()
    if end < len(pattern) and pattern[end] in "?+":
        end += 1
    return bound, unbounded, end


@dataclass
class _Frame:
    product: int = 1        # largest product of nested counted repeats inside
    repeats: bool = False   # holds a quantifier that matches more than once
    unbounded: bool = False  # holds an unbounded quantifier


def _repeat_shape(pattern: str) -> tuple[int, bool]:
    """(largest product of nested counted repeats, whether a repeat is nested in an unbounded one).

    The second covers the classic catastrophic shapes `(a+)+`, `(a*)*` and
    `(\\w+){2,}`: a repeated group that holds a repeat, where either is unbounded.
    """
    stack = [_Frame()]
    nested_unbounded = False
    i = 0

    def apply(frame: _Frame, inner: _Frame | None, at: int) -> int:
        nonlocal nested_unbounded
        bound, unbounded, end = _quantifier(pattern, at)
        many = bound > 1 or unbounded
        if inner is not None:
            if (unbounded and inner.repeats) or (many and inner.unbounded):
                nested_unbounded = True
            frame.product = max(frame.product, inner.product * bound)
            frame.repeats |= inner.repeats
            frame.unbounded |= inner.unbounded
        else:
            frame.product = max(frame.product, bound)
        frame.repeats |= many
        frame.unbounded |= unbounded
        return end

    while i < len(pattern):
        c = pattern[i]
        if c == "\\":
            i = apply(stack[-1], None, i + 2)
        elif c == "[":
            j = i + 1
            if pattern[j:j + 1] == "^":
                j += 1
            if pattern[j:j + 1] == "]":
                j += 1
            while j < len(pattern) and pattern[j] != "]":
                j += 2 if pattern[j] == "\\" else 1
            i = apply(stack[-1], None, j + 1)
        elif c == "(":
            stack.append(_Frame())
            i += 1
        elif c == ")" and len(stack) > 1:
            inner = stack.pop()
            i = apply(stack[-1], inner, i + 1)
        else:
            i = apply(stack[-1], None, i + 1)
    while len(stack) > 1:  # unbalanced; compile rejects it, but stay total
        inner = stack.pop()
        stack[-1].product = max(stack[-1].product, inner.product)
    return stack[0].product, nested_unbounded


def pattern_cost_problem(pattern: str) -> str | None:
    if len(pattern) > MAX_PATTERN_LENGTH:
        return f"longer than {MAX_PATTERN_LENGTH} characters"
    for m in _COUNTED_REPEAT.finditer(pattern):
        if any(g and int(g) > MAX_REPEAT_COUNT for g in m.groups()):
            return f"repeat count above {MAX_REPEAT_COUNT}"
    product, nested_unbounded = _repeat_shape(pattern)
    if product > MAX_NESTED_REPEAT_PRODUCT:
        return f"nested repeat counts multiply above {MAX_NESTED_REPEAT_PRODUCT}"
    if nested_unbounded:
        return "a repeated group holds another repeat and one of them is unbounded, as in (a+)+"
    return None


def compile_bounded(pattern: str, flags: int = 0) -> BoundedPattern | None:
    """Compile `pattern` (stdlib-`re` syntax); None when it is invalid."""
    try:
        return BoundedPattern(regex.compile(pattern, flags))
    except (regex.error, TypeError, ValueError):
        return None


def compile_bounded_all(patterns: Iterable[str] | None, flags: int = 0) -> list[BoundedPattern]:
    """Compile each valid pattern; invalid ones are skipped."""
    out: list[BoundedPattern] = []
    for p in patterns or []:
        c = compile_bounded(p, flags)
        if c is not None:
            out.append(c)
    return out
