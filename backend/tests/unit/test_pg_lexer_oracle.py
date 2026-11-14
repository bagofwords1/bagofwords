"""The Postgres lexer agrees with Postgres's own parser.

Contract: for every query that Postgres's parser accepts and `lex_postgres`
does not refuse, the lexer's canonical text parses to the same thing as the
original: as many statements as the lexer counted, every string and
bit-string constant with the same value, the same tables, and the same parse
tree once source positions are set aside. The guard's own reading agrees
too: the tables sqlglot finds in the canonical text (the traversal table
scope uses, CTE references set aside) are the tables Postgres reads in the
original. Every query the lexer refuses falls in one of the forms it refuses
on purpose; a refusal outside those (say, an "unterminated" literal Postgres
read fine) is a disagreement too. A query Postgres rejects stays rejected:
when the lexer lets it through, Postgres rejects its canonical text as well.

The oracle is pglast (libpg_query, the server's own grammar and scanner),
a test-only dev extra that app/ never imports. The corpus is generated from
a fixed seed: literal kinds (standard, E'...', dollar-quoted, N/B/X), line
and nested block comments, quoted and unquoted names (non-ASCII ones
included), `;` in and out of strings, nested queries, and tokens written
right next to each other. Its whitespace is Postgres 15's; the oracle's
parser is a newer Postgres, and where the two differ (a vertical tab) the
disagreement is recorded as a known one below.
"""

from __future__ import annotations

import functools
import json
import os
import random
import re
import sys
from collections import Counter

import pytest

from app.ai.code_execution.pg_lexer import PgLexResult, lex_postgres
from app.ai.code_execution.sql_guard import ReadOnlyViolation
from app.ai.code_execution.sql_parser import parse_guard_sql
from app.ai.code_execution.table_scope import TableScopeViolation, tables_read

try:
    import pglast  # noqa: F401
except ImportError:
    if os.environ.get("CI"):
        pytest.fail("pglast (the dev extra) is not installed in CI: the Postgres lexer oracle must run",
                    pytrace=False)
    pytest.skip("pglast (the dev extra) is not installed", allow_module_level=True)
from pglast.parser import ParseError, parse_sql_json  # noqa: E402

BS = chr(92)
NL = chr(10)
CR = chr(13)
TAB = chr(9)
FF = chr(12)

SEED = 20261113
QUERIES = 8000
# The share of the corpus Postgres accepts and the lexer lets through.
# Tokens written right next to each other raise the share Postgres rejects
# or the lexer refuses (a tight `u&'x'` is a U& string), so the corpus is
# larger than the 5,000 the plan asks for and the floor is lower.
COMPARED_FLOOR = 0.5

# The forms the lexer refuses on purpose, by the message it gives. An
# unterminated literal or comment is not one of them: Postgres would not
# have parsed the query.
REFUSED_FORMS = {
    "string continued on the next line": "continues a string literal",
    "U& string or name": "U&'...' strings",
    "E-string byte escape for NUL or >= 0x80": "octal or hex escape for NUL or a byte",
    "E-string Unicode escape out of range": "Unicode escape for NUL, a surrogate|invalid Unicode escape",
    "bit or hex string with other digits": "bit string B'...' may hold|hex string X'...' may hold",
    "non-ASCII name read differently": "A name holds a character outside ASCII",
    "non-ASCII dollar tag read differently": "A dollar-quote tag holds a character outside ASCII",
    "string glued to a number": "written right after a number",
    "NUL character": "NUL character",
}

# Positions differ between the two texts by construction.
_POSITION_KEYS = frozenset({"location", "stmt_location", "stmt_len", "list_start", "list_end"})


# --------------------------------------------------------------- the oracle

def _parse(sql: str):
    """Postgres's parse of `sql` as plain data, or None if it rejects it."""
    try:
        return json.loads(parse_sql_json(sql))
    except ParseError:
        return None
    except UnicodeDecodeError as error:
        # pglast cannot decode the parser's error message when it quotes
        # part of a multi-byte character; a parse tree is never this.
        if not error.object.lstrip().startswith(b"{"):
            return None
        raise


def _statement_count(tree) -> int:
    return len(tree.get("stmts", []))


def _nodes(tree, kind: str):
    """Every `kind` node in the tree, in order."""
    if isinstance(tree, dict):
        for key, value in tree.items():
            if key == kind:
                yield value
            yield from _nodes(value, kind)
    elif isinstance(tree, list):
        for item in tree:
            yield from _nodes(item, kind)


