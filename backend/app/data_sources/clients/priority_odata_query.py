"""Query translation for Priority subforms.

A Priority subform (ORDERITEMS under ORDERS) has an EntityType but no
EntitySet, so `GET {root}/ORDERITEMS` is a 404 — its rows exist only nested
inside the parent, reachable as
`ORDERS?$expand=ORDERITEMS_SUBFORM($filter=...;$select=...)`.

The connector presents each subform as an ordinary table instead: one row per
line, carrying the columns of every form above it. This module is the
translation layer behind that table:

* `build_subform_request` turns `ORDERITEMS?$filter=...&$select=...` into the
  parent request. Every top-level `and` clause of `$filter` is routed to the
  level whose columns it names; a clause naming columns of two levels cannot be
  expressed as one Priority request and is refused rather than guessed.
  `$orderby`/`$top`/`$skip` are NOT sent — inside `$expand` they would apply
  per parent row ("top 10 per order"), so they are applied to the flattened
  result instead.
* `flatten_subform` / `flatten_expand` turn the nested response into rows.
* `apply_post` applies ordering, paging and projection to the flat frame.

Only the shape of a clause is inspected — which columns it names. The clause
text itself is passed through verbatim (with prefixed parent columns renamed),
so anything Priority's `$filter` accepts keeps working.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple
from urllib.parse import quote, unquote

import pandas as pd


class QueryError(ValueError):
    """A query the subform translation cannot express; the message says how to
    rewrite it."""


# ── query string ────────────────────────────────────────────────────────────

def split_top_level(text: str, seps: str) -> List[str]:
    """Split on any char of `seps` outside parentheses and quoted strings."""
    out, depth, quoted, cur = [], 0, False, []
    for ch in text:
        if ch == "'":
            quoted = not quoted  # '' inside a literal toggles twice: still quoted
        elif not quoted and ch == "(":
            depth += 1
        elif not quoted and ch == ")":
            depth -= 1
        if not quoted and depth == 0 and ch in seps:
            out.append("".join(cur))
            cur = []
            continue
        cur.append(ch)
    out.append("".join(cur))
    return [p.strip() for p in out if p.strip()]


def parse_query_string(query_string: str) -> Dict[str, str]:
    """`$filter=a eq 'x&y'&$top=5` → {"$filter": "a eq 'x&y'", "$top": "5"}.

    Percent-decoding happens first (agents send both raw and encoded forms),
    and `&` inside a quoted literal does not split. `+` is left alone: it is a
    timezone sign in DateTimeOffset literals, not a space.
    """
    opts: Dict[str, str] = {}
    for part in split_top_level(unquote(query_string or ""), "&"):
        key, sep, value = part.partition("=")
        if not sep:
            raise QueryError(f"Malformed query option '{part}' — expected name=value.")
        if key.strip() in opts:
            raise QueryError(f"{key.strip()} is given more than once; combine the conditions into one "
                             f"{key.strip()} with 'and'.")
        opts[key.strip()] = value.strip()
    return opts


# ── $filter tokens ──────────────────────────────────────────────────────────

_TOKEN_RE = re.compile(r"""
      (?P<ws>\s+)
    | (?P<str>'(?:[^']|'')*')
    | (?P<num>-?\d[\w\-:.+]*)
    | (?P<word>[A-Za-z_][A-Za-z0-9_]*)
    | (?P<sym>[(),/])
    """, re.X)

# Words that are never column references.
_KEYWORDS = {
    "and", "or", "not", "eq", "ne", "gt", "ge", "lt", "le", "has", "in",
    "add", "sub", "mul", "div", "mod", "null", "true", "false",
}


@dataclass
class _Tok:
    kind: str
    text: str


def tokenize(expr: str) -> List[_Tok]:
    toks, pos = [], 0
    while pos < len(expr):
        m = _TOKEN_RE.match(expr, pos)
        if not m:
            if expr[pos] == "'":
                raise QueryError("Unterminated string literal in $filter (a quote inside a value "
                                 "is written as two quotes: 'O''Brien').")
            raise QueryError(f"Cannot read $filter at '{expr[pos:pos + 20]}'.")
        toks.append(_Tok(m.lastgroup, m.group(m.lastgroup)))
        pos = m.end()
    return toks


def _depths(toks: Sequence[_Tok]) -> List[int]:
    """Paren depth *before* each token; raises on unbalanced parentheses."""
    out, depth = [], 0
    for t in toks:
        if t.text == ")":
            depth -= 1
            if depth < 0:
                raise QueryError("Unbalanced parentheses in $filter.")
        out.append(depth)
        if t.text == "(":
            depth += 1
    if depth != 0:
        raise QueryError("Unbalanced parentheses in $filter.")
    return out


def _strip(toks: List[_Tok]) -> List[_Tok]:
    while toks and toks[0].kind == "ws":
        toks = toks[1:]
    while toks and toks[-1].kind == "ws":
        toks = toks[:-1]
    return toks


def _wrapped(toks: List[_Tok]) -> bool:
    """True when the whole clause is one parenthesised group: `( ... )`."""
    if len(toks) < 2 or toks[0].text != "(" or toks[-1].text != ")":
        return False
    depth = 0
    for i, t in enumerate(toks):
        if t.text == "(":
            depth += 1
        elif t.text == ")":
            depth -= 1
            if depth == 0 and i != len(toks) - 1:
                return False
    return True


def split_and(toks: List[_Tok]) -> List[List[_Tok]]:
    """Top-level `and` conjuncts. A conjunct that is one parenthesised group is
    opened and split again, so `(A and B)` routes like `A and B`.

    `and` binds tighter than `or`, so `A or B and C` is `A or (B and C)` — not
    a conjunction at all. Any top-level `or` keeps the expression whole."""
    toks = _strip(toks)
    depths = _depths(toks)
    if any(d == 0 and t.kind == "word" and t.text.lower() == "or" for t, d in zip(toks, depths)):
        return [toks]
    parts, cur = [], []
    for t, d in zip(toks, depths):
        if d == 0 and t.kind == "word" and t.text.lower() == "and":
            parts.append(_strip(cur))
            cur = []
            continue
        cur.append(t)
    parts.append(_strip(cur))
    if any(not p for p in parts):
        raise QueryError("Empty condition around 'and' in $filter.")
    out: List[List[_Tok]] = []
    for p in parts:
        if _wrapped(p):
            inner = p[1:-1]
            sub = split_and(inner)
            out.extend(sub if len(sub) > 1 else [p])
        else:
            out.append(p)
    return out


def column_refs(toks: Sequence[_Tok]) -> List[int]:
    """Indexes of tokens that reference a column: a word that is neither a
    keyword nor a function name (a word directly followed by `(`)."""
    refs = []
    for i, t in enumerate(toks):
        if t.kind != "word" or t.text.lower() in _KEYWORDS:
            continue
        nxt = next((u for u in toks[i + 1:] if u.kind != "ws"), None)
        if nxt is not None and nxt.text == "(":
            continue
        if i + 1 < len(toks) and toks[i + 1].kind == "str":
            continue  # a typed literal: duration'P1D', binary'...'
        refs.append(i)
    return refs


# ── the subform table ───────────────────────────────────────────────────────

@dataclass
class Level:
    form: str
    keys: List[str]
    navigation: Optional[str] = None  # how the level above reaches this one


@dataclass
class SubformRoute:
    """A subform presented as a flat table: the path from its top-level form
    down to it, and which level each exposed column comes from."""
    name: str
    levels: List[Level]
    # exposed column name -> (form, original column name)
    columns: Dict[str, Tuple[str, str]] = field(default_factory=dict)

    @classmethod
    def from_metadata(cls, name: str, meta: dict) -> Optional["SubformRoute"]:
        path = meta.get("path")
        if not path:
            return None
        levels = [Level(form=s["form"], keys=list(s.get("keys") or []), navigation=s.get("navigation"))
                  for s in path]
        cols = {k: (v[0], v[1]) for k, v in (meta.get("columns") or {}).items()}
        return cls(name=name, levels=levels, columns=cols)

    def __post_init__(self):
        self._exposed = {origin: name for name, origin in self.columns.items()}

    @property
    def target(self) -> Level:
        return self.levels[-1]

    def level_index(self, form: str) -> int:
        return next(i for i, lv in enumerate(self.levels) if lv.form == form)

    def exposed(self, form: str, column: str) -> str:
        # A column added to the form after indexing keeps its own name.
        return self._exposed.get((form, column), column)

    def resolve(self, name: str) -> Tuple[int, str]:
        if name not in self.columns:
            raise QueryError(
                f"Column '{name}' does not exist on {self.name} or the forms above it "
                f"({', '.join(lv.form for lv in self.levels[:-1])}). Use the column names "
                f"listed for {self.name}."
            )
        form, col = self.columns[name]
        return self.level_index(form), col


@dataclass
class PostOps:
    """Operations applied to the flattened rows, not sent to Priority."""
    select: List[str] = field(default_factory=list)
    orderby: List[Tuple[str, bool]] = field(default_factory=list)  # (column, descending)
    top: Optional[int] = None
    skip: Optional[int] = None
    # The sort as the top-level form's own columns, when every $orderby column
    # belongs to it: Priority can then sort and page the parents itself, and
    # whole parents taken in that order give the lines in the right order.
    # None when a column of a lower level is sorted on.
    parent_orderby: Optional[List[Tuple[str, bool]]] = None
    # A $filter clause on the top-level form, or $since: what keeps a read of
    # every matching parent from being a read of the whole form.
    parent_bounded: bool = False
    # The top-level form's own conditions (and `$since`), for a request that
    # counts matching parents without their lines.
    parent_filter: Optional[str] = None
    since: Optional[str] = None


# Characters that keep their meaning in the request we build. Everything else
# inside an option value — `&`, `#`, `+`, `%`, non-ASCII — is percent-encoded,
# so a literal like 'R&D' cannot split the query.
_SAFE = "()$,;=:'/@!*-._~"


def _int_option(opts: Dict[str, str], key: str) -> Optional[int]:
    if key not in opts:
        return None
    value = opts[key]
    if not value.isdigit():
        raise QueryError(f"{key} must be a non-negative integer, got '{value}'.")
    return int(value)


def _parse_orderby(text: str) -> List[Tuple[str, bool]]:
    out = []
    for part in split_top_level(text, ","):
        bits = part.split()
        if len(bits) > 2 or (len(bits) == 2 and bits[1].lower() not in ("asc", "desc")):
            raise QueryError(f"Cannot read $orderby item '{part}' — expected 'COLUMN [asc|desc]'.")
        out.append((bits[0], len(bits) == 2 and bits[1].lower() == "desc"))
    return out


def build_subform_request(route: SubformRoute, query_string: str) -> Tuple[str, PostOps]:
    """Translate a query against the subform table into the parent request.

    Returns the request path (relative to the service root) and the
    operations to apply after flattening.
    """
    opts = parse_query_string(query_string)
    supported = {"$filter", "$select", "$orderby", "$top", "$skip", "$since"}
    if "$expand" in opts:
        raise QueryError(
            f"{route.name} is queried as a table, so $expand is not used on it. To read a "
            f"subform below it, query that subform's table by name."
        )
    unknown = [k for k in opts if k not in supported]
    if unknown:
        raise QueryError(
            f"Option {unknown[0]} is not supported when querying {route.name}. "
            f"Supported: {', '.join(sorted(supported))}."
        )

    n = len(route.levels)
    filters: List[List[str]] = [[] for _ in range(n)]
    if opts.get("$filter"):
        for clause in split_and(tokenize(opts["$filter"])):
            refs = column_refs(clause)
            levels = set()
            for i in refs:
                lvl, original = route.resolve(clause[i].text)
                levels.add(lvl)
                clause[i] = _Tok("word", original)
            if len(levels) > 1:
                forms = sorted({route.levels[lv].form for lv in levels}, key=route.level_index)
                text = "".join(t.text for t in clause)
                raise QueryError(
                    f"The condition '{text}' combines columns of {' and '.join(forms)}. A single "
                    f"condition may use columns of one form only; conditions on different forms "
                    f"must be joined with a top-level 'and'. For 'or' across forms, run one query "
                    f"per side and combine the results in pandas."
                )
            lvl = levels.pop() if levels else n - 1
            filters[lvl].append("".join(t.text for t in clause))

    post = PostOps(
        orderby=_parse_orderby(opts["$orderby"]) if opts.get("$orderby") else [],
        top=_int_option(opts, "$top"),
        skip=_int_option(opts, "$skip"),
    )
    if opts.get("$select"):
        post.select = split_top_level(opts["$select"], ",")
    post.parent_bounded = bool(filters[0]) or bool(opts.get("$since"))
    post.parent_filter = " and ".join(filters[0]) or None
    post.since = opts.get("$since") or None
    resolved = [(route.resolve(c), desc) for c, desc in post.orderby]
    if all(lvl == 0 for (lvl, _), _ in resolved):
        post.parent_orderby = [(original, desc) for (_, original), desc in resolved]

    # Columns each level must return: what $select and $orderby name, plus
    # every non-target level's keys (Priority requires parent keys in $select,
    # and they identify the line). No $select at all means "every column".
    selects: Optional[List[List[str]]] = None
    if post.select:
        selects = [list(lv.keys) if i < n - 1 else [] for i, lv in enumerate(route.levels)]
        for name in post.select + [c for c, _ in post.orderby]:
            lvl, original = route.resolve(name)
            if original not in selects[lvl]:
                selects[lvl].append(original)
        if not selects[-1]:
            # Only parent columns were asked for, but there is still one row
            # per line — the line's key keeps the lines in the response.
            selects[-1] = list(route.target.keys)
    else:
        for name, _ in post.orderby:
            route.resolve(name)

    def level_opts(i: int) -> List[str]:
        parts = []
        if filters[i]:
            parts.append("$filter=" + " and ".join(filters[i]))
        if selects is not None and selects[i]:
            parts.append("$select=" + ",".join(selects[i]))
        if i + 1 < n:
            child = route.levels[i + 1]
            inner = level_opts(i + 1)
            parts.append(f"$expand={child.navigation}" + (f"({';'.join(inner)})" if inner else ""))
        return parts

    top = level_opts(0)
    if opts.get("$since"):
        top.append(f"$since={opts['$since']}")
    query = "&".join(quote(p, safe=_SAFE) for p in top)
    return f"{route.levels[0].form}?{query}", post


def with_parent_page(request_path: str, orderby: List[Tuple[str, bool]],
                     top: int, skip: int) -> str:
    """Add paging of the top-level form to a request from
    `build_subform_request`: these options sit at the top level, outside any
    `$expand`, so they order and limit the parents, never lines within one."""
    parts = []
    if orderby:
        parts.append("$orderby=" + ",".join(f"{c} desc" if d else c for c, d in orderby))
    parts.append(f"$top={int(top)}")
    if skip:
        parts.append(f"$skip={int(skip)}")
    extra = "&".join(quote(p, safe=_SAFE) for p in parts)
    path, _, query = request_path.partition("?")
    return f"{path}?{query}&{extra}" if query else f"{path}?{extra}"


def parent_probe_path(route: SubformRoute, post: PostOps, skip: int) -> str:
    """A request that answers "do more than `skip` parents match?" cheaply:
    the parent's own conditions, its key only, no lines, one row."""
    parts = []
    if post.parent_filter:
        parts.append("$filter=" + post.parent_filter)
    parts.append("$select=" + ",".join(route.levels[0].keys))
    parts += ["$top=1", f"$skip={int(skip)}"]
    if post.since:
        parts.append(f"$since={post.since}")
    return f"{route.levels[0].form}?" + "&".join(quote(p, safe=_SAFE) for p in parts)


