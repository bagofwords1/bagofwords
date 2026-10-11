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