def _constants(tree) -> list[tuple[str, str]]:
    """Every string and bit-string constant, as (kind, value), in order."""
    out = []
    for const in _nodes(tree, "A_Const"):
        for kind in ("sval", "bsval"):
            if kind in const:
                out.append((kind, const[kind][kind]))
    return out


def _tables(tree) -> set[tuple[str, str, str]]:
    return {(r.get("catalogname", ""), r.get("schemaname", ""), r.get("relname", "")) for r in _nodes(tree, "RangeVar")}


def _without_positions(tree):
    if isinstance(tree, dict):
        return {k: _without_positions(v) for k, v in tree.items() if k not in _POSITION_KEYS}
    if isinstance(tree, list):
        return [_without_positions(v) for v in tree]
    return tree


def _tables_read(tree) -> frozenset[tuple[str, ...]]:
    """The tables Postgres reads: every RangeVar except an unqualified one
    naming a CTE visible where it stands (a CTE is visible in its query and
    the queries nested in it; inside a CTE's body, only the CTEs before it,
    plus itself when the WITH is RECURSIVE). Names as Postgres folded them,
    as table scope compares Postgres names."""
    out = set()

    def walk(node, visible: frozenset[str]) -> None:
        if isinstance(node, list):
            for item in node:
                walk(item, visible)
            return
        if not isinstance(node, dict):
            return
        with_clause = node.get("withClause")
        if isinstance(with_clause, dict):
            ctes = [c["CommonTableExpr"] for c in with_clause.get("ctes", [])]
            names = [c["ctename"] for c in ctes]
            own = 1 if with_clause.get("recursive") else 0
            for i, cte in enumerate(ctes):
                walk(cte.get("ctequery"), visible | frozenset(names[:i + own]))
            visible = visible | frozenset(names)
        for key, value in node.items():
            if key == "withClause":
                continue
            if key == "RangeVar":
                ref = tuple(value.get(k) for k in ("catalogname", "schemaname", "relname") if value.get(k))
                if not (len(ref) == 1 and ref[0] in visible):
                    out.add(tuple(ref))
                continue
            walk(value, visible)

    walk(tree, frozenset())
    return frozenset(out)


@functools.cache
def _guard_tables(text: str) -> frozenset[tuple[str, ...]] | None:
    """The tables the guard finds in `text`: sqlglot's Postgres parse,
    walked by table scope's own `tables_read`. None when table scope
    refuses the text: sqlglot cannot read it, or `tables_read` refuses it
    (a WITH entry that names itself)."""
    try:
        statements = parse_guard_sql(text, "postgres")
    except Exception:
        return None
    try:
        return frozenset(ref for statement in statements if statement is not None
                         for ref in tables_read(statement, "postgres"))
    except TableScopeViolation:
        return None


def _disagreements(sql: str, lex: PgLexResult) -> list[str]:
    """How Postgres's reading of `lex.text`, and the guard's, differ from
    Postgres's reading of `sql`."""
    original = _parse(sql)
    assert original is not None, "only queries Postgres accepts are compared"
    found = []
    if _statement_count(original) != lex.statements:
        found.append(f"statements: Postgres {_statement_count(original)}, lexer {lex.statements}")
    guard = _guard_tables(lex.text)
    if guard is not None and guard != _tables_read(original):
        found.append(f"guard tables: Postgres {sorted(_tables_read(original))}, sqlglot on the canonical "
                     f"text {sorted(guard)}")
    canonical = _parse(lex.text)
    if canonical is None:
        return found + ["the canonical text does not parse"]
    if _constants(original) != _constants(canonical):
        found.append(f"literals: Postgres {_constants(original)}, canonical text {_constants(canonical)}")
    if _tables(original) != _tables(canonical):
        found.append(f"tables: Postgres {sorted(_tables(original))}, canonical text {sorted(_tables(canonical))}")
    if not found and _without_positions(original) != _without_positions(canonical):
        found.append("parse trees differ")
    return found


def refused_form(error: ReadOnlyViolation) -> str | None:
    message = str(error)
    for form, pattern in REFUSED_FORMS.items():
        if re.search(pattern, message):
            return form
    return None


# --------------------------------------------------------------- the corpus