# ── flattening ──────────────────────────────────────────────────────────────

def _scalars(row: dict, skip: Sequence[str]) -> Dict[str, object]:
    # `@odata.*` and `NAV@odata.*` are response annotations, not columns.
    return {k: v for k, v in row.items() if "@odata" not in k and k not in skip}


def _children(value) -> List[dict]:
    """A navigation value as a list of rows. A collection subform arrives as a
    list; a single-entity one (Priority's text subforms) as one object, or
    null when empty."""
    if isinstance(value, dict):
        return [value]
    return [v for v in (value or []) if isinstance(v, dict)]


def flatten_subform(rows: List[dict], route: SubformRoute) -> List[dict]:
    """One output row per target-level entity, carrying every ancestor's
    columns under their exposed names. A parent with no lines contributes no
    rows, as a line table should."""
    out: List[dict] = []

    def walk(level: int, row: dict, acc: dict):
        lv = route.levels[level]
        nav = route.levels[level + 1].navigation if level + 1 < len(route.levels) else None
        merged = dict(acc)
        for k, v in _scalars(row, [nav] if nav else []).items():
            merged[route.exposed(lv.form, k)] = v
        if nav is None:
            out.append(merged)
            return
        for child in _children(row.get(nav)):
            walk(level + 1, child, merged)

    for r in rows:
        walk(0, r, {})
    return out


