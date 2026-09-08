"""Parser for the diagnosis query language.

Mirrored line-for-line in ``frontend/utils/diagnosisQuery.ts``; both are held
to the same golden fixture (``backend/tests/fixtures/diagnosis_queries.json``),
so every behaviour here — token boundaries, error positions, error wording,
canonical form — is a contract. Change both or neither.

Grammar (whitespace-separated terms, implicit AND):

    query      := orExpr
    orExpr     := andExpr ( "OR" andExpr )*
    andExpr    := notExpr ( ("AND")? notExpr )*
    notExpr    := ("NOT" | "-")? primary
    primary    := "(" orExpr ")" | term
    term       := field ":" value
                | field ":" "(" value ("OR" value)* ")"
                | phrase | word
    value      := (">" | ">=" | "<" | "<=") scalar | scalar ".." scalar | scalar
    scalar     := phrase | word          (typed per field, see fields.py)

The AST is JSON: ``{"v": 1, "root": node}`` where node is one of
``{"t": "and"|"or", "c": [...]}``, ``{"t": "not", "c": node}``,
``{"t": "term", "field", "op", "values": [literal, ...]}`` or
``{"t": "text", "phrase": bool, "s": str}``. Positions ride along as ``_pos``
/ ``_end`` and are stripped for golden comparison.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from app.services.diagnosis import fields as F

AST_VERSION = 1
MAX_LENGTH = 1000
MAX_TERMS = 40
MAX_DEPTH = 4

KEYWORDS = ("AND", "OR", "NOT")
_CMP_OPS = (">=", "<=", ">", "<")
_CMP_NAME = {">": "gt", ">=": "gte", "<": "lt", "<=": "lte"}
_FIELD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.]*")
_NUMBER_RE = re.compile(r"^(\d+(?:\.\d+)?)([kKmM])?$")
_DURATION_RE = re.compile(r"^(\d+(?:\.\d+)?)(ms|s|m|h)?$")
_MONEY_RE = re.compile(r"^\$?(\d+(?:\.\d+)?)$")
_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MONTH_RE = re.compile(r"^\d{4}-\d{2}$")
_REL_RE = re.compile(r"^-(\d+)([dh])$")

_TYPE_WORD = {
    "number": "number",
    "duration": "duration",
    "money": "amount in USD",
    "date": "date",
    "boolean": "true or false",
}


class QueryError(Exception):
    def __init__(self, position: int, message: str, suggestions: Optional[List[str]] = None):
        super().__init__(message)
        self.position = position
        self.message = message
        self.suggestions = suggestions or []

    def to_dict(self) -> dict:
        return {"position": self.position, "message": self.message, "suggestions": list(self.suggestions)}


@dataclass
class Token:
    type: str  # field | colon | op | value | keyword | paren | text | phrase
    start: int
    end: int

    def to_dict(self) -> dict:
        return {"type": self.type, "start": self.start, "end": self.end}


@dataclass
class ParseResult:
    ast: Dict[str, Any]
    tokens: List[Token] = field(default_factory=list)

    @property
    def canonical(self) -> str:
        return canonical(self.ast)


# ---------------------------------------------------------------------------
# Literals
# ---------------------------------------------------------------------------

def _lit(kind: str, raw: str, **extra) -> dict:
    d = {"kind": kind, "raw": raw}
    d.update(extra)
    return d


def parse_scalar(spec: F.FieldSpec, raw: str, is_phrase: bool, position: int) -> dict:
    """Type a raw scalar against a field. Mirrored in TS (``parseScalar``)."""
    t = spec.type
    if t == "enum":
        low = raw.lower()
        if low not in spec.values:
            raise QueryError(
                position,
                f'Unknown value "{raw}" for {spec.name}. Expected one of: {", ".join(spec.values)}',
                list(spec.values),
            )
        return _lit("word", raw, s=low)
    if t in ("text", "id"):
        if is_phrase:
            return _lit("phrase", raw, s=raw)
        if "*" in raw and t == "text":
            return _lit("wild", raw, s=raw)
        return _lit("word", raw, s=raw)
    if t == "number":
        m = _NUMBER_RE.match(raw)
        if not m:
            raise QueryError(position, f'Expected a number for {spec.name}, got "{raw}"')
        n = float(m.group(1))
        suffix = (m.group(2) or "").lower()
        if suffix == "k":
            n *= 1000
        elif suffix == "m":
            n *= 1_000_000
        return _lit("number", raw, n=n)
    if t == "duration":
        m = _DURATION_RE.match(raw)
        if not m:
            raise QueryError(position, f'Expected a duration for {spec.name}, got "{raw}"')
        n = float(m.group(1))
        unit = m.group(2) or "ms"
        ms = n * {"ms": 1, "s": 1000, "m": 60_000, "h": 3_600_000}[unit]
        return _lit("duration", raw, ms=ms)
    if t == "money":
        m = _MONEY_RE.match(raw)
        if not m:
            raise QueryError(position, f'Expected an amount in USD for {spec.name}, got "{raw}"')
        return _lit("money", raw, usd=float(m.group(1)))
    if t == "date":
        low = raw.lower()
        if _DAY_RE.match(raw):
            return _lit("date", raw, s=raw, gran="day")
        if _MONTH_RE.match(raw):
            return _lit("date", raw, s=raw, gran="month")
        if low in ("today", "yesterday"):
            return _lit("named", raw, s=low)
        m = _REL_RE.match(low)
        if m:
            return _lit("reldate", raw, n=-int(m.group(1)), unit=m.group(2))
        raise QueryError(position, f'Expected a date for {spec.name}, got "{raw}"')
    if t == "boolean":
        low = raw.lower()
        if low in ("true", "yes", "1"):
            return _lit("bool", raw, b=True)
        if low in ("false", "no", "0"):
            return _lit("bool", raw, b=False)
        raise QueryError(position, f'Expected true or false for {spec.name}, got "{raw}"')
    raise QueryError(position, f'Unknown field type for {spec.name}')  # pragma: no cover


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

class _Parser:
    def __init__(self, text: str):
        self.s = text
        self.n = len(text)
        self.i = 0
        self.tokens: List[Token] = []
        self.terms = 0
        self.depth = 0

    # -- scanning helpers -------------------------------------------------
    def _ws(self) -> None:
        while self.i < self.n and self.s[self.i].isspace():
            self.i += 1

    def _peek(self, k: int = 0) -> str:
        j = self.i + k
        return self.s[j] if j < self.n else ""

    def _at_end(self) -> bool:
        self._ws()
        return self.i >= self.n

    def _keyword_at(self, kw: str) -> bool:
        """Is the bare word at the cursor exactly ``kw`` (case-insensitive)?"""
        self._ws()
        end = self.i + len(kw)
        if self.s[self.i:end].upper() != kw:
            return False
        nxt = self.s[end] if end < self.n else ""
        return nxt == "" or nxt.isspace() or nxt in "()"

    def _read_word(self) -> str:
        """A run of non-space characters that stops at ``(``, ``)`` and quotes."""
        start = self.i
        while self.i < self.n and not self.s[self.i].isspace() and self.s[self.i] not in "()\"":
            self.i += 1
        return self.s[start:self.i]

    def _read_phrase(self) -> str:
        start = self.i  # at the opening quote
        self.i += 1
        while self.i < self.n and self.s[self.i] != '"':
            self.i += 1
        if self.i >= self.n:
            raise QueryError(start, "Unterminated quote")
        self.i += 1
        return self.s[start + 1:self.i - 1]

    # -- grammar ------------------------------------------------------------
    def parse(self) -> Optional[dict]:
        if self._at_end():
            return None
        node = self.parse_or()
        self._ws()
        if self.i < self.n:
            if self.s[self.i] == ")":
                raise QueryError(self.i, 'Unexpected ")"')
            raise QueryError(self.i, "Unexpected input")  # pragma: no cover
        return node

    def parse_or(self) -> dict:
        first = self.parse_and()
        children = [first]
        while True:
            if self._keyword_at("OR"):
                kw_pos = self.i
                self.tokens.append(Token("keyword", self.i, self.i + 2))
                self.i += 2
                if self._at_end() or self._peek() == ")":
                    raise QueryError(kw_pos, 'Expected a term after "OR"')
                children.append(self.parse_and())
            else:
                break
        if len(children) == 1:
            return first
        return {"t": "or", "c": children, "_pos": children[0]["_pos"], "_end": children[-1]["_end"]}

    def parse_and(self) -> dict:
        children = [self.parse_not()]
        while True:
            self._ws()
            if self.i >= self.n or self._peek() == ")" or self._keyword_at("OR"):
                break
            if self._keyword_at("AND"):
                kw_pos = self.i
                self.tokens.append(Token("keyword", self.i, self.i + 3))
                self.i += 3
                if self._at_end() or self._peek() == ")":
                    raise QueryError(kw_pos, 'Expected a term after "AND"')
            children.append(self.parse_not())
        if len(children) == 1:
            return children[0]
        return {"t": "and", "c": children, "_pos": children[0]["_pos"], "_end": children[-1]["_end"]}

    def parse_not(self) -> dict:
        self._ws()
        start = self.i
        if self._keyword_at("NOT"):
            self.tokens.append(Token("keyword", self.i, self.i + 3))
            self.i += 3
            if self._at_end() or self._peek() == ")":
                raise QueryError(start, 'Expected a term after "NOT"')
            child = self.parse_not()
            return {"t": "not", "c": child, "_pos": start, "_end": child["_end"]}
        if self._peek() == "-" and self._peek(1) and not self._peek(1).isspace():
            self.tokens.append(Token("keyword", self.i, self.i + 1))
            self.i += 1
            child = self.parse_not()
            return {"t": "not", "c": child, "_pos": start, "_end": child["_end"]}
        return self.parse_primary()

    def parse_primary(self) -> dict:
        self._ws()
        start = self.i
        ch = self._peek()
        if ch == "(":
            self.depth += 1
            if self.depth > MAX_DEPTH:
                raise QueryError(start, f"Too much nesting (max {MAX_DEPTH} levels)")
            self.tokens.append(Token("paren", self.i, self.i + 1))
            self.i += 1
            if self._at_end():
                raise QueryError(start, 'Expected ")"')
            if self._peek() == ")":
                raise QueryError(start, 'Expected a term inside "( )"')
            node = self.parse_or()
            self._ws()
            if self._peek() != ")":
                raise QueryError(start, 'Expected ")"')
            self.tokens.append(Token("paren", self.i, self.i + 1))
            self.i += 1
            self.depth -= 1
            node = dict(node)
            node["_pos"] = start
            node["_end"] = self.i
            return node
        if ch == ")":
            raise QueryError(start, 'Unexpected ")"')
        for kw in ("AND", "OR"):
            if self._keyword_at(kw):
                raise QueryError(start, f'Expected a term before "{kw}"')
        return self.parse_term()

    def _count_term(self, pos: int) -> None:
        self.terms += 1
        if self.terms > MAX_TERMS:
            raise QueryError(pos, f"Too many terms (max {MAX_TERMS})")

    def parse_term(self) -> dict:
        start = self.i
        if self._peek() == '"':
            s = self._read_phrase()
            self.tokens.append(Token("phrase", start, self.i))
            self._count_term(start)
            return {"t": "text", "phrase": True, "s": s, "_pos": start, "_end": self.i}

        m = _FIELD_RE.match(self.s, self.i)
        if m and m.end() < self.n and self.s[m.end()] == ":":
            name = m.group(0)
            self.i = m.end() + 1  # past the colon
            return self.parse_field_term(name, start, m.end())

        word = self._read_word()
        if word == "":
            raise QueryError(start, "Unexpected input")  # pragma: no cover
        self.tokens.append(Token("text", start, self.i))
        self._count_term(start)
        return {"t": "text", "phrase": False, "s": word, "_pos": start, "_end": self.i}

    def parse_field_term(self, name: str, start: int, colon_pos: int) -> dict:
        self.tokens.append(Token("field", start, colon_pos))
        self.tokens.append(Token("colon", colon_pos, colon_pos + 1))
        self._count_term(start)

        if name.lower() == "has":
            target_start = self.i
            target = self._read_word()
            spec = F.resolve(target) if target else None
            if spec is None:
                raise QueryError(target_start, f'Unknown field "{target}" after "has:"', F.suggest(target) if target else [])
            self.tokens.append(Token("value", target_start, self.i))
            return {"t": "term", "field": spec.name, "op": "has", "values": [], "_pos": start, "_end": self.i}

        spec = F.resolve(name)
        if spec is None:
            raise QueryError(start, f'Unknown field "{name}"', F.suggest(name))

        # value list: field:(a OR b)
        if self._peek() == "(":
            self.tokens.append(Token("paren", self.i, self.i + 1))
            self.i += 1
            values: List[dict] = []
            while True:
                self._ws()
                if self._peek() == ")":
                    if not values:
                        raise QueryError(self.i, f'Expected a value after "{spec.name}:"')
                    break
                if self.i >= self.n:
                    raise QueryError(self.i, 'Expected ")"')
                if values:
                    if not self._keyword_at("OR"):
                        raise QueryError(self.i, 'Expected "OR" or ")"')
                    self.tokens.append(Token("keyword", self.i, self.i + 2))
                    self.i += 2
                    self._ws()
                values.append(self._parse_scalar_token(spec))
            self.tokens.append(Token("paren", self.i, self.i + 1))
            self.i += 1
            if "any" not in spec.ops:
                raise QueryError(start, f'{spec.name} does not support "( OR )"')
            return {"t": "term", "field": spec.name, "op": "any", "values": values, "_pos": start, "_end": self.i}

        # comparison prefix
        for op in _CMP_OPS:
            if self.s.startswith(op, self.i):
                op_pos = self.i
                self.tokens.append(Token("op", self.i, self.i + len(op)))
                self.i += len(op)
                if self.i >= self.n or self.s[self.i].isspace() or self.s[self.i] == ")":
                    raise QueryError(op_pos, f'Expected a value after "{spec.name}:{op}"')
                if _CMP_NAME[op] not in spec.ops:
                    raise QueryError(op_pos, f'{spec.name} does not support "{op}"')
                lit = self._parse_scalar_token(spec)
                return {"t": "term", "field": spec.name, "op": _CMP_NAME[op], "values": [lit], "_pos": start, "_end": self.i}

        if self.i >= self.n or self.s[self.i].isspace() or self.s[self.i] == ")":
            raise QueryError(self.i, f'Expected a value after "{spec.name}:"')

        # range a..b (only for unquoted scalars)
        if self._peek() != '"':
            save = self.i
            word = self._read_word()
            if ".." in word and not word.startswith("..") and not word.endswith(".."):
                lo, hi = word.split("..", 1)
                if "range" not in spec.ops:
                    raise QueryError(save, f'{spec.name} does not support ".."')
                lo_lit = parse_scalar(spec, lo, False, save)
                hi_lit = parse_scalar(spec, hi, False, save + len(lo) + 2)
                self.tokens.append(Token("value", save, self.i))
                return {"t": "term", "field": spec.name, "op": "range", "values": [lo_lit, hi_lit], "_pos": start, "_end": self.i}
            self.i = save

        lit = self._parse_scalar_token(spec)
        op = "wild" if lit["kind"] == "wild" else "eq"
        return {"t": "term", "field": spec.name, "op": op, "values": [lit], "_pos": start, "_end": self.i}

    def _parse_scalar_token(self, spec: F.FieldSpec) -> dict:
        self._ws()
        start = self.i
        if self._peek() == '"':
            raw = self._read_phrase()
            self.tokens.append(Token("phrase", start, self.i))
            return parse_scalar(spec, raw, True, start)
        raw = self._read_word()
        if raw == "":
            raise QueryError(start, f'Expected a value after "{spec.name}:"')
        lit = parse_scalar(spec, raw, False, start)
        self.tokens.append(Token("value", start, self.i))
        return lit


def parse(text: str) -> ParseResult:
    """Parse a query. Raises ``QueryError`` with a character position."""
    if len(text) > MAX_LENGTH:
        raise QueryError(MAX_LENGTH, f"Query is too long (max {MAX_LENGTH} characters)")
    p = _Parser(text)
    root = p.parse()
    p.tokens.sort(key=lambda t: t.start)
    return ParseResult(ast={"v": AST_VERSION, "root": root}, tokens=p.tokens)


# ---------------------------------------------------------------------------
# Canonical form and helpers
# ---------------------------------------------------------------------------

def strip_positions(node: Any) -> Any:
    if isinstance(node, dict):
        return {k: strip_positions(v) for k, v in node.items() if not k.startswith("_")}
    if isinstance(node, list):
        return [strip_positions(x) for x in node]
    return node


def _lit_text(lit: dict) -> str:
    if lit["kind"] == "phrase":
        return '"' + lit["s"] + '"'
    if lit["kind"] == "word":
        return lit["s"]  # enum values normalise to their canonical spelling
    return lit["raw"]


def _node_text(node: dict, parent: Optional[str]) -> str:
    t = node["t"]
    if t == "text":
        return ('"' + node["s"] + '"') if node["phrase"] else node["s"]
    if t == "term":
        f = node["field"]
        op = node["op"]
        vals = node["values"]
        if op == "has":
            return f"has:{f}"
        if op == "any":
            return f"{f}:(" + " OR ".join(_lit_text(v) for v in vals) + ")"
        if op == "range":
            return f"{f}:{vals[0]['raw']}..{vals[1]['raw']}"
        prefix = {"gt": ">", "gte": ">=", "lt": "<", "lte": "<="}.get(op, "")
        return f"{f}:{prefix}{_lit_text(vals[0])}"
    if t == "not":
        inner = _node_text(node["c"], "not")
        return "NOT " + inner
    if t in ("and", "or"):
        joiner = " AND " if t == "and" else " OR "
        s = joiner.join(_node_text(c, t) for c in node["c"])
        # Parenthesise when precedence would change the meaning.
        if parent == "not" or (parent == "and" and t == "or"):
            return "(" + s + ")"
        return s
    raise ValueError(t)  # pragma: no cover


def canonical(ast: dict) -> str:
    root = ast.get("root")
    return _node_text(root, None) if root else ""


def top_level_terms(ast: dict) -> List[dict]:
    """The terms that are ANDed at the top level (for quick-filter chip state)."""
    root = ast.get("root")
    if not root:
        return []
    if root["t"] == "and":
        return [c for c in root["c"] if c["t"] in ("term", "text")]
    if root["t"] in ("term", "text"):
        return [root]
    return []


def mentions_field(ast: dict, name: str) -> bool:
    def walk(node: Optional[dict]) -> bool:
        if not node:
            return False
        t = node["t"]
        if t == "term":
            return node["field"] == name
        if t == "not":
            return walk(node["c"])
        if t in ("and", "or"):
            return any(walk(c) for c in node["c"])
        return False
    return walk(ast.get("root"))