NAMES = ["orders", "customers", "t1", "_x", "a$b", "order_items", "x2y", "Amount", "u", "U",
         '"Mixed Case"', '"with""quote"', '"semi;colon"', '"dash--dash"', '"slash/*star"',
         "\"it's\"", '"$$"', "caf" + chr(0xE9), chr(0x5E9) + "x"]
# Names Postgres reads as one name and sqlglot may not: a non-ASCII space
# inside, a non-ASCII digit at the start.
ODD_NAMES = ["a" + chr(0xA0) + "b", "a" + chr(0x2003) + "b", "x" + chr(0x3000) + "y",
             chr(0xB2) + "x", chr(0x663) + "$x$"]
OPERATORS = ["||", "=", "+", "<>", "LIKE", "&"]
# Operators often written with no space around them.
SYMBOL_OPERATORS = ["||", "=", "+", "<>", "&"]
NUMBERS = ["1", "2.5", ".5", "1e3", "10", "$1"]
SCHEMAS = ["public", "sales", '"Sales Data"']
# Characters and escapes a literal body is built from (a standard body's
# quote is written doubled).
BODY = ["a", "Z", " ", "''", BS, ";", "--", "/*", "*/", '"', "$", "$$", NL, TAB, chr(0xE9), chr(0x65E5),
        "{#", ":x", "%", "E''", "x", "0"]
E_ESCAPES = [BS + BS, BS + "'", "''", BS + "n", BS + "t", BS + "b", BS + "f", BS + "r", BS + "101", BS + "7",
             BS + "x41", BS + "x4", BS + "u00e9", BS + "U0001F600", BS + "q", BS + ";", BS + '"']
# Escapes the lexer refuses: some Postgres rejects too, some it reads (the
# bytes of a valid UTF-8 character, a valid surrogate pair).
E_REFUSED = [BS + "x80", BS + "200", BS + "0", BS + "ud800", BS + "u12",
             BS + "xc3" + BS + "xa9", BS + "303" + BS + "251", BS + "ud83d" + BS + "ude00"]
# A dollar tag holding a no-break space (Postgres reads it as a tag character).
NBSP_TAG = "a" + chr(0xA0) + "b"
TAGS = ["", "q", "tag", "a1", "_x"]
COMMENT_TEXT = ["note", " ", "'", '"', "$$", ";", "E'", "--", "drop", chr(0xE9), "-", "$x$"]
# Each function with the argument counts it takes (so sqlglot reads the
# call, and the guard's reading is compared).
FUNCTIONS = {"lower": (1, 1), "upper": (1, 1), "coalesce": (1, 3), "length": (1, 1), "date_part": (2, 2),
             "concat": (1, 3)}


def _is_word(c: str) -> bool:
    return c.isalnum() or c in "_$" or c >= chr(0x80)


def _word_end(text: str) -> bool:
    return bool(text) and _is_word(text[-1])


def _word_start(text: str) -> bool:
    return bool(text) and _is_word(text[0])