def nav_form(nav: str) -> str:
    # Priority names a subform navigation property <FORM>_SUBFORM.
    return nav[: -len("_SUBFORM")] if nav.endswith("_SUBFORM") else nav


def expand_chain(expand: str) -> List[str]:
    """`A($select=x;$expand=B(...))` → ["A", "B"]; refuses sibling expands,
    which would multiply rows when flattened."""
    chain: List[str] = []
    current = expand
    while current:
        items = split_top_level(current, ",")
        if len(items) > 1:
            names = ", ".join(i.split("(")[0].strip() for i in items)
            raise QueryError(
                f"Expanding several subforms at once ({names}) would multiply rows when "
                f"flattened and distort every sum. Expand one subform per query."
            )
        name, _, rest = items[0].partition("(")
        chain.append(name.strip())
        inner = rest[:-1] if rest.endswith(")") else ""
        current = ""
        for opt in split_top_level(inner, ";"):
            key, _, value = opt.partition("=")
            if key.strip() == "$expand":
                current = value.strip()
    return chain


def flatten_expand(rows: List[dict], root_form: str, chain: List[str],
                   origins: Optional[Dict[str, Tuple[str, str]]] = None) -> List[dict]:
    """Flatten an explicit `$expand` chain: one row per deepest entity. The
    deepest level keeps its column names; an ancestor column whose name is
    already taken is prefixed with its form (`ORDERS_STATDES`).

    The query was asked of the parent, so a parent with no children is kept
    as one row with the child columns empty — dropping it would read as "this
    order does not exist".

    `origins`, when given, is filled with {output column: (form, column)} —
    which form each column came from. A bare name can come from either level
    (the parent keeps it when the child did not select the same column), so
    only the flattener can say."""
    forms = [root_form] + [nav_form(n) for n in chain]
    out: List[dict] = []

    def walk(level: int, row: dict, acc: List[Tuple[int, str, str, object]]):
        nav = chain[level] if level < len(chain) else None
        here = acc + [(level, forms[level], k, v) for k, v in _scalars(row, [nav] if nav else []).items()]
        kids = _children(row.get(nav)) if nav else []
        if not kids:
            out.append(_name_columns(here, origins))
            return
        for child in kids:
            walk(level + 1, child, here)

    for r in rows:
        walk(0, r, [])
    return out


