"""Query matching for ``search_mcps`` tool discovery.

Kept dependency-free (stdlib only, no app imports) so the matching behaviour
can be unit-tested in isolation without standing up the rest of the backend.

The query passed to ``search_mcps`` is a *relevance hint*, never a hard filter.
Discovery must never return an empty list when tools actually exist — an empty
result dead-ends the agent into guessing argument shapes instead of fetching a
real input schema. See :func:`filter_tools_by_query` for the exact rules.
"""

from __future__ import annotations

import re
from fnmatch import fnmatch
from typing import List, Sequence

_TOKEN_RE = re.compile(r"[^a-z0-9_]+")
_MIN_TOKEN_LEN = 3


def _haystacks(tool) -> tuple[str, str]:
    name = (getattr(tool, "name", None) or "").lower()
    description = (getattr(tool, "description", None) or "").lower()
    return name, description


def _glob_match(tool, pattern: str) -> bool:
    name, description = _haystacks(tool)
    return fnmatch(name, pattern) or fnmatch(description, pattern)


def filter_tools_by_query(tools: Sequence, query: str | None) -> List:
    """Filter and rank MCP/API tools by ``query``.

    Behaviour (in priority order):

    - **Empty/blank query** → return all tools unchanged.
    - **Wildcard query** (contains ``*`` or ``?``) → glob match
      (case-insensitive ``fnmatch``) against each tool's name and description.
      e.g. ``search_*`` → every tool whose name starts with ``search_``;
      ``*contact*`` → anything mentioning "contact".
    - **Plain query** → tokenize (alphanumeric/underscore runs ≥ 3 chars) and
      rank tools by how many tokens appear in their name/description, keeping
      only tools that match at least one token. Higher token-hit counts rank
      first; ties keep their original order.

    In every non-empty case, if nothing matches (an over-specific,
    natural-language, or id-shaped query) the function falls back to returning
    *all* tools — better to hand back every schema than none.
    """
    tools = list(tools)
    if not query or not query.strip():
        return tools

    q = query.strip().lower()

    # Wildcard mode: explicit glob pattern against name/description.
    if "*" in q or "?" in q:
        matched = [t for t in tools if _glob_match(t, q)]
        return matched or tools

    # Plain mode: token relevance scoring.
    tokens = [tok for tok in _TOKEN_RE.split(q) if len(tok) >= _MIN_TOKEN_LEN]
    if not tokens:
        return tools

    scored = []
    for t in tools:
        name, description = _haystacks(t)
        hay = f"{name} {description}"
        score = sum(1 for tok in tokens if tok in hay)
        if score > 0:
            scored.append((score, t))
    # Stable sort by descending score keeps original order within equal scores.
    scored.sort(key=lambda pair: -pair[0])
    matched = [t for _, t in scored]
    return matched or tools
