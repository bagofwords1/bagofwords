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


def _repeat_bound(pattern: str, i: int) -> tuple[int, int]:
    m = _COUNTED_REPEAT.match(pattern, i)
    if not m:
        return 1, i
    return max(int(g) for g in m.groups() if g) if any(m.groups()) else 1, m.end()


def _nested_repeat_product(pattern: str) -> int:
    stack = [1]
    worst = 1
    i = 0
    while i < len(pattern):
        c = pattern[i]
        if c == "\\":
            bound, i = _repeat_bound(pattern, i + 2)
            stack[-1] = max(stack[-1], bound)
            continue
        if c == "[":
            j = i + 2 if pattern[i + 1:i + 2] == "]" else i + 1
            while j < len(pattern) and pattern[j] != "]":
                j += 2 if pattern[j] == "\\" else 1
            bound, i = _repeat_bound(pattern, j + 1)
            stack[-1] = max(stack[-1], bound)
            continue
        if c == "(":
            stack.append(1)
            i += 1
            continue
        if c == ")" and len(stack) > 1:
            inner = stack.pop()
            bound, i = _repeat_bound(pattern, i + 1)
            stack[-1] = max(stack[-1], inner * bound)
            worst = max(worst, stack[-1])
            continue
        bound, i = _repeat_bound(pattern, i + 1)
        stack[-1] = max(stack[-1], bound)
        worst = max(worst, stack[-1])
    return max(worst, *stack)


def pattern_cost_problem(pattern: str) -> str | None:
    if len(pattern) > MAX_PATTERN_LENGTH:
        return f"longer than {MAX_PATTERN_LENGTH} characters"
    for m in _COUNTED_REPEAT.finditer(pattern):
        if any(g and int(g) > MAX_REPEAT_COUNT for g in m.groups()):
            return f"repeat count above {MAX_REPEAT_COUNT}"
    if _nested_repeat_product(pattern) > MAX_NESTED_REPEAT_PRODUCT:
        return f"nested repeat counts multiply above {MAX_NESTED_REPEAT_PRODUCT}"
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