def unique_name(col: str, form: str, level: int, taken) -> str:
    """`col`, else `<FORM>_<col>`, else `<FORM><level>_<col>` — never a name
    already in `taken` (a child may itself own a column called ORDERS_X, and a
    form may appear at two levels)."""
    for candidate in (col, f"{form}_{col}", f"{form}{level}_{col}"):
        if candidate not in taken:
            return candidate
    n = 2
    while f"{form}{level}_{col}_{n}" in taken:
        n += 1
    return f"{form}{level}_{col}_{n}"


def _name_columns(cells: List[Tuple[int, str, str, object]],
                  origins: Optional[Dict[str, Tuple[str, str]]] = None) -> dict:
    """Deepest level first, then nearest ancestor: the first claim on a name
    wins and later ones get a prefixed name."""
    named: Dict[str, object] = {}
    for level, form, col, value in sorted(cells, key=lambda c: -c[0]):
        name = unique_name(col, form, level, named)
        named[name] = value
        if origins is not None:
            origins.setdefault(name, (form, col))
    return named


# ── post-processing ─────────────────────────────────────────────────────────

def apply_post(df: pd.DataFrame, post: PostOps, max_rows: Optional[int] = None) -> pd.DataFrame:
    """Order, then skip/top, then project — in that order, so "top 10 by price"
    means the 10 highest of all rows."""
    if post.orderby:
        missing = [c for c, _ in post.orderby if c not in df.columns]
        if df.empty and missing:
            df = df.reindex(columns=list(df.columns) + missing)
        df = df.sort_values(
            by=[c for c, _ in post.orderby],
            ascending=[not desc for _, desc in post.orderby],
            kind="mergesort",
            na_position="last",
        )
    if post.skip:
        df = df.iloc[post.skip:]
    if post.top is not None:
        df = df.iloc[: post.top]
    if max_rows:
        df = df.iloc[: int(max_rows)]
    if post.select:
        df = df.reindex(columns=post.select)
    return df.reset_index(drop=True)
