"""Compile a deliberately bounded subset of Fabric SELECTs against live company grants."""
import re
from typing import Annotated

import sqlglot
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlglot import exp
from sqlglot.errors import ErrorLevel
from sqlglot.optimizer.scope import Scope, traverse_scope
from sqlglot.tokens import TokenType

_IDENT = r"[A-Za-z_][A-Za-z0-9_]{0,127}"
Identifier = Annotated[str, Field(pattern=rf"^{_IDENT}$")]
TableName = Annotated[str, Field(pattern=rf"^{_IDENT}\.{_IDENT}$")]


class TableGrant(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    table: TableName
    company_column: Identifier
    source: Annotated[str, Field(min_length=1, max_length=256)]


class CompanyPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    grants_table: TableName
    employee_column: Identifier
    company_column: Identifier
    source_column: Identifier
    tables: Annotated[tuple[TableGrant, ...], Field(min_length=1)]

    @model_validator(mode="after")
    def distinct_tables(self):
        names = [item.table.casefold() for item in self.tables]
        if len(set(names)) != len(names) or self.grants_table.casefold() in names:
            raise ValueError("Each protected table must be distinct from the grant table")
        columns = (self.employee_column, self.company_column, self.source_column)
        if len({name.casefold() for name in columns}) != len(columns):
            raise ValueError("Employee, company and source require distinct grant columns")
        if len({item.source for item in self.tables}) != len(self.tables):
            raise ValueError("Each protected table requires its own source value")
        return self


# Validate populated argument slots too. Some SQL modifiers are scalar strings
# on otherwise allowed nodes, so checking node classes alone isn't sufficient.
_FIELDS = {
    "select": "expressions from joins where group having order limit distinct with",
    "table": "this db alias", "from": "this", "column": "this table",
    "identifier": "this quoted", "literal": "this is_string", "star": "",
    "tablealias": "this", "alias": "this alias", "join": "this side kind on",
    "subquery": "this alias", "cte": "this alias", "with": "expressions",
    "union": "this expression distinct with", "distinct": "expressions",
    "limit": "expression", "group": "expressions", "order": "expressions",
    "ordered": "this desc nulls_first", "count": "this big_int",
    "in": "this expressions query", "between": "this low high", "null": "",
}
for _name in "where having paren not sum avg min max exists".split():
    _FIELDS[_name] = "this"
for _name in "eq neq gt gte lt lte is and or".split():
    _FIELDS[_name] = "this expression"
_ALLOWED = {name: set(fields.split()) for name, fields in _FIELDS.items()}


def _validate(tree):
    for node in tree.walk():
        fields = _ALLOWED.get(node.key)
        if fields is None or any(k not in fields and v is not None and v != [] and v is not False
                                 for k, v in node.args.items()):
            raise ValueError("Unsupported company-filtered query")
        if isinstance(node, exp.Identifier) and not re.fullmatch(_IDENT, node.name):
            raise ValueError("Unsupported identifier")
        if isinstance(node, exp.Literal) and not node.is_string and not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", node.this):
            raise ValueError("Unsupported numeric literal")
        if isinstance(node, exp.Join) and (node.side not in ("", "LEFT", "RIGHT", "FULL")
                                           or node.kind not in ("", "INNER", "OUTER", "CROSS")):
            raise ValueError("Unsupported join")


def _odbc_bindings(statement, values, expected_count):
    """Emit positional ODBC parameters in final SQL order, including hoisted CTEs."""
    tokens = sqlglot.tokenize(statement, read="fabric")
    parts, bindings = [], []
    offset = 0
    for index, token in enumerate(tokens):
        if token.token_type != TokenType.PARAMETER:
            continue
        name = tokens[index + 1]
        if name.token_type != TokenType.VAR or name.text not in values or name.start != token.end + 1:
            raise ValueError("Unclassified policy binding")
        parts.extend((statement[offset:token.start], "?"))
        bindings.append(values[name.text])
        offset = name.end + 1
    if len(bindings) != expected_count:
        raise ValueError("Incomplete policy binding")
    parts.append(statement[offset:])
    return "".join(parts), tuple(bindings)


def _exact_text(value):
    # Explicit binary collation keeps case and accents significant even in a
    # warehouse using a linguistic, case-insensitive default.
    # A terminator also distinguishes trailing blanks under T-SQL equality.
    terminated = exp.Anonymous(this="CONCAT", expressions=[value, exp.Literal.string("|")])
    return exp.Collate(this=terminated, expression=exp.Var(this="Latin1_General_100_BIN2_UTF8"))


def _grant_key(column, parameter):
    value = exp.Parameter(this=exp.Var(this=parameter))
    return _exact_text(exp.column(column, table="g", quoted=True)).eq(_exact_text(value))


def protect_query(sql: str, policy: CompanyPolicy, employee: str | None) -> tuple[str, tuple]:
    if not employee or not employee.strip():
        raise ValueError("A verified employee identity is required")
    trees = sqlglot.parse(sql, read="fabric")
    if len(trees) != 1 or not isinstance(trees[0], (exp.Select, exp.Union)):
        raise ValueError("Expected one read-only query")
    tree = trees[0]
    _validate(tree)
    rules = {item.table: item for item in policy.tables}
    classified = set()
    filters = []
    for scope in traverse_scope(tree):
        for ref, source in scope.selected_sources.values():
            if isinstance(ref, exp.Table):
                classified.add(id(ref))
                if isinstance(source, Scope) and not ref.db and not ref.catalog:
                    continue
                key = f"{ref.db}.{ref.name}"
                if not isinstance(source, exp.Table) or key not in rules:
                    raise ValueError("Table outside company policy")
                filters.append((ref, rules[key]))
    if not filters or classified != {id(t) for t in tree.find_all(exp.Table)}:
        raise ValueError("Unclassified company-filtered query")

    values = {"bow_employee": employee}
    for index, (ref, rule) in enumerate(filters):
        source_key = f"bow_source_{index}"
        values[source_key] = rule.source
        grants = exp.to_table(policy.grants_table).as_("g")
        matches = exp.and_(
            _grant_key(policy.employee_column, "bow_employee"),
            _grant_key(policy.source_column, source_key),
            exp.column(policy.company_column, table="g", quoted=True).eq(
                exp.column(rule.company_column, table="r", quoted=True)),
        )
        exists = exp.Exists(this=exp.select(exp.Literal.number(1)).from_(grants).where(matches))
        rows = exp.select("*").from_(exp.to_table(rule.table).as_("r")).where(exists)
        ref.replace(rows.subquery(ref.alias_or_name))
    statement = tree.sql(dialect="fabric", identify=True, comments=False, unsupported_level=ErrorLevel.RAISE)
    return _odbc_bindings(statement, values, 2 * len(filters))


def shared_query_code(code, clients):
    """Rebuild a supported saved report read using only a protected client and physical columns.

    Replaying arbitrary Python would let baked-in values from an earlier author run
    survive even if every new SQL statement had the right employee binding.
    """
    import ast

    message = "This report needs a live company-filtered table query"
    try:
        module = ast.parse(code)
        match module.body:
            case [ast.FunctionDef(name="generate_df", body=[ast.Return(value=ast.Call(
                    func=ast.Attribute(value=ast.Subscript(value=ast.Name(id="ds_clients"),
                                                           slice=ast.Constant(value=key)),
                                       attr="execute_query"),
                    args=[ast.Constant(value=sql)], keywords=[]))])]:
                pass
            case _:
                raise ValueError(message)
        if not isinstance(key, str) or not isinstance(sql, str):
            raise ValueError(message)
        if getattr(clients.get(key), "company_filtered", False) is not True:
            raise ValueError(message)
        trees = sqlglot.parse(sql, read="fabric")
        if len(trees) != 1 or not isinstance(trees[0], exp.Select):
            raise ValueError(message)
        tree = trees[0]
        # Only physical source fields can provide output values and headings.
        # Limit projections to the ordinary saved table view until other
        # visualizations have a verified way to regenerate their labels.
        if (set(k for k, v in tree.args.items() if v) - {"expressions", "from", "where", "order", "limit"}
                or len(list(tree.find_all(exp.Select))) != 1
                or len(list(tree.find_all(exp.Table))) != 1
                or not all(isinstance(col, (exp.Column, exp.Star)) for col in tree.expressions)):
            raise ValueError(message)
        _validate(tree)
        checked = tree.sql(dialect="fabric", identify=True, comments=False, unsupported_level=ErrorLevel.RAISE)
        return f"def generate_df(ds_clients, excel_files):\n    return ds_clients[{key!r}].execute_query({checked!r})"
    except Exception:
        raise ValueError(message) from None
