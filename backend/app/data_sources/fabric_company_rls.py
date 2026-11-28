"""Compile a deliberately bounded subset of Fabric SELECTs against live company grants."""
import re
from typing import Annotated

import sqlglot
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlglot import exp
from sqlglot.errors import ErrorLevel
from sqlglot.optimizer.scope import Scope, traverse_scope
from sqlglot.tokens import TokenType

from app.data_sources.fast.rls import Identity, resolve_source

_IDENT = r"[A-Za-z_][A-Za-z0-9_]{0,127}"
Identifier = Annotated[str, Field(pattern=rf"^{_IDENT}$")]
TableName = Annotated[str, Field(pattern=rf"^{_IDENT}\.{_IDENT}$")]


AttributeSource = Annotated[str, Field(pattern=r"^(?:user\.email|groups|roles|(?:profile|custom)\.[A-Za-z][A-Za-z0-9_.-]{0,63})$")]


class AttributeMatch(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    source: AttributeSource
    values: Annotated[tuple[str, ...], Field(min_length=1, max_length=256)]


class TableGrant(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    table: TableName
    company_column: Identifier
    source: Annotated[str, Field(min_length=1, max_length=256)] | None = None
    row_attribute: AttributeSource | None = None
    # A configured projection is closed to new source columns. None on a
    # column grants every authenticated reader of the row access to that field.
    columns: dict[Identifier, tuple[str, ...] | AttributeMatch | None] | None = None

    @model_validator(mode="after")
    def validate_projection(self):
        if bool(self.row_attribute) == bool(self.source):
            raise ValueError("Choose one row rule source")
        if self.columns is not None and len({c.casefold() for c in self.columns}) != len(self.columns):
            raise ValueError("Column names must be unique")
        return self

    def visible_columns(self, employee: str, identity: Identity | None = None) -> list[str] | None:
        if self.columns is None:
            return None
        return [column for column, readers in self.columns.items()
                if readers is None or (isinstance(readers, tuple) and employee.casefold() in
                                       {email.casefold() for email in readers})
                or (isinstance(readers, AttributeMatch) and identity and
                    set(resolve_source(readers.source, identity)).intersection(readers.values))]


class CompanyPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    grants_table: TableName | None = None
    employee_column: Identifier | None = None
    company_column: Identifier | None = None
    source_column: Identifier | None = None
    tables: Annotated[tuple[TableGrant, ...], Field(min_length=1)]

    @model_validator(mode="after")
    def distinct_tables(self):
        names = [item.table.casefold() for item in self.tables]
        if len(set(names)) != len(names) or (self.grants_table or "").casefold() in names:
            raise ValueError("Each protected table must be distinct from the grant table")
        columns = (self.employee_column, self.company_column, self.source_column)
        grants = [item.source for item in self.tables if item.source]
        if grants and (not self.grants_table or not all(columns)):
            raise ValueError("Entitlement rules need a complete grant table mapping")
        if grants and len({name.casefold() for name in columns}) != len(columns):
            raise ValueError("Employee, company and source require distinct grant columns")
        if len(set(grants)) != len(grants):
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
    "fetch": "direction count limit_options", "limitoptions": "rows",
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


def protect_query(sql: str, policy: CompanyPolicy, employee: str | None, *, identity: Identity | None = None, dialect: str = "fabric", grant_probe: bool = False):
    if dialect not in {"fabric", "tsql", "oracle", "postgres"}:
        raise ValueError("Unsupported policy dialect")
    if not employee or not employee.strip():
        raise ValueError("A verified employee identity is required")
    trees = sqlglot.parse(sql, read=dialect)
    if len(trees) != 1 or not isinstance(trees[0], (exp.Select, exp.Union)):
        raise ValueError("Expected one read-only query")
    tree = trees[0]
    if dialect == "postgres" and "%" in sql:
        raise ValueError("Unsupported literal in protected query")
    if dialect in {"postgres", "oracle"}:
        for ident in tree.find_all(exp.Identifier):
            if not ident.quoted:
                ident.set("this", ident.name.upper() if dialect == "oracle" else ident.name.lower())
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

    values = {}
    binding_count = 0

    def bind(value):
        nonlocal binding_count
        name = f"bow_policy_{binding_count}"
        binding_count += 1
        values[name] = value
        if dialect in {"fabric", "tsql"}:
            return exp.Parameter(this=exp.Var(this=name))
        return exp.Placeholder(this=name)

    def exact(value):
        if dialect in {"fabric", "tsql"}:
            return _exact_text(value)
        if dialect == "postgres":
            return exp.Collate(this=exp.cast(value, "TEXT"), expression=exp.to_identifier("C", quoted=True))
        # Binary comparison is independent of Oracle session NLS settings.
        return exp.Anonymous(this="UTL_RAW.CAST_TO_RAW", expressions=[value])

    for ref, rule in filters:
        key = exp.column(rule.company_column, table="r", quoted=True)
        if rule.row_attribute:
            allowed = resolve_source(rule.row_attribute, identity) if identity else []
            if len(allowed) > 256:
                raise ValueError("Too many policy values")
            predicate = (exact(key).isin(*[exact(bind(v)) for v in allowed])
                         if allowed else exp.EQ(this=exp.Literal.number(1), expression=exp.Literal.number(0)))
        else:
            grants = exp.to_table(policy.grants_table).as_("g")
            matches = exp.and_(
                exact(exp.column(policy.employee_column, table="g", quoted=True)).eq(exact(bind(employee))),
                exact(exp.column(policy.source_column, table="g", quoted=True)).eq(exact(bind(rule.source))),
            )
            if not grant_probe:
                granted = exp.column(policy.company_column, table="g", quoted=True)
                same_company = granted.eq(key) if dialect == "fabric" else exact(granted).eq(exact(key))
                matches = exp.and_(matches, same_company)
            predicate = exp.Exists(this=exp.select(exp.Literal.number(1)).from_(grants).where(matches))
        if grant_probe:
            if len(filters) != 1 or rule.row_attribute:
                raise ValueError("Unsupported grant probe")
            tree = exp.select(exp.Case().when(predicate, exp.Literal.number(1)).else_(exp.Literal.number(0)))
            if dialect == "oracle":
                tree = tree.from_("DUAL")
            break
        columns = rule.visible_columns(employee, identity)
        if columns == []:
            raise ValueError("No visible columns")
        projection = ["*"] if columns is None else [exp.column(c, table="r", quoted=True) for c in columns]
        rows = exp.select(*projection).from_(exp.to_table(rule.table).as_("r")).where(predicate)
        ref.replace(rows.subquery(ref.alias_or_name))
    statement = tree.sql(dialect=dialect, identify=True, comments=False, unsupported_level=ErrorLevel.RAISE)
    if dialect in {"fabric", "tsql"}:
        return _odbc_bindings(statement, values, binding_count)
    return statement, values


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
        dialect = getattr(clients[key], "policy_dialect", "fabric")
        trees = sqlglot.parse(sql, read=dialect)
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
        checked = tree.sql(dialect=dialect, identify=True, comments=False, unsupported_level=ErrorLevel.RAISE)
        return f"def generate_df(ds_clients, excel_files):\n    return ds_clients[{key!r}].execute_query({checked!r})"
    except Exception:
        raise ValueError(message) from None