class _Corpus:
    def __init__(self, seed: int):
        self.rng = random.Random(seed)

    def pick(self, items):
        return self.rng.choice(items)

    def chance(self, p: float) -> bool:
        return self.rng.random() < p

    # separators -------------------------------------------------------

    def comment_text(self) -> str:
        return "".join(self.pick(COMMENT_TEXT) for _ in range(self.rng.randint(0, 4)))

    def block_comment(self, depth: int = 0) -> str:
        inner = self.block_comment(depth + 1) if depth < 2 and self.chance(0.3) else ""
        return "/*" + self.comment_text().replace("*", "").replace("/", "") + inner + "*/"

    def sep(self) -> str:
        roll = self.rng.random()
        if roll < 0.10:
            return ""  # the two tokens written right next to each other
        if roll < 0.62:
            return " "
        if roll < 0.80:
            return "".join(self.pick([" ", NL, TAB, CR + NL, FF]) for _ in range(self.rng.randint(1, 3)))
        if roll < 0.89:
            return " --" + self.comment_text().replace("-", "") + self.pick([NL, CR])
        return " " + self.block_comment() + self.pick(["", " ", NL])

    def join(self, *parts: str) -> str:
        out = parts[0]
        for part in parts[1:]:
            sep = self.sep()
            if not sep and _word_end(out) and _word_start(part):
                sep = " "  # written together, the two words would be one
            out += sep + part
        return out

    # literals ---------------------------------------------------------

    def body(self, pieces) -> str:
        return "".join(self.pick(pieces) for _ in range(self.rng.randint(0, 6)))

    def standard(self) -> str:
        return "'" + self.body(BODY) + "'"

    def e_string(self) -> str:
        escapes = E_ESCAPES + ([self.pick(E_REFUSED)] if self.chance(0.08) else [])
        pieces = [p for p in BODY if p not in ("''", BS)] + escapes
        return self.pick(["E", "e"]) + "'" + self.body(pieces) + "'"

    def literal(self) -> str:
        roll = self.rng.random()
        if roll < 0.30:
            return self.standard()
        if roll < 0.55:
            return self.e_string()
        if roll < 0.75:
            tag = "$" + self.pick(TAGS + (["t" + chr(0xE9), NBSP_TAG] if self.chance(0.1) else [])) + "$"
            while True:
                body = self.body(BODY + ["'"])
                if (body + tag).find(tag) == len(body):
                    return tag + body + tag
        if roll < 0.82:
            return self.pick(["N", "n"]) + "'" + self.body([p for p in BODY if p != BS]) + "'"
        if roll < 0.88:
            digits = "01" if self.chance(0.95) else "012"
            return "B'" + "".join(self.pick(digits) for _ in range(self.rng.randint(0, 6))) + "'"
        if roll < 0.93:
            digits = "0123456789abcdefABCDEF" if self.chance(0.95) else "0fg"
            return "X'" + "".join(self.pick(digits) for _ in range(self.rng.randint(0, 6))) + "'"
        if roll < 0.97:
            return self.pick(["date '2026-01-01'", "interval '7 days'", "timestamp '2026-01-01 10:00'"])
        if roll < 0.985:
            # Two literals on two lines: Postgres joins them into one, the
            # second read with the first one's escape rules.
            first = self.standard() if self.chance(0.5) else self.e_string()
            return first + self.pick([NL, " " + NL, " --c" + NL]) + self.standard()
        return "U&'" + self.body(["a", "b"]) + "'"

    # names and expressions ----------------------------------------------

    def name(self) -> str:
        if self.chance(0.005):
            return self.pick(ODD_NAMES)
        return self.pick(NAMES)

    def table(self) -> str:
        if self.chance(0.4):
            return self.pick(SCHEMAS) + "." + self.name()
        return self.name()

    def expr(self, depth: int = 0) -> str:
        roll = self.rng.random() if depth < 3 else self.rng.random() * 0.5
        if roll < 0.25:
            return self.literal()
        if roll < 0.40:
            return self.name() if self.chance(0.6) else self.name() + "." + self.name()
        if roll < 0.50:
            if self.chance(0.1):
                # A number or literal followed by a literal: Postgres rejects
                # it, and the lexer must not make it a query Postgres runs.
                return self.pick([self.pick(NUMBERS), self.literal()]) + self.sep() + self.literal()
            return self.pick(NUMBERS)
        if roll < 0.62:
            function = self.pick(sorted(FUNCTIONS))
            args = [self.expr(depth + 1) for _ in range(self.rng.randint(*FUNCTIONS[function]))]
            return self.join(function, "(", *self._commas(args), ")")
        if roll < 0.74:
            return self.operation(self.expr(depth + 1), self.expr(depth + 1))
        if roll < 0.82:
            return self.join("(", self.select(depth + 1), ")")
        if roll < 0.90:
            return self.join("CASE", "WHEN", self.expr(depth + 1), "THEN", self.expr(depth + 1),
                             "ELSE", self.expr(depth + 1), "END")
        if roll < 0.95:
            return self.join(self.expr(depth + 1), "::", "text")
        return self.join("ARRAY", "[", self.expr(depth + 1), ",", self.expr(depth + 1), "]")

    def operation(self, left: str, right: str) -> str:
        op = self.pick(OPERATORS)
        if op in SYMBOL_OPERATORS and self.chance(0.4):
            return left + op + right
        return self.join(left, op, right)

    def condition(self, depth: int) -> str:
        """A WHERE condition, often a name compared to a literal."""
        if self.chance(0.5):
            return self.expr(depth)
        return self.operation(self.name(), self.literal() if self.chance(0.7) else self.expr(depth))

    def _commas(self, items: list[str]) -> list[str]:
        out = []
        for i, item in enumerate(items):
            if i:
                out.append(",")
            out.append(item)
        return out

    def select(self, depth: int = 0) -> str:
        items = []
        for _ in range(self.rng.randint(1, 3)):
            item = self.expr(depth)
            if self.chance(0.4):
                item = self.join(item, "AS", self.name())
            items.append(item)
        parts = ["SELECT", *(["DISTINCT"] if self.chance(0.1) else []), *self._commas(items)]
        if self.chance(0.8):
            source = self.table() if depth > 1 or self.chance(0.75) else self.join(
                "(", self.select(depth + 1), ")", self.name())
            parts += ["FROM", source]
            if self.chance(0.3):
                parts += ["JOIN", self.table(), "ON", self.condition(depth + 1)]
            elif self.chance(0.2):
                parts += [",", self.table()]
        if self.chance(0.5):
            parts += ["WHERE", self.condition(depth + 1)]
        union = depth < 2 and self.chance(0.1)
        if not union and self.chance(0.15):
            parts += ["ORDER", "BY", self.expr(depth + 1), *(["DESC"] if self.chance(0.5) else [])]
        if not union and self.chance(0.15):
            parts += ["LIMIT", self.pick(["1", "10", "100"])]
        query = self.join(*parts)
        if union:
            query = self.join(query, "UNION", "ALL", self.select(depth + 1))
        if depth == 0 and self.chance(0.15):
            query = self.join("WITH", self.name(), "AS", "(", self.select(depth + 1), ")", query)
        return query

    def query(self) -> str:
        statements = [self.select() for _ in range(self.rng.choice([1, 1, 1, 2, 3]))]
        text = statements[0]
        for statement in statements[1:]:
            text += self.sep() + self.pick([";", ";;", "; ;"]) + self.sep() + statement
        if self.chance(0.2):
            text = self.pick([";", " ;", "; "]) + self.sep() + text
        if self.chance(0.3):
            text += self.pick([";", " ;", ";;"])
        if self.chance(0.2):
            text += self.sep() + self.pick(["-- end", "/* end */"])
        return text


