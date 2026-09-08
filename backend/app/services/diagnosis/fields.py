"""The field registry: the allowlist the query language may reference.

Everything that knows a field name reads this table — the parser (for
validation and "did you mean"), the compiler (for the column), the facet and
fields endpoints (for the builder and the syntax help), and later the
``list_agent_runs`` tool description. ``frontend/utils/diagnosisQuery.ts``
carries a copy of the parser-relevant part (name, aliases, type, values,
entity); an e2e test compares it against ``GET /console/diagnosis/fields``.

Types
-----
enum      one of ``values`` (case-insensitive)             ops: eq, any
text      substring-insensitive equality, ``*`` wildcards  ops: eq, any, wild
number    ``12`` ``1.5`` ``18k`` ``2.3m``                   ops: eq, any, cmp, range
duration  ``30s`` ``1.5m`` ``250ms`` ``2h`` or bare ms      ops: eq, any, cmp, range
money     ``$0.50`` or bare dollars                         ops: eq, any, cmp, range
date      ``2025-09-05`` ``2025-09`` ``today`` ``-7d``      ops: eq, cmp, range
boolean   ``true`` ``false``                                ops: eq
id        exact string                                     ops: eq, any
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

RUN = "run"
TOOL = "tool"

# Operators each type accepts. ``cmp`` covers > >= < <=.
_OPS: Dict[str, Tuple[str, ...]] = {
    "enum": ("eq", "any"),
    "text": ("eq", "any", "wild"),
    "number": ("eq", "any", "gt", "gte", "lt", "lte", "range"),
    "duration": ("eq", "any", "gt", "gte", "lt", "lte", "range"),
    "money": ("eq", "any", "gt", "gte", "lt", "lte", "range"),
    "date": ("eq", "gt", "gte", "lt", "lte", "range"),
    "boolean": ("eq",),
    "id": ("eq", "any"),
}


@dataclass(frozen=True)
class FieldSpec:
    name: str
    type: str
    entity: str
    help: str
    aliases: Tuple[str, ...] = ()
    values: Tuple[str, ...] = ()
    facetable: bool = False
    sortable: bool = False
    # Shown in the "+ Filter" builder (a few fields are id-only or internal).
    builder: bool = True

    @property
    def ops(self) -> Tuple[str, ...]:
        return _OPS[self.type]

    def to_public(self) -> dict:
        return {
            "name": self.name,
            "aliases": list(self.aliases),
            "type": self.type,
            "entity": self.entity,
            "values": list(self.values),
            "facetable": self.facetable,
            "sortable": self.sortable,
            "builder": self.builder,
            "help": self.help,
        }


FIELDS: List[FieldSpec] = [
    # --- agent runs -------------------------------------------------------
    FieldSpec("status", "enum", RUN, "Run outcome (stale = still running after an hour, i.e. never finished)",
              values=("success", "error", "in_progress", "stale", "sigkill"), facetable=True),
    FieldSpec("user", "text", RUN, "Who ran it (name or email)", facetable=True),
    FieldSpec("agent", "text", RUN, "Agent (data source) the run drew on", facetable=True),
    FieldSpec("platform", "enum", RUN, "Where the prompt came from",
              values=("web", "slack", "teams", "email", "mcp", "api"), facetable=True),
    FieldSpec("feedback", "enum", RUN, "User feedback on the answer", values=("positive", "negative", "none"), facetable=True),
    FieldSpec("judge.confidence", "number", RUN, "Judge score for answer quality, 1–5", aliases=("confidence",), sortable=True),
    FieldSpec("judge.instructions", "number", RUN, "Judge score for instruction coverage, 1–5", aliases=("coverage",), sortable=True),
    FieldSpec("judge.context", "number", RUN, "Judge score for context use, 1–5", sortable=True),
    FieldSpec("model", "text", RUN, "Planner model for the run", facetable=True),
    FieldSpec("provider", "text", RUN, "LLM provider for the run", facetable=True),
    FieldSpec("cost", "money", RUN, "LLM cost of the run in USD", sortable=True),
    FieldSpec("tokens", "number", RUN, "LLM tokens used by the run", sortable=True),
    FieldSpec("tokens.in", "number", RUN, "Prompt tokens", sortable=True, builder=False),
    FieldSpec("tokens.out", "number", RUN, "Completion tokens", sortable=True, builder=False),
    FieldSpec("duration", "duration", RUN, "Wall time of the run", sortable=True),
    FieldSpec("thinking", "duration", RUN, "Time before the first tool call", sortable=True, builder=False),
    FieldSpec("first_token", "duration", RUN, "Time to first streamed token", sortable=True, builder=False),
    FieldSpec("tools", "number", RUN, "Tool calls in the run", sortable=True),
    FieldSpec("tools.failed", "number", RUN, "Failed tool calls in the run", sortable=True),
    FieldSpec("report", "text", RUN, "Report title"),
    FieldSpec("turn", "number", RUN, "Position of the run in its report (1 = first prompt)", sortable=True),
    FieldSpec("report_id", "id", RUN, "Report id", builder=False),
    FieldSpec("run_id", "id", RUN, "Run id", builder=False),
    FieldSpec("version", "text", RUN, "Bag of words version that ran it", facetable=True, builder=False),
    FieldSpec("created", "date", RUN, "When the run started", sortable=True),
    FieldSpec("eval", "boolean", RUN, "Runs spawned by evals (hidden unless mentioned)", builder=False),
    FieldSpec("error", "text", RUN, "Run error message"),
    # --- tool calls (correlated: tool.* terms in one AND group match ONE call)
    FieldSpec("tool", "text", TOOL, "Tool name", aliases=("tool.name",), facetable=True),
    FieldSpec("tool.action", "text", TOOL, "Tool action", facetable=True),
    FieldSpec("tool.status", "enum", TOOL, "Tool call outcome", values=("success", "error", "in_progress", "stopped"), facetable=True),
    FieldSpec("tool.attempt", "number", TOOL, "Attempt number (retries start at 2)"),
    FieldSpec("tool.duration", "duration", TOOL, "Tool call wall time"),
    FieldSpec("tool.error", "text", TOOL, "Tool error message"),
    FieldSpec("table", "text", TOOL, "Table the tool call queried (schema.table or table)", facetable=True),
    FieldSpec("tool.args", "text", TOOL, "Text anywhere in the tool call's arguments"),
    FieldSpec("tool.output", "text", TOOL, "Text in the tool call's result summary"),
]

# Built-in quick filters, in display order. The chip is active when every term
# is present as a top-level conjunction of the query.
QUICK_FILTERS: List[Tuple[str, str]] = [
    ("errors", "status:error"),
    ("failed_queries", "tool:create_data tool.status:error"),
    ("negative_feedback", "feedback:negative"),
    ("low_confidence", "judge.confidence:<3"),
    ("low_coverage", "judge.instructions:<3"),
    ("slow", "duration:>30s"),
    ("expensive", "cost:>$0.50"),
    ("retried", "tool.attempt:>1"),
]

_BY_NAME: Dict[str, FieldSpec] = {}
for _f in FIELDS:
    _BY_NAME[_f.name] = _f
    for _a in _f.aliases:
        _BY_NAME[_a] = _f


def resolve(name: str) -> Optional[FieldSpec]:
    """Look a field up by name or alias, case-insensitively."""
    return _BY_NAME.get(name.lower())


def all_names() -> List[str]:
    return sorted(_BY_NAME.keys())


def public_fields() -> List[dict]:
    return [f.to_public() for f in FIELDS]


def _levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def suggest(name: str, limit: int = 3) -> List[str]:
    """Closest field names for an unknown one. Mirrored in the TS parser, so
    the ranking must stay a pure function of the names: prefix matches first,
    then edit distance ≤ 3, ties broken alphabetically."""
    low = name.lower()
    prefixed = sorted(n for n in _BY_NAME if n.startswith(low) and n != low)
    scored = sorted(
        ((_levenshtein(low, n), n) for n in _BY_NAME if n not in prefixed and n != low),
    )
    close = [n for d, n in scored if d <= 3]
    out: List[str] = []
    for n in prefixed + close:
        canonical = _BY_NAME[n].name
        if canonical not in out:
            out.append(canonical)
        if len(out) >= limit:
            break
    return out
