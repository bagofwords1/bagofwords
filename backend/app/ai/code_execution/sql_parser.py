"""Shared SQL parsing for the read-only and table-access guards.

sqlglot 27's SQLite parser inherits the standard SQL IS predicate parser,
which accepts literals but not SQLite's general right-hand expressions.
Keep this compatibility extension local to guard parsing; never rewrite the
query sent to the source or mutate sqlglot's global SQLite dialect.
"""

import sqlglot
from sqlglot import exp
from sqlglot.dialects.sqlite import SQLite
from sqlglot.tokens import TokenType


class _SQLiteGuardDialect(SQLite):
    class Parser(SQLite.Parser):
        def _parse_is(self, this):
            # SQLite permits any expression on the RHS of IS [NOT], including
            # columns, functions and subqueries. Parse them into the tree so
            # both guards can inspect nested reads/writes as usual.
            # https://www.sqlite.org/lang_expr.html#operators_and_parse_affecting_attributes
            negate = self._match(TokenType.NOT)
            if self._match_text_seq("DISTINCT", "FROM"):
                kind = exp.NullSafeEQ if negate else exp.NullSafeNEQ
                return self.expression(kind, this=this, expression=self._parse_bitwise())

            predicate = self.expression(exp.Is, this=this, expression=self._parse_bitwise())
            return self.expression(exp.Not, this=predicate) if negate else predicate


def parse_guard_sql(sql: str, dialect: str) -> list[exp.Expression | None]:
    """Parse the original SQL, preserving every node for security inspection."""
    if dialect == "sqlite":
        return _SQLiteGuardDialect().parse(sql)
    return sqlglot.parse(sql, read=dialect)