def generated_corpus() -> list[str]:
    corpus = _Corpus(SEED)
    return [corpus.query() for _ in range(QUERIES)]


# --------------------------------------------------------------- the checks

def test_the_canonical_text_reads_the_same_to_postgres_on_the_generated_corpus():
    corpus = generated_corpus()
    assert len(corpus) >= 5000
    compared, rejected, unread_by_sqlglot = 0, 0, 0
    # Refusals of queries Postgres accepts, and of queries it rejects.
    refused: Counter[str] = Counter()
    refused_rejected: Counter[str] = Counter()
    disagreements = []
    for sql in corpus:
        accepted = _parse(sql) is not None
        rejected += not accepted
        try:
            lex = lex_postgres(sql)
        except ReadOnlyViolation as error:
            form = refused_form(error)
            if not accepted:
                refused_rejected[form or "other"] += 1
            elif form is None:
                disagreements.append((sql, [f"refused outside the refused forms: {error}"]))
            else:
                refused[form] += 1
            continue
        if not accepted:
            if _parse(lex.text) is not None:
                disagreements.append((sql, ["Postgres rejects the query but parses its canonical text"]))
            continue
        compared += 1
        unread_by_sqlglot += _guard_tables(lex.text) is None
        found = _disagreements(sql, lex)
        if found:
            disagreements.append((sql, found))
    summary = (f"compared={compared} rejected_by_postgres={rejected} unread_by_sqlglot={unread_by_sqlglot} "
               f"refused={dict(refused)} refused_of_rejected={dict(refused_rejected)}")
    assert not disagreements, f"{len(disagreements)} disagreements ({summary}); first: " + "; ".join(
        f"{sql!r}: {found}" for sql, found in disagreements[:5])
    # The comparison is not vacuous: most of the corpus reaches it, sqlglot
    # reads nearly all of that, and the refused forms are exercised, the
    # ones only adjacent tokens reach included.
    assert compared >= COMPARED_FLOOR * len(corpus), summary
    assert unread_by_sqlglot <= 0.1 * compared, summary
    assert len(refused) >= 6, summary
    fired = refused + refused_rejected
    for form in ("string glued to a number", "string continued on the next line", "non-ASCII name read differently"):
        assert fired[form] > 0, f"{form!r} never refused ({summary})"


# --------------------------------------------------------------- the oracle can fail

def _misread(sql: str, text: str, statements: int = 1) -> PgLexResult:
    """A lexer result that reads `sql` as `text`."""
    return PgLexResult(text=text, code_only=text, statements=statements, has_backslash_literal=False)


def test_the_oracle_reports_a_miscounted_statement():
    sql = "SELECT E'a" + BS + "'; SELECT 1' AS v"
    assert _disagreements(sql, _misread(sql, "SELECT 'a'; SELECT 1", statements=2))


def test_the_oracle_reports_a_literal_read_with_another_value():
    sql = "SELECT E'a" + BS + "'b' AS v"
    assert any("literal" in d for d in _disagreements(sql, _misread(sql, "SELECT 'a" + BS + "''b' AS v")))


def test_the_oracle_reports_a_table_the_canonical_text_lost():
    sql = "SELECT 'x' AS v FROM orders, secret"
    assert any("tables" in d for d in _disagreements(sql, _misread(sql, "SELECT 'x' AS v FROM orders")))


def test_the_oracle_reports_a_canonical_text_postgres_cannot_parse():
    sql = "SELECT $$a'b$$ AS v"
    assert _disagreements(sql, _misread(sql, "SELECT 'a'b' AS v"))


def test_the_oracle_reports_a_table_the_guard_reads_differently():
    # Postgres reads one table here; sqlglot reads another, with an alias.
    sql = "SELECT 1 FROM a" + chr(0xA0) + "b"
    assert any("guard tables" in d for d in _disagreements(sql, _misread(sql, sql)))


def test_the_oracle_reads_the_guard_tables_with_table_scope_s_own_walk():
    """No copy of table scope's walk here: the oracle calls the public one."""
    from app.ai.code_execution import table_scope

    assert not hasattr(sys.modules[__name__], "_resolves_to_cte")
    sql = "WITH o AS (SELECT 1) SELECT * FROM o, Public.Items"
    [statement] = parse_guard_sql(sql, "postgres")
    assert _guard_tables(sql) == frozenset(table_scope.tables_read(statement, "postgres")) == {("public", "items")}


def test_the_oracle_sets_aside_only_the_cte_references_visible_where_they_stand():
    # The CTE `x` is a CTE reference in the outer query; `orders` in the
    # second statement is a table, since the WITH covers only the first.
    sql = "WITH x AS (SELECT * FROM y) SELECT * FROM x; WITH orders AS (SELECT 1) SELECT 2; SELECT * FROM orders"
    assert _tables_read(_parse(sql)) == {("y",), ("orders",)}
    assert _guard_tables(sql) == {("y",), ("orders",)}
    assert _disagreements(sql, lex_postgres(sql)) == []


def test_the_oracle_counts_a_with_entry_that_names_itself_as_refused():
    # Postgres reads `x` inside its own (non-recursive) body as the table;
    # table scope refuses an entry that names itself.
    sql = "WITH x AS (SELECT * FROM x) SELECT * FROM x"
    assert _tables_read(_parse(sql)) == {("x",)}
    assert _guard_tables(sql) is None


def test_the_oracle_accepts_the_lexer_on_a_known_good_query():
    sql = "SELECT E'it" + BS + "'s' AS v, $q$a;b$q$ AS w /* c */ FROM orders -- x" + NL
    assert _disagreements(sql, lex_postgres(sql)) == []


# --------------------------------------------------------------- known disagreements

VT = chr(11)


@pytest.mark.xfail(strict=True, raises=AssertionError, reason=(
    "known disagreement: the lexer models Postgres 15, where a vertical tab is not whitespace, so a "
    "vertical tab alone after the last `;` counts as one more statement and the text is refused; the "
    "oracle's parser (Postgres 18) reads it as whitespace. Over-refusal only."))
@pytest.mark.parametrize("sql", ["SELECT 1;" + VT, "SELECT 1;" + VT + ";"])
def test_a_vertical_tab_after_the_last_statement_reads_as_whitespace(sql):
    assert _disagreements(sql, lex_postgres(sql)) == []


def test_a_refusal_outside_the_refused_forms_has_no_form():
    assert refused_form(ReadOnlyViolation("The query has an unterminated string literal. Close it.")) is None
    assert refused_form(ReadOnlyViolation("A string literal is written right after a number. ...")) is not None
